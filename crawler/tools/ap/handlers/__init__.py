"""
ap/handlers — detect_and_act のシーン別ハンドラ

detect_and_act() はエントリポイントとして DetectContext を構築し、
dispatch() を呼び出して各ハンドラに順次委譲する。
"""
from __future__ import annotations

from typing import Optional

import logging

logger = logging.getLogger(__name__)

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.device import tap_device

from tools.ap.handlers.common import handle_common_guards
from tools.ap.handlers.finger_priority import handle_finger_priority
from tools.ap.handlers.quest import handle_quest_early
from tools.ap.handlers.dialog_phase import handle_dialog_phase
from tools.ap.handlers.tutorial import handle_tutorial
from tools.ap.handlers.home import handle_home
from tools.ap.handlers.scene import handle_scene_specific
from tools.ap.handlers.fallback import handle_fallback
from tools.ap.image_proc import find_gold_button


def dispatch(ctx: DetectContext, state: PilotState) -> tuple[str, float]:
    """detect_and_act のメインディスパッチャ。

    各ハンドラを優先順に呼び出し、最初に結果を返したハンドラの
    (action_name, wait_seconds) を返す。
    """
    # Phase 1: 共通ガード (ブラウザ脱出, MOVIE, DL, Loading, 確認ダイアログ, 権限, 設定, ご注意)
    r = handle_common_guards(ctx, state)
    if r is not None:
        return r

    # Phase 2: ダイアログハンドラ
    r = handle_dialog_phase(ctx, state)
    if r is not None:
        return r

    # Phase 2.5: ミニ会話タップ (OCR パスで検出済みの座標を使用)
    if ctx.mini_conv_pos is not None:
        _mc_cx, _mc_cy, _mc_side = ctx.mini_conv_pos
        logger.info(
            "[MINI_CONV] 吹き出し(%s) → タップ (%d,%d)", _mc_side, _mc_cx, _mc_cy)
        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
        state.last_action = "MINI_CONV_TAP"
        return "MINI_CONV_TAP", 0.3

    # Phase 3: 指アイコン+金枠 (CLAUDE.md §0 優先度1)
    r = handle_finger_priority(ctx, state)
    if r is not None:
        return r

    # Phase 3.5: 金枠ハイライト即タップ (CLAUDE.md §0 優先度1)
    # 指アイコンなしでも金枠が検出されたら即タップ
    # 3回連続画面変化なし → 偽陽性 (カード額縁等) と判断しスキップ (後続ハンドラに委譲)
    # リセットは金枠が検出されなくなった時 (=画面遷移) のみ
    _gold_stall = getattr(state, "_gold_frame_stall_count", 0)
    if (ctx.analysis_path is not None and not state.download_active
            and not ctx.has_dialog_corners):
        _gold = find_gold_button(ctx.analysis_path)
        if _gold:
            if _gold_stall < 3:
                _gx, _gy = _gold
                logger.info("[GOLD_FRAME] 金枠検出 → 即タップ (%d,%d) (stall=%d)",
                            _gx, _gy, _gold_stall)
                tap_device(_gx, _gy, state, "GOLD_FRAME_TAP")
                state._gold_frame_stall_count = _gold_stall + 1
                return "GOLD_FRAME_TAP", 0.5
            else:
                logger.info("[GOLD_FRAME] 3回連続変化なし → スキップ (後続ハンドラに委譲)")
        else:
            state._gold_frame_stall_count = 0

    # Phase 4: チュートリアル (名前入力, 指+金枠, スワイプ, アセットマッチ, ポップアップ)
    r = handle_tutorial(ctx, state)
    if r is not None:
        return r

    # Phase 7: ホーム画面検出 + チュートリアル完了判定
    r = handle_home(ctx, state)
    if r is not None:
        return r

    # Phase 8: シーン固有 (DL二次, クエストマップ, バトルOCR, バトル結果, ADV)
    r = handle_scene_specific(ctx, state)
    if r is not None:
        return r

    # Phase 8.2: クエスト/UI 検出 (MAIN STORY, Result, クエスト詳細)
    r = handle_quest_early(ctx, state)
    if r is not None:
        return r

    # Phase 9: フォールバック (閉じるボタン, システムダイアログ, 規約, 確認, ストーリー, etc.)
    return handle_fallback(ctx, state)
