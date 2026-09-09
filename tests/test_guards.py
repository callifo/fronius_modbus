"""The guards that watch the guards.

Two failure modes threaten a suite of structural guards, and both are quiet:

  A guard goes BLIND. It scans one source file by name, code moves to a new
  module, and the scan keeps passing over the file it still knows. The suite
  stays green about a protection that no longer looks anywhere.

  A guard goes MISSING. Deleting a test is a green diff. Nothing says the
  protection went with it.

So: no test may bind itself to a single package source file, every guard file
has to be listed here by name, and the local check script has to run the same
gates as CI - a local run that promises less than CI is a run people believe
before pushing and should not.
"""

import ast
import pathlib
import re

TESTS = pathlib.Path(__file__).resolve().parent
REPO = TESTS.parents[0]
PACKAGE = REPO / "custom_components" / "fronius_modbus"

# Guard files, and what each one holds. Deleting one of these files - or
# emptying it - fails the index test below. This list is the answer to "what
# stops the old problems coming back", for whoever asks in six months.
GUARD_FILES = {
    "test_budgets.py": "no module or function grows past its frozen budget",
    "test_ci_matrix.py": "the CI workflow tests the Home Assistant release it claims to",
    "test_flow_messages.py": "every config-flow message has a text in every language",
    "test_comment_narration.py": "no comment merely restates the code it sits on",
    "test_durations.py": "no test quietly starts taking minutes",
    "test_imports.py": "every module imports outside the author's own tree",
    "test_entity_table.py": "every legacy key is still registered and translated",
    "test_api_is_ha_free.py": "the device library imports no Home Assistant",
    "test_log_hygiene.py": "no log line names the device, and an outage is not an error",
    "test_mutation_harness.py": "the mutation run fails loudly instead of reporting success",
    "test_mypy_scope.py": "every module is type-checked or says why it is not yet",
    "test_platform_entities.py": "every platform builds its entities through the shared helper",
    "test_requirements.py": "the manifest and requirements.txt name the same dependencies",
    "test_secret_scan.py": "the gitleaks allowlist does not hide a token on the same line",
    "test_guards.py": "the guards stay package-wide and stay present",
}

# Reading ONE source file is right where the subject genuinely is one file.
# Each exemption names why; anything else has to scan the package.
SINGLE_FILE_EXEMPTIONS: set = {
    # the translation keys the secret-scan allowlist must fit live in the one
    # entity table, and nowhere else
    ("test_secret_scan.py", "entities"),
}


def _a_source_file_of_this_package(target) -> set:
    """`<path> / "storage.py"` -> {"storage"}, when storage.py really is one of ours.

    Asked of the PACKAGE rather than of the variable name: keying on the
    literal `PACKAGE` would go blind the moment somebody calls it something
    else. And the question is genuinely "is this a module of the package" -
    the mutation harness builds a throwaway `root / "module.py"` in a temp
    directory to test itself, which looks identical and binds to nothing.
    """
    if not (isinstance(target, ast.BinOp) and isinstance(target.op, ast.Div)):
        return set()
    name = target.right
    if not (isinstance(name, ast.Constant) and str(name.value).endswith(".py")):
        return set()
    if not any(PACKAGE.rglob(str(name.value))):
        return set()
    return {str(name.value).removesuffix(".py")}


def _tests_that_read_one_source_file(source: str):
    """The modules whose source `source` reads as ONE named file.

    Three spellings: `Path(module.__file__).read_text()`,
    `(PACKAGE / "module.py").read_text()`, and a path BOUND to a name first
    and read further down. `Path(module.__file__).parent` is not a hit: that
    resolves the package and is exactly the shape a package-wide scan starts
    from.
    """
    tree = ast.parse(source)
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                module = _a_source_file_of_this_package(node.value)
                if module:
                    bound[target.id] = module

    found = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_text"
        ):
            continue
        found |= _a_source_file_of_this_package(node.func.value)
        if isinstance(node.func.value, ast.Name):
            found |= bound.get(node.func.value.id, set())
        target = ast.unparse(node.func.value)
        if "__file__" not in target or ".parent" in target:
            continue
        for inner in ast.walk(node.func.value):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "__file__"
                and isinstance(inner.value, ast.Name)
            ):
                found.add(inner.value.id)
    return found


def test_no_guard_is_pinned_to_a_single_source_file():
    """A scan that names one file goes blind the moment code moves - and a
    blind guard is worse than none, because the suite stays green."""
    offenders = []
    for test_file in sorted(TESTS.glob("test_*.py")):
        for module in _tests_that_read_one_source_file(
            test_file.read_text(encoding="utf-8")
        ):
            if (test_file.name, module) in SINGLE_FILE_EXEMPTIONS:
                continue
            offenders.append(f"{test_file.name} reads {module}.__file__")

    assert not offenders, (
        f"{offenders} - scan the package instead (PACKAGE.rglob('*.py')), or "
        "add an exemption saying why this subject really is one file."
    )


def test_the_scan_catches_the_shapes_it_was_written_for():
    """A guard that passes proves nothing; fed the exact shapes it exists to
    catch, and the ones it has to let through."""
    went_blind = 'SOURCE = pathlib.Path(entities.__file__).read_text(encoding="utf-8")'
    assert _tests_that_read_one_source_file(went_blind) == {"entities"}

    package_wide = (
        "PACKAGE = pathlib.Path(entities.__file__).parent\n"
        'sources = {f.name: f.read_text(encoding="utf-8") for f in PACKAGE.rglob("*.py")}'
    )
    assert _tests_that_read_one_source_file(package_wide) == set()

    by_package_path = 'source = (PACKAGE / "storage.py").read_text(encoding="utf-8")'
    assert _tests_that_read_one_source_file(by_package_path) == {"storage"}

    bound_first = (
        'STORAGE = pathlib.Path(__file__).resolve().parents[1] / "x" / "storage.py"\n'
        "def test_it():\n"
        "    tree = ast.parse(STORAGE.read_text(encoding='utf-8'))\n"
    )
    assert _tests_that_read_one_source_file(bound_first) == {"storage"}

    a_throwaway = 'text = (root / "module.py").read_text(encoding="utf-8")'
    assert _tests_that_read_one_source_file(a_throwaway) == set()


def test_every_guard_file_is_listed_and_present():
    """Deleting a guard is otherwise a green diff."""
    missing = [
        name
        for name in GUARD_FILES
        if not (TESTS / name).exists()
        or "def test_" not in (TESTS / name).read_text(encoding="utf-8")
    ]

    assert not missing, (
        f"guard file(s) gone or emptied: {missing}. If the protection is "
        "genuinely obsolete, remove the entry here in the same commit and "
        "say in the message what replaced it."
    )


def test_every_guard_is_described_in_the_readme():
    """The index says a guard exists; the README says what to do about it.

    Documentation that nothing checks quietly stops being true, and this text
    exists for the person with no memory of any of this.
    """
    readme = (TESTS / "README.md").read_text(encoding="utf-8")

    undocumented = [name for name in GUARD_FILES if name not in readme]
    assert not undocumented, (
        f"guard file(s) missing from tests/README.md: {undocumented}. Add a "
        "row to the guard table saying what each one holds."
    )


def _github_anchor(heading: str) -> str:
    """The fragment GitHub gives a heading: lowercase, punctuation dropped,
    spaces to hyphens."""
    slug = heading.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s+", "-", slug)


def test_every_link_within_a_document_lands_somewhere():
    """A link that does not resolve reads as documentation and is not."""
    broken = []
    checked = 0
    for document in (REPO / "README.md", TESTS / "README.md"):
        text = document.read_text(encoding="utf-8")
        anchors = {
            _github_anchor(heading)
            for heading in re.findall(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE)
        }
        for target in re.findall(r"\]\(#([^)]+)\)", text):
            checked += 1
            if target not in anchors:
                broken.append(f"{document.name}#{target}")

    assert checked, "no in-document links were found at all, so this proves nothing"
    assert not broken, f"link(s) pointing at no heading: {broken}"


# What CI runs, and the text that proves each one is INVOKED - one needle per
# side, because the two files spell the same call differently: the workflow
# runs `mypy`, check.sh runs `"$PYTHON" -m mypy`. The needles have to be that
# precise on BOTH sides: check.sh prints a banner per gate, so `mypy` alone
# would match `echo "== mypy =="` with the call deleted, and the workflow names
# its jobs after their tool, so the same word matches `name: mypy` in a job
# that runs nothing.
TOOL_INVOCATIONS = {
    "ruff check": ("run: ruff check", "ruff check"),
    "ruff format --check": ("run: ruff format --check", "ruff format --check"),
    "mypy": ("run: mypy", "-m mypy"),
    "pip-audit": (
        "run: pip-audit -r requirements.txt",
        "-m pip_audit -r requirements.txt",
    ),
    "gitleaks": ("uses: gitleaks/gitleaks-action", "gitleaks detect"),
    "pytest": ("run: pytest tests/", "-m pytest tests/"),
    "check_min_ha.py": (
        "run: python .github/scripts/check_min_ha.py",
        "check_min_ha.py",
    ),
    "mutate.py": ("mutate.py .github/mutations", "mutate.py .github/mutations"),
}

WORKFLOW = REPO / ".github" / "workflows" / "test.yaml"
CHECK = REPO / ".github" / "scripts" / "check.sh"


def _tools_ci_runs_and_the_local_check_does_not(workflow: str, check: str) -> set:
    in_ci = {
        name
        for name, (in_workflow, _) in TOOL_INVOCATIONS.items()
        if in_workflow in workflow
    }
    return {name for name in in_ci if TOOL_INVOCATIONS[name][1] not in check}


def test_every_tool_is_recognised_on_the_ci_side_too():
    """A needle that no longer matches the workflow empties the comparison
    instead of failing it - the guard reports nothing missing because it is
    looking for nothing."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    unseen = [
        name
        for name, (in_workflow, _) in TOOL_INVOCATIONS.items()
        if in_workflow not in workflow
    ]

    assert not unseen, (
        f"{unseen} are no longer recognised in the workflow, so the check "
        "below silently stops asking about them."
    )


def test_the_local_check_runs_every_tool_ci_runs():
    """A tool added to CI and forgotten in check.sh turns the local run into a
    claim it cannot back."""
    missing_locally = _tools_ci_runs_and_the_local_check_does_not(
        WORKFLOW.read_text(encoding="utf-8"), CHECK.read_text(encoding="utf-8")
    )

    assert not missing_locally, (
        f"CI runs {missing_locally} but .github/scripts/check.sh does not. "
        "Add it there too, or the local run promises more than it checks."
    )


def test_the_comparison_is_not_satisfied_by_a_banner():
    """The shape it exists to catch: a check.sh that still ANNOUNCES the gate
    but no longer runs it."""
    banner_only = '#!/bin/sh\necho "== mypy =="\necho "== ruff =="\n'

    assert _tools_ci_runs_and_the_local_check_does_not(
        "        run: mypy\n", banner_only
    ) == {"mypy"}


def _scans_the_package(source: str) -> bool:
    """Whether `source` walks the package's modules, however it spells it.

    Asked of the CALL, not of the variable in front of it: matching the text
    "PACKAGE.rglob" would go blind the moment a guard binds its path to a
    different name.
    """
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("glob", "rglob")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            # Exactly "*.py" - every module. `test_*.py` is a scan of the test
            # directory, which several files here do and which says nothing
            # about the package.
            and node.args[0].value == "*.py"
        ):
            return True
    return False


def test_the_index_scan_is_not_pinned_to_a_variable_name():
    assert _scans_the_package(
        'for module in sorted(package.rglob("*.py")):\n    pass\n'
    )
    assert _scans_the_package('PACKAGE.glob("*.py")')
    assert not _scans_the_package('TESTS.glob("test_*.py")'), (
        "a walk of the test directory is not a scan of the package"
    )
    assert not _scans_the_package("PACKAGE.read_text()")


def test_a_new_package_wide_scan_is_added_to_the_index():
    """A guard nobody lists is a guard nobody knows to keep."""
    scanning = {
        test_file.name
        for test_file in sorted(TESTS.glob("test_*.py"))
        if _scans_the_package(test_file.read_text(encoding="utf-8"))
    }

    unlisted = scanning - set(GUARD_FILES)
    assert not unlisted, (
        f"{unlisted} scan(s) the package but are not in GUARD_FILES - add a "
        "line saying what each one holds."
    )
