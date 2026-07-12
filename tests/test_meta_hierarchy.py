from core.db import UserDB


def test_meta_hierarchy_has_specific_and_aggregate_levels():
    assert UserDB.meta_pattern_hierarchy("scalper_BTC_long_s72p") == [
        ("specific", "scalper_BTC_long_s72p"),
        ("asset_side", "scalper_BTC_long"),
        ("side_bucket", "scalper_long_s72p"),
        ("side", "scalper_long"),
    ]


def test_meta_hierarchy_preserves_asset_with_underscore():
    assert UserDB.meta_pattern_hierarchy("scalper_kPEPE_long_s60_64") == [
        ("specific", "scalper_kPEPE_long_s60_64"),
        ("asset_side", "scalper_kPEPE_long"),
        ("side_bucket", "scalper_long_s60_64"),
        ("side", "scalper_long"),
    ]
