"""
ap/handlers/finger_priority.py — 指アイコン+金枠 最優先ハンドラ

CLAUDE.md §0 優先度1: 指差し・ハイライトは即タップ。
Phase 1 (共通ガード) の直後、他の全ハンドラより先に実行する。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.constants import ANALYSIS_W, ANALYSIS_H
from tools.ap.device import tap_device
from tools.ap.image_proc import (
    ASSET_MANAGER,
    roi_to_device,
    find_gold_frame_by_template,
    detect_white_hand_pointer,
    smart_tap_button,
)

logger = logging.getLogger("auto_pilot")


def handle_finger_priority(
    ctx: DetectContext, state: PilotState,
) -> Optional[tuple[str, float]]:
    """金枠ハイライト+指アイコンの最優先検出。

    1. 金枠単独検出: 金枠が出ていればそこしかタップ不可 (指テンプレ不要)
    2. 指テンプレ検出: 金枠なしでも指アイコンがあれば近傍を探索してタップ

    CLAUDE.md §0: 指差し・ハイライトは OCR テキスト解析より優先。
    ゲーム仕様: 金枠が出ている時はそこしかタップ不可。
    """
    analysis_path = ctx.analysis_path
    if analysis_path is None:
        return None

    texts = ctx.texts
    ocr = ctx.ocr
    W, H = ctx.W, ctx.H

    # 【最優先】金枠テンプレマッチ検出 (指テンプレ不要)
    _gold = find_gold_frame_by_template(analysis_path)
    if _gold:
        _gx, _gy, _gw, _gh = _gold
        logger.info("[FINGER_PRIORITY] 金枠検出(%d,%d %dx%d) → タップ", _gx, _gy, _gw, _gh)
        tap_device(_gx, _gy, state, "GOLD_FRAME_TAP")
        return "GOLD_FRAME_TAP", 1.0

    # 指テンプレ回転マッチ (金枠との共検出が必須)
    # TM_CCORR_NORMED+mask は白い形状に偽陽性が出るため、指テンプレ単独ではタップしない
    _finger_match = ASSET_MANAGER.match_finger_rotated(analysis_path)
    if not _finger_match or _finger_match[2] < 0.70:
        return None

    _f_cx, _f_cy, _f_score, _f_dir = (
        _finger_match[0], _finger_match[1], _finger_match[2],
        _finger_match[3] if len(_finger_match) > 3 else "",
    )

    # プレゼントボックス画面: アイテムありなら一括受取 (金枠不要)
    if any("プレゼント" in t or "プレセント" in t for t in texts):
        _no_items = any("受け取れるアイテム" in t for t in texts)
        if not _no_items:
            _bulk_x, _bulk_y = roi_to_device(int(W * 0.89), int(H * 0.92), state.game_roi)
            logger.info("[FINGER_PRIORITY] プレゼントボックス → 一括受取 (%d,%d)", _bulk_x, _bulk_y)
            tap_device(_bulk_x, _bulk_y, state, "PRESENT_BULK_RECEIVE")
            return "PRESENT_BULK_RECEIVE", 2.0

    # 金枠との共検出: 指テンプレ + 金枠の両方が検出された場合のみタップ
    _gold2 = find_gold_frame_by_template(analysis_path)
    if not _gold2:
        logger.debug("[FINGER_PRIORITY] 指(%.2f,%s)(%d,%d) 検出だが金枠なし → スキップ",
                     _f_score, _f_dir, _f_cx, _f_cy)
        return None

    _gx, _gy = _gold2[0], _gold2[1]
    logger.info("[FINGER_PRIORITY] 指(%.2f,%s)(%d,%d) + 金枠(%d,%d) → タップ",
                _f_score, _f_dir, _f_cx, _f_cy, _gx, _gy)
    tap_device(_gx, _gy, state, "FINGER_PRIORITY_TAP")
    return "FINGER_PRIORITY_TAP", 1.0
