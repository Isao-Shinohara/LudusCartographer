"""
ap/handlers/finger.py — 指差しアイコン (肌色ブロブ) 検出ハンドラ

バトル発光 State Machine (#1-pre) + 指ブロブ mega-block (#1):
- バトル画面判定、速度チュートリアル早期検出
- タイトル画面 / ホーム画面検出 (指+金枠)
- ADV/システムダイアログのブロブ除外
- バトル中ブロブフィルタリング (左キャラ, 右パネル, 下部UI)
- Hard Masking, 2段階ターゲット選択, スタック回復
- SWIPE_AUTO (チュートリアル移動シーン)
- 最終タップ決定 (金枠 or 指先端)
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.constants import (
    _BATTLE_CORE_KWS, _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS,
    _OCR_BBOX_Y_PADDING,
    _RIGHT_PANEL_X, _SPATIAL_MARGIN_TOP, _CLOSE_BTN_OFFSET,
    _FINGER_TIP_RATIO, _GLOW_CENTER_Y_OFFSET,
    ANALYSIS_W, ANALYSIS_H, BATTLE_WAIT, PHASH_THRESHOLD,
)
from tools.ap.image_proc import (
    _run_battle_glow_sm, find_finger_blobs, find_gold_frame_near,
    create_finger_mask_image, detect_tutorial_gold_button_tap,
    detect_tutorial_overlay, roi_to_device, ASSET_MANAGER,
    detect_adv_scene,
)
from tools.ap.helpers import has_any, has_text, log_milestone
from tools.ap.device import adb, tap_device, swipe_device, take_screenshot
from lc.utils import compute_phash, phash_distance

# Result画面ハンドラ (auto_pilot.py から分離予定)
from tools.ap.handlers.result import handle_result_screen

logger = logging.getLogger("auto_pilot")


def handle_finger_detection(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """指差しアイコン (肌色ブロブ) 検出 + バトル発光SM。

    Returns:
        (action_name, wait_seconds) or None to fall through.
    """
    texts = ctx.texts
    joined = ctx.joined
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path
    ocr = ctx.ocr
    _is_battle_early = ctx.is_battle_early
    _adv_result = ctx.adv_result
    _is_mini_conv = ctx.is_mini_conv

    # ─── 【最優先 #1-pre】バトル発光 State Machine (フッター下部30%限定) ─────────
    if _is_battle_early and analysis_path is not None:
        _gsm_result = _run_battle_glow_sm(analysis_path, W, H, state, ocr, tag="GLOW_SM")
        if _gsm_result is not None:
            return _gsm_result

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        # 「AUTO」のみはストーリー画面にも表示されるため除外、戦闘固有キーワードで判定
        is_battle_screen = any(kw in joined for kw in _BATTLE_CORE_KWS)
        if is_battle_screen:
            log_milestone(state, "FIRST_BATTLE")
        # ── 速度チュートリアル早期検出 (もや検出より前に処理) ──
        _speed_tip_early = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if _speed_tip_early and is_battle_screen:
            _sp_x, _sp_y = roi_to_device(int(W * 0.927), int(H * 0.026), state.game_roi)
            logger.info(">>> [EARLY] 速度ツールチップ → 速度ボタン (%d,%d) タップ", _sp_x, _sp_y)
            tap_device(_sp_x, _sp_y, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 0.5
        # タイトル画面 / ホーム画面検出: ブロブ誤検出を防ぐ
        _nav_joined = joined
        # 利用規約画面・同意ダイアログが存在する場合はタイトル画面と区別する
        _is_tos_screen = "利用規約" in _nav_joined or "同意してゲームを始める" in _nav_joined
        _title_kws_game = ["魔法", "少女", "まどか", "マギカ", "まどかハ", "MADOKA", "MAGICA"]
        is_title_screen = (
            not state.home_reached and not _is_tos_screen and (
                # 条件A: TAP TO START は確実にタイトル (Magia Exedra 単独は除外)
                any(kw in _nav_joined for kw in ["TAP TO START", "TAPTOSTART"]) or
                # 「動画配信設定」「Ver.」はタイトル画面固有の上部 UI
                (any(kw in _nav_joined for kw in ["動画配信", "勤画配信", "Ver.2", "Ver.2."])
                 and any(kw in _nav_joined for kw in _title_kws_game + ["PUELLA"])) or
                ("VID" in _nav_joined and any(kw in _nav_joined for kw in _title_kws_game)) or
                # フォールバック: ゲームタイトルロゴ文字 + Rank がない + ホームナビがない
                # "Rank" "Main" "推奨" は MAIN STORY 選択画面なのでタイトルと区別する
                (any(kw in _nav_joined for kw in ["PUELLA MAGI", "PUELLAHAGI", "PUELLAMAGI",
                                                   "PUELLA MAGIMADOKA"])
                 and any(kw in _nav_joined for kw in _title_kws_game)
                 and not any(kw in _nav_joined for kw in ["クエスト", "ショップ", "ガチャ",
                                                           "Rank", "Main", "推奨"]))
            )
        )
        if is_title_screen:
            logger.info("  タイトル画面検出 → TAP TO START (760,628) タップ")
            log_milestone(state, "TITLE_TAP")
            _tt_x, _tt_y = roi_to_device(int(W * 0.5), int(H * 0.87), state.game_roi)
            tap_device(_tt_x, _tt_y, state, "TITLE_TAP_START")
            return "TITLE_TAP", 2.0
        # ホーム画面検出: フッターエリア (y > H*0.85) のナビキーワードが2個以上
        # フッター以外 (編成メニュー内の「パーティ」等) は誤検出になるため除外
        _home_nav_kws = ["クエスト", "ショップ", "ガチャ", "ガシャ", "ユニオン",
                         "光の間", "パーティ", "プレイヤーマッチ", "お知らせ",
                         "イベント", "マイページ", "編成", "MAGIA EXEDRA"]
        _footer_y_min = int(H * 0.85)
        _footer_ocr = [item for item in ocr
                       if item.get("center", (0, 0))[1] >= _footer_y_min]
        _footer_texts = [item.get("text", "") for item in _footer_ocr]
        _home_kw_count = sum(1 for h in _home_nav_kws
                             if any(h in t for t in _footer_texts))
        # ── Result画面ハンドラ (OCR mode) ──
        if not is_battle_screen:
            _result_ocr = handle_result_screen(state, analysis_path, ocr, state.last_phash_dist, mode="OCR")
            if _result_ocr:
                return _result_ocr
        # ─── ADV選択肢 — 肯定ボタン絶対優先 ───────────────────────────
        # OK / はい / 了解 を最優先。キャンセル / いいえ は選択禁止。
        _adv_pos = has_any(ocr, _CONFIRM_POS_KWS)
        _adv_neg = has_any(ocr, _CONFIRM_NEG_KWS)
        if _adv_pos:
            _ac_x, _ac_y = _adv_pos["center"]
            # OCR bbox はテキスト下部パディングを含むため Y を上方補正
            # ボタンの上半分を狙い、空振りを防止 (画質設定OK等)
            _ac_y_adj = max(0, _ac_y - _OCR_BBOX_Y_PADDING)
            logger.info(
                "[ADV-Choice] '%s' (%d,%d→Y%d) タップ (否定='%s'無視)",
                _adv_pos["text"], _ac_x, _ac_y, _ac_y_adj,
                _adv_neg["text"] if _adv_neg else "なし",
            )
            tap_device(_ac_x, _ac_y_adj, state, f"ADV_CHOICE '{_adv_pos['text']}'")
            return "ADV_CHOICE", 1.0

        # バトル時は dark_mode=True で輝度閾値を緩和し min_area=200 に下げる（暗背景対応）
        # ホーム画面 / 利用規約ダイアログ / システムダイアログはブロブ誤検出になるためスキップ
        _is_system_dialog = any(kw in _nav_joined for kw in
                                ["画質を設定", "高画質", "省エネ", "省工ネ", "データ引き継ぎ",
                                 "サポート", "お問い合わせ", "キャッシュクリア"])
        if _home_kw_count >= 2:
            logger.info("  ホーム画面検出 (footer nav×%d: %s) → MOYA_TAP スキップ",
                        _home_kw_count, _footer_texts)
            # ── 【最優先】ダンジョン挑戦ボタン: 「挑戦」OCR検出 → 直接タップ (ホーム誤検出突破) ──
            _chal_btn = has_text(ocr, "挑戦", min_conf=0.3)
            if _chal_btn:
                _cb_x, _cb_y = _chal_btn["center"]
                # ROI クランプ: ゲーム領域外 (黒帯) をタップしない
                if state.game_roi:
                    _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
                    _cb_y = min(_cb_y, _roi_max_y)
                # 挑戦ボタンは右下(x>W*0.5, y>H*0.5)にあるはず
                if _cb_x > W * 0.5 and _cb_y > H * 0.5:
                    logger.info("[Challenge] '%s'(%d,%d) → 直接タップ", _chal_btn["text"], _cb_x, _cb_y)
                    tap_device(_cb_x, _cb_y, state, "CHALLENGE_TAP")
                    return "CHALLENGE_TAP", 1.0
            # ── ホームチュートリアル: 指アイコン+金枠がある場合は優先タップ ──
            # 回数制限なし: 指+金枠が実在する限り何回でもタップ
            _ht_blobs = find_finger_blobs(analysis_path, home_mode=True) if analysis_path else []
            _ht_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
            # 暗転オーバーレイ検出 (チュートリアル中は非ハイライト部分が暗い)
            _ht_dimmed = detect_tutorial_overlay(analysis_path) if analysis_path else False
            if _ht_blobs or _ht_gold:
                _ht_target = None
                if _ht_blobs:
                    _ht_chosen = max(_ht_blobs, key=lambda b: b[2])
                    _ht_bx, _ht_by = _ht_chosen[0], _ht_chosen[1]
                    _ht_gf = find_gold_frame_near(analysis_path, _ht_bx, _ht_by) if analysis_path else None
                    # フッターナビ領域 (y > H*0.85) の金色要素を除外
                    if _ht_gf and _ht_gf[1] > H * 0.85:
                        _ht_gf = None
                    if _ht_gf:
                        _ht_fg_dist = ((_ht_bx - _ht_gf[0]) ** 2 + (_ht_by - _ht_gf[1]) ** 2) ** 0.5
                        if _ht_fg_dist <= 200:
                            _ht_target = (_ht_gf[0], _ht_gf[1])
                            logger.info("  ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) dist=%.0f dimmed=%s",
                                        _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1], _ht_fg_dist, _ht_dimmed)
                        elif _ht_dimmed:
                            # 金枠遠い+暗転あり → 指先タップ
                            _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                            _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                            logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠(%d,%d) dist=%.0f>200+暗転あり]",
                                        _ht_bx, _ht_by, *_ht_target, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                        else:
                            logger.info("  ホーム指検出: 指(%d,%d) 金枠(%d,%d) dist=%.0f>200+暗転なし → スキップ",
                                        _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                    elif _ht_dimmed:
                        # 金枠なしだが暗転あり → 指先タップ (チュートリアルの可能性高い)
                        _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                        _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                        logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転あり]",
                                    _ht_bx, _ht_by, *_ht_target)
                    else:
                        # 金枠なし+暗転なし: 画面中央付近なら指先をタップ、端なら偽検出疑い
                        if _ht_bx > 150 and _ht_by > 100 and _ht_bx < W - 100 and _ht_by < H - 80:
                            _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                            _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                            logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転なし・中央付近]",
                                        _ht_bx, _ht_by, *_ht_target)
                        else:
                            logger.info("  ホーム指検出: 指(%d,%d) 金枠なし+暗転なし+画面端 → 偽検出疑い、スキップ",
                                        _ht_bx, _ht_by)
                elif _ht_gold:
                    # 金枠のみ (指なし) → 暗転があればチュートリアル
                    if _ht_dimmed:
                        _ht_target = _ht_gold
                        logger.info("  ホームチュートリアル: 金ボタン(%d,%d) [暗転あり]", *_ht_gold)
                    else:
                        logger.info("  ホーム金枠検出: (%d,%d) 暗転なし → 通常ホーム、スキップ", *_ht_gold)
                if _ht_target:
                    state.home_tutorial_tap_count += 1
                    tap_device(_ht_target[0], _ht_target[1], state, "HOME_TUTORIAL_TAP")
                    return "HOME_TUTORIAL_TAP", 0.5
            blobs = []
        elif _is_tos_screen or _is_system_dialog:
            logger.info("  システムダイアログ/利用規約検出 → MOYA_TAP スキップ")
            blobs = []
        elif _adv_result.is_adv or _is_mini_conv:
            logger.info("  ADV/ミニ会話シーン検出 → 指ブロブ検出スキップ (背景装飾の誤検出防止)")
            blobs = []
        else:
            state.home_tutorial_tap_count = 0  # ホーム以外 → カウンタリセット
            # バトル中は dark_mode=True + min_area=200 で暗背景の指アイコンも検知
            _blob_dark = is_battle_screen
            blobs = find_finger_blobs(analysis_path,
                                      min_area=200 if _blob_dark else 400,
                                      dark_mode=_blob_dark)
            # 画面端の誤検出を除去: 上端/右端最端はシステムUI
            blobs = [b for b in blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        if blobs:
            # バトル中は中央エリア(バトルフィールド)の肌色は誤検出なので無視
            # 優先順位: 左キャラカード(x<600,y>550) > 右パネル > 下部UI(y>H*0.8)
            if is_battle_screen:
                # ── チュートリアル金枠+指: 大面積blob(area>10000)は最優先 ──
                _tutorial_gold = [b for b in blobs if b[2] > 10000]
                left_char = [b for b in blobs if b[0] < 600 and b[1] > H * 0.76]
                # right_panel: スキルボタンは下半分(y>H*0.45)のみ。上部の蝶エネミーを排除
                right_panel = [b for b in blobs if b[0] > _RIGHT_PANEL_X and b[1] > H * 0.45]
                bottom_ui = [b for b in blobs if b[1] > H * 0.8 and b[0] >= 600]
                if _tutorial_gold:
                    blobs = _tutorial_gold[:1]
                    logger.info("  バトル: 金枠+指 (%d,%d) area=%.0f → チュートリアル最優先",
                                blobs[0][0], blobs[0][1], blobs[0][2])
                elif state.char_just_selected:
                    # 左キャラ選択済み → 必殺技を優先、なければ右スキルを選択
                    _skill_match = None
                    if analysis_path is not None:
                        try:
                            _skill_roi = (_RIGHT_PANEL_X, int(H * 0.45), W, H)
                            _skill_match = ASSET_MANAGER.match_single(
                                "battle_skill", analysis_path, roi=_skill_roi)
                        except Exception:
                            pass
                    if _skill_match and _skill_match[2] >= 0.55:
                        # 必殺技ボタン検出 → 直接タップ (モヤ blob ではなくテンプレ座標)
                        _sk_x, _sk_y = _skill_match[0], _skill_match[1]
                        logger.info("  バトル: キャラ選択後 → 必殺技 (%.2f) (%d,%d)",
                                    _skill_match[2], _sk_x, _sk_y)
                        state.char_just_selected = False
                        tap_device(_sk_x, _sk_y, state, "BATTLE_HISSATSU")
                        return "BATTLE_HISSATSU", 0.5
                    elif right_panel:
                        blobs = right_panel
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 右スキルもや %d個", len(blobs))
                    elif bottom_ui:
                        blobs = bottom_ui
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 下部UIもや %d個", len(blobs))
                    else:
                        # 右スキルがまだ表示されていない → 少し待つ
                        logger.info("  バトル: キャラ選択後 → 右スキル待ち")
                        blobs = []
                elif left_char:
                    # フリーバトル: 左キャラ選択が最優先
                    blobs = left_char
                    logger.info("  バトル: 左キャラもや %d個 (最優先)", len(blobs))
                elif right_panel:
                    blobs = right_panel
                    logger.info("  バトル: 右パネルもや %d個", len(blobs))
                elif bottom_ui:
                    blobs = bottom_ui
                    logger.info("  バトル: 下部UIもや %d個", len(blobs))
                else:
                    logger.info("  バトル中: 有効もやなし(中央は誤検出) → OCR判定へ")
                    blobs = []

        if blobs:
            # ── Hard Masking 2.0: バトル中は先頭blobの350×350以外を黒塗り ──
            # 右側スキルボタンの金枠を物理的に排除し、指アイコンの示すターゲットだけを照準
            _hm_analysis = analysis_path
            if is_battle_screen and analysis_path is not None:
                _hm_cx, _hm_cy = blobs[0][0], blobs[0][1]
                _hm_analysis = create_finger_mask_image(analysis_path, _hm_cx, _hm_cy, half=175)
                if _hm_analysis != analysis_path:
                    logger.info("[HARD_MASK] 指(%d,%d)周囲350×350px以外 黒塗り → 認識対象限定",
                                _hm_cx, _hm_cy)

            # ── 2段階ターゲット選択 ──────────────────────────────
            # Step1: 金枠が見つかる blobs を優先（真のGUIガイド要素）
            # Step2: 金枠なし blobs は右側優先フォールバック
            _blob_with_gold = None
            _blob_gold_frame = None
            _blob_fallback = None
            for _b in blobs:
                _bx0, _by0 = _b[0], _b[1]
                # Hard Masking 2.0 適用: バトル時はマスク済み画像で金枠検索
                _gf = find_gold_frame_near(_hm_analysis, _bx0, _by0, search_radius=150)
                if _gf is not None:
                    # 金枠が見つかった最初のblobを使用
                    if _blob_with_gold is None:
                        _blob_with_gold = _b
                        _blob_gold_frame = _gf
                if _blob_fallback is None and _b[0] > _RIGHT_PANEL_X:
                    _blob_fallback = _b  # 右側優先フォールバック
            if _blob_fallback is None and blobs:
                _blob_fallback = blobs[0]
            # ── Hard Mask 一時ファイルを削除 (メモリリーク防止) ──
            if _hm_analysis != analysis_path:
                try:
                    Path(_hm_analysis).unlink(missing_ok=True)
                except OSError:
                    pass

            if _blob_with_gold is not None:
                chosen = _blob_with_gold
                _gold_frame = _blob_gold_frame
                logger.info("  (金枠ありblob優先: %d個中1個)", len(blobs))
            else:
                chosen = _blob_fallback
                _gold_frame = None
                if len(blobs) > 1:
                    logger.info("  (金枠なし → 右パネル優先: %d個中1個を選択)", len(blobs))
            # ────────────────────────────────────────────────────
            fx, fy, fa = chosen[0], chosen[1], chosen[2]
            f_bx, f_by, f_bw, f_bh = chosen[3], chosen[4], chosen[5], chosen[6]
            # 50px 近接判定: アニメーション中のブロブ (±20px移動) でもカウントが継続する
            if state.last_blob_xy == (0, 0):
                # 初回検出: 基準座標を設定してカウントを0にリセット
                state.last_blob_xy = (fx, fy)
                state.blob_same_count = 0
            elif abs(fx - state.last_blob_xy[0]) <= 50 and abs(fy - state.last_blob_xy[1]) <= 50:
                state.blob_same_count += 1
                state.last_blob_xy = (fx, fy)  # 追跡: 次回比較基準を更新
            else:
                state.blob_same_count = 0
                state.last_blob_xy = (fx, fy)
            if state.blob_same_count >= 5:
                _stg = state.blob_same_count
                logger.info(">>> [RECOVERY] スタック stage=%d (%d,%d)", _stg, fx, fy)
                # 移動シーン(OCR無し) + 10回以上 → SWIPE_UP 強制 (最優先)
                if _stg >= 10 and len(texts) == 0:
                    logger.info(">>> [SWIPE_FALLBACK] フィンガースタック%d回+OCR無し → SWIPE_UP強制", _stg)
                    swipe_device(fx, H - 50, fx, 50, 10000, state=state, desc="SWIPE_FALLBACK")
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "SWIPE_FALLBACK", 1.5
                # Stage 1-3 (count=5,6,7): ジッター±10px タップ
                if _stg <= 7:
                    _jx = max(50, min(W - 50, fx + random.randint(-10, 10)))
                    _jy = max(50, min(H - 50, fy + random.randint(-10, 10)))
                    logger.info(">>> [RECOVERY s%d] ジッタータップ (%d,%d)", _stg - 4, _jx, _jy)
                    tap_device(_jx, _jy, state, f"RECOVERY_JITTER_s{_stg - 4}")
                    if _stg == 7:
                        time.sleep(0.5)
                    return "RECOVERY_JITTER", 0.5
                # Stage 4-6 (count=8,9,10): キャッシュ破棄・広域金枠再スキャン
                elif _stg <= 10:
                    _rf_gf = find_gold_frame_near(analysis_path, fx, fy, search_radius=300) if analysis_path else None
                    if _rf_gf:
                        _rf_tx, _rf_ty = _rf_gf[0], _rf_gf[1]
                        logger.info(">>> [RECOVERY s%d] 広域金枠(%d,%d) タップ", _stg - 7, _rf_tx, _rf_ty)
                        tap_device(_rf_tx, _rf_ty, state, f"RECOVERY_RESCAN_s{_stg - 7}")
                        state.blob_same_count = 0
                        state.last_blob_xy = (0, 0)
                        return "RECOVERY_RESCAN", 1.0
                    logger.info(">>> [RECOVERY s%d] 広域金枠なし → OCRフォールバックへ", _stg - 7)
                    # OCR ベース処理に落ちる (fall through)
                # Stage 7-9 (count=11,12,13): ランダムブラインドタップ
                elif _stg <= 13:
                    _bx = max(50, min(W - 50, fx + random.randint(-80, 80)))
                    _by = max(50, min(H - 50, fy + random.randint(-60, 60)))
                    logger.info(">>> [RECOVERY s%d] ブラインドタップ (%d,%d)", _stg - 10, _bx, _by)
                    tap_device(_bx, _by, state, f"RECOVERY_BLIND_s{_stg - 10}")
                    return "RECOVERY_BLIND", 0.8
                # Stage 10 (count>=14): 5秒待機 + リセット
                else:
                    logger.info(">>> [RECOVERY s10] 2秒待機 + カウンタリセット")
                    time.sleep(2.0)
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "RECOVERY_FINAL_WAIT", 0.5
            else:
                # ── Step3-pre: 指タップ静止検出 → スワイプシーン自動切替 ──
                # チュートリアル中のチェック柄移動シーン専用。
                # 条件: チュートリアル中 + GoldSwipe直前実行 + 3回タップ空振り + OCRテキスト少ない
                _SWIPE_AUTO_ACTIONS = ("GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN", "SWIPE_UP",
                                       "SWIPE_AUTO", "MOYA_TAP")
                if (state.finger_tap_static.stalled
                        and not state.home_reached
                        and state.last_action in _SWIPE_AUTO_ACTIONS
                        and _gold_frame is None
                        and len(texts) <= 1):
                    logger.info(
                        ">>> [SWIPE_AUTO] 指タップ静止%d回+OCR無し → 連続スワイプ開始 (指位置: %d,%d)",
                        state.finger_tap_static.count, fx, fy,
                    )
                    _base_ph_sw = compute_phash(analysis_path)
                    _sw_success = False
                    # 動画シーンに完全遷移するまでスワイプ継続
                    # (チェック柄中の微変化では止まらない: 閾値20)
                    _SW_CHANGE_THRESHOLD = 20
                    for _sw_i in range(30):  # 最大30回 (約90秒)
                        swipe_device(fx, H - 50, fx, 50, 10000, state=state, desc="SWIPE_AUTO")
                        time.sleep(0.2)
                        _sw_ss, _, _, _ = take_screenshot()
                        _sw_ph = compute_phash(_sw_ss)
                        if (_base_ph_sw and _sw_ph
                                and phash_distance(_base_ph_sw, _sw_ph) >= _SW_CHANGE_THRESHOLD):
                            logger.info(
                                ">>> [SWIPE_AUTO] %d回目で大きな画面変化検出 (dist=%d) → スワイプ完了",
                                _sw_i + 1, phash_distance(_base_ph_sw, _sw_ph),
                            )
                            _sw_success = True
                            break
                        # 基準phashを更新しない — 初回画面との比較を維持
                    if not _sw_success:
                        logger.warning(">>> [SWIPE_AUTO] 20回スワイプしても変化なし → タップに戻る")
                    state.finger_tap_static.reset()
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "SWIPE_AUTO", 1.5

                # ── Step3: タップ座標決定 (共通ルール) ──
                # 指アイコンは「タップすべき金枠」を指し示すガイド。
                # (A) 指の近く (200px以内) に金枠 → 金枠中心をタップ
                # (B) 金枠なし → 指先端をタップ
                if _gold_frame is not None:
                    gfx, gfy, gfw, gfh = _gold_frame
                    _gbox = (gfx - gfw // 2, gfy - gfh // 2, gfw, gfh)
                    # 指と金枠の距離チェック: 近距離なら金枠中心を採用
                    _fg_dist = ((fx - gfx) ** 2 + (fy - gfy) ** 2) ** 0.5
                    if _fg_dist <= 200:
                        tap_x = gfx
                        tap_y = gfy
                        logger.info("FINGER→GOLD_FRAME (%d,%d) → gold_center(%d,%d) dist=%.0f count=%d",
                                    fx, fy, tap_x, tap_y, _fg_dist, state.blob_same_count)
                    else:
                        # 遠い金枠は無関係 → 指先端
                        tap_x = fx
                        tap_y = f_by + max(1, int(f_bh * _FINGER_TIP_RATIO))
                        _gbox = None
                        logger.info("FINGER_DETECTED (%d,%d) → tip(%d,%d) [gold(%d,%d) dist=%.0f>200 無視] count=%d",
                                    fx, fy, tap_x, tap_y, gfx, gfy, _fg_dist, state.blob_same_count)
                else:
                    _gbox = None
                    tap_x = fx
                    tap_y = f_by + max(1, int(f_bh * _FINGER_TIP_RATIO))
                    logger.info("FINGER_DETECTED (%d,%d) area=%.0f → tip(%d,%d) count=%d",
                                fx, fy, fa, tap_x, tap_y, state.blob_same_count)
                # ── タップ直前 MOVIE チェック: 動画遷移中のタップ防止 ──
                from tools.ap.image_proc import detect_movie_scene, detect_adv_scene
                _pre_tap_movie = detect_movie_scene(
                    analysis_path, adv_result=detect_adv_scene(analysis_path, roi=state.game_roi),
                    phash_dist=dist)
                if _pre_tap_movie.is_movie:
                    logger.info("[MOYA_TAP] タップ直前に MOVIE 検出 → タップ中止")
                    state.current_scene = "MOVIE"
                    return "MOVIE_WAIT", 0.5
                tap_device(tap_x, tap_y, state, f"MOYA_TAP ({tap_x},{tap_y})",
                           finger_box=(f_bx, f_by, f_bw, f_bh),
                           gold_box=_gbox)
                state.finger_detections += 1
                if _gold_frame is not None:
                    state.gold_detections += 1
                # 左キャラ選択後は char_just_selected / character_selected フラグをセット
                if fx < 600 and fy > H * 0.55:
                    state.char_just_selected = True
                    state.character_selected = True  # GLOW SM 用にも同期
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 1.0

    return None
