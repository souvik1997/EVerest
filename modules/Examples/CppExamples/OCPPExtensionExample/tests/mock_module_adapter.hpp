// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest

#pragma once

#include "framework/ModuleAdapter.hpp"
#include "mock_mqtt_abstraction.hpp"

#include <ModuleAdapterStub.hpp>
#include <utils/error/error_database.hpp>
#include <utils/error/error_manager_req_global.hpp>
#include <utils/types.hpp>

#include <ld-ev.hpp>

#include <list>
#include <map>
#include <string_view>

namespace Everest::tests {

class AdapterTest : public module::stub::QuietModuleAdapterStub {
private:
    using CallCallback = Result (AdapterTest::*)(const Parameters& value);
    std::map<std::string, CallCallback> m_call_callbacks;
    std::map<std::string, ValueCallback> m_subscribe_callbacks;
    std::map<std::string, Command> m_commands;

    const std::string m_module_id{"id"};
    std::unordered_map<std::string, std::string> m_module_names;
    std::shared_ptr<MQTTAbstraction> m_mqtt_abstraction;
    std::shared_ptr<::Everest::config::ConfigServiceClient> m_config_service_client;

    // ========================================================================
    // Adapter overrides

    Result call_fn(const Requirement&, const std::string& topic, Parameters value) override {
        Result result;
        if (auto it = m_call_callbacks.find(topic); it != m_call_callbacks.end()) {
            result = std::invoke(it->second, this, value);
        } else {
            std::printf("call_fn(%s)\n", topic.c_str());
        }
        return result;
    }
    void publish_fn(const std::string& topic, const std::string&, Value) override {
        std::printf("publish_fn(%s)\n", topic.c_str());
    }
    void subscribe_fn(const Requirement&, const std::string& topic, ValueCallback cb) override {
        std::printf("subscribe_fn(%s)\n", topic.c_str());
        m_subscribe_callbacks.emplace(topic, std::move(cb));
    }
    std::shared_ptr<::Everest::config::ConfigServiceClient> get_config_service_client_fn() override {
        std::printf("get_config_service_client_fn\n");
        return m_config_service_client;
    }

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

public:
    AdapterTest() {
        m_mqtt_abstraction = std::make_shared<MockMQTTAbstraction>("/everest");
        m_config_service_client =
            std::make_shared<::Everest::config::ConfigServiceClient>(m_mqtt_abstraction, m_module_id, m_module_names);
        m_call_callbacks.emplace("data_transfer", &AdapterTest::request_data_transfer);
        m_call_callbacks.emplace("get_variables", &AdapterTest::request_get_variables);
        m_call_callbacks.emplace("set_variables", &AdapterTest::request_set_variables);
        m_call_callbacks.emplace("monitor_variables", &AdapterTest::request_monitor_variables);
    }

    void register_commands(const std::vector<::Everest::cmd>& cmds) {
        for (const auto& i : cmds) {
            std::printf("module command: %s added\n", i.cmd_name.c_str());
            m_commands.insert({i.cmd_name, i.cmd});
        }
    }

    json publish(const std::string_view& topic, const json& value) {
        json result;
        if (const auto it = m_subscribe_callbacks.find(std::string{topic}); it != m_subscribe_callbacks.end()) {
            std::printf("cmd %s(%s)\n", topic.data(), value.dump().c_str());
            it->second(value);
            // std::printf("cmd result %s(%s)\n", topic.data(), result.dump().c_str());
        } else {
            std::printf("publish to unsubscribed topic (%s)\n", topic.data());
        }
        return result;
    }

    json call(const std::string_view& cmd, const json& args) {
        json result;
        std::printf("cmd %s(%s)\n", cmd.data(), args.dump().c_str());
        if (const auto& it = m_commands.find(std::string{cmd}); it != m_commands.end()) {
            Parameters p = args;
            result = it->second(p);
        }
        std::printf("cmd result %s(%s)\n", cmd.data(), result.dump().c_str());
        return result;
    }

    auto call_data_transfer(const json& args) {
        return call("data_transfer", args);
    }
};

} // namespace Everest::tests
