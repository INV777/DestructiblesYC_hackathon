"""The evaluation suite must stay free of wrong and unsafe answers (evals/cases.yaml)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


def test_evaluation_suite_has_no_wrong_or_unsafe_answer(capsys):
    import eval as suite
    assert suite.main([]) == 0, capsys.readouterr().out
