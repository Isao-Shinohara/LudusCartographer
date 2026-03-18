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
from datetime import datetime
from pathlib import Path
from typing import Optional

from lc.utils import compute_phash, phash_distance
from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H,
    DOWNLOAD_WAIT, PHASH_THRESHOLD,
    _BATTLE_CORE_KWS,
    _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS,
    _CURRENCY_SPEND_KWS, _OCR_BBOX_Y_PADDING,
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
    _browser_kw = ["WEB SHOP", "好評配信中", "doka-exedra", "magia-exedra"]
    if any(kw in joined for kw in _browser_kw):
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
        _ok_x, _ok_y = int(ANALYSIS_W * 0.62), int(ANALYSIS_H * 0.82)
        logger.info("[DL_FAIL_RETRY] 失敗ダイアログ検出 → OK推定位置 (%d,%d) タップ", _ok_x, _ok_y)
        tap_device(_ok_x, _ok_y, state, "DL_FAIL_RETRY_OK")
        return "DL_FAIL_RETRY", 2.0

    # ── 【#-2.9】確認ダイアログ — 肯定ボタン最優先 ──
    # (A) OK/はい + キャンセル/いいえ が共存 → 確認ダイアログ → OK を必ずタップ。
    # (B) 「完了」系テキスト + OK 単独 → 完了通知ダイアログ → OK をタップ。
    # #0-DIALOG の × ボタンが先に発動する問題を根本解決。
    # ダウンロードの次、SKIP より先に評価する。
    _confirm_pos = ctx.confirm_pos
    _confirm_neg = ctx.confirm_neg
    _is_completion_dialog = _confirm_pos and _dl_is_complete
    if (_confirm_pos and _confirm_neg) or _is_completion_dialog:
        # ── 課金保護: 通貨消費キーワード → キャンセル ──
        _is_currency = any(kw in joined for kw in _CURRENCY_SPEND_KWS)
        if _is_currency and _confirm_neg:
            _cn_x, _cn_y = _confirm_neg["center"]
            _cn_y_adj = max(0, _cn_y - _OCR_BBOX_Y_PADDING)
            logger.info("[ConfirmDialog] 課金保護: → キャンセル '%s' タップ",
                        _confirm_neg["text"])
            tap_device(_cn_x, _cn_y_adj, state,
                       f"CURRENCY_CANCEL '{_confirm_neg['text']}'")
            return "CURRENCY_CANCEL", 1.0
        # ── スキップ確認ダイアログ → キャンセルをタップ (スキップ禁止) ──
        _is_story_skip_dialog = any("スキップ" in t for t in texts)
        if _is_story_skip_dialog and _confirm_neg:
            _cn_x, _cn_y = _confirm_neg["center"]
            _cn_y_adj = max(0, _cn_y - _OCR_BBOX_Y_PADDING)
            logger.info(
                "[ConfirmDialog] スキップ検出 → キャンセル '%s' (%d,%d→Y%d) タップ",
                _confirm_neg["text"], _cn_x, _cn_y, _cn_y_adj,
            )
            tap_device(_cn_x, _cn_y_adj, state, f"STORY_SKIP_CANCEL '{_confirm_neg['text']}'")
            return "STORY_SKIP_CANCEL", 1.0
        _cp_x, _cp_y = _confirm_pos["center"]
        # OCR bbox はテキスト下部パディングを含むため Y を上方補正
        _cp_y_adj = max(0, _cp_y - _OCR_BBOX_Y_PADDING)
        _neg_label = _confirm_neg["text"] if _confirm_neg else "(なし)"
        logger.info(
            "[ConfirmDialog] '%s' (%d,%d→Y%d) タップ (否定='%s'無視)",
            _confirm_pos["text"], _cp_x, _cp_y, _cp_y_adj, _neg_label,
        )
        # ── デバッグ: ConfirmDialog のスクリーンショットにタップ座標を描画して保存 ──
        try:
            import cv2 as _cv2_dbg
            _dbg_img = _cv2_dbg.imread(str(analysis_path))
            if _dbg_img is not None:
                # 肯定ボタン (タップ先) を赤丸で描画
                _cv2_dbg.circle(_dbg_img, (_cp_x, _cp_y_adj), 15, (0, 0, 255), 3)
                _cv2_dbg.putText(_dbg_img, f"OK({_cp_x},{_cp_y_adj})",
                                 (_cp_x - 60, _cp_y_adj - 20),
                                 _cv2_dbg.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                # 否定ボタンを青丸で描画
                if _confirm_neg:
                    _cn_cx, _cn_cy = _confirm_neg["center"]
                    _cv2_dbg.circle(_dbg_img, (_cn_cx, _cn_cy), 15, (255, 0, 0), 3)
                    _cv2_dbg.putText(_dbg_img, f"Cancel({_cn_cx},{_cn_cy})",
                                     (_cn_cx - 80, _cn_cy - 20),
                                     _cv2_dbg.FONT_HERSHEY_SIMPLEX, 0.5, (255, 0, 0), 2)
                # 全OCR結果のbboxも描画
                for _ocr_item in ocr:
                    _oc = _ocr_item.get("center")
                    if _oc:
                        _cv2_dbg.circle(_dbg_img, (_oc[0], _oc[1]), 5, (0, 255, 0), -1)
                _dbg_ts = datetime.now().strftime("%H%M%S")
                _dbg_path = Path("storage/evidence") / f"confirm_dialog_{_dbg_ts}.png"
                _dbg_path.parent.mkdir(parents=True, exist_ok=True)
                _cv2_dbg.imwrite(str(_dbg_path), _dbg_img)
                logger.info("[ConfirmDialog][DEBUG] 座標可視化保存: %s", _dbg_path)
        except Exception as _dbg_e:
            logger.debug("[ConfirmDialog][DEBUG] 座標可視化失敗: %s", _dbg_e)
        # ── DL完了ダイアログ: OCR座標でOKタップ + phash検証 ──
        if _is_completion_dialog:
            _base_ph_cd = compute_phash(analysis_path)
            _tap_variants = [
                (_cp_x, _cp_y_adj, "OCR"),
                (_cp_x, max(0, _cp_y_adj - 20), "OCR_UP"),
                (_cp_x + 30, _cp_y_adj, "OCR_RIGHT"),
            ]
            for _tv_i, (_tv_x, _tv_y, _tv_label) in enumerate(_tap_variants):
                tap_device(_tv_x, _tv_y, state,
                           f"DL_COMPLETE_OK_R{_tv_i}({_tv_label})")
                logger.info("[DL_COMPLETE] タップ #%d (%d,%d) [%s] → phash検証",
                            _tv_i + 1, _tv_x, _tv_y, _tv_label)
                time.sleep(0.5)
                _new_ss_cd, _, _, _ = take_screenshot()
                _new_ph_cd = compute_phash(_new_ss_cd)
                if _base_ph_cd and _new_ph_cd:
                    _cd_dist = phash_distance(_base_ph_cd, _new_ph_cd)
                    if _cd_dist >= PHASH_THRESHOLD:
                        logger.info("[DL_COMPLETE] 変化検知 (dist=%d) #%d [%s] → 成功",
                                    _cd_dist, _tv_i + 1, _tv_label)
                        break
                    logger.info("[DL_COMPLETE] 変化なし (dist=%d) #%d [%s] → 次座標",
                                _cd_dist, _tv_i + 1, _tv_label)
                    _base_ph_cd = _new_ph_cd
            state.download_active = False
            logger.info("[DL_PROTECT] DL完了ダイアログOK → download_active 解除")
            log_milestone(state, "DL_END")
            return "DL_COMPLETE_OK", 1.0
        tap_device(_cp_x, _cp_y_adj, state, f"CONFIRM_DIALOG_OK '{_confirm_pos['text']}'")
        return "ADV_CHOICE", 1.0

    # ── 【#-2.5】SKIP ボタン汎用ハンドラ — 無効化 (ストーリースキップ禁止) ──
    # ストーリースキップを防止するため、"SKIP"/"スキップ" OCR検出→タップを無効化。
    # ムービーの⏭ボタンは detect_movie_skip_button() (HSV検出) で別途処理される。
    _in_battle_ctx = any(kw in joined for kw in _BATTLE_CORE_KWS)

    # ── 【#-2.2】Android 権限ダイアログ (単独「許可」ボタン) ──
    # 通知許可等で「許可しない」なしの単独「許可」ダイアログが出ることがある。
    # 確認ダイアログ(#-2.9)は肯定+否定の共存が条件なので、ここで補完する。
    if not _confirm_pos and not _in_battle_ctx:
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

    # ─── 【最優先 #-1】「ご注意」画面 (Google Play 起動時 portrait 注意書き) ───
    # アプリ初回起動時に portrait で表示される法的注意画面。
    # 「同意してゲームを始める」ボタン (右側ゴールドボタン) をOCRで検出してタップ。
    # 「今日は表示しない」があればお知らせポップアップなのでスキップ (誤発火防止)
    _is_notice_text = any("今日は表示しない" in t for t in texts)
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

    return None
