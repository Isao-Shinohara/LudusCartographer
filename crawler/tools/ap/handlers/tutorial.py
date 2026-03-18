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
    _DIALOG_FIRST_KWS, _BATTLE_UI_KWS, _BATTLE_CORE_KWS,
    _CLOSE_BTN_OFFSET,
)
from tools.ap.device import adb, tap_device, swipe_device, take_screenshot
from tools.ap.helpers import (
    has_any, has_text, log_milestone, watchdog_recover, text_core_center,
)
from tools.ap.image_proc import (
    ASSET_MANAGER,
    roi_to_device,
    find_gold_frame_near,
    is_tutorial_walk_scene,
    detect_tutorial_gold_swipe,
    detect_tutorial_gold_button_tap,
    detect_white_hand_pointer,
    smart_tap_button,
    detect_dialog,
    process_paging_dialog,
    count_page_dots,
    detect_text_input_area,
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
            _nf_x, _nf_y = roi_to_device(int(W * 0.46), int(H * 0.58), state.game_roi)
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d)", _nf_x, _nf_y)
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
    if (analysis_path is not None
            and len(texts) <= 2
            and is_tutorial_walk_scene(analysis_path)):
        _sx = int(ANALYSIS_W * 0.5)
        _fy = ANALYSIS_H - 10   # 画面最下端
        _ty = 10                 # 画面最上端
        _dur = 10000
        logger.info(">>> [TutorialWalk] 白黒背景検出 (OCR %d件) → 上ホールドスワイプ (全画面)", len(texts))
        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc="TutorialWalk_UP")
        return "GOLD_SWIPE_UP", BATTLE_WAIT

    # チュートリアル3D移動シーン(チェッカー床/階段/廊下)で発火。
    # phash監視: スワイプ後2s待機 → 変化なければ再実行 (最大2回)
    # バトルUI（通常攻撃・単体攻撃・WAVE・Turn）が見えるとき はバトル中なのでスキップ
    _is_battle_ui = any(kw in joined for kw in _BATTLE_UI_KWS)
    _has_dialog_kw = any(kw in joined for kw in _DIALOG_FIRST_KWS)
    _home_swipe_guard_kws = ["光の間", "ショップ", "ガチャ", "ガシャ", "パーティ",
                             "クエスト", "ユニオン", "プレイヤーマッチ"]
    _is_home_screen = sum(1 for h in _home_swipe_guard_kws if h in joined) >= 3
    if analysis_path is not None and not _is_battle_ui and not ctx.adv_result.is_adv and not _has_dialog_kw and not _is_home_screen:
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
    if analysis_path is not None and _is_battle_tut_context and not ctx.is_battle_early and not state.download_active:
        _gold_btn = detect_tutorial_gold_button_tap(analysis_path, right_half_only=True)
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

    # ─── 【最優先 #0-a】テンプレートマッチング (Asset Match) — 最速 ~0.1s ───
    # チュートリアル中は指アイコン検出(TAP_HIGHLIGHTED_NAV/SWIPE_UP)が最高優先。
    # 指アイコン検出後 → 金色ハイライト要素をタップ。
    # 次優先: セリフ/ADVテキスト確認 (後続の#0/#3-ADV処理)
    if analysis_path is not None:
        asset_hit = ASSET_MANAGER.match(analysis_path, ocr_texts=texts)
        # DIALOG_NAV_RIGHT: ページ送りダイアログの ▷ ボタン
        # ロジック: ▷ が見える限りタップしてページ送り。
        # 最終ページに到達すると × ボタンが出現するので、× を検出したら閉じる。
        if asset_hit and asset_hit[2] == "DIALOG_NAV_RIGHT":
            # 指ブロブ検出中 → チュートリアルガイダンス中の可能性が高い
            # バトル速度ボタン等の ⏭ がダイアログ ▷ と誤検出されるためスキップ
            if ctx.pre_dialog_finger:
                logger.info("[Asset] DIALOG_NAV_RIGHT を指ブロブ検出中のため抑制 → 指+金枠ハンドラへ")
                asset_hit = None
            else:
                # BLUR_GUARD: ダイアログ ▷ は必ず背景ぼかしを伴う
                # バトル画面等の非ダイアログ画面での誤検出を排除
                # (Asset Match で DIALOG_NAV_RIGHT 検出済みのため、ぼかしのみ確認で十分)
                _blur_img = imread_cached(analysis_path) if analysis_path else None
                if _blur_img is not None:
                    _bH, _bW = _blur_img.shape[:2]
                    if not detect_background_blur(_blur_img, _bH, _bW):
                        logger.info("[Asset] DIALOG_NAV_RIGHT を背景ぼかしなしのため抑制 (BLUR_GUARD)")
                        asset_hit = None
            if asset_hit:
                # ガード通過 → × ボタン検出 → 最終ページなら × をタップして閉じる
                _nav_close = ASSET_MANAGER.match_single("close_btn", analysis_path)
                if _nav_close and _nav_close[2] >= 0.60:
                    logger.info("[DIALOG_NAV] × ボタン検出 (%.2f) → 最終ページ、× タップ (%d,%d)",
                                _nav_close[2], _nav_close[0], _nav_close[1])
                    tap_device(_nav_close[0], _nav_close[1], state, "DIALOG_NAV_CLOSE")
                    return "DIALOG_NAV_CLOSE", 1.0
                # ▷ タップでページ送り (× が出るまで繰り返す)
        # 「矢印をタップ」画面では DIALOG_NEXT 誤マッチを無視 → #2-a MAP_ARROW に委譲
        elif asset_hit and asset_hit[2] == "ASSET_TUTORIAL_DIALOG_NEXT":
            if any("矢印を" in t for t in texts):
                logger.info(">>> [Asset Match] DIALOG_NEXT を抑制 (矢印をタップ画面 → #2-a に委譲)")
                asset_hit = None
        # ホーム画面では FINGER_TEMPLATE 偽陽性を抑制 → ホーム検出ハンドラに委譲
        elif asset_hit and asset_hit[2] == "FINGER_TEMPLATE":
            _home_kws_check = ["光の間", "ショップ", "ガチャ", "ガシャ", "マップ", "レイヤ"]
            _home_kw_hits = sum(1 for kw in _home_kws_check
                                if any(kw in t or t in kw for t in texts))
            if _home_kw_hits >= 2:
                logger.info("[Asset] FINGER_TEMPLATE をホーム画面で抑制 (home_kw=%d)", _home_kw_hits)
                asset_hit = None
        if asset_hit:
            cx, cy, action, _asset_region = asset_hit
            # Text-Core: テンプレートマッチ領域 + OCR でテキスト中心優先座標を取得
            _tc_x, _tc_y = text_core_center(_asset_region, ocr, label=f"Asset:{action}")
            if (_tc_x, _tc_y) != (cx, cy):
                logger.info(">>> [Asset Match] '%s' → Template(%d,%d) → TextCore(%d,%d)",
                            action, cx, cy, _tc_x, _tc_y)
            else:
                logger.info(">>> [Asset Match] '%s' → (%d,%d)", action, cx, cy)
            cx, cy = _tc_x, _tc_y
            # スワイプ系アクションの処理
            if action == "SWIPE_UP":
                # 安全ネット: ダイアログKWが見えるときはポップアップ上のスワイプ誤発火を防止
                _swipe_skip = any(kw in joined for kw in _DIALOG_FIRST_KWS)
                if not _swipe_skip:
                    tmpl_meta = ASSET_MANAGER._templates.get("tutorial_swipe_pointer", {})
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
                    logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms", sx, sy, ex, ey, dur)
                    swipe_device(sx, sy, ex, ey, dur, state=state, desc="SWIPE_UP_ASSET")
                    return "SWIPE_UP", 1.5
                else:
                    logger.info(">>> [SWIPE_UP] ダイアログKW検出だが #0-DIALOG が None → 盲タップせず次ループへ")
            # チュートリアル指差し: 金色ハイライトされたUI要素を方向非依存で検出→タップ
            if action == "TAP_HIGHLIGHTED_NAV":
                # 白ハンドポインタ (テンプレートマッチ) で方向を取得
                _wh = detect_white_hand_pointer(analysis_path, threshold=0.85)
                _hand_pos = (cx, cy)
                _hand_dir = ""
                if _wh:
                    _hand_pos = (_wh[0], _wh[1])
                    _hand_dir = _wh[3]  # "up" or "down"
                _hx, _hy = _hand_pos
                tap_x, tap_y = cx, cy  # デフォルト

                # 【プライマリ】テンプレートマッチで指近傍のアイコンを検索
                # nav_back/back_arrow のみ。gold_frame_small は大きなアイコンの
                # 端にマッチして中心を外すため除外。
                _tmpl_found = False
                if analysis_path:
                    _search_r = 200
                    _aroi = (max(0, _hx - _search_r), max(0, _hy - _search_r),
                             _search_r * 2, _search_r * 2)
                    for _btn_name in ("nav_back", "back_arrow"):
                        _m = ASSET_MANAGER.match_single(
                            _btn_name, analysis_path, roi=_aroi)
                        if _m and _m[2] >= 0.65:
                            _ax, _ay = _m[0], _m[1]
                            # 方向フィルタ
                            if (_hand_dir == "up" and _ay > _hy + 30) or \
                               (_hand_dir == "down" and _ay < _hy - 30):
                                continue
                            tap_x, tap_y = _ax, _ay
                            _tmpl_found = True
                            logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → Asset '%s'(%d,%d) score=%.3f",
                                        _hx, _hy, _hand_dir, _btn_name, tap_x, tap_y, _m[2])
                            break

                # 【セカンダリ】テンプレ未検出 → 指の方向にある最近接OCRテキストをタップ (距離200px以内)
                if not _tmpl_found:
                    _MAX_HAND_OCR_DIST = 200
                    _ocr_found = False
                    if _hand_dir and ocr:
                        _dir_items = []
                        for item in ocr:
                            _tx, _ty = item["center"]
                            _dist = abs(_hx - _tx) + abs(_hy - _ty)
                            if _dist > _MAX_HAND_OCR_DIST:
                                continue
                            if _hand_dir == "up" and _ty < _hy:
                                _dir_items.append((_tx, _ty, _dist, item["text"]))
                            elif _hand_dir == "down" and _ty > _hy:
                                _dir_items.append((_tx, _ty, _dist, item["text"]))
                        if _dir_items:
                            _dir_items.sort(key=lambda d: d[2])
                            tap_x, tap_y = _dir_items[0][0], _dir_items[0][1]
                            _ocr_found = True
                            logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → OCR '%s'(%d,%d) dist=%d",
                                        cx, cy, _hand_dir, _dir_items[0][3], tap_x, tap_y, _dir_items[0][2])

                # 【フォールバック】テンプレもOCRも未検出 → 金枠検出
                if not _tmpl_found and not _ocr_found:
                    _gold = find_gold_frame_near(analysis_path, _hx, _hy,
                                                 search_radius=200) if analysis_path else None
                    # 方向フィルタ: 指の向きと逆方向の金枠は除外
                    if _gold:
                        _gx, _gy = _gold[0], _gold[1]
                        if (_hand_dir == "up" and _gy > _hy + 30) or \
                           (_hand_dir == "down" and _gy < _hy - 30):
                            logger.info(">>> [TAP_HIGHLIGHTED_NAV] 金枠(%d,%d) が指方向(%s)と逆 → 除外",
                                        _gx, _gy, _hand_dir)
                            _gold = None
                    if _gold:
                        tap_x, tap_y = _gx, _gy
                        logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → 金枠(%d,%d)",
                                    _hx, _hy, _hand_dir, tap_x, tap_y)
                    else:
                        tap_x, tap_y = smart_tap_button(
                            analysis_path, _hx, _hy, search_r=160, ocr_items=ocr)
                        logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → smart_tap(%d,%d)",
                                    _hx, _hy, _hand_dir, tap_x, tap_y)

                tap_device(tap_x, tap_y, state, "TAP_HIGHLIGHTED_NAV")
                return "TAP_HIGHLIGHTED_NAV", 1.0
            # ── NAME_INPUT_OK_TAP: 名前未入力(0/N)の場合は入力シーケンスへ ──
            if action == "NAME_INPUT_OK_TAP":
                _is_empty_field = any(re.match(r"^0/\d+$", t.strip()) for t in texts)
                if _is_empty_field:
                    _field_pos = detect_text_input_area(analysis_path, W, H, ocr_items=ocr)
                    if _field_pos:
                        _fx, _fy = _field_pos
                    else:
                        _fx, _fy = roi_to_device(int(W * 0.46), int(H * 0.58), state.game_roi)
                    logger.info(
                        ">>> [TEXT_INPUT_AREA] 空フィールド検出(0/N) → (%d,%d)タップ → adb input text",
                        _fx, _fy,
                    )
                    tap_device(_fx, _fy, state, "TEXT_INPUT_FOCUS")
                    time.sleep(0.3)
                    adb("shell input text MadoDora")
                    time.sleep(0.3)
                    # IME変換確定 (ENTER) → キーボード閉じる (BACK) → ダイアログOKタップ
                    adb("shell input keyevent 66")   # KEYCODE_ENTER: IME変換確定
                    time.sleep(0.2)
                    adb("shell input keyevent KEYCODE_BACK")  # keyboard dismiss
                    time.sleep(0.3)
                    # ダイアログOKボタンを直接タップ (テンプレート位置より下方)
                    _ok_x = roi_to_device(int(W * 0.50), int(H * 0.77), state.game_roi)
                    tap_device(_ok_x[0], _ok_x[1], state, "NAME_INPUT_OK_DIRECT")
                    logger.info(">>> [TEXT_INPUT_AREA] 'MadoDora' 入力 → 確定 → KB閉 → OK(%d,%d)", _ok_x[0], _ok_x[1])
                    return "TEXT_INPUT_NAME", 2.0
            # その他のアセットアクション: タップして return (fallthrough なし)
            # GACHA_OK 入力フリーズ検出: 連続タップで応答がない場合 force-stop 復帰
            if action == "GACHA_OK":
                # テンプレートマッチ座標はカード背景で不安定 → OCR "OK" テキスト中心を優先
                _ocr_ok = has_text(ocr, "OK", min_conf=0.3)
                if _ocr_ok:
                    _ok_cx, _ok_cy = _ocr_ok["center"]
                    logger.info("[GACHA_OK] OCR 'OK' center (%d,%d) 使用 (Template was %d,%d)",
                                _ok_cx, _ok_cy, cx, cy)
                    cx, cy = _ok_cx, _ok_cy
                state.gacha_total.tick()
                if state.gacha_total.stalled:
                    logger.warning("[GACHA_FREEZE] %d回タップ応答なし → Unity入力フリーズ → force-stop", state.gacha_total.count)
                    state.gacha_total.reset()
                    watchdog_recover(state)
                    return "GACHA_FREEZE_RECOVER", 3.0
            else:
                state.gacha_total.reset()
            tap_device(cx, cy, state, action)
            # GACHA_OK: テンプレートマッチ成功 = 既に結果一覧画面 → 短め待機で十分
            _asset_wait = 1.5 if action == "GACHA_OK" else 0.5
            return action, _asset_wait

    # ─── 【優先 #0】チュートリアルポップアップ セカンダリセーフネット ───
    # #0-DIALOG (形状ベース) が失敗した場合の OCR キーワードによるバックアップ。
    # キーワードリストは _DIALOG_FIRST_KWS (定数) と共有して管理。
    # BATTLE シーンではロール名 (DEFENDER 等) が常時表示されるため誤検出を防止
    pre_popup = None if state.current_scene == "BATTLE" else has_any(ocr, list(_DIALOG_FIRST_KWS))
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
                _arr_x, _arr_y = int(W * 0.91), int(H * 0.49)
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
            # 名前未入力 → テキストフィールドをタップして "MadoDora" 入力 → Enter → OK
            _nf_x, _nf_y = roi_to_device(int(W * 0.46), int(H * 0.58), state.game_roi)
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d)", _nf_x, _nf_y)
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
    _present_box = has_text(ocr, "プレゼントボックス", min_conf=0.3) or has_text(ocr, "プレゼントボックス", min_conf=0.2)
    if _present_box:
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
        is_battle_guide = any(kw in joined for kw in _BATTLE_CORE_KWS)
        if tutorial_guide and not is_battle_guide:
            close_x = W - _CLOSE_BTN_OFFSET  # 右上 × ボタン
            close_y = _CLOSE_BTN_OFFSET
            logger.info(">>> 【チュートリアルガイド スタック】 '%s' → × (%d,%d) タップ",
                        tutorial_guide["text"][:10], close_x, close_y)
            tap_device(close_x, close_y, state, "TUTORIAL_GUIDE_CLOSE")
            state.blob_same_count = 0
            return "CLOSE_POPUP", 1.0

    return None
