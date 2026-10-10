"""The secret scanner still catches a token next to something it is told to ignore.

The allowlist in .gitleaks.toml exists because the generic-api-key rule reads
every `translation_key="..."` in the entity table as a credential. Matched
against the whole LINE, that exception also hid a real token that sat on the
same line (reproduced with a synthetic key). It is matched
against the rule's match now - and this test is the proof that a second token
on the line is still reported, and that a plain translation key is not.

Needs the gitleaks binary; the pre-push hook and check.sh need it anyway.
"""

import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CONFIG = REPO / ".gitleaks.toml"

# Synthetic, and assembled rather than written out: a literal that looks
# like a credential makes the scanner this test exercises report the test
# itself, in every scan of the history from here on. Neither string was
# ever issued to anyone.
# AKIA and 16 more characters is what the AWS access-key rule matches.
# Mixed characters on purpose: gitleaks 8.30 drops a low-entropy AKIAQ7Q7... as
# not a credential, and the test then proved nothing.
SYNTHETIC_TOKEN = "AKIA" + "J7Q2X9M4" + "T6B3W8N5"
# 32 lowercase alphanumerics with no underscore: the shape the old allowlist
# still exempted when it sat in a bare `key="..."`.
SYNTHETIC_BARE = "7j4g9q2m" + "8z6n3p5r" + "1v0x9s8t" + "7w6y5h4k"
# 40 hex characters is the shape the generic-api-key rule reads as a token.
SYNTHETIC_HEX = "9f8e7d6c5b4a3f2e" + "1d0c9b8a7f6e5d4c" + "3b2a1f0e"
LEAK_FOUND = 2  # gitleaks' exit code for "findings" with --exit-code 2


def _scan(tmp_path: pathlib.Path, source: str) -> int:
    (tmp_path / "table.py").write_text(source, encoding="utf-8")
    result = subprocess.run(
        [
            "gitleaks",
            "detect",
            "--no-git",
            "--no-banner",
            "--redact",
            "--config",
            str(CONFIG),
            "--source",
            str(tmp_path),
            "--exit-code",
            str(LEAK_FOUND),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode


@pytest.fixture(autouse=True)
def _gitleaks_installed():
    if shutil.which("gitleaks") is None:
        # In CI a skip read as green while the allowlist went unguarded (audit F30).
        if os.environ.get("CI"):
            pytest.fail("gitleaks is missing in CI; the allowlist goes unguarded")
        pytest.skip(
            "gitleaks is not installed here; check.sh and the pre-push hook require it"
        )


def test_a_translation_key_alone_is_not_a_finding(tmp_path):
    assert _scan(tmp_path, '_sensor("soc", translation_key="soc_minimum")\n') == 0


def test_a_token_next_to_a_translation_key_is_still_found(tmp_path):
    line = f'_sensor(translation_key="soc_minimum", token="{SYNTHETIC_TOKEN}")\n'
    assert _scan(tmp_path, line) == LEAK_FOUND


def test_the_high_entropy_table_keys_are_not_findings(tmp_path):
    """The shape gitleaks 8.30 flags on a push: long keys with a letter beyond
    f, matched as translation_key=... as a whole."""
    rows = "\n".join(
        f'    key="{key}",\n    translation_key="{key}",'
        for key in (
            "export_soft_limit",
            "api_battery_power",
            "reset_modbus_control",
            "ac_limit_rate",
        )
    )
    assert _scan(tmp_path, rows + "\n") == 0


def test_a_bare_alphanumeric_key_outside_the_table_is_still_found(tmp_path):
    """A lowercase credential with a letter beyond f passed the old allowlist,
    which only ruled out hex. Translation keys have underscores or are short."""
    assert _scan(tmp_path, f'key="{SYNTHETIC_BARE}"\n') == LEAK_FOUND


def test_every_table_key_fits_the_allowlist():
    """A new translation key of the wrong shape would light up the scanner on
    the next push; better to hear it here."""
    table = (REPO / "custom_components" / "fronius_modbus" / "entities.py").read_text(
        encoding="utf-8"
    )
    allowlist = re.search(r"'''(\^.*)'''", CONFIG.read_text(encoding="utf-8")).group(1)
    keys = set(re.findall(r'(?:translation_)?key="([a-z0-9_]+)"', table))
    assert keys, "no entity keys found at all, so this proves nothing"

    assert not [key for key in keys if not re.match(allowlist, f'key="{key}"')]
    assert not [
        key for key in keys if not re.match(allowlist, f'translation_key="{key}"')
    ], "gitleaks 8.30 hands the allowlist the whole translation_key=... match"


def test_a_hex_token_in_the_key_position_is_still_found(tmp_path):
    """A hex string has no letter beyond f and no underscore - the allowlist
    must not take it for a translation key."""
    line = f'api_key="{SYNTHETIC_HEX}"\n'
    assert _scan(tmp_path, line) == LEAK_FOUND
