"""Every text file in the repository ends its lines with LF alone.

An edit written through Python's text mode on Windows turned 23 files to CRLF
on a branch: every commit then diffed as whole files, and nothing else in the
gate noticed, since Python and ruff read either ending.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".json",
    ".toml",
    ".yaml",
    ".yml",
    ".txt",
    ".sh",
    ".ps1",
    ".bat",
}
SKIPPED_DIRECTORIES = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _files_with_carriage_returns(root: pathlib.Path) -> list[str]:
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in TEXT_SUFFIXES
        and not SKIPPED_DIRECTORIES & set(path.relative_to(root).parts)
        and b"\r" in path.read_bytes()
    )


def test_the_scan_finds_a_crlf_file(tmp_path):
    """Proof-of-red: the shape it exists to reject."""
    (tmp_path / "module.py").write_bytes(b"x = 1\r\n")
    (tmp_path / "clean.md").write_bytes(b"# Title\n")

    assert _files_with_carriage_returns(tmp_path) == ["module.py"]


def test_no_text_file_has_a_carriage_return():
    assert _files_with_carriage_returns(ROOT) == []
