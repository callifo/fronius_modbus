# Changelog

## Unreleased

### Changed
- The `Throttle reason` state `export_limit` reads "AC limit" ("AC-Leistungsbegrenzung"): it is the inverter's AC limit (`AC limit enable`, `AC limit rate`), not the export limit set in the web interface, which the inverter does not report in these signals. The state key stays, so automations keep working.
- In German, `Production limit` reads "Aktuelle Leistungsgrenze", so it and "Leistungsgrenze erreicht" name the same limit. Entity ids do not change.

## 1.2.0b4

A small update on 1.2.0b3: it removes a deprecation warning Home Assistant logged for the discovery, adds the smart meter's per-phase energy for meters that count it, and counts the Tauro among the known models. No migration; existing entities, unique ids and entity ids do not change.

### Added
- The Tauro and Tauro ECO count as known models: Fronius publishes the GEN24's SunSpec register maps for them. Setting one up no longer logs an untested model.
- The README describes use cases, gives automation examples, and says what an entry without the web API and an older Datamanager inverter need.
- Per-phase import and export energy of the smart meter, for a meter that counts them per phase (some report 0, SunSpec's "not implemented"). The six sensors come disabled.

### Fixed
- Discovery no longer uses `device_registry.async_get_device`, which Home Assistant 2026.9 deprecated and logged a warning for; it would stop working in 2027.8. A test now fails on any deprecated call Home Assistant reports for the integration.

## 1.2.0b3

Three additions on 1.2.0b2: the web API becomes optional, the inverter is discovered over
mDNS and follows a new address, and a missing login is asked for through Home Assistant's
own reauthentication. A pre-release because entries migrate to minor version 14 and the
login handling changes; existing entities, unique ids and entity ids do not change.

Running an entry without the web API needs Modbus TCP switched on in the inverter first.
If discovery or the new login behaves unexpectedly on your setup, please open an issue.

### Added
- Discovery over mDNS: Home Assistant offers the inverter under Discovered and fills in its host. An inverter already set up is recognized by its serial number, and an entry set up with an IPv4 address follows the inverter to a new address, its Web API token included.
- The web API is optional: choose *Without the web API* as the access role to set an entry up on Modbus alone, without a password or a stored token. The Modbus sensors and controls, the component sensors and the list of smart meters stay, since those endpoints answer without a login; the settings that live only in the inverter's web configuration are not there. Switching an existing entry to it deletes its token and removes the web entities. The new login Home Assistant asks for offers the same choice.

### Changed
- A missing or rejected Web API login is asked for through Home Assistant's own reauthentication on the integration, not a Repairs item. It asks for the role and its password, or offers to go on without the web API; an open Repairs item from an earlier version goes at the next start, or, on an entry that is disabled, when it is confirmed.
- `Production limit`, `Production limit reached`, `Battery max charge power (DC-DC)` and `Battery max discharge power (DC-DC)` are created only once the inverter reports them, like the power modules. Older firmware lacks these fields and showed four sensors that stayed unknown. Existing entries keep the ones they have; entries migrate to minor version 14.
- The component sensors are read without a login. They keep reading after the web login is lost while Home Assistant runs; the settings it guards go unavailable as before. A login the inverter refused is not sent again.

### Fixed
- The export soft limit shows only for a switch the inverter reports as on. A value like "off" read as an active limit.
- An export limit block or a battery value of an unexpected shape, such as an infinite number, leaves that value unknown instead of failing the whole web poll. An export limit that is no number shows as unknown; the power sensor could not take it.
- A new login whose entry is removed while it runs keeps no token; the repair it replaces reported success and left the new token behind.

## 1.2.0b2

An audit round on 1.2.0b1, a write policy, and new sensors. The integration writes only what
differs from what the inverter holds and keeps the inverter settings it does not own, and it
shows what the inverter's component endpoints add beyond Modbus. A pre-release because the
Modbus restriction checkbox becomes a choice and entries migrate to a new minor version;
existing entities, unique ids and entity ids do not change.

Battery settings over the web API are now sent one field at a time, checked on a GEN24. If
changing a SoC limit, the target feed-in or a charge source is refused on your inverter,
please open an issue.

### Added
- Sensors from the inverter's component endpoints, which the web API poll already reads: power module temperatures, fans, per-phase AC power, the production limit and whether it is reached, the battery converter's charge and discharge limits, grid validity, and the power stage firmware on the inverter; the battery's state of health. Feed-in point voltages and frequency, the DC link voltage, the operating time, the time in backup mode, cell and BMS temperatures, the battery's current limits and its nameplate values come disabled. The battery's firmware and hardware version appear on its device page. No sensor takes their serial numbers or device ids; the battery's serial number stays on its device page, as before.

### Security
- Setup and reconfigure no longer lift a Modbus IP restriction set on the inverter. With the restriction checkbox unchecked, which is the default, the integration wrote the restriction off and opened a restricted Modbus server to every host on the network. The checkbox is now a choice between keeping the inverter's setting (the default), restricting Modbus to Home Assistant, and lifting the restriction. Existing entries migrate from checked to "restrict to Home Assistant" and from unchecked to "keep", never to "lift".
- The stored web token is written readable by Home Assistant only. The Digest token alone authenticates to the inverter, and Home Assistant's default storage wrote it world-readable; an existing token file is rewritten on the next start.
- A stored web token is deleted once no entry uses its host and role: on removing the entry, on moving an entry to another host or role, and when the migration to a single role keeps the other one. It had outlived all three. A token minted during setup is kept only once the inverter answered and the entry was created; a failed or aborted setup left it behind.
- A setup, reconfigure, options change or repair for a host another entry already owns is refused before the inverter is contacted. It wrote the Modbus settings, including the IP restriction, and only then reported the host as taken.
- An aborted setup leaves the stored token of the entry that already serves the host alone, and a settings change that fails late keeps the token its entry now logs in with. A stale token came back in both cases.

### Fixed
- Enabling Modbus TCP keeps the rest of the inverter's Modbus settings. The integration wrote a fixed block that moved both RS485 ports to master and switched the `TCP & RTU` mode to TCP alone, cutting off a device that reads the inverter over RS485; it now writes back what it read and changes only its own fields. Restricting Modbus to Home Assistant adds its address to the hosts already allowed instead of replacing them.
- Switching the Solar API on keeps the inverter's own switch that enables it for discovered devices; it was cleared on every change. Switching the Solar API off still clears it, or a discovered device would switch the API back on.
- A web login lost at setup no longer deletes the web entities, and with them the entity ids an owner chose. A rejected token read as an entry without the web API, and the stale-entity cleanup retired everything the web API provides; after a restart without the token it took the second meter's entities too. A missing login now blocks the cleanup like a failed poll.
- A rejected technician token is the one deleted when the meter topology read fails; the customer token was deleted in its place, and the rejected one was offered again on every start.
- An HTTP error from the export-limit endpoint fails the web refresh instead of reading as "no export limit". Only a 404, from firmware without the endpoint, still means the limit is not there.
- The inverter and battery component reads (inverter temperature, cell temperature, battery manufacturer, model and serial) no longer hide a failing endpoint. Their values still turn unknown instead of taking the controls down, but an error other than a 404 is logged once as a warning, and again at info when the endpoint answers. A switched-off inverter is left to the refresh, which already reports it.
- Selecting Charge from Grid reports an error when the web interface refuses the grid charging flags. The storage mode was set and the control showed success, while the battery could not charge from the grid; the message now says so.
- An energy total accepts a new counter range only from samples that agree with each other. A spike between two low readings (10, 100000, 10) counted as three confirmations and put a false reset into the long-term statistics.
- A meter whose register map moved is read at its new address even when the first rediscovery was aborted. The new address was remembered before the rediscovery finished, so the retry kept the component bound to the old registers.
- Diagnostics download while the inverter is offline, with the last poll and its report; the raw registers show which error kept them from being read. The download failed as a whole.
- Reconfigure and Repairs reload the entry once; they reloaded it twice.
- A web control used while the inverter's web interface does not answer shows a translated error. It surfaced as an unknown error with a traceback in the log since 1.1.1.
- An AC limit or power factor override that was on stays on when its new value cannot be written; a scale factor the value could not be converted with left the control switched off.
- The Modbus storage reserve in manual SoC mode checks the SoC window the inverter holds before it writes Modbus, and is refused when that window cannot be read. When the web interface refuses the reserve after Modbus took it, the error says so, since the two minimums then differ.
- One HTTP 404 from a component endpoint that answered before keeps its registered sensors, as unknown; the next start deleted them.
- A failed battery component read keeps the battery's manufacturer, model and serial instead of replacing them with a generic name.
- A Modbus configuration answer of the wrong shape no longer fails the whole web refresh, and its RS485 part is checked before it is written back.

### Changed
- A control set to the value the inverter already holds writes nothing. Every write used to reach the inverter: a Modbus register, and for the AC limit and the power factor a second-long switch-off of the enable flag around it; a battery setting over the web API, and with it the Modbus recovery window and a delayed refresh. The comparison is made against a fresh read, not the last poll, so a value another controller changed in between is still written; when that read fails, the value is written as before, except a SoC limit, which is refused without the window it has to fit.
- A web write sends only the field asked for, and only when it differs; a field the inverter names as refused in its answer is reported as an error. Changing one SoC limit, one charge source, the target feed-in or the self-consumption mode no longer re-sends the other values from the last poll, which undid a change made in the inverter's web interface or by another controller in between. The SoC window is checked against a fresh read.
- The inverter temperature and the battery cell temperature are read like the new component sensors. Firmware without the component endpoints (HTTP 404) gets no such entity, instead of one that stays unknown.

## 1.2.0b1

A quality-scale round: translated error messages, icons in `icons.json`, declared parallel
updates, and outage logging per sub-system. A pre-release because the error texts a user sees
change; entities, unique ids and entity ids do not.

### Fixed
- The Solar API repair explains the firmware risk again. Since 1.0.0b1 its steps showed the text of the reconfigure form, and the repair raised after a rejected login told the owner nothing about why it was there.

### Changed
- Entity icons moved from the code into `icons.json`, the way Home Assistant resolves them now. The icons themselves are unchanged.
- Errors from a refused or failed write reach the user in their language. The library raises keyed errors, and the entities hand the key to Home Assistant; messages no longer name the control, which the user just touched anyway.
- Every platform declares how many actions Home Assistant may run at once: one for the controls, none for the sensors.
- A meter or other sub-system that stops answering is logged once at info, and once more when it answers again, the way the quality scale asks for an unavailable device. A sub-system that answers with a refusal is still a warning.

## 1.1.2

First field feedback on 1.1.x and a second controller on the same inverter. Three fixes, and
write bursts are coalesced instead of hidden behind sliders.

### Fixed
- A web write shows its value at once. The select for self-consumption optimisation snapped back to the old option and only caught up with the delayed refresh ten seconds later; every web write now hands the entities its result immediately.
- The storage `Capacity` sensor keeps its long-term statistics. 1.0.0b1 dropped its state class because Home Assistant does not allow `measurement` on the energy device class, which handed every upgrading installation a statistics repair; the rating is stored energy, and that device class allows it.
- A storage mode another controller wrote on a different base mode, the way evcc holds with `StorCtl_Mod` 2 where this integration writes 3, is adopted once instead of being logged as an outside change on every poll.

### Changed
- The web percent limits are input boxes again. The sliders of 1.1.0 were a stop-gap against write bursts, which the rule below now handles.
- A burst of values for one web control reaches the inverter as its first and its last value. Calls queued behind a running write return once a newer value has taken their place, so an automation stepping a limit ten times no longer stalls the inverter's Modbus side ten times. The last caller still receives the last write's error.

## 1.1.1

An audit round on 1.1.0: log hygiene on the web side, two sentinel and retry edge cases, and
reproducible CI pins. No entity changes.

### Fixed
- Web API transport errors no longer carry the device address into the log, on the poll and on the login. Every call into requests goes through one function that translates its errors, and a guard keeps it that way, so a bug report's log names the error type, not the host.
- An HTTP error answer from the inverter's web server is logged as an error again. It had been classed with the switched-off device, because requests derives its HTTP errors from `OSError`.
- The throttle reason stays unknown while the limit enable flag or the limit percent is unimplemented on the device, instead of reporting `export_limit` or `none` from a sentinel.
- A meter that comes back after being undecided keeps its first failed read to itself. It used to fail the whole Modbus poll, taking every Modbus entity with it for that refresh.
- The HACS and hassfest actions are pinned to a revision, like the other workflow actions.

## 1.1.0

The battery web controls follow the inverter's own two switches. Display names change, unique ids
and entity ids do not; automations keep working, only the names in the UI differ.

### Changed
- The web SoC window follows the inverter's own switch. `SoC Maximum` and the new `SoC Minimum (Web API)` are writable while `BAT_M0_SOC_MODE` is manual, whatever the energy management mode; switching `HYB_EM_MODE` no longer rewrites the SoC window or forces its mode. Before, both hung on the wrong switch, and a minimum set in the inverter's UI never reached Home Assistant.
- The web SoC limits and the backup reserve are sliders. Each step of the input box was a web write, and a burst of them stalls the inverter's Modbus side long enough for other integrations on the same connection to time out.
- Renamed for what they do: `Battery API mode` is now `Self-consumption optimisation`, and the Modbus `SoC Minimum` (model 124 `MinRsvPct`, only applied under Modbus storage control) is now `Modbus storage reserve`. Unique ids are unchanged.

### Added
- A `SoC Minimum (Web API)` number shows and sets `BAT_M0_SOC_MIN`, the minimum the inverter's own UI shows.
- A `Backup reserve` number sets the battery share kept for backup power (`HYB_BACKUP_RESERVED`), in either battery mode.
- A `Web API SoC mode` select shows and switches the inverter's `BAT_M0_SOC_MODE`, the switch that decides whether the SoC window is writable. The README had claimed it was shown already.

## 1.0.0

First stable release of the rewrite. It contains everything from the three 1.0.0 pre-releases
below; upgrading from 0.3.x follows the migration notes under 1.0.0b1.

### Added
- A `Throttle reason` sensor says whether the inverter is limiting its output and why: the operating state it reports itself, an active power setpoint, or an export limit that is switched on below full power. It stays unknown while one of those sources could not be read, rather than claiming there is no throttling.

## 1.0.0b3

Four independent audit rounds worked off, plus the single-role Web API login. Entries migrate on
their own; an entry that had a technician token keeps technician access.

### Changed
- The Web API uses one role per entry: pick `customer` or `technician` during setup and enter only that password. Switching roles is a reconfigure.
- Storage charge and discharge limits are converted to percent of `WChaMax` (model 124), the reference Fronius documents for `InWRte`/`OutWRte`. The nameplate ratings are only used when a device reports no `WChaMax`.
- A device that does not answer is logged as an expected outage instead of an error, the way the quality scale asks for. A device that answers and refuses is still an error.
- Log lines no longer name the host. Diagnostics carry it redacted, and Home Assistant logs travel with bug reports.

### Fixed
- A cancelled service call during an AC-limit or power-factor write no longer leaves the control switched off. The enable flag is restored from the first register on, even while the call is being cancelled.
- Discovery publishes its result in one step and retries what it could not finish. A device that refuses a read past the end of its register map no longer fails setup with "cannot connect", a probe that fails halfway no longer leaves the poll with a meter name and no meter behind it, and completion is only reported once the components are installed.
- Nothing is retired while the picture is uncertain. An unread meter topology, an incomplete chain or a failed poll all block the entity and device cleanup, and the legacy pass no longer removes the meter devices this version builds.
- A meter that answers busy, reports a device failure or does not answer at all is probed again instead of counting as absent and losing its entities. Only a unit that answers without a SunSpec map is treated as empty.
- Components whose model has not moved keep their readings across a rediscovery, so the controls and the storage control never read a component that nothing polls. A model that has moved reloads the entry instead of being published behind the controls' back.
- Cumulative energy sensors only count a poll that actually refreshed the register. Neither a failed read nor the retained poll during the recovery window after a web write can confirm a bad reading any more.
- A meter topology confirmed after a failed one is applied even when it names the meter the entry already polls, so the household load and the meter location arrive without waiting for an unrelated reload.
- The shared SoC minimum is written to both protocols under one lock, so a concurrent maximum change cannot slip between the check and the Modbus write.
- Changing only the spelling of the host no longer deletes the stored login token.
- A timed-out lookup of the inverter's digest hash version is no longer remembered, so password login recovers instead of failing until a restart.

## 1.0.0b2

Fixes from an independent audit of 1.0.0b1 plus two upstream issues; no entity, unique id or option changes.

### Fixed
- The idle inverter-control state read `Normal` while its option list said `normal`, so Home Assistant refused the sensor (regression in 1.0.0b1).
- A failed AC-limit or power-factor value write left a previously active limit switched off; the enable flag is now restored and a failure to restore it is reported.
- A rate change queued behind a storage mode switch was validated against the old mode and could undo a fresh charging block; mode checks now happen under the write lock.
- The storage mode select followed what was written, not what the inverter did: the extended mode is now re-derived from the registers on every poll, a silently refused mode write is given up after three polls, and forced discharge is recognised after a restart (upstream #127).
- A storage mode change writes the rate that rises first, so Charge from Grid → Block Charging no longer trips the firmware's exception 3 (upstream #126).
- A meter that did not answer during discovery was treated as absent for the life of the entry; it is now probed again on every poll, and a sub-system that answers for the first time later (meter, MPPT) reloads the entry so its entities appear.
- Cumulative energy sensors: a sustained gap above 100 kWh (Home Assistant offline) is now accepted after three consecutive polls instead of freezing the sensor forever, and reading the sensor no longer counts as a poll.
- A first poll without the nameplate model fixed the storage rate maxima on 11 000 W; they now follow every later nameplate read.
- Load and grid status are only derived when both the meter and the inverter answered in the same poll.
- The storage rate numbers now go unavailable when the storage registers could not be read.
- Concurrent web API writes (SoC minimum/maximum, charge sources) are serialised; one no longer overwrites the other.
- After a rejected web login the web entities go unavailable and a write reports an error instead of doing nothing.
- The assisted Modbus setup now also switches a float register map to the integer map the integration reads.
- Configure always offers the password step, so the technician password can be added or replaced later; an empty customer password keeps the stored token.
- Changing the host in Configure/Reconfigure moves the entry's unique id with it and refuses a host another entry already serves.

## 1.0.0b1

First beta of the rewrite; the 1.0.0 release follows once the beta has run on more installations.

### Breaking
- **Enum states are now translation keys.** Every select option and every enumerated sensor state changed from an English display text to a lowercase key (shown translated in the UI): `Auto` → `auto`, `PV Charge Limit` → `pv_charge_limit`, `Charge from Grid` → `charge_from_grid`, `Block Discharging` → `block_discharging`, `Enabled`/`Disabled` → `enabled`/`disabled`, `On grid operating` → `on_grid_operating`, `Off grid` → `off_grid`, `Normal` → `normal`, `Throttled` → `throttled`, `Charge and Discharge` → `charge_and_discharge`, `Power reduction,Constant power factor` → `power_reduction_constant_power_factor`, and so on — the rule is lowercase with every non-alphanumeric run replaced by `_`. Automations, templates and dashboards that compare against the old texts (`is_state(..., "Auto")`, `select_option: "Charge from Grid"`) must use the new keys. An unmapped device code now reads `unknown` instead of `Unknown (<code>)`. Reason: hassfest only validates translation keys of the form `[a-z0-9-_]+`, so the German state texts could not be shipped otherwise.

- Minimum supported Home Assistant version is now 2026.9.0.
- The integration now requires Home Assistant's built-in `modbus` integration and connects through it via `async_get_unit` instead of opening its own Modbus TCP connection.

### Changed
- Entity translation keys derived from the 0.3 data keys (`A`, `AphA`, `Conn`, `WHRtg`, ...) are renamed to lowercase names so hassfest validates the translations; unique ids and existing entity ids are unaffected.
- A storage mode change writes the rate that rises first, so a transition such as Charge from Grid to Block Charging no longer leaves both rates at zero for an instant (refused by the firmware with exception 3, upstream #126).

- The register map is discovered at runtime via SunSpec model walking through the `modbus-connection` library (models 1, 101/103, 120, 121, 122, 123, 124, 160, 201-204) instead of a fixed, hand-written register list. Storage entities are present if and only if model 124 is in the chain; meter phase count is derived from the meter model id.
- The Web API is polled on its own interval instead of once per Modbus poll, so a slow or unreachable Web API no longer stalls the Modbus poll or blocks the Modbus entities.
- A firmware update that shifts the inverter's SunSpec model chain now reloads the config entry automatically.
- Grid charge power now scales by the charge maximum and grid discharge power by the discharge maximum. 0.3 wrote each of them against the opposite register's maximum while displaying it with this one, so a written watt value and the value shown back differed whenever the two maxima did.
- The storage energy rating sensor no longer claims the measurement state class.
- The battery API mode now follows `HYB_EM_MODE` alone; a `manual` SoC mode no longer hides the mode as unknown.

### Added

- New option: "Web API update interval (seconds)" (default 60).
- Diagnostics download now includes the raw SunSpec register map, with serial numbers redacted.

### Removed

- The hand-written pymodbus client and its pymodbus version check.
- Per-value plausibility bounds; SunSpec sentinel values and scale factors are now decoded by `modbus-connection`.
