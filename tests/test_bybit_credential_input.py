import pytest

from notify.telegram import parse_bybit_credentials


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("abcdefgh,12345678", ("abcdefgh", "12345678")),
        ("  abcdefgh  ,  12345678  ", ("abcdefgh", "12345678")),
    ],
)
def test_parse_bybit_credentials_accepts_exact_two_parts(raw, expected):
    assert parse_bybit_credentials(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abcdefgh",
        "abcdefgh,",
        ",12345678",
        "short,12345678",
        "abcdefgh,short",
        "abcdefgh,12345678,extra",
    ],
)
def test_parse_bybit_credentials_rejects_unsafe_or_ambiguous_format(raw):
    with pytest.raises(ValueError, match="credential_format"):
        parse_bybit_credentials(raw)
