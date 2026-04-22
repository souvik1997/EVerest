// SPDX-License-Identifier: Apache-2.0
// Copyright Pionix GmbH and Contributors to EVerest

#include <gtest/gtest.h>
#include <memory>
#include <string_view>

#include "OCPPExtensionExample.hpp"
#include "extended_module_adapter.hpp"
#include "generated/types/ocpp.hpp"

namespace {

std::string json_get(const json& obj, const std::string_view& key) {
    try {
        return obj.at(key);
    } catch (...) {
    }
    return {};
}

// ----------------------------------------------------------------------------
// test class - sets up the module ready for each test

class OCPPExtensionTest : public testing::Test {
protected:
    // ld-ev.cpp has static objects with pointer to adapter
    // so it must be consistent for all tests
    static stubs::ExtendedModuleAdapter adapter;

    static void infrastructure_init() {
        static bool once{false};
        if (!once) {
            // module initialisation in ld-ev.cpp (generated code)
            RequirementInitialization req;
            module::register_module_adapter(adapter);
            const auto commands = module::everest_register(req);
            adapter.register_commands(commands);
            once = true;
        }
    }

    ModuleInfo module_info{"ocpp_extension", {}, "Apache-2.0", "ocpp_ext", {"/etc", "/libexec", "/share"}, false, false,
                           std::nullopt};
    stubs::OCPPExtensionExampleStub module;

    OCPPExtensionTest() : module(adapter) {
    }

    void SetUp() override {
        infrastructure_init();
        adapter.clear();
    }

    void TearDown() override {
    }
};

stubs::ExtendedModuleAdapter OCPPExtensionTest::adapter;

// ----------------------------------------------------------------------------
// the tests

TEST_F(OCPPExtensionTest, DataTransfer) {
    // call module->init() which is private
    ModuleConfigs configs = R"({
        "data_transfer": {},
        "!module":{
            "enable": true,
            "poll_interval": 0.0,
            "id": 0,
            "keys_to_monitor": ""
        }
    })"_json;
    module::LdEverest::init(configs, module_info);
    // call module->ready() which is private
    module::LdEverest::ready();

    // test the provided data_transfer interface
    auto result = module.call_data_transfer(R"({"request":{"data":"Hello","vendor_id":"Pionix"}})"_json);
    EXPECT_EQ(json_get(result, "status"), "UnknownVendorId");
}

TEST_F(OCPPExtensionTest, DataTransfer2) {
    // test the provided data_transfer interface
    auto result = module.call_data_transfer(R"({"request":{"data":"Hello","vendor_id":"EVerest"}})"_json);
    EXPECT_EQ(json_get(result, "status"), "Accepted");
}

TEST_F(OCPPExtensionTest, UpdateKeys) {
    adapter.runtime_config_set("keys_to_monitor", "Heartbeat");
    const auto log = adapter.get_module_publish_log();
    ASSERT_EQ(log.size(), 1);
    EXPECT_EQ(
        log[0].msg,
        R"({"data":{"response":{"status":"Accepted"},"status":"Ok","status_info":"","type":"Set"},"msg_type":"SetConfigResponse"})");

    types::ocpp::EventData data;
    data.component_variable.variable.name = "Heartbeat";
    data.event_id = 0;
    data.trigger = types::ocpp::EventTriggerEnum::Delta;
    data.actual_value = "60";
    data.event_notification_type = types::ocpp::EventNotificationType::HardWiredNotification;
    module.var_event_data(data);
}

} // namespace
