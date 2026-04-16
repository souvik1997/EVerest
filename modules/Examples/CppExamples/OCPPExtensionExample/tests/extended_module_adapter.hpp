// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest

#pragma once

#include <framework/ModuleAdapter.hpp>
#include <utils/error/error_database.hpp>
#include <utils/error/error_database_map.hpp>
#include <utils/error/error_manager_req_global.hpp>
#include <utils/types.hpp>

#include <ld-ev.hpp>

#include <list>
#include <map>
#include <string_view>

namespace stubs {

class ErrorDatabaseStub : public Everest::error::ErrorDatabase {
public:
    using ErrorPtr = Everest::error::ErrorPtr;
    using ErrorFilter = Everest::error::ErrorFilter;

    void add_error(ErrorPtr error) override {
    }
    std::list<ErrorPtr> get_errors(const std::list<ErrorFilter>& filters) const override {
        return {};
    }
    std::list<ErrorPtr> edit_errors(const std::list<ErrorFilter>& filters, EditErrorFunc edit_func) override {
        return {};
    }
    std::list<ErrorPtr> remove_errors(const std::list<ErrorFilter>& filters) override {
        return {};
    }
};

class ExtendedModuleAdapter {
public:
    class Hooks {
    public:
        virtual ~Hooks() = default;
        virtual json call_fn(const std::string& topic, const json& value) = 0;
    };

    using ConfigServiceClient = ::Everest::config::ConfigServiceClient;
    using Error = ::Everest::error::Error;
    using ErrorCallback = ::Everest::error::ErrorCallback;
    using ErrorDatabase = ::Everest::error::ErrorDatabase;
    using ErrorDatabaseMap = ::Everest::error::ErrorDatabaseMap;
    using ErrorFactory = ::Everest::error::ErrorFactory;
    using ErrorManagerImpl = ::Everest::error::ErrorManagerImpl;
    using ErrorManagerReq = ::Everest::error::ErrorManagerReq;
    using ErrorManagerReqGlobal = ::Everest::error::ErrorManagerReqGlobal;
    using ErrorStateMonitor = ::Everest::error::ErrorStateMonitor;
    using ErrorType = ::Everest::error::ErrorType;
    using ErrorTypeMap = ::Everest::error::ErrorTypeMap;
    using ModuleAdapter = ::Everest::ModuleAdapter;
    using TelemetryMap = ::Everest::TelemetryMap;

private:
    std::string m_module_id;
    std::string m_implementation_id;
    ImplementationIdentifier m_default_origin;
    std::shared_ptr<ErrorDatabase> m_error_database;
    std::shared_ptr<ErrorDatabaseMap> m_error_database_map;
    std::shared_ptr<ErrorFactory> m_error_factory;
    std::shared_ptr<ErrorManagerImpl> m_error_manager;
    std::shared_ptr<ErrorManagerReq> m_error_manager_req;
    std::shared_ptr<ErrorManagerReqGlobal> m_error_manager_req_global;
    std::shared_ptr<ErrorStateMonitor> m_error_state_monitor;
    std::shared_ptr<ErrorTypeMap> m_error_type_map;
    std::shared_ptr<ConfigServiceClient> m_config_service_client;
    std::map<std::string, Command> m_module_commands;

    Hooks* handler{nullptr};

    Result call_fn(const Requirement&, const std::string& topic, Parameters value) {
        Result result;
        if (handler != nullptr) {
            result = handler->call_fn(topic, value);
        } else {
            std::printf("call_fn(%s) with no handler\n", topic.c_str());
        }
        return result;
    }
    void publish_fn(const std::string&, const std::string&, Value) {
    }
    void subscribe_fn(const Requirement&, const std::string& fn, ValueCallback) {
    }
    std::shared_ptr<ErrorManagerImpl> get_error_manager_impl_fn(const std::string&) {
        return m_error_manager;
    }
    std::shared_ptr<ErrorStateMonitor> get_error_state_monitor_impl_fn(const std::string&) {
        return m_error_state_monitor;
    }
    std::shared_ptr<ErrorManagerReqGlobal> get_global_error_manager_fn() {
        return m_error_manager_req_global;
    }
    std::shared_ptr<ErrorStateMonitor> get_global_error_state_monitor_fn() {
        return m_error_state_monitor;
    }
    std::shared_ptr<ErrorFactory> get_error_factory_fn(const std::string&) {
        return m_error_factory;
    }
    std::shared_ptr<ErrorManagerReq> get_error_manager_req_fn(const Requirement&) {
        return m_error_manager_req;
    }
    std::shared_ptr<ErrorStateMonitor> get_error_state_monitor_req_fn(const Requirement&) {
        return m_error_state_monitor;
    }
    void ext_mqtt_publish_fn(const std::string&, const std::string&) {
    }
    std::function<void()> ext_mqtt_subscribe_fn(const std::string&, StringHandler) {
        return nullptr;
    }
    std::function<void()> ext_mqtt_subscribe_pair_fn(const std::string& topic, const StringPairHandler& handler) {
        return {};
    }
    void telemetry_publish_fn(const std::string&, const std::string&, const std::string&, const TelemetryMap&) {
    }
    std::optional<ModuleTierMappings> get_mapping_fn() {
        return {};
    }
    std::shared_ptr<ConfigServiceClient> get_config_service_client_fn() {
        return m_config_service_client;
    }

public:
    ExtendedModuleAdapter() : m_default_origin(m_module_id, m_implementation_id) {
        m_error_type_map = std::make_shared<ErrorTypeMap>();
        m_error_database_map = std::make_shared<ErrorDatabaseMap>();
        m_error_database = std::make_shared<ErrorDatabaseStub>();
        m_error_manager = std::make_shared<ErrorManagerImpl>(
            m_error_type_map, m_error_database_map, std::list<ErrorType>(), [](const Error&) {}, [](const Error&) {});
        m_error_state_monitor = std::make_shared<ErrorStateMonitor>(m_error_database_map);
        m_error_manager_req_global = std::make_shared<ErrorManagerReqGlobal>(
            m_error_type_map, m_error_database, [](const ErrorCallback&, const ErrorCallback&) {});
        m_error_factory = std::make_shared<ErrorFactory>(m_error_type_map, m_default_origin);
        m_error_manager_req = std::make_shared<ErrorManagerReq>(
            m_error_type_map, m_error_database_map, std::list<ErrorType>(),
            [](const ErrorType&, const ErrorCallback&, const ErrorCallback&) { std::printf("subscribe_error\n"); });
    }

    operator ModuleAdapter() {
        ModuleAdapter result;
        result.call = [this](auto&&... ts) { return call_fn(ts...); };
        result.publish = [this](auto&&... ts) { publish_fn(ts...); };
        result.subscribe = [this](auto&&... ts) { subscribe_fn(ts...); };
        result.get_error_manager_impl = [this](auto&&... ts) { return get_error_manager_impl_fn(ts...); };
        result.get_error_state_monitor_impl = [this](auto&&... ts) { return get_error_state_monitor_impl_fn(ts...); };
        result.get_error_factory = [this](auto&&... ts) { return get_error_factory_fn(ts...); };
        result.get_error_manager_req = [this](auto&&... ts) { return get_error_manager_req_fn(ts...); };
        result.get_error_state_monitor_req = [this](auto&&... ts) { return get_error_state_monitor_req_fn(ts...); };
        result.get_global_error_manager = [this](auto&&... ts) { return get_global_error_manager_fn(ts...); };
        result.get_global_error_state_monitor = [this](auto&&... ts) {
            return get_global_error_state_monitor_fn(ts...);
        };
        result.ext_mqtt_publish = [this](auto&&... ts) { return ext_mqtt_publish_fn(ts...); };
        result.ext_mqtt_subscribe = [this](auto&&... ts) { return ext_mqtt_subscribe_fn(ts...); };
        result.ext_mqtt_subscribe_pair = [this](auto&&... ts) { return ext_mqtt_subscribe_pair_fn(ts...); };
        result.telemetry_publish = [this](auto&&... ts) { return telemetry_publish_fn(ts...); };
        result.get_mapping = [this](auto&&... ts) { return get_mapping_fn(ts...); };
        result.get_config_service_client = [this](auto&&... ts) { return get_config_service_client_fn(ts...); };
        return result;
    }

    // obtain interface functions from the module
    void register_commands(const std::vector<::Everest::cmd>& cmds) {
        for (const auto& i : cmds) {
            std::printf("module command: %s added\n", i.cmd_name.c_str());
            m_module_commands.insert({i.cmd_name, i.cmd});
        }
    }

    Hooks* set_handler(Hooks* ptr) {
        Hooks* tmp = handler;
        handler = ptr;
        return tmp;
    }

    // ========================================================================
    // call interfaces provided by the module

    json call(const std::string_view& cmd, const json& args) {
        json result;
        if (auto it = m_module_commands.find(std::string{cmd}); it != m_module_commands.end()) {
            std::printf("cmd %s(%s)\n", cmd.data(), args.dump().c_str());
            Parameters p = args;
            result = it->second(p);
            std::printf("cmd result %s(%s)\n", cmd.data(), result.dump().c_str());
        } else {
            std::printf("cmd %s not found\n", cmd.data());
        }
        return result;
    }
};

class OCPPExtensionExampleStub : public ExtendedModuleAdapter::Hooks {
private:
    using CallCallback = Result (OCPPExtensionExampleStub::*)(const Parameters& value);

    ExtendedModuleAdapter& m_adapter;
    std::map<std::string, CallCallback> m_call_callbacks;

public:
    OCPPExtensionExampleStub(ExtendedModuleAdapter& adapter) : m_adapter(adapter) {
        // register calls expected to be made by the module
        m_call_callbacks.emplace("data_transfer", &OCPPExtensionExampleStub::request_data_transfer);
        m_call_callbacks.emplace("get_variables", &OCPPExtensionExampleStub::request_get_variables);
        m_call_callbacks.emplace("set_variables", &OCPPExtensionExampleStub::request_set_variables);
        m_call_callbacks.emplace("monitor_variables", &OCPPExtensionExampleStub::request_monitor_variables);
        m_adapter.set_handler(this);
    }

    virtual ~OCPPExtensionExampleStub() {
        m_adapter.set_handler(nullptr);
    }

    // ========================================================================
    // call implementations provided by the module

    json call_fn(const std::string& topic, const json& value) override {
        json result;
        if (auto it = m_call_callbacks.find(topic); it != m_call_callbacks.end()) {
            result = std::invoke(it->second, this, value);
        } else {
            std::printf("call_fn(%s)\n", topic.c_str());
        }
        return result;
    }

    auto call_data_transfer(const json& args) {
        return m_adapter.call("data_transfer", args);
    }

protected:
    // ========================================================================
    // call implementations (requests from the module under test)

    virtual std::optional<json> request_monitor_variables(const json& value) {
        std::printf("request_monitor_variables(%s)\n", value.dump().c_str());
        return {};
    }
    virtual std::optional<json> request_set_variables(const json& value) {
        std::printf("request_set_variables(%s)\n", value.dump().c_str());
        const json res = R"(
        {
            "status":"Accepted",
            "component_variable":{"component":{"name":""},"variable":{"name":"ExampleConfigurationKey"}},
            "value":""
        }
        )"_json;
        json result;
        result.push_back(res);
        result.push_back(res);
        std::printf("request_set_variables result(%s)\n", result.dump().c_str());
        return result;
    }

    virtual std::optional<json> request_get_variables(const json& value) {
        std::printf("request_get_variables(%s)\n", value.dump().c_str());
        const json res = R"(
        {
            "status":"Accepted",
            "component_variable":{"component":{"name":""},"variable":{"name":"ExampleConfigurationKey"}},
            "value":""
        }
        )"_json;
        json result;
        result.push_back(res);
        std::printf("request_get_variables result(%s)\n", result.dump().c_str());
        return result;
    }

    virtual std::optional<json> request_data_transfer(const json& value) {
        std::printf("request_data_transfer(%s)\n", value.dump().c_str());
        const json result = R"(
        {
            "status":"Rejected"
        }
        )"_json;
        std::printf("request_data_transfer result(%s)\n", result.dump().c_str());
        return result;
    }
};

} // namespace stubs
