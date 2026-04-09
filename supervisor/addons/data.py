"""Init file for Supervisor app data."""

from copy import deepcopy
from typing import Any

from ..const import (
    ATTR_IMAGE,
    ATTR_OPTIONS,
    ATTR_SYSTEM,
    ATTR_USER,
    ATTR_VERSION,
    FILE_HASSIO_ADDONS,
)
from ..coresys import CoreSys, CoreSysAttributes
from ..store.addon import AddonStore
from ..utils.common import FileConfiguration
from .addon import Addon
from .validate import SCHEMA_ADDONS_FILE

Config = dict[str, Any]


class AddonsData(FileConfiguration, CoreSysAttributes):
    """Hold data for installed Apps inside Supervisor."""

    def __init__(self, coresys: CoreSys):
        """Initialize data holder."""
        super().__init__(FILE_HASSIO_ADDONS, SCHEMA_ADDONS_FILE)
        self.coresys: CoreSys = coresys

    @property
    def user(self):
        """Return local app user data."""
        return self._data[ATTR_USER]

    @property
    def system(self):
        """Return local app data."""
        return self._data[ATTR_SYSTEM]

    async def install(self, addon: AddonStore) -> None:
        """Set app as installed."""
        self.system[addon.slug] = deepcopy(addon.data)
        self.user[addon.slug] = {
            ATTR_OPTIONS: {},
            ATTR_VERSION: addon.version,
            ATTR_IMAGE: addon.image,
        }
        await self.save_data()

    async def uninstall(self, addon: Addon) -> None:
        """Set app as uninstalled."""
        self.system.pop(addon.slug, None)
        self.user.pop(addon.slug, None)
        await self.save_data()

    async def update(self, addon: AddonStore) -> None:
        """Update version of app."""
        self.system[addon.slug] = deepcopy(addon.data)
        self.user[addon.slug].update(
            {ATTR_VERSION: addon.version, ATTR_IMAGE: addon.image}
        )
        await self.save_data()

    async def restore(
        self, slug: str, user: Config, system: Config, image: str
    ) -> None:
        """Restore data to app."""
        self.user[slug] = deepcopy(user)
        self.system[slug] = deepcopy(system)

        self.user[slug][ATTR_IMAGE] = image
        await self.save_data()
