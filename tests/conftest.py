"""Load production modules with lightweight HA API boundaries, without HA startup.

All register decoding, transport types, hub logic and polling methods are real.
These unit tests do not replace Home Assistant integration or hardware tests.
"""
import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


def module(name, **members):
    result = ModuleType(name)
    result.__dict__.update(members)
    sys.modules[name] = result
    return result


class Coordinator:
    def __init__(self, *args, **kwargs):
        pass


class UpdateFailed(Exception):
    pass


def values(*names):
    return SimpleNamespace(**{name: name.lower() for name in names})


module("homeassistant")
module("homeassistant.config_entries", ConfigEntry=object)
module("homeassistant.core", HomeAssistant=object)
module("homeassistant.helpers")
module("homeassistant.helpers.issue_registry")
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator, UpdateFailed=UpdateFailed)
module("homeassistant.helpers.storage", Store=object)
module("homeassistant.components")
module("homeassistant.components.sensor",
       SensorDeviceClass=values("BATTERY", "CURRENT", "ENERGY", "FREQUENCY", "POWER", "TEMPERATURE", "VOLTAGE"),
       SensorStateClass=values("MEASUREMENT", "TOTAL_INCREASING"))
module("homeassistant.helpers.entity", EntityCategory=values("DIAGNOSTIC"))
package = module("fronius_under_test")
package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/fronius_modbus")]
hub_module = importlib.import_module("fronius_under_test.hub")
client_module = importlib.import_module("fronius_under_test.froniusmodbusclient")
