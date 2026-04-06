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

    # 指テンプレ回転マッチ
    _finger_match = ASSET_MANAGER.match_finger_rotated(analysis_path)
    if not _finger_match or _finger_match[2] < 0.70:
        return None

    _f_cx, _f_cy, _f_score, _f_dir = (
        _finger_match[0], _finger_match[1], _finger_match[2],
        _finger_match[3] if len(_finger_match) > 3 else "",
    )

    # プレゼントボックス画面: アイテムありなら一括受取
    if any("プレゼント" in t or "プレセント" in t for t in texts):
        _no_items = any("受け取れるアイテム" in t for t in texts)
        if not _no_items:
            _bulk_x, _bulk_y = roi_to_device(int(W * 0.89), int(H * 0.92), state.game_roi)
            logger.info("[FINGER_PRIORITY] プレゼントボックス → 一括受取 (%d,%d)", _bulk_x, _bulk_y)
            tap_device(_bulk_x, _bulk_y, state, "PRESENT_BULK_RECEIVE")
            return "PRESENT_BULK_RECEIVE", 2.0

    # 【セカンダリ】白ハンドポインタで方向取得 → 近傍アイコン/OCR/金枠探索
    _wh = detect_white_hand_pointer(analysis_path, threshold=0.85)
    _hand_pos = (_f_cx, _f_cy)
    _hand_dir = _f_dir or ""
    if _wh:
        _hand_pos = (_wh[0], _wh[1])
        _hand_dir = _wh[3]
    _hx, _hy = _hand_pos
    tap_x, tap_y = _f_cx, _f_cy

    # テンプレートマッチで指近傍のアイコンを検索
    _tmpl_found = False
    _search_r = 200
    _aroi = (max(0, _hx - _search_r), max(0, _hy - _search_r),
             _search_r * 2, _search_r * 2)
    for _btn_name in ("icon_back",):
        _m = ASSET_MANAGER.match_single(_btn_name, analysis_path, roi=_aroi)
        if _m and _m[2] >= 0.65:
            _ax, _ay = _m[0], _m[1]
            if (_hand_dir == "up" and _ay > _hy + 30) or \
               (_hand_dir == "down" and _ay < _hy - 30):
                continue
            tap_x, tap_y = _ax, _ay
            _tmpl_found = True
            logger.info("[FINGER_PRIORITY] 指(%d,%d,dir=%s) → Asset '%s'(%d,%d) score=%.3f",
                        _hx, _hy, _hand_dir, _btn_name, tap_x, tap_y, _m[2])
            break

    # 指の方向にある最近接OCRテキストをタップ
    if not _tmpl_found:
        _ocr_found = False
        if _hand_dir and ocr:
            _dir_items = []
            for item in ocr:
                _tx, _ty = item["center"]
                _dist = abs(_hx - _tx) + abs(_hy - _ty)
                if _dist > 200:
                    continue
                if _hand_dir == "up" and _ty < _hy:
                    _dir_items.append((_tx, _ty, _dist, item["text"]))
                elif _hand_dir == "down" and _ty > _hy:
                    _dir_items.append((_tx, _ty, _dist, item["text"]))
                elif _hand_dir == "right" and _tx > _hx:
                    _dir_items.append((_tx, _ty, _dist, item["text"]))
                elif _hand_dir == "left" and _tx < _hx:
                    _dir_items.append((_tx, _ty, _dist, item["text"]))
            if _dir_items:
                _dir_items.sort(key=lambda d: d[2])
                tap_x, tap_y = _dir_items[0][0], _dir_items[0][1]
                _ocr_found = True
                logger.info("[FINGER_PRIORITY] 指(%d,%d,dir=%s) → OCR '%s'(%d,%d) dist=%d",
                            _hx, _hy, _hand_dir, _dir_items[0][3], tap_x, tap_y, _dir_items[0][2])

        # フォールバック: 金枠テンプレマッチ検出
        if not _ocr_found:
            _gold2 = find_gold_frame_by_template(analysis_path)
            if _gold2:
                _gx, _gy = _gold2[0], _gold2[1]
                if (_hand_dir == "up" and _gy > _hy + 30) or \
                   (_hand_dir == "down" and _gy < _hy - 30):
                    _gold2 = None
            if _gold2:
                tap_x, tap_y = _gold2[0], _gold2[1]
                logger.info("[FINGER_PRIORITY] 指(%d,%d,dir=%s) → 金枠(%d,%d)",
                            _hx, _hy, _hand_dir, tap_x, tap_y)
            else:
                tap_x, tap_y = smart_tap_button(
                    analysis_path, _hx, _hy, search_r=160, ocr_items=ocr)
                logger.info("[FINGER_PRIORITY] 指(%d,%d,dir=%s) → smart_tap(%d,%d)",
                            _hx, _hy, _hand_dir, tap_x, tap_y)

    tap_device(tap_x, tap_y, state, "FINGER_PRIORITY_TAP")
    return "FINGER_PRIORITY_TAP", 1.0
