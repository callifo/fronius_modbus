"""The CI workflow must test the Home Assistant release it claims to test.

The job named "current HA" installs Home Assistant's own test plugin, whose
version is the Home Assistant version. Its predecessor ended with 2026.10,
and a job still installing it reads "current" while testing 2026.10.

The "minimum" job is the other end: it pins the old plugin release that ships
exactly the Home Assistant version hacs.json declares, so an API that does
not exist yet on that release is caught before a user meets it.

Only the workflow text is read - no PyPI access, so this stays a fast unit
test rather than a network-dependent one.
"""

import json
from pathlib import Path
import re
import tomllib

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "test.yaml"
TEST_REQUIREMENTS = REPO / "requirements_test.txt"
# The gates that must judge a change the same way on a laptop and in CI.
GATE_TOOLS = ("ruff", "mypy", "pip-audit")


def _steps_of(job: str) -> list:
    """The lines of one workflow job that DO something - not the comments
    that talk about it. A comment explaining why a file is NOT used satisfies
    a plain substring search for that file."""
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index(f"  {job}:")
    rest = workflow[start + 1 :]
    end = re.search(r"^  \w+:\s*$", rest, flags=re.MULTILINE)
    body = workflow[start : start + 1 + end.start()] if end else workflow[start:]
    return [line for line in body.splitlines() if not line.strip().startswith("#")]


def _minimum_job_label() -> str:
    labels = re.findall(
        r'- name: "(minimum[^"]*)"', WORKFLOW.read_text(encoding="utf-8")
    )
    assert len(labels) == 1, f"expected one minimum job, found {labels}"
    return labels[0]


def test_the_minimum_job_names_the_version_hacs_declares():
    """Three places say which Home Assistant is the oldest supported one -
    hacs.json, the pinned plugin release, and the job's own name. The pin can
    only be checked with PyPI (check_min_ha.py does that inside the job); the
    label can be checked here, and it is the half that lies to a reader."""
    declared = json.loads((REPO / "hacs.json").read_text(encoding="utf-8"))[
        "homeassistant"
    ]
    release = ".".join(declared.split(".")[:2])

    assert release in _minimum_job_label(), (
        f"hacs.json declares {declared} as the minimum, but the matrix job is "
        f'called "{_minimum_job_label()}". Raise the job name AND its pinned '
        "plugin release together - the pin is what decides what is tested."
    )


def test_the_minimum_job_actually_checks_the_version_it_installed():
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "check_min_ha.py" in workflow, (
        "the minimum job does not verify the Home Assistant it installed, so "
        "a wrong pin runs green under the right label"
    )
    assert re.search(r"if:\s*matrix\.check-declared-minimum", workflow)
    assert "check-declared-minimum: true" in workflow, (
        "no matrix entry opts into the check, so it never runs"
    )


def test_both_test_jobs_run_the_modbus_libraries_core_pins():
    """The minimum job ran modbus-connection 4.12 where 2026.9 pins 4.10.

    The integration shares core's Modbus connection, so an installation runs
    the library releases core pins. The workflow installs them and both the
    workflow and the local gate check them.
    """
    steps = "\n".join(_steps_of("pytest"))
    gate = (REPO / ".github" / "scripts" / "check.sh").read_text(encoding="utf-8")

    assert "pip install $(python .github/scripts/core_modbus_pins.py)" in steps
    assert "core_modbus_pins.py --check" in steps
    assert '"$PYTHON" .github/scripts/core_modbus_pins.py --check' in gate
    assert '"$MIN_HA_PYTHON" .github/scripts/core_modbus_pins.py --check' in gate


def test_the_test_job_has_the_gitleaks_its_allowlist_tests_need():
    """Without the binary the allowlist tests skipped in CI, every run.

    The gitleaks job scans with the change's own rules, so a widened allowlist
    passes it; only these tests hold the rules to a known token.
    """
    steps = "\n".join(_steps_of("pytest"))

    assert re.search(r'GITLEAKS_VERSION: "\d+\.\d+\.\d+"', steps)
    assert "_linux_x64.tar.gz" in steps
    assert "sha256sum --check" in steps


def test_a_patch_release_still_counts_as_the_declared_minimum():
    """hacs.json names a patch; the plugin pins whichever patch of that
    feature release it ships. Comparing them literally would fail the job for
    being right."""
    namespace: dict = {}
    source = (REPO / ".github" / "scripts" / "check_min_ha.py").read_text(
        encoding="utf-8"
    )
    body = source[source.index("def feature_release") : source.index("def main")]
    exec(compile(body, "check_min_ha.py", "exec"), namespace)  # noqa: S102
    feature_release = namespace["feature_release"]

    assert feature_release("2026.9.4") == feature_release("2026.9.0")
    assert feature_release("2026.10.0") != feature_release("2026.9.0")


def test_the_test_job_installs_its_plugin_from_the_matrix():
    steps = _steps_of("pytest")

    assert any('pip install "${{ matrix.plugin }}"' in line for line in steps)
    assert not [line for line in steps if "requirements_test.txt" in line], (
        "the local requirements are back in the test job, and with them the "
        "new plugin beside the old one in the minimum job"
    )


def test_the_coverage_plugin_is_installed_by_name():
    """The old test plugin brought pytest-cov along, the new one does not.

    CI and check.sh run the suite with --cov, and pytest stops at the
    command line without the plugin.
    """
    plugin_installs = [
        line
        for line in _steps_of("pytest")
        if 'pip install "${{ matrix.plugin }}"' in line
    ]
    requirements = TEST_REQUIREMENTS.read_text(encoding="utf-8").splitlines()

    assert plugin_installs
    assert all("pytest-cov" in line for line in plugin_installs)
    assert "pytest-cov" in [line.strip() for line in requirements]


CURRENT_PLUGIN = "pytest-homeassistant"
LEGACY_PLUGIN = "pytest-homeassistant-custom-component"


def test_the_current_jobs_install_home_assistants_own_test_plugin():
    """The old plugin's last release ships Home Assistant 2026.10.4.

    The job named "current HA" and the type check kept installing it, so both
    tested 2026.10 while users ran 2026.11. Home Assistant publishes its own
    plugin since 2026.11; its version is the Home Assistant version, and its
    betas are pre-releases pip leaves out.
    """
    pytest_steps = "\n".join(_steps_of("pytest"))
    mypy_steps = "\n".join(_steps_of("mypy"))

    assert f'plugin: "{CURRENT_PLUGIN}"' in pytest_steps
    assert f'pip install "{CURRENT_PLUGIN}" ' in mypy_steps
    assert not re.search(r"--pre\b", pytest_steps + mypy_steps), (
        "pip would install a Home Assistant beta"
    )


def test_only_the_minimum_job_installs_the_old_plugin():
    """The new plugin starts at 2026.11, so the minimum job still needs the
    old one. Anywhere else it means Home Assistant 2026.10."""
    workflow = WORKFLOW.read_text(encoding="utf-8").splitlines()
    uses = [
        line.strip()
        for line in workflow
        if LEGACY_PLUGIN in line and not line.strip().startswith("#")
    ]

    assert uses == [f'plugin: "{LEGACY_PLUGIN}==0.13.363"']


def test_the_local_test_requirements_name_the_new_plugin():
    requirements = [
        line.strip()
        for line in TEST_REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    assert CURRENT_PLUGIN in requirements
    assert LEGACY_PLUGIN not in requirements, "the two must not be installed together"


def test_the_suite_starts_without_the_respx_plugin():
    """With the new plugin, respx's pytest plugin imports httpx before Home
    Assistant can alias it, and pytest stops before the first test. The
    option has to be in the configuration: the suite starts pytest itself."""
    options = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = options["tool"]["pytest"]["ini_options"]["addopts"]

    assert "-p no:respx" in addopts


def test_the_test_job_runs_the_whole_suite_and_the_mutations():
    steps = _steps_of("pytest")

    assert any('pytest tests/ -q -m ""' in line for line in steps), (
        'the e2e tests are deselected by default; CI has to pass -m ""'
    )
    assert any("mutate.py .github/mutations" in line for line in steps), (
        "a green suite says the tests pass, not that they would catch anything"
    )


def test_the_type_check_runs_the_same_home_assistant_as_the_test_job():
    """A checker that vouches for another release vouches for nothing."""
    mypy_installs = [line for line in _steps_of("mypy") if "pip install" in line]
    current_plugin = f'plugin: "{CURRENT_PLUGIN}"'

    assert current_plugin in "\n".join(_steps_of("pytest"))
    assert [line for line in mypy_installs if f'"{CURRENT_PLUGIN}"' in line], (
        "the type check installs its Home Assistant some other way than the "
        "job whose type surface it is supposed to be checking"
    )
    assert not [line for line in mypy_installs if "requirements_test.txt" in line]


def test_the_workflow_still_runs_ruff_pinned():
    """Formatting that only one machine checks survives until the first
    commit written somewhere else; an unpinned Ruff enforces whatever it
    decided this week."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "run: ruff check ." in workflow
    assert "run: ruff format --check ." in workflow
    assert re.search(r'pip install "ruff==[\d.]+"', workflow)


def test_the_secret_scan_in_ci_uses_the_repositorys_own_rules():
    """The allowlist for the register table lives in .gitleaks.toml. The
    action does not read it unless told, and then flags the translation keys
    as credentials - a red job for a file with no secret in it."""
    steps = _steps_of("gitleaks")

    assert any("GITLEAKS_CONFIG:" in line for line in steps), (
        "the gitleaks job does not name .gitleaks.toml, so CI scans with "
        "different rules than check.sh and the pre-push hook"
    )
    assert any(".gitleaks.toml" in line for line in steps)


def test_the_secret_scan_in_ci_uses_the_gitleaks_the_gate_requires():
    """The action fell back to its own default, 8.24.3, which the gate refuses.

    The allowlist is written for the match shape of the gate's minimum, so an
    older scanner is a different gate, not a weaker one.
    """
    gate = (REPO / ".github" / "scripts" / "check.sh").read_text(encoding="utf-8")
    minimum = re.search(r"^GITLEAKS_MIN=([\d.]+)$", gate, re.MULTILINE)
    version_of = re.compile(r'GITLEAKS_VERSION: "([\d.]+)"')
    scan = version_of.search(" ".join(_steps_of("gitleaks")))
    installed = version_of.search(" ".join(_steps_of("pytest")))

    assert minimum and scan and installed, "the gitleaks job names no version"
    version = tuple(int(part) for part in scan.group(1).split("."))
    assert version >= tuple(int(part) for part in minimum.group(1).split("."))
    assert scan.group(1) == installed.group(1)


def _pins_of(text: str) -> dict[str, str]:
    """Every `tool==version` in a text, whatever quotes it sits in."""
    return {
        tool: version
        for tool, version in re.findall(r"([a-z-]+)==([\d.]+)", text)
        if tool in GATE_TOOLS
    }


def test_the_workflow_and_the_local_gates_pin_the_same_versions():
    """check.sh and the pre-push hook run what requirements_test.txt
    installs, CI runs what the workflow installs. Two pins for one tool
    means a formatter that reflows differently from the one that decides
    the build - a red CI nobody changed."""
    in_ci = _pins_of(WORKFLOW.read_text(encoding="utf-8"))
    locally = _pins_of(TEST_REQUIREMENTS.read_text(encoding="utf-8"))

    assert set(in_ci) == set(GATE_TOOLS), f"a gate lost its pin in CI: {in_ci}"
    assert in_ci == locally, (
        f"CI and requirements_test.txt disagree: {in_ci} vs {locally}. "
        "Raise both in the same commit."
    )


def test_the_step_reader_skips_comments_and_stops_at_the_next_job():
    """Proof-of-red for the helper the checks above rely on."""
    steps = _steps_of("mypy")

    assert steps and steps[0].strip() == "mypy:"
    assert not any(line.strip().startswith("#") for line in steps)
    assert not any(line.strip() == "pytest:" for line in steps), (
        "the reader ran into the next job"
    )
