from pathlib import Path


def test_paper_retention_dependency_contract():
    root = Path(__file__).resolve().parents[1]
    assert "class PaperExecutor" in (root / "execution" / "paper_executor.py").read_text(encoding="utf-8")
    telegram = (root / "notify" / "telegram.py").read_text(encoding="utf-8")
    assert "cmd_paper" in telegram
    assert "PaperExecutor" in (root / "core" / "user_session.py").read_text(encoding="utf-8")
