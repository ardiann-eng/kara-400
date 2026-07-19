from pathlib import Path

import pytest


def test_pnl_card_uses_pnl_usd_for_exit_reason_color():
    source = (Path(__file__).parents[1] / "notify" / "pnl_card.py").read_text(
        encoding="utf-8"
    )

    assert "_exit_reason_color(exit_reason, pnl_usd)" in source


def test_pnl_card_generates_png_for_profitable_full_close():
    pytest.importorskip("PIL")
    from notify.pnl_card import generate_pnl_card
    image = generate_pnl_card(
        asset="PENGU",
        side="long",
        entry_price=0.01,
        exit_price=0.011,
        pnl_usd=13.40,
        pnl_pct=0.10,
        exit_reason="manual",
        hold_minutes=12,
        leverage=10,
        score=70,
        session_pnl=13.40,
        session_pnl_pct=0.10,
        total_equity=100,
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) > 1000


def test_pnl_card_generates_png_for_losing_full_close():
    pytest.importorskip("PIL")
    from notify.pnl_card import generate_pnl_card
    image = generate_pnl_card(
        asset="PENGU", side="short", entry_price=0.011, exit_price=0.012,
        pnl_usd=-13.40, pnl_pct=-0.10, exit_reason="stop_loss",
        hold_minutes=12, leverage=10, score=70, session_pnl=-13.40,
        session_pnl_pct=-0.10, total_equity=86.60,
    )

    assert image.startswith(b"\x89PNG\r\n\x1a\n")
