# Load calculation reliability and additional AC generation

## Problem and resulting behavior

The `load` sensor could alternate between readings, `unknown`, cached readings
and false `0 W` while the inverter operated normally. The installation also has
a separate balcony solar inverter behind the grid meter. This topology matters:
**a negative Fronius/grid balance can be entirely valid**. The initial version
of this PR rejected all negative grid balances; that policy was incorrect and
has been removed. This document describes the final signed behavior.

The fix reads the primary meter adjacent to inverter AC power, validates host
request intervals and SunSpec values, preserves the signed balance, and removes
zero clamping and old-value substitution. It cannot synchronize device firmware
acquisition or reconstruct unmeasured additional generation.

Entity IDs, configuration migrations and physical write methods are unchanged.
The existing integer + scale-factor profile is required, not the different
float-profile layout. Supported families remain Primo GEN24, Symo GEN24 and
Verto; direct hardware observations are limited to one Symo GEN24 installation.

## Repository baseline and previous fixes

The PR targets the synchronized `anzimber/fronius_modbus:main` at
`81a631bffc99c913af68b68a488ea94951551454` (0.3.3), identical to
`callifo/fronius_modbus:main` when rechecked. The original PR base was
`3b543de` (0.3.1); after the fork was synced, the updated main was merged
into the fix branch without conflicts and all tests were rerun. The PR diff
relative to the new main contains only this load fix, its tests and documentation.
Newer upstream battery-control and mode-resynchronization changes are retained.

The existing `fixes/combined_multiple_fixes` branch is an ancestor of the
original main. Previous load fixes `7f84d10`, `f58ab25` and `5c5a623` were
reviewed. The installed component includes upstream work and a separate
experimental diagnostic patch; the running installation is not changed here.

## Physical meaning: net demand versus household consumption

Use positive grid power for import and negative for export. At the common grid
connection, conservation of power gives:

`household consumption = Fronius AC + other AC generation + grid import`

The existing integration knows only Fronius AC and grid import, so it calculates:

`load = Fronius AC + grid import = household consumption - other AC generation`

That is **signed net demand excluding other generation**, not necessarily total
household consumption. Additional generation must be behind the same grid meter
and must not already be included in the Fronius AC value for these equations.

| Fronius AC | Balcony solar | Household consumption | Grid power | Existing load balance |
| --- | --- | --- | --- | --- |
| 5000 W | 600 W | 200 W | -5400 W | -400 W |
| 5000 W | 600 W | 900 W | -4700 W | 300 W |
| 5000 W | 600 W | 600 W | -5000 W | 0 W |
| -3000 W | 600 W | 200 W | 2600 W | -400 W |

The last example includes AC charging of the DC-coupled battery. The signed
Fronius AC value already includes that battery exchange. Adding battery DC
power again would double-count its contribution and mix different boundaries.
The previous >=1000-W charging exception is therefore removed without adding
a battery correction.

A negative balance means additional generation exceeds household consumption
when the readings are consistent. A positive balance also excludes that
additional generation. Even a zero balance can coexist with nonzero household
consumption. **Clamping to zero loses information and cannot recover the true
household load.** A boolean setting saying other generators exist cannot recover
it either; actual contemporaneous generator power is needed.

Preserve the signed entity for net demand. If total household consumption is
needed, use a separate correctly named sensor that adds measured, appropriately
timed balcony/other generator output to this balance. Do not add nominal panel
ratings, assume constant generation, or substitute zero for missing generator
data. No such new input or sensor is introduced by this focused fix. The existing
Load name/ID remains for compatibility; users with other generators should label
it as net demand in dashboards and review consumers that require gross load.
This includes any Predbat input configured to consume this entity: unchanged
write functions do not guarantee that downstream code accepts signed demand.

## Evidence and limits of the diagnosis

On 2026-10-22, logs from the installed experimental patch reported balances such
as 3227.5 - 4160 = -932.5 W at 0.009-s completion skew and
8442 - 9070 = -628 W at 0.012-s skew. These logs prove the patch was loaded.
They do **not** prove invalid device values: the additional generator can explain
negative balances even with perfectly synchronized reads. In particular, the
reported 5580 - 5751 = -171 W is a valid net balance. For illustration, if balcony
output were 600 W at that instant, household consumption would be 429 W; that
600-W value is an example, not a recorded observation.

Read-only direct Modbus checks confirmed signed register scaling and a grid
meter location. Five adjacent pairs produced 196.1–205.1 W of net demand, with
1.5–3.7 ms per response. The inverter Solar API gave AC 2938.90 W, grid -2732.30 W
and signed `P_Load=-203.58 W` at approximately the same time. These separate
calls are not atomic. Agreement supports the balance calculation, not a claim
that either API independently measures total household consumption with an
unmeasured balcony inverter.

Another 40 pairs over approximately 20 seconds yielded 102.0–225.3 W, maximum
response time 5.15 ms. Three later read-only polls using the implementation gave
252.4, 331.9 and 331.9 W, with complete polls approximately 122, 13 and 24 ms.
These are short net-demand observations, not long-term validation. No physical
write, production component replacement or HA restart was performed.

Confirmed software defects are unnecessary separation of the power reads,
completion-only timing checks, clamping signed balances, an unexpired heuristic
load cache, and scaling SunSpec not-implemented values into numbers. The cache
counter could be reset by intervening rejected samples without expiring its
stored value. Invalid scale factor -32768 can underflow to zero before validation.

Device-internal refresh timing, fast PV changes and meter forwarding delays can
still affect measurement consistency, regardless of the result's sign. This
investigation does not prove that those effects caused any particular logged
negative value. Host completion timestamps do not reveal device acquisition age.
Negative values alone are neither an error criterion nor a reason to warn.

## Registers and meter locations

Addresses are zero-based protocol addresses; numbered registers are one greater:

| Source | Models | Signed W address | Signed W_SF address |
| --- | --- | --- | --- |
| Inverter | 101/102/103 | 40083 | 40084 |
| Meter | 201/202/203 | 40087 | 40091 |

`froniusmodbusclient.py::_calculate_load_power` rejects W=-32768 and scale
factors outside SunSpec's -10..10 range, including its -32768 sentinel, before
scaling. Existing ±50-kW validation and two-decimal precision remain. The related
AC/meter power entity becomes unknown when its register value is invalid.
Other register families and battery write scaling are unchanged.

- Primary location 0 (grid): signed net demand is inverter AC plus grid import.
- Location 1 (load) and 256–511 (subload): signed demand is `-meter_power`.
  Generation within that measured load path may make demand negative. A subload
  represents its measured branch, not necessarily the complete house.
- Location 3 (external generator), missing and unsupported locations do not by
  themselves measure household demand and remain unknown.

No secondary meter silently replaces a missing primary, and arbitrary external
generator meters are not automatically summed. Check wiring and measured scope.
Fronius explicitly documents load paths with generation exceeding consumption.

References:

- [Fronius GEN24 Modbus manual: models, meter locations and signs](https://manuals.fronius.com/html/4204102649/en-US.html)
- [SunSpec inverter model 103](https://github.com/sunspec/models/blob/master/json/model_103.json)
- [SunSpec meter model 203](https://github.com/sunspec/models/blob/master/json/model_203.json)
- [Fronius official register-map downloads](https://www.fronius.com/en/help-center/solar-energy/products/monitoring-control/solutions/open-interfaces/modbus-tcp)

## Data flow, validation and diagnostics

`FroniusCoordinator._async_update_data` resets load/sample metadata each cycle
and reads inverter core data. `Hub._async_refresh_optional_data` reads the
primary meter first and evaluates `Hub._apply_modbus_load_data` immediately,
before secondary meters and optional status/settings/controls/MPPT/storage/Web
API work. The existing optional reads continue independently of load validity.

Client request start is recorded before awaiting transport, including connection,
transaction waits and retries. Completion is recorded only after decoding, and
a new read invalidates its old completion metadata first. Values must be finite
real numbers, not booleans, strings, NaN or infinity. Intervals must be ordered,
completed and at most 500 ms old from request start when the balance is evaluated.
This bounds the entire adjacent request window, not just completion-time skew.
The existing numerical limit is retained rather than increased. Direct load
meters require only their own valid fresh response.

Positive, zero and negative balances all pass the same validity checks. The
signed result is published without clamping, charge exceptions or cached-load
fallback. Recovery follows the next valid fresh pair. Rejection warnings for
missing/invalid/stale samples or unsupported locations are limited to one per
hub per five minutes, with further diagnostics at debug level. Successful signed
balances are debug-level data, not warning-level failures.

A successful coordinator poll with `load=None` means HA `unknown`; other sensor
values remain usable. Outside the unchanged battery-write recovery window, core
failure raises `UpdateFailed` and coordinator entities become `unavailable`.

## Modbus writes and Predbat

Earlier write logs contained an inverter function 0x90 exception code 3 (illegal
data value) and a separately cancelled request. Neither establishes a causal
link to signed load balances. Pymodbus 3.11.2 serializes transactions with an
asyncio lock, but that does not create an atomic multi-device measurement.

No write lock is added around full polling cycles; no write/mode/rate/reserve,
Web API, transport-write or recovery method is changed. A transport wait during
the pair counts toward load timing validity. There are no added polls/retries.
53 write/control/recovery methods remain structurally identical to the fork
base; the same audit on the newer upstream compatibility checkout preserved its
serialization/mode fixes. Mocked signed charge/discharge payload tests perform
no physical write. Review any separate Predbat gross-consumption input as
explained above; the integration cannot infer unmeasured balcony production.

## Tests and verification results

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
python -m pytest -q tests
python -m compileall -q custom_components/fronius_modbus tests
git diff --check
```

Use Python 3.14, matching the investigated HA runtime. The corrected suite has
**83 regression cases**, covering PV/export/import, both battery directions,
signed additional-generator demand, positive and zero balances with nonzero
generator output, the reported -171-W/30-W examples, low/high skew, long response
intervals, invalid/missing data, SunSpec decoding/sentinels, meter locations,
warning throttling, primary-first ordering, failure isolation, recovery windows
and simulated Predbat writes. The actual hub, coordinator and decoder are loaded
with lightweight HA boundaries and real pymodbus data types. This is not a full
HA config-entry/lifecycle test or hardware certification of every inverter model.

**83 tests passed on the original fork base and on the upstream compatibility
checkout, and all 83 passed again after merging the synchronized fork main
(`81a631b`) into the fix branch.** All 53 audited write/control/recovery methods
remain identical to that new base. Syntax compilation and `git diff --check`
also passed. No production deployment has been made.

The initial 79-case suite passed locally and in GitHub Actions before the signed
balance correction; four explicit additional-generator scenarios extend it.
Inherited hassfest validation fails on unchanged uppercase translation keys
`A`/`Conn` in `translations/en.json`, with an existing `config.title` warning.
HACS fails because the fork has no valid repository topics (1/10 checks).
Translation files, manifest, HACS config and inherited workflows are unchanged.
Those failures are documented rather than suppressed; they are outside this fix.

## Installation, verification and rollback

1. Back up the complete installed component and record its version/commit.
2. Review the diff against the synchronized 0.3.3 base. This branch now includes
   that upstream release and its battery-control fixes. If the target installation
   has further local changes or a later version, apply/cherry-pick only the
   production diff onto that exact target version and retest; preserve its fixes.
3. Run `ha core check` on the actual target, then restart/reload to activate the
   Python changes. Entity IDs and config entries do not require migration.
4. Verify PV/grid/AC data and normal scheduling through naturally occurring
   battery transitions. For this sensor, interpret signed net demand correctly
   and check consumers requiring gross household load before deployment.
5. Compare against simultaneous balcony production if a real household
   consumption reference is needed. Negative alone is not an alarm condition.
   The existing production warning automation may separately need adjustment;
   this PR does not edit HA automations or add a generator sensor configuration.
6. For actual unknown samples, inspect the rate-limited reason and temporarily
   enable debug logging for `custom_components.fronius_modbus.hub` if needed.
7. Roll back by restoring the complete backed-up component, running
   `ha core check`, restarting HA and verifying entities/control operation.
