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
    ANALYSIS_W, ANALYSIS_H,
    _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS,
    _CURRENCY_SPEND_KWS, _OCR_BBOX_Y_PADDING,
    CLOSE_ACTION_WAIT, PHASH_THRESHOLD,
)
from tools.ap.helpers import has_any, has_text
from tools.ap.device import adb, tap_device, take_screenshot
from tools.ap.image_proc import (
    detect_popup_overlay,
    detect_dialog_frame_and_nav, process_paging_dialog,
    count_page_dots, detect_dialog, detect_dialog_nav, detect_dialog_corners,
    detect_popup_home_nav,
    detect_login_bonus_popup,
    ASSET_MANAGER, prepare_analysis_image,
    roi_to_device, smart_tap_button,
    detect_background_blur, imread_cached,
)
from lc.ocr import run_ocr
from lc.utils import compute_phash, phash_distance

logger = logging.getLogger("auto_pilot")


# ═══════════════════════════════════════════════════════════════════
#  handle_popup_home — ホームポップアップハンドラ (ダイアログ・お知らせポップアップとは独立)
# ═══════════════════════════════════════════════════════════════════

def handle_popup_home(
    state: "PilotState",
    analysis_path: Optional[Path],
    ocr_count: int = 0,
) -> Optional[tuple[str, float]]:
    """ホームポップアップ検出 → ドット数分 ▷ タップ → 最終ページで × 閉じ。

    popup_home_next / popup_home_close テンプレで検出・操作する。
    ダイアログ・お知らせポップアップの検出ロジックとは完全に独立。
    ポップアップ専用四隅テンプレ（長方形整合性チェック付き）で判定するため
    汎用 dialog_corners とは独立して動作する。
    OCR 0件の場合はスキップ（動画フレームの誤検出防止）。

    Returns: (action_name, wait_sec) or None
    """
    if analysis_path is None:
        return None
    if state.current_scene == "BATTLE":
        return None
    if ocr_count < 1:
        return None
    if not detect_popup_overlay(analysis_path):
        return None

    W, H = ANALYSIS_W, ANALYSIS_H
    _total_pages = count_page_dots(analysis_path)
    _remaining = max(0, _total_pages - 1)
    logger.info(">>> 【ホームポップアップ】ドット=%d → ▷%d回タップ後×閉じ", _total_pages, _remaining)

    # ▷ を検出してページ送り (▷ 優先: × より先に検出)
    if _remaining > 0:
        _nav = detect_popup_home_nav(analysis_path, prefer_close=False)
        if _nav and _nav[0] == "next":
            _nx, _ny = _nav[1], _nav[2]
            for _i in range(_remaining):
                _dx, _dy = roi_to_device(_nx, _ny, state.game_roi)
                tap_device(_dx, _dy, state, "POPUP_HOME_PAGING_NEXT")
                logger.info("[POPUP_HOME] ▷タップ (%d/%d)", _i + 1, _remaining)
                time.sleep(0.5)

    # 最終ページ → × ボタンを検出して閉じる (× 優先)
    time.sleep(0.3)
    _ss = take_screenshot()
    if _ss and _ss[0]:
        _close_analysis = prepare_analysis_image(Path(_ss[0]), _ss[1], _ss[2])
        _close_nav = detect_popup_home_nav(_close_analysis, prefer_close=True)
        if _close_nav and _close_nav[0] == "close":
            _cx, _cy = _close_nav[1], _close_nav[2]
            _dx, _dy = roi_to_device(_cx, _cy, state.game_roi)
            tap_device(_dx, _dy, state, "POPUP_HOME_CLOSE")
            logger.info("[POPUP_HOME] ×閉じ完了 (total=%d pages)", _total_pages)
            return "POPUP_HOME_CLOSE", CLOSE_ACTION_WAIT

    # × テンプレ未検出 → 右上固定座標フォールバック
    _fx, _fy = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
    tap_device(_fx, _fy, state, "POPUP_HOME_CLOSE_FALLBACK")
    logger.info("[POPUP_HOME] × 未検出 → 右上固定座標 (%d,%d) で閉じる", _fx, _fy)
    return "POPUP_HOME_CLOSE", CLOSE_ACTION_WAIT


# ═══════════════════════════════════════════════════════════════════
#  handle_dialog_screen — ダイアログ検出ハンドラ (#0-DIALOG)
# ═══════════════════════════════════════════════════════════════════

def handle_dialog_screen(
    state: "PilotState",
    analysis_path: Optional[Path],
    ocr: list,
    texts: list[str],
    in_battle_ctx: bool,
    is_notice_popup: bool = False,
) -> Optional[tuple[str, float]]:
    """ダイアログ検出ハンドラ (#0-DIALOG)。

    detect_dialog_frame_and_nav() で金色枠/×/▷ を検出し、
    バトルガード / エスカレーション を経てタップ実行。

    is_notice_popup=True の場合:
      - 全ガード (SPATIAL_GATE/バトル) をバイパス
      - ページング可能 → 最終ページまで▷タップ後×で閉じる
      - ページング不可 → そのまま×で閉じる (確認ダイアログ等への誤転送なし)

    Returns: (action_name, wait_sec) or None (非ダイアログ / ガード発動)
    """
    if analysis_path is None:
        return None

    # 名前入力画面: ダイアログとして処理せず tutorial ハンドラに委譲
    if any("名前" in t or "プレイヤー名" in t for t in texts):
        return None

    W, H = ANALYSIS_W, ANALYSIS_H

    _dlg = detect_dialog_frame_and_nav(
        analysis_path, W, H, ocr_texts=texts, roi=state.game_roi
    )

    # ── お知らせポップアップ: ダイアログ枠未検出でも処理続行 ──
    # OCR で「今日は表示しない」が確定しているため、▷/× を直接検索する
    if is_notice_popup and _dlg is None:
        _notice_nav = detect_dialog_nav(analysis_path, W, H)
        if _notice_nav is not None:
            _dlg = _notice_nav
            logger.info("[NOTICE_POPUP] ダイアログ枠未検出だが ▷/× を直接検出 → 続行")
        else:
            # ▷/× も見つからない → 右上固定座標で × を狙う
            _fx, _fy = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
            logger.info("[NOTICE_POPUP] ▷/× 未検出 → 右上 × 固定座標 (%d,%d) で閉じる", _fx, _fy)
            tap_device(_fx, _fy, state, "NOTICE_POPUP_CLOSE_DIRECT")
            return "NOTICE_POPUP_CLOSE", CLOSE_ACTION_WAIT

    if _dlg is None:
        return None

    _dlg_type, _dlg_x, _dlg_y = _dlg

    # ── 通常ダイアログ優先: OK ボタンがあればお知らせポップアップより先に処理 ──
    # 画質設定等の通常ダイアログが NOTICE_POPUP に誤判定される問題の根本対策
    if is_notice_popup and _dlg_type in ("next", "bottom"):
        _ok_btn_early = has_any(ocr, _CONFIRM_POS_KWS, exact=True)
        _cancel_btn_early = has_any(ocr, _CONFIRM_NEG_KWS, exact=True)
        if _ok_btn_early and not _cancel_btn_early:
            _ok_x, _ok_y = _ok_btn_early["center"]
            logger.info("[Dialog#0] NOTICE_POPUP だが OK '%s'(%d,%d) 検出 → 通常ダイアログとして処理",
                        _ok_btn_early["text"], _ok_x, _ok_y)
            tap_device(_ok_x, _ok_y, state, f"DIALOG_OK_DIRECT '{_ok_btn_early['text']}'")
            state.pre_popup_tap_count = 0
            return "DIALOG_OK_DIRECT", CLOSE_ACTION_WAIT

    # ── お知らせポップアップ: ドット数分 ▷ タップ → × 閉じ ──
    if is_notice_popup:
        _total_pages = count_page_dots(analysis_path)
        _remaining = max(0, _total_pages - 1)
        logger.info(
            ">>> 【お知らせポップアップ】ドット=%d → ▷%d回タップ後×閉じ",
            _total_pages, _remaining,
        )

        # ▷ を確定回数分タップ（途中の再検出は不要）
        if _remaining > 0 and _dlg_type in ("next", "bottom"):
            for _np in range(_remaining):
                tap_device(_dlg_x, _dlg_y, state, "NOTICE_PAGING_NEXT")
                logger.info("[NOTICE_POPUP] ▷タップ (%d/%d)", _np + 1, _remaining)
                time.sleep(0.5)

        # 最終ページ → × で閉じる（右上固定座標）
        time.sleep(0.3)
        _fx, _fy = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
        tap_device(_fx, _fy, state, "NOTICE_POPUP_CLOSE")
        logger.info("[NOTICE_POPUP] ×閉じ完了 (total=%d pages)", _total_pages)

        state.pre_popup_tap_count = 0
        return "NOTICE_POPUP_CLOSE", CLOSE_ACTION_WAIT

    # ── [SPATIAL GATE 撤廃] ──────────────────────────────────
    # handle_dialog_screen 内部での指ブロブ・金枠によるダイアログスキップは廃止。
    # チュートリアルダイアログの▷/×処理をブロックする誤検出が多発するため。
    # ※ 指ガードは handle_dialog_phase → ctx.pre_dialog_finger 経由で
    #   tutorial.py の Asset Match (DIALOG_NAV_RIGHT) に対してのみ有効。

    # ── バトル中 × 誤検出ガード ──────────────────────────────────────────
    # 四隅テンプレが検出済みなら本物のダイアログ → ガードをバイパス
    if (_dlg is not None and _dlg_type == "close"
            and in_battle_ctx and _dlg_y < 100):
        _has_corners = detect_dialog_corners(analysis_path)
        if _has_corners:
            logger.info(
                "[BATTLE_DIALOG_GUARD] close(%d,%d) y<100 だが四隅テンプレあり → ガードバイパス",
                _dlg_x, _dlg_y,
            )
        else:
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
        # ── OK のみダイアログ優先: ▷/bottom 検出でも OK ボタンがあれば OK タップ ──
        # 「魔法少女解放」等の通知ダイアログは ▷ ではなく OK で閉じる
        _ok_btn = has_any(ocr, _CONFIRM_POS_KWS, exact=True)
        _cancel_btn = has_any(ocr, _CONFIRM_NEG_KWS, exact=True)
        # 名前入力画面: OK があるが先に名前を入力する必要がある → tutorial ハンドラに委譲
        _is_name_input = any("名前" in t or "プレイヤー名" in t for t in texts)
        if _ok_btn and not _cancel_btn and not _is_name_input:
            _ok_x, _ok_y = _ok_btn["center"]
            logger.info("[Dialog#0] OK のみダイアログ検出 → OK '%s'(%d,%d) タップ (PAGING 回避)",
                        _ok_btn["text"], _ok_x, _ok_y)
            tap_device(_ok_x, _ok_y, state, f"DIALOG_OK_DIRECT '{_ok_btn['text']}'")
            state.pre_popup_tap_count = 0
            return "DIALOG_OK_DIRECT", CLOSE_ACTION_WAIT
        # ── ダイアログ再確認ガード ──
        # 1. 四隅テンプレ優先。失敗時はページドット+背景ぼかしでフォールバック
        _has_dialog_frame = detect_dialog_corners(analysis_path) if analysis_path else False
        if not _has_dialog_frame:
            # フォールバック: ページドット≥1 + 背景ぼかし → 本物のダイアログとみなす
            _fb_dots = count_page_dots(analysis_path) if analysis_path else 0
            _fb_blur = detect_dialog(analysis_path, W, H, require_blur=True) if analysis_path else None
            if _fb_dots >= 1 and _fb_blur:
                logger.info("[DIALOG_FRAME_GUARD] 四隅テンプレなし → ドット=%d+背景ぼかしあり → PAGING フォールバック続行",
                            _fb_dots)
            else:
                logger.debug("[DIALOG_FRAME_GUARD] 四隅テンプレなし (dots=%d, blur=%s) → PAGING スキップ (dlg_type=%s)",
                            _fb_dots, _fb_blur is not None, _dlg_type)
                # ガードでスキップ → 未処理なのでカウンタを戻す
                state.pre_popup_tap_count = max(0, state.pre_popup_tap_count - 1)
                return None
        # 2. "next" でページドット=0 は誤検出 (▷がある=ページ複数=ドット≥1)
        if _dlg_type == "next":
            _next_dots = count_page_dots(analysis_path) if analysis_path else 0
            if _next_dots < 1:
                logger.info("[DIALOG_FRAME_GUARD] next だがドット=0 → 誤検出、PAGING スキップ")
                return None
        if analysis_path and not detect_dialog(analysis_path, W, H, require_blur=True):
            # 四隅テンプレあり + 背景ぼかしなし → ドット数で追加確認
            _guard_dots = count_page_dots(analysis_path)
            _guard_nav = detect_dialog_nav(analysis_path, W, H)
            if _guard_dots >= 2:
                # バトル中はドットだけでは不十分 (UIアイコンの誤検出)
                # → 四隅テンプレ (dialog_corner) で本物のダイアログか確認
                # OCR にバトルKW が無くても current_scene が BATTLE なら同様にガード
                _in_battle = in_battle_ctx
                # 背景ぼかしなし + ドット検出: 常にダイアログ四隅テンプレで確認。
                # バトルUIアイコンがドットに見えるケースが多いため、
                # _in_battle に関わらず四隅テンプレが無ければスキップする。
                _has_corner = detect_dialog_corners(analysis_path)
                _has_dialog_full = detect_dialog(analysis_path, W, H, require_blur=False) if _has_corner else None
                if not _has_corner:
                    logger.info(
                        "[DIALOG_BLUR_GUARD] ドット=%d だが四隅テンプレなし → PAGING スキップ (battle=%s)",
                        _guard_dots, _in_battle)
                    return None
                if not _has_dialog_full:
                    logger.info(
                        "[DIALOG_BLUR_GUARD] ドット=%d + 四隅テンプレあり + ▷/×未検出 → PAGING 続行 (battle=%s)",
                        _guard_dots, _in_battle)
                else:
                    _nav_type = _has_dialog_full[0] if isinstance(_has_dialog_full, tuple) else "?"
                    logger.info(
                        "[DIALOG_BLUR_GUARD] ドット=%d + 四隅テンプレあり + ▷/×=%s → PAGING 続行 (battle=%s)",
                        _guard_dots, _nav_type, _in_battle)
            else:
                logger.info(
                    "[DIALOG_BLUR_GUARD] 背景ぼかしなし+ドット=%d+▷/×=%s → ダイアログではない、PAGING スキップ",
                    _guard_dots, _guard_nav is not None)
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
            # PAGING TIMEOUT → 右上隅の×固定位置タップでクローズ試行
            # ダイアログの×は常に右上(~97%, ~5%)にあるが、テンプレ不一致で検出失敗する場合がある
            _close_x, _close_y = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
            logger.warning(
                "[PAGING_TIMEOUT_FALLBACK] ×未検出 → 右上固定タップ(%d,%d)でクローズ試行",
                _close_x, _close_y)
            tap_device(_close_x, _close_y, state, "PAGING_TIMEOUT_CLOSE_TAP")
        return _pg_result, CLOSE_ACTION_WAIT
    else:
        # "close": × ボタンを即タップ
        # ── 確認ダイアログ (OK+キャンセル共存) → × ではなく OK 優先 ──
        _dlg_pos = has_any(ocr, _CONFIRM_POS_KWS, exact=True)
        _dlg_neg = has_any(ocr, _CONFIRM_NEG_KWS, exact=True)
        if _dlg_pos and _dlg_neg:
            _dp_x, _dp_y = _dlg_pos["center"]
            logger.info(
                "[Dialog#0] 確認ダイアログ OK優先 '%s'(%d,%d) タップ",
                _dlg_pos["text"], _dp_x, _dp_y,
            )
            tap_device(_dp_x, _dp_y, state, f"DIALOG_CONFIRM_OK '{_dlg_pos['text']}'")
            state.pre_popup_tap_count = 0
            return "DIALOG_CONFIRM_OK", CLOSE_ACTION_WAIT
        # ── OK のみダイアログ (キャンセルなし) → 2回で OK 直タップ ──
        if state.pre_popup_tap_count >= 2 and _dlg_pos and not _dlg_neg:
            _dp_x, _dp_y = _dlg_pos["center"]
            _dp_y_adj = max(0, _dp_y - 6)
            logger.info(
                "[Dialog#0] OK のみダイアログ (× 失敗%d回) → OK直タップ '%s'(%d,%d)",
                state.pre_popup_tap_count, _dlg_pos["text"], _dp_x, _dp_y_adj,
            )
            tap_device(_dp_x, _dp_y_adj, state, f"DIALOG_OK_ONLY '{_dlg_pos['text']}'")
            state.pre_popup_tap_count = 0
            return "DIALOG_OK_ONLY", CLOSE_ACTION_WAIT
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
                return "DIALOG_OK_FALLBACK", CLOSE_ACTION_WAIT
            # OCR で OK 未検出 → ダイアログ下部中央をタップ
            _ok_fb_x, _ok_fb_y = roi_to_device(int(W * 0.7), int(H * 0.92), state.game_roi)
            logger.info(
                ">>> 【ダイアログ#0-DIALOG】close失敗%d回 → 下部中央フォールバック(%d,%d)",
                state.pre_popup_tap_count, _ok_fb_x, _ok_fb_y,
            )
            tap_device(_ok_fb_x, _ok_fb_y, state, "DIALOG_BOTTOM_FALLBACK")
            state.pre_popup_tap_count = 0
            return "DIALOG_BOTTOM_FALLBACK", CLOSE_ACTION_WAIT
        logger.info(
            ">>> 【ダイアログ#0-DIALOG】%s(%d,%d) (試行%d回)",
            _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count,
        )
        tap_device(_dlg_x, _dlg_y, state, "DIALOG_CLOSE")
        return "DIALOG_CLOSE", CLOSE_ACTION_WAIT


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

    # ── 四隅テンプレ結果を ctx にキャッシュ (Phase 4 で再利用) ──
    if ctx.has_dialog_corners is None and analysis_path is not None:
        ctx.has_dialog_corners = detect_dialog_corners(analysis_path)

    # ─── バトルトーストガード ─────────────────────────────────
    # 「メニューが使用できません」トースト → DIALOG 誤検出スキップ (toast 自然消滅)
    _is_battle_ctx = ctx.in_battle_ctx
    _battle_menu_toast = "メニューが使用できません" in joined
    if _is_battle_ctx and _battle_menu_toast:
        logger.info("[#0-PRE] 「メニューが使用できません」トースト検出 → DIALOG_CLOSE スキップ (2s wait)")
        return "BATTLE_MENU_TOAST_WAIT", 2.0

    # ── 【ホームポップアップ検出】ダイアログ・お知らせポップアップとは独立 ──────────
    if analysis_path is not None:
        _popup_home_result = handle_popup_home(
            state, analysis_path, ocr_count=len(ocr))
        if _popup_home_result is not None:
            return _popup_home_result

    # ── 【ログインボーナスポップアップ】エッジ投影ベース検出 → × で閉じ ──────
    # OK/キャンセル等の操作ボタンがある通常ダイアログは LOGIN_BONUS ではない
    _has_action_btn = any(has_text(ocr, kw, min_conf=0.5)
                         for kw in ("OK", "キャンセル", "はい", "いいえ", "決定"))
    if analysis_path is not None and not _is_battle_ctx and not _has_action_btn:
        _lbp = detect_login_bonus_popup(analysis_path)
        if _lbp is not None:
            _close_info = _lbp["close_btn"]
            _lbx, _lby = _close_info[0], _close_info[1]
            logger.info(">>> 【ログインボーナスポップアップ】 × テンプレ(%.2f) (%d,%d) タップ",
                        _close_info[2], _lbx, _lby)
            tap_device(_lbx, _lby, state, "LOGIN_BONUS_CLOSE")
            return "LOGIN_BONUS_CLOSE", 1.0

    # ── 【お知らせ一覧画面】タブ3つ全て検出 → × で閉じ ──────────
    _notice_list_tabs = ["お知らせ", "情報", "不具合"]
    _notice_list_hits = sum(1 for kw in _notice_list_tabs if has_text(ocr, kw, min_conf=0.3))
    if _notice_list_hits >= 3:
        _close_m = ASSET_MANAGER.match_single("close_btn", analysis_path)
        if _close_m and _close_m[2] >= 0.50:
            tap_device(_close_m[0], _close_m[1], state, "NOTICE_LIST_CLOSE")
            logger.info(">>> 【お知らせ一覧】 ×テンプレート(%d,%d score=%.2f) で閉じる",
                        _close_m[0], _close_m[1], _close_m[2])
            return "NOTICE_LIST_CLOSE", 1.0
        _nx, _ny = roi_to_device(int(W * 0.975), int(H * 0.055), state.game_roi)
        tap_device(_nx, _ny, state, "NOTICE_LIST_CLOSE_FB")
        logger.info(">>> 【お知らせ一覧】 × 固定座標(%d,%d) で閉じる", _nx, _ny)
        return "NOTICE_LIST_CLOSE", 1.0

    # ── 【Android 権限ダイアログ】単独「許可」ボタン ──────────
    if not ctx.confirm_pos and not _is_battle_ctx:
        _perm_btn = has_any(ocr, ["許可", "Allow", "ALLOW"])
        _perm_ctx = has_any(ocr, ["通知", "位置情報", "ストレージ", "カメラ",
                                   "notification", "permission"])
        if _perm_btn and _perm_ctx:
            _pm_x, _pm_y = _perm_btn["center"]
            logger.info(">>> [PERMISSION] Android権限ダイアログ '%s' (%d,%d) タップ",
                        _perm_btn["text"], _pm_x, _pm_y)
            tap_device(_pm_x, _pm_y, state, f"PERMISSION_ALLOW '{_perm_btn['text']}'")
            return "PERMISSION_ALLOW", 1.0

    # ── 【設定メニュー誤起動】BACK キーで閉じる ──────────
    _settings_menu_kws = ["サポート", "データ引き継ぎ", "キャッシュクリア", "お問い合わせ"]
    _story_context_kws = ["1-1", "1-2", "第1幕", "第1階層", "第2幕", "WAVE", "AUTO", "1-3", "2-1"]
    _in_story_ctx = any(kw in joined for kw in _story_context_kws)
    _settings_hits = sum(1 for kw in _settings_menu_kws
                         if has_text(ocr, kw, min_conf=0.3) is not None)
    if not _in_story_ctx and _settings_hits >= 2:
        logger.info(">>> 【設定メニュー誤起動】 BACK キーで閉じる")
        adb("shell input keyevent 4")
        return "SETTINGS_BACK", 1.5

    # ── 【Play Games ポップアップ】BACK キーで閉じる ──────────
    if has_text(ocr, "Play Games", min_conf=0.3) or has_text(ocr, "Play ゲーム", min_conf=0.3):
        logger.info(">>> 【Play Games ポップアップ】 BACK キーで閉じる")
        adb("shell input keyevent 4")
        return "PLAY_GAMES_BACK", 1.0

    # ── 【確認ダイアログ】OK+Cancel 共存 or 完了通知 → OK タップ ──────────
    # ダイアログ証拠 (四隅テンプレ or 背景ぼかし) を確認してから処理する。
    # 課金保護: 通貨消費KW → キャンセル。スキップ確認 → キャンセル。
    _confirm_pos = ctx.confirm_pos
    _confirm_neg = ctx.confirm_neg
    _dl_is_complete = any("完了" in t or "Complete" in t for t in texts)
    _is_completion_dialog = _confirm_pos and _dl_is_complete
    _has_dialog_evidence = False
    if (_confirm_pos and _confirm_neg) or _is_completion_dialog:
        if analysis_path is None:
            _has_dialog_evidence = True
        elif detect_dialog_corners(analysis_path):
            _has_dialog_evidence = True
        else:
            _blur_img = imread_cached(analysis_path)
            if _blur_img is not None:
                _bH, _bW = _blur_img.shape[:2]
                if detect_background_blur(_blur_img, _bH, _bW):
                    _has_dialog_evidence = True
        if not _has_dialog_evidence and not _is_completion_dialog:
            logger.info("[ConfirmDialog] 四隅テンプレ/背景ぼかし未検出 → スキップ")
    if ((_confirm_pos and _confirm_neg and _has_dialog_evidence) or _is_completion_dialog):
        # 課金保護: 通貨消費キーワード → キャンセル
        _is_currency = any(kw in joined for kw in _CURRENCY_SPEND_KWS)
        if _is_currency and _confirm_neg:
            _cn_x, _cn_y = _confirm_neg["center"]
            _cn_y_adj = max(0, _cn_y - _OCR_BBOX_Y_PADDING)
            logger.info("[ConfirmDialog] 課金保護: → キャンセル '%s' タップ",
                        _confirm_neg["text"])
            tap_device(_cn_x, _cn_y_adj, state,
                       f"CURRENCY_CANCEL '{_confirm_neg['text']}'")
            return "CURRENCY_CANCEL", 1.0
        # スキップ確認ダイアログ → キャンセル (スキップ禁止)
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
        _cp_y_adj = max(0, _cp_y - _OCR_BBOX_Y_PADDING)
        _neg_label = _confirm_neg["text"] if _confirm_neg else "(なし)"
        logger.info(
            "[ConfirmDialog] '%s' (%d,%d→Y%d) タップ (否定='%s'無視)",
            _confirm_pos["text"], _cp_x, _cp_y, _cp_y_adj, _neg_label,
        )
        # DL完了ダイアログ: phash 検証付きリトライ
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
            from tools.ap.helpers import log_milestone as _lm
            _lm(state, "DL_END")
            return "DL_COMPLETE_OK", 1.0
        tap_device(_cp_x, _cp_y_adj, state, f"CONFIRM_DIALOG_OK '{_confirm_pos['text']}'")
        return "ADV_CHOICE", 1.0

    # ── 【お知らせポップアップ検出】──────────
    _is_notice = False
    if analysis_path is not None:
        _popup = detect_popup_overlay(analysis_path, texts)
        _is_notice = _popup is not None and _popup.get("is_notice", False)
    ctx.is_notice = _is_notice

    # ── 【確認ダイアログ「以下の内容でよろしいですか」】SmartTap OK ──
    _confirm_dlg = has_text(ocr, "以下の内容でよろしいですか", min_conf=0.3)
    if _confirm_dlg:
        _ok_bottom = next(
            (item for item in ocr
             if "OK" in item.get("text", "") and item["center"][1] > H * 0.6),
            None
        )
        if _ok_bottom:
            _ocr_cx, _ocr_cy = _ok_bottom["center"]
        else:
            _ocr_cx, _ocr_cy = roi_to_device(
                int(W * 0.70), int(H * 0.88), state.game_roi)
        _cx, _cy = smart_tap_button(analysis_path, _ocr_cx, _ocr_cy, ocr_items=ocr)
        logger.info(">>> 【確認ダイアログ】 SmartTap OK (%d,%d)", _cx, _cy)
        tap_device(_cx, _cy, state, "CONFIRM_DIALOG_OK")
        return "CONFIRM_DIALOG_OK", 1.0

    # ─── 【#0-DIALOG】ダイアログ・ファースト ────────────
    # 指アイコン+金枠は dispatch Phase 2 (handle_finger_priority) で処理済み
    _dialog_result = handle_dialog_screen(
        state, analysis_path, ocr, texts, _is_battle_ctx,
        is_notice_popup=_is_notice)
    if _dialog_result is not None:
        return _dialog_result

    # ── 【チュートリアルポップアップ】形状ベース検出 ──
    # ダイアログ四隅 + ページドット ≥ 1 + 背景ぼかし + ▷/× ボタン
    # BATTLE シーンでは誤検出防止のためスキップ
    _in_battle_popup = state.current_scene == "BATTLE" or getattr(state, "_from_battle", False)
    if not _in_battle_popup and analysis_path is not None:
        _tp_corners = ctx.has_dialog_corners if ctx.has_dialog_corners is not None else (
            detect_dialog_corners(analysis_path))
        if _tp_corners:
            _tp_dots = count_page_dots(analysis_path)
            _tp_blur_img = imread_cached(analysis_path)
            _tp_blur = (_tp_blur_img is not None
                        and detect_background_blur(_tp_blur_img,
                                                   _tp_blur_img.shape[0],
                                                   _tp_blur_img.shape[1]))
            _tp_nav = detect_dialog(analysis_path, W, H) if (_tp_dots >= 1 and _tp_blur) else None
            if _tp_nav is not None:
                _nav_type, _nx, _ny = _tp_nav
                if _nav_type == "close" and _tp_dots < 2:
                    logger.info(">>> 【チュートリアルポップアップ】 ×→(%d,%d) dots=%d",
                                _nx, _ny, _tp_dots)
                    tap_device(_nx, _ny, state, "TUTORIAL_POPUP_CLOSE")
                    return "TUTORIAL_POPUP", 1.0
                if _nav_type == "close" and _tp_dots >= 2:
                    logger.info(">>> 【チュートリアルポップアップ→PAGING】 dots=%d, ×検出→先にページ走査",
                                _tp_dots)
                    _arr_x, _arr_y = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
                    _pg_result = process_paging_dialog(
                        analysis_path, W, H, state,
                        initial_dlg=("next", _arr_x, _arr_y),
                        ocr_texts=texts,
                    )
                    return _pg_result, 1.0
                logger.info(">>> 【チュートリアルポップアップ→PAGING】 ▷(%d,%d) dots=%d → 全ページ走査",
                            _nx, _ny, _tp_dots)
                _pg_result = process_paging_dialog(
                    analysis_path, W, H, state,
                    initial_dlg=(_nav_type, _nx, _ny),
                    ocr_texts=texts,
                )
                return _pg_result, 1.0

    return None
