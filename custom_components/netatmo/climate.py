"""Support for Netatmo Smart thermostats."""
from __future__ import annotations

import logging
from typing import Any, cast

from . import pyatmo
import voluptuous as vol

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    DEFAULT_MIN_TEMP,
    HVAC_MODE_AUTO,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_AWAY,
    PRESET_BOOST,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_SUGGESTED_AREA,
    ATTR_TEMPERATURE,
    PRECISION_HALVES,
    STATE_OFF,
    TEMP_CELSIUS,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import async_get_registry
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_HEATING_POWER_REQUEST,
    ATTR_SCHEDULE_NAME,
    ATTR_SELECTED_SCHEDULE,
    DATA_DEVICE_IDS,
    DATA_HANDLER,
    DATA_HOMES,
    DATA_SCHEDULES,
    DOMAIN,
    EVENT_TYPE_CANCEL_SET_POINT,
    EVENT_TYPE_SCHEDULE,
    EVENT_TYPE_SET_POINT,
    EVENT_TYPE_THERM_MODE,
    MANUFACTURER,
    MODULE_TYPE_THERM,
    MODULE_TYPE_VALVE,
    NETATMO_CREATE_BATTERY,
    SERVICE_SET_SCHEDULE,
    SIGNAL_NAME,
    TYPE_ENERGY,
)
from .data_handler import (
    CLIMATE_STATE_CLASS_NAME,
    CLIMATE_TOPOLOGY_CLASS_NAME,
    NetatmoDataHandler,
    NetatmoDevice,
)
from .netatmo_entity_base import NetatmoBase

_LOGGER = logging.getLogger(__name__)

PRESET_FROST_GUARD = "Frost Guard"
PRESET_SCHEDULE = "Schedule"
PRESET_MANUAL = "Manual"

SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE | SUPPORT_PRESET_MODE
SUPPORT_HVAC = [HVAC_MODE_HEAT, HVAC_MODE_AUTO, HVAC_MODE_OFF]
SUPPORT_PRESET = [PRESET_AWAY, PRESET_BOOST, PRESET_FROST_GUARD, PRESET_SCHEDULE]

STATE_NETATMO_SCHEDULE = "schedule"
STATE_NETATMO_HG = "hg"
STATE_NETATMO_MAX = "max"
STATE_NETATMO_AWAY = PRESET_AWAY
STATE_NETATMO_OFF = STATE_OFF
STATE_NETATMO_MANUAL = "manual"
STATE_NETATMO_HOME = "home"

PRESET_MAP_NETATMO = {
    PRESET_FROST_GUARD: STATE_NETATMO_HG,
    PRESET_BOOST: STATE_NETATMO_MAX,
    PRESET_SCHEDULE: STATE_NETATMO_SCHEDULE,
    PRESET_AWAY: STATE_NETATMO_AWAY,
    STATE_NETATMO_OFF: STATE_NETATMO_OFF,
}

NETATMO_MAP_PRESET = {
    STATE_NETATMO_HG: PRESET_FROST_GUARD,
    STATE_NETATMO_MAX: PRESET_BOOST,
    STATE_NETATMO_SCHEDULE: PRESET_SCHEDULE,
    STATE_NETATMO_AWAY: PRESET_AWAY,
    STATE_NETATMO_OFF: STATE_NETATMO_OFF,
    STATE_NETATMO_MANUAL: STATE_NETATMO_MANUAL,
    STATE_NETATMO_HOME: PRESET_SCHEDULE,
}

HVAC_MAP_NETATMO = {
    PRESET_SCHEDULE: HVAC_MODE_AUTO,
    STATE_NETATMO_HG: HVAC_MODE_AUTO,
    PRESET_FROST_GUARD: HVAC_MODE_AUTO,
    PRESET_BOOST: HVAC_MODE_HEAT,
    STATE_NETATMO_OFF: HVAC_MODE_OFF,
    STATE_NETATMO_MANUAL: HVAC_MODE_AUTO,
    PRESET_MANUAL: HVAC_MODE_AUTO,
    STATE_NETATMO_AWAY: HVAC_MODE_AUTO,
}

CURRENT_HVAC_MAP_NETATMO = {True: CURRENT_HVAC_HEAT, False: CURRENT_HVAC_IDLE}

DEFAULT_MAX_TEMP = 30


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Netatmo energy platform."""
    data_handler = hass.data[DOMAIN][entry.entry_id][DATA_HANDLER]

    await data_handler.register_data_class(
        CLIMATE_TOPOLOGY_CLASS_NAME, CLIMATE_TOPOLOGY_CLASS_NAME, None
    )
    climate_topology = data_handler.data.get(CLIMATE_TOPOLOGY_CLASS_NAME)

    if not climate_topology or climate_topology.raw_data == {}:
        raise PlatformNotReady

    entities = []
    for home_id in climate_topology.home_ids:
        signal_name = f"{CLIMATE_STATE_CLASS_NAME}-{home_id}"
        await data_handler.register_data_class(
            CLIMATE_STATE_CLASS_NAME, signal_name, None, home_id=home_id
        )
        climate_state = data_handler.data.get(signal_name)
        climate_topology.register_handler(home_id, climate_state.process_topology)

        for room in climate_state.homes[home_id].rooms.values():
            if room.device_type is None or room.device_type.value not in [
                MODULE_TYPE_THERM,
                MODULE_TYPE_VALVE,
            ]:
                continue
            entities.append(NetatmoThermostat(data_handler, room))

        hass.data[DOMAIN][DATA_SCHEDULES][home_id] = climate_state.homes[
            home_id
        ].schedules

        hass.data[DOMAIN][DATA_HOMES][home_id] = climate_state.homes[home_id].name

    _LOGGER.debug("Adding climate devices %s", entities)
    async_add_entities(entities, True)

    platform = entity_platform.async_get_current_platform()

    if climate_topology is not None:
        platform.async_register_entity_service(
            SERVICE_SET_SCHEDULE,
            {vol.Required(ATTR_SCHEDULE_NAME): cv.string},
            "_async_service_set_schedule",
        )


class NetatmoThermostat(NetatmoBase, ClimateEntity):
    """Representation a Netatmo thermostat."""

    def __init__(
        self, data_handler: NetatmoDataHandler, room: pyatmo.climate.NetatmoRoom
    ) -> None:
        """Initialize the sensor."""
        ClimateEntity.__init__(self)
        super().__init__(data_handler)

        self._id = room.entity_id
        self._home_id = room.home.entity_id

        self._climate_state_class = f"{CLIMATE_STATE_CLASS_NAME}-{self._home_id}"
        self._climate_state = data_handler.data[self._climate_state_class]

        self._data_classes.extend(
            [
                {
                    "name": CLIMATE_TOPOLOGY_CLASS_NAME,
                    SIGNAL_NAME: CLIMATE_TOPOLOGY_CLASS_NAME,
                },
                {
                    "name": CLIMATE_STATE_CLASS_NAME,
                    "home_id": self._home_id,
                    SIGNAL_NAME: self._climate_state_class,
                },
            ]
        )

        self._room = room
        self._model: str = getattr(room.device_type, "value")

        self._netatmo_type = TYPE_ENERGY

        self._device_name = self._room.name
        self._attr_name = f"{MANUFACTURER} {self._device_name}"
        self._current_temperature: float | None = None
        self._target_temperature: float | None = None
        self._preset: str | None = None
        self._away: bool | None = None
        self._operation_list = [HVAC_MODE_AUTO, HVAC_MODE_HEAT]
        self._support_flags = SUPPORT_FLAGS
        self._hvac_mode: str = HVAC_MODE_AUTO
        self._connected: bool | None = None

        self._away_temperature: float | None = None
        self._hg_temperature: float | None = None
        self._boilerstatus: bool | None = None
        self._selected_schedule = None

        if self._model == MODULE_TYPE_THERM:
            self._operation_list.append(HVAC_MODE_OFF)

        self._attr_max_temp = DEFAULT_MAX_TEMP
        self._attr_unique_id = f"{self._id}-{self._model}"

    async def async_added_to_hass(self) -> None:
        """Entity created."""
        await super().async_added_to_hass()

        for event_type in (
            EVENT_TYPE_SET_POINT,
            EVENT_TYPE_THERM_MODE,
            EVENT_TYPE_CANCEL_SET_POINT,
            EVENT_TYPE_SCHEDULE,
        ):
            self.data_handler.config_entry.async_on_unload(
                async_dispatcher_connect(
                    self.hass,
                    f"signal-{DOMAIN}-webhook-{event_type}",
                    self.handle_event,
                )
            )

        registry = await async_get_registry(self.hass)
        device = registry.async_get_device({(DOMAIN, self._id)}, set())
        assert device
        self.hass.data[DOMAIN][DATA_DEVICE_IDS][self._home_id] = device.id

        async_dispatcher_send(
            self.hass,
            NETATMO_CREATE_BATTERY,
            NetatmoDevice(
                self.data_handler,
                self._home_id,
                self._id,
                self._device_name,
                self._model,
                self._climate_state_class,
            ),
        )

    @callback
    def handle_event(self, event: dict) -> None:
        """Handle webhook events."""
        data = event["data"]

        if self._home_id != data["home_id"]:
            return

        if data["event_type"] == EVENT_TYPE_SCHEDULE and "schedule_id" in data:
            self._selected_schedule = getattr(
                self.hass.data[DOMAIN][DATA_SCHEDULES][self._home_id].get(
                    data["schedule_id"]
                ),
                "name",
                None,
            )
            self._attr_extra_state_attributes.update(
                {ATTR_SELECTED_SCHEDULE: self._selected_schedule}
            )
            self.async_write_ha_state()
            self.data_handler.async_force_update(self._climate_state_class)
            return

        home = data["home"]

        if self._home_id != home["id"]:
            return

        if data["event_type"] == EVENT_TYPE_THERM_MODE:
            self._preset = NETATMO_MAP_PRESET[home[EVENT_TYPE_THERM_MODE]]
            self._hvac_mode = HVAC_MAP_NETATMO[self._preset]
            if self._preset == PRESET_FROST_GUARD:
                self._target_temperature = self._hg_temperature
            elif self._preset == PRESET_AWAY:
                self._target_temperature = self._away_temperature
            elif self._preset == PRESET_SCHEDULE:
                self.async_update_callback()
                self.data_handler.async_force_update(self._climate_state_class)
            self.async_write_ha_state()
            return

        for room in home.get("rooms", []):
            if data["event_type"] == EVENT_TYPE_SET_POINT and self._id == room["id"]:
                if room["therm_setpoint_mode"] == STATE_NETATMO_OFF:
                    self._hvac_mode = HVAC_MODE_OFF
                    self._preset = STATE_NETATMO_OFF
                    self._target_temperature = 0
                elif room["therm_setpoint_mode"] == STATE_NETATMO_MAX:
                    self._hvac_mode = HVAC_MODE_HEAT
                    self._preset = PRESET_MAP_NETATMO[PRESET_BOOST]
                    self._target_temperature = DEFAULT_MAX_TEMP
                elif room["therm_setpoint_mode"] == STATE_NETATMO_MANUAL:
                    self._hvac_mode = HVAC_MODE_HEAT
                    self._target_temperature = room["therm_setpoint_temperature"]
                else:
                    self._target_temperature = room["therm_setpoint_temperature"]
                    if self._target_temperature == DEFAULT_MAX_TEMP:
                        self._hvac_mode = HVAC_MODE_HEAT
                self.async_write_ha_state()
                return

            if (
                data["event_type"] == EVENT_TYPE_CANCEL_SET_POINT
                and self._id == room["id"]
            ):
                self.async_update_callback()
                self.async_write_ha_state()
                return

    @property
    def _data(self) -> pyatmo.AsyncClimate:
        """Return data for this entity."""
        return cast(pyatmo.AsyncClimate, self._climate_state)

    @property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return self._support_flags

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return TEMP_CELSIUS

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._target_temperature

    @property
    def target_temperature_step(self) -> float | None:
        """Return the supported step of target temperature."""
        return PRECISION_HALVES

    @property
    def hvac_mode(self) -> str:
        """Return hvac operation ie. heat, cool mode."""
        return self._hvac_mode

    @property
    def hvac_modes(self) -> list[str]:
        """Return the list of available hvac operation modes."""
        return self._operation_list

    @property
    def hvac_action(self) -> str | None:
        """Return the current running hvac operation if supported."""
        if self._model == MODULE_TYPE_THERM and self._boilerstatus is not None:
            return CURRENT_HVAC_MAP_NETATMO[self._boilerstatus]
        # Maybe it is a valve
        if (
            heating_req := getattr(self._room, "heating_power_request", 0)
        ) is not None and heating_req > 0:
            return CURRENT_HVAC_HEAT
        return CURRENT_HVAC_IDLE

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """Set new target hvac mode."""
        if hvac_mode == HVAC_MODE_OFF:
            await self.async_turn_off()
        elif hvac_mode == HVAC_MODE_AUTO:
            if self.hvac_mode == HVAC_MODE_OFF:
                await self.async_turn_on()
            await self.async_set_preset_mode(PRESET_SCHEDULE)
        elif hvac_mode == HVAC_MODE_HEAT:
            await self.async_set_preset_mode(PRESET_BOOST)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if self.hvac_mode == HVAC_MODE_OFF:
            await self.async_turn_on()

        if self.target_temperature == 0:
            await self._data.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_HOME,
            )

        if (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX)
            and self._model == MODULE_TYPE_VALVE
            and self.hvac_mode == HVAC_MODE_HEAT
        ):
            await self._data.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_HOME,
            )
        elif (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX)
            and self._model == MODULE_TYPE_VALVE
        ):
            await self._data.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_MANUAL,
                DEFAULT_MAX_TEMP,
            )
        elif (
            preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX)
            and self.hvac_mode == HVAC_MODE_HEAT
        ):
            await self._data.async_set_room_thermpoint(self._id, STATE_NETATMO_HOME)
        elif preset_mode in (PRESET_BOOST, STATE_NETATMO_MAX):
            await self._data.async_set_room_thermpoint(
                self._id, PRESET_MAP_NETATMO[preset_mode]
            )
        elif preset_mode in (PRESET_SCHEDULE, PRESET_FROST_GUARD, PRESET_AWAY):
            await self._climate_state.async_set_thermmode(
                PRESET_MAP_NETATMO[preset_mode]
            )
        else:
            _LOGGER.error("Preset mode '%s' not available", preset_mode)

        self.async_write_ha_state()

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp."""
        return self._preset

    @property
    def preset_modes(self) -> list[str] | None:
        """Return a list of available preset modes."""
        return SUPPORT_PRESET

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature for 2 hours."""
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self._data.async_set_room_thermpoint(
            self._id, STATE_NETATMO_MANUAL, min(temp, DEFAULT_MAX_TEMP)
        )

        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        """Turn the entity off."""
        if self._model == MODULE_TYPE_VALVE:
            await self._data.async_set_room_thermpoint(
                self._id,
                STATE_NETATMO_MANUAL,
                DEFAULT_MIN_TEMP,
            )
        elif self.hvac_mode != HVAC_MODE_OFF:
            await self._data.async_set_room_thermpoint(self._id, STATE_NETATMO_OFF)
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn the entity on."""
        await self._data.async_set_room_thermpoint(self._id, STATE_NETATMO_HOME)
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """If the device hasn't been able to connect, mark as unavailable."""
        return bool(self._connected)

    @callback
    def async_update_callback(self) -> None:
        """Update the entity's state."""
        if not self._room.reachable:
            if self.available:
                self._connected = False
            return

        self._connected = True

        self._away_temperature = self._room.home.get_away_temp()
        self._hg_temperature = self._room.home.get_hg_temp()
        self._current_temperature = self._room.therm_measured_temperature
        self._target_temperature = self._room.therm_setpoint_temperature
        self._preset = NETATMO_MAP_PRESET[
            getattr(self._room, "therm_setpoint_mode", STATE_NETATMO_SCHEDULE)
        ]
        self._hvac_mode = HVAC_MAP_NETATMO[self._preset]
        self._away = self._hvac_mode == HVAC_MAP_NETATMO[STATE_NETATMO_AWAY]

        self._selected_schedule = getattr(
            self._room.home.get_selected_schedule(), "name", None
        )
        self._attr_extra_state_attributes.update(
            {ATTR_SELECTED_SCHEDULE: self._selected_schedule}
        )

        if self._model == MODULE_TYPE_VALVE:
            self._attr_extra_state_attributes[
                ATTR_HEATING_POWER_REQUEST
            ] = self._room.heating_power_request
        else:
            for module in self._room.modules.values():
                self._boilerstatus = module.boiler_status
                break

    async def _async_service_set_schedule(self, **kwargs: Any) -> None:
        schedule_name = kwargs.get(ATTR_SCHEDULE_NAME)
        schedule_id = None
        for sid, schedule in self.hass.data[DOMAIN][DATA_SCHEDULES][
            self._home_id
        ].items():
            if schedule.name == schedule_name:
                schedule_id = sid
                break

        if not schedule_id:
            _LOGGER.error("%s is not a valid schedule", kwargs.get(ATTR_SCHEDULE_NAME))
            return

        await self._climate_state.async_switch_home_schedule(schedule_id=schedule_id)
        _LOGGER.debug(
            "Setting %s schedule to %s (%s)",
            self._home_id,
            kwargs.get(ATTR_SCHEDULE_NAME),
            schedule_id,
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for the thermostat."""
        device_info: DeviceInfo = super().device_info
        device_info[ATTR_SUGGESTED_AREA] = self._device_name
        return device_info
