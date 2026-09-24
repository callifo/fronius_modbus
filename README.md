[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

# fronius_modbus
This is a fork of [callifo/fronius_modbus](https://github.com/callifo/fronius_modbus) (itself derived from redpomodoro/fronius_modbus), rewritten in 1.0 onto Home Assistant's shared Modbus connection and the `modbus-connection` library. Upstream issues fixed here are listed in the CHANGELOG.

Home Assistant custom component for reading data from Fronius GEN24 and Verto inverters, connected smart meters, and battery storage. Modbus TCP (SunSpec) is the primary source; the authenticated Fronius Web API adds setup assistance and battery controls that are not available over Modbus.

**Requirements:** Home Assistant 2026.9.0 or newer (the integration depends on the built-in `modbus` integration for its connection); Modbus TCP enabled on the inverter (the setup can enable it for you when a Web API password is provided).

> [!CAUTION]
> Version 1.0 is a rewrite of the Modbus layer. It has been verified against a Symo GEN24 10.0 with a BYD Battery-Box Premium HV and a Fronius Smart Meter TS 65A-3; other models should work through SunSpec discovery but are untested here. Breaking changes are listed in the CHANGELOG.
>
> This is an unofficial implementation and not supported by Fronius. It might stop working at any point in time.
> You are using this module (and its prerequisites/dependencies) at your own risk. Not me neither any of contributors to this or any prerequired/dependency project are responsible for damage in any kind caused by this project or any of its prerequisites/dependencies.

> [!IMPORTANT]
> It is recommended to keep the inverter up to date, this integration will only be tested on recent firmwares. Firmware below 1.40.7-1 has a Solar API issue that caused outages on GEN24 inverters; the integration raises a repair issue on such firmware and offers to disable the Solar API until the inverter is updated.

## What changed in 1.0

> [!IMPORTANT]
> Enum states are translation keys now: `Auto` became `auto`, `Charge from Grid` became `charge_from_grid`, `On grid operating` became `on_grid_operating`, `Enabled`/`Disabled` became `enabled`/`disabled`. Update automations and templates that compare against the old texts; see the CHANGELOG for the rule.

- **Minimum Home Assistant version: 2026.9.0.**
- The integration no longer opens its own Modbus TCP connection. It now asks Home Assistant's built-in `modbus` integration for a unit on a shared connection (`async_get_unit`), so it can coexist with other integrations talking to the same inverter without pymodbus version conflicts.
- The register map is discovered at runtime via SunSpec model walking (through the `modbus-connection` library) instead of a hand-written pymodbus client and fixed register list: models 1, 101/103, 120, 121, 122, 123, 124, 160, and 201-204 are read from the model chain. Battery storage entities appear if and only if model 124 is present, and smart meter phase count is derived from the meter model id.
- Per-value plausibility bounds are gone; SunSpec sentinel values and scale factors are decoded by the library instead.
- The Web API is now polled on its own interval (new option, see below) instead of once per Modbus poll. A slow or unreachable Web API no longer stalls the Modbus poll or blocks the Modbus entities.
- New option: **"Web API update interval (seconds)"** (default 60), separate from the Modbus poll interval.
- Diagnostics download (Settings -> Devices -> device -> Download diagnostics) now includes the raw SunSpec register map, with serial numbers redacted.
- If a firmware update changes the inverter's SunSpec model chain, the integration reloads the config entry automatically.
- Entity IDs, unique IDs, history/statistics, and existing options are unaffected by this change; storage modes, the AC-limit/power-factor enable pulse, and the battery API controls behave the same as before, with one correction: the grid charge/discharge power entities now scale by their own rate maximum (0.3 wrote them against the opposite one).

# Installation

## HACS installation

- Go to HACS
- Click on the 3 dots in the top right corner.
- Select "Custom repositories"
- Add the [URL](https://github.com/Varitras/fronius_modbus) to the repository.
- Select the 'integration' type.
- Click the "ADD" button.

## Manual installation

Copy contents of custom_components folder to your home-assistant config/custom_components folder.
After reboot of Home-Assistant, this integration can be configured through the integration setup UI.

## Inverter Setup

### Web API Assisted Setup

Choose either the `customer` or the `technician` local Web API role during setup and provide that role's password. The integration can then:

- auto-enable Modbus TCP during setup and relevant configuration changes
- handle the Modbus IP restriction of auto-enabled Modbus TCP by choice: keep the inverter's setting (default), restrict it and allow the Home Assistant host IP, or lift it. Allowing Home Assistant adds its address to the hosts already allowed instead of replacing them. The choice is applied again whenever the Modbus settings are written, so a lifted restriction stays lifted until you choose otherwise.
- leave the rest of the inverter's Modbus settings alone: the RS485 ports keep their master or slave role, and the `TCP & RTU` mode stays as it is
- derive configured smart meter addresses from `/api/components/PowerMeter/readable`
- expose authenticated battery controls from `/api/config/batteries`
- expose Modbus service diagnostics from `/api/config/modbus`
- expose the export limit control, with the `technician` role

![solar_login](images/solar_login.jpg?raw=true "storage")

The selected role is the local `customer` or `technician` login used when you connect with a web browser directly to the inverter by its LAN IP address. Your installer should have provided it during installation. It is not the Solar Web login used for the cloud (e.g. https://www.solarweb.com/). The `technician` role covers everything the `customer` role does and additionally exposes the export limit control. An entry uses exactly one role; reconfigure it to switch.
The integration stores a derived digest token in Home Assistant storage, readable by Home Assistant only, and does not keep the password in the config entry. The token is deleted when no entry uses its host and role any more: on removing the entry, or on moving it to another host or role.
During setup, reconfigure, or Repairs, the password is only requested if no stored token exists for the selected host and role or the existing token must be refreshed. Configure always offers the password step, so a stored token can be replaced.

### Migrating Older Entries

Entries created with older Modbus-only versions are migrated with safe defaults and keep working temporarily.
If an entry has no valid stored Web API token for the configured host and role, Home Assistant raises a Repairs item that lets you review the settings and enter that role's password to mint a new token.

## Charging From Grid

Turn off scheduled (dis)charging in the web UI to avoid unexpected behavior.

Grid charging stops at around 500 W while the inverter's own battery configuration
does not allow charging from the grid, whatever charge power is written over Modbus.
PV charging reaches full power in the same state, which makes this look like a Modbus
fault. Selecting `Charge from Grid` enables the `Charge from grid` and `Charge from AC`
toggles over the Web API, so a configured Web API clears this on its own; without one,
enable both in the inverter web UI. If the Web API refuses the toggles, Home Assistant
reports an error: the storage mode is then set, but grid charging is not.

# Usage

### Battery Storage

If Web API credentials are configured, the integration exposes both Modbus battery controls and authenticated battery API controls together.
The inverter keeps two independent switches, and the entities follow them:

- `Self-consumption optimisation` is `HYB_EM_MODE`, the inverter's automatic/manual energy management. `Target Feed In` belongs to it.
- `Web API SoC mode` is `BAT_M0_SOC_MODE`, the automatic/manual switch of the SoC window, and can be switched from Home Assistant. `SoC Minimum (Web API)` and `SoC Maximum` belong to it and are only writable while it is `manual`; switching self-consumption optimisation does not touch the window.
- `Modbus storage reserve` is model 124 `MinRsvPct`, a second minimum next to `SoC Minimum (Web API)`; see below.
- entering Modbus `Charge from Grid` also enables the Web API `Charge from grid` and `Charge from AC` toggles when Web API is configured
- turning on the Web API `Charge from grid` switch also enables `Charge from AC`
- `Target Feed In` is ignored by the inverter when battery charging is unavailable
- `Target Feed In` is only available while `Self-consumption optimisation` is `Manual`
- `Backup reserve` is independent of both switches

#### Two minimum SoC values

The inverter has two minimum-SoC settings, read by two different regulators, and the integration shows both:

| Entity                   | Register              | Who applies it                                                                                                                                                                       |
| ------------------------ | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `Modbus storage reserve` | model 124 `MinRsvPct` | The SunSpec storage control, only while a Modbus storage mode other than `Auto` is active (`StorCtl_Mod` not 0). In `Auto` the inverter ignores it.                                  |
| `SoC Minimum (Web API)`  | `BAT_M0_SOC_MIN`      | The inverter's own battery management, the value its web UI shows, whenever `Web API SoC mode` is `manual`. In `Auto` this is the only minimum that counts.                          |

Synchronisation goes one way: writing `Modbus storage reserve` also writes `SoC Minimum (Web API)` while the SoC mode is `manual`, because a web value under an automatic window would be ignored by the inverter. A change of the web minimum, in Home Assistant or in the inverter's UI, is not written into the Modbus register: that register only means something under an active Modbus mode, and the integration does not write registers whose effect depends on a mode the user did not choose.

In practice: if you only use the inverter's own battery management (storage mode `Auto`), set `Web API SoC mode` to `manual` and use `SoC Minimum (Web API)` and `SoC Maximum`; leave the Modbus reserve alone. If you use the Modbus storage modes, set `Modbus storage reserve`; it is mirrored to the web window while that is `manual`.

### Controls

| Entity                 | Description                                                                                                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discharge Limit        | This is maximum discharging power in watts of which the battery can be discharged by.                                                                                                                                 |
| Grid Charge Power      | The charging power in watts when the storage is being charged from the grid. Note that grid charging seems to be limited to an effective 50% by the hardware.                                                         |
| Grid Discharge Power   | The discharging power in watts when the storage is being discharged to the grid.                                                                                                                                      |
| Modbus storage reserve | Model 124 `MinRsvPct`, the reserve the inverter applies under Modbus storage control. Whole numbers only. While the SoC mode is `manual` it is mirrored to `SoC Minimum (Web API)` and must not exceed `SoC Maximum`. |
| PV Charge Limit        | This is maximum PV charging power in watts of which the battery can be charged by.                                                                                                                                    |

### Battery API Controls

| Entity                        | Description                                                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Self-consumption optimisation | `HYB_EM_MODE`: `Automatic` or `Manual`. Does not change the SoC window.                                                                                                                                                                                                                                                                                                             |
| Charge from AC                | Web API toggle for `HYB_BM_CHARGEFROMAC`. This is also auto-enabled when Modbus `Charge from Grid` is selected from the integration. Turning it off disables both charge-source flags.                                                                                                                                                                                              |
| Charge from grid              | Web API toggle for `HYB_EVU_CHARGEFROMGRID`. Turning it on also enables `Charge from AC`. Turning it off only disables the grid flag. This is also auto-enabled when Modbus `Charge from Grid` is selected from the integration.                                                                                                                                                    |
| Target Feed In                | Manual Fronius target feed-in in watts. Positive values target feed-in watts. Negative values target grid consumption watts, and the inverter will target that grid consumption even when PV power is available. This setting is ignored by the inverter when battery charging is unavailable. It is disabled unless `Self-consumption optimisation` is `Manual` (`HYB_EM_MODE=1`). |
| Web API SoC mode              | `BAT_M0_SOC_MODE`: `Automatic` or `Manual`. The SoC window below is only writable while it is `Manual`.                                                                                                                                                                                                                                                                             |
| SoC Maximum                   | `BAT_M0_SOC_MAX` from the Web API. Only available while `Web API SoC mode` is `manual`, and it must not be set below `SoC Minimum (Web API)`.                                                                                                                                                                                                                                       |
| SoC Minimum (Web API)         | `BAT_M0_SOC_MIN` from the Web API, the minimum shown in the inverter's own UI. Only available while `Web API SoC mode` is `manual`.                                                                                                                                                                                                                                                 |
| Backup reserve                | `HYB_BACKUP_RESERVED` from the Web API: the share of the battery kept for backup power, 5 to 100 percent. Independent of both switches.                                                                                                                                                                                                                                             |

### Storage Control Modes

| Mode                          | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auto                          | The inverter runs its own battery management; the `Modbus storage reserve` is not applied in this mode.                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| PV Charge Limit               | The storage can be charged with PV power at a limited rate. Limit will be set to maximum power after change.                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Discharge Limit               | The storage can be charged with PV power and discharged at a limited rate. in Fronius Web UI. Limit will be set to maximum power after change.                                                                                                                                                                                                                                                                                                                                                                                                                                |
| PV Charge and Discharge Limit | Allows setting both PV charge and discharge limits. Limits will be set to maximum power after change.                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Charge from Grid              | The storage will be charged from the grid using the charge rate from 'Grid Charge Power'. Power will be set 0 after change. Set the Grid Charge Power to a number in Watts, in a multiple of '10'. If the number is not rounded to 10, it will not work and does odd things like charging at 500W. If you need to press 'increment' to get it to charge, its likely the 10 issue. You do not need to touch the `Modbus storage reserve`. When this mode is selected from the integration and Web API is configured, `Charge from grid` and `Charge from AC` are also enabled. |
| Discharge to Grid             | The storage will discharge to the grid using the discharge rate from 'Grid Discharge Power'. Power will be set 0 after change.                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Block discharging             | The storage can only be charged with PV power. Charge limit will be set to maximum power.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Block charging                | The storage can only be discharged and won't be charged with PV power. Discharge limit will be set to maximum power.                                                                                                                                                                                                                                                                                                                                                                                                                                                          |

Note to change the mode first then set controls active in that mode. The mode names in this table are the display texts; the entity state and the `select_option` value are the keys `auto`, `pv_charge_limit`, `discharge_limit`, `pv_charge_and_discharge_limit`, `charge_from_grid`, `discharge_to_grid`, `block_discharging`, `block_charging`.

### Controls used by Modes

| Mode                          | Charge Limit   | Discharge Limit | Grid Charge Power | Grid Discharge Power | Modbus storage reserve |
| ----------------------------- | -------------- | --------------- | ----------------- | -------------------- | ---------------------- |
| Auto                          | Ignored (100%) | Ignored (100%)  | Ignored (0%)      | Ignored (0%)         | Ignored                |
| PV Charge Limit               | Used           | Ignored (100%)  | Ignored (0%)      | Ignored (0%)         | Used                   |
| Discharge Limit               | Ignored (100%) | Used            | Ignored (0%)      | Ignored (0%)         | Used                   |
| PV Charge and Discharge Limit | Used           | Used            | Ignored (0%)      | Ignored (0%)         | Used                   |
| Charge from Grid              | Ignored        | Ignored         | Used              | Ignored (0%)         | Used                   |
| Discharge to Grid             | Ignored        | Ignored         | Ignored (0%)      | Used                 | Used                   |
| Block discharging             | Used           | Ignored (0%)    | Ignored (0%)      | Ignored (0%)         | Used                   |
| Block charging                | Ignored (0%)   | Used            | Ignored (0%)      | Ignored (0%)         | Used                   |

### Fronius Web UI mapping

| Web UI name            | Integration Control  | Integration Mode  |
| ---------------------- | -------------------- | ----------------- |
| Max. charging power    | PV Charge Limit      | PV Charge Limit   |
| Min. charging power    | Grid Charge Power    | Charge from Grid  |
| Max. discharging power | Discharge Limit      | Discharge Limit   |
| Min. discharging power | Grid Discharge Power | Discharge to Grid |

### Battery Storage Sensors

| Entity                 | Description                                                                     |
| ---------------------- | ------------------------------------------------------------------------------- |
| Charge Status          | `holding` / `charging` / `discharging` (plus `off`, `empty`, `full`, `testing`) |
| Modbus storage reserve | Model 124 `MinRsvPct`. Not the SoC minimum of the inverter's own UI.            |
| State of Charge        | The current battery level                                                       |

### Inverter Sensors

| Entity                  | Description                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| Load                    | The current total power consumption which is derived by adding up the meter AC power and inverter AC power. |
| AC Current              | Total inverter AC current.                                                                                  |
| AC Current L1 / L2 / L3 | Per-phase inverter AC current.                                                                              |

### Smart Meter Sensors

| Entity                    | Description                                                                                                           |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| AC Current / L1 / L2 / L3 | Total and per-phase smart meter AC current.                                                                           |
| Power                     | Net grid power measured by the smart meter.                                                                           |
| Power L1 / L2 / L3        | Per-phase smart meter real power from SunSpec `WphA`, `WphB`, and `WphC`. The sign matches the meter power direction. |

### Component Sensors (Web API)

The inverter's component endpoints (`/api/components/inverter/readable` and `/api/components/BatteryManagementSystem/readable`) add values Modbus does not carry. They come with the web API poll that already runs; no extra request is made. A power module the inverter does not report creates no entity; one it has reported before keeps its entity, which then shows as unknown. A power module that appears later gets its entity at the next reload. If a power module entity stays permanently unknown, remove it manually in Home Assistant. An answer that names no power module at all keeps all four, as unknown. Any other value the inverter does not report shows as unknown. Firmware without these endpoints (HTTP 404) gets none of these sensors; a sensor registered before stays, as unknown, since one 404 from an endpoint that answered before proves nothing. No sensor takes their serial numbers, part serials or device ids; the battery's serial number shows on its device page, as before, and is redacted in diagnostics.

| Entity                                               | Device   | Default  | Description                                                                                  |
| ---------------------------------------------------- | -------- | -------- | -------------------------------------------------------------------------------------------- |
| Power module 1–4 temperature                         | Inverter | enabled  | Temperatures of the power modules the inverter reports.                                      |
| Fan 1 / 2                                            | Inverter | enabled  | Fan speed in percent.                                                                        |
| AC power L1 / L2 / L3                                | Inverter | enabled  | Per-phase active power of the inverter.                                                      |
| Production limit / Production limit reached          | Inverter | enabled  | The active power limit in effect, and whether the inverter is running at it.                 |
| Battery max charge / discharge power (DC-DC)         | Inverter | enabled  | What the battery converter can take or give right now.                                      |
| Grid valid                                           | Inverter | enabled  | The inverter's own verdict on the grid at its feed-in point.                                 |
| Power stage 1 / 2 firmware                           | Inverter | enabled  | Diagnostic.                                                                                  |
| Feed-in point voltage L1–L3, L1-L2–L3-L1, frequency  | Inverter | disabled | Grid side of the inverter's relays; differs from the AC output only while they are open.     |
| DC link voltage, Operating time, Power stage hardware | Inverter | disabled | Diagnostic. The operating time counts in seconds but is no exact clock.                      |
| Time in backup mode                                  | Inverter | disabled | Total time the inverter ran in backup mode.                                                  |
| State of health                                      | Battery  | enabled  | The battery's own estimate, in percent.                                                      |
| Cell temperature min / max, BMS ambient temperature  | Battery  | disabled |                                                                                              |
| Current discharge limit / Current power limit        | Battery  | disabled | The limits the battery management system reports right now.                                  |
| Peak charge / discharge power, Manufacturer SoC min / max, Voltage range min / max, Modules, Connection | Battery | disabled | Diagnostic, from the battery's nameplate and attributes. |

The battery's own firmware and hardware version appear on its device page.

### Inverter Diagnostics

| Entity                                       | Description                                                                                                                                                                                                                                                                |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grid status                                  | `on_grid_operating`, `on_grid`, `off_grid_operating` or `off_grid`, based on meter and inverter frequency. If inverter frequency is 53hz it is running in off grid mode and normally in 50hz. When the inverter is sleeping the meter frequency is checked for connection. |
| Throttle reason                              | Why the inverter limits its output: its own operating state, an active power setpoint, or an export limit switched on below full power. `none` when nothing limits, unknown while one of the sources could not be read.                                                    |
| Status / Vendor status                       | Standard SunSpec inverter state plus the Fronius vendor-specific state code.                                                                                                                                                                                               |
| Reference voltage / Reference voltage offset | SunSpec model 121 PCC voltage reference values exposed by the inverter.                                                                                                                                                                                                    |
| Web API Modbus mode / control / SunSpec mode | Authenticated Modbus service diagnostics from `/api/config/modbus`.                                                                                                                                                                                                        |
| Web API Modbus restriction / restriction IP  | Shows whether the inverter is restricting Modbus access by IP.                                                                                                                                                                                                             |

### Inverter Controls

| Entity               | Description                                                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| AC Limit Enable      | Allows limiting inverter AC output. Enable this setting first, and then set the AC limit below.                                 |
| AC Limit Rate        | Sets the AC limit in watts. Internally this is mapped to SunSpec `WMaxLimPct` (% of `WMax`) using the inverter scale factor.    |
| Power Factor Control | Enables or disables the Modbus fixed power factor control (`OutPFSet_Ena`).                                                     |
| Power Factor         | Fixed power factor (`OutPFSet`). Range is `-1.0` to `1.0`. Negative values are over-excited, positive values are under-excited. |

# Example Devices
These images are examples, and have recently changed slightly. They will be grouped into two categories one for the inverter, and one for the battery. Previously the WattMeter was shown separately. 

Battery Storage
![battery storage](images/example_batterystorage.jpg?raw=true "storage")

Smart Meter
![smart meter](images/example_meter.jpg?raw=true "meter")

Inverter
![smart meter](images/example_inverter.jpg?raw=true "inverter")

# References

- https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42,0410,2649.pdf
- https://github.com/binsentsu/home-assistant-solaredge-modbus/
- https://github.com/bigramonk/byd_charging

# Development

Development happens against a WSL2 Home Assistant test environment; the integration itself only needs a recent Home Assistant.

- `.github/scripts/check.sh` runs the gates of the Test workflow: ruff, mypy, pip-audit, gitleaks, pytest with a coverage gate, and a mutation run. Run it (or at least `pytest tests/ -q`) before calling a change done. The HACS and hassfest validations run in CI only.
- The default `pytest tests/ -q` run skips the end-to-end tests. Pass `-m ""` to include them.
- `tests/fixtures/symo_gen24_fw1386.json` is a captured SunSpec register map from a Fronius Symo GEN24 10.0 running firmware 1.38.6-1. To capture a fixture from another inverter/firmware combination: download diagnostics for the integration's device (Settings -> Devices -> device -> Download diagnostics), take the `registers` object (`{unit_id: {space: {address: word}}}`), prepend the SunSpec marker registers (40000/40001, `"SunS"`) and append the end-of-chain header (a model id of `0xFFFF`) if the diagnostics dump doesn't already include them, and blank out the serial number words before committing the fixture.
