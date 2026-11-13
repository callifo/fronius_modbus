[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

# fronius_modbus

Home Assistant custom component for reading data from Fronius GEN24 and Verto inverters, connected smart meters, and battery storage. Modbus TCP (SunSpec) is the primary source; the authenticated Fronius Web API is optional and adds setup assistance and battery controls that are not available over Modbus. The inverter's web interface also reports module temperatures, fans, battery health and the inverter's event log, without a login.

It began as redpomodoro/fronius_modbus and continued here. Version 1.0 is a rewrite onto Home Assistant's shared Modbus connection and the `modbus-connection` library.

**The full documentation is in the [wiki](https://github.com/callifo/fronius_modbus/wiki):** every entity, the battery and inverter controls, the inverter's events, how data updates, examples, troubleshooting and migration.

> [!CAUTION]
> This is an unofficial implementation and not supported by Fronius. It might stop working at any point in time.
> You are using this module (and its prerequisites/dependencies) at your own risk. Neither I nor any contributor to this or any prerequisite or dependency project is responsible for damage of any kind caused by this project or any of its prerequisites or dependencies.

> [!IMPORTANT]
> It is recommended to keep the inverter up to date; this integration is only tested on recent firmware. Firmware below 1.40.7-1 has a Solar API issue that caused outages on GEN24 inverters; with a Web API role and the Solar API switched on, the integration raises a repair issue on such firmware and offers to disable the Solar API until the inverter is updated.

## Features

- Inverter, battery and smart meter values over Modbus TCP (SunSpec), with the register map discovered at runtime: AC and DC power, energy, MPPT strings, battery state of charge, grid power per phase, household load, grid status. The grid meter's power can be read on its own, as often as every second, for a battery control that follows feed-in and draw.
- Battery control over Modbus: eight storage modes (such as *Charge from Grid*, *Block Discharging*), charge and discharge limits, grid charge power, storage reserve.
- Inverter control over Modbus: AC limit, power factor, grid connection.
- With a Web API login: the inverter's own battery management (SoC window, backup reserve, self-consumption optimisation, charge sources), the export soft limit (`technician` role), the Solar API switch, and the setup of the inverter's Modbus.
- Without a login, from the inverter's web interface: module temperatures, fans, per-phase power, battery health, the inverter's event log as entities and events, why the inverter throttles, and the state of the energy management outputs.
- Discovery over mDNS, diagnostics download with the raw register map.

See [Entities](https://github.com/callifo/fronius_modbus/wiki/Entities) and [Battery Control](https://github.com/callifo/fronius_modbus/wiki/Battery-Control) in the wiki.

## Requirements

- Home Assistant 2026.9.0 or newer. The integration depends on the built-in `modbus` integration for its connection.
- Modbus TCP enabled on the inverter. The setup can enable it for you when a Web API password is provided.
- Optional: the password of a local Web API role. Without it the integration runs on Modbus alone; see [Web API](https://github.com/callifo/fronius_modbus/wiki/Web-API) in the wiki.

## Supported devices

The integration works with Fronius GEN24 inverters (Primo GEN24 and Symo GEN24, also Plus) and Verto Plus over Modbus TCP. The integration finds each device's registers through SunSpec, so it does not rely on a list of models.

Users of this integration and of redpomodoro/fronius_modbus ran it, mostly in versions before 1.x, on for example Primo GEN24 from 3.0 to 10.0, Symo GEN24 from 6.0 to 12.0 and Verto Plus from 17.5 to 30.0, with BYD Battery-Box Premium HVS, HVM and HV or Fronius Reserva batteries, and the Fronius Smart Meter TS 65A-3, TS 100A-1 and 63A. Several inverters run as separate entries.

Tauro models (Tauro ECO included) should work the same way: Fronius publishes the same SunSpec register maps for them, without the storage model. There are no reports yet.

Older Fronius inverters with a Datamanager (the SnapINverter family: Symo, Primo, Eco, Galvo without GEN24) are untested. Their Web API has other logins and paths, so they can only be set up *Without the web API*, and only with the Datamanager's Modbus set to the `int+SF` SunSpec model type; with `float` the inverter model is not found and the setup stops, reporting that Modbus did not answer.

## Coming from 0.3.x

Entries, entity IDs, history, statistics and options are kept; existing entries are migrated on the first start. Before you update:

1. **Take a Home Assistant backup.** Going back to 0.3.x afterwards needs it.
2. **Home Assistant 2026.9.0 or newer** is required. HACS refuses the installation on an older Home Assistant; a 0.3.x release stays available there.
3. **Check your automations and templates:**
   - Enum states are keys now: `Auto` became `auto`, `Charge from Grid` became `charge_from_grid`, `Enabled`/`Disabled` became `enabled`/`disabled` ([rule](https://github.com/callifo/fronius_modbus/wiki/Migration#enum-states-are-keys-now)).
   - `Grid charge power`, `Grid discharge power`, `PV charge limit` and `Discharge limit` can give another power for the same value than in 0.3.x. Check the values you set ([Battery](https://github.com/callifo/fronius_modbus/wiki/Migration#battery)).
   - `SoC Maximum` and `SoC Minimum (Web API)` can only be changed while the new `Web API SoC mode` is *Manual*. In 0.3.x they followed `Battery API mode`, now called `Self-consumption optimisation` ([Battery](https://github.com/callifo/fronius_modbus/wiki/Migration#battery)).
   - `Load` can be negative when other generation behind the grid meter, such as a balcony inverter, feeds in ([Connection and data](https://github.com/callifo/fronius_modbus/wiki/Migration#connection-and-data)).
   - Values from the web interface update every 60 seconds by default, not with every Modbus poll ([Connection and data](https://github.com/callifo/fronius_modbus/wiki/Migration#connection-and-data)).
4. **After the update:** if Home Assistant asks for the web login, log in again. Until then the values from the web interface, the events and a second smart meter are missing ([Entries without a valid login](https://github.com/callifo/fronius_modbus/wiki/Migration#entries-without-a-valid-login)). The checkbox "Restrict Modbus to this IP" is now the option *Modbus IP restriction*: a migrated entry keeps its choice, but an unchecked box no longer lifts the inverter's restriction ([Setup and security](https://github.com/callifo/fronius_modbus/wiki/Migration#setup-and-security)).

Some entities show a new name; their entity IDs stay ([New names](https://github.com/callifo/fronius_modbus/wiki/Migration#new-names)). Every change in detail is in [Migration](https://github.com/callifo/fronius_modbus/wiki/Migration#coming-from-03x), the breaking changes in the [CHANGELOG](CHANGELOG.md#100b1).

Coming straight from 0.2.7 or older, the MPPT and storage module sensors (`mppt1_power`, `mppt1_lfte` and the like) come back under new entity IDs, as they did with 0.2.8. Entries from redpomodoro 0.1.9 are migrated as well ([Migration](https://github.com/callifo/fronius_modbus/wiki/Migration#coming-from-redpomodorofronius_modbus)).

## Installation

### HACS

1. Go to HACS.
2. Click on the three dots in the top right corner.
3. Select "Custom repositories".
4. Add the [URL](https://github.com/callifo/fronius_modbus) of this repository.
5. Select the "Integration" type.
6. Click "ADD".
7. Open the repository in HACS and download it.
8. Restart Home Assistant.
9. Add **Fronius Modbus** under Settings -> Devices & services, or accept the card under **Discovered**.

### Manual

Copy the contents of the `custom_components` folder to your Home Assistant `config/custom_components` folder. After a restart of Home Assistant, the integration can be set up through the integration setup UI.

## Setup

The setup asks for the inverter's host, the Web API access role (`customer`, `technician` or *Without the web API*), the update intervals and the Modbus IP restriction. With a role it asks for that role's password; this is the local login of the inverter's web interface, not the Solar.web login. The password is not stored; the integration keeps a derived token that only Home Assistant can read.

With a role, the setup sets the inverter's Modbus itself. Without the web API, set it first in the inverter's web UI: Modbus TCP on, port 502, the SunSpec model type `int + SF`, inverter address 1 and meter address offset 200, and *Allow Control* on for the Modbus controls.

Every parameter, discovery, the Modbus IP restriction and reconfiguring are in [Installation and Setup](https://github.com/callifo/fronius_modbus/wiki/Installation-and-Setup) in the wiki.

## Troubleshooting

- **Diagnostics:** Settings -> Devices & services -> Fronius Modbus -> device -> Download diagnostics. Serial numbers and the Modbus restriction IP are redacted.
- **Everything the inverter reads out:** on Windows, run [`tools/inverter_dump.bat`](tools/inverter_dump.bat) (it starts `inverter_dump.ps1` next to it; both files are needed). It only reads, and by default replaces serial numbers, addresses and names with `<redacted>`; still look through the file before you share it. Attached to an issue, such a file helps to support a device that is not verified yet. To look for a meter on every Modbus unit, not only the usual ones, add the address and `-AllUnits`: `inverter_dump.bat <address> -AllUnits`. On a Datamanager (SnapINverter) it offers the `admin` or `service` login instead of `customer` or `technician`.
- Error messages, repair notices, logs and known limitations: [Troubleshooting and Diagnostics](https://github.com/callifo/fronius_modbus/wiki/Troubleshooting-and-Diagnostics) in the wiki.

## Removal

1. Undo the controls you set: `Storage Control Mode` to `auto`, `AC limit enable` and `Power factor control` to disabled, `Inverter connection` to enabled, and disable the automations that drive them. The inverter keeps what was written over Modbus, with no timeout, also after the entry is gone.
2. Settings -> Devices & services -> Fronius Modbus -> the entry's menu -> Delete.
3. The stored Web API token for the entry's host and role is deleted with it, unless another entry still uses them.
4. The Modbus settings the integration wrote on the inverter (Modbus TCP, SunSpec mode, *Allow Control*, *Restrict Control*, port and addresses) stay as they are; change them in the inverter's web UI if you no longer need them.
5. For a HACS installation, remove the integration in HACS as well.

## References

- https://www.fronius.com/~/downloads/Solar%20Energy/Operating%20Instructions/42,0410,2649.pdf

## Development

Development happens against a WSL2 Home Assistant test environment; the integration itself only needs a recent Home Assistant.

- `.github/scripts/check.sh` runs the gates of the Test workflow: ruff (lint and format), mypy, pip-audit, gitleaks (8.30 or newer), the check of the Modbus library pins against Home Assistant's core, pytest with a coverage gate, a mutation run, and the dump tool's offline tests, which need PowerShell 7 (`pwsh`); see [tests/README.md](tests/README.md). Run it (or at least `pytest tests/ -q`) before calling a change done. The HACS and hassfest validations run in CI only.
- The default `pytest tests/ -q` run skips the end-to-end tests. Pass `-m ""` to include them.
- `tests/fixtures/symo_gen24_fw1386.json` is a captured SunSpec register map from a Fronius Symo GEN24 10.0 running firmware 1.38.6-1. To capture a fixture from another inverter/firmware combination: download diagnostics for the integration's device (Settings -> Devices -> device -> Download diagnostics), take the `registers` object (`{unit_id: {space: {address: word}}}`), prepend the SunSpec marker registers (40000/40001, `"SunS"`) and append the end-of-chain header (a model id of `0xFFFF`) if the diagnostics dump doesn't already include them, and blank out the serial number words before committing the fixture.
