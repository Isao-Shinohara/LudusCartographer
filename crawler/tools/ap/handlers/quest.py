"""
ap/handlers/quest.py — クエスト早期検出ハンドラ

MAIN STORY 画面、Result 画面早期検出、クエスト詳細画面(挑戦ボタン)を処理する。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.device import tap_device
from tools.ap.helpers import has_any, has_text
from tools.ap.image_proc import ASSET_MANAGER

logger = logging.getLogger("auto_pilot")


def handle_quest_early(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """クエスト関連 UI の早期検出。

    - MAIN STORY 画面: クエストカードタップ
    - Result 画面: 「次へ」ボタンタップ
    - クエスト詳細画面: 「挑戦」ボタンタップ
    """
    texts = ctx.texts
    joined = ctx.joined
    ocr = ctx.ocr
    W = ctx.W
    H = ctx.H

    # ─── 【最優先 #-1b】MAIN STORY 画面 ───
    # (A) クエスト選択画面: 「NEW」+「推奨」+「Main」→ クエストカードをタップ
    # (B) ローディング背景: タイトル後の非インタラクティブ画面 → 自動遷移待ち
    _is_main_story_bg = (
        any("MAIN" in t or "Main" in t for t in texts) and
        any("推奨" in t or "STORY" in t for t in texts) and
        not any(kw in joined for kw in ["クエスト", "ショップ", "ガチャ", "ガシャ", "光の間", "パーティ"])
    )
    if _is_main_story_bg:
        # クエスト選択画面 — "Main" カードをタップ (NEW バッジの有無は問わない)
        _quest_hit = has_text(ocr, "Main", min_conf=0.2)
        if _quest_hit:
            _qx, _qy = _quest_hit["center"]
            logger.info(">>> MAIN STORY クエスト選択 — 'Main' カードタップ (%d,%d)", _qx, _qy)
            tap_device(_qx, _qy, state, "MAIN_STORY_QUEST_TAP")
            return "MAIN_STORY_QUEST_TAP", 2.0
        # フォールバック: 画面下部中央をタップ
        _qx, _qy = int(W * 0.5), int(H * 0.85)
        logger.info(">>> MAIN STORY クエスト選択 — フォールバックタップ (%d,%d)", _qx, _qy)
        tap_device(_qx, _qy, state, "MAIN_STORY_QUEST_FB")
        return "MAIN_STORY_QUEST_FB", 2.0

    # ─── 【最優先 #-1b2】Result画面 — 「次へ」ボタンタップ ───
    # "Result" + "次へ" が見えたら即タップ (SWIPE_UP 誤マッチ防止)
    _is_result_early = any("Result" in t for t in texts)
    if _is_result_early:
        state.result_total_taps += 1
        # 30タップ超えても座標ズレの可能性が高い — ログのみ出して継続
        if state.result_total_taps >= 30 and state.result_total_taps % 30 == 0:
            logger.warning("[RESULT_STALL] RESULT_NEXT_EARLY %d回 — 座標ズレの可能性 (force-stop しない)",
                           state.result_total_taps)
        _next_btn = has_text(ocr, "次へ", min_conf=0.3)
        if _next_btn:
            _nx, _ny = _next_btn["center"]
            logger.info(">>> Result画面 — '次へ'(%d,%d) タップ", _nx, _ny)
            tap_device(_nx, _ny, state, "RESULT_NEXT_EARLY")
            return "RESULT_NEXT_EARLY", 1.5

    # ─── 【最優先 #-1c】クエスト詳細画面 — 「挑戦」ボタンタップ ───
    # ステージ番号 (1-1等) + "推奨" or "報酬" → クエスト詳細画面と判定
    # "挑戦" はゴールド装飾フォントで OCR 検出不可のため固定位置タップ
    # AssetManager (SWIPE_UP/DIALOG_NEXT) が誤マッチするため、ここで先に処理する
    _quest_stage = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                                  "3-1", "3-2", "4-1", "4-2"])
    _quest_detail_kw = any(kw in joined for kw in ["推奨", "報酬", "パーティ"])
    # 指アイコン+金枠表示中はそこしかタップ不可 → handle_tutorial に委譲
    if _quest_stage and _quest_detail_kw and ctx.analysis_path is not None:
        _finger = ASSET_MANAGER.match_finger_rotated(ctx.analysis_path)
        if _finger and _finger[2] >= 0.70:
            logger.info("[QUEST_DETAIL] 指テンプレ検出(%.3f) → 挑戦ボタン抑制 (指+金枠ハンドラへ)",
                        _finger[2])
            return None
    if _quest_stage and _quest_detail_kw:
        # OCR で挑戦テキストが読めた場合はその座標を使う
        _quest_chal = None
        for _qkw in ["挑戦", "戦闘", "出撃"]:
            _qc = has_text(ocr, _qkw, min_conf=0.3)
            if _qc and _qc["center"][1] > H * 0.5:
                _quest_chal = _qc
                break
        if _quest_chal:
            _qcx, _qcy = _quest_chal["center"]
            # 挑戦ボタンは右端にあるため、OCR テキスト中心が左寄りの場合を補正
            _qcx = max(_qcx, int(W * 0.88))
        else:
            # 固定位置: 挑戦ボタンは画面右下 (x=92%, y=90%)
            _qcx, _qcy = int(W * 0.92), int(H * 0.90)
        if state.game_roi:
            _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
            _qcy = min(_qcy, _roi_max_y)
        logger.info(">>> クエスト詳細 — 挑戦ボタン(%d,%d) タップ", _qcx, _qcy)
        tap_device(_qcx, _qcy, state, "QUEST_DETAIL_CHALLENGE")
        return "QUEST_DETAIL_CHALLENGE", 2.0

    return None
