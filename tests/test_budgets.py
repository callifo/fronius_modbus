"""Size and complexity may only ever go down (the anti-erosion ratchet).

Two different rules, because the two numbers behave differently:

  Lines are a CEILING. They move on almost every change, so demanding an
  exact match would mean a budget commit for every added comment. A file
  simply may not grow past what is written here.

  Complexity is a RATCHET - an exact match. It changes rarely, and when a
  function does get simpler, that progress is locked in rather than left as
  headroom for the next person to spend.

Adding an entry is allowed. It is meant to be a visible, deliberate act in a
diff. Every number below was frozen at what the code weighed when the guard
was adopted - none of them is a target, and each is a place to shrink.
"""

import ast
import pathlib

from .complexity import functions, score

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)

# No module may pass this without an entry below. It sits in today's gap
# between the ordinary modules (the largest just under 900 lines) and the
# entity table.
LINE_LIMIT = 900

# The entity table: one EntityDescription per entity, ~85 % declarative.
LINE_BUDGETS = {
    # 1665 until the total sensor learned to follow a genuine counter reset.
    # 1688 until the reset counter learned to require consecutive lower polls.
    # 1691 until the module entered the mypy scope: the narrowing helpers and
    # the value_fn factories that replaced the loop-bound lambda defaults.
    # +29: the translation-key map that keeps hassfest happy (upstream data keys
    # are not valid translation keys), table file, ~85 % declarative
    # 1832 + the throttling sensor (issue 112)
    # 1888 + the backup reserve, the SoC mode, the web SoC minimum (issue 136), the capacity note
    # 1909 + translated write errors (quality scale exception-translations)
    # 1842 - the icons, now in icons.json (quality scale icon-translations)
    # 1850 + the translated error for an unreachable web interface
    # 1910 + the component sensor builder; the table is component_readings.py
    # 1889 - the two web temperatures, now rows of that table
    # 1900 + a power module kept once registered
    # 1625 - the base entity and the device info, now entity.py
    # 1680 + the inverter event entities
    # 1625 - the description types, now descriptions.py; + the service event entities
    # 1560 - the MPPT module sensors, now mppt_entities.py
    # 1562 + the energy output sensors, whose rows are energy_output_entities.py
    # 1570 + the storage mode select waits for readable registers
    # 1572 + a Datamanager's sensors, whose rows are datamanager_entities.py
    # 1557 - the battery's DC side, now storage_dc.py
    "entities.py": 1557,
    # 898 + a meter the answer names without a usable address marks the
    # answer incomplete; the parser shares _body_data,
    # _clean_text and _as_int with the other payloads, so it stays here.
    # 907 + an error status keeps the fields the inverter names as refused,
    # read by the same helper as the refusals in a 200 answer.
    # 918 + a status page or a soft limit config of another shape is a failed
    # read, not an AttributeError or KeyError that escapes the client.
    # 922 + the address to allow is confirmed in the flow, not only resolved.
    # 923 + a challenge part without a value is refused like a missing one.
    # 931 + the restriction says whether it already allows an address.
    "froniuswebclient.py": 931,
    # 916 + a reserve without the web login is refused instead of reaching
    # Modbus alone; the check sits in the write lock, next to the other one.
    # 932 + a cancelled write waits for its thread before it leaves the
    # write lock; every web write goes through the one job runner.
    # 960 + a reserve waits for the SoC mode, a cancelled caller drops its
    # ticket, and its thread's outcome is taken instead of left to the loop.
    # 964 + a login refused after the caller was cancelled asks to log in.
    "web_control.py": 964,
    # 1060 + the step that shows the address to allow before the restriction
    # is written, in all four flows; splitting the flows is a follow-up.
    # 1070 + the address step refuses addresses no client has, keeps a
    # corrected one, and asks for one it could not resolve.
    # 1073 + the address is asked only when the allowed list would change.
    # (- the error for an address that cannot be resolved: the step asks.)
    # 1108: the owner can ask for the address an allowed list skips, and the
    # model type answer keeps the address settled before it.
    # 1099 - the check of the address to allow, now with its form.
    # 1064 - the claim of a new entry, now inverter_choice.py; - the form errors.
    # 1083 + the login of a Datamanager, - the token and address helpers.
    "config_flow.py": 1083,
}

# SonarSource's own default. Above it, a function is one somebody has to
# re-read from the top to change safely.
COMPLEXITY_LIMIT = 15

# What was over the limit when the guard came in. None of these was
# simplified on adoption - the point of the guard is that nothing gets worse
# and every improvement is written down, so an entry that got under the limit
# stays as a ratchet at its new value.
COMPLEXITY_BUDGETS = {
    "froniuswebclient.py::_parse_power_meter_info": 24,
    "migrations.py::async_migrate_v019_mppt_statistics": 14,
}


def _relative(source_file: pathlib.Path) -> str:
    return source_file.relative_to(PACKAGE).as_posix()


def _line_counts():
    return {
        _relative(source_file): len(
            source_file.read_text(encoding="utf-8").splitlines()
        )
        for source_file in sorted(PACKAGE.rglob("*.py"))
    }


def _complexities():
    found = {}
    for source_file in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for name, node in functions(tree):
            found[f"{_relative(source_file)}::{name}"] = score(node)
    return found


def test_no_module_grows_past_its_budget():
    too_big = {
        name: count
        for name, count in _line_counts().items()
        if count > LINE_BUDGETS.get(name, LINE_LIMIT)
    }

    assert not too_big, (
        f"module(s) over budget: {too_big}. Split the module, or - if the "
        "growth is genuinely warranted - raise its entry in LINE_BUDGETS in "
        "the same commit, so the decision is visible in the diff."
    )


def test_a_shrunk_module_is_not_left_with_its_old_budget():
    """Otherwise the ceiling drifts away from reality and stops meaning
    anything: a file that halved would still be allowed to double back."""
    counts = _line_counts()
    stale = {
        name: (budget, counts.get(name))
        for name, budget in LINE_BUDGETS.items()
        if counts.get(name) is None or budget - counts[name] > 200
    }

    assert not stale, (
        f"LINE_BUDGETS entries far above the real size (budget, actual): "
        f"{stale}. Lower them to today's count - locking in the shrink is "
        "the point of the ratchet."
    )


def test_no_function_is_more_complex_than_its_budget():
    over = {
        name: value
        for name, value in _complexities().items()
        if value > COMPLEXITY_BUDGETS.get(name, COMPLEXITY_LIMIT)
    }

    assert not over, (
        f"function(s) over budget: {over}. Cognitive complexity counts "
        "NESTING, so an early return or a guard clause usually helps more "
        "than extracting a helper. If the complexity is warranted, add the "
        "entry to COMPLEXITY_BUDGETS."
    )


def test_a_simplified_function_lowers_its_budget():
    """The ratchet itself: progress is written down, not left as headroom.

    Also catches the entry for a function that was renamed or deleted - an
    exemption nobody can find is an exemption nobody removes.
    """
    complexities = _complexities()
    drifted = {
        name: (budget, complexities.get(name))
        for name, budget in COMPLEXITY_BUDGETS.items()
        if complexities.get(name) != budget
    }

    assert not drifted, (
        f"COMPLEXITY_BUDGETS out of step (budget, actual; None = gone): "
        f"{drifted}. Set each to the current value, or drop the entry when "
        "the function is gone or back under the limit."
    )


def test_the_measuring_stick_still_measures():
    """Guards the guard: a scorer that returned 0 for everything would make
    every budget above pass. The textbook shape - three levels of nesting,
    one boolean sequence."""
    tree = ast.parse(
        "def f(items):\n"
        "    for item in items:\n"  # +1
        "        if item and item.ok:\n"  # +2 (nesting 1) +1 (bool seq)
        "            while item.next:\n"  # +3 (nesting 2)
        "                item = item.next\n"
    )

    assert score(tree.body[0]) == 7
