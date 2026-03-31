"""
ap/handlers/fallback.py — フォールバックハンドラ

Phase 9: 閉じるボタン、システムダイアログ、メンテナンス/アップデート、
利用規約、確認ダイアログ、ストーリー送り、吹き出し、ログインボーナス、
WAIT_FOR_CHANGE。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from lc.utils import compute_phash, phash_distance
from tools.ap.constants import _CLOSE_BTN_OFFSET, _MENU_SCREEN_KWS, PHASH_THRESHOLD
from tools.ap.context import DetectContext
from tools.ap.device import adb, tap_device, swipe_device, take_screenshot
from tools.ap.helpers import has_any, has_text, log_milestone
from tools.ap.image_proc import (
    smart_tap_button, roi_to_device, ASSET_MANAGER,
    detect_dialog_corners,
)
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def handle_fallback(ctx: DetectContext, state: PilotState) -> tuple[str, float]:
    """フォールバックハンドラ。必ず結果を返す (Optional ではない)。"""
    ocr = ctx.ocr
    texts = ctx.texts
    joined = ctx.joined
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── シーン判定フラグ ───
    _is_dialog = detect_dialog_corners(analysis_path) if analysis_path else False
    _has_close_btn = False
    _close_btn_pos = None
    if analysis_path:
        _close_m = ASSET_MANAGER.match_single("close_btn", analysis_path)
        if _close_m and _close_m[2] >= 0.90:
            _has_close_btn = True
            _close_btn_pos = (_close_m[0], _close_m[1])

    # ─── 非ダイアログ画面の×ボタン: WFC_ESCAPE に統合 (auto_pilot.py) ───
    # close_btn 単独タップは WAIT_FOR_CHANGE 3回連続後の WFC_ESCAPE で処理

    # ─── ゲーム内システムダイアログ (画質設定・ダウンロード確認 等) ───
    # smart_tap_button で金色ボタン枠の幾何学的中心を取得 (OCR ずれを排除)
    sys_dlg_kws = ["画質を設定", "アセット更新", "ダウンロードを開始", "ダウンロードしますか",
                   "Wi-Fiを使用", "モバイル通信でダウンロード", "ダウンロードが完了しました"]
    sys_dlg_match = has_any(ocr, sys_dlg_kws, min_conf=0.3)
    if sys_dlg_match:
        ok_item = next((item for item in ocr if "OK" in item.get("text", "")), None)
        if ok_item and ok_item["center"][0] > W * 0.5:
            ocr_ok_x, ocr_ok_y = ok_item["center"]
            ok_x, ok_y = smart_tap_button(analysis_path, ocr_ok_x, ocr_ok_y, ocr_items=ocr)
            logger.info(">>> 【システムダイアログ】 '%s' → SmartTap OK (%d,%d)",
                        sys_dlg_match["text"][:15], ok_x, ok_y)
            tap_device(ok_x, ok_y, state, "SYSTEM_DLG_OK")
            return "SYSTEM_DLG_OK", 1.0
        else:
            logger.info(">>> 【システムダイアログ】 '%s' → OK 未検出、盲タップせずスキップ",
                        sys_dlg_match["text"][:15])

    # ─── メンテナンス/アップデート検出 ───
    _maint_kws = ["メンテナンス", "Maintenance", "maintenance"]
    _update_kws = ["アップデート", "Update", "update", "最新バージョン"]
    _maint_hit = has_any(ocr, _maint_kws, min_conf=0.3)
    _update_hit = has_any(ocr, _update_kws, min_conf=0.3)
    if _maint_hit:
        logger.warning(">>> [MAINTENANCE] メンテナンス検出: '%s' — 60秒待機", _maint_hit["text"])
        return "MAINTENANCE_WAIT", 60.0
    if _update_hit and not ctx.in_battle_ctx:
        # アップデートダイアログ: OK/確認ボタンがあればタップ
        _upd_ok = has_any(ocr, ["OK", "確認", "ストアへ"])
        if _upd_ok:
            _uo_x, _uo_y = _upd_ok["center"]
            logger.info(">>> [UPDATE] アップデート通知 → '%s' (%d,%d) タップ", _upd_ok["text"], _uo_x, _uo_y)
            tap_device(_uo_x, _uo_y, state, "UPDATE_DIALOG_OK")
            return "UPDATE_DIALOG", 3.0
        logger.warning(">>> [UPDATE] アップデート検出: '%s' — 手動対応が必要な可能性", _update_hit["text"])
        return "UPDATE_WAIT", 10.0

    # ─── 利用規約同意ダイアログ ───
    # 「同意してゲームを始める」ボタンを右下の固定座標または OCR 座標でタップ
    tos_screen = has_any(ocr, ["同意してゲームを始める", "プライバシーポリシー"], min_conf=0.3)
    if tos_screen and has_text(ocr, "利用規約", min_conf=0.3):
        # "始める" または "ゲームを始める" を OCR で探して座標タップ
        agree_ocr = has_any(ocr, ["始める", "ゲームを始める", "同意してゲームを始める"], min_conf=0.3)
        if agree_ocr:
            cx, cy = agree_ocr["center"]
            logger.info(">>> 【利用規約同意】 '%s' (%d,%d) タップ", agree_ocr["text"][:10], cx, cy)
            tap_device(cx, cy, state, "AGREE_TOS")
        else:
            logger.info(">>> 【利用規約同意】 OCR で同意ボタン未検出 → 盲タップせずスキップ")
            return "TOS_NO_BUTTON", 1.0
        return "AGREE_TOS", 2.0

    # ─── 規約同意 ───
    agree_match = has_any(ocr, ["同意", "規約", "利用規約"])
    if agree_match:
        logger.info(">>> 規約画面 — スクロール→同意")
        for _ in range(3):
            swipe_device(int(W * 0.46), int(H * 0.69), int(W * 0.46), int(H * 0.28), 500, state=state, desc="TOS_SCROLL")
            time.sleep(0.3)
        agree_btn = has_any(ocr, ["同意"])
        if agree_btn:
            cx, cy = agree_btn["center"]
            tap_device(cx, cy, state, "AGREE")
        return "AGREE", 1.0

    # ─── ガチャ結果確認: 「限界突破」+「確定/獲得」→ OK 固定位置タップ ───
    _gacha_limit = has_text(ocr, "限界突破", min_conf=0.2)
    _gacha_kakutei = has_text(ocr, "確定", min_conf=0.2) or has_text(ocr, "獲得", min_conf=0.2)
    if _gacha_limit and _gacha_kakutei:
        _ok_x, _ok_y = roi_to_device(int(W * 0.41), int(H * 0.89), state.game_roi)
        logger.info(">>> 【ガチャ結果確認】 限界突破+確定/獲得 検出 → OK想定位置 (%d,%d) タップ", _ok_x, _ok_y)
        tap_device(_ok_x, _ok_y, state, "GACHA_RESULT_OK")
        return "GACHA_RESULT_OK", 1.5

    # ─── 閉じるポップアップ (報酬/通知系) → × ボタンで閉じる ───
    _close_popup_kws = ["限界突破", "強化完了", "レベルアップ", "称号獲得", "エピソード解放",
                        "ランクアップ", "新しいコンテンツ", "アンロック",
                        "マギアボックス", "ミッション達成", "デイリーミッション",
                        "ログインボーナス", "初心者ログイン", "キャンペーン"]
    _close_popup = has_any(ocr, _close_popup_kws)
    if _close_popup and analysis_path and not _is_dialog:
        logger.debug("[CLOSE_POPUP] 四隅テンプレなし → ダイアログではない、スキップ (kw='%s')",
                    _close_popup["text"][:10])
        _close_popup = None
    if _close_popup:
        if state.pre_popup_tap_count >= 8:
            logger.warning(">>> 【%s ポップアップ】 × が8回空振り → BACK キーで脱出",
                           _close_popup["text"][:6])
            try:
                adb("shell input keyevent KEYCODE_BACK")
            except Exception as _e:
                logger.debug("[CLOSE_POPUP] BACK キー送信例外: %s", _e)
            state.pre_popup_tap_count = 0
            return "CLOSE_POPUP_BACK", 1.0
        _close_match = ASSET_MANAGER.match_single("close_btn", analysis_path) if analysis_path else None
        if _close_match and _close_match[2] >= 0.60:
            _cpx, _cpy = _close_match[0], _close_match[1]
            logger.info(">>> 【%s ポップアップ】 → × テンプレ(%.2f) (%d,%d) タップ",
                        _close_popup["text"][:6], _close_match[2], _cpx, _cpy)
        else:
            _cpx = W - _CLOSE_BTN_OFFSET
            _cpy = _CLOSE_BTN_OFFSET
            logger.info(">>> 【%s ポップアップ】 → × 固定座標 (%d,%d) タップ",
                        _close_popup["text"][:6], _cpx, _cpy)
        state.pre_popup_tap_count += 1
        tap_device(_cpx, _cpy, state, f"CLOSE_POPUP_{_close_popup['text'][:6]}")
        return "CLOSE_POPUP", 1.0

    # ─── 「タップして次へ」: 報酬獲得画面の次へ進む ───
    _tap_next = has_text(ocr, "タップして次へ", min_conf=0.3)
    if _tap_next:
        cx, cy = _tap_next["center"]
        logger.info(">>> 【報酬/次へ】 'タップして次へ' (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "REWARD_NEXT")
        return "REWARD_NEXT", 1.0

    # ─── 確認ダイアログ (ダイアログ証拠必須) ───
    # ボタンラベルのみ。四隅テンプレでダイアログが確認できた場合に限りタップ。
    if _is_dialog:
        confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                       "受け取る", "受取", "了解", "わかった",
                                       "進む", "START", "開始",
                                       "TAP TO START", "TOUCH", "始める"])
        if confirm_match:
            cx, cy = confirm_match["center"]
            text = confirm_match["text"]
            logger.info(">>> 確認 '%s' (%d,%d) [ダイアログ検出済]", text, cx, cy)
            tap_device(cx, cy, state, f"CONFIRM '{text}'")
            return "CONFIRM", 1.0

    # ─── ストーリー/会話 (下部テキストボックス) ───
    # ADV 構造的証拠 (AUTOボタン・↓送りボタン・ADVツールバー) がある場合のみ
    _STORY_TAP_EXCLUDE = {"Rank", "Pank", "Runk", "AUTO", "SKIP", ">>", ">|"}
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6
                   and r["text"] not in _STORY_TAP_EXCLUDE]
    # 防御的 ADV 検出: adv_result が空でも AUTO + ↓ボタンで判定
    # ↓ボタンは ADV 固有。ADV固有アイコン(menu/log/skip)は探索パート等で誤マッチするため不使用。
    _has_auto_template = False
    if not ctx.adv_result.is_adv and not ctx.is_mini_conv and analysis_path:
        from tools.ap.constants import ADV_TOOLBAR_ROI
        _ft_auto = ASSET_MANAGER.match_single("icon_auto", analysis_path, roi=ADV_TOOLBAR_ROI)
        if _ft_auto and _ft_auto[2] >= 0.50:
            _ft_next = ASSET_MANAGER.match_single("next_btn", analysis_path)
            if _ft_next and _ft_next[2] >= 0.70:
                _has_auto_template = True
    _has_adv_evidence = ctx.adv_result.is_adv or ctx.is_mini_conv or _has_auto_template
    if lower_texts and len(ocr) <= 15 and not state.download_active and _has_adv_evidence:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── お知らせ一覧画面 (タブ: お知らせ/情報/不具合 の3つ全て) → ×ボタンで閉じる ───
    _notice_tabs = sum(1 for kw in ["お知らせ", "情報", "不具合"]
                       if has_text(ocr, kw, min_conf=0.3))
    if _notice_tabs >= 3:
        # ×ボタン: 右上固定位置
        _nx, _ny = int(W * 0.97), int(H * 0.05)
        logger.info(">>> 【お知らせ一覧】 タブ%d個検出 → ×クローズ (%d,%d)", _notice_tabs, _nx, _ny)
        tap_device(_nx, _ny, state, "NOTICE_LIST_CLOSE")
        return "NOTICE_LIST_CLOSE", 1.0

    # ─── サブ画面脱出: 左上にメニュー画面名 → 戻るボタンタップ ───
    # ガチャ/交換所等のサブ画面からの脱出 (チュートリアル中は誤動作するため home_reached 後のみ)
    if state.home_reached and not ctx.in_battle_ctx:
        for item in ocr:
            _t = item.get("text", "")
            _c = item.get("center", (0, 0))
            if _c[0] < W * 0.20 and _c[1] < H * 0.15:
                if any(kw in _t for kw in _MENU_SCREEN_KWS):
                    _back_x = max(int(_c[0] - W * 0.05), int(W * 0.02))
                    _back_y = _c[1]
                    logger.info(">>> 【サブ画面脱出】 左上に '%s' 検出 → 戻る (%d,%d)",
                                _t, _back_x, _back_y)
                    tap_device(_back_x, _back_y, state, "SUB_SCREEN_BACK")
                    return "SUB_SCREEN_BACK", 1.5

    # ─── メニュースタック救済: 左上メニューKW + icon_back テンプレ → 戻る ───
    if state.ineffective_tap_count >= 5 and not ctx.in_battle_ctx:
        _menu_text = None
        for item in ocr:
            _t = item.get("text", "")
            _c = item.get("center", (0, 0))
            if _c[0] < W * 0.20 and _c[1] < H * 0.15:
                if any(kw in _t for kw in _MENU_SCREEN_KWS):
                    _menu_text = _t
                    break
        if _menu_text and analysis_path:
            _back_roi = (0, 0, int(W * 0.15), int(H * 0.20))
            _back_match = ASSET_MANAGER.match_single("icon_back", analysis_path, roi=_back_roi)
            if _back_match and _back_match[2] >= 0.60:
                _bx, _by = _back_match[0], _back_match[1]
                logger.warning(
                    ">>> 【メニュースタック救済】 '%s' + icon_back(%.2f) → 戻る (%d,%d) (ineffective=%d)",
                    _menu_text, _back_match[2], _bx, _by, state.ineffective_tap_count,
                )
                tap_device(_bx, _by, state, "MENU_STALL_BACK")
                return "MENU_STALL_BACK", 1.5

    # ─── プレゼントボックス: 「一括受取」タップ or BACK で戻る ───
    # 指テンプレ検出時 (pre_dialog_finger) はスキップ → finger_priority で処理
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
            logger.info(">>> 【プレゼントボックス】 一括受取なし → BACK で戻る")
            adb("shell input keyevent KEYCODE_BACK")
            return "PRESENT_BOX_BACK", 1.0

    # ─── チュートリアルガイドスタック: blob_same_count≥5 + 「してみましょう」→ × で閉じ ───
    if state.blob_same_count >= 5:
        _tutorial_guide = (has_text(ocr, "てみましょう", min_conf=0.3) or
                           has_text(ocr, "しましょう", min_conf=0.3))
        if _tutorial_guide and not ctx.in_battle_ctx:
            _tg_x = W - _CLOSE_BTN_OFFSET
            _tg_y = _CLOSE_BTN_OFFSET
            logger.info(">>> 【チュートリアルガイド スタック】 '%s' → × (%d,%d) タップ",
                        _tutorial_guide["text"][:10], _tg_x, _tg_y)
            tap_device(_tg_x, _tg_y, state, "TUTORIAL_GUIDE_CLOSE")
            state.blob_same_count = 0
            return "CLOSE_POPUP", 1.0

    # ─── 「ご注意」画面 (初回起動時) → 同意ボタンタップ + phash リトライ ───
    _is_notice_text = any("今日は表示し" in t for t in texts)
    if not _is_notice_text and (
        has_text(ocr, "ご注意", min_conf=0.3) or (
            has_text(ocr, "基本無料", min_conf=0.3) and has_text(ocr, "未成年", min_conf=0.3)
        )
    ):
        agree_btn = (has_text(ocr, "同意してゲーム", min_conf=0.2) or
                     has_text(ocr, "同意して", min_conf=0.2) or
                     has_text(ocr, "ゲームを始める", min_conf=0.2))
        if agree_btn:
            cx, cy = agree_btn["center"]
            logger.info(">>> 【ご注意画面】 同意ボタン検出 OCR(%d,%d)", cx, cy)
        else:
            cx, cy = roi_to_device(int(W * 0.66), int(H * 0.79), state.game_roi)
            logger.info(">>> 【ご注意画面】 同意ボタン未検出 → ROI補正フォールバック (%d,%d)", cx, cy)
        _base_ph = compute_phash(analysis_path) if analysis_path else ""
        _agree_changed = False
        for _retry_i in range(5):
            _tap_x = cx + _retry_i * 20
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
                    logger.info(">>> 【ご注意→変化検知!】 #%d phash_dist=%d → 画面安定待ち",
                                _retry_i + 1, _dist)
                    _agree_changed = True
                    break
                logger.info(">>> 【ご注意→変化なし】 #%d phash_dist=%d → +20px再試行",
                            _retry_i + 1, _dist)
                _base_ph = _new_ph
        if _agree_changed:
            _poll_ph = _new_ph
            for _poll_i in range(20):
                time.sleep(0.5)
                _poll_ss, _, _, _ = take_screenshot()
                _poll_new = compute_phash(_poll_ss)
                if _poll_ph and _poll_new:
                    _poll_dist = phash_distance(_poll_ph, _poll_new)
                    if _poll_dist < PHASH_THRESHOLD:
                        logger.info(">>> 【NOTICE_DISMISS】 画面安定検知 (poll=%d, dist=%d)",
                                    _poll_i + 1, _poll_dist)
                        break
                    _poll_ph = _poll_new
            else:
                logger.info(">>> 【NOTICE_DISMISS】 10秒経過 → 続行")
            log_milestone(state, "NOTICE_DISMISS")
            return "NOTICE_DISMISS", 0.5
        else:
            return "NOTICE_DISMISS", 3.0

    # ─── タイトル画面 (TAP TO START) ───
    _is_tos_screen = "利用規約" in joined or "同意してゲームを始める" in joined
    _title_kws_game = ["魔法", "少女", "まどか", "マギカ", "まどかハ", "MADOKA", "MAGICA"]
    _is_title_screen = (
        not state.home_reached and not _is_tos_screen
        and any(kw in joined for kw in ["TAP TO START", "TAPTOSTART", "TAP T0 START",
                                         "TAPTO START", "TAP TOSTART"])
        and any(kw in joined for kw in _title_kws_game)
    )
    if _is_title_screen:
        logger.info("  タイトル画面検出 → TAP TO START タップ")
        log_milestone(state, "TITLE_TAP")
        _tt_x, _tt_y = roi_to_device(int(W * 0.5), int(H * 0.87), state.game_roi)
        tap_device(_tt_x, _tt_y, state, "TITLE_TAP_START")
        return "TITLE_TAP", 2.0

    # ─── フォールバック: 何も見つからない ───
    logger.info(">>> 画面が安定するまで待機 (OCR %d件)", len(ocr))
    return "WAIT_FOR_CHANGE", 0
