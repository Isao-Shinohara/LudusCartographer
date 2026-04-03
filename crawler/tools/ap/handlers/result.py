"""
ap/handlers/result.py — Result / ガチャ結果画面ハンドラ

handle_result_screen: RAPID (pre-OCR グロー即タップ) / OCR (フル解析) 両モード対応。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H
from tools.ap.device import tap_device
from tools.ap.helpers import has_text, watchdog_recover
from tools.ap.image_proc import detect_guide_glow, roi_to_device
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")

# ─── Result画面定数 ──────────────────────────────
_RESULT_NEXT_X_RATIO = 0.785
_RESULT_NEXT_Y_RATIO = 0.914

# パーティ編成画面の除外キーワード (Lv.1 が出るが Result ではない)
_FORMATION_KWS = ["パーティ", "編成", "キオク", "ポートレイト", "自動編成"]


def _is_result_screen(ocr: list, texts: list[str]) -> tuple[bool, str]:
    """Result画面判定。戻り値: (is_result, subtype)
    subtype: "GACHA" | "BATTLE" | ""
    """
    # 除外: パーティ編成画面
    if any(kw in t for kw in _FORMATION_KWS for t in texts):
        return False, ""
    # ガチャ結果: NEW×3 以上 (10連結果一覧)
    new_count = sum(1 for t in texts if t == "NEW")
    if new_count >= 3:
        return True, "GACHA"
    # ガチャ結果: 1枚表示 (キャラ紹介画面)
    # SKIP が右上にある + ★ が左下にある + バトルKWなし
    _has_skip = any("SKIP" in t for t in texts)
    _has_star = any("★" in t for t in texts)
    _has_battle = any(kw in t for kw in ("通常攻撃", "BREAK", "WAVE", "Turn", "AUTO") for t in texts)
    if _has_skip and _has_star and not _has_battle:
        return True, "GACHA"
    # バトルResult: Result / EXP / Lv.1 / リザルト
    if (has_text(ocr, "Result") or has_text(ocr, "EXP")
            or has_text(ocr, "Lv.1") or has_text(ocr, "リザルト")):
        return True, "BATTLE"
    return False, ""


def _find_next_button(ocr: list, W: int, H: int, subtype: str) -> Optional[dict]:
    """Result画面の進行ボタンを検索。位置フィルタ付き。"""
    if subtype == "GACHA":
        return has_text(ocr, "OK", min_conf=0.5)
    # BATTLE: 右下 (y>60%, x>50%) の「次へ」/「NEXT」
    for item in ocr:
        txt = item.get("text", "")
        if "次へ" in txt or "NEXT" in txt:
            cx, cy = item["center"]
            if cy > H * 0.6 and cx > W * 0.5:
                return item
    return None


def handle_result_screen(
    state: PilotState,
    analysis_path: Optional[Path],
    ocr: list,
    dist: int,
    mode: str,
) -> Optional[tuple[str, float]]:
    """Result/ガチャ結果画面の統一ハンドラ。

    mode="RAPID": pre-OCR グロー検知即タップ (main loop から呼出)
    mode="OCR":   フル OCR 解析後の判定 (detect_and_act から呼出)

    Returns: (action_name, wait_sec) or None (非Result / 条件不一致)
    """
    W, H = ANALYSIS_W, ANALYSIS_H

    # ── RAPID モード ──
    if mode == "RAPID":
        _rapid_ok = (
            state.last_action in ("RESULT_TAP", "RESULT_NEXT", "RESULT_RAPID",
                                  "GACHA_OK")
            and analysis_path is not None
            and dist <= 30
            and state.result_rapid_count < 8
        )
        if not _rapid_ok:
            return None

        _result_glows = detect_guide_glow(
            analysis_path, W, H, footer_ratio=0.10)
        # 右側グロー (x > 60%) を優先 — ボタンは画面右側に集中
        _right_glows = [g for g in _result_glows
                        if g["cx"] > W * 0.60]
        if _right_glows:
            _rg = max(_right_glows, key=lambda g: g["area"])
            _rgx, _rgy = _rg["cx"], _rg["cy"]
            logger.info("[RESULT_RAPID] right glow(%d,%d) → 即タップ",
                        _rgx, _rgy)
            tap_device(_rgx, _rgy, state, "RESULT_RAPID")
        else:
            # 右側グローなし → Result 画面が実在する証拠がない
            # 画面遷移済みの可能性があるため OCR フルパスで正確に判定する
            logger.info("[RESULT_RAPID] no right glow → OCR フォールスルー")
            return None

        state.result_rapid_count += 1
        state.result_total_taps += 1

        # 累積 30 タップで Unity 入力フリーズ復旧
        if state.result_total_taps >= 30:
            logger.warning(
                "[RESULT_FREEZE] RESULT_RAPID %d回 — Unity入力フリーズ → force-stop",
                state.result_total_taps)
            state.result_total_taps = 0
            state.result_rapid_count = 0
            watchdog_recover(state)
            return "RESULT_FREEZE", 0.0

        return "RESULT_RAPID", 1.0

    # ── OCR モード ──
    texts = [r.get("text", "") for r in ocr]
    is_result, subtype = _is_result_screen(ocr, texts)
    if not is_result:
        return None

    state.result_subtype = subtype
    btn = _find_next_button(ocr, W, H, subtype)

    if subtype == "GACHA":
        logger.info("  ガチャ結果画面検出 (subtype=%s) → ハンドラ処理", subtype)
        if btn:
            cx, cy = btn["center"]
            logger.info(">>> 【ガチャ結果】 OK (%d,%d) → ダブルタップ", cx, cy)
            tap_device(cx, cy, state, "GACHA_RESULT_OK_1", rapid=True)
            tap_device(cx, cy, state, "GACHA_RESULT_OK_2")
        else:
            _gc_x, _gc_y = roi_to_device(
                int(W * 0.5), int(H * 0.5), state.game_roi)
            logger.info(">>> 【ガチャ結果初期】 OK未検出 → 画面中央ダブルタップ (%d,%d)",
                        _gc_x, _gc_y)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_1",
                       rapid=True)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_2")
        state.result_total_taps += 1
        return "GACHA_OK", 1.0

    # BATTLE subtype
    if btn:
        _nx, _ny = btn["center"]
        logger.info(">>> 【バトルResult】 次へ (%d,%d) タップ", _nx, _ny)
        tap_device(_nx, _ny, state, "RESULT_NEXT")
    else:
        _nx, _ny = roi_to_device(
            int(W * _RESULT_NEXT_X_RATIO),
            int(H * _RESULT_NEXT_Y_RATIO), state.game_roi)
        logger.info(">>> 【バトルResult】 次へ未検出 → 想定位置 (%d,%d) タップ",
                    _nx, _ny)
        tap_device(_nx, _ny, state, "RESULT_NEXT")
    state.result_total_taps += 1
    return "RESULT_TAP", 1.0
