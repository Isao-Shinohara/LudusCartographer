"""
ap/handlers/common.py — 共通ガードハンドラ

detect_and_act の最初に評価される純粋なガード群:
  - ブラウザ脱出
  - MOVIE シーン待機
  - ダウンロード状態管理 / Loading 保護 / DL厳格判定
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H, DOWNLOAD_WAIT
from tools.ap.context import DetectContext
from tools.ap.device import adb, tap_device, take_screenshot, check_foreground_app
from tools.ap.helpers import has_any, log_milestone
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
    # _from_movie_ttl: MOVIE→UNKNOWN 遷移直後も8フレームはタップ抑制
    if state.current_scene == "MOVIE" or getattr(state, "_from_movie_ttl", 0) > 0:
        logger.debug("[detect_and_act] MOVIE シーン/遷移直後 → タップ抑制, 待機")
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

    # ダイアログ系 (確認DLG, 権限, 設定, Play Games, お知らせ一覧) は dialog_phase.py に移動済み
    # 起動時画面 (ご注意, タイトル) は fallback.py に移動済み

    return None
