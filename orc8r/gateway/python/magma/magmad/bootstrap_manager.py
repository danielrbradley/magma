"""
Copyright (c) 2016-present, Facebook, Inc.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. An additional grant
of patent rights can be found in the PATENTS file in the same directory.
"""
# pylint: disable=broad-except

import datetime
import enum
import logging

import grpc
import os
import snowflake
from cryptography.exceptions import InternalError
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import \
    decode_dss_signature
from google.protobuf.duration_pb2 import Duration
from orc8r.protos.bootstrapper_pb2 import ChallengeKey, Response
from orc8r.protos.bootstrapper_pb2_grpc import BootstrapperStub
from orc8r.protos.certifier_pb2 import CSR
from orc8r.protos.identity_pb2 import AccessGatewayID, Identity

import magma.common.cert_utils as cert_utils
from magma.common.cert_validity import cert_is_invalid
from magma.common.rpc_utils import grpc_async_wrapper
from magma.common.sdwatchdog import SDWatchdogTask
from magma.common.service_registry import ServiceRegistry
from magma.configuration.service_configs import load_service_config
from magma.magmad.metrics import BOOTSTRAP_EXCEPTION


class BootstrapError(Exception):
    pass


@enum.unique
class BootstrapState(enum.Enum):
    INITIAL = 0
    BOOTSTRAPPING = 1
    SCHEDULED_BOOTSTRAP = 2
    SCHEDULED_CHECK = 3
    IDLE = 4


class BootstrapManager(SDWatchdogTask):
    """
    Bootstrap the gateway by contacting the controller.

    Bootstrap manager responds to the challenge from the controller to
    verify the device. As a result of the bootstrap process, the
    gateways' session certs would be written to /var/opt/magma/certs.
    Before the session certs expire, bootstrap would make sure we
    fetch new certs by maintaining a timer internally.
    """
    # delay in asyncio should not exceed one day
    PERIODIC_BOOTSTRAP_CHECK_INTERVAL = datetime.timedelta(hours=1)
    PREEXPIRY_BOOTSTRAP_INTERVAL = datetime.timedelta(hours=20)
    SHORT_BOOTSTRAP_RETRY_INTERVAL = datetime.timedelta(seconds=30)
    LONG_BOOTSTRAP_RETRY_INTERVAL = datetime.timedelta(minutes=1)

    def __init__(self, service, bootstrap_success_cb):
        super().__init__(
            self.PERIODIC_BOOTSTRAP_CHECK_INTERVAL.total_seconds(),
            service.loop
        )

        control_proxy_config = load_service_config('control_proxy')

        self._challenge_key_file \
            = service.config['bootstrap_config']['challenge_key']
        self._hw_id = snowflake.snowflake()
        self._gateway_key_file = control_proxy_config['gateway_key']
        self._gateway_cert_file = control_proxy_config['gateway_cert']
        self._gateway_key = None
        self._state = BootstrapState.INITIAL
        self._bootstrap_success_cb = bootstrap_success_cb

        # give some margin on watchdog check interval
        self.set_timeout(self._interval * 1.1)

    def start_bootstrap_manager(self):
        self.start()
        self._maybe_create_challenge_key()

    def stop_bootstrap_manager(self):
        self._state = BootstrapState.IDLE
        self.stop()

    async def _run(self):
        if self._state == BootstrapState.INITIAL:
            await self._bootstrap_check()
        elif self._state == BootstrapState.BOOTSTRAPPING:
            pass
        elif self._state == BootstrapState.SCHEDULED_BOOTSTRAP:
            await self._bootstrap_now()
        elif self._state == BootstrapState.SCHEDULED_CHECK:
            await self._bootstrap_now()
        elif self._state == BootstrapState.IDLE:
            pass


    async def on_checkin_fail(self, err_code):
        """Checks for invalid certificate as cause for checkin failures"""
        if err_code == grpc.StatusCode.PERMISSION_DENIED:
            # Immediately bootstrap if the error is PERMISSION_DENIED
            return await self.bootstrap()
        proxy_config = ServiceRegistry.get_proxy_config()
        host = proxy_config['cloud_address']
        port = proxy_config['cloud_port']
        certfile = proxy_config['gateway_cert']
        keyfile = proxy_config['gateway_key']

        not_valid = await \
            cert_is_invalid(host, port, certfile, keyfile, self._loop)
        await self._cert_is_invalid_done(not_valid)
        return not_valid  # for testing

    async def bootstrap(self):
        """Public Interface to start a bootstrap

        1. If the device is bootstrapping, do nothing
        2. If there is something scheduled, put it in idle so the run loop is
           paused until this _bootstrap_now is complete
        3. run _bootstrap_now
        """
        if self._state is BootstrapState.BOOTSTRAPPING:
            return

        if self._state in [BootstrapState.SCHEDULED_CHECK,
                           BootstrapState.SCHEDULED_BOOTSTRAP]:
            self._state = BootstrapState.IDLE
        await self._bootstrap_now()

    async def _cert_is_invalid_done(self, not_valid):
        if not_valid:
            logging.info('Bootstrapping due to invalid cert')
            await self._bootstrap_now()
        else:
            logging.error('Checkin failure likely not due to invalid cert')

    def _maybe_create_challenge_key(self):
        """Generate key the first time it runs if key does not exist"""
        if not os.path.exists(self._challenge_key_file):
            logging.info('Generating challenge key and written into %s',
                         self._challenge_key_file)
            challenge_key = ec.generate_private_key(
                ec.SECP384R1(), default_backend())
            cert_utils.write_key(challenge_key, self._challenge_key_file)

    async def _bootstrap_check(self):
        """Check whether bootstrap is need

        Check whether cert is present and still valid
        If so, a future _bootstrap_check will be scheduled.
        Otherwise _bootstrap_now will be called immediately
        """
        # flag to ensure the loop is still running, successfully or not
        self.heartbeat()

        try:
            cert = cert_utils.load_cert(self._gateway_cert_file)
        except (IOError, ValueError):
            logging.info('Cannot load a proper cert, start bootstrapping')
            await self._bootstrap_now()
            return

        now = datetime.datetime.utcnow()
        if now + self.PREEXPIRY_BOOTSTRAP_INTERVAL > cert.not_valid_after:
            logging.info(
                'Certificate is expiring soon at %s, start bootstrapping',
                cert.not_valid_after)
            await self._bootstrap_now()
            return
        if now < cert.not_valid_before:
            logging.error(
                'Certificate is not valid until %s', cert.not_valid_before)
            await self._bootstrap_now()
            return

        # no need to restart control_proxy
        await self._bootstrap_success_cb(False)
        self._schedule_next_bootstrap_check()

    async def _bootstrap_now(self):
        """Main entrance to bootstrapping

        1. set self._state to BOOTSTRAPPING
        2. set up a gPRC channel and get a challenge (async)
        3. call _get_challenge_done_success  to deal with the response
        If any steps fails, a new _bootstrap_now call will be scheduled.
        """
        assert self._state != BootstrapState.BOOTSTRAPPING, \
                              'At most one bootstrap is happening'
        self._state = BootstrapState.BOOTSTRAPPING

        try:
            chan = ServiceRegistry.get_bootstrap_rpc_channel()
        except ValueError as exp:
            logging.error('Failed to get rpc channel: %s', exp)
            self._schedule_next_bootstrap(hard_failure=False)
            return

        client = BootstrapperStub(chan)
        try:
            result = await grpc_async_wrapper(
                client.GetChallenge.future(AccessGatewayID(id=self._hw_id)),
                self._loop
            )
            await self._get_challenge_done_success(result)

        except grpc.RpcError as err:
            self._get_challenge_done_fail(err)

    async def _get_challenge_done_success(self, challenge):
        # create key
        try:
            self._gateway_key = ec.generate_private_key(
                ec.SECP384R1(), default_backend())
        except InternalError as exp:
            logging.error('Fail to generate private key: %s', exp)
            BOOTSTRAP_EXCEPTION.labels(
                cause='GetChallengeDonePrivateKey').inc()
            self._schedule_next_bootstrap(hard_failure=True)
            return
        # create csr and send for signing
        try:
            csr = self._create_csr()
        except Exception as exp:
            logging.error('Fail to create csr: %s', exp)
            BOOTSTRAP_EXCEPTION.labels(
                cause='GetChallengeDoneCreateCSR:%s' % type(
                    exp).__name__).inc()

        try:
            response = self._construct_response(challenge, csr)
        except BootstrapError as exp:
            logging.error('Fail to create response: %s', exp)
            BOOTSTRAP_EXCEPTION.labels(
                cause='GetChallengeDoneCreateResponse').inc()
            self._schedule_next_bootstrap(hard_failure=True)
            return
        await self._request_sign(response)

    def _get_challenge_done_fail(self, err):
        err = 'GetChallenge error! [%s] %s' % (err.code(), err.details())
        logging.error(err)
        BOOTSTRAP_EXCEPTION.labels(cause='GetChallengeResp').inc()
        self._schedule_next_bootstrap(hard_failure=False)

    async def _request_sign(self, response):
        """Request a signed certificate

        set up a gPRC channel and set the response

        If it fails, schedule the next bootstrap,
        Otherwise _request_sign_done callback is called
        """
        try:
            chan = ServiceRegistry.get_bootstrap_rpc_channel()
        except ValueError as exp:
            logging.error('Failed to get rpc channel: %s', exp)
            BOOTSTRAP_EXCEPTION.labels(cause='RequestSignGetRPC').inc()
            self._schedule_next_bootstrap(hard_failure=False)
            return

        try:
            client = BootstrapperStub(chan)
            result = await grpc_async_wrapper(
                client.RequestSign.future(response),
                self._loop
            )
            await self._request_sign_done_success(result)

        except grpc.RpcError as err:
            self._request_sign_done_fail(err)

    async def _request_sign_done_success(self, cert):
        if not self._is_valid_certificate(cert):
            BOOTSTRAP_EXCEPTION.labels(cause='RequestSignDoneInvalidCert').inc()
            self._schedule_next_bootstrap(hard_failure=True)
            return
        try:
            cert_utils.write_key(self._gateway_key, self._gateway_key_file)
            cert_utils.write_cert(cert.cert_der, self._gateway_cert_file)
        except Exception as exp:
            BOOTSTRAP_EXCEPTION.labels(cause='RequestSignDoneWriteCert:%s' % type(exp).__name__).inc()
            logging.error('Failed to write cert: %s', exp)

        # need to restart control_proxy
        await self._bootstrap_success_cb(True)
        self._gateway_key = None
        self._schedule_next_bootstrap_check()
        logging.info("Bootstrapped Successfully!")

    def _request_sign_done_fail(self, err):
        err = 'RequestSign error! [%s], %s' % (err.code(), err.details())
        BOOTSTRAP_EXCEPTION.labels(cause='RequestSignDoneResp').inc()
        logging.error(err)
        self._schedule_next_bootstrap(hard_failure=False)

    def _schedule_next_bootstrap(self, hard_failure):
        """Schedule a bootstrap

        Args:
            hard_failure: bool. If set, the time to next retry will be longer
        """
        if hard_failure:
            interval = self.LONG_BOOTSTRAP_RETRY_INTERVAL.total_seconds()
        else:
            interval = self.SHORT_BOOTSTRAP_RETRY_INTERVAL.total_seconds()
        logging.info('Retrying bootstrap in %d seconds', interval)
        self.set_interval(interval)
        self._state = BootstrapState.SCHEDULED_BOOTSTRAP

    def _schedule_next_bootstrap_check(self):
        """Schedule a bootstrap_check"""
        self.set_interval(
            self.PERIODIC_BOOTSTRAP_CHECK_INTERVAL.total_seconds()
        )
        self._state = BootstrapState.SCHEDULED_CHECK

    def _create_csr(self):
        """Create CSR protobuf

        Returns:
             CSR protobuf object
        """
        csr = cert_utils.create_csr(self._gateway_key, self._hw_id)
        duration = Duration()
        duration.FromTimedelta(datetime.timedelta(days=4))
        csr = CSR(
            id=Identity(gateway=Identity.Gateway(hardware_id=self._hw_id)),
            valid_time=duration,
            csr_der=csr.public_bytes(serialization.Encoding.DER),
        )
        return csr

    def _construct_response(self, challenge, csr):
        """Construct response message given challenge and csr message

        Args:
            challenge: Challenge(key_type, challenge)
            csr: CSR object returned by create_csr

        Returns:
             protobuf Response object

        Raises:
            BootstrapError: Unknown key type, cannot load challenge key,
             or wrong type of challenge key
        """
        if challenge.key_type == ChallengeKey.ECHO:
            echo_resp = Response.Echo(
                response=challenge.challenge,
            )
            response = Response(
                hw_id=AccessGatewayID(id=self._hw_id),
                challenge=challenge.challenge,
                echo_response=echo_resp,
                csr=csr,
            )
        elif challenge.key_type == ChallengeKey.SOFTWARE_ECDSA_SHA256:
            r_bytes, s_bytes = self._ecdsa_sha256_response(challenge.challenge)
            ecdsa_resp = Response.ECDSA(r=r_bytes, s=s_bytes)
            response = Response(
                hw_id=AccessGatewayID(id=self._hw_id),
                challenge=challenge.challenge,
                ecdsa_response=ecdsa_resp,
                csr=csr,
            )
        else:
            raise BootstrapError('Unknown key type: %s' % challenge.key_type)
        return response

    def _is_valid_certificate(self, cert):
        """Check whether certificate is usable

        Args:
            cert: Certificate object returned by RequestSign gRPC call

        Returns:
            err: error message, None if no error
        """
        now = datetime.datetime.utcnow()
        not_before = cert.not_before.ToDatetime()
        if now < not_before:
            logging.error(
                'Received a not-yet-valid certificate from: %s', not_before)
            return False

        not_after = cert.not_after.ToDatetime()
        valid_time = not_after - now
        if valid_time < self.PREEXPIRY_BOOTSTRAP_INTERVAL:
            valid_hours = valid_time.total_seconds() / 3600
            logging.error('Received a %.1f-hour certificate', valid_hours)
            return False

        return True

    def _ecdsa_sha256_response(self, challenge):
        """Compute the ecdsa signature

        Args:
            challenge: content of challenge in bytes

        Returns:
            r_bytes, s_bytes: ecdsa signature R, S in bytes

        Raises:
            BootstrapError: if the gateway cannot be properly loaded
        """
        try:
            challenge_key = cert_utils.load_key(self._challenge_key_file)
        except (IOError, ValueError, TypeError) as e:
            raise BootstrapError(
                'Gateway does not have a proper challenge key: %s' % e)

        try:
            signature = challenge_key.sign(challenge, ec.ECDSA(hashes.SHA256()))
        except TypeError:
            raise BootstrapError(
                'Challenge key cannot be used for ECDSA signature')

        r_int, s_int = decode_dss_signature(signature)
        r_bytes = r_int.to_bytes((r_int.bit_length() + 7) // 8, 'big')
        s_bytes = s_int.to_bytes((s_int.bit_length() + 7) // 8, 'big')
        return r_bytes, s_bytes
