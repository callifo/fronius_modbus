# AGENTS.md

Home Assistant custom integration for Fronius inverters: SunSpec over Modbus TCP
plus the inverter's local web API. Domain `fronius_modbus`, distributed through
HACS from `callifo/fronius_modbus`. It writes to real power hardware, and its
users carry years of entity history, so two things outrank everything else:
**write safety** and **never stranding an existing entity**.

`tests/README.md` explains every gate and guard in depth. Read it before
changing a guard, a budget or the CI workflow.

## Setup

Python **3.14 only**: the code uses PEP 758 `except A, B:` without parentheses.
That is intentional, do not "fix" it. Older interpreters cannot even collect
the tests.

Keep the virtualenv **outside the repository** (several guards scan the whole
tree).

```sh
uv venv --python 3.14 ../fm-venv
PY=../fm-venv/bin/python
uv pip install --python $PY -r requirements_test.txt
uv pip install --python $PY $($PY .github/scripts/core_modbus_pins.py)
```

The last line is not optional. `requirements_test.txt` leaves Home Assistant
unpinned, and `modbus-connection` then resolves newer than the exact pin of
Home Assistant's own `modbus` integration. Without it
`core_modbus_pins.py --check` fails and so does `check.sh`.

## Verify your work

| Purpose | Command | Time |
| --- | --- | --- |
| Lint | `$PY -m ruff check .` | < 1 s |
| Format check (apply: `ruff format .`) | `$PY -m ruff format --check .` | < 1 s |
| Types | `$PY -m mypy` | ~5 s |
| Fast tests, no e2e | `$PY -m pytest tests/ -q` | ~30 s |
| All tests, as CI runs them | `$PY -m pytest tests/ -q -m ""` | ~50 s |
| One file / one test | `$PY -m pytest tests/test_storage.py -q` / `... -k name_part` | seconds |
| Mutation plan still matches the code | `$PY -m pytest tests/test_mutation_harness.py -q` | seconds |
| Mutation run | `$PY .github/scripts/mutate.py .github/mutations/plan.json` | ~5 min |
| Everything, same set as CI | `PYTHON=$PY .github/scripts/check.sh` | ~7 min |

- Plain `pytest` **deselects the `e2e` marker** (about 170 tests that boot a
  real Home Assistant). Before calling work done, run with `-m ""`.
- `check.sh` stops at the first failure and **fails closed** when `gitleaks`
  (>= 8.30) or `pwsh` is missing. If you cannot install them, run its steps by
  hand and say which ones you skipped. Without `gitleaks` the six tests in
  `tests/test_secret_scan.py` skip locally but fail in CI.
- `MIN_HA_PYTHON=<second venv> check.sh` repeats the suite on the minimum Home
  Assistant from `hacs.json`. CI always runs that; locally it is optional.
- hassfest and HACS validation exist only as CI workflows.

Minimum loop after any change: `ruff check`, `ruff format --check`, `mypy`,
`pytest -m ""`. Add the mutation run when you touched a write path, a
migration, or anything listed in `.github/mutations/plan.json`.

### Gates that turn a green run red

- Coverage floor 90 % (`check.sh`). Raise it as coverage rises, never lower it.
- Any single test over 30 s fails the session (`tests/durations.py`).
- Module ceiling of 900 lines and cognitive complexity of 15 per function
  (`tests/test_budgets.py`). The listed exceptions are exact ratchets: when
  code grows or shrinks, change the budget entry in the same commit.
- Every package module is either in `[tool.mypy] files` or listed with a reason
  in `tests/test_mypy_scope.py`. A new module needs one of the two; prefer
  typing it.
- LF line endings only, in every text file (`tests/test_line_endings.py`).
- The CI workflow and `check.sh` must run the same tools
  (`tests/test_guards.py`), and `tests/test_ci_matrix.py` asserts literal
  strings in `.github/workflows/test.yaml`. Edit the workflow with both tests
  open.

## Architecture

All paths below are under `custom_components/fronius_modbus/`.

| Layer | Modules | Rule |
| --- | --- | --- |
| Device library | `fronius_modbus_api/` (`device`, `storage`, `controls`, `writes`, `sunspec_models`, `exceptions`) | No `homeassistant` or `voluptuous` import, ever. Raises `ControlRefused` / `ControlUnavailable` with a translation key. |
| Pure parsers | `derived`, `component_readings`, `inverter_events`, `energy_outputs`, `meter_topology`, `export_limit`, `web_errors`, `froniuswebclient` | No Home Assistant. `froniuswebclient` is synchronous `requests`, run in the executor. |
| Runtime | `coordinator` (Modbus, fast meter power and web coordinators, `FroniusRuntimeData`), `web_control` (web writes, their serialisation, repair issue) | |
| Entities | `descriptions` (dataclass types), `entities` (the table), `mppt_entities`, `energy_output_entities`, `event_entities`, `entity_base` | Behaviour is data: each row carries `value_fn`, `exists_fn`, `available_fn`, `set_fn`. |
| Platforms | `sensor`, `number`, `select`, `switch`, `button`, `event` | Thin. They build entities from `entities.*_descriptions(runtime)`. |
| Lifecycle | `__init__`, `config_flow`, `flow_*`, `discovery`, `migrations`, `adoption`, `repairs`, `diagnostics`, `token_store` | |

- The Modbus connection is the shared one of Home Assistant's core `modbus`
  integration (`dependencies: ["modbus"]`); this integration opens no socket of
  its own.
- Setup order in `__init__.async_setup_entry` is load-bearing: token, web
  client, meter topology, units, first Modbus refresh, web refresh, runtime
  data, adoption, migrations, cleanup, platforms.
- The entity set changes only through an **entry reload**. The coordinator
  schedules one when a sub-system answers for the first time, the SunSpec map
  shifts, the device identity changes, or a meter topology is confirmed.
- Every write goes through `FroniusEntity.async_run_write`, which maps
  `ControlRefused` to `ServiceValidationError` and `ControlUnavailable` or a
  Modbus error to `HomeAssistantError`.

## Hard rules

### Existing installs

- A description's `key` is permanent. The unique id is
  `fm_<instance_key>_<key>`; `tests/legacy_unique_ids.json` is the contract.
  Never rename or remove a key, move it to another platform, or drop the
  `state_class` of a sensor that has statistics. Display names live in the
  translations and are free to change.
- Device identifiers (`<instance_key>_inverter`, `_battery_storage`,
  `_meter_<unit>`) are just as permanent.
- `async_remove_unexpected_entities` **deletes** every registered entity whose
  unique id the current runtime does not expect. An `exists_fn` that flips to
  false on a transient condition therefore deletes the entity and its history.
  For anything that can appear and disappear (MPPT strings at night, endpoints
  that return 404) reuse the `runtime.registered_keys` pattern.
- Config entry `VERSION` stays 1. `MINOR_VERSION` lives in both
  `config_flow.py` and `migrations.py`; bump it only together with a migration
  step. An additive option with a default needs no bump.
- Never reuse a retired enum state key for a new meaning: it relabels history
  and breaks automations.

### Real hardware

Writes persist on the inverter with no timeout, also after the integration is
removed. The sequences below encode measured firmware behaviour and are covered
by the mutation plan.

- Do not reorder the writes in `StorageControl.set_mode` or `_write_rate`
  (rates first, `StorCtl_Mod` last). The firmware refuses an invalid window.
- Do not add a read-back after a write. A GEN24 refuses reads for a moment
  after one. Reads that matter happen before the first write; the following
  polls confirm.
- The AC limit and power factor pulse their enable flag off and on around the
  value. Keep the restore-on-failure-and-cancel path, or a live limit stays
  off.
- A web battery write stalls the inverter's Modbus for seconds. Keep the
  tolerance window in the coordinator, and send web writes through
  `@_serialised` or `@_last_writer_wins`, never in a burst.
- `_derive_extended_mode` attributes register images that several modes share,
  also when a second controller such as evcc writes them. Read issues 126, 127
  and 141 before simplifying it.
- `ensure_modbus_enabled` rewrites the inverter's whole Modbus configuration.
  The restriction default stays `keep`, and a SunSpec mode switch needs the
  owner's consent form.

### Secrets and privacy

- No logger call passes a host, serial, token, IP or URL
  (`tests/test_log_hygiene.py`). Log errors through `error_text(err)`.
- `requests` is called in exactly one function, `_http` in
  `froniuswebclient.py`.
- The web password is never stored. The derived token is password-equivalent
  and lives only in `token_store`: never in entry data, diagnostics or logs.
- A new `WebData` field that carries an identifier must be added to the
  redaction in `diagnostics.py`. Component readings are a field whitelist; keep
  it one.
- Fixtures hold sanitised captures and documentation addresses (`192.0.2.x`).
  Never commit a credential-shaped literal, not even a fake one.

### Machine-enforced conventions

- Every user-visible string exists in `translations/en.json` **and** `de.json`:
  entity names, state keys, exception messages (same placeholders), flow errors
  and aborts. Orphaned texts fail as well. Keys are lowercase snake case.
- Icons live only in `icons.json`, never as `icon=` in code.
- Comments say why (decision, constraint, incident) and never restate the code
  (`tests/test_comment_narration.py`, tests included).
- Inside the package use relative imports.
- Each platform module declares `PARALLEL_UPDATES`: 0 for sensor, event and
  number, 1 for the others.
- A runtime requirement appears identically in `manifest.json` and
  `requirements.txt`, as a lower bound.
- A new guard test needs a proof that it can fail, an entry in `GUARD_FILES`
  (`tests/test_guards.py`) and a row in the `tests/README.md` table.

## Recipes

**Modbus sensor**

1. Add the point to `fronius_modbus_api/sunspec_models.py` if it is missing.
2. Add a `_sensor(...)` row to `entities.py`; an enum also needs its state map
   in `const.py`.
3. Add the name (and states) to `en.json` and `de.json`.
4. Test in `test_entities.py` / `test_device.py`; raise the `entities.py` line
   budget if needed.

**Web component sensor**: add a `ComponentReading` row in
`component_readings.py`, the translations, and a test in
`test_component_readings.py`.

**Writable control**

1. Add a library method that validates, raises `ControlRefused` (the user can
   correct it) or `ControlUnavailable`, and compares before it writes. For the
   web side add a client method plus a `web_control` setter.
2. Add the description with `set_fn` to the matching table in `entities.py`.
3. Add entity and exception texts in both languages.
4. Test in `test_storage.py` / `test_controls.py` / `test_web_control.py` /
   `test_entity_writes.py`, and add a mutation for every safety guard.

**Config option**: `const.py` (`CONF_*`, `DEFAULT_*`), `flow_forms.py`,
`flow_settings.py`, validation in `config_flow.py`, texts under both `config`
and `options` in both languages, read with `_entry_value` in `__init__.py`,
tests in `test_config_flow.py`.

**Moved or edited code**: when a snippet in `.github/mutations/plan.json` no
longer matches exactly once, or a test named in a selector was renamed, update
the plan. A mutation that survives means a weak test: fix the test, not the
plan.

## Tests

- Never a real network. An autouse fixture fails any test that reaches the real
  Modbus connection.
- Fixtures (`tests/conftest.py`): `connection`, `inverter_unit` and
  `meter_unit` for library tests, an in-memory device seeded from a captured
  GEN24; `mock_modbus` for Home Assistant level tests.
- The web API is faked at `requests.adapters.HTTPAdapter.send`, so the real
  digest auth runs. A fake must not answer attributes the real client lacks.
- Test names are full-sentence behaviour descriptions
  (`test_a_meter_outage_only_takes_the_meter_entities_down`). No test classes.
  A docstring only for the why, for example the issue a fix came from.
- `asyncio_mode = auto`: write plain `async def test_...`.

## Code style

- A module docstring that says what the module is and why it exists;
  docstrings on public classes and functions, noun style. Five legacy modules
  have frozen ruff ignores; do not extend that list.
- `from __future__ import annotations`, `X | None`, PEP 695 aliases and
  generics. Everything in the mypy scope is strictly typed.
- Descriptions are `@dataclass(frozen=True, kw_only=True)` rows built through
  small factories.
- Lazy `%` logging. An expected outage logs at info, once when it starts and
  once on recovery; a device that answers and refuses stays at warning or
  error.
- Tuning constants sit at the top of their module with a comment giving the
  measurement or incident behind them.

## Commits, changelog, releases

- Subjects: sentence case, imperative, no prefix, no trailing period, about
  40 to 70 characters ("Keep the 0.1.9 meter on the address it was set to").
  Tests often go in a companion commit starting with "Test ...".
- Refer to issues as "issue 141", without `#`, in commits, comments and the
  changelog.
- Branches `feature/...`, `fix/...`, `docs/...`, `release/<version>`; pull
  requests to `main`, merged with a merge commit.
- `CHANGELOG.md`: every user-visible change gets a bullet under
  `## Unreleased` (create the section when it is missing), below
  `### Added`, `### Changed` or `### Fixed`, written as the effect on the user.
  Refactors, tests and CI changes get none.
- The version lives in `manifest.json`; the minimum Home Assistant in
  `hacs.json`, which the CI matrix follows. A release is its own
  `release/<version>` change touching `CHANGELOG.md`, `manifest.json` and
  `README.md`, followed by a bare version tag (`1.4.0b3`). Do not bump the
  version or tag unless asked.
- User documentation lives in the GitHub wiki, outside this repository. When a
  change adds or renames entities, say that the wiki needs an update.

## Other

- `tools/inverter_dump.ps1` is a read-only PowerShell dump tool that users run
  for bug reports. A change to it needs `tools/inverter_dump.tests.ps1`
  updated; CI runs those tests with `pwsh`.
- New register fixtures come from a diagnostics download; see the Development
  section of `README.md`.
