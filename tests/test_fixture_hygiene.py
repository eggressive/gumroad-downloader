"""Fixture hygiene: the repo must never carry a live looking Gumroad purchase token.

A real ``/d/<token>`` value is a credential (the token plus the buyer email opens the
purchase), so every token shaped string under ``README.md``, ``tests/`` and
``src/gumroad_dl/`` must be the synthetic fixture token. This is the mistake the
fixtures were caught with once already: a real purchase id copied out of a working
download link.
"""

from __future__ import annotations

import re
from pathlib import Path

SYNTHETIC_TOKEN = "abcdefababcdefababcdefababcdefab"
ALLOWED = {SYNTHETIC_TOKEN, SYNTHETIC_TOKEN.upper()}
SCANNED = ["README.md"]
SCANNED_DIRS = ["tests", "src/gumroad_dl"]
TOKEN_SHAPE = re.compile(r"\b[0-9a-fA-F]{32}\b")


def scan(root: Path) -> list[str]:
    """Return every 32 hex string that is not the synthetic fixture token."""
    files = [root / name for name in SCANNED]
    for directory in SCANNED_DIRS:
        files.extend(sorted((root / directory).glob("*.py")))
    return [
        f"{path.relative_to(root)}: {found}"
        for path in files
        for found in TOKEN_SHAPE.findall(path.read_text())
        if found not in ALLOWED
    ]


def test_the_repository_carries_only_the_synthetic_token():
    root = Path(__file__).resolve().parent.parent
    files = [root / name for name in SCANNED] + [
        path for directory in SCANNED_DIRS for path in (root / directory).glob("*.py")
    ]
    assert len(files) > 5, "the guard is not looking at the repository"
    assert scan(root) == []


def test_the_guard_flags_a_token_that_is_not_the_synthetic_one(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "gumroad_dl").mkdir(parents=True)
    (tmp_path / "README.md").write_text("")
    planted = "".join(f"{n:02x}" for n in range(1, 17))
    (tmp_path / "tests" / "test_x.py").write_text(f'TOKEN = "{planted}"\n')
    assert scan(tmp_path) == [f"tests/test_x.py: {planted}"]
