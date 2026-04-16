#!/bin/sh
log=/tmp/$(basename $0)$$

update() {
    ev-cli module update --force --schemas-dir ./lib/everest/framework/schemas --build-dir ./build  $1 1>"${log}" 2>&1
    if [ $? -ne 0 ]; then
        echo "Failed: $1"
        cat "${log}"
        echo
    fi
    rm -f "${log}"
}

. build/venv/bin/activate

pip install applications/utils/ev-dev-tools/

if [ $# -eq 0 ]; then
    update API/API
    update API/auth_consumer_API
    update API/auth_token_provider_API
    update API/auth_token_validator_API
    update API/dc_external_derate_consumer_API
    update API/display_message_API
    update API/error_history_consumer_API
    update API/EvAPI
    update API/ev_board_support_API
    update API/evse_board_support_API
    update API/evse_manager_consumer_API
    update API/evse_security_consumer_API
    update API/external_energy_limits_consumer_API
    update API/generic_error_raiser_API
    update API/isolation_monitor_API
    update API/ocpp_consumer_API
    update API/over_voltage_monitor_API
    update API/powermeter_API
    update API/power_supply_DC_API
    update API/RpcApi
    update API/session_cost_API
    update API/session_cost_consumer_API
    update API/slac_API
    update API/system_API
    update BringUp/BUDCExternalDerate
    update BringUp/BUDisplayMessage
    update BringUp/BUEvseBoardSupport
    update BringUp/BUIsolationMonitor
    update BringUp/BUOcppConsumer
    update BringUp/BUOverVoltageMonitor
    update BringUp/BUPowermeter
    update BringUp/BUPowerSupplyDC
    update BringUp/BUSlac
    update BringUp/BUSystem
    update BringUp/BUTokenProvider
    update EnergyManagement/EnergyManager
    update EnergyManagement/EnergyNode
    update EV/EvManager
    update EV/EvSlac
#    update EV/PyEvJosev
    update EVSE/Auth
    update EVSE/Evse15118D20
    update EVSE/EvseManager
    update EVSE/EvseSecurity
    update EVSE/EvseSlac
    update EVSE/EvseV2G
    update EVSE/Iso15118InternetVas
    update EVSE/Iso15118InternetVas
    update EVSE/IsoMux
    update EVSE/OCPP
    update EVSE/OCPP201
    update EVSE/StaticISO15118VASProvider
    update Examples/CppExamples/Example
    update Examples/CppExamples/ExampleUser
    update Examples/CppExamples/OCPPExtensionExample
    update Examples/CppExamples/TerminalCostAndPriceMessage
    update Examples/CppExamples/TerminalDisplayMessage
    update HardwareDrivers/EVSE/AdAcEvse22KwzKitBSP
    update HardwareDrivers/EVSE/MicroMegaWattBSP
    update HardwareDrivers/EVSE/PhyVersoBSP
    update HardwareDrivers/EVSE/TIDA010939
    update HardwareDrivers/EVSE/YetiDriver
    update HardwareDrivers/EV/YetiEvDriver
    update HardwareDrivers/IsolationMonitors/Bender_isoCHA425HV
    update HardwareDrivers/IsolationMonitors/DoldRN5893
    update HardwareDrivers/NfcReaders/NxpNfcFrontendTokenProvider
    update HardwareDrivers/NfcReaders/PN532TokenProvider
    update HardwareDrivers/NfcReaders/PN7160TokenProvider
#    update HardwareDrivers/Payment/RsPaymentTerminal
    update HardwareDrivers/PowerMeters/Acrel_DJSF1352_RN
    update HardwareDrivers/PowerMeters/AST_DC650
    update HardwareDrivers/PowerMeters/CarloGavazzi_EM580
    update HardwareDrivers/PowerMeters/DZG_GSH01
    update HardwareDrivers/PowerMeters/GenericPowermeter
    update HardwareDrivers/PowerMeters/IsabellenhuetteIemDcr
    update HardwareDrivers/PowerMeters/LemDCBM400600
#    update HardwareDrivers/PowerMeters/RsIskraMeter
    update HardwareDrivers/PowerSupplies/DPM1000
    update HardwareDrivers/PowerSupplies/Huawei_R100040Gx
    update HardwareDrivers/PowerSupplies/Huawei_V100R023C10
    update HardwareDrivers/PowerSupplies/InfyPower
    update HardwareDrivers/PowerSupplies/InfyPower_BEG1K075G
    update HardwareDrivers/PowerSupplies/UUGreenPower_UR1000X0
    update HardwareDrivers/PowerSupplies/Winline
    update Misc/ChargerInfo
    update Misc/ErrorHistory
    update Misc/Linux_Systemd_Rauc
    update Misc/LocalAllowlistTokenValidator
    update Misc/PacketSniffer
    update Misc/PersistentStore
    update Misc/SerialCommHub
    update Misc/Setup
    update Misc/Store
    update Misc/System
    update Misc/YamlStore
    update Simulation/DCSupplySimulator
    update Simulation/IMDSimulator
    update Simulation/OVMSimulator
    update Simulation/SlacSimulator
    update Simulation/YetiSimulator
    update Testing/DummyBankSessionTokenProvider
    update Testing/DummySessionCostProvider
    update Testing/DummyTokenProvider
    update Testing/DummyTokenProviderManual
    update Testing/DummyTokenValidator
    update Testing/DummyV2G
else
    while [ $# -gt 0 ];
    do
        update $1
        shift
    done
fi
