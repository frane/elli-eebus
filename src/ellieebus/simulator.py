"""A simulated Elli Charger 2 as seen over EEBUS, including its firmware bugs.

The device model (entities, features, data, use cases) follows recordings of
a real Elli Charger 2. Use it to develop against without the wallbox, e.g.
``elli-eebus simulate``.

Known firmware behaviour that is reproduced:

* a power limit written with a duration (``timePeriod``) gets no answer and
  is not applied;
* a limit can only be lifted with ``isLimitActive: false`` and value 0.
"""

from __future__ import annotations

import logging
from typing import Any

from pyeebus.service import EebusService
from pyeebus.ship import Identity, TrustStore
from pyeebus.spine import (
    DropMessage,
    Event,
    EventType,
    Message,
    Role,
    SpineError,
    scaled_number,
    scaled_value,
)
from pyeebus.spine.model import format_datetime

_LOGGER = logging.getLogger(__name__)

LIMIT_ID = 10
POWER_MEASUREMENT_ID = 8


def _sn(value: float) -> dict[str, int]:
    return scaled_number(value)


class SimulatedElli:
    """Elli Charger 2 with entity ``[1]`` (EVSE) offering LPC and MPC."""

    def __init__(self, identity: Identity, *, serial: str = "00099999", port: int = 4711,
                 trust: TrustStore | None = None, nominal_max: float = 11040.0,
                 failsafe_power: float = 22000.0, quirks: bool = True, **node_options: Any) -> None:
        self.quirks = quirks
        self.service = EebusService(
            identity, brand="Elli", model="Elli", serial=serial, vendor="3210", device_type="ChargingStation",
            entity_types=("EVSE",), port=port, trust=trust, ship_id=f"Elli-Wallbox-{serial}",
            heartbeat_timeout=60.0, **node_options)
        self.nominal_max = nominal_max
        self.demand = 0.0  # W the car would draw without a limit
        self.evse = self.service.entities[0]
        self.evse.description = "Electric Vehicle Supply Equipment"
        self._setup(serial, failsafe_power)
        self.service.device.subscribe_events(self._on_event)

    @property
    def device(self):
        return self.service.device

    @property
    def ski(self) -> str:
        return self.service.ski

    def _setup(self, serial: str, failsafe_power: float) -> None:
        e = self.evse
        f = e.add_feature("DeviceClassification", Role.SERVER, "Device Classification for EVSE")
        f.add_function("deviceClassificationManufacturerData")
        f.data["deviceClassificationManufacturerData"] = {
            "deviceName": "EVSE", "serialNumber": serial, "softwareRevision": "", "vendorName": "Elli",
            "vendorCode": "3210", "brandName": "Elli", "manufacturerLabel": "eSystems"}

        f = e.add_feature("DeviceDiagnosis", Role.SERVER, "Device Diagnosis EVSE")
        f.add_function("deviceDiagnosisStateData")
        f.data["deviceDiagnosisStateData"] = {"operatingState": "normalOperation"}
        f.add_function("deviceDiagnosisHeartbeatData")

        e.add_feature("DeviceClassification", Role.CLIENT, "EMS Device Classification")

        f = e.add_feature("DeviceConfiguration", Role.SERVER, "Device Configuration EV")
        f.add_function("deviceConfigurationKeyValueDescriptionListData")
        f.add_function("deviceConfigurationKeyValueListData", read=True, write=True)
        f.data["deviceConfigurationKeyValueDescriptionListData"] = {"deviceConfigurationKeyValueDescriptionData": [
            {"keyId": 1, "keyName": "failsafeConsumptionActivePowerLimit", "valueType": "scaledNumber", "unit": "W"},
            {"keyId": 2, "keyName": "failsafeDurationMinimum", "valueType": "duration"}]}
        f.data["deviceConfigurationKeyValueListData"] = {"deviceConfigurationKeyValueData": [
            {"keyId": 1, "value": {"scaledNumber": _sn(failsafe_power)}, "isValueChangeable": True},
            {"keyId": 2, "value": {"duration": "PT2H"}, "isValueChangeable": True}]}

        e.add_feature("DeviceDiagnosis", Role.CLIENT, "Device Diagnosis Heartbeat")

        f = e.add_feature("Bill", Role.SERVER, "Bill Feature for EVSE")
        f.add_function("billDescriptionListData")
        f.add_function("billConstraintsListData")
        f.add_function("billListData", read=True, write=True)
        f.data["billDescriptionListData"] = {"billDescriptionData": [
            {"billId": 1, "billWriteable": True, "updateRequired": False, "supportedBillType": ["chargingSummary"]}]}
        f.data["billConstraintsListData"] = {"billConstraintsData": [
            {"billId": 1, "positionCountMin": 0, "positionCountMax": 2}]}
        f.data["billListData"] = {"billData": [{}]}

        f = e.add_feature("LoadControl", Role.SERVER, "Load Control EVSE")
        f.add_function("loadControlLimitDescriptionListData")
        f.add_function("loadControlLimitListData", read=True, write=True)
        f.data["loadControlLimitDescriptionListData"] = {"loadControlLimitDescriptionData": [
            {"limitId": LIMIT_ID, "limitType": "signDependentAbsValueLimit", "limitCategory": "obligation",
             "limitDirection": "consume", "measurementId": POWER_MEASUREMENT_ID, "unit": "W",
             "scopeType": "activePowerLimit"}]}
        f.data["loadControlLimitListData"] = {"loadControlLimitData": [
            {"limitId": LIMIT_ID, "isLimitChangeable": True, "isLimitActive": False, "value": _sn(0)}]}
        f.write_approval = self._approve_limit

        f = e.add_feature("Measurement", Role.SERVER, "Measurements EVSE")
        f.add_function("measurementListData")
        f.add_function("measurementDescriptionListData")
        f.data["measurementDescriptionListData"] = {"measurementDescriptionData": [
            {"measurementId": POWER_MEASUREMENT_ID, "measurementType": "power", "commodityType": "electricity",
             "unit": "W", "scopeType": "acPowerTotal"}]}
        f.data["measurementListData"] = {"measurementData": [self._power_item(0)]}

        f = e.add_feature("ElectricalConnection", Role.SERVER, "Electrical Connection")
        for fn in ("electricalConnectionParameterDescriptionListData", "electricalConnectionDescriptionListData",
                   "electricalConnectionCharacteristicListData", "electricalConnectionPermittedValueSetListData"):
            f.add_function(fn)
        f.data["electricalConnectionParameterDescriptionListData"] = {
            "electricalConnectionParameterDescriptionData": [
                {"electricalConnectionId": 0, "parameterId": 8, "measurementId": POWER_MEASUREMENT_ID,
                 "voltageType": "ac", "acMeasuredPhases": "abc", "acMeasuredInReferenceTo": "neutral",
                 "acMeasurementType": "real", "acMeasurementVariant": "rms", "scopeType": "acPowerTotal"}]}
        f.data["electricalConnectionDescriptionListData"] = {"electricalConnectionDescriptionData": [
            {"electricalConnectionId": 0, "powerSupplyType": "ac", "acConnectedPhases": 3,
             "positiveEnergyDirection": "consume"}]}
        f.data["electricalConnectionCharacteristicListData"] = {"electricalConnectionCharacteristicData": [
            {"electricalConnectionId": 1, "parameterId": 1, "characteristicId": 1, "characteristicContext": "entity",
             "characteristicType": "powerConsumptionNominalMax", "value": _sn(self.nominal_max), "unit": "W"},
            {"electricalConnectionId": 1, "parameterId": 1, "characteristicId": 3, "characteristicContext": "entity",
             "characteristicType": "contractualConsumptionNominalMax", "value": _sn(self.nominal_max),
             "unit": "W"}]}
        phase = {"value": [{"number": 100, "scale": -3}], "range": [{"min": _sn(6), "max": _sn(16)}]}
        f.data["electricalConnectionPermittedValueSetListData"] = {"electricalConnectionPermittedValueSetData": [
            {"electricalConnectionId": 0, "parameterId": 1, "permittedValueSet": [phase]},
            {"electricalConnectionId": 0, "parameterId": 3, "permittedValueSet": [phase]},
            {"electricalConnectionId": 0, "parameterId": 5, "permittedValueSet": [phase]},
            {"electricalConnectionId": 0, "parameterId": 8, "permittedValueSet": [
                {"value": [{"number": 100, "scale": -3}],
                 "range": [{"min": _sn(6 * 230 * 3), "max": _sn(self.nominal_max)}]}]}]}

        e.add_use_case("EVSE", "evseCommissioningAndConfiguration", "1.0.1", [1, 2])
        e.add_use_case("EVSE", "evChargingSummary", "1.0.0", [1])
        e.add_use_case("ControllableSystem", "limitationOfPowerConsumption", "1.0.0", [1, 2, 3, 4])
        e.add_use_case("MonitoredUnit", "monitoringOfPowerConsumption", "1.0.0", [1])

    # --- behaviour ------------------------------------------------------------------------

    def _approve_limit(self, msg: Message) -> SpineError | None:
        for item in (msg.data or {}).get("loadControlLimitData") or []:
            if self.quirks and item.get("timePeriod"):
                _LOGGER.info("ignoring limit with timePeriod (Elli firmware bug)")
                raise DropMessage
            if self.quirks and item.get("isLimitActive") is False and scaled_value(item.get("value")) != 0:
                return SpineError(1, "Write failed")
        return None

    @property
    def limit(self) -> tuple[float, bool]:
        item = self.evse.feature("LoadControl", Role.SERVER).data["loadControlLimitListData"][
            "loadControlLimitData"][0]
        return scaled_value(item.get("value")) or 0.0, bool(item.get("isLimitActive"))

    @property
    def power(self) -> float:
        value, active = self.limit
        return min(self.demand, value) if active else self.demand

    def set_demand(self, watts: float) -> None:
        """What a connected car would draw; the reported power respects the limit."""
        self.demand = watts
        self._update_power()

    @staticmethod
    def _power_item(watts: float) -> dict[str, Any]:
        return {"measurementId": POWER_MEASUREMENT_ID, "valueType": "value", "timestamp": format_datetime(),
                "value": _sn(round(watts)), "valueSource": "measuredValue", "valueState": "normal"}

    def _update_power(self) -> None:
        self.evse.feature("Measurement", Role.SERVER).set_data(
            "measurementListData", {"measurementData": [self._power_item(self.power)]})

    def _on_event(self, event: Event) -> None:
        if event.type == EventType.DATA and event.classifier == "write" and \
                event.function == "loadControlLimitListData":
            _LOGGER.info("limit now %s", self.limit)
            self._update_power()

    async def start(self) -> None:
        await self.service.start()

    async def stop(self) -> None:
        await self.service.stop()
