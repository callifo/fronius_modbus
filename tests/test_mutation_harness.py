"""The mutation harness has to fail loudly rather than report success.

Its whole value is the sentence "all mutations caught". Every way that sentence
can be printed without being true is a way to be lied to about test quality -
which is exactly the problem the harness exists to solve. So the silent-pass
routes are pinned here: a snippet that no longer matches, a selector that
matches no tests, a selector clause that names a test which does not exist,
and a clause so wide that any red test at all would count.

The subprocess call is stubbed; running the real suite inside the suite would
add minutes for no extra confidence about this logic.
"""

import importlib.util
import json
from pathlib import Path
import re

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / ".github" / "scripts" / "mutate.py"
PLAN = REPO / ".github" / "mutations" / "plan.json"


def _load():
    spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutate = _load()


class _Result:
    """What subprocess.run returns, as far as the harness reads it."""

    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_a_snippet_that_no_longer_matches_is_an_error(tmp_path, monkeypatch):
    """The dangerous case: the code moved on, the mutation quietly does
    nothing, and the run reports the test as verified."""
    target = tmp_path / "module.py"
    target.write_text("value = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        mutate.apply_mutation(
            {"path": "module.py", "old": "value = 2", "new": "value = 3"}
        )

    assert "expected once" in str(excinfo.value)


def test_an_ambiguous_snippet_is_an_error(tmp_path, monkeypatch):
    target = tmp_path / "module.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    with pytest.raises(SystemExit):
        mutate.apply_mutation({"path": "module.py", "old": "x = 1", "new": "x = 2"})


def test_the_original_comes_back_byte_for_byte(tmp_path, monkeypatch):
    """The snippets in the plan are written with \\n, so matching needs a
    newline-normalised read - and writing that back would turn a CRLF checkout
    into an LF one on every run."""
    target = tmp_path / "module.py"
    original = b"value = 1\r\nother = 2\r\n"
    target.write_bytes(original)
    monkeypatch.setattr(mutate, "REPO", tmp_path)

    path, kept = mutate.apply_mutation(
        {"path": "module.py", "old": "value = 1", "new": "value = 99"}
    )
    assert b"99" in path.read_bytes(), "the mutation was not applied at all"

    path.write_bytes(kept)

    assert path.read_bytes() == original


def test_a_selector_matching_no_tests_is_an_error(monkeypatch):
    """pytest exits 5 for "no tests ran", which is non-zero - a typo in the
    selector would otherwise certify every mutation as caught."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(5, "no tests ran")
    )

    with pytest.raises(SystemExit) as excinfo:
        mutate.run_tests("nothing_matches_this")

    assert "matched no tests" in str(excinfo.value)


def test_a_failing_suite_counts_as_caught(monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(1, "1 failed")
    )

    assert mutate.run_tests("something") is True


def test_a_passing_suite_counts_as_survived(monkeypatch):
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(0, "3 passed")
    )

    assert mutate.run_tests("something") is False


@pytest.mark.parametrize("code", [2, 3, 4])
def test_a_run_that_says_nothing_is_not_read_as_caught(monkeypatch, code):
    """Only exit code 1 means "the tests noticed". A usage error, an internal
    error or an interrupted run are not evidence."""
    monkeypatch.setattr(
        mutate.subprocess, "run", lambda *_args, **_kwargs: _Result(code, "", "boom")
    )

    with pytest.raises(SystemExit):
        mutate.run_tests("something")


def test_the_run_cannot_be_failed_by_the_duration_budget(monkeypatch):
    """conftest turns an otherwise GREEN run red when a test ran past the
    duration budget - here that would be a false "caught". The two signals
    share one exit code, so they have to be kept apart at the call."""
    seen = {}

    def record(argv, **_kwargs):
        seen["argv"] = argv
        return _Result(0, "3 passed")

    monkeypatch.setattr(mutate.subprocess, "run", record)

    mutate.run_tests("something")

    argv = seen["argv"]
    assert "--slow-test-seconds" in argv
    assert argv[argv.index("--slow-test-seconds") + 1] == "inf"


def test_the_file_is_restored_even_when_the_run_explodes(tmp_path, monkeypatch):
    """A harness that leaves mutated source behind would poison every later
    run - and the next commit."""
    target = tmp_path / "module.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(
        mutate, "collect_test_locations", lambda: {"anything": {"tests/x.py"}}
    )

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mutate, "run_tests", explode)
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            [
                {
                    "path": "module.py",
                    "old": "value = 1",
                    "new": "value = 2",
                    "tests": "anything",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan), "--jobs", "1"])

    with pytest.raises(RuntimeError):
        mutate.main()

    assert target.read_text(encoding="utf-8") == original


def test_an_empty_plan_is_refused(tmp_path, monkeypatch):
    """ "all 0 mutations caught" is the same green line as a real run."""
    plan = tmp_path / "plan.json"
    plan.write_text("[]", encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["mutate.py", str(plan)])

    with pytest.raises(SystemExit) as excinfo:
        mutate.main()

    assert "no mutations" in str(excinfo.value)


def _test_names() -> set:
    names = set()
    for module in Path(__file__).parent.glob("test_*.py"):
        names.update(
            re.findall(
                r"^\s*(?:async )?def (test_\w+)",
                module.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
    assert names, "no test functions found - the check would pass vacuously"
    return names


def _plan() -> list:
    return json.loads(PLAN.read_text(encoding="utf-8"))


def test_every_selector_clause_names_a_real_test():
    """mutate.py refuses a selector that matches NOTHING. It cannot refuse a
    selector where one clause of an `or` matches nothing - the other clause
    carries the run and the dead name sits there looking meaningful."""
    names = _test_names()

    for case in _plan():
        for clause in re.split(r"\s+(?:or|and)\s+", case["tests"]):
            clause = clause.strip()
            assert any(clause in name for name in names), (
                f"{case['label']}: selector clause {clause!r} matches no test"
            )


# How many tests one clause may select before it stops naming anything. A
# clause is matched as a SUBSTRING, so an ordinary word picks up whatever else
# happens to contain it. Three leaves room for a deliberate family of names.
CLAUSE_BREADTH_LIMIT = 3


def test_no_selector_clause_is_a_word_that_means_anything():
    """A mutation whose selector drags in two dozen unrelated tests is
    reported as caught by whichever of them happens to be red."""
    names = _test_names()

    too_wide = []
    for case in _plan():
        for clause in re.split(r"\s+(?:or|and)\s+", case["tests"]):
            clause = clause.strip()
            selected = [name for name in names if clause in name]
            if len(selected) > CLAUSE_BREADTH_LIMIT:
                too_wide.append(f"{clause!r} selects {len(selected)}")

    assert not too_wide, (
        f"selector clause(s) too wide to be evidence: {too_wide}. Name the "
        "test the mutation is actually about."
    )


def test_the_shipped_plan_still_matches_the_code():
    """The plan is only useful while its snippets exist. Left to rot it would
    fail at the worst moment - when someone finally runs it."""
    for case in _plan():
        source = (REPO / case["path"]).read_text(encoding="utf-8")
        assert source.count(case["old"]) == 1, (
            f"{case['label']}: the snippet no longer matches {case['path']} "
            "exactly once - update the plan"
        )


def test_the_shipped_plan_is_not_empty():
    assert len(_plan()) >= 10, "a plan this small guards next to nothing"
