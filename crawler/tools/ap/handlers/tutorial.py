"""
ap/handlers/tutorial.py — チュートリアル固有ハンドラ

名前入力、チュートリアル歩行/スワイプ、金枠ボタン、アセットマッチング。
ポップアップ/ダイアログ系は dialog_phase.py / fallback.py に移動済み。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H,
    BATTLE_WAIT, PHASH_THRESHOLD,
    _DIALOG_FIRST_KWS, _BATTLE_UI_KWS,
)
from tools.ap.device import adb, tap_device, swipe_device, take_screenshot
from tools.ap.helpers import (
    has_any, has_text, log_milestone,
)
from tools.ap.image_proc import (
    ASSET_MANAGER,
    roi_to_device,
    is_tutorial_walk_scene,
    detect_tutorial_gold_swipe,
    find_gold_button,
    detect_movie_scene,
    detect_adv_scene,
    prepare_analysis_image,
    detect_background_blur,
    imread_cached,
)
from lc.ocr import run_ocr
from lc.utils import compute_phash, phash_distance

logger = logging.getLogger("auto_pilot")


def handle_tutorial(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """チュートリアル固有の画面を処理するハンドラ。

    - プレイヤー名入力ダイアログ (#0-b-name)
    - チュートリアル歩行シーン / Gold swipe (#0-walk, #0-aa)
    - 金枠ボタンタップ (バトルチュートリアル) (#0-ab)
    - アセットマッチハンドラ (#0-a): スワイプ指、ダイアログ▷、動画スキップ

    Returns:
        (action_name, wait_seconds) or None to fall through.
    """
    texts = ctx.texts
    joined = ctx.joined
    W = ctx.W
    H = ctx.H
    ocr = ctx.ocr
    analysis_path = ctx.analysis_path

    # ─── 【最優先 #0-b-name】プレイヤー名入力ダイアログ (FINGER_GOLD_TAP より先に評価) ───
    # 名前入力画面の金枠入力フィールドが FINGER_GOLD_TAP に食われるのを防ぐ
    _name_input_item = has_text(ocr, "プレイヤー名を入力", min_conf=0.3)
    if _name_input_item:
        _ni_ui_words = {"プレイヤー名を入力してください", "プレイヤー名は", "変更後3日間", "名前入力", "OK"}
        _ni_name_texts = [t for t in texts if t not in _ni_ui_words and len(t) >= 2
                          and not t.startswith("プレイヤー") and "/" not in t
                          and not t.startswith("※") and not t.startswith("＜")]
        _ni_ok = next(
            (item for item in ocr if "OK" in item.get("text", "") and item["center"][1] > H * 0.5),
            None
        )
        if _ni_name_texts and _ni_ok:
            _ni_cx = _ni_ok["center"][0]
            _ni_cy = int(H * 0.78)
            logger.info(">>> 【名前入力 OK】 入力済み='%s' → (%d,%d) タップ", _ni_name_texts[0], _ni_cx, _ni_cy)
            log_milestone(state, "NAME_INPUT")
            tap_device(_ni_cx, _ni_cy, state, "NAME_INPUT_OK")
            return "NAME_INPUT_OK", 2.0
        elif _ni_ok:
            # テキストフィールドのプレースホルダー「プレイヤー名を入力」を探す
            # 説明文「〜してください」ではなく、短い方(フィールド内)の座標を使用
            _field_item = next(
                (item for item in ocr
                 if item.get("text", "").strip() == "プレイヤー名を入力"),
                _name_input_item  # フォールバック: 最初のマッチ
            )
            _nf_ocr_x, _nf_ocr_y = _field_item["center"]
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d) [解析座標]", _nf_ocr_x, _nf_ocr_y)
            tap_device(_nf_ocr_x, _nf_ocr_y, state, "NAME_INPUT_FOCUS")
            time.sleep(0.5)
            adb("shell input text MadoDora")
            time.sleep(0.3)
            adb("shell input keyevent 66")
            logger.info(">>> 【名前入力】 'MadoDora' 入力完了 → OK タップ待ち")
            return "NAME_INPUT_TEXT", 1.5

    # ─── 【最優先 #0-aa】HSV金色ポインター検出 → ホールドスワイプ (Type A) ───
    # 縦長金色領域 h/w>=3.5 かつ幅<=100px のみ有効 (ボタン/カード誤検出防止)。
    # ─── 【最優先 #0-walk】チュートリアル歩行シーン (白黒背景) → 上ホールドスワイプ ───
    # 指アイコンが出ない場面でも白黒市松/階段背景なら上スワイプを強制実行。
    # ADV検出ガードより先に評価する (ADV誤検出でブロックされるのを防ぐ)。
    _walk_ap = analysis_path is not None
    _walk_texts = len(texts) <= 2
    _walk_scene = state.current_scene != "MOVIE"
    _walk_check = is_tutorial_walk_scene(analysis_path) if (_walk_ap and _walk_texts and _walk_scene) else False
    if _walk_ap and _walk_texts and _walk_scene and _walk_check:
        _sx = int(ANALYSIS_W * 0.5)
        _fy = ANALYSIS_H - 20   # 下端ギリギリ (より大きなスワイプ)
        _ty = 20                 # 上端ギリギリ
        _dur = 10000
        logger.info(">>> [TutorialWalk] 白黒背景検出 (OCR %d件) → 上ホールドスワイプ (全画面)", len(texts))
        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc="TutorialWalk_UP")
        return "GOLD_SWIPE_UP", BATTLE_WAIT

    # チュートリアル3D移動シーン(チェッカー床/階段/廊下)で発火。
    # phash監視: スワイプ後2s待機 → 変化なければ再実行 (最大2回)
    # バトルUI（通常攻撃・単体攻撃・WAVE・Turn）が見えるとき はバトル中なのでスキップ
    _is_battle_ui = any(kw in joined for kw in _BATTLE_UI_KWS)
    _has_dialog_kw = any(kw in joined for kw in _DIALOG_FIRST_KWS)
    from tools.ap.image_proc import count_home_nav_templates
    _is_home_screen = count_home_nav_templates(analysis_path) >= 3 if analysis_path else False
    if analysis_path is not None and not _is_battle_ui and not ctx.adv_result.is_adv and not _has_dialog_kw and not _is_home_screen and not state.post_download:
        _gold = detect_tutorial_gold_swipe(analysis_path)
        if _gold:
            _dir, _sx, _fy, _ty, _dur = _gold
            # 距離が短すぎる場合はフルスクリーンスワイプを強制 (解像度差対策)
            _min_dist = int(ANALYSIS_H * 0.6)
            if abs(_fy - _ty) < _min_dist:
                if _dir == "UP":
                    _fy = ANALYSIS_H - 50
                    _ty = 50
                else:
                    _fy = 50
                    _ty = ANALYSIS_H - 50
            _gs_action = "GOLD_SWIPE_UP" if _dir == "UP" else "GOLD_SWIPE_DOWN"
            logger.info(">>> [GoldSwipe] %s (%d,%d)→(%d,%d) %dms",
                        _dir, _sx, _fy, _sx, _ty, _dur)
            swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc=f"GoldSwipe_{_dir}")
            # スワイプ後にシーン変化を確認 — MOVIE/ADV/BATTLE/UI出現で停止
            time.sleep(0.3)
            _gs_path, _gs_w, _gs_h, _ = take_screenshot()
            _gs_analysis = prepare_analysis_image(_gs_path, _gs_w, _gs_h) if _gs_path else None
            if _gs_analysis:
                _gs_movie = detect_movie_scene(_gs_analysis, adv_result=None, phash_dist=99)
                _gs_adv = detect_adv_scene(_gs_analysis, roi=state.game_roi)
                if _gs_movie.is_movie:
                    logger.info(">>> [GoldSwipe] スワイプ後 MOVIE 検出 → スワイプ完了")
                    state.current_scene = "MOVIE"
                elif _gs_adv.is_adv:
                    logger.info(">>> [GoldSwipe] スワイプ後 ADV 検出 → スワイプ完了")
                    state.current_scene = "ADV"
                else:
                    # OCR でテキスト増加チェック (UI出現)
                    _gs_ocr = run_ocr(_gs_analysis)
                    if len(_gs_ocr) >= 5:
                        logger.info(">>> [GoldSwipe] スワイプ後 UI出現 (OCR %d件) → スワイプ完了",
                                    len(_gs_ocr))
                    else:
                        logger.info(">>> [GoldSwipe] スワイプ後 シーン変化なし → 次ループで継続")
            return _gs_action, BATTLE_WAIT

    # ─── 【最優先 #0-ab】HSV金枠ボタン検出 → 中心タップ (Type B) ───
    # バトルチュートリアルで指アイコンが金枠ハイライトボタンを指している場面。
    # OCR が "隣接攻撃" "必殺技" を検出し、かつ右半分に金枠ボタンがある場合に発火。
    # DL中はゲージバーを金枠と誤検出するためスキップ
    _battle_tut_kws = ["隣接攻撃", "必殺技", "巫殺技", "ATTACKER", "通常攻撃"]
    _is_battle_tut_context = any(kw in joined for kw in _battle_tut_kws)
    # バトルUI確認済みの場合はフッター外GoldBtnをスキップ → Glow SM (フッター) に委ねる
    if analysis_path is not None and _is_battle_tut_context and not ctx.in_battle_ctx and not state.download_active:
        _gold_btn = find_gold_button(analysis_path)
        if _gold_btn:
            _bx, _by = _gold_btn[0], _gold_btn[1]
            logger.info(">>> [GoldBtn] 金枠ボタン検出 → tap(%d,%d)", _bx, _by)
            _base_ph_gb = compute_phash(analysis_path)
            tap_device(_bx, _by, state, "GOLD_BTN_TAP")
            _new_path_gb, _new_w_gb, _new_h_gb, _ = take_screenshot()
            try:
                _new_ph_gb = compute_phash(_new_path_gb)
            except Exception:
                _new_ph_gb = None
            _new_analysis_gb = prepare_analysis_image(_new_path_gb, _new_w_gb, _new_h_gb) if _new_path_gb else None
            if (not _base_ph_gb or not _new_ph_gb or
                    phash_distance(_base_ph_gb, _new_ph_gb) < PHASH_THRESHOLD):
                # 変化なし → OCR再取得して近傍テキスト中心でリトライ (最大2回)
                _gb_retried = False
                if _new_analysis_gb:
                    _retry_ocr = run_ocr(_new_analysis_gb)
                    for _retry_e in _retry_ocr:
                        _rc = _retry_e.get("center", (0, 0))
                        _rt = _retry_e.get("text", "")
                        if (len(_rt) >= 2 and abs(_rc[0] - _bx) < 150 and abs(_rc[1] - _by) < 150
                                and _rc[1] < H * 0.85):
                            logger.info(">>> [GoldBtn] phash変化なし → OCRリトライ '%s'(%d,%d)",
                                        _rt, _rc[0], _rc[1])
                            tap_device(_rc[0], _rc[1], state, "GOLD_BTN_TAP_OCR_RETRY")
                            _gb_retried = True
                            break
                if not _gb_retried:
                    # OCR近傍テキストなし → Y方向に±20px探索 (3回)
                    for _gb_dy in [20, -20, 40]:
                        _ry = _by + _gb_dy
                        if 0 < _ry < H:
                            logger.info(">>> [GoldBtn] OCRリトライ失敗 → Y%+dpx (%d,%d)",
                                        _gb_dy, _bx, _ry)
                            tap_device(_bx, _ry, state, "GOLD_BTN_TAP_Y_RETRY")
                            time.sleep(0.3)
                            _retry_ss, _, _, _ = take_screenshot()
                            try:
                                _retry_ph = compute_phash(_retry_ss)
                            except Exception:
                                _retry_ph = None
                            if (_retry_ph and _base_ph_gb and
                                    phash_distance(_base_ph_gb, _retry_ph) >= PHASH_THRESHOLD):
                                break
            return "GOLD_BTN_TAP", BATTLE_WAIT

    # ─── 【#0-a】個別テンプレートマッチング ───
    # 指テンプレ+金枠は finger_priority.py (Phase 1.5) に移動済み。
    # ここではスワイプ、ダイアログ▷、動画スキップ、マップ矢印のみ処理する。
    if analysis_path is not None:
        asset_hit = None  # (cx, cy, action, (bx, by, bw, bh)) or None

        # --- 1. スワイプ指テンプレ (SWIPE_UP) ---
        _swipe_m = ASSET_MANAGER.match_single("tutorial_swipe_finger", analysis_path)
        if _swipe_m and _swipe_m[2] >= 0.82:
            asset_hit = (_swipe_m[0], _swipe_m[1], "SWIPE_UP", (0, 0, 0, 0))

        # --- 2. チュートリアルダイアログ ▷ (ASSET_TUTORIAL_DIALOG_NEXT) ---
        if not asset_hit:
            _dnext_m = ASSET_MANAGER.match_single("tutorial_dialog_next", analysis_path)
            if _dnext_m and _dnext_m[2] >= 0.91:
                # 「矢印をタップ」画面では誤マッチ → #2-a MAP_ARROW に委譲
                if any("矢印を" in t for t in texts):
                    logger.info(">>> [Asset] DIALOG_NEXT を抑制 (矢印をタップ画面 → #2-a に委譲)")
                else:
                    # × ボタンが見える場合は最終ページ → × をタップして閉じる
                    _close = ASSET_MANAGER.match_single("close_btn", analysis_path)
                    _close = _close if (_close and _close[2] >= 0.60) else None
                    if _close:
                        logger.info("[DIALOG_NEXT] × ボタン検出 (%.2f) → 最終ページ、× タップ (%d,%d)",
                                    _close[2], _close[0], _close[1])
                        tap_device(_close[0], _close[1], state, "DIALOG_NEXT_CLOSE")
                        return "DIALOG_NEXT_CLOSE", 1.0
                    # スタック検出
                    _cnt = getattr(state, "_dialog_next_stall_count", 0) + 1
                    state._dialog_next_stall_count = _cnt
                    if _cnt >= 3:
                        _close_x, _close_y = roi_to_device(int(W * 0.94), int(H * 0.13), state.game_roi)
                        logger.warning("[DIALOG_NEXT] %d 回連続同一画面 → 上端×エリア (%d,%d) タップ",
                                       _cnt, _close_x, _close_y)
                        tap_device(_close_x, _close_y, state, "DIALOG_NEXT_CORNER_CLOSE")
                        state._dialog_next_stall_count = 0
                        return "DIALOG_NEXT_CORNER_CLOSE", 1.5
                    asset_hit = (_dnext_m[0], _dnext_m[1], "ASSET_TUTORIAL_DIALOG_NEXT", (0, 0, 0, 0))

        # --- 4. 動画スキップ (MOVIE_SKIP_TEXT) ---
        if not asset_hit:
            _skip_m = ASSET_MANAGER.match_single("movie_skip", analysis_path)
            if _skip_m and _skip_m[2] >= 0.70:
                _title_kws = ["MAGIA", "EXEDRA", "TAP", "START", "サポート"]
                _menu_kws = ["光の間", "ショップ", "ガチャ", "ガシャ", "交換所",
                             "パーティ", "クエスト", "クエス", "マップ", "レイヤ"]
                _menu_hits = sum(1 for kw in _menu_kws if any(kw in t for t in texts))
                if any(kw in joined for kw in _title_kws):
                    logger.info("[Asset] MOVIE_SKIP_TEXT をタイトル画面で抑制")
                elif _menu_hits >= 2:
                    logger.info("[Asset] MOVIE_SKIP_TEXT をメニュー画面で抑制 (menu_kw=%d)", _menu_hits)
                else:
                    asset_hit = (_skip_m[0], _skip_m[1], "MOVIE_SKIP_TEXT", (0, 0, 0, 0))

        # --- 5. マップ矢印 → navigation.py (Phase 6) に統合済み ---

        # BATTLE_UPPER_GUARD: バトル中は上部テンプレマッチを除外
        if asset_hit and ctx.in_battle_ctx:
            _hit_cy = asset_hit[1]
            if _hit_cy < H * 0.4:
                logger.info("[Asset] '%s' をバトル中の上部マッチとして抑制 (y=%d < %d) (BATTLE_UPPER_GUARD)",
                            asset_hit[2], _hit_cy, int(H * 0.4))
                asset_hit = None

        if asset_hit:
            # DIALOG_NEXT 以外のアセットが検出された場合、スタックカウンタリセット
            if asset_hit[2] != "ASSET_TUTORIAL_DIALOG_NEXT":
                state._dialog_next_stall_count = 0
            cx, cy, action, _asset_region = asset_hit
            # スワイプ系アクションの処理
            if action == "SWIPE_UP":
                # 安全ネット: ダイアログKWまたは背景ぼかしがあればポップアップ上のスワイプ誤発火を防止
                # ただし OCR 0件なら本物のポップアップではない (チェッカー柄スワイプシーン等)
                _swipe_skip = any(kw in joined for kw in _DIALOG_FIRST_KWS)
                if not _swipe_skip and analysis_path is not None and len(texts) >= 2:
                    _blur_img = imread_cached(analysis_path)
                    if _blur_img is not None:
                        _bH, _bW = _blur_img.shape[:2]
                        if detect_background_blur(_blur_img, _bH, _bW):
                            _swipe_skip = True
                            logger.info("[SWIPE_UP] 背景ぼかし+OCR%d件 → ポップアップ上のスワイプを回避", len(texts))
                if not _swipe_skip:
                    tmpl_meta = ASSET_MANAGER._templates.get("tutorial_swipe_finger", {})
                    # ratio ベース座標 (解像度非依存)
                    _sw_fx_r = tmpl_meta.get("swipe_from_x_ratio", 0.691)
                    _sw_fy_r = tmpl_meta.get("swipe_from_y_ratio", 0.806)
                    _sw_tx_r = tmpl_meta.get("swipe_to_x_ratio", 0.691)
                    _sw_ty_r = tmpl_meta.get("swipe_to_y_ratio", 0.069)
                    sx = int(W * _sw_fx_r)
                    sy = int(H * _sw_fy_r)
                    ex = int(W * _sw_tx_r)
                    ey = int(H * _sw_ty_r)
                    dur = tmpl_meta.get("swipe_duration_ms", 10000)
                    # チェッカー柄シーンが終わるまで繰り返しスワイプ
                    state._in_checker_walk = True
                    _max_repeat = tmpl_meta.get("max_repeat", 10)
                    _sw_prev_ph = compute_phash(analysis_path) if analysis_path else ""
                    for _sw_i in range(_max_repeat):
                        logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms (repeat %d/%d)",
                                    sx, sy, ex, ey, dur, _sw_i + 1, _max_repeat)
                        swipe_device(sx, sy, ex, ey, dur, state=state, desc="SWIPE_UP_ASSET")
                        # スワイプ後にシーン変化を確認
                        _sw_img, _sw_w, _sw_h, _ = take_screenshot()
                        if _sw_img:
                            _sw_analysis = prepare_analysis_image(_sw_img, _sw_w, _sw_h)
                            # phash で大きな画面変化を検出 → 即終了
                            _sw_cur_ph = compute_phash(_sw_analysis)
                            if _sw_prev_ph and _sw_cur_ph:
                                _sw_ph_dist = phash_distance(_sw_prev_ph, _sw_cur_ph)
                                if _sw_ph_dist >= PHASH_THRESHOLD * 2:
                                    logger.info("[SWIPE_UP] phash大変化 (dist=%d) → スワイプ終了", _sw_ph_dist)
                                    break
                            _sw_prev_ph = _sw_cur_ph
                            # スワイプ指テンプレが消えた or チェッカー床でなくなった → 終了
                            _sw_finger = ASSET_MANAGER.match_single("tutorial_swipe_finger", _sw_analysis)
                            _sw_walk = is_tutorial_walk_scene(_sw_analysis)
                            if (not _sw_finger or _sw_finger[2] < 0.70) and not _sw_walk:
                                logger.info("[SWIPE_UP] シーン変化検出 → スワイプ終了")
                                break
                    return "SWIPE_UP", 1.5
                else:
                    logger.info(">>> [SWIPE_UP] ポップアップ上の誤検出 → アセットマッチ破棄")
                    return None  # handle_tutorial を抜けて次ハンドラに委譲
            # その他のアセットアクション: タップして return
            tap_device(cx, cy, state, action)
            return action, 0.5

    return None
