#!/bin/sh
# Every gate this repository ships, in one command.
#
# Run this before calling a change done; CI runs the same set, and a guard in
# tests/test_guards.py fails if the two ever drift apart. The order is cheap to
# expensive, stopping at the first failure.
#
# Anything machine-local arrives through the environment, never as a path in
# this file:
#
#     PYTHON=/path/to/venv/bin/python .github/scripts/check.sh

set -e

PYTHON="${PYTHON:-python}"

echo "== ruff =="
"$PYTHON" -m ruff check .

echo "== format =="
"$PYTHON" -m ruff format --check .

echo "== mypy =="
"$PYTHON" -m mypy

echo "== pip-audit =="
"$PYTHON" -m pip_audit -r requirements.txt --strict

# Every commit this fork added, fail-closed: a missing scanner is a failed
# gate, not a skipped one - this script says "all gates passed" and has to
# mean it.
#
# Not the whole history. Everything below FORK_BASE is the upstream project
# as it was adopted: immutable, already scanned once, and its findings are
# not ours to fix. Listing them here to keep the scan green would mean
# shipping a list of another project's credential locations. Commits that
# arrive later through a merge from upstream are NOT below the base and are
# scanned like any other.
#
# The last commit of the upstream fronius_modbus project before this fork.
FORK_BASE=eafca35aa5f5caf01f9952d4f29dbdb9648a7a18
echo "== gitleaks =="
if ! command -v gitleaks >/dev/null 2>&1; then
    echo "gitleaks is not installed; install it (apt/brew/winget) - the secret gate cannot be skipped" >&2
    exit 1
fi
# Two versions judged the same commits differently (8.16 clean, 8.30 thirteen
# findings): the allowlist is written for the match shape of 8.30, so an
# older scanner is not a weaker gate but a different one - refuse it.
GITLEAKS_MIN=8.30
GITLEAKS_FOUND=$(gitleaks version 2>/dev/null | grep -oE "[0-9]+\.[0-9]+" | head -1)
if [ -z "$GITLEAKS_FOUND" ] || [ "$(printf '%s\n' "$GITLEAKS_MIN" "$GITLEAKS_FOUND" | sort -V | head -1)" != "$GITLEAKS_MIN" ]; then
    echo "gitleaks ${GITLEAKS_FOUND:-of unknown version} found, $GITLEAKS_MIN or newer required (the Ubuntu package is older; put a current binary first in PATH)" >&2
    exit 1
fi
echo "gitleaks $GITLEAKS_FOUND"
gitleaks detect --no-banner --redact --source . --log-opts="$FORK_BASE..HEAD"

echo "== pytest =="
# -m "" cancels the `-m "not e2e"` default from pyproject.toml, so the slow
# end-to-end tests against a real Home Assistant run here too.
# --cov-fail-under: 73 % when the gate was adopted (2026-09-05), 89 % once the
# web client and repair flows were covered, 90 % after the audit fixes of 2026-09-06.
# web client and the repair flows were covered - the config flow is what is
# left. It is a floor, not a target: raise it when coverage rises, never lower
# it to get past a red run.
"$PYTHON" -m pytest tests/ -q -m "" --cov=custom_components/fronius_modbus --cov-report=term:skip-covered --cov-fail-under=90

# The second Home Assistant version is optional because its interpreter
# lives wherever you put it:
#
#     MIN_HA_PYTHON=/path/to/min-ha-venv/bin/python .github/scripts/check.sh
#
# Without it the minimum-version run is SKIPPED and says so - a skipped gate
# that announces itself is honest; one that passes silently is not.
MINIMUM_RUN="skipped"
if [ -n "$MIN_HA_PYTHON" ]; then
    MINIMUM_RUN="passed"
    # Which Home Assistant that interpreter actually holds, before spending a
    # full suite on it: a local venv is pinned by hand and ages quietly.
    echo "== minimum Home Assistant version =="
    "$MIN_HA_PYTHON" .github/scripts/check_min_ha.py

    echo "== pytest (minimum Home Assistant) =="
    "$MIN_HA_PYTHON" -m pytest tests/ -q -m ""
else
    echo "== pytest (minimum Home Assistant): SKIPPED, set MIN_HA_PYTHON =="
fi

echo "== mutations =="
"$PYTHON" .github/scripts/mutate.py .github/mutations/plan.json

echo
if [ "$MINIMUM_RUN" = "passed" ]; then
    echo "all gates passed"
else
    echo "all gates passed EXCEPT the minimum Home Assistant run, which was skipped"
fi
