[![hacs_badge](https://img.shields.io/badge/HACS-Default-orange.svg)](https://github.com/custom-components/hacs)

# fronius_modbus
# This is a fork from redpomodoro/fronius_modbus, with some merged changes and PRs.

Home assistant Custom Component for reading data from Fronius Gen24 Inverter and connected smart meters and battery storage. This integration uses a local modbus connection. 

> [!CAUTION]
> This is a work in progress project - it is still in early development stage, so there are still breaking changes possible.
>
> This is an unofficial implementation and not supported by Fronius. It might stop working at any point in time.
> You are using this module (and it's prerequisites/dependencies) at your own risk. Not me neither any of contributors to this or any prerequired/dependency project are responsible for damage in any kind caused by this project or any of its prerequsites/dependencies.

# Installation
## HACS installation
* Go to HACS
* Click on the 3 dots in the top right corner.
* Select "Custom repositories"
* Add the [URL](https://github.com/callifo/fronius_modbus) to the repository.
* Select the 'integration' type.
* Click the "ADD" button.

## Manual installation
Copy contents of custom_components folder to your home-assistant config/custom_components folder.
After reboot of Home-Assistant, this integration can be configured through the integration setup UI.

## Inverter Setup
Make sure modbus is enabled on the inverter. You can check by going into the web interface of the inverter and go to:
"Communication" -> "Modbus"

And turn on:
- "Con­trol sec­ond­ary in­ver­t­er via Mod­bus TCP"
- "Allow control"
- Make sure that under 'SunSpec Model Type' has 'int + SF' selected. 

![modbus settings](images/modbus_settings.png?raw=true "modbus")

Where the inverter has an 'Insulation Warning' page, Insulation Measurement Mode must be set to 'Exact' (or Accurate) depending on the translation. If this is not set correctly, the integration will generate a lot of error messages and not function.

"ValueError: Exceeds the limit (4300 digits) for integer string conversion; use sys.set_int_max_str_digits() to increase the limit"

![modbus settings](images/resistance.png?raw=true "resistance")

## Charging From Grid
For Charging from Grid to work you must have it enabled in the Inverter. 
Energy Management -> Battery Management -> SoC Settings
Battery Charging from Other Sources: Enabled
From other generators in the home network and from Public Grid: Checked

> [!IMPORTANT]
> Turn off scheduled (dis)charging in the web UI to avoid unexpected behavior.

> [!IMPORTANT]
> When using multiple integrations that use pymodbus package it can lead to version conflicts as they will share 1 package in HA. This can be fixed by removing ALL integrations using pymodbus and modbus configuratio.yaml (for the build in integration into HA), rebooting HA and then reinstalling the integrations and the modbus configuration yaml.

> [!IMPORTANT]
> Update your GEN24 inverter firmware to 1.34.6-1 or higher otherwise battery charging might be limited. Its recommended to keep the inverter up to date, this integration will only be tested on recent firmwares.

# Usage

### Battery Storage
### Controls
| Entity  | Description |
| --- | --- |
| Discharge Limit | This is maxium discharging power in watts of which the battery can be discharged by.  |
| Grid Charge Power | The charging power in watts when the storage is being charged from the grid. Note that grid charging is seems to be limited to an effictive 50% by the hardware. |
| Grid Discharge Power | The discharging power in watts when the storage is being discharged to the grid. |
| Minimum Reserve | The minimum reserve for storage when discharging. Note that the storage will charge from the grid with 0.5kW if SOC falls below this level. Called 'Reserve Capacity' in Fronius Web UI. |
| PV Charge Limit  | This is maximum PV charging power in watts of which the battery can be charged by.  |

### Storage Control Modes
| Mode  | Description |
| --- | --- |
| Auto  | The storage will allow charging and discharging up to the minimum reserve. |
| PV Charge Limit | The storage can be charged with PV power at a limited rate. Limit will be set to maximum power after change.  |
| Discharge Limit | The storage can be charged with PV power and discharged at a limited rate.  in Fronius Web UI. Limit will be set to maximum power after change. |
| PV Charge and Discharge Limit | Allows setting both PV charge and discharge limits. Limits will be set to maximum power after change. |
| Charge from Grid | The storage will be charged from the grid using the charge rate from 'Grid Charge Power'. Power will be set 0 after change. Set the Grid Charge Power to a number in Watts, in a multiple of '10'. If the number is not rounded to 10, it will not work and does odd things like charging at 500W. If you need to press 'increment' to get it to charge, its likely the 10 issue. You do not need to fiddle with the 'Minimum Reserve' setting. |
| Discharge to Grid | The storage will discharge to the gird using the discharge rate from 'Gird Discharge Power'. Power will be set 0 after change. |
| Block discharging | The storage can only be charged with PV power. Charge limit will be set to maximum power. |
| Block charging | The can only be discharged and won't be charged with PV power. Discharge limit will be set to maximum power. |

Note to change the mode first then set controls active in that mode.

### Controls used by Modes
| Mode | Charge Limit | Discharge Limit | Grid Charge Power |  Grid Discharge Power | Minimum Reserve | 
| --- | --- | --- | --- | --- | --- |
| Auto | Ignored (100%) | Ignored (100%) | Ignored (0%) | Ignored (0%) | Used | 
| PV Charge Limit | Used | Ignored (100%) | Ignored (0%) | Ignored (0%) | Used |
| Discharge Limit  | Ignored (100%) | Used | Ignored (0%) | Ignored (0%) | Used |
| PV Charge and Discharge Limit  | Used | Used | Ignored (0%) | Ignored (0%) | Used |
| Charge from Grid | Ignored | Ignored | Used | Ignored (0%) | Used |
| Charge from Grid | Ignored | Ignored | Ignored (0%) | Used | Used |
| Block discharging | Used | Ignored (0%) | Ignored (0%) | Ignored (0%) | Used |
| Block charging | Ignored (0%) | Used | Ignored (0%) | Ignored (0%) | Used |

### Fronius Web UI mapping
| Web UI name | Integration Control | Integration Mode |
| --- | --- | --- |
| Max. charging power | PV Charge Limit | PV Charge Limit |
| Min. charging power | Grid Charging Power | Charge from Grid |
| Max. discharging power | Discharge Limit | Discharge Limit |
| Min. discharging power | Grid Discharge Power | Grid Discharge Power | 

### Battery Storage Sensors
| Entity  | Description |
| --- | --- |
| Charge Status | Holding / Charging / Discharging |
| Minimum Reserve | This is minium level to which the battery can be discharged and will be charged from the grid if falls below. Called 'Reserve Capacity' in Web UI. |
| State of Charge | The current battery level |

### Diagnostic
| Entity  | Description |
| --- | --- |
To come!


### Inverter Sensors
| Entity  | Description |
| --- | --- |
| Load | The current total power consumption which is derived by adding up the meter AC power and interver AC power. |


### Inverter Diagnostics
| Entity  | Description |
| --- | --- |
| Grid status | Grid status based on meter and interter frequency. If inverter frequency is 53hz it is running in off grid mode and normally in 50hz. When the inverter is sleeping the meter frequency is checked for connection. |

### Inverter Controls
| Entity  | Description |
| --- | --- |
| Export Limit Enable | Allows limiting of inverter export to grid. Enable this setting first, and after set the export limit in setting below. |
| Export Limit Rate | This setting in Watts allows setting how much solar can be exported to the grid. Setting seems to take this as a maximum, and it wont be exactly this, likely a bit less.  |

# Example Devices

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
