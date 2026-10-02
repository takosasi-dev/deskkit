# v0.4.1 B-2: 設定のキーが無いだけ(初回)では settings_fixed の警告を出さない。値が合わないときだけ警告する。
# どちらの場合も既定値を入れて settings.json へ書き戻す(書き戻しは今までどおり)。
from __future__ import annotations

from typing import Any

from deskkit.modules.startupwatch.fakes import sample_api
from deskkit.modules.startupwatch.module import DEFAULTS, validate, validate_detail


def test_first_run_missing_keys_are_filled_silently(make_module: Any) -> None:
    m, ctx, _ = make_module(sample_api())  # セクションは {"enabled": True} だけ(初回)
    assert "settings_fixed" not in ctx.handler.text()
    assert ctx.writes, "無いキーを既定値で書き戻していない"
    written = ctx.writes[0][0]
    for k, dv in DEFAULTS.items():
        assert written[k] == dv and m.cfg[k] == dv


def test_bad_value_warns_only_for_that_key(make_module: Any) -> None:
    _m, ctx, _ = make_module(sample_api(), section={"poll_minutes": 1})  # ほかのキーは無い
    text = ctx.handler.text()
    assert "settings_fixed key=poll_minutes" in text
    for k in DEFAULTS:
        if k != "poll_minutes":
            assert f"settings_fixed key={k}" not in text
    assert ctx.writes and ctx.writes[0][0]["poll_minutes"] == DEFAULTS["poll_minutes"]


def test_complete_valid_section_is_not_written_back(make_module: Any) -> None:
    _m, ctx, _ = make_module(sample_api(), section=dict(DEFAULTS))
    assert ctx.writes == [] and "settings_fixed" not in ctx.handler.text()


def test_validate_detail_splits_missing_and_bad() -> None:
    sec, fixed, filled = validate_detail({"poll_minutes": True, "settle_ms": 500})
    assert fixed == ["poll_minutes"]
    assert set(filled) == set(DEFAULTS) - {"poll_minutes", "settle_ms"}
    assert sec["poll_minutes"] == DEFAULTS["poll_minutes"] and sec["settle_ms"] == 500
    _sec, both = validate({"poll_minutes": True, "settle_ms": 500})
    assert set(both) == set(fixed) | set(filled)  # 従来の validate は両方を返す(書き戻しの判定用)
    _sec, fixed2, _ = validate_detail({"poll_minutes": None})  # キーはあるが値が合わない(null)
    assert fixed2 == ["poll_minutes"]
