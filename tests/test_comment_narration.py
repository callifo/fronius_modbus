"""Heuristic guard against narrating comments.

A comment states a decision or a constraint, not what the next line already
says - a narrating comment is noise today and doc-drift tomorrow (a comment
that describes behaviour the code no longer has). A prose rule
does not reach the moment of writing; this scan does, over the shipped package
and the test suite alike.

ponytail: heuristic with a known ceiling - it flags only the bluntest form, a
comment whose every content word already appears in the adjacent code. A
decision comment carries extra words (why, incident, limit) and passes; the
scan cannot judge meaning, so treat a hit as a prompt to reread and reword, and
exempt only a comment that genuinely earns its place.
"""

import io
import keyword
import pathlib
import re
import tokenize

PACKAGE = (
    pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "fronius_modbus"
)
TESTS = pathlib.Path(__file__).resolve().parent

WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
EXEMPT_PREFIXES = ("ponytail:", "noqa", "type:", "TODO", "FIXME", "!", "fmt:")
FILLER = set(keyword.kwlist) | {
    "the",
    "and",
    "for",
    "with",
    "this",
    "that",
    "into",
    "from",
    "set",
    "get",
    "value",
    "values",
}

# A comment the heuristic flags but that genuinely earns its place goes here as
# (filename, verbatim text).
EXEMPTIONS: set = set()


def _identifier_parts(name: str) -> set:
    parts = set(name.lower().split("_"))
    parts |= {p.lower() for p in re.findall(r"[A-Z]?[a-z0-9]+", name)}
    return parts - FILLER


def _suspects(source: str):
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    code_parts_by_line: dict = {}
    for token in tokens:
        if token.type == tokenize.NAME and token.string not in keyword.kwlist:
            code_parts_by_line.setdefault(token.start[0], set()).update(
                _identifier_parts(token.string)
            )

    found = []
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        text = token.string.lstrip("#").strip()
        if not text or text.startswith(EXEMPT_PREFIXES):
            continue
        words = {w.lower() for w in WORD.findall(text)} - FILLER
        if len(words) < 2:
            continue
        line = token.start[0]
        nearby = code_parts_by_line.get(line, set())
        for following in range(line + 1, line + 3):
            if following in code_parts_by_line:
                nearby = nearby | code_parts_by_line[following]
                break
        if words <= nearby:
            found.append((line, text))
    return found


def test_no_comment_merely_narrates_the_adjacent_code():
    offenders = []
    for source_file in sorted(PACKAGE.rglob("*.py")) + sorted(TESTS.glob("*.py")):
        for line, text in _suspects(source_file.read_text(encoding="utf-8")):
            if (source_file.name, text) in EXEMPTIONS:
                continue
            offenders.append(f"{source_file.name}:{line}: # {text}")

    assert not offenders, (
        "comment(s) that only restate the code:\n  "
        + "\n  ".join(offenders)
        + "\nSay WHY (decision, constraint, incident) or delete the comment; "
        "a genuinely earned hit goes into EXEMPTIONS with its verbatim text."
    )


def test_the_heuristic_flags_narration_and_passes_decisions():
    """Proof-of-red, both directions."""
    narration = "# read the user name\nuser_name = read_input()\n"
    assert _suspects(narration), "pure restatement must be flagged"

    decision = (
        "# expected-version write: a plain save lost concurrent edits (P1)\n"
        "rows = save(expected_version)\n"
    )
    assert not _suspects(decision), "a why-comment with extra words must pass"
