# NSW Rural Fire Service Fire Danger.
import asyncio
import logging
from datetime import timedelta

import voluptuous as vol
import xmltodict
from homeassistant.components.rest.data import RestData
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    STATE_OK,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from pyexpat import ExpatError

from .config_flow import configured_instances
from .const import (
    COMPONENTS,
    CONF_DISTRICT_NAME,
    DEFAULT_ATTRIBUTION,
    DEFAULT_METHOD,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    SENSOR_ATTRIBUTES,
    DEFAULT_URL,
    XML_DISTRICT,
    XML_FIRE_DANGER_MAP,
    XML_NAME,
    ESA_URL,
    ACT_DEFAULT_ATTRIBUTION,
    ESA_DISTRICTS,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_DISTRICT_NAME): cv.string,
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): cv.time_period,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the NSW Rural Fire Service Fire Danger component."""
    if DOMAIN not in config:
        return True

    conf = config[DOMAIN]
    district_name = conf.get[CONF_DISTRICT_NAME]
    scan_interval = conf[CONF_SCAN_INTERVAL]
    identifier = f"{district_name}"
    if identifier in configured_instances(hass):
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={CONF_DISTRICT_NAME: district_name, CONF_SCAN_INTERVAL: scan_interval},
        )
    )
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Set up the NSW Rural Fire Service Fire Danger component as config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Create feed entity manager for all platforms.
    manager = NswRfsFireDangerFeedEntityManager(hass, config_entry)
    hass.data[DOMAIN][config_entry.entry_id] = manager
    _LOGGER.debug("Feed entity manager added for %s", config_entry.entry_id)
    await manager.async_init()
    await manager.async_update()
    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry):
    """Unload an NSW Rural Fire Service Fire Danger component config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(config_entry, component)
                for component in COMPONENTS
            ]
        )
    )
    if unload_ok:
        manager = hass.data[DOMAIN].pop(config_entry.entry_id)
        await manager.async_stop()
    return unload_ok


class NswRfsFireDangerApi:
    URL = DEFAULT_URL
    ATTRIBUTION = DEFAULT_ATTRIBUTION

    def __init__(self, hass):
        self.hass = hass
        self.rest = RestData(
            self.hass,
            DEFAULT_METHOD,
            self.URL,
            None,
            None,
            None,
            None,
            DEFAULT_VERIFY_SSL,
        )
        self._data = None

    async def async_update(self):
        await self.rest.async_update()
        self._data = self.rest.data

    @property
    def attribution(self):
        return self.ATTRIBUTION

    @property
    def data(self):
        return self._data

    @property
    def extra_attrs(self):
        return dict()


class ActEsaFireDangerApi(NswRfsFireDangerApi):
    URL = ESA_URL
    ATTRIBUTION = ACT_DEFAULT_ATTRIBUTION

    async def async_update(self):
        await super().async_update()
        # At the end of the bushfire season, the ESA return a blank file
        # TODO: do a better check of whats going on
        if not self._data:
            api = NswRfsFireDangerApi(self.hass)
            await api.rest.async_update()
            self._data = api.rest.data
            self.DEFAULT_ATTRIBUTION = (
                api.DEFAULT_ATTRIBUTION
            )  # TODO: This should likely b e a property or something
            _LOGGER.warn("Requested data from ESA API but falling back to RFS")

    @property
    def extra_attrs(self):
        import xmltodict

        if not self.data:
            return dict()

        parse = xmltodict.parse(self.data)
        if "rss" not in parse:
            return dict()

        value = parse["rss"]["channel"]

        return {"publish date": value["pubDate"], "build date": value["lastBuildDate"]}


class NswRfsFireDangerFeedEntityManager:
    """Feed Entity Manager for NSW Rural Fire Service Fire Danger feed."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry):
        """Initialize the Feed Entity Manager."""
        self._hass = hass
        self._config_entry = config_entry
        self._district_name = config_entry.data[CONF_DISTRICT_NAME]
        self._config_entry_id = config_entry.entry_id
        self._scan_interval = timedelta(seconds=config_entry.data[CONF_SCAN_INTERVAL])
        self._track_time_remove_callback = None
        self._attributes = None
        if self._district_name in ESA_DISTRICTS:
            self._api = ActEsaFireDangerApi(self._hass)
            _LOGGER.info(
                "District {} is in ACT ESA jurisdiction".format(self._district_name)
            )
        else:
            self._api = NswRfsFireDangerApi(self._hass)
            _LOGGER.info(
                "District {} is in NSW RFS jurisdiction".format(self._district_name)
            )

    @property
    def district_name(self) -> str:
        """Return the district name of the manager."""
        return self._district_name

    @property
    def attribution(self):
        """Return the attribution of the manager."""
        return self._api.attribution

    @property
    def attributes(self):
        """Return the district name of the manager."""
        return self._attributes

    async def async_init(self):
        """Schedule initial and regular updates based on configured time interval."""

        for component in COMPONENTS:
            self._hass.async_create_task(
                self._hass.config_entries.async_forward_entry_setup(
                    self._config_entry, component
                )
            )

        async def update(event_time):
            """Update."""
            await self.async_update()

        # Trigger updates at regular intervals.
        self._track_time_remove_callback = async_track_time_interval(
            self._hass, update, self._scan_interval
        )

        _LOGGER.debug("Feed entity manager initialized")

    async def async_update(self):
        """Get the latest data from REST API and update the state."""
        _LOGGER.debug("Start updating feed")
        await self._api.async_update()
        value = self._api.data
        attributes = {}
        self._state = STATE_UNKNOWN
        if value:
            try:
                value = xmltodict.parse(value)

                # The ESA has some extra data we need to strip
                if XML_FIRE_DANGER_MAP not in value:
                    value = value["rss"]["channel"]
                    value[XML_FIRE_DANGER_MAP][XML_DISTRICT] = [
                        value[XML_FIRE_DANGER_MAP][XML_DISTRICT]
                    ]

                districts = {
                    k[XML_NAME]: dict(k)
                    for k in value[XML_FIRE_DANGER_MAP][XML_DISTRICT]
                }
                district = districts.get(self._district_name)

                for key, replacement in SENSOR_ATTRIBUTES.items():
                    if key not in district:
                        # Ignore items not in sensor_attributes
                        continue
                    text_value = district.get(key)
                    conversion = replacement[1]
                    if conversion:
                        text_value = conversion(text_value)
                    attributes[replacement[0]] = text_value

                self._state = STATE_OK
                self._attributes = attributes
                # Dispatch to sensors.
                async_dispatcher_send(
                    self._hass,
                    f"nsw_rfs_fire_danger_update_{self._district_name}",
                )
            except ExpatError as ex:
                _LOGGER.warning("Unable to parse feed data: %s", ex)

    async def async_stop(self):
        """Stop this feed entity manager from refreshing."""
        if self._track_time_remove_callback:
            self._track_time_remove_callback()
        _LOGGER.debug("Feed entity manager stopped")
