# The gates and the guards

What protects this integration, what each guard holds, and what to do when
one of them turns red. Written for whoever changes this code next - which is
usually somebody with no memory of why any of it is here.

## Run everything

```sh
.github/scripts/check.sh
```

Ruff, formatting, mypy, pip-audit, gitleaks, the test suite with its coverage floor and the mutation run, in that order,
stopping at the first failure. CI runs the same set; a guard in
`tests/test_guards.py` fails if the two ever drift apart. Anything
machine-local arrives through the environment:

```sh
PYTHON=/path/to/venv/bin/python .github/scripts/check.sh
```

The suite is also run against the **minimum** supported Home Assistant (the
version `hacs.json` declares), which needs a second interpreter:

```sh
MIN_HA_PYTHON=/path/to/min-ha-venv/bin/python .github/scripts/check.sh
```

That run first checks which Home Assistant the interpreter actually holds
against `hacs.json` - a hand-pinned local venv ages quietly. Without the
variable the minimum run is skipped **and says so**.

The everyday run is shorter. The end-to-end tests boot a real Home Assistant
instance each and are deselected by default:

```sh
pytest tests/ -q            # fast: everything but e2e
pytest tests/ -q -m e2e     # only the slow ones
pytest tests/ -q -m ""      # all - what CI and check.sh run
```

## Why a mutation run

A passing test proves nothing on its own. `.github/mutations/plan.json`
describes deliberate breakages ("delete this guard clause", "swap these two
values"), and `.github/scripts/mutate.py` checks that a test actually fails
for each one.

A mutation that **survives** means the code was broken and the suite stayed
green: either the test is worthless, or a guard has gone blind and no longer
looks where the code moved.

When you add a behaviour worth keeping, add a mutation for it. When you move
code, the `path` and `old` fields move with it - `test_mutation_harness.py`
fails as soon as a snippet no longer matches.

## The guards

Structural tests that fail on a shape rather than on a value. Each one exists
because the thing it prevents happened, here or in a sibling project.

| Guard | Holds |
|---|---|
| `test_budgets.py` | No module or function grows past its frozen budget |
| `test_flow_messages.py` | Every abort reason and error key the config flow can show has a text in every translation file, and none outlives its use |
| `test_ci_matrix.py` | The CI matrix tests the Home Assistant releases it claims to: the declared minimum, and the newest final release (never a beta) |
| `test_comment_narration.py` | No comment merely restates the code it sits on (heuristic; a genuine why-comment passes) |
| `test_durations.py` | No single test quietly starts taking minutes (budget in `durations.py`, enforced from `conftest.py`), and a run that stops making progress is cut off rather than only measured |
| `test_guards.py` | No guard binds itself to one source file; every guard is listed; `check.sh` matches CI |
| `test_imports.py` | Every module imports outside the author's own tree - the `from config.custom_components...` line that shipped on main cannot ship again |
| `test_entity_table.py` | Every legacy unique-id key is still registered and translated (`legacy_unique_ids.json`) |
| `test_api_is_ha_free.py` | The device library under `fronius_modbus_api/` imports no Home Assistant |
| `test_mutation_harness.py` | The mutation run fails loudly rather than printing "all caught" without having checked |
| `test_mypy_scope.py` | Every module is in the mypy scope or carries a written reason why not yet |
| `test_platform_entities.py` | Every platform builds its entities through the one shared helper |
| `test_requirements.py` | `manifest.json` and `requirements.txt` name the same dependencies |
| `test_secret_scan.py` | The gitleaks allowlist for translation keys does not hide a token on the same line (needs the gitleaks binary; skips without it) |

### test_log_hygiene.py

Two things at once. An AST scan over the package refuses any logger call that
passes a host, serial, token or URL: Home Assistant logs travel to GitHub with
bug reports, and diagnostics already carry those values redacted. Drop the
value from the message rather than adding an exception here.

The other two tests hold the level a device outage is logged at. The quality
scale asks for `info` when a device goes away, but `DataUpdateCoordinator`
logs the outage at `error` and only the recovery at `info`, with no setting to
change it. `DeviceUnreachable` plus a filter on the coordinator's own logger
downgrades exactly that case; the counter-check keeps a device that answers
and refuses at `error`, so the filter cannot quietly swallow real failures.

## When a budget turns red

`tests/test_budgets.py` freezes size and complexity so nothing grows back.
Two different rules, on purpose:

**Lines are a ceiling.** They move on almost every change, so the test only
asks that a module not grow past its entry. Modules under `LINE_LIMIT` (900)
need no entry at all.

**Complexity is a ratchet - an exact match.** It changes rarely, and when a
function does get simpler that progress is written down rather than left as
headroom for the next person to spend.

| Message | What it means | What to do |
|---|---|---|
| module over budget | a file grew past its entry (or past 900 lines without one) | Split it. If the growth is genuinely warranted, raise the entry **in the same commit** - the point is that the decision is visible in the diff. |
| budget far above the real size | a module shrank; the ceiling is now meaningless | Lower the entry to today's count. |
| function over budget | new complexity, undeclared | Read the function. Cognitive complexity counts **nesting**, so an early return or a guard clause usually helps more than extracting a helper. If it is warranted, add the entry. |
| budget out of step | a function got simpler, was renamed, or is gone | Set the entry to the current value, or drop it. |

The measure is Cognitive Complexity (Campbell / SonarSource), implemented in
`tests/complexity.py`. It counts nesting rather than paths: a flat ten-case
dispatch is cheap, three loops inside each other are not. `COMPLEXITY_LIMIT`
is 15, SonarSource's default.

## The entity table

`entities.py` is the integration's data: one `EntityDescription` per entity,
with the value, availability and write functions beside it. It is ~85 % table
and therefore has its own line budget (`LINE_BUDGETS` in `test_budgets.py`)
rather than being held to the 900-line limit. `test_entity_table.py` holds it
against `legacy_unique_ids.json`: every unique id an existing installation
already has must still be produced, and every entity must have a translation.

## Adding a guard

Two rules, both learned the hard way:

1. **Scan the package, never a single file.** `PACKAGE.rglob("*.py")`, not
   `Path(some_module.__file__).read_text()`. A scan pinned to one file goes
   blind the moment code moves to a new module - and a blind guard is worse
   than none, because the suite stays green. The guard against it is in
   `test_guards.py`.
2. **Prove the guard can fail.** A test that passes proves nothing. Add a
   case that feeds the detector the exact shape it exists to catch - see
   `test_the_scan_catches_the_line_that_shipped` in `test_imports.py`, which
   uses the verbatim line from the incident.

Then add the file to `GUARD_FILES` in `test_guards.py` with one line saying
what it holds, and a row to [the guard table](#the-guards) above.

## Layout

Tests move with their subject: `test_device.py`, `test_storage.py`,
`test_controls.py` and `test_sunspec_models.py` cover the device library
against the library's in-memory mock unit, `test_coordinator.py` and
`test_derived.py` the polling and the derived values, `test_entities.py` and
`test_entity_table.py` the entity layer, `test_web_control.py` the web side,
and `test_config_flow.py` and `test_e2e.py` the parts that only exist once
Home Assistant is driving the integration (`test_e2e.py` is marked `e2e`).
The `mock_modbus` fixture in `conftest.py` stands in for the connection Home
Assistant's `modbus` integration shares - a fresh in-memory connection per
(re)load, seeded from the captured Symo GEN24 in `tests/fixtures/`.
