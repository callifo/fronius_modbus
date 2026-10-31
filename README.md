[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

# fronius_modbus

Home Assistant custom component for reading data from Fronius GEN24 and Verto inverters, connected smart meters, and battery storage. Modbus TCP (SunSpec) is the primary source; the authenticated Fronius Web API is optional and adds setup assistance and battery controls that are not available over Modbus. The inverter's web interface also reports module temperatures, fans, battery health and the inverter's event log, without a login.

This is a fork of [callifo/fronius_modbus](https://github.com/callifo/fronius_modbus) (itself derived from redpomodoro/fronius_modbus), rewritten in 1.0 onto Home Assistant's shared Modbus connection and the `modbus-connection` library. Upstream issues fixed here are listed in the CHANGELOG.

**The full documentation is in the [wiki](https://github.com/Varitras/fronius_modbus/wiki):** every entity, the battery and inverter controls, the inverter's events, how data updates, examples, troubleshooting and migration.

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
- Without a login, from the inverter's web interface: module temperatures, fans, per-phase power, battery health, the inverter's event log as entities and events, and why the inverter throttles.
- Discovery over mDNS, diagnostics download with the raw register map.

See [Entities](https://github.com/Varitras/fronius_modbus/wiki/Entities) and [Battery Control](https://github.com/Varitras/fronius_modbus/wiki/Battery-Control) in the wiki.

## Requirements

- Home Assistant 2026.9.0 or newer. The integration depends on the built-in `modbus` integration for its connection.
- Modbus TCP enabled on the inverter. The setup can enable it for you when a Web API password is provided.
- Optional: the password of a local Web API role. Without it the integration runs on Modbus alone; see [Web API](https://github.com/Varitras/fronius_modbus/wiki/Web-API) in the wiki.

## Supported devices

Verified against a Symo GEN24 10.0 with a BYD Battery-Box Premium HV and a Fronius Smart Meter TS 65A-3. Other GEN24, Verto and Tauro models (Tauro ECO included), batteries and meters should work through SunSpec discovery, but are untested here; Fronius publishes the same SunSpec register maps for them, the Tauro without the storage model.

Older Fronius inverters with a Datamanager (the SnapINverter family: Symo, Primo, Eco, Galvo without GEN24) are untested. Their Web API has other logins and paths, so they can only be set up *Without the web API*, and only with the Datamanager's Modbus set to the `int+SF` SunSpec model type; with `float` the inverter model is not found and the setup stops, reporting that Modbus did not answer.

## Coming from upstream

Entity IDs, unique IDs, history/statistics and existing options of [callifo/fronius_modbus](https://github.com/callifo/fronius_modbus) are kept; existing entries are migrated on the first start. Coming straight from 0.2.7 or older, the MPPT and storage module sensors (`mppt1_power`, `mppt1_lfte` and the like) come back under new entity ids, as they did with callifo 0.2.8. Breaking changes are listed in the CHANGELOG; what changes in detail is in [Migration](https://github.com/Varitras/fronius_modbus/wiki/Migration) in the wiki.

> [!IMPORTANT]
> Enum states are translation keys now: `Auto` became `auto`, `Charge from Grid` became `charge_from_grid`, `On grid operating` became `on_grid_operating`, `Enabled`/`Disabled` became `enabled`/`disabled`. Update automations and templates that compare against the old texts; see the CHANGELOG for the rule.

- **Minimum Home Assistant version: 2026.9.0**.
- The checkbox "Restrict Modbus to this IP" became the option **Modbus IP restriction**; checked becomes "restrict and allow this Home Assistant", unchecked becomes "keep". Allowing Home Assistant adds its address to the hosts already allowed while the inverter's restriction is on; while it is off, Home Assistant's address becomes the only allowed host.
- The SoC window follows the inverter's own switch: `SoC Maximum` and the new `SoC Minimum (Web API)` are writable while the new `Web API SoC mode` is manual, instead of following `Self-consumption optimisation`.

## Installation

### HACS

1. Go to HACS.
2. Click on the three dots in the top right corner.
3. Select "Custom repositories".
4. Add the [URL](https://github.com/Varitras/fronius_modbus) of this repository.
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

Every parameter, discovery, the Modbus IP restriction and reconfiguring are in [Installation and Setup](https://github.com/Varitras/fronius_modbus/wiki/Installation-and-Setup) in the wiki.

## Troubleshooting

- **Diagnostics:** Settings -> Devices & services -> Fronius Modbus -> device -> Download diagnostics. Serial numbers and the Modbus restriction IP are redacted.
- **Everything the inverter reads out:** on Windows, run [`tools/inverter_dump.bat`](tools/inverter_dump.bat) (it starts `inverter_dump.ps1` next to it; both files are needed). It only reads, and by default replaces serial numbers, addresses and names with `<redacted>`; still look through the file before you share it. Attached to an issue, such a file helps to support a device that is not verified yet.
- Error messages, repair notices, logs and known limitations: [Troubleshooting and Diagnostics](https://github.com/Varitras/fronius_modbus/wiki/Troubleshooting-and-Diagnostics) in the wiki.

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
