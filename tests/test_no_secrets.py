"""Fail the suite if anything that looks like a real key is in a file git tracks."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),          # Anthropic
    re.compile(r"\bcd_[a-f0-9]{20,}\b"),                 # Crustdata
    re.compile(r"\brzp_(?:test|live)_[A-Za-z0-9]{6,}"),  # Razorpay key id
    re.compile(r"RAZORPAY_KEY_SECRET\s*=\s*\S+"),        # any non-empty Razorpay secret
]


def tracked_files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [ROOT / p for p in out.splitlines() if p]


def test_no_keys_in_tracked_files():
    hits = []
    for p in tracked_files():
        if p.suffix in {".png", ".jpg", ".pdf", ".db"} or not p.is_file():
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for pat in PATTERNS:
            if pat.search(text):
                hits.append(f"{p.relative_to(ROOT)}: {pat.pattern}")
    assert not hits, "possible secrets in tracked files (move them to .env): " + "; ".join(hits)


def test_env_file_is_not_tracked():
    """.env holds real keys and must only exist on your own machine, never in git."""
    tracked = {p.relative_to(ROOT).as_posix() for p in tracked_files()}
    assert ".env" not in tracked, "remove it with: git rm --cached .env (then rotate the keys it held)"
    if tracked:
        assert ".env.example" in tracked, ".env.example (placeholders only) is the template new users copy"
