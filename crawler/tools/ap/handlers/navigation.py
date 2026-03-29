"""
ap/handlers/navigation.py — マップ矢印 / ハイライト指示 / ストーリータップ指示

Phase 6: detect_and_act のナビゲーション系ハンドラ。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H
from tools.ap.context import DetectContext
from tools.ap.device import tap_device
from tools.ap.helpers import has_any, has_text
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

    # ─── ストーリーセリフ進行 (バトル外でセリフが出ている) ───
    # 「画面をタップ」系の指示 or バトルでもホームでもない日本語テキストが複数ある
    is_battle_now = ctx.in_battle_ctx
    tap_screen_kws = ["画面をタップ", "タップして進む", "タップで進む", "タップしてください",
                      "タップして次へ", "TOUCH TO CONTINUE"]
    tap_screen = has_any(ocr, tap_screen_kws)
    if tap_screen and not is_battle_now:
        cx, cy = tap_screen["center"]
        logger.info(">>> 【画面タップ指示】 '%s' (%d,%d)", tap_screen["text"], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP_HINT")
        return "STORY_TAP", 0.3

    # ─── ガチャ/交換所等のサブ画面からの脱出 ───
    # 左上に「↩ 画面名」が表示されるサブ画面を OCR で検出し、左上の戻るボタンをタップ
    # (チュートリアル中は誤動作するため home_reached 後のみ)
    if state.home_reached and not is_battle_now:
        _sub_screen_names = ["ガチャ", "交換所", "ショップ", "パーティ", "編成",
                             "ミッション", "メニュー", "設定", "フレンド", "プレゼント",
                             "クエスト", "育成", "タワー"]
        for item in ocr:
            _t = item.get("text", "")
            _c = item.get("center", (0, 0))
            # 左上 (x < 20%, y < 15%) に画面名テキストがあるか
            if _c[0] < W * 0.20 and _c[1] < H * 0.15:
                if any(kw in _t for kw in _sub_screen_names):
                    # 画面名の左にある戻るボタン (↩) をタップ
                    _back_x = max(int(_c[0] - W * 0.05), int(W * 0.02))
                    _back_y = _c[1]
                    logger.info(">>> 【サブ画面脱出】 左上に '%s' 検出 → 戻る (%d,%d)",
                                _t, _back_x, _back_y)
                    tap_device(_back_x, _back_y, state, "SUB_SCREEN_BACK")
                    return "SUB_SCREEN_BACK", 1.5

    return None
