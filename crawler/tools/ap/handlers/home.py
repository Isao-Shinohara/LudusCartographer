"""
ap/handlers/home.py — ホーム画面検出 + チュートリアル完了判定

Phase 7: ホームナビテンプレマッチング、チュートリアル完了判定、
吹き出しセリフ、周回モード対応。

指+金枠タップは finger_priority (Phase 1.5) で処理済み。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.device import tap_device
from tools.ap.helpers import has_text, log_milestone
from tools.ap.image_proc import (
    ASSET_MANAGER,
    find_gold_button,
    detect_tutorial_overlay,
    roi_to_device,
)
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def _handle_grind_nav(ctx: DetectContext, state: PilotState) -> tuple[str, float]:
    """周回モード: ホーム到達 → クエストへ自動ナビゲート。"""
    state.grind_cycles_completed += 1
    logger.info("=" * 50)
    logger.info("  [GRIND] 周回 #%d 完了! → クエストへ自動ナビゲート",
                state.grind_cycles_completed)
    logger.info("=" * 50)
    if 0 < state.grind_max_cycles <= state.grind_cycles_completed:
        logger.info("[GRIND] 目標周回数 %d に到達 → 終了", state.grind_max_cycles)
        return "GOAL_GRIND_COMPLETE", 0
    state.battle_wait_count = 0
    state.auto_activated = False
    state.result_rapid_count = 0
    state.result_total_taps = 0
    state.result_subtype = ""
    state.home_nav_count = 0
    state.home_tutorial_tap_count = 0
    state.char_just_selected = False
    state.character_selected = False
    quest_btn = has_text(ctx.ocr, "クエスト", min_conf=0.3)
    if quest_btn:
        cx, cy = quest_btn["center"]
        logger.info(">>> [GRIND] クエストボタン (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "GRIND_QUEST_NAV")
    else:
        _qf_x, _qf_y = roi_to_device(int(ctx.W * 0.88), int(ctx.H * 0.96), state.game_roi)
        logger.info(">>> [GRIND] クエスト固定位置 (%d,%d) タップ", _qf_x, _qf_y)
        tap_device(_qf_x, _qf_y, state, "GRIND_QUEST_NAV_FIXED")
    return "GRIND_QUEST_NAV", 3.0


def handle_home(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """ホーム画面検出 + チュートリアル完了判定。"""
    ocr = ctx.ocr
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── ホーム画面検出 ───
    from tools.ap.image_proc import count_home_nav_templates
    home_count = count_home_nav_templates(analysis_path) if analysis_path else 0
    if home_count < 3:
        return None

    state.home_reached = True

    # 前段ハンドラ (finger_priority) で指テンプレが処理済みの場合、
    # チュートリアル証拠なしカウンタをリセット (誤完了判定防止)
    if ctx.pre_dialog_finger:
        state._home_no_evidence_count = 0

    # ── チュートリアル完了判定: 指テンプレ / 暗転+金枠 の有無 ──
    # チュートリアル中: 指アイコン or (暗転オーバーレイ + 金枠) が表示される
    # チュートリアル完了後: 指なし + 暗転なし + 金枠なし
    _has_hand = False
    if analysis_path:
        _ft_rot = ASSET_MANAGER.match_finger_rotated(analysis_path)
        if _ft_rot and _ft_rot[2] >= 0.70:
            _has_hand = True
            logger.info(">>> ホーム: finger_%s(%.2f) (%d,%d) 検出 → チュートリアル中",
                        _ft_rot[3], _ft_rot[2], _ft_rot[0], _ft_rot[1])
    _has_overlay = detect_tutorial_overlay(analysis_path) if analysis_path else False
    _home_gold = find_gold_button(analysis_path) if analysis_path else None
    # 暗転なし+指なし → 金枠は偽陽性 (カード装飾等)
    if not _has_overlay and not _has_hand:
        _home_gold = None
    _has_tutorial_evidence = _has_hand or (_has_overlay and _home_gold is not None)

    if not _has_tutorial_evidence:
        # チュートリアル完了候補
        # 1フレームの遷移瞬間で誤判定しないよう、3回連続で確認
        _no_evidence_count = getattr(state, "_home_no_evidence_count", 0) + 1
    else:
        state._home_no_evidence_count = 0

    if not _has_tutorial_evidence:
        state._home_no_evidence_count = _no_evidence_count
        if not state.tutorial_cleared and _no_evidence_count < 3:
            logger.info(">>> ホーム画面 チュートリアル証拠なし (%d/3) → 次フレームで再確認",
                        _no_evidence_count)
            return "HOME_TUTORIAL_RECHECK", 0.5
        if not state.tutorial_cleared:
            logger.info(">>> ホーム画面 暗転なし+金枠なし (3回連続) → チュートリアル完了")
            state.tutorial_cleared = True
            log_milestone(state, "HOME_REACHED")
        logger.info(">>> ホーム画面検出 (%d個) — チュートリアル完了済み", home_count)
        return "GOAL_HOME_REACHED", 0

    # ── チュートリアル中: 暗転+金枠あり ──
    # 指+金枠タップは finger_priority (Phase 1.5) で処理済みのため、
    # ここではスタック時のクエストナビゲートと吹き出しのみ処理
    state._home_no_evidence_count = 0
    logger.info(">>> ホーム画面 チュートリアル中 (暗転=%s 金枠=%s)", _has_overlay, _home_gold is not None)

    # スタック時: クエストへナビゲート
    if state.blob_same_count >= 5:
        logger.info(">>> ホーム画面 + スタック → クエストへナビゲート")
        state.blob_same_count = 0
        state.home_nav_count += 1
        quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
        if quest_btn:
            cx, cy = quest_btn["center"]
            logger.info(">>> クエストボタン (%d,%d) タップ", cx, cy)
            tap_device(cx, cy, state, "QUEST_FROM_HOME")
            return "QUEST_FROM_HOME", 3.0
        _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
        tap_device(_qf_x, _qf_y, state, "QUEST_FIXED")
        return "QUEST_FROM_HOME", 3.0

    # ── 右上吹き出しセリフチェック: まだチュートリアルガイダンス中 ──
    _BUBBLE_EXCLUDE_EXACT = {"AUTO", ">>", ">|", "D1", "×", "+", "■", "畄", "目", "SKIP"}
    _BUBBLE_EXCLUDE_SUBSTR = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora", "M.8", "M8X",
                              "AUTO", "UTO", "UT0", "AUT")
    _BUBBLE_NUM_RE = re.compile(r'^[\d,./:%+\-・\s]+$')
    _BUBBLE_ALPHANUM_NOISE_RE = re.compile(r'^[A-Za-z0-9.,\-+×★☆\s]{1,5}$')
    _auto_pos_h = None
    if analysis_path:
        _auto_mh = ASSET_MANAGER.match_single("icon_auto", analysis_path)
        if _auto_mh and _auto_mh[2] >= 0.60:
            _auto_pos_h = (_auto_mh[0], _auto_mh[1])
    _bubble_texts = [r for r in ocr
                     if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                     and r["text"] not in _BUBBLE_EXCLUDE_EXACT
                     and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR)
                     and not _BUBBLE_NUM_RE.match(r["text"])
                     and not _BUBBLE_ALPHANUM_NOISE_RE.match(r["text"])
                     and len(r["text"]) > 2
                     and not (_auto_pos_h and abs(r["center"][0] - _auto_pos_h[0]) < 50
                              and abs(r["center"][1] - _auto_pos_h[1]) < 50)]
    if _bubble_texts:
        _bt = _bubble_texts[0]
        _btx, _bty = _bt["center"]
        logger.info(">>> ホーム画面 + 吹き出しセリフ '%s' → チュートリアル継続 (%d,%d)",
                    _bt["text"][:10], _btx, _bty)
        tap_device(_btx, _bty, state, "BUBBLE_TAP")
        return "BUBBLE_TAP", 0.3

    # ── 暗転あるが指/金枠/吹き出しなし → 待機 ──
    logger.info(">>> ホーム画面 暗転あり + 操作対象なし → 待機")
    return "HOME_CLEAR_CHECK", 0.5
