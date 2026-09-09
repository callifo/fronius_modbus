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
# between the ordinary modules (the largest is under 700 lines) and the
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
    "entities.py": 1814,
}

# SonarSource's own default. Above it, a function is one somebody has to
# re-read from the top to change safely.
COMPLEXITY_LIMIT = 15

# What is over the limit today, exactly. None of these was simplified on
# adoption - the point of the guard is that nothing gets worse and every
# improvement is written down.
COMPLEXITY_BUDGETS = {
    "derived.py::LoadEstimator.update": 19,
    "froniuswebclient.py::_parse_power_meter_info": 26,
    "migrations.py::async_migrate_v019_mppt_statistics": 19,
    "web_control.py::FroniusWebControl._async_refresh_locked": 18,
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
