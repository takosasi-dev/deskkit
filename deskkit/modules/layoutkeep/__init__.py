# LayoutKeep: ウィンドウ配置をモニタ構成ごとに保存し、崩れた配置をワンアクションで戻す DeskKit モジュール。
# 入口は create(ctx) だけ。重い import(PySide6 の画面・ctypes)は create の中で行う。
from __future__ import annotations

from typing import Any


def create(ctx: Any) -> Any:
    from .module import LayoutKeepModule

    return LayoutKeepModule(ctx)
