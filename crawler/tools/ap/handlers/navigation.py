"""
ap/handlers/navigation.py — マップ矢印 / ハイライト指示 / ストーリータップ指示

Phase 6: detect_and_act のナビゲーション系ハンドラ。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H, _MENU_SCREEN_KWS
from tools.ap.context import DetectContext
from tools.ap.device import tap_device
from tools.ap.helpers import has_text
from tools.ap.image_proc import ASSET_MANAGER, find_3d_arrow, roi_to_device
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def handle_navigation(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """マップ矢印 / ハイライト指示 / ストーリータップ指示を処理する。"""
    ocr = ctx.ocr
    joined = ctx.joined
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── 【最優先 #2-a】探索マップ 3D矢印タップ ───
    # 「矢印をタップしてください」が出ている場合、3D空間の矢印を検出してタップ
    arrow_instruction = has_text(ocr, "矢印を", min_conf=0.2)
    if arrow_instruction and analysis_path is not None:
        pos = find_3d_arrow(analysis_path)
        if pos:
            cx, cy = pos
            logger.info(">>> 【3D矢印】 探索マップ矢印 (%d,%d) 検出 → タップ", cx, cy)
            tap_device(cx, cy, state, "MAP_ARROW_TAP")
            # [Auto Save] 初回検出時にテンプレートとして保存
            if "map_arrow" not in ASSET_MANAGER._templates:
                half_w, half_h = 70, 50
                ASSET_MANAGER.save_template(
                    analysis_path,
                    max(0, cx - half_w), max(0, cy - half_h),
                    min(W, cx + half_w), min(H, cy + half_h),
                    name="map_arrow", action="MAP_ARROW_TAP",
                    threshold=0.65,
                    require_ocr=["矢印をタップ"],
                )
            return "MAP_ARROW_TAP", 1.0
        else:
            # 自動検出失敗 → キャラ頭上デフォルト座標
            _ma_x, _ma_y = roi_to_device(int(W * 0.5), int(H * 0.29), state.game_roi)
            logger.info(">>> 【3D矢印】 自動検出失敗 → デフォルト (%d,%d) タップ", _ma_x, _ma_y)
            tap_device(_ma_x, _ma_y, state, "MAP_ARROW_FALLBACK")
            return "MAP_ARROW_TAP", 1.0

    # ─── ガチャ/交換所等のサブ画面からの脱出 ───
    # 左上に「↩ 画面名」が表示されるサブ画面を OCR で検出し、左上の戻るボタンをタップ
    # (チュートリアル中は誤動作するため home_reached 後のみ)
    if state.home_reached and not ctx.in_battle_ctx:
        for item in ocr:
            _t = item.get("text", "")
            _c = item.get("center", (0, 0))
            # 左上 (x < 20%, y < 15%) に画面名テキストがあるか
            if _c[0] < W * 0.20 and _c[1] < H * 0.15:
                if any(kw in _t for kw in _MENU_SCREEN_KWS):
                    # 画面名の左にある戻るボタン (↩) をタップ
                    _back_x = max(int(_c[0] - W * 0.05), int(W * 0.02))
                    _back_y = _c[1]
                    logger.info(">>> 【サブ画面脱出】 左上に '%s' 検出 → 戻る (%d,%d)",
                                _t, _back_x, _back_y)
                    tap_device(_back_x, _back_y, state, "SUB_SCREEN_BACK")
                    return "SUB_SCREEN_BACK", 1.5

    return None


# ─── スタック救済: メニュー画面で操作が効かない場合の戻るボタン押下 ───
# 条件 (全て AND):
#   1. action_repeat_count >= 閾値 (同じアクションが繰り返されスタック)
#   2. 左上にメニューキーワードが OCR で検出される
#   3. icon_back テンプレートが左上領域でマッチする
_MENU_STALL_THRESHOLD = 5


def handle_menu_stall_recovery(
    ctx: DetectContext, state: PilotState,
) -> Optional[tuple[str, float]]:
    """メニュー画面でスタックした際の救済処理。

    タップしても phash/OCR に変化がない状態が続いた場合、
    左上にメニューキーワード + icon_back テンプレがあれば戻るボタンを押す。
    """
    if state.ineffective_tap_count < _MENU_STALL_THRESHOLD:
        return None
    if ctx.in_battle_ctx:
        return None

    ocr = ctx.ocr
    W, H = ctx.W, ctx.H
    analysis_path = ctx.analysis_path

    # 1. 左上にメニューキーワードがあるか
    _menu_text = None
    for item in ocr:
        _t = item.get("text", "")
        _c = item.get("center", (0, 0))
        if _c[0] < W * 0.20 and _c[1] < H * 0.15:
            if any(kw in _t for kw in _MENU_SCREEN_KWS):
                _menu_text = _t
                break
    if not _menu_text:
        return None

    # 2. icon_back テンプレが左上領域でマッチするか
    if analysis_path is None:
        return None
    _back_roi = (0, 0, int(W * 0.15), int(H * 0.20))
    _back_match = ASSET_MANAGER.match_single("icon_back", analysis_path, roi=_back_roi)
    if not _back_match or _back_match[2] < 0.60:
        return None

    _bx, _by = _back_match[0], _back_match[1]
    logger.warning(
        ">>> 【メニュースタック救済】 '%s' + icon_back(%.2f) → 戻る (%d,%d) (repeat=%d)",
        _menu_text, _back_match[2], _bx, _by, state.action_repeat_count,
    )
    tap_device(_bx, _by, state, "MENU_STALL_BACK")
    return "MENU_STALL_BACK", 1.5
