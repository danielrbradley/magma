/*
Copyright (c) Facebook, Inc. and its affiliates.
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree.
*/

package config

import (
	"fmt"
	"io/ioutil"
	"os"
	"path/filepath"
	"strings"

	"magma/orc8r/cloud/go/errors"

	"github.com/golang/glog"
	"gopkg.in/yaml.v2"
)

const (
	// ConfigDir is where the per-service configuration files are stored
	ConfigDir         = "/etc/magma/configs"
	OldConfigDir      = "/etc/magma"
	ConfigOverrideDir = "/var/opt/magma/configs/"
)

// ConfigMap is a struct for representing a map generated from a service YML file
type ConfigMap struct {
	RawMap map[interface{}]interface{}
}

// NewConfigMap creates a new ConfigMap based on the input map
func NewConfigMap(config map[interface{}]interface{}) *ConfigMap {
	return &ConfigMap{config}
}

// GetServiceConfig loads a config by name to a map of parameters
// Input: configName - name of config to load, e.g. control_proxy
// Output: map of parameters if it exists, error if not
func GetServiceConfig(moduleName string, serviceName string) (*ConfigMap, error) {
	return getServiceConfigImpl(moduleName, serviceName, ConfigDir, OldConfigDir, ConfigOverrideDir)

}

// GetStringParam is used to retrieve a string param from a YML file and returns error if param does not exist/ill-formed
func (cfgMap *ConfigMap) GetStringParam(key string) (string, error) {
	return getStringParamImpl(cfgMap, key)
}

// GetRequiredStringParam is same as GetStringParam but fails when the string does not exist
func (cfgMap *ConfigMap) GetRequiredStringParam(key string) string {
	str, err := getStringParamImpl(cfgMap, key)
	if err != nil {
		glog.Fatalf("Error retrieving %s: %v\n", key, err)
	}
	return str
}

// GetIntParam is used to retrieve an int param from a YML file
func (cfgMap *ConfigMap) GetIntParam(key string) (int, error) {
	paramIface, ok := cfgMap.RawMap[key]
	if !ok {
		return 0, fmt.Errorf("Could not find key %s", key)
	}
	param, ok := paramIface.(int)
	if !ok {
		return 0, fmt.Errorf("Could not convert param to integer for key %s", key)
	}
	return param, nil
}

func getServiceConfigImpl(moduleName string, serviceName, configDir, oldConfigDir, configOverrideDir string) (*ConfigMap, error) {
	// Filenames should be lower case
	moduleName = strings.ToLower(moduleName)
	serviceName = strings.ToLower(serviceName)

	configFileName := filepath.Join(oldConfigDir, moduleName, fmt.Sprintf("%s.yml", serviceName))
	if fi, err := os.Stat(configFileName); err != nil || fi.IsDir() {
		configFileName = filepath.Join(configDir, moduleName,
			fmt.Sprintf("%s.yml", serviceName))
	}

	config, err := loadYamlFile(configFileName)
	if err != nil {
		return config, err
	}

	overrideFileName := filepath.Join(configOverrideDir, moduleName,
		fmt.Sprintf("%s.yml", serviceName))
	if fi, err := os.Stat(overrideFileName); err == nil && !fi.IsDir() {
		overrides, err := loadYamlFile(overrideFileName)
		if err != nil {
			return config, err
		}
		config = updateMap(config, overrides)
	}
	return config, nil
}

func updateMap(baseMap, overrides *ConfigMap) *ConfigMap {
	for k, v := range overrides.RawMap {
		if _, ok := baseMap.RawMap[k]; ok {
			baseMap.RawMap[k] = v
		}
	}
	return baseMap
}

// loadYamlFile loads a config by file name to a map of parameters
// Input: configFileName - name of config file to load, e.g. /etc/magma/control_proxy.yml
// Output: map of parameters if it exists, error if not
func loadYamlFile(configFileName string) (*ConfigMap, error) {
	yamlFile, err := ioutil.ReadFile(configFileName)
	if err != nil {
		return nil, err
	}
	configMap := make(map[interface{}]interface{})
	err = yaml.Unmarshal([]byte(yamlFile), &configMap)
	if err != nil {
		return nil, err
	}
	return &ConfigMap{configMap}, nil
}

// getStringParamImpl retrieves a string param from a ConfigMap
func getStringParamImpl(cfgMap *ConfigMap, key string) (string, error) {
	paramIface, ok := cfgMap.RawMap[key]
	if !ok {
		return "", errors.ErrNotFound
	}
	param, ok := paramIface.(string)
	if !ok {
		return "", fmt.Errorf("Could not convert param to string for key %s", key)
	}
	return param, nil
}
