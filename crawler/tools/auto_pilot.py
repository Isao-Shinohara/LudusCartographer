#!/usr/bin/env python3
"""
auto_pilot.py — まどドラ自律操縦スクリプト (ハイブリッド版)

1秒 phash ポーリング → 5秒変化なしで強制 OCR → 指差しアイコン最優先タップ。
WAIT_FOR_CHANGE 後の閾値引き上げを廃止し、デッドロックを根絶。

使い方:
    cd crawler
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\
    venv/bin/python -u tools/auto_pilot.py
"""
from __future__ import annotations

import argparse
import gc
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

# ─── SIGSEGV 防止: OpenMP / cv2 スレッド競合対策 ─────────────────
# OpenMP スレッドの重複を許可 (PaddlePaddle + OpenCV 共存時のSIGSEGV防止)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
# OpenMP スレッド数を制限してメモリ競合を防ぐ
os.environ.setdefault("OMP_NUM_THREADS", "2")
# OpenCV のスレッド数も制限
os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
import cv2
import json
import numpy as np
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルート (ap/constants.py から import)
sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.ocr import run_ocr, find_best
from lc.utils import (
    get_android_serial, compute_phash, phash_distance, ensure_adb_connection,
    uninstall_app, is_app_installed, open_play_store, WIFI_DEVICE_ADDR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_pilot")

# PIL/Pillow の DEBUG STREAM ログを抑制 (スクリーンショット毎に IHDR/sRGB/IDAT が出る)
logging.getLogger("PIL").setLevel(logging.WARNING)
logging.getLogger("PIL.PngImagePlugin").setLevel(logging.WARNING)

# ─── 定数: ap/constants.py から一括 import ───
from tools.ap.constants import (  # noqa: E402
    _CRAWLER_ROOT, SCREENSHOT_PATH, ANALYSIS_PATH, REMOTE_PATH, EVIDENCE_DIR,
    MAX_ITERATIONS, POLL_INTERVAL, PHASH_THRESHOLD, FORCE_ANALYZE_AFTER,
    STALL_TIMEOUT, BATTLE_WAIT, DOWNLOAD_WAIT, MIN_TAP_INTERVAL,
    WATCHDOG_DEADLOCK_THRESHOLD, WATCHDOG_MAX_SOFT_RECOVERIES,
    WATCHDOG_MAX_TOTAL_RECOVERIES, APP_PACKAGE, APP_ACTIVITY,
    WATCHDOG_EXEMPT_ACTIONS, ADV_RAPID_PHASH_MAX, BLACKOUT_BRIGHTNESS,
    _DEBUG_SAVE_IMAGES, _GOLD_UI_ACTIONS, _SCENE_REEVAL_THRESHOLD,
    _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS, _UI_TEXT_KWS, _SINGLE_ONLY,
    _DIALOG_FIRST_KWS, _BATTLE_CORE_KWS, _BATTLE_UI_KWS,
    ANALYSIS_W, ANALYSIS_H,
    _OCR_BBOX_Y_PADDING, _GLOW_CENTER_Y_OFFSET,
    _GOLD_BTN_RETRY_Y_OFFSET, _FINGER_TIP_RATIO,
    _RIGHT_PANEL_X, _CHAR_HEAD_X1, _CHAR_HEAD_X2,
    _CHAR_HEAD_Y1, _CHAR_HEAD_Y2, _SPATIAL_MARGIN_TOP, _CLOSE_BTN_OFFSET,
    OCR_LANG, OCR_MIN_CONF, SCENE_INTERVAL,
    _TRANSITION_SLOW_SEC, _TRANSITION_HISTORY_MAX,
    _IMMEDIATE_ACTIONS,
)

# ─── 状態クラス: ap/state.py から import ───
from tools.ap.state import PilotState, StallCounter, TapCandidate  # noqa: E402
# ─── ヘルパー: ap/helpers.py から import ───
from tools.ap.helpers import (  # noqa: E402
    classify_scene, text_core_center, save_evidence,
    has_any, has_text, all_texts,
)
# ─── デバイス操作: ap/device.py から import ───
import tools.ap.device as _ap_device  # noqa: E402
from tools.ap.device import (  # noqa: E402
    set_device_serial, set_scrcpy_device,
    adb, tap_device, swipe, swipe_device, take_screenshot, manage_scrcpy,
    get_device_resolution, _query_status_bar_height, check_adb_liveness,
)
# DEVICE_SERIAL / SCRCPY_DEVICE はモジュール変数 — 読取りは _ap_device 経由
# 後方互換 re-export 用プロパティ代替
def __getattr__(name):
    if name == "DEVICE_SERIAL":
        return _ap_device.DEVICE_SERIAL
    if name == "SCRCPY_DEVICE":
        return _ap_device.SCRCPY_DEVICE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 排除された偽の指ブロブキャッシュ (debug_latest_tap.png への [REJECTED] 描画用)
_rejected_finger_blobs: list = []

# Ctrl+C シグナルハンドラ用: main() で設定する PilotState への参照
_pilot_state_ref: Optional["PilotState"] = None



# ─── 画像処理: ap/image_proc.py から import ───
from tools.ap.image_proc import (  # noqa: E402
    detect_game_roi, roi_to_device, is_dark_screen, is_tutorial_walk_scene,
    prepare_analysis_image,
    find_finger_blobs, detect_white_hand_pointer, create_finger_mask_image,
    detect_guide_glow, _run_battle_glow_sm, detect_active_battle_char,
    find_gold_frame_near, is_adv_toolbar_cached, detect_adv_advance_icon,
    is_adv_toolbar_visible, detect_movie_skip_button, detect_mini_conversation,
    detect_tutorial_dialog_nav, detect_dialog_frame_and_nav,
    process_paging_dialog, detect_notice_popup, count_page_dots,
    detect_text_input_area,
    detect_tutorial_gold_swipe, detect_tutorial_gold_button_tap,
    smart_tap_button, find_golden_highlighted_button, find_3d_arrow,
    AssetManager, ASSET_MANAGER,
    detect_adv_scene, detect_adv_scene_cached, AdvSceneResult,
)


# ─── Result画面ハンドラ ──────────────────────────────
_RESULT_NEXT_X_RATIO = 0.785
_RESULT_NEXT_Y_RATIO = 0.914

# パーティ編成画面の除外キーワード (Lv.1 が出るが Result ではない)
_FORMATION_KWS = ["パーティ", "編成", "キオク", "ポートレイト", "自動編成"]


def _is_result_screen(ocr: list, texts: list[str]) -> tuple[bool, str]:
    """Result画面判定。戻り値: (is_result, subtype)
    subtype: "GACHA" | "BATTLE" | ""
    """
    # 除外: パーティ編成画面
    if any(kw in t for kw in _FORMATION_KWS for t in texts):
        return False, ""
    # ガチャ結果: NEW×3 以上
    new_count = sum(1 for t in texts if t == "NEW")
    if new_count >= 3:
        return True, "GACHA"
    # バトルResult: Result / EXP / Lv.1 / リザルト
    if (has_text(ocr, "Result") or has_text(ocr, "EXP")
            or has_text(ocr, "Lv.1") or has_text(ocr, "リザルト")):
        return True, "BATTLE"
    return False, ""


def _find_next_button(ocr: list, W: int, H: int, subtype: str) -> Optional[dict]:
    """Result画面の進行ボタンを検索。位置フィルタ付き。"""
    if subtype == "GACHA":
        return has_text(ocr, "OK", min_conf=0.5)
    # BATTLE: 右下 (y>60%, x>50%) の「次へ」/「NEXT」
    for item in ocr:
        txt = item.get("text", "")
        if "次へ" in txt or "NEXT" in txt:
            cx, cy = item["center"]
            if cy > H * 0.6 and cx > W * 0.5:
                return item
    return None


def handle_result_screen(
    state: PilotState,
    analysis_path: Optional[Path],
    ocr: list,
    dist: int,
    mode: str,
) -> Optional[tuple[str, float]]:
    """Result/ガチャ結果画面の統一ハンドラ。

    mode="RAPID": pre-OCR グロー検知即タップ (main loop から呼出)
    mode="OCR":   フル OCR 解析後の判定 (detect_and_act から呼出)

    Returns: (action_name, wait_sec) or None (非Result / 条件不一致)
    """
    W, H = ANALYSIS_W, ANALYSIS_H

    # ── RAPID モード ──
    if mode == "RAPID":
        _rapid_ok = (
            state.last_action in ("RESULT_TAP", "RESULT_NEXT", "RESULT_RAPID",
                                  "GACHA_OK")
            and analysis_path is not None
            and dist <= 30
            and state.result_rapid_count < 8
        )
        if not _rapid_ok:
            return None

        _result_glows = detect_guide_glow(
            analysis_path, W, H, footer_ratio=0.10)
        # 右側グロー (x > 60%) を優先 — ボタンは画面右側に集中
        _right_glows = [g for g in _result_glows
                        if g["cx"] > W * 0.60]
        if _right_glows:
            _rg = max(_right_glows, key=lambda g: g["area"])
            _rgx, _rgy = _rg["cx"], _rg["cy"]
            logger.info("[RESULT_RAPID] right glow(%d,%d) → 即タップ",
                        _rgx, _rgy)
            tap_device(_rgx, _rgy, state, "RESULT_RAPID")
        else:
            # 右側グローなし → 「次へ」想定位置 (ROI補正付き)
            _rc_x, _rc_y = roi_to_device(
                int(W * _RESULT_NEXT_X_RATIO),
                int(H * _RESULT_NEXT_Y_RATIO), state.game_roi)
            logger.info("[RESULT_RAPID] no right glow → 次へ想定位置 (%d,%d)",
                        _rc_x, _rc_y)
            tap_device(_rc_x, _rc_y, state, "RESULT_RAPID")

        state.result_rapid_count += 1
        state.result_total_taps += 1

        # 累積 30 タップで Unity 入力フリーズ復旧
        if state.result_total_taps >= 30:
            logger.warning(
                "[RESULT_FREEZE] RESULT_RAPID %d回 — Unity入力フリーズ → force-stop",
                state.result_total_taps)
            state.result_total_taps = 0
            state.result_rapid_count = 0
            watchdog_recover(state)
            return "RESULT_FREEZE", 0.0

        return "RESULT_RAPID", 1.0

    # ── OCR モード ──
    texts = [r.get("text", "") for r in ocr]
    is_result, subtype = _is_result_screen(ocr, texts)
    if not is_result:
        return None

    state.result_subtype = subtype
    btn = _find_next_button(ocr, W, H, subtype)

    if subtype == "GACHA":
        logger.info("  ガチャ結果画面検出 (subtype=%s) → ハンドラ処理", subtype)
        if btn:
            cx, cy = btn["center"]
            logger.info(">>> 【ガチャ結果】 OK (%d,%d) → ダブルタップ", cx, cy)
            tap_device(cx, cy, state, "GACHA_RESULT_OK_1", rapid=True)
            tap_device(cx, cy, state, "GACHA_RESULT_OK_2")
        else:
            _gc_x, _gc_y = roi_to_device(
                int(W * 0.5), int(H * 0.5), state.game_roi)
            logger.info(">>> 【ガチャ結果初期】 OK未検出 → 画面中央ダブルタップ (%d,%d)",
                        _gc_x, _gc_y)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_1",
                       rapid=True)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_2")
        state.result_total_taps += 1
        return "GACHA_OK", 1.0

    # BATTLE subtype
    if btn:
        _nx, _ny = btn["center"]
        logger.info(">>> 【バトルResult】 次へ (%d,%d) タップ", _nx, _ny)
        tap_device(_nx, _ny, state, "RESULT_NEXT")
    else:
        _nx, _ny = roi_to_device(
            int(W * _RESULT_NEXT_X_RATIO),
            int(H * _RESULT_NEXT_Y_RATIO), state.game_roi)
        logger.info(">>> 【バトルResult】 次へ未検出 → 想定位置 (%d,%d) タップ",
                    _nx, _ny)
        tap_device(_nx, _ny, state, "RESULT_NEXT")
    state.result_total_taps += 1
    return "RESULT_TAP", 1.0


# ─── ダイアログ検出ハンドラ (#0-DIALOG) ──────────────────────────

def handle_dialog_screen(
    state: PilotState,
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
        state.dialog_close_total = 0
        return "NOTICE_POPUP_CLOSE", 1.0

    # ── 指ガード: ×のみダイアログは指がある場合スキップ ──
    # ページングダイアログ (▷) は SPATIAL_GATE に委任して指との距離で判断
    # scene_reeval_mode 中はガード緩和 (誤認識からの脱出のため)
    if has_finger_guard and _dlg_type == "close" and not state.scene_reeval_mode:
        logger.debug("[DIALOG_FINGER_GUARD] 指ブロブ + ×のみ → スキップ")
        return None

    # ── [SPATIAL GATE] ▷ページングより指アイコンを最優先 ──────────────
    if _dlg_type in ("next", "bottom"):
        _sg_blobs = find_finger_blobs(analysis_path, min_area=400)
        _sg_blobs = [b for b in _sg_blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        if _sg_blobs:
            _sg_best = max(_sg_blobs, key=lambda b: b[2])
            _sg_dist = ((_dlg_x - _sg_best[0]) ** 2 + (_dlg_y - _sg_best[1]) ** 2) ** 0.5
            if _sg_dist > 300:
                logger.info(
                    ">>> [SPATIAL_GATE] 指(%d,%d)↔▷(%d,%d) 距離=%.0fpx>300 → #0-DIALOG スキップ",
                    _sg_best[0], _sg_best[1], _dlg_x, _dlg_y, _sg_dist,
                )
                _dlg = None
        # ── 白ハンドポインタ画面ガード ──────────────────────────
        if _dlg is not None:
            _sg_white = detect_white_hand_pointer(analysis_path, threshold=0.90)
            if _sg_white is not None:
                logger.info(
                    "[SPATIAL_GATE] 白ハンドポインタ(%d,%d) score=%.3f → #0-DIALOG(▷) スキップ",
                    _sg_white[0], _sg_white[1], _sg_white[2],
                )
                _dlg = None
            elif any(any(k in t for k in ("NEW", "報酬", "推奖", "报酬")) for t in texts):
                logger.info("[SPATIAL_GATE] クエストKW検出(補助) → #0-DIALOG(▷) スキップ")
                _dlg = None

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
    state.dialog_close_total += 1
    state.dialog_detections += 1

    # ── エスカレーション: 12回以上 → ダイアログ検出自体をスキップ ──
    if state.dialog_close_total >= 12:
        logger.warning(
            ">>> 【ダイアログ#0-DIALOG】累計%d回スタック → ダイアログ検出スキップ (他処理へ)",
            state.dialog_close_total,
        )
        state.dialog_close_total = 0
        state.pre_popup_tap_count = 0
        return None

    # ── エスカレーション: 8回以上 → Android BACK キー ──
    if state.dialog_close_total >= 8:
        logger.warning(
            ">>> 【ダイアログ#0-DIALOG】累計%d回失敗 → BACK キー押下",
            state.dialog_close_total,
        )
        try:
            adb("shell input keyevent KEYCODE_BACK")
        except Exception as _e:
            logger.debug("[DIALOG] BACK キー送信例外: %s", _e)
        state.pre_popup_tap_count = 0
        return "DIALOG_BACK_ESCALATION", 2.0

    if _dlg_type in ("next", "bottom"):
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
            ">>> 【ダイアログ#0-DIALOG】%s(%d,%d) (試行%d回/累計%d)",
            _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count, state.dialog_close_total,
        )
        tap_device(_dlg_x, _dlg_y, state, "DIALOG_CLOSE")
        return "DIALOG_CLOSE", 1.0



# ─── 代替タップ候補収集 ──────────────────────────────
_MAX_CANDIDATES = 5


def collect_secondary_candidates(
    ocr: list, state: PilotState,
    analysis_path: Optional[Path], primary_action: str,
) -> list[TapCandidate]:
    """
    detect_and_act() の主候補が空振りした際に試す代替タップ候補を収集する。
    同じ OCR/画像データを使い回すため追加の OCR コストは発生しない。

    主候補のカテゴリと重複する候補は除外する。
    最大 _MAX_CANDIDATES 個、priority 昇順でソートして返す。
    """
    candidates: list[TapCandidate] = []
    W, H = ANALYSIS_W, ANALYSIS_H
    texts = all_texts(ocr)

    # ── 1. ダイアログ ×/▷ (priority=10) ──
    if not primary_action.startswith("DIALOG") and analysis_path is not None:
        dlg_nav = detect_dialog_frame_and_nav(
            analysis_path, W, H, ocr_texts=texts, roi=state.game_roi)
        if dlg_nav:
            _nav_type, _nx, _ny = dlg_nav
            candidates.append(TapCandidate(
                x=_nx, y=_ny, action=f"CAND_DIALOG_{_nav_type.upper()}",
                priority=10, desc=f"dialog {_nav_type}"))

    # ── 2. 金枠ボタン (priority=20) ──
    if primary_action != "GOLD_BTN_TAP" and analysis_path is not None:
        gold_btn = detect_tutorial_gold_button_tap(
            analysis_path, right_half_only=False)
        if gold_btn:
            candidates.append(TapCandidate(
                x=gold_btn[0], y=gold_btn[1], action="CAND_GOLD_BTN",
                priority=20, desc="gold button"))

    # ── 3. OCR 確認ボタン (OK/はい/次へ) (priority=30) ──
    if not primary_action.startswith("CONFIRM") and primary_action != "ADV_CHOICE":
        confirm_btn = has_any(ocr, _CONFIRM_POS_KWS)
        if confirm_btn:
            _cx, _cy = confirm_btn["center"]
            _cy = max(0, _cy - _OCR_BBOX_Y_PADDING)
            candidates.append(TapCandidate(
                x=_cx, y=_cy, action="CAND_CONFIRM_OK",
                priority=30, desc=f"confirm '{confirm_btn['text']}'"))

    # ── 4. 代替指ブロブ (2番目以降) (priority=40) ──
    if primary_action == "MOYA_TAP" and analysis_path is not None:
        blobs = find_finger_blobs(analysis_path, min_area=300, max_area=15000)
        blobs = [b for b in blobs
                 if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        # 1番目は主候補で使用済み → 2番目以降を追加
        for _bi, blob in enumerate(blobs[1:_MAX_CANDIDATES], start=2):
            _tip_y = blob[4] + int(blob[6] * _FINGER_TIP_RATIO)
            _tip_x = blob[3] + blob[5] // 2
            candidates.append(TapCandidate(
                x=_tip_x, y=_tip_y, action=f"CAND_FINGER_{_bi}",
                priority=40, desc=f"finger blob #{_bi}"))

    # ── 5. 閉じるボタン OCR (閉じる/Close) (priority=50) ──
    if not primary_action.startswith("CLOSE"):
        close_btn = has_any(ocr, ["閉じる", "Close", "CLOSE"])
        if close_btn:
            _clx, _cly = close_btn["center"]
            _cly = max(0, _cly - _OCR_BBOX_Y_PADDING)
            candidates.append(TapCandidate(
                x=_clx, y=_cly, action="CAND_CLOSE",
                priority=50, desc=f"close '{close_btn['text']}'"))

    # ── 6. ストーリータップ (下部テキスト) (priority=60) ──
    if primary_action != "STORY_TAP":
        # 下部1/3にテキストがあればセリフ送りとしてタップ
        _bottom_texts = [item for item in ocr
                         if item.get("center", (0, 0))[1] > H * 0.7
                         and len(item.get("text", "")) >= 3]
        if _bottom_texts:
            _st = _bottom_texts[0]
            candidates.append(TapCandidate(
                x=_st["center"][0], y=_st["center"][1],
                action="CAND_STORY_TAP", priority=60,
                desc=f"story '{_st['text'][:8]}'"))

    # ソート & 制限
    candidates.sort(key=lambda c: c.priority)
    return candidates[:_MAX_CANDIDATES]


# ─── 画面判定・アクション ──────────────────────────
def detect_and_act(ocr: list, state: PilotState,
                   analysis_path: Optional[Path] = None) -> tuple[str, float]:
    """
    OCR + 指差しブロブを分析し、アクションを決定する。
    analysis_path が渡された場合は finger blob 検出も実行。

    Returns: (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    W, H = ANALYSIS_W, ANALYSIS_H
    # ADVシーン検出結果 (detect_adv_scene_cached で事前計算済み)
    _adv_result: AdvSceneResult = state._adv_scene_cache_result or AdvSceneResult()
    joined = " ".join(texts)

    # ── 【#-3】ダウンロード画面の厳格判定 ──
    # 条件: 右下エリアに "Download" テキスト + "MB" 進捗テキストが両方存在
    # → これ以外の画面は 100% ゲーム実行中であり、ロード待ちを禁止する。
    # 通信速度やネットワーク状態による推測は一切行わない。
    _has_download_text = any("Download" in t or "ダウンロード" in t for t in texts)
    _has_size_progress = any("MB" in t or "GB" in t for t in texts)
    # 確認/完了ダイアログ除外:
    # - 「ダウンロードを開始しますか?」等の質問 or OK+キャンセル共存
    # - 「ダウンロード完了」等の完了通知 + OK ボタン
    _dl_is_question = any("しますか" in t or "開始" in t for t in texts if "ダウンロード" in t)
    _dl_is_complete = any("完了" in t or "Complete" in t for t in texts)
    _dl_has_ok = any("OK" in t for t in texts)
    _dl_has_cancel = any("キャンセル" in t for t in texts)
    _dl_is_confirm_dialog = _dl_is_question or (_dl_has_ok and _dl_has_cancel) or (_dl_is_complete and _dl_has_ok)
    if _has_download_text and _has_size_progress and not _dl_is_confirm_dialog:
        _dl_texts = [t for t in texts if "Download" in t or "MB" in t or "GB" in t or "ダウンロード" in t]
        logger.info(">>> [DOWNLOAD_STRICT] 右下ゲージ確認: %s — ダウンロード待機", _dl_texts)
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ── 【#-2.9】確認ダイアログ — 肯定ボタン最優先 ──
    # (A) OK/はい + キャンセル/いいえ が共存 → 確認ダイアログ → OK を必ずタップ。
    # (B) 「完了」系テキスト + OK 単独 → 完了通知ダイアログ → OK をタップ。
    # #0-DIALOG の × ボタンが先に発動する問題を根本解決。
    # ダウンロードの次、SKIP より先に評価する。
    _confirm_pos = has_any(ocr, _CONFIRM_POS_KWS)
    _confirm_neg = has_any(ocr, _CONFIRM_NEG_KWS)
    _is_completion_dialog = _confirm_pos and not _confirm_neg and _dl_is_complete
    if (_confirm_pos and _confirm_neg) or _is_completion_dialog:
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

    # ─── 【最優先 #-1】「ご注意」画面 (Google Play 起動時 portrait 注意書き) ───
    # アプリ初回起動時に portrait で表示される法的注意画面。
    # 「同意してゲームを始める」ボタン (右側ゴールドボタン) をOCRで検出してタップ。
    if has_text(ocr, "ご注意", min_conf=0.3) or (
        has_text(ocr, "基本無料", min_conf=0.3) and has_text(ocr, "未成年", min_conf=0.3)
    ):
        # 「同意」ボタンをOCRで検出
        # ご注意画面は非 immersive (ステータスバー表示中) のため、
        # adb input tap の Y 座標がスクリーンキャプチャより 48px 上にズレる。
        # 補正: OCR/フォールバック座標の Y からステータスバー高さを差し引く。
        _STATUS_BAR_Y = _query_status_bar_height()
        agree_btn = (has_text(ocr, "同意してゲーム", min_conf=0.2) or
                     has_text(ocr, "同意して", min_conf=0.2) or
                     has_text(ocr, "ゲームを始める", min_conf=0.2))
        if agree_btn:
            cx, cy = agree_btn["center"]
            logger.info(">>> 【ご注意画面】 同意ボタン検出 OCR(%d,%d) → Y-%d補正",
                        cx, cy, _STATUS_BAR_Y)
        else:
            # フォールバック: 比率ベース (W*0.66, H*0.79) + ROI 補正
            cx, cy = roi_to_device(int(W * 0.66), int(H * 0.79), state.game_roi)
            logger.info(">>> 【ご注意画面】 同意ボタン未検出 → ROI補正フォールバック (%d,%d) → Y-%d補正",
                        cx, cy, _STATUS_BAR_Y)
        cy -= _STATUS_BAR_Y  # 非 immersive ステータスバー補正

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
            logger.info(">>> 【Unity初期化待機】 30秒 Watchdog停止 (NOTICE_DISMISS exempt)")
            return "NOTICE_DISMISS", 30.0
        else:
            logger.info(">>> 【ご注意→リトライ上限(5回)】 次ループで再検出")
            return "NOTICE_DISMISS", 3.0

    # ─── 【最優先 #-1b】MAIN STORY ローディング背景 ───
    # タイトル画面TAP後に表示される非インタラクティブなローディング背景。
    # 「Main」カードと「推奨」テキストが同時に存在する場合は待機のみ（タップ不要）。
    # この画面は自動でホーム画面へ遷移する。
    _is_main_story_bg = (
        any("MAIN" in t or "Main" in t for t in texts) and
        any("推奨" in t or "STORY" in t for t in texts) and
        not any(kw in joined for kw in ["クエスト", "ショップ", "ガチャ", "ガシャ", "光の間", "パーティ"])
    )
    if _is_main_story_bg:
        logger.info(">>> MAIN STORY ローディング背景 — 自動遷移待ち (10s)")
        return "MAIN_STORY_LOADING", 10.0

    # ─── 【最優先 #0-PRE】バトル発光SM ガード ─────────────────────────────────
    # DIALOG_CLOSE が「通常攻撃」等のバトルアクションを踏み越えるのを防ぐ。
    # ① 「メニューが使用できません」トースト → DIALOG 誤検出スキップ (toast 自然消滅)
    # ② P1: 左キャラ発光 (character_selected=False) → GLOW_LEFT_CHAR
    # ③ P2: 右スキル発光 (character_selected=True) → GLOW_RIGHT_SKILL
    # ④ P3: 発光なし + character_selected → 通常攻撃 OCR フォールバック
    _is_battle_early = any(kw in joined for kw in _BATTLE_CORE_KWS)
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

    # ── 【#0-DIALOG 前ガード】指ブロブ検出時はダイアログ検出をスキップ ──────
    # お知らせポップアップ検出時はガードをバイパス (×で確実に閉じるため)
    # ADV/ミニ会話シーン検出時はスキップ (指アイコンは出ない — 背景装飾の誤検出防止)
    _pre_dialog_finger = False
    _is_mini_conv = detect_mini_conversation(analysis_path) is not None if analysis_path else False
    _is_result_screen = any(
        any(k in t for k in ("Result", "リザルト", "次へ"))
        for t in texts
    )
    if analysis_path is not None and not _is_result_screen and not _is_notice and not _adv_result.is_adv and not _is_mini_conv:
        _pdg_blobs = find_finger_blobs(analysis_path, min_area=300, max_area=5000)
        _pdg_blobs = [b for b in _pdg_blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        if _pdg_blobs:
            _pre_dialog_finger = True
            logger.info("[PRE_DIALOG_GUARD] 指ブロブ %d 個検出 → #0-DIALOG スキップ", len(_pdg_blobs))
        if not _pre_dialog_finger:
            _white_hand_pos = detect_white_hand_pointer(analysis_path, threshold=0.90)
            if _white_hand_pos is not None:
                _pre_dialog_finger = True
                logger.info(
                    "[PRE_DIALOG_GUARD] 白ハンドポインタ (%d,%d) score=%.3f → #0-DIALOG スキップ",
                    _white_hand_pos[0], _white_hand_pos[1], _white_hand_pos[2],
                )

    # ─── 【最優先 #0-DIALOG】ダイアログ・ファースト ────────────
    _dialog_result = handle_dialog_screen(
        state, analysis_path, ocr, texts, _is_battle_early, _pre_dialog_finger,
        is_notice_popup=_is_notice)
    if _dialog_result is not None:
        return _dialog_result

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
    _gs_roi_x = state.game_roi[0] if state.game_roi else 0
    _gs_letterbox = _gs_roi_x >= 80  # レターボックス動画中はスワイプ抑制
    if analysis_path is not None and not _is_battle_ui and not _adv_result.is_adv and not _has_dialog_kw and not _is_home_screen and not _gs_letterbox:
        _gold = detect_tutorial_gold_swipe(analysis_path)
        if _gold:
            # 連続スワイプ上限チェック: 閾値超えたら GoldSwipe をスキップして他の処理へ
            if state.gold_swipe.stalled:
                logger.warning(
                    "[GoldSwipe] detect_and_act: 連続 %d 回 → スキップ (別アクション探索)",
                    state.gold_swipe.count,
                )
                state.gold_swipe.reset()
            else:
                _dir, _sx, _fy, _ty, _dur = _gold
                state.gold_swipe.tick()
                _base_ph_gs = compute_phash(analysis_path)
                # 距離が短すぎる場合はフルスクリーンスワイプを強制 (解像度差対策)
                _min_dist = int(ANALYSIS_H * 0.6)
                if abs(_fy - _ty) < _min_dist:
                    if _dir == "UP":
                        _fy = ANALYSIS_H - 50   # 画面下端
                        _ty = 50                 # 画面上端
                    else:
                        _fy = 50
                        _ty = ANALYSIS_H - 50
                for _gs_retry in range(2):
                    if _dir == "UP":
                        logger.info(">>> [GoldSwipe] SWIPE_UP (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc="GoldSwipe_UP")
                    else:
                        logger.info(">>> [GoldSwipe] SWIPE_DOWN (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc="GoldSwipe_DOWN")
                    time.sleep(0.3)
                    _new_ss, _, _, _ = take_screenshot()
                    if _new_ss is None:
                        continue  # 破損スクリーンショット → リトライ
                    _new_ph = compute_phash(_new_ss)
                    if _base_ph_gs and _new_ph and phash_distance(_base_ph_gs, _new_ph) >= PHASH_THRESHOLD:
                        state.gold_swipe.reset()  # 画面変化 → リセット
                        break  # 変化検知 → 成功
                    _base_ph_gs = _new_ph
                    # 座標を少しずらして再試行 (+40px x方向)
                    _sx += 40
                return "GOLD_SWIPE_UP" if _dir == "UP" else "GOLD_SWIPE_DOWN", BATTLE_WAIT

    # ─── 【最優先 #0-ab】HSV金枠ボタン検出 → 中心タップ (Type B) ───
    # バトルチュートリアルで指アイコンが金枠ハイライトボタンを指している場面。
    # OCR が "隣接攻撃" "必殺技" を検出し、かつ右半分に金枠ボタンがある場合に発火。
    _battle_tut_kws = ["隣接攻撃", "必殺技", "巫殺技", "ATTACKER", "通常攻撃"]
    _is_battle_tut_context = any(kw in joined for kw in _battle_tut_kws)
    # バトルUI確認済みの場合はフッター外GoldBtnをスキップ → Glow SM (フッター) に委ねる
    if analysis_path is not None and _is_battle_tut_context and not _is_battle_early:
        _gold_btn = detect_tutorial_gold_button_tap(analysis_path, right_half_only=True)
        if _gold_btn:
            _bx, _by = _gold_btn
            logger.info(">>> [GoldBtn] 金枠ボタン検出 → tap(%d,%d)", _bx, _by)
            _base_ph_gb = compute_phash(analysis_path)
            tap_device(_bx, _by, state, "GOLD_BTN_TAP")
            _new_ss_gb, _, _, _ = take_screenshot()
            try:
                _new_ph_gb = compute_phash(_new_ss_gb)
            except Exception:
                _new_ph_gb = None
            if (not _base_ph_gb or not _new_ph_gb or
                    phash_distance(_base_ph_gb, _new_ph_gb) < PHASH_THRESHOLD):
                # 変化なし → Y方向に+30pxずらして再試行
                logger.info(">>> [GoldBtn] phash変化なし → +%dpx 再タップ (%d,%d)",
                            _GOLD_BTN_RETRY_Y_OFFSET, _bx, _by + _GOLD_BTN_RETRY_Y_OFFSET)
                tap_device(_bx, _by + _GOLD_BTN_RETRY_Y_OFFSET, state, "GOLD_BTN_TAP_RETRY")
            return "GOLD_BTN_TAP", BATTLE_WAIT

    # ─── 【最優先 #0-a】テンプレートマッチング (Asset Match) — 最速 ~0.1s ───
    # チュートリアル中は指アイコン検出(TAP_HIGHLIGHTED_NAV/SWIPE_UP)が最高優先。
    # 指アイコン検出後 → 金色ハイライト要素をタップ。
    # 次優先: セリフ/ADVテキスト確認 (後続の#0/#3-ADV処理)
    if analysis_path is not None:
        asset_hit = ASSET_MANAGER.match(analysis_path, ocr_texts=texts)
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
                # 安全ネット1: ダイアログKWが見えるときはポップアップ上のスワイプ誤発火を防止
                _swipe_skip = any(kw in joined for kw in _DIALOG_FIRST_KWS)
                # 安全ネット2: SWIPE_UP 連続空振り → テンプレート誤検出と判断しスキップ
                if state.gold_swipe.stalled:
                    logger.warning(
                        "[SWIPE_UP] Asset Match 連続 %d 回空振り → スキップ (誤検出)",
                        state.gold_swipe.count)
                    state.gold_swipe.reset()
                    _swipe_skip = True
                if not _swipe_skip:
                    state.gold_swipe.tick()
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
                    logger.info(">>> [SWIPE_UP] ダイアログKW検出 → スキップ (#0-DIALOGへ)  ← safety net")
                    # ここに到達した場合は #0-DIALOG が None を返した異常ケース
                    # 固定座標でダイアログ ▷ をタップして続行
                    _dnf_x, _dnf_y = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
                    tap_device(_dnf_x, _dnf_y, state, "DIALOG_NEXT_FALLBACK")
                    return "DIALOG_NEXT_FALLBACK", 1.0
            # チュートリアル指差し: 金色ハイライトされたUI要素を方向非依存で検出→タップ
            if action == "TAP_HIGHLIGHTED_NAV":
                gold_pos = find_golden_highlighted_button(analysis_path)
                if gold_pos:
                    tap_x, tap_y = gold_pos
                else:
                    # フォールバック: 指アイコン直下160px
                    tap_x, tap_y = smart_tap_button(analysis_path, cx, cy + 160, search_r=160, ocr_items=ocr)
                logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d) → 金色ハイライト(%d,%d)",
                            cx, cy, tap_x, tap_y)
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
    pre_popup = has_any(ocr, list(_DIALOG_FIRST_KWS))
    if pre_popup:
        state.pre_popup_tap_count += 1
        # ── テンプレートマッチングで ▷/× を優先検出 ──
        _nav = detect_tutorial_dialog_nav(analysis_path, W, H) if analysis_path else None
        if _nav:
            _nav_type, cx, cy = _nav
            if _nav_type == "close":
                logger.info(">>> 【チュートリアルポップアップ】 '%s' ×→(%d,%d) [template]",
                            pre_popup["text"][:10], cx, cy)
                tap_device(cx, cy, state, "PRE_POPUP_TAP")
                return "TUTORIAL_POPUP", 1.0
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
        logger.info(">>> 【チュートリアルポップアップ→PAGING(FB)】 '%s' ▷(%d,%d) → 全ページ走査",
                    pre_popup["text"][:10], _arr[0], _arr[1])
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
    close_popup = has_any(ocr, close_popup_kws)
    if close_popup:
        close_x = W - _CLOSE_BTN_OFFSET  # 右上 × ボタン
        close_y = _CLOSE_BTN_OFFSET
        logger.info(">>> 【%s ポップアップ】 → × (%d,%d) タップ", close_popup["text"][:6], close_x, close_y)
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

    # ─── 【最優先 #1-pre】バトル発光 State Machine (フッター下部30%限定) ─────────
    if _is_battle_early and analysis_path is not None:
        _gsm_result = _run_battle_glow_sm(analysis_path, W, H, state, ocr, tag="GLOW_SM")
        if _gsm_result is not None:
            return _gsm_result

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        # 「AUTO」のみはストーリー画面にも表示されるため除外、戦闘固有キーワードで判定
        is_battle_screen = any(kw in joined for kw in _BATTLE_CORE_KWS)
        # ── 速度チュートリアル早期検出 (もや検出より前に処理) ──
        _speed_tip_early = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if _speed_tip_early and is_battle_screen:
            _sp_x, _sp_y = roi_to_device(int(W * 0.927), int(H * 0.026), state.game_roi)
            logger.info(">>> [EARLY] 速度ツールチップ → 速度ボタン (%d,%d) タップ", _sp_x, _sp_y)
            tap_device(_sp_x, _sp_y, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 0.5
        # タイトル画面 / ホーム画面検出: ブロブ誤検出を防ぐ
        _nav_joined = joined
        # 利用規約画面・同意ダイアログが存在する場合はタイトル画面と区別する
        _is_tos_screen = "利用規約" in _nav_joined or "同意してゲームを始める" in _nav_joined
        _title_kws_game = ["魔法", "少女", "まどか", "マギカ", "まどかハ", "MADOKA", "MAGICA"]
        is_title_screen = (
            not state.home_reached and not _is_tos_screen and (
                any(kw in _nav_joined for kw in ["TAP TO START", "Magia Exedra",
                                                  "MAGIA EXEDRA", "TAPTOSTART"]) or
                # 「動画配信設定」「Ver.」はタイトル画面固有の上部 UI
                (any(kw in _nav_joined for kw in ["動画配信", "勤画配信", "Ver.2", "Ver.2."])
                 and any(kw in _nav_joined for kw in _title_kws_game + ["PUELLA"])) or
                ("VID" in _nav_joined and any(kw in _nav_joined for kw in _title_kws_game)) or
                # フォールバック: ゲームタイトルロゴ文字 + Rank がない + ホームナビがない
                # "Rank" "Main" "推奨" は MAIN STORY 選択画面なのでタイトルと区別する
                (any(kw in _nav_joined for kw in ["PUELLA MAGI", "PUELLAHAGI", "PUELLAMAGI",
                                                   "PUELLA MAGIMADOKA"])
                 and any(kw in _nav_joined for kw in _title_kws_game)
                 and not any(kw in _nav_joined for kw in ["クエスト", "ショップ", "ガチャ",
                                                           "Rank", "Main", "推奨"]))
            )
        )
        if is_title_screen:
            logger.info("  タイトル画面検出 → TAP TO START (760,628) タップ")
            _tt_x, _tt_y = roi_to_device(int(W * 0.5), int(H * 0.87), state.game_roi)
            tap_device(_tt_x, _tt_y, state, "TITLE_TAP_START")
            return "TITLE_TAP", 2.0
        # ホーム画面検出: フッターエリア (y > H*0.85) のナビキーワードが2個以上
        # フッター以外 (編成メニュー内の「パーティ」等) は誤検出になるため除外
        _home_nav_kws = ["クエスト", "ショップ", "ガチャ", "ガシャ", "ユニオン",
                         "光の間", "パーティ", "プレイヤーマッチ", "お知らせ",
                         "イベント", "マイページ", "編成", "MAGIA EXEDRA"]
        _footer_y_min = int(H * 0.85)
        _footer_ocr = [item for item in ocr
                       if item.get("center", (0, 0))[1] >= _footer_y_min]
        _footer_texts = [item.get("text", "") for item in _footer_ocr]
        _home_kw_count = sum(1 for h in _home_nav_kws
                             if any(h in t for t in _footer_texts))
        # ── Result画面ハンドラ (OCR mode) ──
        if not is_battle_screen:
            _result_ocr = handle_result_screen(state, analysis_path, ocr, state.last_phash_dist, mode="OCR")
            if _result_ocr:
                return _result_ocr
        # ─── ADV選択肢 — 肯定ボタン絶対優先 ───────────────────────────
        # OK / はい / 了解 を最優先。キャンセル / いいえ は選択禁止。
        _adv_pos = has_any(ocr, _CONFIRM_POS_KWS)
        _adv_neg = has_any(ocr, _CONFIRM_NEG_KWS)
        if _adv_pos:
            _ac_x, _ac_y = _adv_pos["center"]
            # OCR bbox はテキスト下部パディングを含むため Y を上方補正
            # ボタンの上半分を狙い、空振りを防止 (画質設定OK等)
            _ac_y_adj = max(0, _ac_y - _OCR_BBOX_Y_PADDING)
            logger.info(
                "[ADV-Choice] '%s' (%d,%d→Y%d) タップ (否定='%s'無視)",
                _adv_pos["text"], _ac_x, _ac_y, _ac_y_adj,
                _adv_neg["text"] if _adv_neg else "なし",
            )
            tap_device(_ac_x, _ac_y_adj, state, f"ADV_CHOICE '{_adv_pos['text']}'")
            return "ADV_CHOICE", 1.0

        # バトル時は dark_mode=True で輝度閾値を緩和し min_area=200 に下げる（暗背景対応）
        # ホーム画面 / 利用規約ダイアログ / システムダイアログはブロブ誤検出になるためスキップ
        _is_system_dialog = any(kw in _nav_joined for kw in
                                ["画質を設定", "高画質", "省エネ", "省工ネ", "データ引き継ぎ",
                                 "サポート", "お問い合わせ", "キャッシュクリア"])
        if _home_kw_count >= 2:
            logger.info("  ホーム画面検出 (footer nav×%d: %s) → MOYA_TAP スキップ",
                        _home_kw_count, _footer_texts)
            # ── 【最優先】ダンジョン挑戦ボタン: 「挑戦」OCR検出 → 直接タップ (ホーム誤検出突破) ──
            _chal_btn = has_text(ocr, "挑戦", min_conf=0.3)
            if _chal_btn:
                _cb_x, _cb_y = _chal_btn["center"]
                # ROI クランプ: ゲーム領域外 (黒帯) をタップしない
                if state.game_roi:
                    _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
                    _cb_y = min(_cb_y, _roi_max_y)
                # 挑戦ボタンは右下(x>W*0.5, y>H*0.5)にあるはず
                if _cb_x > W * 0.5 and _cb_y > H * 0.5:
                    logger.info("[Challenge] '%s'(%d,%d) → 直接タップ", _chal_btn["text"], _cb_x, _cb_y)
                    tap_device(_cb_x, _cb_y, state, "CHALLENGE_TAP")
                    return "CHALLENGE_TAP", 1.0
            # ── ホームチュートリアル: 指アイコン+金枠がある場合は優先タップ ──
            # ただし 10回超ループしたら LATE path (grind_mode処理) に委譲
            if state.home_tutorial_tap_count < 10:
                _ht_blobs = find_finger_blobs(analysis_path) if analysis_path else []
                _ht_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
                if _ht_blobs or _ht_gold:
                    _ht_target = None
                    if _ht_blobs:
                        _ht_chosen = max(_ht_blobs, key=lambda b: b[2])
                        _ht_bx, _ht_by = _ht_chosen[0], _ht_chosen[1]
                        _ht_gf = find_gold_frame_near(analysis_path, _ht_bx, _ht_by) if analysis_path else None
                        if _ht_gf:
                            _ht_fg_dist = ((_ht_bx - _ht_gf[0]) ** 2 + (_ht_by - _ht_gf[1]) ** 2) ** 0.5
                            if _ht_fg_dist <= 200:
                                _ht_target = (_ht_gf[0], _ht_gf[1])
                                logger.info("  ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) dist=%.0f タップ",
                                            _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                            else:
                                _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                                _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                                logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠(%d,%d) dist=%.0f>200 無視]",
                                            _ht_bx, _ht_by, *_ht_target, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                        else:
                            _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                            _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                            logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) タップ",
                                        _ht_bx, _ht_by, *_ht_target)
                    elif _ht_gold:
                        _ht_target = _ht_gold
                        logger.info("  ホームチュートリアル: 金ボタン(%d,%d) タップ", *_ht_gold)
                    if _ht_target:
                        state.home_tutorial_tap_count += 1
                        tap_device(_ht_target[0], _ht_target[1], state, "HOME_TUTORIAL_TAP")
                        return "HOME_TUTORIAL_TAP", 0.5
            else:
                logger.info("  HOME_TUTORIAL %d回超 → LATE path (grind/ホーム到達) に委譲",
                            state.home_tutorial_tap_count)
            blobs = []
        elif _is_tos_screen or _is_system_dialog:
            logger.info("  システムダイアログ/利用規約検出 → MOYA_TAP スキップ")
            blobs = []
        elif _adv_result.is_adv or _is_mini_conv:
            logger.info("  ADV/ミニ会話シーン検出 → 指ブロブ検出スキップ (背景装飾の誤検出防止)")
            blobs = []
        else:
            state.home_tutorial_tap_count = 0  # ホーム以外 → カウンタリセット
            # バトル中は dark_mode=True + min_area=200 で暗背景の指アイコンも検知
            _blob_dark = is_battle_screen
            blobs = find_finger_blobs(analysis_path,
                                      min_area=200 if _blob_dark else 400,
                                      dark_mode=_blob_dark)
            # 画面端の誤検出を除去: 上端/右端最端はシステムUI
            blobs = [b for b in blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
        if blobs:
            # バトル中は中央エリア(バトルフィールド)の肌色は誤検出なので無視
            # 優先順位: 左キャラカード(x<600,y>550) > 右パネル > 下部UI(y>H*0.8)
            if is_battle_screen:
                # ── チュートリアル金枠+指: 大面積blob(area>10000)は最優先 ──
                _tutorial_gold = [b for b in blobs if b[2] > 10000]
                left_char = [b for b in blobs if b[0] < 600 and b[1] > H * 0.76]
                # right_panel: スキルボタンは下半分(y>H*0.45)のみ。上部の蝶エネミーを排除
                right_panel = [b for b in blobs if b[0] > _RIGHT_PANEL_X and b[1] > H * 0.45]
                bottom_ui = [b for b in blobs if b[1] > H * 0.8 and b[0] >= 600]
                if _tutorial_gold:
                    blobs = _tutorial_gold[:1]
                    logger.info("  バトル: 金枠+指 (%d,%d) area=%.0f → チュートリアル最優先",
                                blobs[0][0], blobs[0][1], blobs[0][2])
                elif state.char_just_selected:
                    # 左キャラ選択済み → 右スキルを選択 (左キャラ再タップしない)
                    if right_panel:
                        blobs = right_panel
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 右スキルもや %d個", len(blobs))
                    elif bottom_ui:
                        blobs = bottom_ui
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 下部UIもや %d個", len(blobs))
                    else:
                        # 右スキルがまだ表示されていない → 少し待つ
                        logger.info("  バトル: キャラ選択後 → 右スキル待ち")
                        blobs = []
                elif left_char:
                    # フリーバトル: 左キャラ選択が最優先
                    blobs = left_char
                    logger.info("  バトル: 左キャラもや %d個 (最優先)", len(blobs))
                elif right_panel:
                    blobs = right_panel
                    logger.info("  バトル: 右パネルもや %d個", len(blobs))
                elif bottom_ui:
                    blobs = bottom_ui
                    logger.info("  バトル: 下部UIもや %d個", len(blobs))
                else:
                    logger.info("  バトル中: 有効もやなし(中央は誤検出) → OCR判定へ")
                    blobs = []

        if blobs:
            # ── Hard Masking 2.0: バトル中は先頭blobの350×350以外を黒塗り ──
            # 右側スキルボタンの金枠を物理的に排除し、指アイコンの示すターゲットだけを照準
            _hm_analysis = analysis_path
            if is_battle_screen and analysis_path is not None:
                _hm_cx, _hm_cy = blobs[0][0], blobs[0][1]
                _hm_analysis = create_finger_mask_image(analysis_path, _hm_cx, _hm_cy, half=175)
                if _hm_analysis != analysis_path:
                    logger.info("[HARD_MASK] 指(%d,%d)周囲350×350px以外 黒塗り → 認識対象限定",
                                _hm_cx, _hm_cy)

            # ── 2段階ターゲット選択 ──────────────────────────────
            # Step1: 金枠が見つかる blobs を優先（真のGUIガイド要素）
            # Step2: 金枠なし blobs は右側優先フォールバック
            _blob_with_gold = None
            _blob_gold_frame = None
            _blob_fallback = None
            for _b in blobs:
                _bx0, _by0 = _b[0], _b[1]
                # Hard Masking 2.0 適用: バトル時はマスク済み画像で金枠検索
                _gf = find_gold_frame_near(_hm_analysis, _bx0, _by0, search_radius=150)
                if _gf is not None:
                    # 金枠が見つかった最初のblobを使用
                    if _blob_with_gold is None:
                        _blob_with_gold = _b
                        _blob_gold_frame = _gf
                if _blob_fallback is None and _b[0] > _RIGHT_PANEL_X:
                    _blob_fallback = _b  # 右側優先フォールバック
            if _blob_fallback is None and blobs:
                _blob_fallback = blobs[0]

            if _blob_with_gold is not None:
                chosen = _blob_with_gold
                _gold_frame = _blob_gold_frame
                logger.info("  (金枠ありblob優先: %d個中1個)", len(blobs))
            else:
                chosen = _blob_fallback
                _gold_frame = None
                if len(blobs) > 1:
                    logger.info("  (金枠なし → 右パネル優先: %d個中1個を選択)", len(blobs))
            # ────────────────────────────────────────────────────
            fx, fy, fa = chosen[0], chosen[1], chosen[2]
            f_bx, f_by, f_bw, f_bh = chosen[3], chosen[4], chosen[5], chosen[6]
            # 50px 近接判定: アニメーション中のブロブ (±20px移動) でもカウントが継続する
            if state.last_blob_xy == (0, 0):
                # 初回検出: 基準座標を設定してカウントを0にリセット
                state.last_blob_xy = (fx, fy)
                state.blob_same_count = 0
            elif abs(fx - state.last_blob_xy[0]) <= 50 and abs(fy - state.last_blob_xy[1]) <= 50:
                state.blob_same_count += 1
                state.last_blob_xy = (fx, fy)  # 追跡: 次回比較基準を更新
            else:
                state.blob_same_count = 0
                state.last_blob_xy = (fx, fy)
            if state.blob_same_count >= 5:
                _stg = state.blob_same_count
                logger.info(">>> [RECOVERY] スタック stage=%d (%d,%d)", _stg, fx, fy)
                # 移動シーン(OCR無し) + 10回以上 → SWIPE_UP 強制 (最優先)
                if _stg >= 10 and len(texts) == 0:
                    logger.info(">>> [SWIPE_FALLBACK] フィンガースタック%d回+OCR無し → SWIPE_UP強制", _stg)
                    swipe_device(fx, H - 50, fx, 50, 10000, state=state, desc="SWIPE_FALLBACK")
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "SWIPE_FALLBACK", 1.5
                # Stage 1-3 (count=5,6,7): ジッター±10px タップ
                if _stg <= 7:
                    _jx = max(50, min(W - 50, fx + random.randint(-10, 10)))
                    _jy = max(50, min(H - 50, fy + random.randint(-10, 10)))
                    logger.info(">>> [RECOVERY s%d] ジッタータップ (%d,%d)", _stg - 4, _jx, _jy)
                    tap_device(_jx, _jy, state, f"RECOVERY_JITTER_s{_stg - 4}")
                    if _stg == 7:
                        time.sleep(0.5)
                    return "RECOVERY_JITTER", 0.5
                # Stage 4-6 (count=8,9,10): キャッシュ破棄・広域金枠再スキャン
                elif _stg <= 10:
                    _rf_gf = find_gold_frame_near(analysis_path, fx, fy, search_radius=300) if analysis_path else None
                    if _rf_gf:
                        _rf_tx, _rf_ty = _rf_gf[0], _rf_gf[1]
                        logger.info(">>> [RECOVERY s%d] 広域金枠(%d,%d) タップ", _stg - 7, _rf_tx, _rf_ty)
                        tap_device(_rf_tx, _rf_ty, state, f"RECOVERY_RESCAN_s{_stg - 7}")
                        state.blob_same_count = 0
                        state.last_blob_xy = (0, 0)
                        return "RECOVERY_RESCAN", 1.0
                    logger.info(">>> [RECOVERY s%d] 広域金枠なし → OCRフォールバックへ", _stg - 7)
                    # OCR ベース処理に落ちる (fall through)
                # Stage 7-9 (count=11,12,13): ランダムブラインドタップ
                elif _stg <= 13:
                    _bx = max(50, min(W - 50, fx + random.randint(-80, 80)))
                    _by = max(50, min(H - 50, fy + random.randint(-60, 60)))
                    logger.info(">>> [RECOVERY s%d] ブラインドタップ (%d,%d)", _stg - 10, _bx, _by)
                    tap_device(_bx, _by, state, f"RECOVERY_BLIND_s{_stg - 10}")
                    return "RECOVERY_BLIND", 0.8
                # Stage 10 (count>=14): 5秒待機 + リセット
                else:
                    logger.info(">>> [RECOVERY s10] 2秒待機 + カウンタリセット")
                    time.sleep(2.0)
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "RECOVERY_FINAL_WAIT", 0.5
            else:
                # ── Step3-pre: 指タップ静止検出 → スワイプシーン自動切替 ──
                # 3回以上 MOYA_TAP しても画面が変わらない + OCR テキスト少ない
                # → タップでは進まないスワイプシーン (チェック柄チュートリアル等)
                # len<=1: OCR誤検出 ('1','口' 等) 1件までは許容
                if (state.finger_tap_static.stalled
                        and _gold_frame is None
                        and len(texts) <= 1):
                    logger.info(
                        ">>> [SWIPE_AUTO] 指タップ静止%d回+OCR無し → 連続スワイプ開始 (指位置: %d,%d)",
                        state.finger_tap_static.count, fx, fy,
                    )
                    _base_ph_sw = compute_phash(analysis_path)
                    _sw_success = False
                    # 動画シーンに完全遷移するまでスワイプ継続
                    # (チェック柄中の微変化では止まらない: 閾値20)
                    _SW_CHANGE_THRESHOLD = 20
                    for _sw_i in range(30):  # 最大30回 (約90秒)
                        swipe_device(fx, H - 50, fx, 50, 10000, state=state, desc="SWIPE_AUTO")
                        time.sleep(0.2)
                        _sw_ss, _, _, _ = take_screenshot()
                        _sw_ph = compute_phash(_sw_ss)
                        if (_base_ph_sw and _sw_ph
                                and phash_distance(_base_ph_sw, _sw_ph) >= _SW_CHANGE_THRESHOLD):
                            logger.info(
                                ">>> [SWIPE_AUTO] %d回目で大きな画面変化検出 (dist=%d) → スワイプ完了",
                                _sw_i + 1, phash_distance(_base_ph_sw, _sw_ph),
                            )
                            _sw_success = True
                            break
                        # 基準phashを更新しない — 初回画面との比較を維持
                    if not _sw_success:
                        logger.warning(">>> [SWIPE_AUTO] 20回スワイプしても変化なし → タップに戻る")
                    state.finger_tap_static.reset()
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "SWIPE_AUTO", 1.5

                # ── Step3: タップ座標決定 (共通ルール) ──
                # 指アイコンは「タップすべき金枠」を指し示すガイド。
                # (A) 指の近く (200px以内) に金枠 → 金枠中心をタップ
                # (B) 金枠なし → 指先端をタップ
                if _gold_frame is not None:
                    gfx, gfy, gfw, gfh = _gold_frame
                    _gbox = (gfx - gfw // 2, gfy - gfh // 2, gfw, gfh)
                    # 指と金枠の距離チェック: 近距離なら金枠中心を採用
                    _fg_dist = ((fx - gfx) ** 2 + (fy - gfy) ** 2) ** 0.5
                    if _fg_dist <= 200:
                        tap_x = gfx
                        tap_y = gfy
                        logger.info("FINGER→GOLD_FRAME (%d,%d) → gold_center(%d,%d) dist=%.0f count=%d",
                                    fx, fy, tap_x, tap_y, _fg_dist, state.blob_same_count)
                    else:
                        # 遠い金枠は無関係 → 指先端
                        tap_x = fx
                        tap_y = f_by + max(1, int(f_bh * _FINGER_TIP_RATIO))
                        _gbox = None
                        logger.info("FINGER_DETECTED (%d,%d) → tip(%d,%d) [gold(%d,%d) dist=%.0f>200 無視] count=%d",
                                    fx, fy, tap_x, tap_y, gfx, gfy, _fg_dist, state.blob_same_count)
                else:
                    _gbox = None
                    tap_x = fx
                    tap_y = f_by + max(1, int(f_bh * _FINGER_TIP_RATIO))
                    logger.info("FINGER_DETECTED (%d,%d) area=%.0f → tip(%d,%d) count=%d",
                                fx, fy, fa, tap_x, tap_y, state.blob_same_count)
                tap_device(tap_x, tap_y, state, f"MOYA_TAP ({tap_x},{tap_y})",
                           finger_box=(f_bx, f_by, f_bw, f_bh),
                           gold_box=_gbox)
                state.finger_detections += 1
                if _gold_frame is not None:
                    state.gold_detections += 1
                # 左キャラ選択後は char_just_selected / character_selected フラグをセット
                if fx < 600 and fy > H * 0.76:
                    state.char_just_selected = True
                    state.character_selected = True  # GLOW SM 用にも同期
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 1.0

    # ─── 【最優先 #2-a】探索マップ 3D矢印タップ ───
    # 「矢印をタップしてください」が出ている場合、3D空間の矢印を検出してタップ
    arrow_instruction = has_text(ocr, "矢印をタップ", min_conf=0.2)
    if arrow_instruction and analysis_path is not None:
        pos = find_3d_arrow(analysis_path)
        if pos:
            cx, cy = pos
            logger.info(">>> 【3D矢印】 探索マップ矢印 (%d,%d) 検出 → タップ", cx, cy)
            tap_device(cx, cy, state, "MAP_ARROW_TAP")
            # [Auto Save] 初回検出時にテンプレートとして保存
            if "map_arrow" not in ASSET_MANAGER._templates:
                half_w, half_h = 70, 50
                ASSET_MANAGER.save_template(
                    analysis_path,
                    max(0, cx - half_w), max(0, cy - half_h),
                    min(W, cx + half_w), min(H, cy + half_h),
                    name="map_arrow", action="MAP_ARROW_TAP",
                    threshold=0.65,
                    require_ocr=["矢印をタップ"],
                )
            return "MAP_ARROW_TAP", 1.0
        else:
            # 自動検出失敗 → キャラ頭上デフォルト座標
            _ma_x, _ma_y = roi_to_device(int(W * 0.5), int(H * 0.29), state.game_roi)
            logger.info(">>> 【3D矢印】 自動検出失敗 → デフォルト (%d,%d) タップ", _ma_x, _ma_y)
            tap_device(_ma_x, _ma_y, state, "MAP_ARROW_FALLBACK")
            return "MAP_ARROW_TAP", 1.0

    # ─── 【最優先 #2】ハイライト指示テキスト ───
    tutorial_kws = ["ここをタップ", "タップしてください", "タップして下さい", "タップして"]
    for kw in tutorial_kws:
        match = has_text(ocr, kw, min_conf=0.3)
        if match:
            cx, cy = match["center"]
            logger.info(">>> 【ハイライト指示】 '%s' (%d,%d)", kw, cx, cy)
            tap_device(cx, cy, state, f"HIGHLIGHT '{kw}'")
            return "HIGHLIGHT_TAP", 0.5

    # ─── ストーリーセリフ進行 (バトル外でセリフが出ている) ───
    # 「画面をタップ」系の指示 or バトルでもホームでもない日本語テキストが複数ある
    is_battle_now = any(kw in joined for kw in _BATTLE_CORE_KWS)
    tap_screen_kws = ["画面をタップ", "タップして進む", "タップで進む", "タップしてください",
                      "タップして次へ", "TOUCH TO CONTINUE"]
    tap_screen = has_any(ocr, tap_screen_kws)
    if tap_screen and not is_battle_now:
        cx, cy = tap_screen["center"]
        logger.info(">>> 【画面タップ指示】 '%s' (%d,%d)", tap_screen["text"], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP_HINT")
        return "STORY_TAP", 0.3

    # ─── ホーム画面検出 ───
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成"]
    home_count = sum(1 for h in home_indicators if any(h in t for t in texts))
    if home_count >= 3:
        state.home_reached = True
        # ── 指アイコン or 金枠がある場合 → まだホームチュートリアル中 ──
        # 「ホーム画面かつ指アイコン+金枠がない」状態が本当のチュートリアル終了
        # 安全弁: HOME_TUTORIAL_TAP 5回超 → 偽検出と判断しチュートリアル完了扱い
        _tutorial_tap_limit = state.home_tutorial_tap_count >= 5
        if _tutorial_tap_limit:
            logger.info(">>> HOME_TUTORIAL_TAP %d回到達 → 偽検出と判断、チュートリアル完了扱い",
                        state.home_tutorial_tap_count)
        _home_blobs = [] if _tutorial_tap_limit else (
            find_finger_blobs(analysis_path) if analysis_path else [])
        # ホーム画面ではナビバーが中央付近に来るため right_half_only=False で全域検索
        _home_gold = None if _tutorial_tap_limit else (
            detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None)
        if _home_blobs or _home_gold:
            # 指アイコン+金枠が存在 → ガイドに従いタップして続行
            _tap_target = None
            if _home_blobs:
                _chosen_blob = max(_home_blobs, key=lambda b: b[2])  # area最大
                _bx, _by = _chosen_blob[0], _chosen_blob[1]
                # 金枠があれば金枠中心を優先
                _gf = find_gold_frame_near(analysis_path, _bx, _by) if analysis_path else None
                if _gf:
                    _tap_target = (_gf[0], _gf[1])  # (frame_cx, frame_cy)
                else:
                    # blob tuple: (cx, cy, area, bx, by, bw, bh) → 指先は bx+bw/2, by+bh*0.1
                    _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                    _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
            elif _home_gold:
                _tap_target = _home_gold
            if _tap_target:
                state.home_tutorial_tap_count += 1
                logger.info(">>> ホームチュートリアル継続: 指/金枠 → (%d,%d) タップ [%d回目]",
                            _tap_target[0], _tap_target[1], state.home_tutorial_tap_count)
                tap_device(_tap_target[0], _tap_target[1], state, "HOME_TUTORIAL_TAP")
                return "HOME_TUTORIAL_TAP", 0.5
            # blob/gold検出あるがタップ対象なし → blob_same_count 処理へ
            if state.blob_same_count >= 5:
                logger.info(">>> ホーム画面 + もやスタック → クエストへナビゲート")
                state.blob_same_count = 0
                state.home_nav_count += 1
                quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
                if quest_btn:
                    cx, cy = quest_btn["center"]
                    logger.info(">>> クエストボタン (%d,%d) タップ", cx, cy)
                    tap_device(cx, cy, state, "QUEST_FROM_HOME")
                    return "QUEST_FROM_HOME", 3.0
                _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
                tap_device(_qf_x, _qf_y, state, "QUEST_FIXED")
                return "QUEST_FROM_HOME", 3.0
            if state.home_nav_count > 0:
                logger.info(">>> ホーム画面 + 遷移試行 %d回目 → 画面変化待ち", state.home_nav_count)
                return "HOME_NAV_WAIT", 2.0
        # ── 右上吹き出しセリフチェック: まだチュートリアルガイダンス中 ──
        _bubble_texts = [r for r in ocr
                         if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                         and r["text"] not in ("AUTO", ">>", ">|", "D1", "×")]
        if _bubble_texts:
            _bt = _bubble_texts[0]
            _btx, _bty = _bt["center"]
            logger.info(">>> ホーム画面 + 吹き出しセリフ '%s' → チュートリアル継続 (%d,%d)",
                        _bt["text"][:10], _btx, _bty)
            tap_device(_btx, _bty, state, "BUBBLE_TAP")
            return "BUBBLE_TAP", 0.3
        # ── 指アイコンも金枠もない → チュートリアル完了判定 ──
        # nav カウンタをリセット (チュートリアル指標消失)
        state.home_nav_count = 0
        state.blob_same_count = 0
        if state.grind_mode:
            # ── 周回モード: ホーム到達 → クエストへ自動ナビゲート ──
            state.grind_cycles_completed += 1
            logger.info("=" * 50)
            logger.info("  [GRIND] 周回 #%d 完了! → クエストへ自動ナビゲート",
                        state.grind_cycles_completed)
            logger.info("=" * 50)
            # 周回上限チェック
            if 0 < state.grind_max_cycles <= state.grind_cycles_completed:
                logger.info("[GRIND] 目標周回数 %d に到達 → 終了",
                            state.grind_max_cycles)
                return "GRIND_COMPLETE", 0
            # バトル関連カウンタをリセット
            state.battle_wait_count = 0
            state.auto_activated = False
            state.result_rapid_count = 0
            state.result_total_taps = 0
            state.result_subtype = ""
            state.home_nav_count = 0
            state.home_tutorial_tap_count = 0
            state.char_just_selected = False
            state.character_selected = False
            # クエストボタンをタップ
            quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
            if quest_btn:
                cx, cy = quest_btn["center"]
                logger.info(">>> [GRIND] クエストボタン (%d,%d) タップ", cx, cy)
                tap_device(cx, cy, state, "GRIND_QUEST_NAV")
            else:
                _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
                logger.info(">>> [GRIND] クエスト固定位置 (%d,%d) タップ", _qf_x, _qf_y)
                tap_device(_qf_x, _qf_y, state, "GRIND_QUEST_NAV_FIXED")
            return "GRIND_QUEST_NAV", 3.0
        logger.info(">>> ホーム画面検出! (%d個) 指/金枠なし → チュートリアル完了!", home_count)
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 (セカンダリチェック) ───
    # ※ メインの厳格判定は関数冒頭の【絶対最優先 #-3】で実施済み。
    # ここではフォールバックとして「ダウンロード」(日本語) + 進捗テキスト の組み合わせのみ検出。
    # 通信速度やネットワーク状態による推測は一切行わない。
    _dl_jp = has_any(ocr, ["ダウンロード", "追加データ"])
    _dl_progress = any("MB" in t or "GB" in t for t in texts)
    if _dl_jp and _dl_progress:
        logger.info(">>> [DOWNLOAD_STRICT_JP] %s + 進捗あり — 待機", _dl_jp["text"])
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ─── クエストマップ/ステージ選択 ───
    stage_num = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                               "3-1", "3-2", "4-1", "4-2", "Main"])
    sentu_btn = None
    for _skw in ["戦闘", "出撃", "挑戦"]:
        _sb = has_text(ocr, _skw)
        if _sb and _sb["center"][1] > H * 0.5:
            sentu_btn = _sb
            break
    # 「挑戦」がヘッダー位置(y<H*0.5)でのみ検出された場合:
    # 装飾フォントのボタンテキストをOCRが読めていない → 固定位置タップ
    if not sentu_btn and has_text(ocr, "挑戦") and stage_num:
        _chal_fx, _chal_fy = roi_to_device(int(W * 0.82), int(H * 0.91), state.game_roi)
        logger.info(">>> クエストマップ — 「挑戦」ボタン固定位置 (%d,%d)", _chal_fx, _chal_fy)
        tap_device(_chal_fx, _chal_fy, state, "QUEST_START_CHALLENGE_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0
    if not sentu_btn:
        expl = has_text(ocr, "探索")
        if expl and expl["center"][1] > H * 0.6:
            sentu_btn = expl
    if stage_num and sentu_btn:
        cx, cy = sentu_btn["center"]
        # ROI クランプ: ゲーム領域外 (黒帯) をタップしない
        if state.game_roi:
            _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
            cy = min(cy, _roi_max_y)
        logger.info("[QuestStart] '%s'(%d,%d) タップ", sentu_btn["text"], cx, cy)
        tap_device(cx, cy, state, f"QUEST_START {sentu_btn['text']}")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0
    elif stage_num:
        fx, fy = roi_to_device(int(W * 0.74), int(H * 0.91), state.game_roi)
        logger.info(">>> クエストマップ(固定) (%d,%d)", fx, fy)
        tap_device(fx, fy, state, "QUEST_START_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0

    # ─── バトル画面 ───
    # 「AUTO」「HP」「戦闘」はストーリー画面にも出るため除外、戦闘固有キーワードで判定
    battle_keywords = ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "隣接攻撃", "必殺技", "巫殺技",  # チュートリアルバトルキーワード追加
                       "BREAK", "Turn", "WAVE"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1

        # バトルチュートリアル: バフ効果
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx, by = roi_to_device(int(W * 0.888), int(H * 0.667), state.game_roi)
            logger.info(">>> バフチュートリアル (%d,%d)", bx, by)
            tap_device(bx, by, state, "BUFF_TUTORIAL")
            return "BATTLE_TUTORIAL", 0.5

        # バトルチュートリアル: スキル使用
        skill_tut = has_any(ocr, ["スキルを使ってみましょう", "スキを使ってみ",
                                   "戦闘スキルを使", "戦闘スキを使",
                                   "スキルを使用してみ", "使ってみましょう"])
        if skill_tut:
            sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
            logger.info(">>> スキルチュートリアル (%d,%d)", sx, sy)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL", rapid=True)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.0

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL", rapid=True)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.0

        # バトルチュートリアル: 攻撃対象変更
        if has_any(ocr, ["攻撃対象を変更", "対象を変更"]):
            ex, ey = roi_to_device(int(W * 0.651), int(H * 0.361), state.game_roi)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 0.5

        # バトルチュートリアル: 一般ポップアップ
        tutorial_popup = has_any(ocr, [
            "タイムライン", "表示されている", "行動してい",
            "ここをタップ", "タップしてください",
            "ことができます", "することができ",
            "スキルを使用", "スキルを選択", "カードを選択",
            "ましょう", "みましょう", "てみよう",
            "一番上に", "順番に行動", "DEFENDER",
            # ロール説明ポップアップ
            "ロールについて", "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "HEALER",
            # バトル説明ポップアップ
            "STEP1", "STEP2", "バトルシステム", "ブレイクし",
        ])
        # バトル速度ツールチップは速度ボタン本体 (1409,19) をタップして消す
        speed_tip = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if speed_tip:
            _sp_x, _sp_y = roi_to_device(int(W * 0.927), int(H * 0.026), state.game_roi)
            logger.info(">>> 速度ツールチップ → 速度ボタン (%d,%d) タップ", _sp_x, _sp_y)
            tap_device(_sp_x, _sp_y, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 0.5
        if tutorial_popup:
            # ── テンプレートマッチングで ▷/× を優先検出 ──
            _btl_nav = detect_tutorial_dialog_nav(analysis_path, W, H) if analysis_path else None
            if _btl_nav:
                _btn, _bx, _by = _btl_nav
                logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) [template]",
                            tutorial_popup["text"][:10], "×" if _btn == "close" else "▷", _bx, _by)
                tap_device(_bx, _by, state, "BATTLE_TUTORIAL_POPUP")
                return "BATTLE_TUTORIAL", 0.5
            # フォールバック: ▷ 矢印 → × ボタンのシーケンス
            state.pre_popup_tap_count += 1
            _arr_b = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
            _cls_b = roi_to_device(int(W * 0.98), int(H * 0.056), state.game_roi)
            _btl_candidates = [_arr_b, _arr_b, _arr_b, _arr_b, _cls_b, _cls_b]
            _bidx = min(state.pre_popup_tap_count - 1, len(_btl_candidates) - 1)
            cx, cy = _btl_candidates[_bidx]
            _blabel = "×" if (cx, cy) == _cls_b else "▷"
            logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) (試行%d回目)",
                        tutorial_popup["text"][:10], _blabel, cx, cy, state.pre_popup_tap_count)
            tap_device(cx, cy, state, "BATTLE_TUTORIAL_POPUP")
            return "BATTLE_TUTORIAL", 0.5

        # AUTO ボタン
        if not state.auto_activated:
            ax, ay = roi_to_device(int(W * 0.845), int(H * 0.090), state.game_roi)
            logger.info(">>> AUTO タップ (%d,%d)", ax, ay)
            tap_device(ax, ay, state, "AUTO_ON")
            state.auto_activated = True
            return "BATTLE_AUTO", BATTLE_WAIT

        # バトル停滞時: ハイライト候補を順番にタップ試行
        if state.battle_wait_count > 8:
            stall_phase = (state.battle_wait_count - 8) % 12
            if stall_phase == 0:
                sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
                logger.info(">>> バトル停滞 — スキルタップ (%d,%d)", sx, sy)
                tap_device(sx, sy, state, "STALL_SKILL", rapid=True)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 0.0
            elif stall_phase == 4:
                hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU", rapid=True)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 0.0
            elif stall_phase == 8:
                # 探索バトル: 左パネルのキャラカードを再タップ (char_just_selected リセット)
                state.char_just_selected = False
                lx, ly = roi_to_device(int(W * 0.141), int(H * 0.875), state.game_roi)
                logger.info(">>> バトル停滞 — 左カード再タップ (%d,%d)", lx, ly)
                tap_device(lx, ly, state, "STALL_LEFT_CARD")
                return "BATTLE_STALL", 1.0

        # 高回数停滞: auto_activated リセットで再検出
        if state.battle_wait_count > 30:
            logger.warning(">>> バトル長期停滞 (count=%d) — auto_activated/char_justSelected リセット",
                           state.battle_wait_count)
            state.auto_activated = False
            state.char_just_selected = False
            state.battle_wait_count = 0

        logger.info(">>> バトル中 — 待機 (count=%d, auto=%s)",
                    state.battle_wait_count, state.auto_activated)
        return "BATTLE_WAIT", BATTLE_WAIT

    # バトル終了検出
    if state.battle_wait_count > 0:
        logger.info(">>> バトル終了検出 (wait_count was %d)", state.battle_wait_count)
        state.battle_wait_count = 0
        state.auto_activated = False

    # ─── バトル結果/リザルト ───
    result_match = has_any(ocr, ["リザルト", "Result", "RESULT", "勝利", "Victory",
                                  "クリア", "CLEAR", "EXP", "経験値", "ランクアップ"])
    if result_match:
        # まず「次へ」ボタンを優先タップ (右下エリアのみ: y>H*0.6 & x>W*0.5)
        _nxt_r = None
        for _ocr_item_r in ocr:
            _txt_r = _ocr_item_r.get("text", "")
            if "次へ" in _txt_r or "NEXT" in _txt_r:
                _rx_c, _ry_c = _ocr_item_r["center"]
                if _ry_c > H * 0.6 and _rx_c > W * 0.5:
                    _nxt_r = _ocr_item_r
                    break
        if _nxt_r:
            _rx, _ry = _nxt_r["center"]
            logger.info(">>> バトル結果【次へ優先】 (%d,%d) タップ", _rx, _ry)
            tap_device(_rx, _ry, state, "RESULT_NEXT")
            return "RESULT_TAP", 1.0
        cx, cy = result_match["center"]
        text = result_match["text"]
        logger.info(">>> バトル結果 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, "RESULT_TAP")
        return "RESULT_TAP", 1.0

    # ─── スキップ — 無効化 (ストーリースキップ禁止) ───
    # ストーリースキップを防止するため、"スキップ"/"SKIP" テキストタップを無効化。
    # ムービーの⏭ボタンは detect_movie_skip_button() (HSV検出) で別途処理される。

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
        else:
            ocr_ok_x, ocr_ok_y = int(W * 0.65), int(H * 0.88)  # 比率ベースフォールバック
        ok_x, ok_y = smart_tap_button(analysis_path, ocr_ok_x, ocr_ok_y, ocr_items=ocr)
        logger.info(">>> 【システムダイアログ】 '%s' → SmartTap OK (%d,%d)",
                    sys_dlg_match["text"][:15], ok_x, ok_y)
        tap_device(ok_x, ok_y, state, "SYSTEM_DLG_OK")
        return "SYSTEM_DLG_OK", 1.0

    # ─── メンテナンス/アップデート検出 ───
    _maint_kws = ["メンテナンス", "Maintenance", "maintenance"]
    _update_kws = ["アップデート", "Update", "update", "最新バージョン"]
    _maint_hit = has_any(ocr, _maint_kws, min_conf=0.3)
    _update_hit = has_any(ocr, _update_kws, min_conf=0.3)
    if _maint_hit:
        logger.warning(">>> [MAINTENANCE] メンテナンス検出: '%s' — 60秒待機", _maint_hit["text"])
        return "MAINTENANCE_WAIT", 60.0
    if _update_hit and not _in_battle_ctx:
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
            _tos_x, _tos_y = roi_to_device(int(W * 0.72), int(H * 0.89), state.game_roi)
            logger.info(">>> 【利用規約同意】 固定座標 (%d,%d) タップ", _tos_x, _tos_y)
            tap_device(_tos_x, _tos_y, state, "AGREE_TOS")
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

    # ─── ADVシーン: ↓ボタンのみタップ、上部アイコンは無視 ───
    if _adv_result.is_adv:
        if _adv_result.next_btn_pos:
            cx, cy = _adv_result.next_btn_pos
            logger.info(">>> ADV ↓ボタンタップ (%d,%d)", cx, cy)
            tap_device(cx, cy, state, "ADV_NEXT_TAP")
            return "ADV_NEXT_TAP", 0.3
        # ↓ ボタンなし → SKIP ボタンを探す (キャラ紹介/ガチャ演出等)
        _skip_match = has_any(ocr, ["SKIP", "スキップ"])
        if _skip_match:
            sx, sy = _skip_match["center"]
            logger.info(">>> ADV ↓未検出 → SKIP '%s' (%d,%d)", _skip_match["text"], sx, sy)
            tap_device(sx, sy, state, "ADV_SKIP_TAP")
            return "ADV_SKIP_TAP", 1.0
        # ↓テンプレ不一致でもADVツールバーは確定 → ↓想定位置にフォールバックタップ
        # NOTE: BUBBLE_TAP は ADV ツールバー誤タップのリスクがあるためここでは使わない
        _fb_x, _fb_y = roi_to_device(int(W * 0.855), int(H * 0.903), state.game_roi)
        logger.info(">>> ADV ↓未検出 → フォールバックタップ (%d,%d)", _fb_x, _fb_y)
        tap_device(_fb_x, _fb_y, state, "ADV_NEXT_FALLBACK")
        return "ADV_NEXT_FALLBACK", 0.3

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
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6]
    if lower_texts and len(ocr) <= 15:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── 右上吹き出しセリフ (メニュー画面上のキャラガイダンス) ───
    # 右上エリア (x>55%, y<35%) にテキストがあり、AUTO/>> ボタン等のUI要素と共存
    # → セリフが止まっている (前回と同一テキスト or phash安定) ならタップで送る
    _bubble_region = [r for r in ocr
                      if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                      and r["text"] not in ("AUTO", ">>", ">|", "D1", "×")]
    if _bubble_region and len(ocr) <= 20:
        _bubble = _bubble_region[0]
        _bx, _by = _bubble["center"]
        logger.info(">>> 吹き出しセリフ送り '%s' (%d,%d)", _bubble["text"][:10], _bx, _by)
        tap_device(_bx, _by, state, "BUBBLE_TAP")
        return "BUBBLE_TAP", 0.3

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


# ─── Watchdog: デッドロック自動復旧 ─────────────────────
def watchdog_recover(state: PilotState) -> bool:
    """
    Unityメインスレッドのデッドロックを検出した際の自動復旧。

    戦略 (pm clear は一切使用しない — BAN リスク排除):
      1〜3回目: am force-stop → am start (ソフト再起動のみ)
      4回目以降: 諦めて False を返す (人間に委譲)

    Returns: True=復旧試行を実施, False=諦め(mainが終了する)
    """
    state.watchdog_recovery_count += 1
    count = state.watchdog_recovery_count
    elapsed = time.time() - state.last_screen_change_time

    # 3回を超えたら諦めて人間に報告
    if count > WATCHDOG_MAX_TOTAL_RECOVERIES:
        logger.error(
            "[WATCHDOG] 復旧試行%d回失敗 (last_action=%s, %.0f秒経過) — 人間の介入が必要です。停止します。",
            count - 1, state.last_action, elapsed
        )
        return False

    # ソフト再起動のみ (pm clear は使用しない)
    logger.warning(
        "[WATCHDOG] デッドロック判定: 画面変化なし %.0f秒 / last_action=%s / 復旧試行 #%d",
        elapsed, state.last_action, count
    )
    logger.warning("[WATCHDOG] → am force-stop → am start (ソフト再起動のみ。pm clearは使用しない)")
    adb(f"shell am force-stop {APP_PACKAGE}")
    time.sleep(3)

    # 再起動
    adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
    logger.info("[WATCHDOG] am start 実行 — 15秒待機 (初期化 + ご注意画面の出現を待つ)")
    time.sleep(15)  # 起動＋スプラッシュ待機

    # 状態リセット (デバイス解像度・回数・周回進捗は保持)
    # force-stop → am start でタイトル画面に戻るため、
    # home_reached / auto_activated 等のフラグもリセットする。
    state.last_phash = ""
    state.same_phash_count = 0
    state.stall_start = 0.0
    state.stall_corner_tried = False
    state.home_reached = False
    state.auto_activated = False
    state.character_selected = False
    state.char_just_selected = False
    state.battle_wait_count = 0
    state.last_action = "WATCHDOG_RECOVERY"
    state.last_screen_change_time = time.time()
    return True


# ─── レポート生成 + クリップボードコピー ────────────────
def generate_and_copy_report(state: PilotState, reason: str) -> None:
    """
    ホーム到達またはエラー停止時に状況レポートを生成し pbcopy でコピー。
    Gemini へのペースト用。
    """
    # Git コミット情報
    try:
        commit_id = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=str(_CRAWLER_ROOT.parent), timeout=5,
        ).stdout.strip()
    except Exception:
        commit_id = "unknown"

    last_ocr_preview = ", ".join(state.last_ocr_texts[:6]) if state.last_ocr_texts else "(なし)"
    report_lines = [
        "=" * 64,
        "# まどドラ自律操縦レポート (auto_pilot.py)",
        f"停止理由: {reason}",
        f"日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 修正/確認したファイルと定数",
        "- `crawler/tools/auto_pilot.py`",
        f"  - WATCHDOG_DEADLOCK_THRESHOLD: {WATCHDOG_DEADLOCK_THRESHOLD} 秒",
        f"  - WATCHDOG_MAX_TOTAL_RECOVERIES: {WATCHDOG_MAX_TOTAL_RECOVERIES}",
        f"  - WATCHDOG_EXEMPT_ACTIONS: {', '.join(sorted(WATCHDOG_EXEMPT_ACTIONS))}",
        f"  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒",
        f"  - DOWNLOAD_WAIT: {DOWNLOAD_WAIT} 秒/ループ (無限忍耐・Watchdog免除)",
        f"  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB",
        "",
        "## 現在の画面状況",
        f"- 最終アクション : {state.last_action}",
        f"- 現在シーン    : {state.current_scene}",
        f"- 最終OCRテキスト: {last_ocr_preview}",
        f"- ホーム到達    : {state.home_reached}",
        "",
        "## 実行統計",
        f"- 総ループ数           : {state.iteration + 1}",
        f"- 総タップ数           : {state.total_taps}",
        f"- OCR実行回数          : {state.total_ocr_calls}",
        f"- OCRスキップ          : {state.total_ocr_skipped}",
        f"- 暗転スキップ         : {state.total_blackout_skipped}",
        f"- SIGSEGV回避回数      : {state.screenshot_retry_count}",
        f"- Watchdog復旧試行     : {state.watchdog_recovery_count}",
        f"- エビデンス保存数     : {state.screenshots_saved}",
        f"- 平均判定速度         : {state.total_loop_ms / max(state.iteration + 1, 1):.0f} ms/loop",
        "",
        "## 主要検知成功率",
        f"- Dialog検知           : {state.dialog_detections} 回",
        f"- Finger検知           : {state.finger_detections} 回",
        f"- GoldBtn検知          : {state.gold_detections} 回",
        "",
        "## テレメトリ",
        f"- 平均遷移時間         : {sum(state.transition_times) / max(len(state.transition_times), 1):.1f}s",
        f"- 最大遷移時間         : {max(state.transition_times) if state.transition_times else 0:.1f}s",
        f"- 10秒超遷移回数       : {sum(1 for t in state.transition_times if t > _TRANSITION_SLOW_SEC)}",
        f"- 計測サンプル数       : {len(state.transition_times)}",
        "",
        "## 戦績サマリー",
        f"- ホーム到達           : {'✓ CLEARED' if state.home_reached else '未到達'}",
        f"- チュートリアル       : {'All Tutorials Cleared' if state.home_reached else '進行中'}",
        f"- 周回モード           : {'ON' if state.grind_mode else 'OFF'}",
        f"- 周回完了数           : {state.grind_cycles_completed}",
        f"- 最終シーン           : {state.current_scene}",
        f"- Rank                 : {'1 / HOME REACHED' if state.home_reached else 'In Progress'}",
        "",
        "## 最新コミット",
        f"- commit: {commit_id}",
        f"- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/{commit_id}",
        "=" * 64,
    ]
    report = "\n".join(report_lines)

    try:
        storage_dir = _CRAWLER_ROOT / "storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        report_path = storage_dir / "auto_pilot_report.md"
        report_path.write_text(report, encoding="utf-8")
        # pbcopy でクリップボードにコピー
        subprocess.run(
            ["bash", "-c", f"cat '{report_path}' | pbcopy"],
            check=False, timeout=5,
        )
        logger.info("[REPORT] レポート保存: %s", report_path)
        logger.info("[REPORT] クリップボードにコピー完了 — Gemini にペーストしてください")
    except Exception as e:
        logger.error("[REPORT] コピー失敗: %s", e)

    print("\n" + report)
    print("\n>>> 報告をクリップボードにコピーしました。Geminiにペーストしてください。")


# ─── BATTLE 高速パス: OCR 前テンプレートマッチング ──────────────────
def _battle_fast_check(analysis_path: Path,
                       state: "PilotState") -> tuple[str, float]:
    """
    BATTLE シーン専用 OCR 前高速判定。
    OpenCV のみで GoldSwipe / GoldBtn を検出し、見つかれば即タップして (action, wait) を返す。
    見つからなければ ("", 0.0) を返して通常 OCR フローへ移行する。
    """
    # 1. GoldSwipe (Type A) — チュートリアル移動シーン
    # 前回OCRでバトルUIキーワード(通常攻撃/WAVE/Turn等)を確認済みならSPゲージ誤検出の
    # 可能性が高いためスキップ
    _confirmed_battle_ui = any(kw in state.last_ocr_texts for kw in _BATTLE_UI_KWS)
    if _confirmed_battle_ui:
        logger.debug("[FAST] バトルUI確認済み → GoldSwipe スキップ (SPゲージ誤検出防止)")
    gs = None if _confirmed_battle_ui else detect_tutorial_gold_swipe(analysis_path)
    if gs:
        _dir, _sx, _fy, _ty, _dur = gs
        # 距離が短すぎる場合はフルスクリーンスワイプを強制 (解像度差対策)
        _min_dist = int(ANALYSIS_H * 0.6)
        if abs(_fy - _ty) < _min_dist:
            if _dir == "UP":
                _fy = ANALYSIS_H - 50
                _ty = 50
            else:
                _fy = 50
                _ty = ANALYSIS_H - 50
        logger.info("[FAST] GoldSwipe %s → swipe (%d,%d)→(%d,%d) %dms",
                    _dir, _sx, _fy, _sx, _ty, _dur)
        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc=f"FAST_GoldSwipe_{_dir}")
        return ("GOLD_SWIPE_UP" if _dir == "UP" else "GOLD_SWIPE_DOWN"), BATTLE_WAIT

    # 2. GoldBtn (Type B) — バトルチュートリアルボタン (右半分)
    # バトルUI確認済み (通常攻撃/WAVE等) の場合はフッター外の金色オブジェクトに
    # 誤反応しないよう GoldBtn もスキップし、Glow SM (フッター優先) に委ねる。
    if _confirmed_battle_ui:
        logger.debug("[FAST] バトルUI確認済み → GoldBtn スキップ (フッター発光SMへ委譲)")
    else:
        gb = detect_tutorial_gold_button_tap(analysis_path, right_half_only=True)
        if gb:
            gx, gy = gb
            logger.info("[FAST] GoldBtn → tap(%d,%d)", gx, gy)
            tap_device(gx, gy, state, "GOLD_BTN_TAP")
            return "GOLD_BTN_TAP", BATTLE_WAIT

    return "", 0.0


# ─── 早期シーン判定 ─────────────────────────────────
def detect_scene_early(img_path: Path, state: PilotState, dist: int) -> str:
    """OCR 前にシーンを判定する。

    利用する信号 (すべて OCR 不要):
    - レターボックス (ROI 左端 >= 80) + ADV ツールバーなし → MOVIE
    - 前回シーン == BATTLE + phash 小変化 → BATTLE
    - ADV ツールバー検出 → ADV
    - それ以外 → UNKNOWN (フルOCR 必要)

    Returns: "MOVIE" | "BATTLE" | "ADV" | "UNKNOWN"
    """
    # MOVIE: レターボックス (左黒帯 >= 80px) + ADV ツールバーなし
    roi_x = state.game_roi[0] if state.game_roi else 0
    if roi_x >= 80:
        adv = detect_adv_scene_cached(img_path, state)
        if not adv.is_adv:
            return "MOVIE"

    # BATTLE: 前回シーン == BATTLE + phash 小変化 (シーン継続)
    if state.current_scene == "BATTLE" and dist < 30:
        return "BATTLE"

    # ADV: ツールバー検出 (MENU シーンは OCR でボタン検出が必要)
    if state.current_scene != "MENU":
        adv = detect_adv_scene_cached(img_path, state)
        if adv.is_adv:
            return "ADV"

    return "UNKNOWN"


def handle_movie(img_path: Path, state: PilotState, dist: int,
                 cur_phash: str) -> bool:
    """動画シーン専用ハンドラ。指アイコン / GoldSwipe / 金枠ボタン検出なし。

    - post_download なら SKIP ボタンを HSV で探してタップ
    - そうでなければ待機 (MOVIE_WAIT)
    - 8 回連続待機でエスケープ (post_download → SKIP 位置, 通常 → 中央タップ)

    Returns: True if handled (caller should continue), False for fallthrough.
    """
    W, H = ANALYSIS_W, ANALYSIS_H
    _MOVIE_WAIT_ESCAPE = 8

    # ── post_download → SKIP ボタン検出 ──
    if state.post_download:
        _movie_btn = detect_movie_skip_button(img_path)
        if _movie_btn:
            _sk_x, _sk_y = _movie_btn
            _sk_x, _sk_y = roi_to_device(_sk_x, _sk_y, state.game_roi)
            logger.info("[MOVIE] DL直後 SKIP ボタン検出 → タップ (%d,%d)", _sk_x, _sk_y)
            tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP")
            state.last_action = "MOVIE_SKIP"
            state.movie_wait_consecutive = 0
            state.last_phash = ""
            return True

    # ── 待機カウンタ ──
    state.movie_wait_consecutive += 1

    # ── エスケープ: 連続待機上限到達 ──
    if state.movie_wait_consecutive >= _MOVIE_WAIT_ESCAPE:
        if state.post_download:
            logger.warning(
                "[MOVIE] DL直後+動画待機 %d 回 → SKIP タップ",
                state.movie_wait_consecutive)
            state.movie_wait_consecutive = 0
            _x, _y = roi_to_device(int(W * 0.93), int(H * 0.06), state.game_roi)
            tap_device(_x, _y, state, "MOVIE_SKIP_ESCAPE")
            state.last_action = "MOVIE_SKIP"
        else:
            logger.warning(
                "[MOVIE] 動画待機 %d 回 → 画面中央タップ",
                state.movie_wait_consecutive)
            state.movie_wait_consecutive = 0
            _x, _y = roi_to_device(int(W * 0.5), int(H * 0.5), state.game_roi)
            tap_device(_x, _y, state, "MOVIE_RESUME_TAP")
            state.last_action = "SCENE_TAP"
        state.last_phash = ""
        state.same_phash_count = 0
        return True

    # ── 通常待機 ──
    roi_x = state.game_roi[0] if state.game_roi else 0
    logger.info("[MOVIE] letterbox L=%d → 待機 (%d/%d)",
                roi_x, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
    state.last_action = "MOVIE_WAIT"
    state.stall_start = 0.0
    time.sleep(0.5)
    state.last_phash = cur_phash
    return True


def handle_battle(analysis_path: Path, state: PilotState, dist: int) -> bool:
    """バトルシーン専用ハンドラ。発光/モヤ/通常攻撃のみ。GoldSwipe なし。

    既存 BATTLE_RAPID のロジックを関数化。
    - Phase 0: チュートリアル金枠 + 指ブロブ
    - Phase A: アクティブキャラ (赤/ピンク発光)
    - Phase B: 右側スキル/攻撃ボタン
    - Phase C: 通常攻撃フォールバック

    Returns: True if handled, False for fallthrough to OCR.
    """
    # ── force OCR override: phash 静止 → ダイアログ可能性 ──
    if dist <= 2 and state.same_phash_count >= FORCE_ANALYZE_AFTER:
        logger.info("[BATTLE] force_ocr (dist=%d, same=%d) → OCR フォールスルー",
                    dist, state.same_phash_count)
        return False

    # 速度チュートリアル表示中は OCR で処理
    if any(any(k in t for k in ("このボタンでバトル", "進行速度を変更"))
           for t in state.last_ocr_texts):
        return False

    # BATTLE_RAPID 連続ループ上限
    if state.battle_rapid_consecutive.stalled:
        logger.info("[BATTLE] 連続 %d 回 → OCR で再評価",
                    state.battle_rapid_consecutive.count)
        state.battle_rapid_consecutive.reset()
        return False

    _rapid_tx = _rapid_ty = 0
    _rapid_action = ""
    _rapid_double = False

    # ── 共通: 指ブロブ検出 ──
    _rapid_blobs = find_finger_blobs(analysis_path, min_area=200, dark_mode=True)
    _rapid_blobs = [b for b in _rapid_blobs
                    if b[1] > _SPATIAL_MARGIN_TOP and b[0] < ANALYSIS_W - _CLOSE_BTN_OFFSET]

    # ── Phase 0: チュートリアル金枠+指 → 最優先タップ ──
    _rapid_tutorial_gold = [b for b in _rapid_blobs if b[2] > 10000]
    if _rapid_tutorial_gold:
        _gold_tap = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False)
        if _gold_tap:
            _rapid_tx, _rapid_ty = _gold_tap
            _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"

    # ── Phase A: アクティブキャラ検出 (赤/ピンク発光) ──
    _active_char = detect_active_battle_char(analysis_path, ANALYSIS_W, ANALYSIS_H)
    if not _rapid_action and not state.character_selected and _active_char is not None:
        _rapid_tx, _rapid_ty = _active_char[0], _active_char[1]
        _rapid_action = "BATTLE_RAPID_ACTIVE_P1"
        _rapid_double = True

    # ── Phase B: 右側スキル/攻撃ボタン ──
    if not _rapid_action:
        _rapid_glows = detect_guide_glow(
            analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
        _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]
        _right_panel = [b for b in _rapid_blobs
                        if b[0] > _RIGHT_PANEL_X and b[1] > ANALYSIS_H * 0.45]
        if state.character_selected or state.char_just_selected:
            if _rapid_right_g:
                _rr = max(_rapid_right_g, key=lambda g: g["area"])
                _rapid_tx = _rr["cx"]
                _rapid_ty = max(1, _rr["by"] + _rr["bh"] // 3)
                _rapid_action = "BATTLE_RAPID_GLOW_P2"
            elif _right_panel:
                _tb = max(_right_panel, key=lambda b: b[2])
                _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                _rapid_action = "BATTLE_RAPID_MOYA_P2"
            else:
                _rapid_tx, _rapid_ty = roi_to_device(
                    int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                _rapid_action = "BATTLE_RAPID_NORMATK_P2"

    # ── Phase C: フォールバック → 右側攻撃ボタン ──
    if not _rapid_action:
        if state.normatk_fallback.stalled:
            logger.info("[BATTLE] FALLBACK %d回連続 → OCR で再評価",
                        state.normatk_fallback.count)
            state.normatk_fallback.reset()
            return False
        _rapid_tx, _rapid_ty = roi_to_device(
            int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
        _rapid_action = "BATTLE_RAPID_NORMATK_FALLBACK"
        state.normatk_fallback.tick()
    else:
        state.normatk_fallback.reset()

    # ── 共通タップ実行 ──
    if _rapid_action:
        logger.info("[%s] tap(%d,%d)%s",
                    _rapid_action, _rapid_tx, _rapid_ty,
                    " ダブルタップ" if _rapid_double else "")
        tap_device(_rapid_tx, _rapid_ty, state, _rapid_action,
                   rapid=_rapid_double)
        if _rapid_double:
            tap_device(_rapid_tx, _rapid_ty, state, _rapid_action)
        # 状態更新
        if "P1" in _rapid_action:
            state.character_selected = True
            state.char_just_selected = True
        else:
            state.character_selected = False
            state.char_just_selected = False
        state.finger_detections += 1
        state.last_action = _rapid_action
        state.stall_start = 0.0
        state.stall_corner_tried = False
        state.same_phash_count = 0
        state.battle_rapid_consecutive.tick()
        return True

    return False


def handle_adv(img_path: Path, state: PilotState, dist: int,
               cur_phash: str, actual_w: int, actual_h: int) -> bool:
    """ADV シーン専用ハンドラ。↓ボタン / バーストタップ / ミニ会話。

    GoldSwipe / 指アイコン / バトル判定なし。

    Returns: True if handled, False for fallthrough to OCR.
    """
    W, H = ANALYSIS_W, ANALYSIS_H
    adv = detect_adv_scene_cached(img_path, state)
    _adv_tap_x = int(W * 0.93)
    _adv_tap_y = int(H * 0.91)

    # ── ↓アイコン or ADV ツールバー → バーストタップ ──
    if adv.is_adv or detect_adv_advance_icon(img_path):
        _burst_count = 0
        _burst_max = 3
        _burst_img = img_path
        while _burst_count < _burst_max:
            if detect_adv_advance_icon(_burst_img):
                _burst_count += 1
                logger.info("[ADV] ↓検出 → タップ #%d (%d,%d)",
                            _burst_count, _adv_tap_x, _adv_tap_y)
                tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                state.last_action = "ADV_RAPID_TAP"
                _b_path, _b_w, _b_h, _ = take_screenshot()
                if _b_path is None:
                    break
                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
            elif adv.is_adv and adv.next_btn_pos and _burst_count == 0:
                _adv_nx = int(adv.next_btn_pos[0] * W / actual_w)
                _adv_ny = int(adv.next_btn_pos[1] * H / actual_h)
                logger.info("[ADV] ↓ボタン座標 (%d,%d)", _adv_nx, _adv_ny)
                tap_device(_adv_nx, _adv_ny, state, "ADV_RAPID_TAP")
                state.last_action = "ADV_RAPID_TAP"
                _burst_count += 1
                _b_path, _b_w, _b_h, _ = take_screenshot()
                if _b_path is None:
                    break
                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
            else:
                break
        if _burst_count > 0:
            logger.info("[ADV] バースト完了: %d タップ", _burst_count)
            state.movie_wait_consecutive = 0
            state.last_phash = ""
            return True

    # ── ミニ会話バーストタップ ──
    _mc = detect_mini_conversation(img_path)
    if _mc is not None:
        _burst_count = 0
        _burst_max = 3
        _burst_img = img_path
        while _burst_count < _burst_max:
            _mc_res = detect_mini_conversation(_burst_img)
            if _mc_res is not None:
                _mc_cx, _mc_cy, _mc_side = _mc_res
                _burst_count += 1
                logger.info("[ADV] 吹き出し(%s) → タップ #%d (%d,%d)",
                            _mc_side, _burst_count, _mc_cx, _mc_cy)
                tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                state.last_action = "MINI_CONV_TAP"
                _b_path, _b_w, _b_h, _ = take_screenshot()
                if _b_path is None:
                    break
                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
            else:
                break
        if _burst_count > 0:
            logger.info("[ADV] 吹き出しバースト完了: %d タップ", _burst_count)
            state.movie_wait_consecutive = 0
            state.last_phash = ""
            return True

    # ↓ なし + 吹き出しなし → フォールスルー
    return False


# ─── コマンドライン引数 ───────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="まどドラ自律操縦")
    parser.add_argument("--verbose", action="store_true", help="デバッグログ出力")
    parser.add_argument("--grind", action="store_true",
                        help="周回モード: ホーム到達後もクエストへ自動ナビゲート")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="周回上限 (0=無制限, デフォルト: 0)")
    parser.add_argument("--pairing-code", type=str, default=None,
                        help="adb pair 用ペアリングコード (Android 11+)")
    parser.add_argument("--pairing-port", type=int, default=None,
                        help="adb pair 用ポート番号 (Android 11+)")
    parser.add_argument("--fresh-install", action="store_true",
                        help="アンインストール → Play Store 再インストール (新規アカウント)")
    parser.add_argument("--wifi-addr", type=str, default=None,
                        help="Wi-Fi ADB 接続先アドレス (IP:PORT, 例: 192.168.10.118:5555)")
    # parse_known_args: main.py 経由の場合に --android, --package 等の未知引数を無視
    args, _ = parser.parse_known_args()
    return args


# ─── Play Store 再インストール ──────────────────────
def _fresh_install_from_play_store(serial: str, package: str) -> None:
    """
    アプリをアンインストール → Play Store から再インストールする。

    PilotState はまだ存在しない段階で呼ばれるため、
    tap_device() ではなく直接 adb shell input tap を使用する。
    """
    INSTALL_KEYWORDS = ["インストール", "Install", "install"]
    ACCEPT_KEYWORDS = ["同意する", "Accept", "OK"]
    OPEN_KEYWORDS = ["開く", "Open"]
    MAX_OCR_ATTEMPTS = 10
    POLL_INTERVAL_SEC = 5
    MAX_POLL_COUNT = 60  # 5秒 × 60 = 5分

    def _adb_tap(x: int, y: int) -> None:
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
            capture_output=True, timeout=5,
        )

    def _adb_screenshot(path: str) -> bool:
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and len(r.stdout) >= 5_000:
                Path(path).write_bytes(r.stdout)
                return True
        except (subprocess.TimeoutExpired, Exception):
            pass
        return False

    # --- Step 1: アンインストール ---
    logger.info("[FRESH_INSTALL] === アプリ再インストール開始 ===")
    uninstall_app(serial, package)
    time.sleep(2)

    # --- Step 2: Play Store を開く ---
    if not open_play_store(serial, package):
        logger.error("[FRESH_INSTALL] Play Store を開けませんでした。手動で対応してください。")
        return
    time.sleep(5)

    # --- Step 3: OCR → インストールボタンをタップ ---
    tmp_ss = str(Path(tempfile.gettempdir()) / "fresh_install_ss.png")
    installed_via_tap = False

    for attempt in range(MAX_OCR_ATTEMPTS):
        logger.info("[FRESH_INSTALL] OCR 試行 %d/%d", attempt + 1, MAX_OCR_ATTEMPTS)
        if not _adb_screenshot(tmp_ss):
            logger.warning("[FRESH_INSTALL] スクリーンショット取得失敗 — リトライ")
            time.sleep(2)
            continue

        try:
            ocr_results = run_ocr(tmp_ss)
        except Exception as e:
            logger.warning("[FRESH_INSTALL] OCR 失敗: %s", e)
            time.sleep(2)
            continue

        # 「開く」が見える → 既にインストール済み → アンインストール再試行
        for kw in OPEN_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                logger.info("[FRESH_INSTALL] 「%s」検出 — 既インストール → 再アンインストール", kw)
                uninstall_app(serial, package)
                time.sleep(2)
                open_play_store(serial, package)
                time.sleep(5)
                break

        # 「インストール」ボタンを検出
        for kw in INSTALL_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                cx, cy = hit["center"]
                logger.info("[FRESH_INSTALL] 「%s」検出 → タップ (%d, %d)", kw, cx, cy)
                _adb_tap(cx, cy)
                installed_via_tap = True
                time.sleep(3)
                break
        if installed_via_tap:
            # 権限ダイアログ処理
            time.sleep(2)
            if _adb_screenshot(tmp_ss):
                try:
                    ocr2 = run_ocr(tmp_ss)
                    for akw in ACCEPT_KEYWORDS:
                        ahit = find_best(ocr2, akw)
                        if ahit:
                            ax, ay = ahit["center"]
                            logger.info("[FRESH_INSTALL] 「%s」検出 → タップ (%d, %d)", akw, ax, ay)
                            _adb_tap(ax, ay)
                            break
                except Exception:
                    pass
            break

        # ポップアップ (Google Play Games 等) が遮っている可能性 → BACK で閉じて再表示
        logger.info("[FRESH_INSTALL] インストールボタン未検出 → BACK + Play Store 再表示")
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", "4"],
            capture_output=True, timeout=5,
        )
        time.sleep(2)
        open_play_store(serial, package)
        time.sleep(5)

    # --- Step 4: インストール完了ポーリング ---
    logger.info("[FRESH_INSTALL] インストール完了を待機中... (最大%d秒)", POLL_INTERVAL_SEC * MAX_POLL_COUNT)
    for i in range(MAX_POLL_COUNT):
        if is_app_installed(serial, package):
            logger.info("[FRESH_INSTALL] インストール完了を確認 (%d秒経過)", (i + 1) * POLL_INTERVAL_SEC)
            break
        time.sleep(POLL_INTERVAL_SEC)
    else:
        logger.error("[FRESH_INSTALL] タイムアウト — 手動でインストールを完了してください")
        return

    # --- Step 5: Play Store を閉じる ---
    time.sleep(2)
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "force-stop", "com.android.vending"],
        capture_output=True, timeout=5,
    )
    logger.info("[FRESH_INSTALL] Play Store を閉じました")
    time.sleep(2)
    logger.info("[FRESH_INSTALL] === 再インストール完了 → 通常起動シーケンスへ ===")


# ─── メインループ ─────────────────────────────────
def main():
    import tools.ap.constants as _ap_const  # _DEBUG_SAVE_IMAGES 直接書換え用

    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        _ap_const._DEBUG_SAVE_IMAGES = True

    # ─── ADB 自動接続: USB → Wi-Fi フォールバック ───
    try:
        _detected = ensure_adb_connection(
            wifi_addr=args.wifi_addr or WIFI_DEVICE_ADDR,
            pairing_code=args.pairing_code,
            pairing_port=args.pairing_port,
        )
        if not os.environ.get("ANDROID_UDID") and not os.environ.get("ANDROID_SERIAL"):
            os.environ["ANDROID_UDID"] = _detected
        _serial = get_android_serial()
        set_device_serial(_serial)
        set_scrcpy_device(_serial)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # ─── --fresh-install: アンインストール → Play Store 再インストール ───
    if args.fresh_install:
        _fresh_install_from_play_store(_ap_device.DEVICE_SERIAL, APP_PACKAGE)

    # ─── ゲーム未インストール保護 ───
    if not is_app_installed(_ap_device.DEVICE_SERIAL, APP_PACKAGE):
        logger.error("[ABORT] ゲーム '%s' がインストールされていません。", APP_PACKAGE)
        logger.error("[ABORT] --fresh-install でのインストールに失敗した可能性があります。")
        logger.error("[ABORT] 手動でインストールしてから再実行してください。")
        sys.exit(1)

    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (ハイブリッド版)")
    logger.info("  デバイス: %s", _ap_device.DEVICE_SERIAL)
    logger.info("  ポーリング: %.1fs  強制解析: %d回変化なし  スタックTimeout: %.0fs",
                POLL_INTERVAL, FORCE_ANALYZE_AFTER, STALL_TIMEOUT)
    if args.grind:
        _cycle_str = f"{args.max_cycles}周" if args.max_cycles > 0 else "無制限"
        logger.info("  周回モード: ON (%s)", _cycle_str)
    logger.info("=" * 62)

    state = PilotState()
    state.grind_mode = args.grind
    state.grind_max_cycles = args.max_cycles
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # ─── Ctrl+C シグナルハンドラ登録 (レポート自動生成) ───
    global _pilot_state_ref
    _pilot_state_ref = state

    def _sigint_handler(signum, frame):
        logger.info("\n[Ctrl+C] 手動停止 — レポートを生成します...")
        generate_and_copy_report(_pilot_state_ref, "手動停止 (Ctrl+C / SIGINT)")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    # ─── scrcpy 管理: 不整合 Kill → 規定オプション起動 ───
    _scrcpy_proc = manage_scrcpy()

    # 起動直後に実機物理解像度を取得してログ出力 (ループ内の初回 take_screenshot より早期)
    _dev_w, _dev_h = get_device_resolution()
    logger.info("[DEVICE_RES] wm size: %dx%d / 解析基準: %dx%d (ROI補正で座標変換)",
                _dev_w, _dev_h, ANALYSIS_W, ANALYSIS_H)

    logger.info("[TOKEN_SAVE] 節約モード稼働中。バトル発光検知で OCR スキップ → 爆速モードで進行します")

    # ─── 初回アプリ起動: ランチャーにいる場合は自動で起動 ───
    try:
        # 画面ウェイクアップ (scrcpy --turn-screen-off で消灯済みの場合)
        adb("shell input keyevent KEYCODE_WAKEUP")
        time.sleep(1)
        _focus = adb("shell dumpsys window")
        if APP_PACKAGE not in _focus:
            logger.info("[STARTUP] アプリ未起動 → am start で起動します")
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
            logger.info("[STARTUP] 15秒待機 (スプラッシュ + 初期化)")
            time.sleep(15)
        else:
            logger.info("[STARTUP] アプリ既に起動中: %s", APP_PACKAGE)
    except Exception as _e:
        logger.warning("[STARTUP] フォーカス確認失敗: %s — am start で起動を試行", _e)
        adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(15)

    # ─── ランドスケープ待機: ポートレートならアプリ起動待ち ───
    for _orient_wait in range(10):
        _ss_check = take_screenshot()
        if _ss_check[0] is not None and _ss_check[1] > _ss_check[2]:
            logger.info("[STARTUP] ランドスケープ確認 (%dx%d)", _ss_check[1], _ss_check[2])
            break
        logger.info("[STARTUP] ポートレート検出 (%dx%d) — アプリ起動待ち (%d/10)",
                    _ss_check[1], _ss_check[2], _orient_wait + 1)
        if _orient_wait == 4:
            # 5回目で再起動を試行
            logger.info("[STARTUP] アプリ再起動を試行")
            adb(f"shell am force-stop {APP_PACKAGE}")
            time.sleep(2)
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(3)

    for i in range(MAX_ITERATIONS):
        state.iteration = i
        _loop_t0 = time.time()  # [PERF] ループ開始時刻

        # ── 定期健診 (100 iter ごと) ──
        if i > 0 and i % 100 == 0:
            logger.info("[WATCHDOG] Periodic check (iter=%d). Running physical diagnostics...", i)
            if not check_adb_liveness():
                logger.warning("[WATCHDOG] Periodic check FAILED → attempting reconnect")
                subprocess.run(["adb", "kill-server"], timeout=5)
                time.sleep(2)
                subprocess.run(["adb", "start-server"], timeout=5)
                time.sleep(2)
                if _ap_device.DEVICE_SERIAL:
                    subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5)
                    time.sleep(1)
                # scrcpy が ADB 再起動で死んだ場合は再起動
                if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] WATCHDOG ADB reconnect 後に再起動")
                    _scrcpy_proc = manage_scrcpy()
            else:
                logger.info("[WATCHDOG] Periodic check OK")

        # ── 1) スクリーンショット取得 ──
        img_path, actual_w, actual_h, _ss_retries = take_screenshot()
        state.screenshot_retry_count += _ss_retries
        if _ss_retries > 0:
            logger.info("[SCREENSHOT] 破損リトライ %d回 (累計: %d回)",
                        _ss_retries, state.screenshot_retry_count)
        # ── Wi-Fi 破損防御: 全リトライ失敗 → continue で次ループ ──
        if img_path is None:
            state.wifi_fail_streak += 1
            logger.warning("[WIFI_ERROR] 連続失敗 %d/5 — 次ループで再取得",
                           state.wifi_fail_streak)
            if state.wifi_fail_streak >= 5:
                logger.error("[WIFI_ERROR] 連続5回失敗 → ADB再接続を試行")
                try:
                    subprocess.run(["adb", "disconnect"], timeout=5, capture_output=True)
                    time.sleep(1)
                    subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5, capture_output=True)
                    time.sleep(2)
                except Exception as _rc_e:
                    logger.error("[WIFI_ERROR] ADB再接続例外: %s", _rc_e)
                state.wifi_fail_streak = 0
            time.sleep(1.0)
            continue
        state.wifi_fail_streak = 0  # 成功時リセット
        # メモリ上に最新画像を保持 + ROI更新 (スロットル: 画面変化時 or 50iter毎)
        try:
            state.last_screen = cv2.imread(str(img_path))
            _roi_needed = (state.game_roi is None or i % 50 == 0
                           or state.same_phash_count == 0)  # phash変化直後
            if state.last_screen is not None and _roi_needed:
                _new_roi = detect_game_roi(state.last_screen)
                # 非黒画面のときのみ ROI を更新 (暗転中は前の ROI を維持)
                if _new_roi[2] >= actual_w * 0.5:
                    # デバイス空間 → 解析空間に正規化 (Xperia等の非1520x720デバイス対応)
                    if actual_w != ANALYSIS_W or actual_h != ANALYSIS_H:
                        _sx = ANALYSIS_W / actual_w
                        _sy = ANALYSIS_H / actual_h
                        _new_roi = (
                            int(_new_roi[0] * _sx), int(_new_roi[1] * _sy),
                            int(_new_roi[2] * _sx), int(_new_roi[3] * _sy),
                        )
                    if _new_roi != state.game_roi:
                        logger.info("[ROI] ゲーム描画領域更新: x=%d y=%d w=%d h=%d (黒帯: L=%d R=%d T=%d B=%d)",
                                    _new_roi[0], _new_roi[1], _new_roi[2], _new_roi[3],
                                    _new_roi[0], ANALYSIS_W - _new_roi[0] - _new_roi[2],
                                    _new_roi[1], ANALYSIS_H - _new_roi[1] - _new_roi[3])
                        state.game_roi = _new_roi
        except Exception as _e:
            logger.debug("[ROI] detect_game_roi 例外: %s", _e)
            state.last_screen = None

        if not state.device_w:
            state.device_w = actual_w
            state.device_h = actual_h
            logger.info("実機解像度: %dx%d (解析基準: %dx%d)",
                        actual_w, actual_h, ANALYSIS_W, ANALYSIS_H)
        elif (actual_w, actual_h) != (state.device_w, state.device_h):
            # portrait→landscape 遷移など解像度変化を追跡
            logger.info("解像度変化: %dx%d → %dx%d", state.device_w, state.device_h, actual_w, actual_h)
            state.device_w = actual_w
            state.device_h = actual_h

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            state.total_blackout_skipped += 1
            state.consecutive_blackouts += 1
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転 — 3s 待機 (連続: %d)",
                            i, state.consecutive_blackouts)
            # ── 暗転復帰: 画面中央タップ (スキップボタンは押さない) ──
            # 長時間暗転 (30回=~90秒) → 画面中央をタップして復帰を試みる
            if state.consecutive_blackouts >= 30 and state.consecutive_blackouts % 10 == 0:
                logger.info("[BLACKOUT_RECOVER] 連続暗転 %d 回 → 画面中央タップで復帰試行",
                            state.consecutive_blackouts)
                tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "BLACKOUT_RECOVER")
            else:
                time.sleep(0.5)
            state.last_phash = ""
            state.same_phash_count = 0
            continue

        # ── 暗転解除 ──
        if state.consecutive_blackouts > 0:
            state.consecutive_blackouts = 0

        # ── 3) phash 粗解析 ──
        try:
            cur_phash = compute_phash(img_path)
        except Exception:
            cur_phash = ""

        if state.last_phash and cur_phash:
            dist = phash_distance(state.last_phash, cur_phash)
        else:
            dist = 999
        state.last_phash_dist = dist

        screen_changed = dist >= PHASH_THRESHOLD

        # ── 動的しきい値: Gold UI アクション後はアニメーション変化でも即解析 ──
        if not screen_changed and state.last_action in _GOLD_UI_ACTIONS and dist >= 1:
            screen_changed = True
            state.same_phash_count = 0
            logger.debug("  [DYN_PHASH] %s 後 dist=%d → 即解析", state.last_action, dist)

        # ── AUTO バトル中: dist>=1 でもウォッチドッグタイマーをリセット ──
        # バトルアニメーションは phash_dist=1-4 の微小変化が続くため、
        # PHASH_THRESHOLD=5 を超えなくてもバトルは進行中なのでウォッチドッグを抑制。
        if (not screen_changed and dist >= 1
                and state.last_action in ("BATTLE_WAIT", "BATTLE_AUTO", "BATTLE_STALL")
                and state.auto_activated):
            state.last_screen_change_time = time.time()
            state.stall_start = 0.0

        # ── 早期シーン判定 ──
        # OCR 前にシーンを分類し、シーン別ハンドラへルーティングする。
        # MOVIE: 指/GoldSwipe 誤発動を防止
        # BATTLE: GoldSwipe/ADV チェックをスキップ
        # ADV: 指/GoldSwipe/バトル判定をスキップ
        # UNKNOWN: フルOCR → detect_and_act() (既存フロー)
        _early_scene = detect_scene_early(img_path, state, dist)
        _skip_rapid = False  # True: 早期ハンドラがフォールスルー → インライン RAPID をスキップ
        _early_analysis = None  # BATTLE 用に先行計算した analysis_path を再利用

        if _early_scene == "MOVIE":
            if handle_movie(img_path, state, dist, cur_phash):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (MOVIE_EARLY)", _fms)
                continue

        elif _early_scene == "BATTLE":
            _early_analysis = prepare_analysis_image(img_path, actual_w, actual_h)
            if handle_battle(_early_analysis, state, dist):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_EARLY)", _fms)
                continue
            _skip_rapid = True  # BATTLE ハンドラがフォールスルー → OCR へ直行

        elif _early_scene == "ADV":
            if handle_adv(img_path, state, dist, cur_phash, actual_w, actual_h):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (ADV_EARLY)", _fms)
                continue
            _skip_rapid = True  # ADV ハンドラがフォールスルー → OCR へ直行

        if screen_changed:
            # 画面変化あり → カウンタリセット & Watchdog タイマーリセット
            state.same_phash_count = 0
            state.consecutive_frozen_frames = 0
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.pre_popup_tap_count = 0  # ポップアップ試行カウンタもリセット
            state.dialog_close_total = 0  # ダイアログclose累計もリセット
            state.pending_candidates = []  # 候補リストクリア
            state.pending_candidate_idx = 0
            # 指スワイプ判定: MOYA_TAP 後の微小変化 (dist<PHASH_THRESHOLD) では
            # リセットしない。チェック柄シーンのアニメーション中に永久リセットされる問題を防止。
            if not (state.last_action == "MOYA_TAP" and dist < PHASH_THRESHOLD):
                state.finger_tap_static.reset()
            elif state.last_action == "MOYA_TAP":
                state.finger_tap_static.tick()  # 微小変化でもタップ静止としてカウント
            state.last_screen_change_time = time.time()  # Watchdog: 最終変化時刻更新

            # ── テレメトリ: アクション→画面変化の遷移時間を記録 ──
            if state.last_action_time > 0:
                _trans_sec = time.time() - state.last_action_time
                if len(state.transition_times) >= _TRANSITION_HISTORY_MAX:
                    state.transition_times.pop(0)
                state.transition_times.append(_trans_sec)
                if _trans_sec > _TRANSITION_SLOW_SEC:
                    logger.debug(
                        "[TELEMETRY] SLOW transition %.1fs after '%s'",
                        _trans_sec, state.last_action)

            # ── ADV 高速モード: OCR スキップして画面下部を即連打 ──
            # 前回 STORY_TAP かつ phash 変化が小さい（テキスト送り）→ 即タップ
            # MENU シーンはホームチュートリアル中の可能性があるため除外（指/金枠をOCRで確認）
            # Result 画面は ADV_RAPID 禁止 (キャラアニメの phash 変動で誤発動する)
            _last_texts = state.last_ocr_texts
            _is_result_like = any(
                any(k in t for k in ("Result", "EXP", "次へ"))
                for t in _last_texts
            )
            if (not _skip_rapid and
                    state.last_action in ("STORY_TAP", "ADV_RAPID_TAP", "ADV_NEXT_TAP", "ADV_WAIT",
                                      "ADV_NEXT_FALLBACK", "ADV_SKIP_TAP",
                                      "STORY_TAP_HINT", "BUBBLE_TAP",
                                      "MINI_CONV_TAP", "MOYA_TAP", "MOVIE_SKIP", "MOVIE_WAIT",
                                      "SCENE_TAP") and
                    PHASH_THRESHOLD <= dist <= ADV_RAPID_PHASH_MAX and
                    state.current_scene not in ("MENU", "BATTLE") and
                    not _is_result_like):
                # ── MOVIE_WAIT 脱出: 8回連続 (~24秒) 動画待機ならフルOCRへフォールスルー ──
                _MOVIE_WAIT_ESCAPE = 8
                if state.movie_wait_consecutive >= _MOVIE_WAIT_ESCAPE:
                    logger.warning(
                        "[MOVIE_ESCAPE] 動画待機 %d 回連続 → フルOCR解析にフォールスルー",
                        state.movie_wait_consecutive)
                    state.movie_wait_consecutive = 0
                    # continue しない → 下の OCR パスへ落ちる
                else:
                    # レターボックス判定: 左黒帯>=80px
                    _rapid_roi_x = state.game_roi[0] if state.game_roi else 0
                    _rapid_adv = detect_adv_scene_cached(img_path, state)
                    if _rapid_roi_x >= 80:
                        # ADVツールバー(AUTO/>>)があればADV_RAPIDへフォールスルー
                        if not _rapid_adv.is_adv:
                            state.movie_wait_consecutive += 1
                            logger.info("[iter %d] phash_dist=%d レターボックス動画 → 待機 (%d/%d)",
                                        i, dist, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                            state.last_action = "MOVIE_WAIT"
                            state.stall_start = 0.0  # ムービー待機中はスタックタイマー抑制
                            time.sleep(0.5)
                            state.last_phash = cur_phash
                            continue
                        # ADVツールバーあり → ADV_RAPID へフォールスルー
                    # ── ADV↓アイコン検出 (ツールバー判定に依存しない高速パス) ──
                    # ADVシーンではセリフが連続するため、↓が見える限りバーストタップ
                    _adv_tap_x = int(ANALYSIS_W * 0.93)
                    _adv_tap_y = int(ANALYSIS_H * 0.91)
                    if _rapid_adv.is_adv or detect_adv_advance_icon(img_path):
                        # ── ADVバーストタップ: ↓が見える限り連続タップ (OCR/phashスキップ) ──
                        _burst_count = 0
                        _burst_max = 3  # 安全上限
                        _burst_img = img_path
                        while _burst_count < _burst_max:
                            if detect_adv_advance_icon(_burst_img):
                                _burst_count += 1
                                logger.info("[ADV_BURST][iter %d] ↓検出 → タップ #%d (%d,%d)",
                                            i, _burst_count, _adv_tap_x, _adv_tap_y)
                                tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                                state.last_action = "ADV_RAPID_TAP"
                                # 次のスクリーンショット (ROI/phash省略で高速)
                                _b_path, _b_w, _b_h, _ = take_screenshot()
                                if _b_path is None:
                                    break
                                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
                                actual_w, actual_h = _b_w, _b_h
                            elif _rapid_adv.is_adv and _rapid_adv.next_btn_pos and _burst_count == 0:
                                # ↓アイコンHSV未検出だがADVツールバーあり+座標あり → 1回タップ
                                _adv_nx = int(_rapid_adv.next_btn_pos[0] * ANALYSIS_W / actual_w)
                                _adv_ny = int(_rapid_adv.next_btn_pos[1] * ANALYSIS_H / actual_h)
                                logger.info("[iter %d] ADV_RAPID → ↓ボタン座標 (%d,%d)", i, _adv_nx, _adv_ny)
                                tap_device(_adv_nx, _adv_ny, state, "ADV_RAPID_TAP")
                                state.last_action = "ADV_RAPID_TAP"
                                _burst_count += 1
                                _b_path, _b_w, _b_h, _ = take_screenshot()
                                if _b_path is None:
                                    break
                                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
                                actual_w, actual_h = _b_w, _b_h
                            else:
                                break
                        if _burst_count > 0:
                            logger.info("[ADV_BURST] 完了: %d タップ", _burst_count)
                        state.movie_wait_consecutive = 0
                        state.last_phash = ""  # バースト後はphashリセット
                        img_path = _burst_img  # 最新画像で次の判定へ
                        continue
                    # ── ミニ会話バーストタップ: 吹き出しが見える限り連続タップ ──
                    _mc = detect_mini_conversation(img_path)
                    if _mc is not None:
                        _burst_count = 0
                        _burst_max = 3
                        _burst_img = img_path
                        while _burst_count < _burst_max:
                            _mc_res = detect_mini_conversation(_burst_img)
                            if _mc_res is not None:
                                _mc_cx, _mc_cy, _mc_side = _mc_res
                                _burst_count += 1
                                logger.info("[MINI_CONV_BURST][iter %d] 吹き出し(%s) → タップ #%d (%d,%d)",
                                            i, _mc_side, _burst_count, _mc_cx, _mc_cy)
                                tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                                state.last_action = "MINI_CONV_TAP"
                                _b_path, _b_w, _b_h, _ = take_screenshot()
                                if _b_path is None:
                                    break
                                _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
                                actual_w, actual_h = _b_w, _b_h
                            else:
                                break
                        if _burst_count > 0:
                            logger.info("[MINI_CONV_BURST] 完了: %d タップ", _burst_count)
                        state.movie_wait_consecutive = 0
                        state.last_phash = ""
                        img_path = _burst_img
                        continue
                    # ツールバーなし + ↓なし + 吹き出しなし → >| ボタン有無で動画判定
                    _movie_btn = detect_movie_skip_button(img_path)
                    # >| 誤検知ガード: レターボックスなし + テキスト2件以上 → UI画面
                    # レターボックスありなら字幕2件でも動画 (UIキーワードで判定)
                    _rapid_roi_x = state.game_roi[0] if state.game_roi else 0
                    _rapid_letterbox = _rapid_roi_x >= 80
                    if _movie_btn and not _rapid_letterbox and len(state.last_ocr_texts) >= 2:
                        logger.info("[iter %d] >|検出だがOCR%d件+レターボックスなし → UI画面 → SCENE_TAP",
                                    i, len(state.last_ocr_texts))
                        _movie_btn = None  # 誤検知として取り消し
                    if _movie_btn:
                        state.movie_wait_consecutive += 1
                        logger.info("[iter %d] phash_dist=%d 動画検出(>|のみ) → 待機 (%d/%d)",
                                    i, dist, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                        state.last_action = "MOVIE_WAIT"
                        state.last_phash = cur_phash
                        continue
                    # 金色⏭なし + ツールバーなし + ↓なし + 吹き出しなし
                    # → 動画ではない静止画面 (ガチャ演出等) → 画面タップで進む
                    _st_x = int(ANALYSIS_W * 0.5)
                    _st_y = int(ANALYSIS_H * 0.5)
                    logger.info("[iter %d] phash_dist=%d 非動画静止画面 → SCENE_TAP (%d,%d)",
                                i, dist, _st_x, _st_y)
                    tap_device(_st_x, _st_y, state, "SCENE_TAP")
                    state.last_action = "SCENE_TAP"
                    state.movie_wait_consecutive = 0
                    state.last_phash = cur_phash
                    continue

        else:
            # 画面変化なし
            state.same_phash_count += 1
            state.total_ocr_skipped += 1
            # MOYA_TAP 連続静止カウンタ: 指タップしても画面が変わらない回数を追跡
            if state.last_action == "MOYA_TAP":
                state.finger_tap_static.tick()
            # 完全凍結 (dist=0) のみカウント
            if dist == 0:
                state.consecutive_frozen_frames += 1
            else:
                state.consecutive_frozen_frames = 0  # わずかな変化でもリセット

            # ── 階層型 Watchdog 第2段階: 5フレーム凍結 → 物理診断 ──
            if state.consecutive_frozen_frames == 5:
                logger.info(
                    "[WATCHDOG] Suspect freeze (%d consecutive dist=0). "
                    "Running physical diagnostics...",
                    state.consecutive_frozen_frames,
                )
                if not check_adb_liveness():
                    # 第3段階: 物理診断失敗 → kill-server + 再接続
                    logger.warning("[WATCHDOG] Physical diagnostics FAILED → adb kill-server + reconnect")
                    subprocess.run(["adb", "kill-server"], timeout=5)
                    time.sleep(2)
                    subprocess.run(["adb", "start-server"], timeout=5)
                    time.sleep(2)
                    if _ap_device.DEVICE_SERIAL:
                        subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5)
                        time.sleep(1)
                    # scrcpy が ADB 再起動で死んだ場合は再起動
                    if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
                        logger.info("[SCRCPY] WATCHDOG ADB reconnect 後に再起動")
                        _scrcpy_proc = manage_scrcpy()
                    state.consecutive_frozen_frames = 0
                    state.last_phash = ""  # 次ループで強制再取得
                else:
                    logger.info("[WATCHDOG] Physical diagnostics OK — game screen is static")

            # ── Watchdog チェック: 10分以上変化なし → 本当のデッドロック ──
            watchdog_elapsed = time.time() - state.last_screen_change_time
            if watchdog_elapsed >= WATCHDOG_DEADLOCK_THRESHOLD:
                if state.last_action in WATCHDOG_EXEMPT_ACTIONS:
                    # 免除シーン: まだ待機中なのでWatchdogを発動しない
                    if int(watchdog_elapsed) % 60 == 0:  # 1分ごとにログ
                        logger.info(
                            "[WATCHDOG] 免除: %.0f秒経過 (last_action=%s) — 引き続き待機",
                            watchdog_elapsed, state.last_action
                        )
                else:
                    logger.warning(
                        "[WATCHDOG] デッドロック疑い: %.0f秒間画面変化なし / last_action=%s "
                        "/ iter=%d → 自動復旧開始",
                        watchdog_elapsed, state.last_action, state.iteration
                    )
                    save_evidence(img_path, [], "WATCHDOG_DEADLOCK", state)
                    if not watchdog_recover(state):
                        generate_and_copy_report(
                            state, f"Watchdog復旧不能 (elapsed={watchdog_elapsed:.0f}秒, count={state.watchdog_recovery_count})"
                        )
                        return  # 復旧不能 → 終了
                    continue   # 復旧後は次のイテレーションから

            # ── 候補リトライ: 残り候補があれば OCR なしで次の候補をタップ ──
            if (state.pending_candidates
                    and state.pending_candidate_idx < len(state.pending_candidates)):
                _cand = state.pending_candidates[state.pending_candidate_idx]
                state.pending_candidate_idx += 1
                logger.info("[CANDIDATE_RETRY] #%d/%d: %s (%d,%d) — %s",
                            state.pending_candidate_idx, len(state.pending_candidates),
                            _cand.action, _cand.x, _cand.y, _cand.desc)
                tap_device(_cand.x, _cand.y, state, _cand.desc or _cand.action)
                state.last_action = _cand.action
                state.same_phash_count = 0
                state.last_phash = cur_phash
                continue

            # N 回変化なし → 強制 OCR (デッドロック防止の核心)
            if state.same_phash_count >= FORCE_ANALYZE_AFTER:
                logger.info("[iter %d] phash_dist=%d same=%d → 強制 OCR",
                            i, dist, state.same_phash_count)
                screen_changed = True  # OCR ブロックへ進む

            else:
                # まだ待機フェーズ — シーン別インターバル
                _poll = SCENE_INTERVAL.get(state.current_scene, POLL_INTERVAL)
                if i % 3 == 0:
                    logger.info("[%s][iter %d] phash_dist=%d same=%d — polling (%.1fs)...",
                                state.current_scene, i, dist, state.same_phash_count, _poll)
                # ── ADV送り待ちアイコン検知: phash 安定中でもバーストタップ ──
                _adv_tap_x = int(ANALYSIS_W * 0.93)
                _adv_tap_y = int(ANALYSIS_H * 0.91)
                if detect_adv_advance_icon(img_path):
                    _burst_count = 0
                    _burst_max = 3
                    _burst_img = img_path
                    while _burst_count < _burst_max:
                        if detect_adv_advance_icon(_burst_img):
                            _burst_count += 1
                            logger.info("[ADV_BURST][iter %d] ↓検出 → タップ #%d", i, _burst_count)
                            tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                            state.last_action = "ADV_RAPID_TAP"
                            _b_path, _b_w, _b_h, _ = take_screenshot()
                            if _b_path is None:
                                break
                            _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
                            actual_w, actual_h = _b_w, _b_h
                        else:
                            break
                    if _burst_count > 0:
                        logger.info("[ADV_BURST] 完了: %d タップ", _burst_count)
                    state.last_phash = ""
                    state.same_phash_count = 0
                    state.stall_start = 0.0
                    img_path = _burst_img
                    continue
                # ── ミニ会話バーストタップ (phash安定時) ──
                _mc = detect_mini_conversation(img_path)
                if _mc is not None:
                    _burst_count = 0
                    _burst_max = 3
                    _burst_img = img_path
                    while _burst_count < _burst_max:
                        _mc_res = detect_mini_conversation(_burst_img)
                        if _mc_res is not None:
                            _mc_cx, _mc_cy, _mc_side = _mc_res
                            _burst_count += 1
                            logger.info("[MINI_CONV_BURST][iter %d] 吹き出し(%s) → タップ #%d (%d,%d)",
                                        i, _mc_side, _burst_count, _mc_cx, _mc_cy)
                            tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                            state.last_action = "MINI_CONV_TAP"
                            _b_path, _b_w, _b_h, _ = take_screenshot()
                            if _b_path is None:
                                break
                            _burst_img = prepare_analysis_image(_b_path, _b_w, _b_h)
                            actual_w, actual_h = _b_w, _b_h
                        else:
                            break
                    if _burst_count > 0:
                        logger.info("[MINI_CONV_BURST] 完了: %d タップ", _burst_count)
                    state.last_phash = ""
                    state.same_phash_count = 0
                    state.stall_start = 0.0
                    img_path = _burst_img
                    continue
                # 動画シーンでは ADV ツールバーが無いためタップ抑制
                if state.current_scene in ("STORY", "ADV", "UNKNOWN"):
                    _aa_adv = detect_adv_scene_cached(img_path, state)
                    if _aa_adv.is_adv:
                        if _aa_adv.next_btn_pos:
                            logger.info("[ADV_ADVANCE][iter %d] ADVツールバー検出 → ↓ボタンタップ", i)
                            _aa_x = int(_aa_adv.next_btn_pos[0] * ANALYSIS_W / actual_w)
                            _aa_y = int(_aa_adv.next_btn_pos[1] * ANALYSIS_H / actual_h)
                            tap_device(_aa_x, _aa_y, state, "ADV_ADVANCE")
                            state.last_phash = ""
                            state.same_phash_count = 0
                            state.stall_start = 0.0
                            continue
                    else:
                        # ツールバーなし → >| ボタン有無で動画判定
                        _movie_btn = detect_movie_skip_button(img_path)
                        _stable_roi_x = state.game_roi[0] if state.game_roi else 0
                        _stable_letterbox = _stable_roi_x >= 80
                        if _movie_btn and not _stable_letterbox and len(state.last_ocr_texts) >= 2:
                            logger.info("[iter %d] >|検出だがOCR%d件+レターボックスなし → UI画面 → SCENE_TAP",
                                        i, len(state.last_ocr_texts))
                            _movie_btn = None
                        if _movie_btn:
                            logger.info("[MOVIE_WAIT] 動画検出(>|のみ) → 待機 (phash stable)")
                            state.last_action = "MOVIE_WAIT"
                            state.stall_start = 0.0  # ムービー待機中はスタックタイマー抑制
                            continue
                        # 金色⏭なし → 動画ではない静止画面 → タップで進む
                        _st_x = int(ANALYSIS_W * 0.5)
                        _st_y = int(ANALYSIS_H * 0.5)
                        logger.info("[iter %d] 静止画面(非動画) → SCENE_TAP (%d,%d)", i, _st_x, _st_y)
                        tap_device(_st_x, _st_y, state, "SCENE_TAP")
                        state.last_action = "SCENE_TAP"
                        state.last_phash = cur_phash
                        continue
                state.last_phash = cur_phash
                time.sleep(_poll)
                continue

            # ── スタック介入 (強制OCRでもタップできず続いた場合) ──
            if state.stall_start == 0.0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                # ADVシーン中は右上タップ禁止 (ツールバー >| スキップを押してしまうため)
                _stall_is_adv = is_adv_toolbar_cached(img_path, state) if img_path else False
                if _stall_is_adv:
                    logger.info(">>> %.0f秒スタック — ADVシーン → 右上×スキップ (セリフ送り代用)",
                                stall_elapsed)
                    # ADVでは画面中央下部をタップしてセリフ送りを試みる
                    _sc_x, _sc_y = roi_to_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.85), state.game_roi)
                    tap_device(_sc_x, _sc_y, state, "STALL_ADV_TAP")
                else:
                    logger.warning(">>> %.0f秒スタック — 右上×ボタン試行", stall_elapsed)
                    save_evidence(img_path, [], "STALL_CORNER", state)
                    _sc_x, _sc_y = roi_to_device(int(ANALYSIS_W * 0.97), int(ANALYSIS_H * 0.06), state.game_roi)
                    tap_device(_sc_x, _sc_y, state, "STALL_CORNER")
                state.stall_corner_tried = True
                state.last_phash = ""
                state.same_phash_count = 0
                continue

            if stall_elapsed >= STALL_TIMEOUT * 4 and state.stall_corner_tried:
                _restart_count = state.unity_restart_count
                if _restart_count >= 3:
                    logger.error(">>> %.0f秒スタック解消不能 (再起動%d回失敗) — 停止",
                                 stall_elapsed, _restart_count)
                    save_evidence(img_path, [], "STALL_FATAL", state)
                    generate_and_copy_report(state, f"スタック解消不能 (restart={_restart_count})")
                    return
                # ── Unity 入力フリーズ自動復帰 ──
                logger.warning(">>> %.0f秒スタック — Unity入力フリーズ疑い → ゲーム再起動 (試行%d)",
                               stall_elapsed, _restart_count + 1)
                save_evidence(img_path, [], "UNITY_FREEZE_RESTART", state)
                try:
                    subprocess.run(["adb", "-s", _ap_device.DEVICE_SERIAL, "shell", "am", "force-stop",
                                    APP_PACKAGE], timeout=5)
                    time.sleep(3)
                    subprocess.run(["adb", "-s", _ap_device.DEVICE_SERIAL, "shell", "am", "start", "-n",
                                    f"{APP_PACKAGE}/{APP_ACTIVITY}"],
                                   timeout=5)
                    logger.info("[UNITY_RESTART] ゲーム再起動完了 — 30秒待機")
                    time.sleep(30)
                except Exception as _uf_e:
                    logger.error("[UNITY_RESTART] 再起動失敗: %s", _uf_e)
                state.unity_restart_count = _restart_count + 1
                state.stall_start = 0.0
                state.stall_corner_tried = False
                state.same_phash_count = 0
                state.last_phash = ""
                # force-stop でタイトル画面に戻るため、進行フラグをリセット
                state.home_reached = False
                state.auto_activated = False
                state.character_selected = False
                state.char_just_selected = False
                state.battle_wait_count = 0
                continue

        # ── 4) 解析用画像の準備 ──
        state.last_phash = cur_phash
        analysis_path = _early_analysis or prepare_analysis_image(img_path, actual_w, actual_h)

        # ── 4.2) Result画面ハンドラ (RAPID mode) ──
        _result_action = handle_result_screen(
            state, analysis_path, [], dist, mode="RAPID")
        if _result_action:
            if _result_action[0] == "RESULT_FREEZE":
                # watchdog_recover は handle_result_screen 内で実行済み
                continue
            state.last_action = _result_action[0]
            state.stall_start = 0.0
            state.same_phash_count = 0
            _fms = (time.time() - _loop_t0) * 1000
            state.total_loop_ms += _fms
            logger.info("  [PERF] Loop %.0fms (%s) [%d/15]",
                        _fms, _result_action[0], state.result_rapid_count)
            continue
        # RESULT状態リセット (Result画面を脱出した時)
        if state.last_action not in ("RESULT_TAP", "RESULT_NEXT",
                                     "RESULT_RAPID", "GACHA_OK"):
            state.result_rapid_count = 0
            state.result_total_taps = 0

        # ── 4.3) BATTLE_RAPID: 発光/MOYA 検知即タップ → OCR 完全スキップ ──
        # detect_guide_glow() + find_finger_blobs() は OpenCV のみ (10-50ms)
        # OCR (6-8s) の 40-50 倍高速
        # ※ 強制 OCR (phash 静止 → ダイアログ可能性) 時は RAPID をスキップして OCR に回す
        #
        # 【永続バトルルール】
        #   左側キャラにモヤ（選択待ち発光）がある → キャラをタップ
        #   左側キャラにモヤがない → 右側の通常攻撃 or 戦闘スキルをタップ
        #   キャラ肖像の王冠/ロール装飾 (area<5000) は偽モヤ → 無視
        _force_ocr_override = (dist <= 2 and state.same_phash_count >= FORCE_ANALYZE_AFTER)
        # 速度チュートリアル表示中は BATTLE_RAPID をスキップして OCR で処理
        _last_texts_br = state.last_ocr_texts
        _speed_tip_in_last = any(
            any(k in t for k in ("このボタンでバトル", "進行速度を変更"))
            for t in _last_texts_br
        )
        if _speed_tip_in_last:
            _force_ocr_override = True
        # BATTLE_RAPID 連続ループ上限: 50回 (~4分) で OCR 再評価を強制
        # ムービーシーン等をバトルと誤分類し続ける問題を防止
        if state.battle_rapid_consecutive.stalled:
            logger.info("[BATTLE_RAPID] 連続 %d 回 → OCR で再評価 (シーン誤分類の可能性)",
                        state.battle_rapid_consecutive.count)
            state.battle_rapid_consecutive.reset()
            _force_ocr_override = True
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override and not _skip_rapid):
            _rapid_tx = _rapid_ty = 0
            _rapid_action = ""
            _rapid_double = False

            # ── 共通: 指ブロブ検出 (Phase 0 / Phase B で共用) ──
            _rapid_blobs = find_finger_blobs(analysis_path, min_area=200, dark_mode=True)
            _rapid_blobs = [b for b in _rapid_blobs
                            if b[1] > _SPATIAL_MARGIN_TOP and b[0] < ANALYSIS_W - _CLOSE_BTN_OFFSET]

            # ── Phase 0: チュートリアル金枠+指 (area>10000 かつ金枠ボタン検出) → 最優先タップ ──
            _rapid_tutorial_gold = [b for b in _rapid_blobs if b[2] > 10000]
            if _rapid_tutorial_gold:
                _gold_tap = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False)
                if _gold_tap:
                    _rapid_tx, _rapid_ty = _gold_tap
                    _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"

            # ── Phase A: アクティブキャラ検出 (赤/ピンク発光ハロー) ──
            # 【永続ルール】キャラ選択モヤ = 赤/ピンクの発光。明度差で識別。
            _active_char = detect_active_battle_char(analysis_path, ANALYSIS_W, ANALYSIS_H)

            if not _rapid_action and not state.character_selected and _active_char is not None:
                _rapid_tx, _rapid_ty = _active_char[0], _active_char[1]
                _rapid_action = "BATTLE_RAPID_ACTIVE_P1"
                _rapid_double = True

            # ── Phase B: 右側スキル/攻撃ボタン ──
            if not _rapid_action:
                _rapid_glows = detect_guide_glow(
                    analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
                _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]

                _right_panel = [b for b in _rapid_blobs
                                if b[0] > _RIGHT_PANEL_X and b[1] > ANALYSIS_H * 0.45]

                if state.character_selected or state.char_just_selected:
                    # キャラ選択済み → 右スキル優先
                    if _rapid_right_g:
                        _rr = max(_rapid_right_g, key=lambda g: g["area"])
                        # bbox上端 + 高さ1/3 = ボタン視覚中心
                        _rapid_tx = _rr["cx"]
                        _rapid_ty = max(1, _rr["by"] + _rr["bh"] // 3)
                        _rapid_action = "BATTLE_RAPID_GLOW_P2"
                    elif _right_panel:
                        _tb = max(_right_panel, key=lambda b: b[2])
                        _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                        _rapid_action = "BATTLE_RAPID_MOYA_P2"
                    else:
                        _rapid_tx, _rapid_ty = roi_to_device(
                            int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                        _rapid_action = "BATTLE_RAPID_NORMATK_P2"

            # ── Phase C: 左モヤなしフォールバック → 右側攻撃ボタン ──
            # 【永続ルール】左キャラにモヤがない場合は常に右側の通常攻撃/戦闘スキルをタップ
            # 安全弁: 連続10回フォールバック → バトル以外のシーンの可能性 → OCR 再評価
            if not _rapid_action:
                if state.normatk_fallback.stalled:
                    logger.info("[BATTLE_RAPID] FALLBACK %d回連続 → OCR で再評価",
                                state.normatk_fallback.count)
                    state.normatk_fallback.reset()
                    # BATTLE_RAPID を抜けて OCR に回す (continue しない)
                else:
                    _rapid_tx, _rapid_ty = roi_to_device(
                        int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                    _rapid_action = "BATTLE_RAPID_NORMATK_FALLBACK"
                    state.normatk_fallback.tick()
            else:
                state.normatk_fallback.reset()

            # ── 共通タップ実行 ──
            if _rapid_action:
                logger.info("[%s] tap(%d,%d)%s",
                            _rapid_action, _rapid_tx, _rapid_ty,
                            " ダブルタップ" if _rapid_double else "")
                tap_device(_rapid_tx, _rapid_ty, state, _rapid_action,
                          rapid=_rapid_double)
                if _rapid_double:
                    tap_device(_rapid_tx, _rapid_ty, state, _rapid_action)
                # 状態更新
                if "P1" in _rapid_action:
                    state.character_selected = True
                    state.char_just_selected = True
                else:
                    state.character_selected = False
                    state.char_just_selected = False
                state.finger_detections += 1
                state.last_action = _rapid_action
                state.stall_start = 0.0
                state.stall_corner_tried = False
                state.same_phash_count = 0
                state.battle_rapid_consecutive.tick()
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_RAPID)", _fms)
                continue  # OCR スキップ

        # BATTLE_RAPID を通過 → カウンタリセット
        state.battle_rapid_consecutive.reset()

        # ── 4.5) BATTLE 高速パス: OCR 前テンプレートマッチング ──
        # BATTLE シーンで GoldBtn/GoldSwipe が見つかれば OCR (6-8s) をスキップ
        # ※ 強制 OCR 時はスキップ (ダイアログ検出を優先)
        if state.current_scene == "BATTLE" and not _force_ocr_override and not _skip_rapid:
            _fast_action, _fast_wait = _battle_fast_check(analysis_path, state)
            if _fast_action:
                # GoldSwipe 連続回数制限: 6回超えたら OCR へフォールバック
                if "GOLD_SWIPE" in _fast_action:
                    state.gold_swipe.tick()
                    if state.gold_swipe.stalled:
                        logger.warning(
                            "[GoldSwipe] 連続 %d 回 → OCR フォールバック (ループ脱出)",
                            state.gold_swipe.count,
                        )
                        state.gold_swipe.reset()
                        # FAST_PATH をスキップして通常 OCR へ
                    else:
                        state.last_action = _fast_action
                        state.stall_start = 0.0
                        state.stall_corner_tried = False
                        state.same_phash_count = 0
                        if _fast_wait > 0:
                            logger.info("  [FAST][%s] wait %.1fs (OCR skip)", _fast_action, _fast_wait)
                            time.sleep(_fast_wait)
                        _fms = (time.time() - _loop_t0) * 1000
                        state.total_loop_ms += _fms
                        logger.info("  [PERF] Loop %.0fms (FAST_PATH)", _fms)
                        continue  # OCR スキップ
                else:
                    state.gold_swipe.reset()  # GoldSwipe 以外でカウンタリセット
                    state.last_action = _fast_action
                    state.stall_start = 0.0
                    state.stall_corner_tried = False
                    state.same_phash_count = 0
                    if _fast_wait > 0:
                        logger.info("  [FAST][%s] wait %.1fs (OCR skip)", _fast_action, _fast_wait)
                        time.sleep(_fast_wait)
                    _fms = (time.time() - _loop_t0) * 1000
                    state.total_loop_ms += _fms
                    logger.info("  [PERF] Loop %.0fms (FAST_PATH)", _fms)
                    continue  # OCR スキップ

        # ── 5) OCR 精査 ──
        state.total_ocr_calls += 1
        try:
            ocr_results = run_ocr(str(analysis_path), lang=OCR_LANG,
                                  min_confidence=OCR_MIN_CONF)
        except Exception as e:
            logger.error("OCR failed: %s", e)
            state.last_phash = ""  # 次回も確実に解析
            time.sleep(0.3)
            continue

        texts = all_texts(ocr_results)

        # ── ロック画面検出: "緊急通報のみ" = デバイスがスリープ → 復帰 ──
        _ocr_text_joined = " ".join(texts) if texts else ""
        if "緊急通報" in _ocr_text_joined or "通報のみ" in _ocr_text_joined:
            logger.warning("[LOCK_SCREEN] ロック画面検出 → WAKEUP + UNLOCK")
            adb("shell input keyevent KEYCODE_WAKEUP")
            time.sleep(1)
            adb("shell input keyevent 82")  # KEYCODE_MENU = swipe unlock
            time.sleep(2)
            adb(f"shell am start -n {APP_PACKAGE}/{APP_ACTIVITY}")
            time.sleep(5)
            state.last_phash = ""
            continue

        # ── ADVシーン検出 (キャッシュ付き) ──
        _adv_result = detect_adv_scene_cached(
            analysis_path or img_path, state, ocr_items=ocr_results)

        # ── シーン分類 ──
        scene, next_interval = classify_scene(
            texts, state.last_action, adv_detected=_adv_result.is_adv)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── 動画シーン検出: detect_and_act 前にガード ──
        # 動画中にタップするとUIが一時停止/再生を繰り返すため抑制する
        # 検出条件:
        #   A) レターボックス (左黒帯>=80px) + ADVツールバーなし
        #   B) ⏭スキップボタン検出 + ADVツールバーなし (レターボックス不問)
        # ただし OCR で UI テキストが豊富な場合は動画ではない (利用規約画面等)
        _roi_x = state.game_roi[0] if state.game_roi else 0
        _is_movie_letterbox = _roi_x >= 80
        _has_ui_kw = any(kw in _ocr_text_joined for kw in _UI_TEXT_KWS)
        # レターボックスあり: 字幕2件程度は動画。UIキーワードのみで判定。
        # レターボックスなし: テキスト2件以上ならUI画面の可能性が高い。
        _has_ui_text = _has_ui_kw if _is_movie_letterbox else (_has_ui_kw or len(texts) >= 2)
        # ⏭ ボタン検出を先に実行 (レターボックスなし+OCR多めでも動画を検出するため)
        _movie_btn = detect_movie_skip_button(analysis_path) if analysis_path else None
        # D: ADVアイコン安全弁 — menu/log/ff のどれか1個でもマッチすれば
        # >| は ADV ツールバーの一部であり動画⏭ではないと判断
        if _movie_btn and not _adv_result.is_adv and analysis_path:
            from tools.ap.image_proc import ASSET_MANAGER as _AM
            _adv_icon_check = any(
                _AM.match_single(n, analysis_path) is not None
                for n in ("adv_icon_menu", "adv_icon_log", "adv_icon_ff")
            )
            if _adv_icon_check:
                logger.info("[MOVIE_GUARD] >|検出だがADVアイコンも検出 → 動画ではなくADV")
                _movie_btn = None  # 動画⏭判定を取り消し
        _movie_candidate = (
            _is_movie_letterbox
            or _movie_btn is not None
            or (len(texts) <= 3 and scene not in ("BATTLE", "MENU"))
        )
        if _movie_candidate and not _has_ui_text and scene not in ("BATTLE", "MENU") and analysis_path:
            if not _adv_result.is_adv:
                # ツールバーなし → レターボックス or >| ボタン検出で動画判定
                if _is_movie_letterbox or _movie_btn:
                    # ダウンロード直後のみ動画SKIP許可 (通常ストーリー動画は視聴)
                    if state.post_download:
                        _skip_item = next(
                            (item for item in ocr_results
                             if "SKIP" in item.get("text", "").upper()
                             or item.get("text", "").upper() == "SK"),
                            None)
                        if _skip_item:
                            _sk_x, _sk_y = _skip_item["center"]
                            _sk_x, _sk_y = roi_to_device(_sk_x, _sk_y, state.game_roi)
                            logger.info(
                                "[MOVIE_SKIP_OCR] DL直後動画SKIP '%s' → タップ (%d,%d)",
                                _skip_item["text"], _sk_x, _sk_y)
                            tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP_OCR")
                            state.last_action = "MOVIE_SKIP"
                            state.movie_wait_consecutive = 0
                            state.last_phash = ""
                            continue
                    state.movie_wait_consecutive += 1
                    _MOVIE_WAIT_ESCAPE = 8
                    if state.movie_wait_consecutive >= _MOVIE_WAIT_ESCAPE:
                        if state.post_download:
                            # DL直後 → SKIP想定位置 (右上) をタップ
                            logger.warning(
                                "[MOVIE_GUARD_ESCAPE] DL直後+動画待機 %d 回 → SKIPタップ",
                                state.movie_wait_consecutive)
                            state.movie_wait_consecutive = 0
                            _resume_x, _resume_y = roi_to_device(
                                int(ANALYSIS_W * 0.93), int(ANALYSIS_H * 0.06), state.game_roi)
                            tap_device(_resume_x, _resume_y, state, "MOVIE_SKIP_ESCAPE")
                            state.last_action = "MOVIE_SKIP"
                        else:
                            # 通常動画 → 画面中央タップで再開試行
                            logger.warning(
                                "[MOVIE_GUARD_ESCAPE] 動画待機 %d 回 → 画面中央タップ",
                                state.movie_wait_consecutive)
                            state.movie_wait_consecutive = 0
                            _resume_x, _resume_y = roi_to_device(
                                int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state.game_roi)
                            tap_device(_resume_x, _resume_y, state, "MOVIE_RESUME_TAP")
                            state.last_action = "SCENE_TAP"
                        state.last_phash = ""
                        state.same_phash_count = 0
                        continue
                    _reason = f"letterbox L={_roi_x}" if _is_movie_letterbox else ">|ボタン検出"
                    logger.info(
                        "[MOVIE_GUARD] %s+ツールバーなし → 待機 (%d/%d)",
                        _reason, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                    state.last_action = "MOVIE_WAIT"
                    state.stall_start = 0.0  # ムービー待機中はスタックタイマー抑制
                    time.sleep(0.5)
                    state.last_phash = cur_phash
                    continue
                # レターボックスなし + >|なし → 動画ではない → detect_and_act へ

        # ── 6) 判定 & アクション (finger blob も渡す) ──
        action, wait_sec = detect_and_act(ocr_results, state, analysis_path)
        state.last_action = action
        # 副作用アクション以外なら代替候補を収集
        if action not in _IMMEDIATE_ACTIONS:
            state.pending_candidates = collect_secondary_candidates(
                ocr_results, state, analysis_path, primary_action=action)
            state.pending_candidate_idx = 0
            if state.pending_candidates:
                logger.info("[CANDIDATES] %d 個の代替候補を収集: %s",
                            len(state.pending_candidates),
                            [(c.action, c.x, c.y) for c in state.pending_candidates])
        else:
            state.pending_candidates = []
            state.pending_candidate_idx = 0
        # フルOCR解析に到達 → MOVIE_WAIT脱出カウンタリセット
        if action != "MOVIE_WAIT":
            state.movie_wait_consecutive = 0

        # ── 7) フルOCR後バースト再突入: ↓アイコン/吹き出しが残っていれば即連打 ──
        # バトル/メニュー画面ではバーストしない (ボタン装飾の誤検出防止)
        # ADVシーン(↓検出済み)ではミニ会話をスキップ (ツールバー誤検出防止)
        _post_burst_img = analysis_path or img_path
        _post_burst_count = 0
        _post_burst_max = 3
        _post_adv_x = int(ANALYSIS_W * 0.93)
        _post_adv_y = int(ANALYSIS_H * 0.91)
        _post_is_adv = _adv_result.is_adv  # ADVシーンならミニ会話をスキップ
        _skip_burst = scene in ("BATTLE", "MENU") or action in (
            "DOWNLOAD_WAIT", "LOADING_WAIT", "MOVIE_WAIT", "MAIN_STORY_LOADING")
        while _post_burst_count < _post_burst_max and not _skip_burst:
            # ADV ↓アイコン
            if detect_adv_advance_icon(_post_burst_img):
                _post_burst_count += 1
                _post_is_adv = True  # ↓検出 = ADVシーン確定
                logger.info("[POST_OCR_BURST] ↓アイコン → タップ #%d", _post_burst_count)
                tap_device(_post_adv_x, _post_adv_y, state, "ADV_ADVANCE_TAP")
                state.last_action = "ADV_RAPID_TAP"
            elif not _post_is_adv:
                # ADVシーンでない場合のみミニ会話吹き出しを検出
                _post_mc = detect_mini_conversation(_post_burst_img)
                if _post_mc is not None:
                    _post_burst_count += 1
                    _pm_cx, _pm_cy, _pm_side = _post_mc
                    logger.info("[POST_OCR_BURST] 吹き出し(%s) → タップ #%d (%d,%d)",
                                _pm_side, _post_burst_count, _pm_cx, _pm_cy)
                    tap_device(_pm_cx, _pm_cy, state, "MINI_CONV_TAP")
                    state.last_action = "MINI_CONV_TAP"
                else:
                    break
            else:
                break
            _pb_path, _pb_w, _pb_h, _ = take_screenshot()
            if _pb_path is None:
                break
            _post_burst_img = prepare_analysis_image(_pb_path, _pb_w, _pb_h)
            actual_w, actual_h = _pb_w, _pb_h
        if _post_burst_count > 0:
            logger.info("[POST_OCR_BURST] 完了: %d タップ", _post_burst_count)
            state.last_phash = ""
            img_path = _post_burst_img
            continue

        # ── シーン再評価: 同一アクション連続時にシーン認識を疑う ──
        if action == state.last_action and action not in (
            "WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT",
            "MOVIE_WAIT", "LOADING_WAIT", "ADV_WAIT",
        ):
            state.action_repeat_count += 1
        else:
            state.action_repeat_count = 0
            state.scene_reeval_mode = False

        if state.action_repeat_count >= _SCENE_REEVAL_THRESHOLD:
            logger.warning(
                "[SCENE_REEVAL] '%s' が %d 回連続 → シーン再評価 (ガード緩和)",
                action, state.action_repeat_count,
            )
            state.scene_reeval_mode = True
            # 新しいスクリーンショットでフル再判定 (phash 変化なければ OCR 再利用)
            try:
                _re_img, _re_w, _re_h, _ = take_screenshot()
                _re_analysis = prepare_analysis_image(_re_img, _re_w, _re_h)
                # phash チェック: 画面が変わっていなければ既存 OCR を再利用
                try:
                    _re_phash = compute_phash(_re_analysis)
                    _re_dist = phash_distance(state.last_phash, _re_phash) if state.last_phash and _re_phash else 999
                except Exception:
                    _re_dist = 999
                if _re_dist < 3 and ocr_results:
                    logger.info("[SCENE_REEVAL] phash_dist=%d < 3 → 既存OCR再利用 (OCRスキップ)", _re_dist)
                    _re_ocr = ocr_results
                else:
                    _re_ocr = run_ocr(str(_re_analysis), lang=OCR_LANG,
                                      min_confidence=OCR_MIN_CONF)
                _re_texts = all_texts(_re_ocr)
                _re_adv = detect_adv_scene(_re_analysis, ocr_items=_re_ocr,
                                            roi=state.game_roi)
                _re_scene, _ = classify_scene(_re_texts, action,
                                              adv_detected=_re_adv.is_adv)
                if _re_scene != state.current_scene:
                    logger.warning(
                        "[SCENE_REEVAL] シーン不一致: %s → %s → 切替+再判定",
                        state.current_scene, _re_scene,
                    )
                    state.current_scene = _re_scene
                # レターボックスガード (動画シーンでのdetect_and_actバイパス)
                _re_roi_x = state.game_roi[0] if state.game_roi else 0
                if _re_roi_x >= 80 and _re_scene not in ("BATTLE", "MENU"):
                    if not _re_adv.is_adv:
                        logger.info("[SCENE_REEVAL] レターボックス動画 → MOVIE_WAIT")
                        state.last_action = "MOVIE_WAIT"
                        state.action_repeat_count = 0
                        state.scene_reeval_mode = False
                        time.sleep(0.5)
                        continue
                action, wait_sec = detect_and_act(_re_ocr, state, _re_analysis)
                state.last_action = action
                state.action_repeat_count = 0
                logger.info("[SCENE_REEVAL] 再判定結果: %s", action)
            except Exception as _re_err:
                logger.debug("[SCENE_REEVAL] 再評価例外: %s", _re_err)
            state.scene_reeval_mode = False

        # タップ成功時: スタックカウンタリセット
        if action not in ("WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT"):
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.same_phash_count = 0

        # ── ダウンロード中フラグ管理 ──
        if action == "DOWNLOAD_WAIT":
            state.post_download = True
        elif action in ("HOME_REACHED", "GRIND_COMPLETE"):
            state.post_download = False

        # ── ダウンロード進捗ログ (30秒ごとに生存確認) ──
        if action == "DOWNLOAD_WAIT":
            _now = time.time()
            if _now - state.last_download_progress_log >= 30.0:
                _prog = [t for t in texts if "%" in t or "MB" in t or "GB" in t]
                logger.info(
                    "[DOWNLOAD_PROGRESS] 進捗数値: %s | OCR全体: %s",
                    _prog if _prog else "(数値なし)", texts[:6],
                )
                state.last_download_progress_log = _now

        # エビデンス保存
        if i % 20 == 0 or action in ("HOME_REACHED", "GRIND_COMPLETE", "GRIND_QUEST_NAV",
                                      "SKIP", "AGREE", "RESULT_TAP"):
            save_evidence(img_path, ocr_results, action, state)

        # ── 7) ホーム到達 / 周回完了チェック ──
        if action in ("HOME_REACHED", "GRIND_COMPLETE"):
            if action == "GRIND_COMPLETE":
                _reason = f"周回完了 ({state.grind_cycles_completed}/{state.grind_max_cycles}周)"
            else:
                _reason = "ホーム画面到達 (チュートリアル完了)"
            logger.info("=" * 62)
            logger.info("  %s", _reason)
            logger.info("  総タップ: %d  イテレーション: %d  周回: %d",
                        state.total_taps, i + 1, state.grind_cycles_completed)
            logger.info("  OCR実行: %d  スキップ: %d  暗転: %d",
                        state.total_ocr_calls, state.total_ocr_skipped,
                        state.total_blackout_skipped)
            logger.info("=" * 62)
            save_evidence(img_path, ocr_results, "FINAL_HOME", state)
            if _scrcpy_proc and _scrcpy_proc.poll() is None:
                _scrcpy_proc.terminate()
                logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)
            generate_and_copy_report(state, _reason)
            return

        # ── 8) 待機 ──
        # 短い wait (< 5.0s) はスキップ: MIN_TAP_INTERVAL + phash ポーリングが制御
        # 長い wait (>= 5.0s) のみ実施: ダウンロード/メンテ/ロード等
        if wait_sec >= 5.0:
            if action == "DOWNLOAD_WAIT":
                # 適応ポーリング: 3秒ごとに phash チェック → 画面変化で早期脱出
                _dl_remaining = wait_sec
                _DL_POLL = 3.0
                logger.info("  [%s][%s] adaptive wait %.1fs (poll=%.1fs)",
                            scene, action, wait_sec, _DL_POLL)
                while _dl_remaining > 0:
                    _sleep_chunk = min(_DL_POLL, _dl_remaining)
                    time.sleep(_sleep_chunk)
                    _dl_remaining -= _sleep_chunk
                    if _dl_remaining <= 0:
                        break
                    try:
                        _dl_img, _, _, _ = take_screenshot()
                        _dl_ph = compute_phash(_dl_img)
                        _dl_dist = phash_distance(state.last_phash, _dl_ph) if state.last_phash and _dl_ph else 0
                        if _dl_dist >= PHASH_THRESHOLD:
                            logger.info("  [DOWNLOAD_ADAPTIVE] 画面変化検出 (dist=%d) → 早期脱出", _dl_dist)
                            state.last_phash = _dl_ph
                            break
                    except Exception:
                        pass
            else:
                logger.info("  [%s][%s] long wait %.1fs",
                            scene, action, wait_sec)
                time.sleep(wait_sec)

        _loop_elapsed_ms = (time.time() - _loop_t0) * 1000
        state.total_loop_ms += _loop_elapsed_ms
        logger.info("  [PERF] Loop %.0fms (OCR)", _loop_elapsed_ms)

        # ── 9) メモリ解放 (SIGSEGV防止) ──
        # cv2 オブジェクトを毎イテレーション解放してメモリ断片化を防ぐ
        if i % 50 == 0:
            gc.collect()
            # scrcpy 不死身モード: 50イテレーションごとにチェック
            if i > 0:
                if _scrcpy_proc is None or _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] プロセス消滅を検知 — 自動再起動")
                    _scrcpy_proc = manage_scrcpy()

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)
    generate_and_copy_report(state, f"最大イテレーション({MAX_ITERATIONS})到達")

    # scrcpy プロセスを終了
    if _scrcpy_proc and _scrcpy_proc.poll() is None:
        _scrcpy_proc.terminate()
        logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)


if __name__ == "__main__":
    main()
