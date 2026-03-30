"""
ap/handlers/common.py — 共通ガードハンドラ

detect_and_act の最初に評価される共通ガード群:
  - ブラウザ脱出
  - MOVIE シーン待機
  - ダウンロード状態管理 / Loading 保護 / DL厳格判定
  - 確認ダイアログ (OK/Cancel)
  - Android 権限ダイアログ
  - 設定/Play Games ポップアップ
  - ご注意画面 (Notice)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from lc.utils import compute_phash, phash_distance
from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H,
    DOWNLOAD_WAIT, PHASH_THRESHOLD,
)
from tools.ap.context import DetectContext
from tools.ap.device import adb, tap_device, take_screenshot, check_foreground_app
from tools.ap.helpers import has_any, has_text, log_milestone
from tools.ap.image_proc import roi_to_device
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def handle_common_guards(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """共通ガード群。detect_and_act の最初に評価される。

    Returns:
        (action_name, wait_seconds) or None (次のハンドラへフォールスルー)
    """
    texts = ctx.texts
    joined = ctx.joined
    ocr = ctx.ocr
    W, H = ctx.W, ctx.H
    analysis_path = ctx.analysis_path

    # ── 【#-5】ブラウザ脱出 — WEB SHOP 等の外部リンクを検出したら即 BACK ──
    # 「今日は表示しない」があればお知らせポップアップ内のテキストなのでスキップ
    _browser_kw = ["WEB SHOP", "好評配信中", "doka-exedra", "magia-exedra"]
    _is_notice_ctx = any("今日は表示し" in t for t in texts)
    if not _is_notice_ctx and any(kw in joined for kw in _browser_kw):
        logger.warning("[BROWSER_ESCAPE] ブラウザ画面検出 (%s) → BACK キーで脱出",
                       [kw for kw in _browser_kw if kw in joined])
        adb("shell input keyevent KEYCODE_BACK")
        time.sleep(2.0)
        # ゲーム終了ダイアログが出た場合に備えてフォアグラウンドチェック
        check_foreground_app()
        return "BROWSER_ESCAPE", 3.0

    # ── 【#-4】MOVIE シーン中は一切タップしない ──
    # 動画はタップで一時停止/再開を繰り返す仕様 → 絶対にタップ禁止
    # detect_scene_early で MOVIE 判定済み → ここでは待機のみ返す
    if state.current_scene == "MOVIE":
        logger.debug("[detect_and_act] MOVIE シーン → タップ抑制, 待機")
        return "MOVIE_WAIT", 0.5

    # ── 【#-3b】download_active 状態管理 ──
    # download_active はDL完了ダイアログのOKタップ (DL_COMPLETE_OK) まで維持する。
    # OCRテキスト消失だけでは解除しない (DL完了→ダイアログ表示の遷移中にOCR 0件になるため)。
    if state.download_active:
        # チュートリアル中のみ: DL完了ダイアログのOKを確認するまで download_active を維持
        if not state.home_reached:
            if len(texts) == 0:
                # DL完了直後のアニメーション遷移中はOCR 0件。タップせず待機してリトライ
                logger.info("[DL_PROTECT] download_active + OCR 0件 → DL完了ダイアログ待ち (DOWNLOAD_WAIT)")
                return "DOWNLOAD_WAIT", 2.0
            # OCR結果ありだがDL関連テキストも完了テキストもない → DL画面を完全に離脱
            _dl_any = ["Download", "ダウンロード", "追加データ", "MB", "GB", "完了", "Complete"]
            if not any(kw in joined for kw in _dl_any):
                logger.info("[DL_PROTECT] OCRにDL/完了テキストなし → download_active 解除 (画面遷移済み)")
                state.download_active = False
                log_milestone(state, "DL_END")
        else:
            # ホーム到達後: OCRにDLテキストがなければ即解除
            _dl_kws_check = ["Download", "ダウンロード", "追加データ", "MB", "GB"]
            if not any(kw in joined for kw in _dl_kws_check):
                logger.info("[DL_PROTECT] ホーム後 + DLテキストなし → download_active 解除")
                state.download_active = False
                log_milestone(state, "DL_END")

    # ── 【#-3a】Loading 画面保護 ──
    # "Now Loading" 等が表示されている間は金枠/指ブロブの誤検出でタップしない
    _loading_kws = ["Now Loading", "Loading", "読み込み中", "接続しています"]
    if any(kw in joined for kw in _loading_kws) and len(texts) <= 3:
        logger.debug("[detect_and_act] Loading 画面 (%d件) → タップ抑制", len(texts))
        return "LOADING_WAIT", 1.0

    # ── 【#-3】ダウンロード画面の厳格判定 ──
    # 条件: 右下エリアに "Download" テキスト + "MB" 進捗テキストが両方存在
    # → これ以外の画面は 100% ゲーム実行中であり、ロード待ちを禁止する。
    # 通信速度やネットワーク状態による推測は一切行わない。
    # NOTE: OCR が "Download" を "Downiond"/"Down ond" 等と誤読するため、
    # "Down" 前方一致 + MB/GB 進捗パターンでも検出する
    _has_download_text = any(
        "Download" in t or "ダウンロード" in t or t.startswith("Down") for t in texts)
    _has_size_progress = any("MB" in t or "GB" in t for t in texts)
    # 追加: "XXX MB/YYY MB" パターン (スラッシュ区切りのサイズ表記) は確実にダウンロード
    _has_size_slash = any(re.search(r"\d+.*MB/\d+", t) for t in texts)
    if _has_size_slash:
        _has_download_text = True
        _has_size_progress = True
    # 確認/完了/失敗ダイアログ除外:
    # - 「ダウンロードを開始しますか?」等の質問 or OK+キャンセル共存
    # - 「ダウンロード完了」等の完了通知 + OK ボタン
    # - 「ダウンロードに失敗しました」等の失敗ダイアログ (リトライ確認)
    _dl_is_question = any("しますか" in t or "開始" in t for t in texts if "ダウンロード" in t)
    _dl_is_complete = any("完了" in t or "Complete" in t for t in texts)
    _dl_is_failure = any("失敗" in t or "リトライ" in t for t in texts)
    _dl_has_ok = any("OK" in t for t in texts)
    _dl_has_cancel = any("キャンセル" in t for t in texts)
    _dl_is_confirm_dialog = (_dl_is_question or (_dl_has_ok and _dl_has_cancel)
                             or (_dl_is_complete and _dl_has_ok) or _dl_is_failure)
    if _has_download_text and _has_size_progress and not _dl_is_confirm_dialog:
        _dl_texts = [t for t in texts if "Download" in t or "MB" in t or "GB" in t or "ダウンロード" in t]
        logger.info(">>> [DOWNLOAD_STRICT] 右下ゲージ確認: %s — ダウンロード待機", _dl_texts)
        state.download_active = True
        log_milestone(state, "DL_START")
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT
    # ── DL失敗ダイアログ: OCR がボタンテキスト検出できない場合の安全網 ──
    if _dl_is_failure and _has_download_text:
        state.download_active = False
        _ok_x, _ok_y = roi_to_device(int(ANALYSIS_W * 0.62), int(ANALYSIS_H * 0.82), state.game_roi)
        logger.info("[DL_FAIL_RETRY] 失敗ダイアログ検出 → OK推定位置 (%d,%d) タップ", _ok_x, _ok_y)
        tap_device(_ok_x, _ok_y, state, "DL_FAIL_RETRY_OK")
        return "DL_FAIL_RETRY", 2.0

    # 確認ダイアログ (OK+Cancel) は dialog_phase.py に移動済み

    _in_battle_ctx = ctx.in_battle_ctx

    # ── 【#-2.2】Android 権限ダイアログ (単独「許可」ボタン) ──
    # 通知許可等で「許可しない」なしの単独「許可」ダイアログが出ることがある。
    # 確認ダイアログ(#-2.9)は肯定+否定の共存が条件なので、ここで補完する。
    if not ctx.confirm_pos and not _in_battle_ctx:
        _perm_btn = has_any(ocr, ["許可", "Allow", "ALLOW"])
        _perm_ctx = has_any(ocr, ["通知", "位置情報", "ストレージ", "カメラ",
                                   "notification", "permission"])
        if _perm_btn and _perm_ctx:
            _pm_x, _pm_y = _perm_btn["center"]
            logger.info(">>> [PERMISSION] Android権限ダイアログ '%s' (%d,%d) タップ",
                        _perm_btn["text"], _pm_x, _pm_y)
            tap_device(_pm_x, _pm_y, state, f"PERMISSION_ALLOW '{_perm_btn['text']}'")
            return "PERMISSION_ALLOW", 1.0

    # ── 【#-2】タイトル画面 設定/サポートメニュー ──
    # 「動画配信設定」アイコンを誤タップして開く設定ポップアップ → BACK で閉じる
    # ただし、ストーリー/バトル/マップシーン中は「サポート」がセリフに含まれるため除外
    _settings_menu_kws = ["サポート", "データ引き継ぎ", "キャッシュクリア", "お問い合わせ"]
    _story_context_kws = ["1-1", "1-2", "第1幕", "第1階層", "第2幕", "WAVE", "AUTO", "1-3", "2-1"]
    _in_story_ctx = any(kw in joined for kw in _story_context_kws)
    # 設定メニューはストーリーコンテキスト外かつ2つ以上のキーワードが揃った時のみ判定
    _settings_hits = sum(1 for kw in _settings_menu_kws
                         if has_text(ocr, kw, min_conf=0.3) is not None)
    if not _in_story_ctx and _settings_hits >= 2:
        logger.info(">>> 【設定メニュー誤起動】 BACK キーで閉じる")
        adb("shell input keyevent 4")
        return "SETTINGS_BACK", 1.5

    # ─── Play Games ポップアップ → BACK キーで閉じる ───
    # 中央タップすると Chrome が起動してスタックするため BACK で安全に閉じる
    if has_text(ocr, "Play Games", min_conf=0.3) or has_text(ocr, "Play ゲーム", min_conf=0.3):
        logger.info(">>> 【Play Games ポップアップ】 BACK キーで閉じる")
        adb("shell input keyevent 4")
        return "PLAY_GAMES_BACK", 1.0

    # ─── お知らせ一覧画面 → × で閉じる ───
    # 「お知らせ」「情報」「不具合」のタブヘッダが 3 つ全てあれば一覧画面
    _notice_list_tabs = ["お知らせ", "情報", "不具合"]
    _notice_list_hits = sum(1 for kw in _notice_list_tabs if has_text(ocr, kw, min_conf=0.3))
    if _notice_list_hits >= 3:
        # × テンプレートで閉じる
        from tools.ap.image_proc import ASSET_MANAGER as _AM_notice
        _close_m = _AM_notice.match_single("close_btn", analysis_path)
        if _close_m and _close_m[2] >= 0.50:
            tap_device(_close_m[0], _close_m[1], state, "NOTICE_LIST_CLOSE")
            logger.info(">>> 【お知らせ一覧】 ×テンプレート(%d,%d score=%.2f) で閉じる",
                        _close_m[0], _close_m[1], _close_m[2])
            return "NOTICE_LIST_CLOSE", 1.0
        # テンプレ未検出 → 右上固定座標
        _nx, _ny = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
        tap_device(_nx, _ny, state, "NOTICE_LIST_CLOSE_FB")
        logger.info(">>> 【お知らせ一覧】 × 固定座標(%d,%d) で閉じる", _nx, _ny)
        return "NOTICE_LIST_CLOSE", 1.0

    # ─── 【最優先 #-1】「ご注意」画面 (Google Play 起動時 portrait 注意書き) ───
    # アプリ初回起動時に portrait で表示される法的注意画面。
    # 「同意してゲームを始める」ボタン (右側ゴールドボタン) をOCRで検出してタップ。
    # 「今日は表示しない」があればお知らせポップアップなのでスキップ (誤発火防止)
    _is_notice_text = any("今日は表示し" in t for t in texts)
    if not _is_notice_text and (
        has_text(ocr, "ご注意", min_conf=0.3) or (
            has_text(ocr, "基本無料", min_conf=0.3) and has_text(ocr, "未成年", min_conf=0.3)
        )
    ):
        # 「同意」ボタンをOCRで検出
        # scrcpy はステータスバー込みの全画面をキャプチャするため、
        # OCR 座標 → _to_device() でそのまま正しいタップ座標に変換される。
        agree_btn = (has_text(ocr, "同意してゲーム", min_conf=0.2) or
                     has_text(ocr, "同意して", min_conf=0.2) or
                     has_text(ocr, "ゲームを始める", min_conf=0.2))
        if agree_btn:
            cx, cy = agree_btn["center"]
            logger.info(">>> 【ご注意画面】 同意ボタン検出 OCR(%d,%d)", cx, cy)
        else:
            # フォールバック: 比率ベース (W*0.66, H*0.79) + ROI 補正
            cx, cy = roi_to_device(int(W * 0.66), int(H * 0.79), state.game_roi)
            logger.info(">>> 【ご注意画面】 同意ボタン未検出 → ROI補正フォールバック (%d,%d)", cx, cy)

        # ─── phash監視付き動的リトライ (固定120秒スリープを廃止) ───
        # 仕様: タップ → 2s待機 → phash変化確認 → 変化なし → x+20pxずらして最大5回リトライ
        # 変化検知 → Unity初期化待機(60s)へ即移行
        _base_ph = compute_phash(analysis_path) if analysis_path else ""
        _agree_changed = False
        for _retry_i in range(5):
            _tap_x = cx + _retry_i * 20  # x方向に +20px ずつ調整
            _tap_y = cy
            tap_device(_tap_x, _tap_y, state,
                       f"GO_CHUI_AGREE_R{_retry_i}({'OCR' if agree_btn else 'FB'})")
            logger.info(">>> 【ご注意→phash監視】 #%d タップ(%d,%d) → 待機",
                        _retry_i + 1, _tap_x, _tap_y)
            time.sleep(0.3)
            _new_ss, _, _, _ = take_screenshot()
            _new_ph = compute_phash(_new_ss)
            if _base_ph and _new_ph:
                _dist = phash_distance(_base_ph, _new_ph)
                if _dist >= PHASH_THRESHOLD:
                    logger.info(
                        ">>> 【ご注意→変化検知!】 #%d tap(%d,%d) phash_dist=%d → Unity初期化待機へ",
                        _retry_i + 1, _tap_x, _tap_y, _dist
                    )
                    _agree_changed = True
                    break
                logger.info(">>> 【ご注意→変化なし】 #%d phash_dist=%d → 座標+20pxで再試行",
                            _retry_i + 1, _dist)
                _base_ph = _new_ph  # 次回比較の基準を更新
            else:
                logger.info(">>> 【ご注意→phash計算失敗】 #%d → 次座標で再試行", _retry_i + 1)

        if _agree_changed:
            # ポーリングで画面安定を待つ (最大10秒, 0.5秒間隔)
            # 旧: 30秒固定wait → 利用規約画面が即表示されても30秒無駄に待っていた
            _poll_ph = _new_ph
            for _poll_i in range(20):  # 0.5s × 20 = 最大10秒
                time.sleep(0.5)
                _poll_ss, _, _, _ = take_screenshot()
                _poll_new = compute_phash(_poll_ss)
                if _poll_ph and _poll_new:
                    _poll_dist = phash_distance(_poll_ph, _poll_new)
                    if _poll_dist < PHASH_THRESHOLD:
                        # 画面が安定した → 即脱出
                        logger.info(">>> 【NOTICE_DISMISS】 画面安定検知 (poll=%d, dist=%d) → 即続行",
                                    _poll_i + 1, _poll_dist)
                        break
                    _poll_ph = _poll_new
            else:
                logger.info(">>> 【NOTICE_DISMISS】 10秒経過 → 続行")
            log_milestone(state, "NOTICE_DISMISS")
            return "NOTICE_DISMISS", 0.5
        else:
            logger.info(">>> 【ご注意→リトライ上限(5回)】 次ループで再検出")
            return "NOTICE_DISMISS", 3.0

    # ─── 【#-0.5】タイトル画面 (TAP TO START) ───
    # ホーム未到達 + 利用規約でない + タイトル固有キーワード → 画面下部タップ
    _is_tos_screen = "利用規約" in joined or "同意してゲームを始める" in joined
    _title_kws_game = ["魔法", "少女", "まどか", "マギカ", "まどかハ", "MADOKA", "MAGICA"]
    _is_title_screen = (
        not state.home_reached and not _is_tos_screen and (
            any(kw in joined for kw in ["TAP TO START", "TAPTOSTART"]) or
            (any(kw in joined for kw in ["動画配信", "勤画配信", "Ver.2", "Ver.2."])
             and any(kw in joined for kw in _title_kws_game + ["PUELLA"])) or
            ("VID" in joined and any(kw in joined for kw in _title_kws_game)) or
            (any(kw in joined for kw in ["PUELLA MAGI", "PUELLAHAGI", "PUELLAMAGI",
                                           "PUELLA MAGIMADOKA"])
             and any(kw in joined for kw in _title_kws_game)
             and not any(kw in joined for kw in ["クエスト", "ショップ", "ガチャ",
                                                   "Rank", "Main", "推奨"]))
        )
    )
    if _is_title_screen:
        logger.info("  タイトル画面検出 → TAP TO START タップ")
        log_milestone(state, "TITLE_TAP")
        _tt_x, _tt_y = roi_to_device(int(W * 0.5), int(H * 0.87), state.game_roi)
        tap_device(_tt_x, _tt_y, state, "TITLE_TAP_START")
        return "TITLE_TAP", 2.0

    return None
