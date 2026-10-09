"""Fixture hygiene: the repo must never carry a live looking Gumroad purchase token.

A real ``/d/<token>`` value is a credential (the token plus the buyer email opens the
purchase), so every token shaped string in a tracked file must be the synthetic fixture
token, which is also low entropy enough to keep entropy based scanners such as gitleaks
and GitGuardian quiet. This is the mistake this repository was caught with once already:
a real purchase id copied out of a working download link.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SYNTHETIC_TOKEN = "abcdefababcdefababcdefababcdefab"
ALLOWED = {SYNTHETIC_TOKEN, SYNTHETIC_TOKEN.upper()}
TOKEN_SHAPE = re.compile(r"\b[0-9a-fA-F]{32}\b")
FALLBACK_FILES = ["README.md", "pyproject.toml", "LICENSE", ".gitignore"]
FALLBACK_DIRS = ["tests", "src/gumroad_dl", ".github", ".githooks"]


def tracked_files(root: Path, use_git: bool = True) -> list[Path]:
    """Every file git tracks, or a fixed set of paths when git cannot be asked."""
    if use_git:
        try:
            listed = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z"],
                capture_output=True,
                check=True,
            ).stdout.decode()
            return [root / name for name in listed.split("\0") if name]
        except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
            pass
    names = list(FALLBACK_FILES)
    names += [
        str(path.relative_to(root))
        for directory in FALLBACK_DIRS
        for path in sorted((root / directory).rglob("*"))
        if path.is_file()
    ]
    return [root / name for name in names]


def scan(root: Path, use_git: bool = True) -> list[str]:
    """Return every 32 hex string in a tracked file that is not the synthetic token."""
    findings = []
    for path in tracked_files(root, use_git):
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(
            f"{path.relative_to(root)}: {found}"
            for found in TOKEN_SHAPE.findall(text)
            if found not in ALLOWED
        )
    return findings


def test_the_repository_carries_only_the_synthetic_token():
    root = Path(__file__).resolve().parent.parent
    files = tracked_files(root)
    assert len(files) > 5, "the guard is not looking at the repository"
    assert scan(root) == []


def test_the_guard_flags_a_token_that_is_not_the_synthetic_one(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "README.md").write_text("")
    planted = "".join(f"{n:02x}" for n in range(1, 17))
    (tmp_path / "tests" / "test_x.py").write_text(f'TOKEN = "{planted}"\n')
    assert scan(tmp_path, use_git=False) == [f"tests/test_x.py: {planted}"]
