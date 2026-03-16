"""
ap/handlers/dialog_phase.py — バトル前ガード + ダイアログハンドラ

Lines 1055-1120 of detect_and_act() を抽出:
  1. Battle glow SM pre-guard (#0-PRE)
  2. Pre-dialog finger guard (notice popup, finger blob, white hand pointer)
  3. Dialog handler call (handle_dialog_screen)

handle_dialog_screen は auto_pilot.py から循環importを避けるため
このファイルにコピーしている。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.state import PilotState
from tools.ap.constants import (
    _SPATIAL_MARGIN_TOP, _CLOSE_BTN_OFFSET,
    ANALYSIS_W, ANALYSIS_H,
    _BATTLE_CORE_KWS, _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS,
)
from tools.ap.helpers import has_any, has_text
from tools.ap.device import adb, tap_device, take_screenshot
from tools.ap.image_proc import (
    _run_battle_glow_sm,
    detect_notice_popup, detect_mini_conversation,
    find_finger_blobs, detect_white_hand_pointer,
    detect_dialog_frame_and_nav, process_paging_dialog,
    count_page_dots, detect_background_blur,
    imread_cached, ASSET_MANAGER, prepare_analysis_image,
    roi_to_device,
)
from lc.ocr import run_ocr

logger = logging.getLogger("auto_pilot")


# ═══════════════════════════════════════════════════════════════════
#  handle_dialog_screen — ダイアログ検出ハンドラ (#0-DIALOG)
# ═══════════════════════════════════════════════════════════════════

def handle_dialog_screen(
    state: "PilotState",
    analysis_path: Optional[Path],
    ocr: list,
    texts: list[str],
    is_battle_early: bool,
    has_finger_guard: bool,
    is_notice_popup: bool = False,
) -> Optional[tuple[str, float]]:
    """ダイアログ検出ハンドラ (#0-DIALOG)。

    detect_dialog_frame_and_nav() で金色枠/×/▷ を検出し、
    Spatial Gate / White Hand ガード / エスカレーション を経てタップ実行。

    is_notice_popup=True の場合:
      - 全ガード (指/SPATIAL_GATE/バトル) をバイパス
      - ページング可能 → 最終ページまで▷タップ後×で閉じる
      - ページング不可 → そのまま×で閉じる (確認ダイアログ等への誤転送なし)

    Returns: (action_name, wait_sec) or None (非ダイアログ / ガード発動)
    """
    if analysis_path is None:
        return None

    W, H = ANALYSIS_W, ANALYSIS_H

    _dlg = detect_dialog_frame_and_nav(
        analysis_path, W, H, ocr_texts=texts, roi=state.game_roi
    )
    if _dlg is None:
        return None

    _dlg_type, _dlg_x, _dlg_y = _dlg

    # ── お知らせポップアップ: 全ガードバイパス → ドット数でページング → × 閉じ ──
    if is_notice_popup:
        # ページドット数からページ数を把握
        _total_pages = count_page_dots(analysis_path)
        _remaining = max(0, _total_pages - 1)  # 現在1ページ目 → 残りN-1回▷
        logger.info(
            ">>> 【お知らせポップアップ】ドット=%d → ▷%d回タップ後×閉じ",
            _total_pages, _remaining,
        )

        # ▷ タップで最終ページまで進む
        if _remaining > 0 and _dlg_type in ("next", "bottom"):
            for _np in range(_remaining):
                # 2回目以降は再検出して▷座標を取得
                if _np > 0:
                    _img_path, _aw, _ah, _ = take_screenshot()
                    analysis_path = prepare_analysis_image(_img_path, _aw, _ah)
                    _re_dlg = detect_dialog_frame_and_nav(
                        analysis_path, W, H, ocr_texts=texts, roi=state.game_roi)
                    if _re_dlg is None:
                        logger.info("[NOTICE_POPUP] ダイアログ消失 (page=%d) → 完了", _np)
                        break
                    _dlg_type, _dlg_x, _dlg_y = _re_dlg
                    if _dlg_type == "close":
                        break  # もう▷がない → ×閉じへ
                tap_device(_dlg_x, _dlg_y, state, "NOTICE_PAGING_NEXT")
                logger.info("[NOTICE_POPUP] ▷タップ (%d/%d)", _np + 1, _remaining)
                time.sleep(0.3)

        # 最終ページ到達 → × で閉じる
        _img_path, _aw, _ah, _ = take_screenshot()
        analysis_path = prepare_analysis_image(_img_path, _aw, _ah)
        _close_dlg = detect_dialog_frame_and_nav(
            analysis_path, W, H, ocr_texts=texts, roi=state.game_roi)
        if _close_dlg is not None:
            _ct, _cx, _cy = _close_dlg
            # close でも next でも × 位置を探してタップ
            if _ct == "close":
                tap_device(_cx, _cy, state, "NOTICE_POPUP_CLOSE")
            else:
                # ▷ しか見つからない場合 → 右上固定座標で × を狙う
                _fx, _fy = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
                tap_device(_fx, _fy, state, "NOTICE_POPUP_CLOSE_FB")
            logger.info("[NOTICE_POPUP] ×閉じ完了 (total=%d pages)", _total_pages)

        state.pre_popup_tap_count = 0
        return "NOTICE_POPUP_CLOSE", 1.0

    # ── [SPATIAL GATE / 指ガード 撤廃] ──────────────────────────────────
    # 指ブロブ・金枠・白ハンドポインタによるダイアログスキップは廃止。
    # チュートリアルポップアップの▷/×処理をブロックする誤検出が多発するため。

    # ── バトル中 × 誤検出ガード ──────────────────────────────────────────
    if (_dlg is not None and _dlg_type == "close"
            and is_battle_early and _dlg_y < 100):
        logger.info(
            "[BATTLE_DIALOG_GUARD] close(%d,%d) y<100 → バトル上部UI誤検出 スキップ",
            _dlg_x, _dlg_y,
        )
        _dlg = None

    if _dlg is None:
        return None

    state.pre_popup_tap_count += 1
    state.dialog_detections += 1

    # ── エスカレーション (pre_popup_tap_count 一本化) ──
    # 8-11: BACK キー送信
    if state.pre_popup_tap_count >= 8:
        logger.warning(
            ">>> 【ダイアログ#0-DIALOG】累計%d回失敗 → BACK キー押下",
            state.pre_popup_tap_count,
        )
        try:
            adb("shell input keyevent KEYCODE_BACK")
        except Exception as _e:
            logger.debug("[DIALOG] BACK キー送信例外: %s", _e)
        # 12回以上: OCR再実行してダイアログ存在確認
        if state.pre_popup_tap_count >= 12:
            _recheck_path, _recheck_w, _recheck_h, _ = take_screenshot()
            _recheck_analysis = prepare_analysis_image(_recheck_path, _recheck_w, _recheck_h) if _recheck_path else None
            _recheck_ocr = run_ocr(_recheck_analysis) if _recheck_analysis else []
            _recheck_texts = [e.get("text", "") for e in _recheck_ocr]
            _recheck_dlg = detect_dialog_frame_and_nav(
                _recheck_analysis, W, H, ocr_texts=_recheck_texts,
                roi=state.game_roi) if _recheck_analysis else None
            if _recheck_dlg is None:
                logger.info(">>> 【ダイアログ#0-DIALOG】OCR再確認: ダイアログ消失 → スキップ")
                state.pre_popup_tap_count = 0
                return None
            logger.warning(">>> 【ダイアログ#0-DIALOG】OCR再確認: ダイアログ存続 → 座標更新してリトライ")
            _dlg_type, _dlg_x, _dlg_y = _recheck_dlg
            state.pre_popup_tap_count = 8  # BACK エスカレーションに留まる
        return "DIALOG_BACK_ESCALATION", 2.0

    if _dlg_type in ("next", "bottom"):
        # ── 背景ぼかし必須ガード: ポップアップは必ず背景がぼける ──
        # ホーム画面の金色枠装飾をダイアログと誤検出する問題の根本対策
        _blur_img = imread_cached(analysis_path) if analysis_path else None
        if _blur_img is not None:
            _bH, _bW = _blur_img.shape[:2]
            if not detect_background_blur(_blur_img, _bH, _bW):
                logger.info(
                    "[DIALOG_BLUR_GUARD] 背景ぼかしなし → ポップアップではない、PAGING スキップ")
                return None
        # ページング式ダイアログ: ▷ → … → × を一括処理
        logger.info(
            ">>> 【ダイアログ#0-DIALOG-PAGING】%s(%d,%d) (試行%d回) → process_paging_dialog",
            _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count,
        )
        _pg_result = process_paging_dialog(
            analysis_path, W, H, state,
            initial_dlg=(_dlg_type, _dlg_x, _dlg_y),
            ocr_texts=texts,
        )
        if _pg_result == "DIALOG_PAGING_TIMEOUT":
            # PAGING TIMEOUT → 右上固定位置で×をタップするフォールバック
            _close_x, _close_y = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
            logger.warning(
                "[PAGING_TIMEOUT_FALLBACK] ×未検出 → 右上固定座標(%d,%d)でクローズ試行",
                _close_x, _close_y)
            tap_device(_close_x, _close_y, state, "PAGING_TIMEOUT_CLOSE_FB")
        return _pg_result, 1.0
    else:
        # "close": × ボタンを即タップ
        # ── 確認ダイアログ (OK+キャンセル共存) → × ではなく OK 優先 ──
        _dlg_pos = has_any(ocr, _CONFIRM_POS_KWS)
        _dlg_neg = has_any(ocr, _CONFIRM_NEG_KWS)
        if _dlg_pos and _dlg_neg:
            _dp_x, _dp_y = _dlg_pos["center"]
            logger.info(
                "[Dialog#0] 確認ダイアログ OK優先 '%s'(%d,%d) タップ",
                _dlg_pos["text"], _dp_x, _dp_y,
            )
            tap_device(_dp_x, _dp_y, state, f"DIALOG_CONFIRM_OK '{_dlg_pos['text']}'")
            state.pre_popup_tap_count = 0
            return "DIALOG_CONFIRM_OK", 1.0
        # ── 4回連続失敗 → OK/確認ボタンを探してフォールバック ──
        if state.pre_popup_tap_count >= 4:
            _ok_ocr = has_any(ocr, ["OK", "確認", "決定", "おまかせ"])
            if _ok_ocr:
                _ok_cx, _ok_cy = _ok_ocr["center"]
                logger.info(
                    ">>> 【ダイアログ#0-DIALOG】close失敗%d回 → OKフォールバック '%s'(%d,%d)",
                    state.pre_popup_tap_count, _ok_ocr["text"], _ok_cx, _ok_cy,
                )
                tap_device(_ok_cx, _ok_cy, state, "DIALOG_OK_FALLBACK")
                state.pre_popup_tap_count = 0
                return "DIALOG_OK_FALLBACK", 1.0
            # OCR で OK 未検出 → ダイアログ下部中央をタップ
            _ok_fb_x, _ok_fb_y = roi_to_device(int(W * 0.7), int(H * 0.92), state.game_roi)
            logger.info(
                ">>> 【ダイアログ#0-DIALOG】close失敗%d回 → 下部中央フォールバック(%d,%d)",
                state.pre_popup_tap_count, _ok_fb_x, _ok_fb_y,
            )
            tap_device(_ok_fb_x, _ok_fb_y, state, "DIALOG_BOTTOM_FALLBACK")
            state.pre_popup_tap_count = 0
            return "DIALOG_BOTTOM_FALLBACK", 1.0
        logger.info(
            ">>> 【ダイアログ#0-DIALOG】%s(%d,%d) (試行%d回)",
            _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count,
        )
        tap_device(_dlg_x, _dlg_y, state, "DIALOG_CLOSE")
        return "DIALOG_CLOSE", 1.0


# ═══════════════════════════════════════════════════════════════════
#  handle_dialog_phase — ダイアログフェーズハンドラ (dispatch から呼ばれる)
# ═══════════════════════════════════════════════════════════════════

def handle_dialog_phase(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """バトル前ガード + ダイアログ検出フェーズ。

    1. Battle glow SM pre-guard (#0-PRE)
    2. Notice popup / 指ブロブ / 白ハンドポインタ の事前計算 (ctx に反映)
    3. handle_dialog_screen 呼び出し

    ctx.is_notice, ctx.pre_dialog_finger, ctx.white_hand_pos を更新する
    (後続ハンドラが参照するため)。
    """
    texts = ctx.texts
    joined = ctx.joined
    ocr = ctx.ocr
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── 【最優先 #0-PRE】バトル発光SM ガード ─────────────────────────────────
    # DIALOG_CLOSE が「通常攻撃」等のバトルアクションを踏み越えるのを防ぐ。
    # ① 「メニューが使用できません」トースト → DIALOG 誤検出スキップ (toast 自然消滅)
    # ② P1: 左キャラ発光 (character_selected=False) → GLOW_LEFT_CHAR
    # ③ P2: 右スキル発光 (character_selected=True) → GLOW_RIGHT_SKILL
    # ④ P3: 発光なし + character_selected → 通常攻撃 OCR フォールバック
    _is_battle_early = ctx.is_battle_early
    _battle_menu_toast = "メニューが使用できません" in joined
    if _is_battle_early and _battle_menu_toast:
        # メニューボタン誤タップ → トースト表示中。DIALOG_CLOSE を完全スキップして2秒待機
        logger.info("[#0-PRE] 「メニューが使用できません」トースト検出 → DIALOG_CLOSE スキップ (2s wait)")
        return "BATTLE_MENU_TOAST_WAIT", 2.0
    if _is_battle_early and analysis_path is not None:
        _pre_result = _run_battle_glow_sm(analysis_path, W, H, state, ocr, tag="#0-PRE")
        if _pre_result is not None:
            return _pre_result

    # ── 【お知らせポップアップ検出】PRE_DIALOG_GUARD バイパス ──────────
    _is_notice = False
    if analysis_path is not None:
        _is_notice = detect_notice_popup(analysis_path, texts)
    ctx.is_notice = _is_notice

    # ── 【#0-DIALOG 前ガード】指ブロブ検出時はダイアログ検出をスキップ ──────
    # お知らせポップアップ検出時はガードをバイパス (×で確実に閉じるため)
    # ADV/ミニ会話/動画シーン検出時はスキップ (指アイコンは出ない — 背景装飾の誤検出防止)
    _pre_dialog_finger = False
    _is_mini_conv = detect_mini_conversation(analysis_path) is not None if analysis_path else False
    ctx.is_mini_conv = _is_mini_conv
    _is_result_screen = any(
        any(k in t for k in ("Result", "リザルト", "次へ"))
        for t in texts
    )
    ctx.is_result_screen_flag = _is_result_screen
    # ADV/MOVIE シーンでは指ブロブ+金枠検出を完全スキップ (緑発光等の誤検出防止)
    _is_adv_or_movie = (
        ctx.adv_result.is_adv
        or state.current_scene in ("ADV", "MOVIE")
        or any(t in ("SKIP", "スキップ") for t in texts)
    )
    ctx.is_adv_or_movie = _is_adv_or_movie
    _white_hand_pos = None  # (cx, cy, score, direction) or None
    if analysis_path is not None and not _is_result_screen and not _is_notice and not _is_adv_or_movie and not _is_mini_conv:
        _pdg_blobs = find_finger_blobs(analysis_path, min_area=300, max_area=5000)
        _pdg_blobs = [b for b in _pdg_blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        if _pdg_blobs:
            # × ボタンが高信頼度で存在する場合は指ガードを抑制
            _close_match = ASSET_MANAGER.match_single("tutorial_dialog_close", analysis_path)
            if _close_match and _close_match[2] >= 0.85:
                logger.info("[PRE_DIALOG_GUARD] 指 %d 個だが ×(%.3f) → ガード抑制",
                            len(_pdg_blobs), _close_match[2])
            else:
                _pre_dialog_finger = True
                logger.info("[PRE_DIALOG_GUARD] 指ブロブ %d 個検出 → #0-DIALOG スキップ",
                            len(_pdg_blobs))
        if not _pre_dialog_finger:
            _white_hand_pos = detect_white_hand_pointer(analysis_path, threshold=0.90)
            if _white_hand_pos is not None:
                _pre_dialog_finger = True
                logger.info(
                    "[PRE_DIALOG_GUARD] 白ハンドポインタ (%d,%d) score=%.3f → #0-DIALOG スキップ",
                    _white_hand_pos[0], _white_hand_pos[1], _white_hand_pos[2],
                )
    ctx.pre_dialog_finger = _pre_dialog_finger
    ctx.white_hand_pos = _white_hand_pos

    # ─── 【最優先 #0-DIALOG】ダイアログ・ファースト ────────────
    _dialog_result = handle_dialog_screen(
        state, analysis_path, ocr, texts, _is_battle_early, _pre_dialog_finger,
        is_notice_popup=_is_notice)
    if _dialog_result is not None:
        return _dialog_result

    return None
