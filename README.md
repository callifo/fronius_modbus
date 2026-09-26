[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

# fronius_modbus

Home Assistant custom component for reading data from Fronius GEN24 and Verto inverters, connected smart meters, and battery storage. Modbus TCP (SunSpec) is the primary source; the authenticated Fronius Web API is optional and adds setup assistance and battery controls that are not available over Modbus.

This is a fork of [callifo/fronius_modbus](https://github.com/callifo/fronius_modbus) (itself derived from redpomodoro/fronius_modbus), rewritten in 1.0 onto Home Assistant's shared Modbus connection and the `modbus-connection` library. Upstream issues fixed here are listed in the CHANGELOG.

> [!CAUTION]
> This is an unofficial implementation and not supported by Fronius. It might stop working at any point in time.
> You are using this module (and its prerequisites/dependencies) at your own risk. Neither I nor any contributor to this or any prerequisite or dependency project is responsible for damage of any kind caused by this project or any of its prerequisites or dependencies.

> [!IMPORTANT]
> It is recommended to keep the inverter up to date; this integration is only tested on recent firmware. Firmware below 1.40.7-1 has a Solar API issue that caused outages on GEN24 inverters; the integration raises a repair issue on such firmware and offers to disable the Solar API until the inverter is updated.

## Requirements

- Home Assistant 2026.9.0 or newer. The integration depends on the built-in `modbus` integration for its connection.
- Modbus TCP enabled on the inverter. The setup can enable it for you when a Web API password is provided.
- Optional: the password of a local Web API role. Without it the integration runs on Modbus alone; see [Without the web API](#without-the-web-api).

## Supported devices

Verified against a Symo GEN24 10.0 with a BYD Battery-Box Premium HV and a Fronius Smart Meter TS 65A-3. Other GEN24, Verto and Tauro models (Tauro ECO included), batteries and meters should work through SunSpec discovery, but are untested here; Fronius publishes the same SunSpec register maps for them, the Tauro without the storage model:

- the register map is discovered at runtime by walking the SunSpec model chain: models 1, 101/103, 120, 121, 122, 123, 124, 160 and 201-204
- battery storage entities appear if and only if model 124 is present
- a smart meter's phase count is derived from its model id

Older Fronius inverters with a Datamanager (the SnapINverter family: Symo, Primo, Eco, Galvo without GEN24) are untested. Their Web API has other logins and paths, so they can only be set up *Without the web API*, and only with the Datamanager's Modbus set to the `int+SF` SunSpec model type; with `float` the inverter model is not found and the setup stops, reporting that Modbus did not answer.

## Coming from upstream

What changes for users of [callifo/fronius_modbus](https://github.com/callifo/fronius_modbus) (checked against its 0.3.3). Breaking changes are listed in the CHANGELOG.

> [!IMPORTANT]
> Enum states are translation keys now: `Auto` became `auto`, `Charge from Grid` became `charge_from_grid`, `On grid operating` became `on_grid_operating`, `Enabled`/`Disabled` became `enabled`/`disabled`. Update automations and templates that compare against the old texts; see the CHANGELOG for the rule.

**Kept:** entity IDs, unique IDs, history/statistics and existing options. Existing entries are migrated on the first start.

**Connection and data**

- **Minimum Home Assistant version: 2026.9.0** (upstream: 2024.4.0).
- The integration no longer opens its own Modbus TCP connection with its own `pymodbus`. It asks Home Assistant's built-in `modbus` integration for a unit on a shared connection (`async_get_unit`), so it can coexist with other integrations talking to the same inverter without pymodbus version conflicts.
- Registers are decoded by the `modbus-connection` library from the SunSpec model definitions, including sentinel values and scale factors, instead of by hand-written decoding.
- If a firmware update changes the inverter's SunSpec model chain, the integration reloads the config entry automatically.
- The Web API is polled on its own interval (new option, default 60 s) instead of once per Modbus poll. A slow or unreachable Web API no longer stalls the Modbus poll or blocks the Modbus entities.
- An energy total still ignores a drop and an implausible jump, but accepts a genuine counter reset, such as a replaced meter, after three readings that agree. Upstream refuses every lower value for good.
- A control set to the value the inverter already holds writes nothing, over Modbus and over the Web API.

**Setup and security**

- The checkbox "Restrict Modbus to this IP" became the option **Modbus IP restriction** with three choices; the default keeps the inverter's setting. Upstream lifted the restriction whenever the box was unchecked, and a checked box replaced the allowed hosts with Home Assistant's address; allowing Home Assistant now adds its address to the hosts already allowed. Migrated entries keep their choice: checked becomes "restrict and allow this Home Assistant", unchecked becomes "keep".
- Enabling Modbus TCP keeps the rest of the inverter's Modbus settings. Upstream writes a fixed block that moves both RS485 ports to master and turns the `TCP & RTU` mode into TCP.
- The stored Web API token is readable by Home Assistant only, and it is deleted once no entry uses its host and role.
- The Web API login is optional: an entry can run on Modbus alone. Upstream requires it since 0.2.9.
- The inverter is discovered over mDNS and offered under Discovered, and an entry set up by IPv4 address follows it to a new address (see [Discovery](#discovery)). Upstream has no discovery.

**Battery**

- The SoC window follows the inverter's own switch: `SoC Maximum` and the new `SoC Minimum (Web API)` are writable while the new `Web API SoC mode` is manual. Upstream ties them to `Self-consumption optimisation`.
- `Grid Charge Power` and `Grid Discharge Power` scale by their own rate maximum; upstream writes each against the opposite one.

**New entities and tools**

- Component sensors from the inverter's Web API: power module temperatures, fans, per-phase AC power, production limit, grid validity, battery state of health and more (see [Component sensors](#component-sensors-web-api)).
- `Throttle reason`, `Web API SoC mode`, `SoC Minimum (Web API)`.
- Diagnostics download (Settings -> Devices -> device -> Download diagnostics), with the raw SunSpec register map; serial numbers are redacted.

## Installation

### HACS

1. Go to HACS.
2. Click on the three dots in the top right corner.
3. Select "Custom repositories".
4. Add the [URL](https://github.com/Varitras/fronius_modbus) of this repository.
5. Select the "Integration" type.
6. Click "ADD".

### Manual

Copy the contents of the `custom_components` folder to your Home Assistant `config/custom_components` folder. After a restart of Home Assistant, the integration can be set up through the integration setup UI.

## Setup

### Discovery

The inverter announces itself over mDNS (`_Fronius-SE-Inverter._tcp.local.`), and Home Assistant offers it under **Discovered** with its model name. Adding it opens the setup with the host filled in; the role and password are asked for as usual.

- mDNS stays within one network segment. An inverter in another subnet is found only if the router repeats mDNS between them (an mDNS repeater or reflector); otherwise add it by hand.
- An inverter announced with an IPv6 address only is not offered; add it by hand with its IPv4 address.
- An inverter already set up is not offered again, also when its entry uses a host name: the serial number it announces is matched against the one it reports over Modbus.
- An entry set up with an IPv4 address follows the inverter to a new address it announces: the host, the entry's title and the stored Web API token move with it. An entry set up with a host name is left as it is, since the name finds the new address itself. Anyone able to send mDNS in your network could announce another address for the same serial number; if that is a concern, set the entry up with a host name.

### Web API role

Choose the `customer` or the `technician` local Web API role during setup and provide that role's password, or choose *Without the web API* to set the entry up on Modbus alone (see [Without the web API](#without-the-web-api)).

![solar_login](images/solar_login.jpg?raw=true "storage")

The role is the local `customer` or `technician` login used when you connect with a web browser directly to the inverter by its LAN IP address. Your installer should have provided it during installation. It is not the Solar Web login used for the cloud (e.g. https://www.solarweb.com/). The `technician` role covers everything the `customer` role does and additionally exposes the export limit control. An entry uses exactly one role; reconfigure it to switch.

The integration stores a derived digest token in Home Assistant storage, readable by Home Assistant only, and does not keep the password in the config entry. The token is deleted when no entry uses its host and role any more: on removing the entry, or on moving it to another host or role. When discovery follows the inverter to a new address, the token moves with it. During setup, reconfigure, or a new login, the password is only requested if no stored token exists for the selected host and role or the existing token must be refreshed. Configure always offers the password step, so a stored token can be replaced.

### What the Web API setup does

With the Web API login, the integration can:

- enable Modbus TCP during setup and relevant configuration changes
- handle the Modbus IP restriction of auto-enabled Modbus TCP by choice (see [Configuration](#configuration))
- leave the rest of the inverter's Modbus settings alone: the RS485 ports keep their master or slave role, and the `TCP & RTU` mode stays as it is
- derive configured smart meter addresses from `/api/components/PowerMeter/readable`
- expose authenticated battery controls from `/api/config/batteries`
- expose Modbus service diagnostics from `/api/config/modbus`
- expose the export limit control, with the `technician` role

A host that another entry already serves is refused before the inverter is contacted. The check runs again right before the Modbus settings are written, so an entry that takes the host until then stops the setup there.

### Without the web API

Choose *Without the web API* as the access role to run the entry on Modbus alone. No password is asked for and no token is stored. Switch Modbus TCP on in the inverter's web UI first, with the SunSpec model type set to `int + SF`: without a login the setup cannot do either, and it reports *Modbus did not answer* until both are set.

Stays:

- all Modbus sensors and controls: storage control mode, charge and discharge limits, grid charge power, `Modbus storage reserve`, AC limit, power factor, inverter on/off
- the component sensors and the list of smart meters, since those endpoints answer without a login

Needs the web API, so not there:

- `Web API SoC mode`, `SoC Maximum`, `SoC Minimum (Web API)`, the backup reserve, self-consumption optimisation and its target feed in
- the `Charge from grid` and `Charge from AC` toggles, the Solar API switch, the export soft limit, `Reset Modbus Control`
- the Modbus service diagnostics, and enabling Modbus TCP during setup

`Charge from Grid` then stops at around 500 W unless grid charging is allowed in the inverter's own battery configuration; allow it once in the inverter web UI (see [Charging from the grid](#charging-from-the-grid)).

Switching an existing entry to *Without the web API* deletes its stored token and removes the entities that need the web API; their recorded history stays in Home Assistant. The removal waits for a start at which the list of smart meters can be read, like every cleanup; if the inverter reports no meter at all, delete those entities yourself. Switching back to a role creates them again and, as long as the entry lets the integration set up Modbus (the default), writes the Modbus settings, the IP restriction choice included, since nothing wrote them without the login. An entry that keeps a role but lost its login is not the same: its web entities stay, unavailable, and Home Assistant asks for a new login.

### Migrating older entries

Entries created with older Modbus-only versions are migrated with safe defaults and keep working temporarily. If an entry has no valid stored Web API token for the configured host and role, Home Assistant asks for a new login on the integration (**Reauthenticate**): choose the role and enter its password, or choose *Without the web API*. Host, intervals and the Modbus IP restriction stay as they are; change them with **Reconfigure**.

## Configuration

Set during setup; change them later with **Configure** or **Reconfigure** on the integration.

| Option                            | Default                     | Description                                                                                                                                                                                                                                                                          |
| --------------------------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Host                              |                             | The inverter's IP address or host name.                                                                                                                                                                                                                                              |
| Update interval (seconds)         | 10                          | The Modbus poll. Minimum 5.                                                                                                                                                                                                                                                          |
| Web API update interval (seconds) | 60                          | The Web API poll, separate from the Modbus poll. 5 to 3600.                                                                                                                                                                                                                          |
| Web API access role               | `customer`                  | `customer`, `technician` or *Without the web API*; see [Web API role](#web-api-role).                                                                                                                                                                                                |
| Modbus IP restriction             | Keep the inverter's setting | What the integration does with the inverter's Modbus IP restriction whenever it writes the Modbus settings: keep it, restrict Modbus and allow this Home Assistant (its address is added to the hosts already allowed), or lift it. A lifted restriction stays lifted until you choose otherwise. |

## Data updates

- The Modbus poll reads the inverter, its meters and the battery every update interval. The Web API is polled on its own interval; a slow or unreachable Web API does not hold up the Modbus entities.
- The component sensors come with the Web API poll that already runs; no extra request is made. They are read without a login, so they also run without the web API.
- A value that could not be read shows as unavailable or unknown; a failed sub-system is logged once when it fails and once when it answers again.
- A control set to the value the inverter already holds writes nothing. The comparison is made against a fresh read, so a value another controller changed in between is still written.

## Entities

### Inverter sensors

| Entity                  | Description                                                                                                 |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- |
| Load                    | The current total power consumption, derived by adding up the meter AC power and inverter AC power.         |
| AC Current              | Total inverter AC current.                                                                                  |
| AC Current L1 / L2 / L3 | Per-phase inverter AC current.                                                                              |

### Smart meter sensors

| Entity                    | Description                                                                                                           |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| AC Current / L1 / L2 / L3 | Total and per-phase smart meter AC current.                                                                           |
| Power                     | Net grid power measured by the smart meter.                                                                           |
| Power L1 / L2 / L3        | Per-phase smart meter real power from SunSpec `WphA`, `WphB`, and `WphC`. The sign matches the meter power direction. |
| Exported / Imported L1 / L2 / L3 | Per-phase energy counters (`TotWhExpPhA`-`C`, `TotWhImpPhA`-`C`). Created only for a meter that counts them, since some meters report 0, SunSpec's "not implemented"; a counter that starts counting later gets its entity at the next restart. Disabled by default. |

### Battery storage sensors

| Entity                 | Description                                                                     |
| ---------------------- | ------------------------------------------------------------------------------- |
| Charge Status          | `holding` / `charging` / `discharging` (plus `off`, `empty`, `full`, `testing`) |
| Modbus storage reserve | Model 124 `MinRsvPct`. Not the SoC minimum of the inverter's own UI.            |
| State of Charge        | The current battery level.                                                      |

### Component sensors (Web API)

The inverter's component endpoints (`/api/components/inverter/readable` and `/api/components/BatteryManagementSystem/readable`) add values Modbus does not carry.

- A value the inverter does not report shows as unknown.
- A power module the inverter does not report creates no entity; one it has reported before keeps its entity, as unknown. A power module that appears later gets its entity at the next reload.
- `Production power limit`, `Production power limit reached`, `Battery max charge power (DC-DC)` and `Battery max discharge power (DC-DC)` follow the same rule, since older firmware lacks these fields. Such sensors an entry already has from an earlier version stay; if they only ever show unknown, disable them.
- Firmware without these endpoints (HTTP 404) gets none of these sensors; a sensor registered before stays, as unknown, since one 404 from an endpoint that answered before proves nothing.
- No sensor takes their serial numbers, part serials or device ids; the battery's serial number shows on its device page, as before, and is redacted in diagnostics.

| Entity                                                                                                  | Device   | Default  | Description                                                                              |
| ------------------------------------------------------------------------------------------------------- | -------- | -------- | ---------------------------------------------------------------------------------------- |
| Power module 1–4 temperature                                                                            | Inverter | enabled  | Temperatures of the power modules the inverter reports.                                  |
| Fan 1 / 2                                                                                               | Inverter | enabled  | Fan speed in percent.                                                                    |
| AC power L1 / L2 / L3                                                                                   | Inverter | enabled  | Per-phase active power of the inverter.                                                  |
| Production power limit / Production power limit reached                                                 | Inverter | enabled  | The active power limit in effect, and whether the inverter is running at it.             |
| Battery max charge / discharge power (DC-DC)                                                            | Inverter | enabled  | What the battery converter can take or give right now.                                   |
| Grid valid                                                                                              | Inverter | enabled  | The inverter's own verdict on the grid at its feed-in point.                             |
| Power stage 1 / 2 firmware                                                                              | Inverter | enabled  | Diagnostic.                                                                              |
| Feed-in point voltage L1–L3, L1-L2–L3-L1, frequency                                                     | Inverter | disabled | Grid side of the inverter's relays; differs from the AC output only while they are open. |
| DC link voltage, Operating time, Power stage hardware                                                   | Inverter | disabled | Diagnostic. The operating time counts in seconds but is no exact clock.                  |
| Time in backup mode                                                                                     | Inverter | disabled | Total time the inverter ran in backup mode.                                              |
| State of health                                                                                         | Battery  | enabled  | The battery's own estimate, in percent.                                                  |
| Cell temperature min / max, BMS ambient temperature                                                     | Battery  | disabled |                                                                                          |
| Current discharge limit / Current power limit                                                           | Battery  | disabled | The limits the battery management system reports right now.                              |
| Peak charge / discharge power, Manufacturer SoC min / max, Voltage range min / max, Modules, Connection | Battery  | disabled | Diagnostic, from the battery's nameplate and attributes.                                 |

The battery's own firmware and hardware version appear on its device page.

### Inverter diagnostics

| Entity                                       | Description                                                                                                                                                                                                                                                                  |
| -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Grid status                                  | `on_grid_operating`, `on_grid`, `off_grid_operating` or `off_grid`, based on meter and inverter frequency. An inverter frequency of 53 Hz means off-grid operation; normally it is 50 Hz. While the inverter sleeps, the meter frequency is checked for the connection. |
| Throttle reason                              | Why the inverter limits its output: its own operating state, an active power setpoint, or the AC limit (`AC limit enable`, `AC limit rate`) switched on below full power. `none` when nothing limits, unknown while one of the sources could not be read. An export limit set in the inverter's web interface is not among them: the inverter reports it in none of these signals, so an inverter held at its export limit shows `none`.                                                      |
| Status / Vendor status                       | Standard SunSpec inverter state plus the Fronius vendor-specific state code.                                                                                                                                                                                                 |
| Reference voltage / Reference voltage offset | SunSpec model 121 PCC voltage reference values exposed by the inverter.                                                                                                                                                                                                      |
| Web API Modbus mode / control / SunSpec mode | Authenticated Modbus service diagnostics from `/api/config/modbus`. A value the inverter answers in an unexpected shape shows as unknown.                                                                                                                                      |
| Web API Modbus restriction / restriction IP  | Whether the inverter is restricting Modbus access by IP.                                                                                                                                                                                                                     |

### Inverter controls

| Entity               | Description                                                                                                                                                              |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| AC Limit Enable      | Allows limiting inverter AC output. Enable this setting first, and then set the AC limit below.                                                                          |
| AC Limit Rate        | Sets the AC limit in watts. Internally this is mapped to SunSpec `WMaxLimPct` (% of `WMax`) using the inverter scale factor.                                             |
| Power Factor Control | Enables or disables the Modbus fixed power factor control (`OutPFSet_Ena`).                                                                                              |
| Power Factor         | Fixed power factor (`OutPFSet`). Range is `-1.0` to `1.0`. Negative values are over-excited, positive values are under-excited.                                          |
| Inverter connection  | Connects or disconnects the inverter from the grid over Modbus (`Conn`).                                                                                                 |
| Export soft limit    | The soft export limit in watts, over the Web API. Requires the `technician` role.                                                                                        |
| Solar API            | Switches the inverter's Solar API v1 on or off over the Web API.                                                                                                         |
| Reset Modbus Control | Sends the inverter's Modbus reset command (`/api/commands/ModbusReset`) over the Web API. Diagnostic.                                                                   |

A new value for an active AC limit or power factor is applied by switching the control off and on around the write, as the inverter requires; if the write fails, the control is switched back on.

## Battery control

If Web API credentials are configured, the integration exposes both Modbus battery controls and authenticated battery API controls together. Without the web API only the Modbus controls are there.

### Storage control modes

`Storage Control Mode` selects how the battery is controlled over Modbus. Change the mode first, then set the controls active in that mode. The mode names in this table are the display texts; the entity state and the `select_option` value are the keys `auto`, `pv_charge_limit`, `discharge_limit`, `pv_charge_and_discharge_limit`, `charge_from_grid`, `discharge_to_grid`, `block_discharging`, `block_charging`.

| Mode                          | Description                                                                                                                                                                                                                  |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Auto                          | The inverter runs its own battery management; the `Modbus storage reserve` is not applied in this mode.                                                                                                                      |
| PV Charge Limit               | The storage can be charged with PV power at a limited rate. The limit is set to maximum power after the change.                                                                                                              |
| Discharge Limit               | The storage can be charged with PV power and discharged at a limited rate. The limit is set to maximum power after the change.                                                                                              |
| PV Charge and Discharge Limit | Allows setting both PV charge and discharge limits. The limits are set to maximum power after the change.                                                                                                                    |
| Charge from Grid              | The storage is charged from the grid at the rate from `Grid Charge Power`, which is set to 0 after the change. See [Charging from the grid](#charging-from-the-grid).                                                        |
| Discharge to Grid             | The storage discharges to the grid at the rate from `Grid Discharge Power`, which is set to 0 after the change.                                                                                                             |
| Block discharging             | The storage can only be charged with PV power. The charge limit is set to maximum power.                                                                                                                                    |
| Block charging                | The storage can only be discharged and is not charged with PV power. The discharge limit is set to maximum power.                                                                                                          |

### Modbus controls

| Entity                 | Description                                                                                                                                                                                                           |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| PV Charge Limit        | The maximum PV charging power in watts.                                                                                                                                                                               |
| Discharge Limit        | The maximum discharging power in watts.                                                                                                                                                                               |
| Grid Charge Power      | The charging power in watts when the storage is charged from the grid. Grid charging has been reported to reach only about 50% of the set power.                                                                     |
| Grid Discharge Power   | The discharging power in watts when the storage is discharged to the grid.                                                                                                                                            |
| Modbus storage reserve | Model 124 `MinRsvPct`, the reserve the inverter applies under Modbus storage control. Whole numbers only. While the SoC mode is `manual` it is mirrored to `SoC Minimum (Web API)` and must not exceed `SoC Maximum`. |

Which control each mode uses:

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

How the modes relate to the time-dependent battery control of the Fronius web UI:

| Web UI name            | Integration control  | Integration mode  |
| ---------------------- | -------------------- | ----------------- |
| Max. charging power    | PV Charge Limit      | PV Charge Limit   |
| Min. charging power    | Grid Charge Power    | Charge from Grid  |
| Max. discharging power | Discharge Limit      | Discharge Limit   |
| Min. discharging power | Grid Discharge Power | Discharge to Grid |

### Web API controls

The inverter keeps two independent switches, and the entities follow them:

- `Self-consumption optimisation` is `HYB_EM_MODE`, the inverter's automatic/manual energy management. `Target Feed In` belongs to it.
- `Web API SoC mode` is `BAT_M0_SOC_MODE`, the automatic/manual switch of the SoC window. `SoC Minimum (Web API)` and `SoC Maximum` belong to it; switching self-consumption optimisation does not touch the window.
- `Backup reserve` is independent of both switches.

| Entity                        | Description                                                                                                                                                                                                                                                 |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Self-consumption optimisation | `HYB_EM_MODE`: `Automatic` or `Manual`. Does not change the SoC window.                                                                                                                                                                                     |
| Target Feed In                | Manual Fronius target feed-in in watts. Positive values target feed-in, negative values target grid consumption, which the inverter then targets even when PV power is available. Ignored by the inverter when battery charging is unavailable. Only available while `Self-consumption optimisation` is `Manual` (`HYB_EM_MODE=1`). |
| Web API SoC mode              | `BAT_M0_SOC_MODE`: `Automatic` or `Manual`. The SoC window below is only writable while it is `Manual`.                                                                                                                                                    |
| SoC Maximum                   | `BAT_M0_SOC_MAX`. Only available while `Web API SoC mode` is `Manual`, and it must not be set below `SoC Minimum (Web API)`.                                                                                                                               |
| SoC Minimum (Web API)         | `BAT_M0_SOC_MIN`, the minimum shown in the inverter's own UI. Only available while `Web API SoC mode` is `Manual`.                                                                                                                                         |
| Backup reserve                | `HYB_BACKUP_RESERVED`: the share of the battery kept for backup power, 5 to 100 percent.                                                                                                                                                                    |
| Charge from grid              | Toggle for `HYB_EVU_CHARGEFROMGRID`. Turning it on also enables `Charge from AC`; turning it off only disables the grid flag.                                                                                                                              |
| Charge from AC                | Toggle for `HYB_BM_CHARGEFROMAC`. Turning it off disables both charge-source flags.                                                                                                                                                                        |

### Two minimum SoC values

The inverter has two minimum-SoC settings, read by two different regulators, and the integration shows both:

| Entity                   | Register              | Who applies it                                                                                                                                              |
| ------------------------ | --------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Modbus storage reserve` | model 124 `MinRsvPct` | The SunSpec storage control, only while a Modbus storage mode other than `Auto` is active (`StorCtl_Mod` not 0). In `Auto` the inverter ignores it.         |
| `SoC Minimum (Web API)`  | `BAT_M0_SOC_MIN`      | The inverter's own battery management, the value its web UI shows, whenever `Web API SoC mode` is `Manual`. In `Auto` this is the only minimum that counts. |

Synchronisation goes one way: writing `Modbus storage reserve` also writes `SoC Minimum (Web API)` while the SoC mode is `Manual`, because a web value under an automatic window would be ignored by the inverter. A change of the web minimum, in Home Assistant or in the inverter's UI, is not written into the Modbus register: that register only means something under an active Modbus mode, and the integration does not write registers whose effect depends on a mode the user did not choose.

While the SoC mode is `Manual`, a new `Modbus storage reserve` is first checked against the SoC window the inverter holds. If that window cannot be read, nothing is written. If the Web API refuses the minimum after Modbus took it, the error says so: the two minimums then differ until one of them is set again.

In practice: if you only use the inverter's own battery management (storage mode `Auto`), set `Web API SoC mode` to `Manual` and use `SoC Minimum (Web API)` and `SoC Maximum`; leave the Modbus reserve alone. If you use the Modbus storage modes, set `Modbus storage reserve`; it is mirrored to the web window while that is `Manual`.

### Charging from the grid

Turn off scheduled (dis)charging in the inverter's web UI to avoid unexpected behavior.

Set `Grid Charge Power` to a multiple of 10 W. Other values do not work and lead to odd behavior such as charging at about 500 W; if you need to press "increment" to get the battery to charge, this is the likely cause. You do not need to touch the `Modbus storage reserve`.

Grid charging also stops at around 500 W while the inverter's own battery configuration does not allow charging from the grid, whatever charge power is written over Modbus. PV charging reaches full power in the same state, which makes this look like a Modbus fault. Selecting `Charge from Grid` therefore enables the `Charge from grid` and `Charge from AC` toggles over the Web API, so a configured Web API clears this on its own; without one, enable both in the inverter web UI. If the Web API refuses the toggles, Home Assistant reports an error: the storage mode is then set, but grid charging is not.

## Use cases

- **Dynamic electricity prices:** charge the battery from the grid in the cheap hours (`Charge from Grid` with `Grid charge power`) and keep it from discharging while the price is low (`Block Discharging`).
- **Negative prices or an export cap:** limit the inverter's output with `AC limit enable` and `AC limit rate`, or, with the `technician` role, only the feed-in with the export soft limit.
- **Keeping energy for later:** hold the battery for the evening or for backup power with `Block Discharging`, the `Modbus storage reserve` (it applies in a Modbus storage mode, not in `Auto`), or the `Backup reserve` of the inverter's own battery management.
- **Monitoring:** household load, grid power per phase, battery state of charge and state of health, power module temperatures and fans, and why the inverter throttles (`Throttle reason`), all without a cloud connection.
- **Modbus only:** read the inverter and steer the battery over Modbus without handing Home Assistant a web password (see [Without the web API](#without-the-web-api)).

## Examples

The entity ids below are examples; take yours from Settings -> Devices & services -> Entities. The storage mode is set first, then the value it uses: a mode change resets its rate (see [Storage control modes](#storage-control-modes)).

Charge the battery from the grid at night, and hand it back to the inverter in the morning:

```yaml
automation:
  - alias: "Battery: charge from the grid at night"
    triggers:
      - trigger: time
        at: "02:00:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.fronius_storage_control_mode
        data:
          option: charge_from_grid
      - action: number.set_value
        target:
          entity_id: number.fronius_grid_charge_power
        data:
          value: 3000
  - alias: "Battery: back to automatic"
    triggers:
      - trigger: time
        at: "05:00:00"
    actions:
      - action: select.select_option
        target:
          entity_id: select.fronius_storage_control_mode
        data:
          option: auto
```

Keep the battery from discharging into the car while it charges:

```yaml
automation:
  - alias: "Battery: hold while the car charges"
    triggers:
      - trigger: state
        entity_id: binary_sensor.car_charging
        to: ["on", "off"]
    actions:
      - action: select.select_option
        target:
          entity_id: select.fronius_storage_control_mode
        data:
          option: "{{ 'block_discharging' if trigger.to_state.state == 'on' else 'auto' }}"
```

Switch the inverter's output off while the price is negative. `AC limit rate` limits everything the inverter puts out, not only the feed-in, so the house then draws from the grid, which a negative price pays for:

```yaml
automation:
  - alias: "Inverter: off at negative prices"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.electricity_price
        below: 0
    actions:
      - action: select.select_option
        target:
          entity_id: select.fronius_ac_limit_enable
        data:
          option: enabled
      - action: number.set_value
        target:
          entity_id: number.fronius_ac_limit_rate
        data:
          value: 0
  - alias: "Inverter: on again"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.electricity_price
        above: 0
    actions:
      - action: select.select_option
        target:
          entity_id: select.fronius_ac_limit_enable
        data:
          option: disabled
```

## Known limitations

- Models other than the verified setup (see [Supported devices](#supported-devices)) are untested.
- A power module or a limit sensor that appears later, for example after a firmware update, gets its entity at the next reload of the entry.
- The Web API cannot be reached through an IPv6 address; set the entry up with an IPv4 address or a host name.
- A power module entity that stays unknown for good, because the module is gone or because a development build created it as a placeholder, can be disabled in Home Assistant. It cannot be deleted: the integration keeps every module the inverter once reported, so one incomplete answer cannot take a real module with its history.
- When the last smart meter is removed, its entities stay unavailable; delete them in Home Assistant, where they show as no longer provided. An empty meter list from the inverter is not taken as proof that no meter exists, so one short answer cannot delete meter entities and their history.
- While the SoC mode is `Manual`, the `Modbus storage reserve` cannot be changed while the Web API does not answer (see [Two minimum SoC values](#two-minimum-soc-values)).
- Scheduled (dis)charging in the inverter's web UI and the Modbus storage modes get in each other's way; turn the schedule off when using the modes.

## Troubleshooting

- **Diagnostics:** Settings -> Devices & services -> Fronius Modbus -> device -> Download diagnostics. The download includes which sub-systems the last poll read and which failed, the Web API values, and the raw SunSpec register map; serial numbers and the Modbus restriction IP are redacted. It also works while the inverter is offline.
- **New login:** Home Assistant shows *Reauthentication required* on the integration when an entry has no valid token for its host and role, for example after the password was changed on the inverter. Follow it to enter the role's password again, or choose *Without the web API* to run the entry on Modbus alone.
- **Repairs:**
  - *Disable Fronius Solar API on older firmware* appears on firmware below 1.40.7-1 with the Solar API switched on. It offers to switch the Solar API off until the inverter is updated.
- **Discovery:** no card appears for an inverter that is already set up, one in another subnet without an mDNS repeater between them, or one announced with an IPv6 address only (see [Discovery](#discovery)).
- **Logs:** a sub-system that stops answering (inverter, a meter, the battery, the Web API) is logged once when it fails and once when it answers again. Web API errors name the error type, not the host.

## Removal

1. Settings -> Devices & services -> Fronius Modbus -> the entry's menu -> Delete.
2. The stored Web API token for the entry's host and role is deleted with it, unless another entry still uses them.
3. The Modbus settings the integration enabled on the inverter (Modbus TCP, SunSpec mode, IP restriction) stay as they are; change them in the inverter's web UI if you no longer need them.
4. For a HACS installation, remove the integration in HACS as well.

## Example devices

The entities are grouped into devices: the inverter, the battery storage, and each smart meter.

Battery Storage
![battery storage](images/example_batterystorage.jpg?raw=true "storage")

Smart Meter
![smart meter](images/example_meter.jpg?raw=true "meter")

Inverter
![inverter](images/example_inverter.jpg?raw=true "inverter")

## References

- https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42,0410,2649.pdf
- https://github.com/binsentsu/home-assistant-solaredge-modbus/
- https://github.com/bigramonk/byd_charging

## Development

Development happens against a WSL2 Home Assistant test environment; the integration itself only needs a recent Home Assistant.

- `.github/scripts/check.sh` runs the gates of the Test workflow: ruff, mypy, pip-audit, gitleaks, pytest with a coverage gate, and a mutation run. Run it (or at least `pytest tests/ -q`) before calling a change done. The HACS and hassfest validations run in CI only.
- The default `pytest tests/ -q` run skips the end-to-end tests. Pass `-m ""` to include them.
- `tests/fixtures/symo_gen24_fw1386.json` is a captured SunSpec register map from a Fronius Symo GEN24 10.0 running firmware 1.38.6-1. To capture a fixture from another inverter/firmware combination: download diagnostics for the integration's device (Settings -> Devices -> device -> Download diagnostics), take the `registers` object (`{unit_id: {space: {address: word}}}`), prepend the SunSpec marker registers (40000/40001, `"SunS"`) and append the end-of-chain header (a model id of `0xFFFF`) if the diagnostics dump doesn't already include them, and blank out the serial number words before committing the fixture.
