# Changelog

## Unreleased

### Changed
- A device that does not answer is logged as an expected outage instead of an error, the way the quality scale asks for. A device that answers and refuses is still an error.
- Log lines no longer name the host: diagnostics carry it redacted, and Home Assistant logs travel with bug reports.

### Fixed
- A meter that answers busy or reports a device failure is treated as undecided and probed again, instead of counting as absent and losing its entities.
- Discovery publishes its result in one step: a probe that fails halfway no longer leaves the poll with a meter name and no meter behind it.
- A model missing from one incomplete scan comes back as the same component, so the controls and the storage control keep reading what the poll refreshes.
- The AC-limit and power-factor write is guarded from the first register on: a cancellation right after the inverter disabled the control no longer leaves it off.
- A repeated discovery keeps the components whose model has not moved, so the controls and the storage control keep reading what the poll refreshes.
- Discovery only counts as complete once its components are installed; an identity read that fails during a retry no longer ends the retries.
- A meter topology that is confirmed after a failed one is applied even when it names the meter the entry already polls, so the household load and the meter location arrive without waiting for an unrelated reload.
- During the tolerated outage after a web write the previous poll is served for display but no longer counts as a new sample for the cumulative energy sensors.
- A cancelled service call during an AC-limit or power-factor write no longer leaves the control switched off: the enable flag is restored even while the call is being cancelled.
- Cumulative energy sensors no longer accept a bad reading that a failed poll repeated: only a poll that actually refreshed the register counts as confirmation.
- Discovery that ended in a refused register read is retried on every poll and reported as a failure, instead of passing as a device without those models. While discovery or the meter topology is uncertain, no entity or device is retired.
- A meter device is no longer retired and rebuilt on every reload: the legacy pattern that removes pre-web-API devices also matched the identifiers this version builds.
- The shared SoC minimum is written to both protocols under one lock, so a concurrent maximum change can no longer slip between the check and the Modbus write.
- Changing only the spelling of the host no longer deletes the stored login token.
- A timed-out lookup of the inverter's digest hash version is no longer remembered, so password login recovers instead of failing until a restart.
- A device that refuses a read past the end of its register map instead of answering the SunSpec end marker no longer fails the whole setup with "cannot connect": discovery keeps the models the device did serve and logs where the chain stopped.

### Changed
- The Web API now uses one role per entry: pick `customer` or `technician` during setup and enter only that password. An entry that had a technician token keeps technician access; every other entry stays on `customer`. Switching roles is a reconfigure.
- Storage charge/discharge limits are converted to percent of WChaMax (model 124), the reference Fronius documents for InWRte/OutWRte; the nameplate ratings are only used when a device reports no WChaMax. Identical on devices where both agree.

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
