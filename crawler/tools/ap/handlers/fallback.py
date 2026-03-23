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

from tools.ap.context import DetectContext
from tools.ap.device import tap_device, swipe_device
from tools.ap.helpers import has_any, has_text
from tools.ap.image_proc import smart_tap_button, roi_to_device, ASSET_MANAGER
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

    # ─── 閉じるボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        logger.info(">>> 閉じる '%s' (%d,%d)", close_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CLOSE '{close_match['text']}'")
        return "CLOSE", 0.5

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

    # ─── 確認ダイアログ ───
    confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                   "受け取る", "受取", "了解", "わかった",
                                   "進む", "START", "開始",
                                   "TAP TO START", "TOUCH", "始める",
                                   "戦闘", "出撃", "クエスト開始", "バトル開始",
                                   # チュートリアルで案内されるボタン名
                                   "自動編成", "一括受取", "強化", "合成", "強化素材",
                                   "クエスト", "探索開始", "バトル"])
    if confirm_match:
        cx, cy = confirm_match["center"]
        text = confirm_match["text"]
        logger.info(">>> 確認 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"CONFIRM '{text}'")
        return "CONFIRM", 1.0

    # ─── ストーリー/会話 (下部テキストボックス) ───
    # ADV 構造的証拠 (AUTOボタン・↓送りボタン・ADVツールバー) がある場合のみ
    _STORY_TAP_EXCLUDE = {"Rank", "Pank", "Runk", "AUTO", "SKIP", ">>", ">|"}
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6
                   and r["text"] not in _STORY_TAP_EXCLUDE]
    # 防御的 ADV 検出: adv_result が空でも AUTO + ADV固有アイコンで判定
    _has_auto_template = False
    if not ctx.adv_result.is_adv and not ctx.is_mini_conv and analysis_path:
        from tools.ap.constants import ANALYSIS_W, ANALYSIS_H
        _auto_roi = (0, 0, ANALYSIS_W, int(ANALYSIS_H * 0.15))
        _ft_auto = ASSET_MANAGER.match_single("adv_icon_auto", analysis_path, roi=_auto_roi)
        if _ft_auto and _ft_auto[2] >= 0.50:
            for _ft_icon in ("adv_icon_menu", "adv_icon_log", "adv_icon_skip"):
                _ft_m = ASSET_MANAGER.match_single(_ft_icon, analysis_path, roi=_auto_roi)
                if _ft_m and _ft_m[2] >= 0.40:
                    _has_auto_template = True
                    break
    _has_adv_evidence = ctx.adv_result.is_adv or ctx.is_mini_conv or _has_auto_template
    if lower_texts and len(ocr) <= 15 and not state.download_active and _has_adv_evidence:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── 右上吹き出しセリフ (メニュー画面上のキャラガイダンス) ───
    # 右上エリア (x>55%, y<35%) にテキストがあり、AUTO/>> ボタン等のUI要素と共存
    # → セリフが止まっている (前回と同一テキスト or phash安定) ならタップで送る
    _BUBBLE_EXCLUDE_EXACT_2 = {"AUTO", ">>", ">|", "D1", "×", "+", "■", "畄", "目", "SKIP"}
    _BUBBLE_EXCLUDE_SUBSTR_2 = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora",
                                "AUTO", "UTO", "UT0", "AUT")
    _BUBBLE_NUM_RE_2 = re.compile(r'^[\d,./:%+\-・\s]+$')
    # AUTO ボタン位置を検出 → その近傍 50px 以内のテキストも除外
    _auto_pos = None
    if analysis_path:
        _auto_m = ASSET_MANAGER.match_single("adv_icon_auto", analysis_path)
        if _auto_m and _auto_m[2] >= 0.60:
            _auto_pos = (_auto_m[0], _auto_m[1])
    _bubble_region = [r for r in ocr
                      if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                      and r["text"] not in _BUBBLE_EXCLUDE_EXACT_2
                      and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR_2)
                      and not _BUBBLE_NUM_RE_2.match(r["text"])
                      and len(r["text"]) > 2
                      and not (_auto_pos and abs(r["center"][0] - _auto_pos[0]) < 50
                               and abs(r["center"][1] - _auto_pos[1]) < 50)]
    if _bubble_region and len(ocr) <= 20:
        _bubble = _bubble_region[0]
        _bx, _by = _bubble["center"]
        logger.info(">>> 吹き出しセリフ送り '%s' (%d,%d)", _bubble["text"][:10], _bx, _by)
        tap_device(_bx, _by, state, "BUBBLE_TAP")
        return "BUBBLE_TAP", 0.3

    # ─── お知らせ一覧画面 (タブ: お知らせ/情報/不具合) → ×ボタンで閉じる ───
    _notice_tabs = sum(1 for kw in ["お知らせ", "情報", "不具合"]
                       if has_text(ocr, kw, min_conf=0.3))
    if _notice_tabs >= 2:
        # ×ボタン: 右上固定位置
        _nx, _ny = int(W * 0.97), int(H * 0.05)
        logger.info(">>> 【お知らせ一覧】 タブ%d個検出 → ×クローズ (%d,%d)", _notice_tabs, _nx, _ny)
        tap_device(_nx, _ny, state, "NOTICE_LIST_CLOSE")
        return "NOTICE_LIST_CLOSE", 1.0

    # ─── ログインボーナス等 ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        cx, cy = bonus_match["center"]
        logger.info(">>> ポップアップ '%s' (%d,%d)", bonus_match["text"], cx, cy)
        tap_device(cx, cy, state, "POPUP_TAP")
        return "POPUP_TAP", 1.0

    # ─── フォールバック: 何も見つからない ───
    logger.info(">>> 不明な画面 — WAIT_FOR_CHANGE (OCR %d件)", len(ocr))
    return "WAIT_FOR_CHANGE", 0
