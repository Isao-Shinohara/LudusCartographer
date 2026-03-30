"""
ap/handlers/quest.py — クエスト早期検出ハンドラ

MAIN STORY 画面、メインクエスト選択、クエストマップノード、
Result 画面早期検出、クエスト詳細画面(挑戦ボタン)を処理する。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.device import tap_device
from tools.ap.helpers import has_any, has_text

logger = logging.getLogger("auto_pilot")


def handle_quest_early(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """クエスト関連 UI の早期検出。

    - メインクエスト選択画面: 「Main」ボタンタップ
    - クエストマップ画面: ノード選択 + 挑戦ボタン
    - MAIN STORY 画面: クエストカードタップ
    - Result 画面: 「次へ」ボタンタップ
    - クエスト詳細画面: 「挑戦」ボタンタップ
    """
    texts = ctx.texts
    joined = ctx.joined
    ocr = ctx.ocr
    W = ctx.W
    H = ctx.H

    # ─── メインクエスト選択画面: 「Main」ボタンを直接タップ ───
    # 金枠がバナー装飾を拾って空振りするため、OCRの「Main」テキスト位置をタップ
    # 白ハンドポインタがある場合は指差しガイドが優先 (Upgrade等を指す場合がある)
    if any("メインクエスト" in t for t in texts) and ctx.white_hand_pos is None:
        _main_btn = has_text(ocr, "Main", min_conf=0.3)
        if _main_btn:
            _mx, _my = _main_btn["center"]
            logger.info(">>> 【メインクエスト】 Main ボタン (%d,%d) タップ", _mx, _my)
            tap_device(_mx, _my, state, "MAIN_QUEST_TAP")
            return "MAIN_QUEST_TAP", 2.0

    # ─── クエストマップ画面: ノード選択 + 挑戦ボタン ───
    _has_main = has_text(ocr, "Main", min_conf=0.3)
    _has_floor = any("階層" in t for t in texts)
    _challenge_btn = has_text(ocr, "挑戦", min_conf=0.3)
    # 挑戦ボタンが見えていればクエスト詳細パネルが開いている → 挑戦タップ
    if _has_main and _challenge_btn:
        _cx, _cy = _challenge_btn["center"]
        logger.info(">>> 【クエストマップ】 挑戦ボタン (%d,%d) タップ", _cx, _cy)
        tap_device(_cx, _cy, state, "QUEST_CHALLENGE_TAP")
        return "QUEST_CHALLENGE_TAP", 3.0
    if _has_main and _has_floor:
        # ノードラベル "X-Y" パターンを探す
        _quest_node = None
        for _r in ocr:
            if _r.get("confidence", 0) < 0.3:
                continue
            if re.fullmatch(r"\d+-\d+", _r["text"].strip()):
                _quest_node = _r
                break
        if _quest_node:
            _qx, _qy = _quest_node["center"]
            logger.info(">>> 【クエストマップ】 ノード '%s' (%d,%d) タップ",
                        _quest_node["text"], _qx, _qy)
            tap_device(_qx, _qy, state, "QUEST_NODE_TAP")
            return "QUEST_NODE_TAP", 2.0

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
