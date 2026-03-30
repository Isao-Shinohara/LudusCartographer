"""
ap/handlers/tutorial.py — チュートリアル系ハンドラ

名前入力、指+金枠ボタン、チュートリアルスワイプ、アセットマッチング、
チュートリアルポップアップ、報酬/強化ポップアップ等を処理する。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H,
    BATTLE_WAIT, PHASH_THRESHOLD,
    _DIALOG_FIRST_KWS, _BATTLE_UI_KWS,
    _CLOSE_BTN_OFFSET,
)
from tools.ap.device import adb, tap_device, swipe_device, take_screenshot
from tools.ap.helpers import (
    has_any, has_text, log_milestone, watchdog_recover,
)
from tools.ap.image_proc import (
    ASSET_MANAGER,
    roi_to_device,
    is_tutorial_walk_scene,
    detect_tutorial_gold_swipe,
    find_gold_button,
    smart_tap_button,
    detect_dialog,
    process_paging_dialog,
    count_page_dots,
    detect_movie_scene,
    detect_adv_scene,
    prepare_analysis_image,
    detect_background_blur,
    detect_dialog_corners,
    imread_cached,
)
from lc.ocr import run_ocr
from lc.utils import compute_phash, phash_distance

logger = logging.getLogger("auto_pilot")


def handle_tutorial(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """チュートリアル系の画面を処理するハンドラ。

    - プレイヤー名入力ダイアログ (#0-b-name)
    - 指+金枠ボタン (FINGER_GOLD_TAP)
    - メインクエストボタン / クエストマップノード
    - チュートリアル歩行シーン / Gold swipe (#0-walk, #0-aa)
    - 金枠ボタンタップ (バトルチュートリアル) (#0-ab)
    - アセットマッチハンドラ (#0-a)
    - チュートリアルポップアップ セカンダリ (#0)
    - プレイヤー名入力 (重複セーフネット) (#0-b-extra)
    - 各種ポップアップ (確認, 報酬, ガチャ結果, クローズ, プレゼントボックス, カルーセル, ガイド)

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
            _ni_cx, _ni_cy = roi_to_device(_ni_ok["center"][0], int(H * 0.78), state.game_roi)
            logger.info(">>> 【名前入力 OK】 入力済み='%s' → ROI補正(%d,%d) タップ", _ni_name_texts[0], _ni_cx, _ni_cy)
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
            _nf_x, _nf_y = roi_to_device(_nf_ocr_x, _nf_ocr_y, state.game_roi)
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d) [OCR座標]", _nf_x, _nf_y)
            tap_device(_nf_x, _nf_y, state, "NAME_INPUT_FOCUS")
            adb("shell input text MadoDora")
            time.sleep(0.2)
            adb("shell input keyevent 66")
            logger.info(">>> 【名前入力】 'MadoDora' 入力完了 → OK タップ待ち")
            return "NAME_INPUT_TEXT", 1.5

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
            # テキストラベルの中心をそのままタップ (ノードのヒットボックスはラベル領域も含む)
            logger.info(">>> 【クエストマップ】 ノード '%s' (%d,%d) タップ",
                        _quest_node["text"], _qx, _qy)
            tap_device(_qx, _qy, state, "QUEST_NODE_TAP")
            return "QUEST_NODE_TAP", 2.0
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
            _bx, _by = _gold_btn
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

        # --- 5. マップ矢印 (MAP_ARROW_TAP) --- require_ocr: 矢印をタップ
        if not asset_hit and any("矢印をタップ" in t for t in texts):
            _arrow_m = ASSET_MANAGER.match_single("map_arrow", analysis_path)
            if _arrow_m and _arrow_m[2] >= 0.65:
                asset_hit = (_arrow_m[0], _arrow_m[1], "MAP_ARROW_TAP", (0, 0, 0, 0))

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
                    for _sw_i in range(_max_repeat):
                        logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms (repeat %d/%d)",
                                    sx, sy, ex, ey, dur, _sw_i + 1, _max_repeat)
                        swipe_device(sx, sy, ex, ey, dur, state=state, desc="SWIPE_UP_ASSET")
                        # スワイプ後にシーン変化を確認
                        _sw_img, _sw_w, _sw_h, _ = take_screenshot()
                        if _sw_img:
                            _sw_analysis = prepare_analysis_image(_sw_img, _sw_w, _sw_h)
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

    # ─── 【優先 #0】チュートリアルポップアップ セカンダリセーフネット ───
    # #0-DIALOG (形状ベース) が失敗した場合の OCR キーワードによるバックアップ。
    # キーワードリストは _DIALOG_FIRST_KWS (定数) と共有して管理。
    # BATTLE シーンではロール名 (DEFENDER 等) が常時表示されるため誤検出を防止
    _in_battle_ctx = state.current_scene == "BATTLE" or getattr(state, "_from_battle", False)
    pre_popup = None if _in_battle_ctx else has_any(ocr, list(_DIALOG_FIRST_KWS))
    if pre_popup:
        # 四隅テンプレで本物のダイアログか確認 (ホーム画面等の誤検出防止)
        _corners = ctx.has_dialog_corners if ctx.has_dialog_corners is not None else (detect_dialog_corners(analysis_path) if analysis_path else False)
        if analysis_path and not _corners:
            logger.info("[PRE_POPUP] 四隅テンプレなし → ダイアログではない、スキップ (kw='%s')",
                        pre_popup["text"][:10])
            pre_popup = None
    if pre_popup:
        state.pre_popup_tap_count += 1
        # ── ページドット検出: ドット≥2 → ページングが必要 (× 即タップではなく全ページ走査) ──
        _popup_dots = count_page_dots(analysis_path) if analysis_path else 0
        # ── テンプレートマッチングで ▷/× を優先検出 ──
        _nav = detect_dialog(analysis_path, W, H) if analysis_path else None
        if _nav:
            _nav_type, cx, cy = _nav
            if _nav_type == "close" and _popup_dots < 2:
                # ページなし → × で即閉じ
                logger.info(">>> 【チュートリアルポップアップ】 '%s' ×→(%d,%d) [template] dots=%d",
                            pre_popup["text"][:10], cx, cy, _popup_dots)
                tap_device(cx, cy, state, "PRE_POPUP_TAP")
                return "TUTORIAL_POPUP", 1.0
            if _nav_type == "close" and _popup_dots >= 2:
                # ページあり + × 検出 → ▷ は固定座標で走査後 × で閉じ
                logger.info(">>> 【チュートリアルポップアップ→PAGING】 '%s' dots=%d, × 検出→先にページ走査",
                            pre_popup["text"][:10], _popup_dots)
                _arr_x, _arr_y = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
                _pg_result = process_paging_dialog(
                    analysis_path, W, H, state,
                    initial_dlg=("next", _arr_x, _arr_y),
                    ocr_texts=texts,
                )
                state.pre_popup_tap_count = 0
                return _pg_result, 1.0
            # ▷ 検出 → ページングダイアログ: process_paging_dialog で全ページ一括処理
            # (単発タップだとページ2以降の OCR が _DIALOG_FIRST_KWS に不一致 → スタックする)
            logger.info(">>> 【チュートリアルポップアップ→PAGING】 '%s' ▷(%d,%d) → 全ページ走査開始",
                        pre_popup["text"][:10], cx, cy)
            _pg_result = process_paging_dialog(
                analysis_path, W, H, state,
                initial_dlg=(_nav_type, cx, cy),
                ocr_texts=texts,
            )
            state.pre_popup_tap_count = 0
            return _pg_result, 1.0
        # ── フォールバック: 固定座標 → process_paging_dialog に委譲 ──
        # テンプレートなしでも ▷ 位置を推定してページング処理
        _arr = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)   # ▷ 矢印
        _cls = roi_to_device(int(W * 0.98), int(H * 0.056), state.game_roi)  # × ボタン
        # まず ▷ をタップして反応を見る → process_paging_dialog で全ページ処理
        logger.info(">>> 【チュートリアルポップアップ→PAGING(FB)】 '%s' ▷(%d,%d) dots=%d → 全ページ走査",
                    pre_popup["text"][:10], _arr[0], _arr[1], _popup_dots)
        _pg_result = process_paging_dialog(
            analysis_path, W, H, state,
            initial_dlg=("next", _arr[0], _arr[1]),
            ocr_texts=texts,
        )
        state.pre_popup_tap_count = 0
        return _pg_result, 1.0

    # ─── 【最優先 #0-b-extra】プレイヤー名入力ダイアログ ───
    # 「プレイヤー名を入力してください」→ 名前入力 → OKタップ
    # 注意: OCR で "OK" の center が y≈593 と検出されるが、
    #        実際のボタンヒットゾーンはゴールデンエリア y≈555-575 (実測)
    name_input = has_text(ocr, "プレイヤー名を入力", min_conf=0.3)
    if name_input:
        # 入力済みテキストを確認 (プレースホルダー・UI テキスト以外のひらがな/英字)
        ui_words = {"プレイヤー名を入力してください", "プレイヤー名は", "変更後3日間", "名前入力", "OK"}
        name_texts = [t for t in texts if t not in ui_words and len(t) >= 2
                      and not t.startswith("プレイヤー") and "/" not in t]
        ok_item = next(
            (item for item in ocr if "OK" in item.get("text", "") and item["center"][1] > H * 0.5),
            None
        )
        if name_texts and ok_item:
            # 名前入力済み → OKタップ (ROI補正: OCR-X + 比率Y=H*0.78)
            cx, cy = roi_to_device(ok_item["center"][0], int(H * 0.78), state.game_roi)
            logger.info(">>> 【名前入力 OK】 入力済み='%s' → ROI補正(%d,%d) タップ", name_texts[0], cx, cy)
            tap_device(cx, cy, state, "NAME_INPUT_OK")
            return "NAME_INPUT_OK", 2.0
        elif ok_item:
            # テキストフィールドのプレースホルダー「プレイヤー名を入力」を探す
            _field_item = next(
                (item for item in ocr
                 if item.get("text", "").strip() == "プレイヤー名を入力"),
                name_input
            )
            _nf_ocr_x, _nf_ocr_y = _field_item["center"]
            _nf_x, _nf_y = roi_to_device(_nf_ocr_x, _nf_ocr_y, state.game_roi)
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d) [OCR座標]", _nf_x, _nf_y)
            tap_device(_nf_x, _nf_y, state, "NAME_INPUT_FOCUS")
            adb("shell input text MadoDora")
            time.sleep(0.2)
            adb("shell input keyevent 66")
            logger.info(">>> 【名前入力】 'MadoDora' 入力完了 → OK タップ待ち")
            return "NAME_INPUT_TEXT", 1.5

    # ─── 【最優先 #0-b】報酬/強化結果ポップアップを即時処理 (ブロブ誤検出防止) ───
    # 「以下の内容でよろしいですか」確認ダイアログ → SmartTap で OK 物理中心をタップ
    confirm_dlg = has_text(ocr, "以下の内容でよろしいですか", min_conf=0.3)
    if confirm_dlg:
        ok_bottom = next(
            (item for item in ocr
             if "OK" in item.get("text", "") and item["center"][1] > H * 0.6),
            None
        )
        if ok_bottom:
            ocr_cx, ocr_cy = ok_bottom["center"]
        else:
            ocr_cx, ocr_cy = roi_to_device(
                int(W * 0.70), int(H * 0.88), state.game_roi)  # 比率ベースフォールバック
        cx, cy = smart_tap_button(analysis_path, ocr_cx, ocr_cy, ocr_items=ocr)
        logger.info(">>> 【確認ダイアログ】 SmartTap OK (%d,%d)", cx, cy)
        tap_device(cx, cy, state, "CONFIRM_DIALOG_OK")
        return "CONFIRM_DIALOG_OK", 1.0

    # 「タップして次へ」: 報酬獲得画面の次へ進む
    tap_next = has_text(ocr, "タップして次へ", min_conf=0.3)
    if tap_next:
        cx, cy = tap_next["center"]
        logger.info(">>> 【報酬/次へ】 'タップして次へ' (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "REWARD_NEXT")
        return "REWARD_NEXT", 1.0

    # ガチャ結果確認画面: 「限界突破」+「確定で獲得」→ OK ボタン想定位置タップ
    # (OCR が "OK" を拾えない低解像度を考慮し、テキスト存在だけで判定)
    _gacha_limit = has_text(ocr, "限界突破", min_conf=0.2)
    _gacha_kakutei = has_text(ocr, "確定", min_conf=0.2) or has_text(ocr, "獲得", min_conf=0.2)
    if _gacha_limit and _gacha_kakutei:
        _ok_x, _ok_y = roi_to_device(int(W * 0.41), int(H * 0.89), state.game_roi)
        logger.info(">>> 【ガチャ結果確認】 限界突破+確定/獲得 検出 → OK想定位置 (%d,%d) タップ", _ok_x, _ok_y)
        tap_device(_ok_x, _ok_y, state, "GACHA_RESULT_OK")
        return "GACHA_RESULT_OK", 1.5

    # 限界突破/強化完了/レベルアップ系ポップアップ → 右上 × ボタンで閉じる
    close_popup_kws = ["限界突破", "強化完了", "レベルアップ", "称号獲得", "エピソード解放",
                       "ランクアップ", "新しいコンテンツ", "アンロック",
                       "マギアボックス", "ミッション達成", "デイリーミッション",
                       "ログインボーナス", "初心者ログイン", "キャンペーン"]

    # カルーセル型チュートリアルポップアップ (「メインクエストをPLAYして」等の複数ページ説明)
    # 閉じるボタン: ポップアップフレーム右上 (1430, 88) — 実測 2026-03-05
    carousel_popup_kws = ["メインクエストをPLAY", "ピュエラピクトゥーラ", "POWER UP"]
    carousel_match = has_any(ocr, carousel_popup_kws)
    if carousel_match:
        # 最終ページへ移動 (右ナビゲーション × 6) → フレーム右上 × をタップ
        _cn_x, _cn_y = roi_to_device(int(W * 0.96), int(H * 0.5), state.game_roi)
        for _ in range(6):
            tap_device(_cn_x, _cn_y, state, "CAROUSEL_NAV_RIGHT", rapid=True)
        close_x, close_y = roi_to_device(int(W * 0.94), int(H * 0.12), state.game_roi)
        logger.info(">>> 【カルーセルポップアップ】 '%s' → フレーム右上 (%d,%d) タップ",
                    carousel_match["text"][:10], close_x, close_y)
        tap_device(close_x, close_y, state, "CAROUSEL_CLOSE")
        return "CLOSE_POPUP", 1.0
    # ─── プレゼントボックス画面: 「一括受取」タップ or BACK で戻る ───
    # 指テンプレ検出時 (pre_dialog_finger) はスキップ → Asset Match の指+金枠フローに委譲
    # OCR が「プレセントポックス」等に誤読するケースも拾う
    _present_box = (has_text(ocr, "プレゼントボックス", min_conf=0.2)
                    or (has_any(ocr, ["プレセント", "プレゼント"], min_conf=0.2)
                        and has_any(ocr, ["ボックス", "ポックス", "ボック"], min_conf=0.2)))
    if _present_box and not ctx.pre_dialog_finger:
        _bulk_receive = has_text(ocr, "一括受取", min_conf=0.3)
        if _bulk_receive:
            _br_x, _br_y = _bulk_receive["center"]
            logger.info(">>> 【プレゼントボックス】 一括受取 (%d,%d) タップ", _br_x, _br_y)
            tap_device(_br_x, _br_y, state, "PRESENT_BULK_RECEIVE")
            return "PRESENT_BULK_RECEIVE", 2.0
        else:
            # 一括受取ボタンが見えない → BACK キーで戻る
            logger.info(">>> 【プレゼントボックス】 一括受取なし → BACK で戻る")
            adb("shell input keyevent KEYCODE_BACK")
            return "PRESENT_BOX_BACK", 1.0

    close_popup = has_any(ocr, close_popup_kws)
    _corners2 = ctx.has_dialog_corners if ctx.has_dialog_corners is not None else (detect_dialog_corners(analysis_path) if analysis_path else False)
    if close_popup and analysis_path and not _corners2:
        logger.info("[CLOSE_POPUP] 四隅テンプレなし → ダイアログではない、スキップ (kw='%s')",
                    close_popup["text"][:10])
        close_popup = None
    if close_popup:
        # CLOSE_POPUP スタック脱出: 8回以上累計失敗 → BACK キーで閉じる
        if state.pre_popup_tap_count >= 8:
            logger.warning(">>> 【%s ポップアップ】 × が8回空振り → BACK キーで脱出",
                           close_popup["text"][:6])
            try:
                adb("shell input keyevent KEYCODE_BACK")
            except Exception as _e:
                logger.debug("[CLOSE_POPUP] BACK キー送信例外: %s", _e)
            state.pre_popup_tap_count = 0
            return "CLOSE_POPUP_BACK", 1.0
        # テンプレートマッチングで正確な × 位置を取得 (固定座標より優先)
        _close_match = ASSET_MANAGER.match_single("close_btn", analysis_path) if analysis_path else None
        if _close_match and _close_match[2] >= 0.60:
            close_x, close_y = _close_match[0], _close_match[1]
            logger.info(">>> 【%s ポップアップ】 → × テンプレ(%.2f) (%d,%d) タップ",
                        close_popup["text"][:6], _close_match[2], close_x, close_y)
        else:
            close_x = W - _CLOSE_BTN_OFFSET  # 右上 × ボタン
            close_y = _CLOSE_BTN_OFFSET
            logger.info(">>> 【%s ポップアップ】 → × 固定座標 (%d,%d) タップ",
                        close_popup["text"][:6], close_x, close_y)
        state.pre_popup_tap_count += 1
        tap_device(close_x, close_y, state, f"CLOSE_POPUP_{close_popup['text'][:6]}")
        return "CLOSE_POPUP", 1.0

    # 「〜してみましょう」型チュートリアルガイド + ブロブスタック → × で閉じる
    # 例: "今回は自動編成をしてみましょう。" が表示されたまま動かない場合
    if state.blob_same_count >= 5:
        tutorial_guide = (has_text(ocr, "てみましょう", min_conf=0.3) or
                          has_text(ocr, "しましょう", min_conf=0.3))
        if tutorial_guide and not ctx.in_battle_ctx:
            close_x = W - _CLOSE_BTN_OFFSET  # 右上 × ボタン
            close_y = _CLOSE_BTN_OFFSET
            logger.info(">>> 【チュートリアルガイド スタック】 '%s' → × (%d,%d) タップ",
                        tutorial_guide["text"][:10], close_x, close_y)
            tap_device(close_x, close_y, state, "TUTORIAL_GUIDE_CLOSE")
            state.blob_same_count = 0
            return "CLOSE_POPUP", 1.0

    return None
