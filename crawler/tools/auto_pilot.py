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
import sqlite3
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

# ─── 永続化: SQLite (ludus.db) ──────────────────────────────
_STATE_DB_PATH = Path(__file__).parent.parent / "storage" / "ludus.db"


def _ensure_state_table():
    """auto_pilot_state テーブルがなければ作成する。"""
    conn = sqlite3.connect(str(_STATE_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auto_pilot_state "
        "(key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()


def persist_state(key: str, value: str):
    """状態を SQLite に永続化する。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO auto_pilot_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[PERSIST] 書き込み失敗 key=%s: %s", key, e)


def load_state(key: str, default: str = "") -> str:
    """SQLite から状態を読み込む。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        row = conn.execute(
            "SELECT value FROM auto_pilot_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def delete_state(key: str):
    """SQLite から状態を削除する。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        conn.execute("DELETE FROM auto_pilot_state WHERE key = ?", (key,))
        conn.commit()
        conn.close()
    except Exception:
        pass


_ensure_state_table()

# ─── 定数: ap/constants.py から一括 import ───
from tools.ap.constants import (  # noqa: E402
    _CRAWLER_ROOT, SCREENSHOT_PATH, ANALYSIS_PATH, REMOTE_PATH, EVIDENCE_DIR,
    MAX_ITERATIONS, POLL_INTERVAL, PHASH_THRESHOLD, FORCE_ANALYZE_AFTER,
    STALL_TIMEOUT, BATTLE_WAIT, DOWNLOAD_WAIT, MIN_TAP_INTERVAL, MIN_CAPTURE_INTERVAL,
    WATCHDOG_DEADLOCK_THRESHOLD, WATCHDOG_MAX_SOFT_RECOVERIES,
    WATCHDOG_MAX_TOTAL_RECOVERIES, APP_PACKAGE, APP_ACTIVITY,
    WATCHDOG_EXEMPT_ACTIONS, ADV_RAPID_PHASH_MAX, BLACKOUT_BRIGHTNESS,
    _DEBUG_SAVE_IMAGES, _GOLD_UI_ACTIONS, _SCENE_REEVAL_THRESHOLD,
    _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS, _CURRENCY_SPEND_KWS,
    _UI_TEXT_KWS, _SINGLE_ONLY,
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
    pop_last_scrcpy_bgr, check_foreground_app,
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
    find_gold_frame_near, detect_adv_advance_icon,
    is_adv_toolbar_visible, detect_movie_skip_button, detect_mini_conversation,
    detect_tutorial_dialog_nav, detect_dialog_frame_and_nav,
    process_paging_dialog, detect_notice_popup, count_page_dots, _detect_background_blur,
    detect_text_input_area,
    detect_tutorial_gold_swipe, detect_tutorial_gold_button_tap, detect_tutorial_overlay,
    smart_tap_button, find_golden_highlighted_button, find_3d_arrow,
    AssetManager, ASSET_MANAGER,
    detect_adv_scene, AdvSceneResult,
    detect_movie_scene, MovieSceneResult,
    clear_imread_cache, imread_cached,
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
        # ── ページドット + 背景ぼかし = ポップアップ確定 → SPATIAL_GATE バイパス ──
        # (ドット単体はホーム画面UIで誤検出多い → 背景ぼかし必須)
        _page_dots = count_page_dots(analysis_path) if analysis_path else 0
        _bypass_spatial = False
        if _page_dots >= 3 and analysis_path:
            _blur_img = imread_cached(analysis_path)
            if _blur_img is not None and _detect_background_blur(
                    _blur_img, _blur_img.shape[0], _blur_img.shape[1]):
                _bypass_spatial = True
                logger.info("[SPATIAL_GATE_BYPASS] ドット=%d+背景ぼかし → ポップアップ確定, SPATIAL_GATE スキップ", _page_dots)
        if not _bypass_spatial:
            _sg_blobs = find_finger_blobs(analysis_path, min_area=400)
            _sg_blobs = [b for b in _sg_blobs if b[1] > _SPATIAL_MARGIN_TOP and b[0] < W - _CLOSE_BTN_OFFSET]
            if _sg_blobs:
                _sg_best = max(_sg_blobs, key=lambda b: b[2])
                _sg_dist = ((_dlg_x - _sg_best[0]) ** 2 + (_dlg_y - _sg_best[1]) ** 2) ** 0.5
                if _sg_dist > 300:
                    # ▷ は指から遠い → 偽検出の可能性高い
                    # ×ボタンがあれば × で閉じるフォールバック
                    _sg_close_fb = ASSET_MANAGER.match_single(
                        "tutorial_dialog_close", analysis_path,
                        roi=(int(W * 0.85), 0, int(W * 0.15), int(H * 0.15)))
                    if _sg_close_fb and _sg_close_fb[2] >= 0.70:
                        logger.info(
                            ">>> [SPATIAL_GATE] 指(%d,%d)↔▷(%d,%d) 距離=%.0fpx>300 → ×フォールバック(%d,%d) score=%.3f",
                            _sg_best[0], _sg_best[1], _dlg_x, _dlg_y, _sg_dist,
                            _sg_close_fb[0], _sg_close_fb[1], _sg_close_fb[2],
                        )
                        _dlg = ("close", _sg_close_fb[0], _sg_close_fb[1])
                        _dlg_type, _dlg_x, _dlg_y = _dlg
                    else:
                        logger.info(
                            ">>> [SPATIAL_GATE] 指(%d,%d)↔▷(%d,%d) 距離=%.0fpx>300 → #0-DIALOG スキップ",
                            _sg_best[0], _sg_best[1], _dlg_x, _dlg_y, _sg_dist,
                        )
                        _dlg = None
        # ── 白ハンドポインタ画面ガード (×フォールバック時はスキップ) ──
        if _dlg is not None and not _bypass_spatial and _dlg_type != "close":
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
            # 画面右端クランプ: x=1517-1519 (device端) → W*0.95 に制限
            _nx = min(_nx, int(W * 0.95))
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
    # ADVシーン検出結果 (detect_adv_scene で毎回フレッシュ検出)
    _adv_result: AdvSceneResult = AdvSceneResult()
    joined = " ".join(texts)

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
                _log_milestone(state, "DL_END")
        else:
            # ホーム到達後: OCRにDLテキストがなければ即解除
            _dl_kws_check = ["Download", "ダウンロード", "追加データ", "MB", "GB"]
            if not any(kw in joined for kw in _dl_kws_check):
                logger.info("[DL_PROTECT] ホーム後 + DLテキストなし → download_active 解除")
                state.download_active = False
                _log_milestone(state, "DL_END")

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
        _log_milestone(state, "DL_START")
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT
    # ── DL失敗ダイアログ: OCR がボタンテキスト検出できない場合の安全網 ──
    if _dl_is_failure and _has_download_text:
        state.download_active = False
        W, H = ANALYSIS_W, ANALYSIS_H
        _ok_x, _ok_y = int(W * 0.62), int(H * 0.82)
        logger.info("[DL_FAIL_RETRY] 失敗ダイアログ検出 → OK推定位置 (%d,%d) タップ", _ok_x, _ok_y)
        tap_device(_ok_x, _ok_y, state, "DL_FAIL_RETRY_OK")
        return "DL_FAIL_RETRY", 2.0

    # ── 【#-2.9】確認ダイアログ — 肯定ボタン最優先 ──
    # (A) OK/はい + キャンセル/いいえ が共存 → 確認ダイアログ → OK を必ずタップ。
    # (B) 「完了」系テキスト + OK 単独 → 完了通知ダイアログ → OK をタップ。
    # #0-DIALOG の × ボタンが先に発動する問題を根本解決。
    # ダウンロードの次、SKIP より先に評価する。
    _confirm_pos = has_any(ocr, _CONFIRM_POS_KWS)
    _confirm_neg = has_any(ocr, _CONFIRM_NEG_KWS)
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
                        logger.info("[DL_COMPLETE] ✅ 変化検知 (dist=%d) #%d [%s] → 成功",
                                    _cd_dist, _tv_i + 1, _tv_label)
                        break
                    logger.info("[DL_COMPLETE] 変化なし (dist=%d) #%d [%s] → 次座標",
                                _cd_dist, _tv_i + 1, _tv_label)
                    _base_ph_cd = _new_ph_cd
            state.download_active = False
            logger.info("[DL_PROTECT] DL完了ダイアログOK → download_active 解除")
            _log_milestone(state, "DL_END")
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
    if has_text(ocr, "ご注意", min_conf=0.3) or (
        has_text(ocr, "基本無料", min_conf=0.3) and has_text(ocr, "未成年", min_conf=0.3)
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
            _log_milestone(state, "NOTICE_DISMISS")
            return "NOTICE_DISMISS", 0.5
        else:
            logger.info(">>> 【ご注意→リトライ上限(5回)】 次ループで再検出")
            return "NOTICE_DISMISS", 3.0

    # ─── 【最優先 #-1b】MAIN STORY 画面 ───
    # (A) クエスト選択画面: 「NEW」+「推奨」+「Main」→ クエストカードをタップ
    # (B) ローディング背景: タイトル後の非インタラクティブ画面 → 自動遷移待ち
    _is_main_story_bg = (
        any("MAIN" in t or "Main" in t for t in texts) and
        any("推奨" in t or "STORY" in t for t in texts) and
        not any(kw in joined for kw in ["クエスト", "ショップ", "ガチャ", "ガシャ", "光の間", "パーティ"])
    )
    if _is_main_story_bg:
        # クエスト選択画面 — "Main" カードをタップ (NEW バッジの有無は問わない)
        _quest_hit = has_text(ocr, "Main", min_conf=0.2)
        if _quest_hit:
            _qx, _qy = _quest_hit["center"]
            logger.info(">>> MAIN STORY クエスト選択 — 'Main' カードタップ (%d,%d)", _qx, _qy)
            tap_device(_qx, _qy, state, "MAIN_STORY_QUEST_TAP")
            return "MAIN_STORY_QUEST_TAP", 2.0
        # フォールバック: 画面下部中央をタップ
        _qx, _qy = int(W * 0.5), int(H * 0.85)
        logger.info(">>> MAIN STORY クエスト選択 — フォールバックタップ (%d,%d)", _qx, _qy)
        tap_device(_qx, _qy, state, "MAIN_STORY_QUEST_FB")
        return "MAIN_STORY_QUEST_FB", 2.0

    # ─── 【最優先 #-1b2】Result画面 — 「次へ」ボタンタップ ───
    # "Result" + "次へ" が見えたら即タップ (SWIPE_UP 誤マッチ防止)
    _is_result_early = any("Result" in t for t in texts)
    if _is_result_early:
        state.result_total_taps += 1
        # フリーズ検出: 30タップ超えたら force-stop で復旧
        if state.result_total_taps >= 30:
            logger.warning("[RESULT_FREEZE] RESULT_NEXT_EARLY %d回 → Unity入力フリーズ → force-stop",
                           state.result_total_taps)
            state.result_total_taps = 0
            state.result_rapid_count = 0
            watchdog_recover(state)
            return "RESULT_FREEZE", 0.0
        _next_btn = has_text(ocr, "次へ", min_conf=0.3)
        if _next_btn:
            _nx, _ny = _next_btn["center"]
            logger.info(">>> Result画面 — '次へ'(%d,%d) タップ", _nx, _ny)
            tap_device(_nx, _ny, state, "RESULT_NEXT_EARLY")
            return "RESULT_NEXT_EARLY", 1.5

    # ─── 【最優先 #-1c】クエスト詳細画面 — 「挑戦」ボタンタップ ───
    # ステージ番号 (1-1等) + "推奨" or "報酬" → クエスト詳細画面と判定
    # "挑戦" はゴールド装飾フォントで OCR 検出不可のため固定位置タップ
    # AssetManager (SWIPE_UP/DIALOG_NEXT) が誤マッチするため、ここで先に処理する
    _quest_stage = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                                  "3-1", "3-2", "4-1", "4-2"])
    _quest_detail_kw = any(kw in joined for kw in ["推奨", "報酬", "パーティ"])
    if _quest_stage and _quest_detail_kw:
        # OCR で挑戦テキストが読めた場合はその座標を使う
        _quest_chal = None
        for _qkw in ["挑戦", "戦闘", "出撃"]:
            _qc = has_text(ocr, _qkw, min_conf=0.3)
            if _qc and _qc["center"][1] > H * 0.5:
                _quest_chal = _qc
                break
        if _quest_chal:
            _qcx, _qcy = _quest_chal["center"]
            # 挑戦ボタンは右端にあるため、OCR テキスト中心が左寄りの場合を補正
            _qcx = max(_qcx, int(W * 0.88))
        else:
            # 固定位置: 挑戦ボタンは画面右下 (x=92%, y=90%)
            _qcx, _qcy = int(W * 0.92), int(H * 0.90)
        if state.game_roi:
            _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
            _qcy = min(_qcy, _roi_max_y)
        logger.info(">>> クエスト詳細 — 挑戦ボタン(%d,%d) タップ", _qcx, _qcy)
        tap_device(_qcx, _qcy, state, "QUEST_DETAIL_CHALLENGE")
        return "QUEST_DETAIL_CHALLENGE", 2.0

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
    # ADV/ミニ会話/動画シーン検出時はスキップ (指アイコンは出ない — 背景装飾の誤検出防止)
    _pre_dialog_finger = False
    _is_mini_conv = detect_mini_conversation(analysis_path) is not None if analysis_path else False
    _is_result_screen = any(
        any(k in t for k in ("Result", "リザルト", "次へ"))
        for t in texts
    )
    # ADV/MOVIE シーンでは指ブロブ+金枠検出を完全スキップ (緑発光等の誤検出防止)
    _is_adv_or_movie = (
        _adv_result.is_adv
        or state.current_scene in ("ADV", "MOVIE")
        or any(t in ("SKIP", "スキップ") for t in texts)
    )
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

    # ─── 【最優先 #0-DIALOG】ダイアログ・ファースト ────────────
    _dialog_result = handle_dialog_screen(
        state, analysis_path, ocr, texts, _is_battle_early, _pre_dialog_finger,
        is_notice_popup=_is_notice)
    if _dialog_result is not None:
        return _dialog_result

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
            _log_milestone(state, "NAME_INPUT")
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

    # ─── 【指+金枠ボタン】PRE_DIALOG_GUARD で指ブロブ検出 → ゴールドボタン直タップ ───
    # ダイアログ×ボタンがない画面 (プレゼントボックス等) で指が金色ボタンを指している場合
    # 白ハンドポインタの指先方向がわかれば、その方向の金枠を優先タップ
    # 「矢印をタップ」画面では MAP_ARROW (#2-a) に委譲するためスキップ
    _arrow_instruction = any("矢印を" in t for t in texts)
    # ホーム画面では WHITE_HAND+金枠が装飾UIで偽陽性 → ホーム検出に委譲
    _home_kws_fg = ["光の間", "ショップ", "ガチャ", "ガシャ", "マップ", "レイヤ"]
    _is_home_fg = sum(1 for kw in _home_kws_fg
                      if any(kw in t or t in kw for t in texts)) >= 2
    # ─── メインクエスト選択画面: 「Main」ボタンを直接タップ ───
    # 金枠がバナー装飾を拾って空振りするため、OCRの「Main」テキスト位置をタップ
    # 白ハンドポインタがある場合は指差しガイドが優先 (Upgrade等を指す場合がある)
    if any("メインクエスト" in t for t in texts) and _white_hand_pos is None:
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
            # テキストラベルはノードアイコンの下にある → 60px上を狙う
            _qy = max(_qy - 60, 10)
            logger.info(">>> 【クエストマップ】 ノード '%s' (%d,%d) タップ",
                        _quest_node["text"], _qx, _qy)
            tap_device(_qx, _qy, state, "QUEST_NODE_TAP")
            return "QUEST_NODE_TAP", 2.0
    if _pre_dialog_finger and analysis_path is not None and not _arrow_instruction and not _is_home_fg:
        _hand_xy = None
        _hand_d = ""
        if _white_hand_pos is not None:
            _hand_xy = (_white_hand_pos[0], _white_hand_pos[1])
            _hand_d = _white_hand_pos[3] if len(_white_hand_pos) >= 4 else ""
        _finger_gold_pos = find_golden_highlighted_button(
            analysis_path, hand_pos=_hand_xy, hand_dir=_hand_d)
        if _finger_gold_pos:
            _fg_x, _fg_y = _finger_gold_pos
            logger.info(">>> [FINGER_GOLD_TAP] 指ブロブ+金色ボタン → (%d,%d)", _fg_x, _fg_y)
            tap_device(_fg_x, _fg_y, state, "GOLD_BTN_TAP")
            return "GOLD_BTN_TAP", 1.0

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
    if analysis_path is not None and not _is_battle_ui and not _adv_result.is_adv and not _has_dialog_kw and not _is_home_screen:
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
    # DL中はゲージバーを金枠と誤検出するためスキップ
    _battle_tut_kws = ["隣接攻撃", "必殺技", "巫殺技", "ATTACKER", "通常攻撃"]
    _is_battle_tut_context = any(kw in joined for kw in _battle_tut_kws)
    # バトルUI確認済みの場合はフッター外GoldBtnをスキップ → Glow SM (フッター) に委ねる
    if analysis_path is not None and _is_battle_tut_context and not _is_battle_early and not state.download_active:
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
        # DIALOG_NAV_RIGHT 連続空振りガード: 画面変化なく繰り返す場合は偽陽性
        if asset_hit and asset_hit[2] == "DIALOG_NAV_RIGHT":
            # ページドット必須: ページングダイアログには必ずドットがある。
            # ドット0個 = ページングダイアログではない → ▷ テンプレは偽陽性
            _popup_dots_nav = count_page_dots(analysis_path) if analysis_path else 0
            if _popup_dots_nav == 0:
                logger.info("[DIALOG_NAV] ページドット未検出 → ▷ マッチは偽陽性、スキップ")
                asset_hit = None
            # ポップアップ(ドット+blur)内では偽陽性率が高い → OCRカルーセルハンドラに委譲
            if _popup_dots_nav >= 2:
                _pi_nav = imread_cached(analysis_path) if analysis_path else None
                if _pi_nav is not None and _detect_background_blur(
                        _pi_nav, _pi_nav.shape[0], _pi_nav.shape[1]):
                    logger.info("[DIALOG_NAV] ポップアップ内 (dots=%d) → OCRハンドラに委譲",
                                _popup_dots_nav)
                    asset_hit = None
            # 抑制期間中 (stall発動後 16iter) は DIALOG_NAV_RIGHT を常に無視
            if asset_hit:
                _dns = getattr(state, '_dialog_nav_suppress', 0)
                if _dns > 0:
                    state._dialog_nav_suppress = _dns - 1
                    asset_hit = None
            if asset_hit and state.last_phash_dist < 8:
                state.dialog_nav_stall.tick()
                if state.dialog_nav_stall.stalled:
                    logger.warning(
                        "[DIALOG_NAV_STALL] %d回空振り (phash変化なし) → 16iter抑制",
                        state.dialog_nav_stall.count)
                    state.dialog_nav_stall.reset()
                    state._dialog_nav_suppress = 16
                    asset_hit = None  # フォールスルーして他の検出器に委譲
            else:
                state.dialog_nav_stall.reset()
                state._dialog_nav_suppress = 0
        elif asset_hit:
            state.dialog_nav_stall.reset()  # 別アクションが来たらリセット
            state._dialog_nav_suppress = 0
        # 「矢印をタップ」画面では DIALOG_NEXT 誤マッチを無視 → #2-a MAP_ARROW に委譲
        if asset_hit and asset_hit[2] == "ASSET_TUTORIAL_DIALOG_NEXT":
            if any("矢印を" in t for t in texts):
                logger.info(">>> [Asset Match] DIALOG_NEXT を抑制 (矢印をタップ画面 → #2-a に委譲)")
                asset_hit = None
        # ホーム画面では FINGER_TEMPLATE 偽陽性を抑制 → ホーム検出ハンドラに委譲
        if asset_hit and asset_hit[2] == "FINGER_TEMPLATE":
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
                # 白ハンドポインタ (テンプレートマッチ) で方向を取得
                _wh = detect_white_hand_pointer(analysis_path, threshold=0.85)
                _hand_pos = (cx, cy)
                _hand_dir = ""
                if _wh:
                    _hand_pos = (_wh[0], _wh[1])
                    _hand_dir = _wh[3]  # "up" or "down"
                _hx, _hy = _hand_pos
                tap_x, tap_y = cx, cy  # デフォルト

                # 【プライマリ】指の方向にある最近接OCRテキストをタップ (距離200px以内)
                _MAX_HAND_OCR_DIST = 200
                _ocr_found = False
                if _hand_dir and ocr:
                    _dir_items = []
                    for item in ocr:
                        _tx, _ty = item["center"]
                        _dist = abs(_hx - _tx) + abs(_hy - _ty)
                        if _dist > _MAX_HAND_OCR_DIST:
                            continue  # 遠すぎるOCRは無視
                        if _hand_dir == "up" and _ty < _hy:
                            _dir_items.append((_tx, _ty, _dist, item["text"]))
                        elif _hand_dir == "down" and _ty > _hy:
                            _dir_items.append((_tx, _ty, _dist, item["text"]))
                    if _dir_items:
                        _dir_items.sort(key=lambda d: d[2])  # 距離順
                        tap_x, tap_y = _dir_items[0][0], _dir_items[0][1]
                        _ocr_found = True
                        logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → OCR '%s'(%d,%d) dist=%d",
                                    cx, cy, _hand_dir, _dir_items[0][3], tap_x, tap_y, _dir_items[0][2])

                # 【フォールバック】OCR なし or 距離内に該当なし → 指アイコン方向にオフセット
                if not _ocr_found:
                    _offset = -80 if _hand_dir == "up" else 160
                    tap_x, tap_y = smart_tap_button(
                        analysis_path, _hx, _hy + _offset, search_r=160, ocr_items=ocr)
                    logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d,dir=%s) → offset(%d,%d)",
                                cx, cy, _hand_dir, tap_x, tap_y)

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
        _nav = detect_tutorial_dialog_nav(analysis_path, W, H) if analysis_path else None
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
        if state.dialog_close_total >= 8:
            logger.warning(">>> 【%s ポップアップ】 × が8回空振り → BACK キーで脱出",
                           close_popup["text"][:6])
            try:
                adb("shell input keyevent KEYCODE_BACK")
            except Exception as _e:
                logger.debug("[CLOSE_POPUP] BACK キー送信例外: %s", _e)
            state.dialog_close_total = 0
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
        state.dialog_close_total += 1
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
        if is_battle_screen:
            _log_milestone(state, "FIRST_BATTLE")
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
                # 条件A: TAP TO START は確実にタイトル (Magia Exedra 単独は除外)
                any(kw in _nav_joined for kw in ["TAP TO START", "TAPTOSTART"]) or
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
            _log_milestone(state, "TITLE_TAP")
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
            # 回数制限なし: 指+金枠が実在する限り何回でもタップ
            _ht_blobs = find_finger_blobs(analysis_path, home_mode=True) if analysis_path else []
            _ht_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
            # 暗転オーバーレイ検出 (チュートリアル中は非ハイライト部分が暗い)
            _ht_dimmed = detect_tutorial_overlay(analysis_path) if analysis_path else False
            if _ht_blobs or _ht_gold:
                _ht_target = None
                if _ht_blobs:
                    _ht_chosen = max(_ht_blobs, key=lambda b: b[2])
                    _ht_bx, _ht_by = _ht_chosen[0], _ht_chosen[1]
                    _ht_gf = find_gold_frame_near(analysis_path, _ht_bx, _ht_by) if analysis_path else None
                    # フッターナビ領域 (y > H*0.85) の金色要素を除外
                    if _ht_gf and _ht_gf[1] > H * 0.85:
                        _ht_gf = None
                    if _ht_gf:
                        _ht_fg_dist = ((_ht_bx - _ht_gf[0]) ** 2 + (_ht_by - _ht_gf[1]) ** 2) ** 0.5
                        if _ht_fg_dist <= 200:
                            _ht_target = (_ht_gf[0], _ht_gf[1])
                            logger.info("  ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) dist=%.0f dimmed=%s",
                                        _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1], _ht_fg_dist, _ht_dimmed)
                        elif _ht_dimmed:
                            # 金枠遠い+暗転あり → 指先タップ
                            _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                            _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                            logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠(%d,%d) dist=%.0f>200+暗転あり]",
                                        _ht_bx, _ht_by, *_ht_target, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                        else:
                            logger.info("  ホーム指検出: 指(%d,%d) 金枠(%d,%d) dist=%.0f>200+暗転なし → スキップ",
                                        _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1], _ht_fg_dist)
                    elif _ht_dimmed:
                        # 金枠なしだが暗転あり → 指先タップ (チュートリアルの可能性高い)
                        _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                        _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                        logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転あり]",
                                    _ht_bx, _ht_by, *_ht_target)
                    else:
                        # 金枠なし+暗転なし: 画面中央付近なら指先をタップ、端なら偽検出疑い
                        if _ht_bx > 150 and _ht_by > 100 and _ht_bx < W - 100 and _ht_by < H - 80:
                            _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                            _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                            logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転なし・中央付近]",
                                        _ht_bx, _ht_by, *_ht_target)
                        else:
                            logger.info("  ホーム指検出: 指(%d,%d) 金枠なし+暗転なし+画面端 → 偽検出疑い、スキップ",
                                        _ht_bx, _ht_by)
                elif _ht_gold:
                    # 金枠のみ (指なし) → 暗転があればチュートリアル
                    if _ht_dimmed:
                        _ht_target = _ht_gold
                        logger.info("  ホームチュートリアル: 金ボタン(%d,%d) [暗転あり]", *_ht_gold)
                    else:
                        logger.info("  ホーム金枠検出: (%d,%d) 暗転なし → 通常ホーム、スキップ", *_ht_gold)
                if _ht_target:
                    state.home_tutorial_tap_count += 1
                    tap_device(_ht_target[0], _ht_target[1], state, "HOME_TUTORIAL_TAP")
                    return "HOME_TUTORIAL_TAP", 0.5
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
                    # 左キャラ選択済み → 必殺技を優先、なければ右スキルを選択
                    _skill_match = None
                    if analysis_path is not None:
                        try:
                            _skill_roi = (_RIGHT_PANEL_X, int(H * 0.45), W, H)
                            _skill_match = ASSET_MANAGER.match_single(
                                "battle_skill", analysis_path, roi=_skill_roi)
                        except Exception:
                            pass
                    if _skill_match and _skill_match[2] >= 0.55:
                        # 必殺技ボタン検出 → 直接タップ (モヤ blob ではなくテンプレ座標)
                        _sk_x, _sk_y = _skill_match[0], _skill_match[1]
                        logger.info("  バトル: キャラ選択後 → 必殺技 (%.2f) (%d,%d)",
                                    _skill_match[2], _sk_x, _sk_y)
                        state.char_just_selected = False
                        tap_device(_sk_x, _sk_y, state, "BATTLE_HISSATSU")
                        return "BATTLE_HISSATSU", 0.5
                    elif right_panel:
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
            # ── Hard Mask 一時ファイルを削除 (メモリリーク防止) ──
            if _hm_analysis != analysis_path:
                try:
                    Path(_hm_analysis).unlink(missing_ok=True)
                except OSError:
                    pass

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
                if fx < 600 and fy > H * 0.55:
                    state.char_just_selected = True
                    state.character_selected = True  # GLOW SM 用にも同期
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 1.0

    # ─── 【最優先 #2-a】探索マップ 3D矢印タップ ───
    # 「矢印をタップしてください」が出ている場合、3D空間の矢印を検出してタップ
    arrow_instruction = has_text(ocr, "矢印を", min_conf=0.2)
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
    # OCR が文字を途中で切る場合がある ("クエスト"→"クエス", "パーティ"→"パーテ")
    # → 短いプレフィックスで双方向部分一致: keyword in text OR text in keyword
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成",
                       "マップ", "レイヤー"]
    # 短縮プレフィックス (2文字以上一致で検出)
    _home_prefixes = ["光の", "ショッ", "ガシャ", "ガチャ", "パーテ",
                      "クエス", "ミッシ", "メニュ", "ホーム",
                      "お知ら", "イベン", "フレン", "マイペ", "編成",
                      "マッ", "レイヤ"]
    def _home_match(t: str) -> int:
        """home_indicators のうち t にマッチする数を返す (完全 or プレフィックス)"""
        count = 0
        for h, p in zip(home_indicators, _home_prefixes):
            if h in t or p in t or t in h:
                count += 1
        return count
    home_count = sum(min(1, _home_match(t)) for t in texts)
    # 重複排除: 同じ indicator に複数テキストがマッチしても1回
    _matched = set()
    for t in texts:
        for idx, (h, p) in enumerate(zip(home_indicators, _home_prefixes)):
            if idx not in _matched and (h in t or p in t or t in h):
                _matched.add(idx)
    home_count = len(_matched)
    if home_count >= 3:
        state.home_reached = True
        # ── 指アイコン+金枠 → まだホームチュートリアル中 ──
        # 「ホーム画面かつ指+金枠がない」= チュートリアル終了
        # 回数制限なし: 指+金枠+暗転オーバーレイで判定 (カウンタ偽検出は廃止)
        _home_blobs = find_finger_blobs(analysis_path, home_mode=True) if analysis_path else []
        _home_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
        _home_dimmed = detect_tutorial_overlay(analysis_path) if analysis_path else False
        # tutorial_hand_pointer テンプレートも指の証拠として使用
        # (scrcpy 低解像度では find_finger_blobs の金枠検出が失敗することがある)
        _hand_match = ASSET_MANAGER.match_single("tutorial_hand_pointer", analysis_path) if analysis_path else None
        if _hand_match and _hand_match[2] >= 0.70 and not _home_blobs:
            # ハンドポインタ座標を指ブロブとして追加 (area=10000 ダミー)
            _hx, _hy = _hand_match[0], _hand_match[1]
            logger.info(">>> ホーム: tutorial_hand_pointer(%.2f) (%d,%d) → 指ブロブとして追加",
                        _hand_match[2], _hx, _hy)
            _home_blobs = [(_hx, _hy, 10000.0, _hx - 20, _hy - 20, 40, 40)]
        if _home_blobs or _home_gold:
            _tap_target = None
            if _home_blobs:
                _chosen_blob = max(_home_blobs, key=lambda b: b[2])  # area最大
                _bx, _by = _chosen_blob[0], _chosen_blob[1]
                # ── 優先1: OCR テキスト中心 (指の近傍でフッターナビ外) ──
                _ocr_target = None
                _HOME_NAV_KWS = {"光の間", "ショップ", "ガチャ", "ガシャ", "マップ", "レイヤ",
                                 "マッチ", "ユニオン", "クエスト", "クエス", "パーティ", "育成",
                                 "ころの器", "こころの器"}
                for _oe in ocr:
                    _ot = _oe.get("text", "")
                    _oc = _oe.get("center", (0, 0))
                    # フッターナビ外のテキストで、指の近傍250px以内
                    if (any(kw in _ot for kw in _HOME_NAV_KWS)
                            or len(_ot) < 2):
                        continue
                    _odx = abs(_oc[0] - _bx)
                    _ody = abs(_oc[1] - _by)
                    if _odx < 250 and _ody < 250 and _oc[1] < H * 0.85:
                        _ocr_target = (_oc[0], _oc[1])
                        logger.info(">>> ホームチュートリアル: 指(%d,%d)→OCRテキスト '%s'(%d,%d) [%d回目]",
                                    _bx, _by, _ot, _oc[0], _oc[1],
                                    state.home_tutorial_tap_count + 1)
                        break
                if _ocr_target:
                    _tap_target = _ocr_target
                else:
                    # ── 優先2: HSV金枠検出 (フォールバック) ──
                    _gf = find_gold_frame_near(analysis_path, _bx, _by, search_radius=250) if analysis_path else None
                    if _gf and _gf[1] > H * 0.85:
                        logger.info(">>> ホーム: 金枠(%d,%d) がフッターナビ領域 → 除外", _gf[0], _gf[1])
                        _gf = None
                    if _gf:
                        _tap_target = (_gf[0], _gf[1])
                        logger.info(">>> ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) dimmed=%s [%d回目]",
                                    _bx, _by, _gf[0], _gf[1], _home_dimmed, state.home_tutorial_tap_count + 1)
                    elif _home_gold:
                        _tap_target = _home_gold
                        logger.info(">>> ホームチュートリアル: 指(%d,%d)→GoldBtn(%d,%d) [近傍外] [%d回目]",
                                    _bx, _by, *_home_gold, state.home_tutorial_tap_count + 1)
                    elif _home_dimmed:
                        _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                        _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
                        logger.info(">>> ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転あり]",
                                    _bx, _by, *_tap_target)
                    else:
                        if _bx > 150 and _by > 100 and _bx < W - 100 and _by < H - 80:
                            _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                            _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
                            logger.info(">>> ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転なし・中央付近]",
                                        _bx, _by, *_tap_target)
                        else:
                            logger.info(">>> ホーム指検出: 指(%d,%d) 金枠なし+暗転なし+画面端 → 偽検出疑い、スキップ",
                                        _bx, _by)
            elif _home_gold:
                # 指なし+金枠あり: dimmed でも非 dimmed でもチュートリアル金枠の可能性あり
                _tap_target = _home_gold
                if _home_dimmed:
                    logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転あり]", *_home_gold)
                else:
                    logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転なし・指なし]", *_home_gold)
            if _tap_target:
                state.home_tutorial_tap_count += 1
                # 指/金枠を検出 → チュートリアル未完了なので HOME_CLEAR_CHECK を常にリセット
                if hasattr(state, '_home_clear_count'):
                    state._home_clear_count = 0
                tap_device(_tap_target[0], _tap_target[1], state, "HOME_TUTORIAL_TAP")
                return "HOME_TUTORIAL_TAP", 0.5
            # blob/gold検出あるがタップ対象なし → 指/金枠の存在自体がチュートリアル未完了の証拠
            if hasattr(state, '_home_clear_count') and state._home_clear_count > 0:
                logger.info(">>> 指/金枠検出あり(タップ対象なし) → HOME_CLEAR_COUNT リセット (%d→0)",
                            state._home_clear_count)
                state._home_clear_count = 0
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
        _BUBBLE_EXCLUDE_EXACT = {"AUTO", ">>", ">|", "D1", "×", "+", "■", "畄", "目", "SKIP"}
        _BUBBLE_EXCLUDE_SUBSTR = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora", "M.8", "M8X")
        _BUBBLE_NUM_RE = re.compile(r'^[\d,./:%+\-・\s]+$')
        _BUBBLE_ALPHANUM_NOISE_RE = re.compile(r'^[A-Za-z0-9.,\-+×★☆\s]{1,5}$')
        _bubble_texts = [r for r in ocr
                         if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                         and r["text"] not in _BUBBLE_EXCLUDE_EXACT
                         and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR)
                         and not _BUBBLE_NUM_RE.match(r["text"])
                         and not _BUBBLE_ALPHANUM_NOISE_RE.match(r["text"])
                         and len(r["text"]) > 2]
        if _bubble_texts and state.home_tutorial_tap_count < 10:
            _bt = _bubble_texts[0]
            _btx, _bty = _bt["center"]
            logger.info(">>> ホーム画面 + 吹き出しセリフ '%s' → チュートリアル継続 (%d,%d)",
                        _bt["text"][:10], _btx, _bty)
            if hasattr(state, '_home_clear_count'):
                state._home_clear_count = 0
            tap_device(_btx, _bty, state, "BUBBLE_TAP")
            return "BUBBLE_TAP", 0.3
        # ── チュートリアル完了判定 ──
        # 条件: 暗転なし + 指なし + 金枠なし → 通常ホーム画面
        # 暗転中は指/金枠検出が失敗しているだけでチュートリアル中
        if not hasattr(state, '_home_clear_count'):
            state._home_clear_count = 0
            state._home_clear_last_phash = ""
        if _home_dimmed:
            # 暗転中 = チュートリアル中 → 完了判定リセット
            if state._home_clear_count > 0:
                logger.info(">>> ホーム画面 暗転あり → チュートリアル中 (HOME_CLEAR_COUNT %d→0)",
                            state._home_clear_count)
                state._home_clear_count = 0
            else:
                logger.info(">>> ホーム画面 暗転あり → チュートリアル中 (指/金枠未検出だが暗転)")
            return "HOME_CLEAR_CHECK", 0.5
        # 暗転なし + 指なし + 金枠なし → 通常ホームの可能性
        # 同一フレーム (phash同一) での重複カウントを防止
        _cur_phash = getattr(state, 'last_phash', "")
        if _cur_phash and _cur_phash == state._home_clear_last_phash:
            logger.info(">>> ホーム画面 指/金枠/暗転なし (同一フレーム, %d/3) → スキップ",
                        state._home_clear_count)
            return "HOME_CLEAR_CHECK", 1.0
        state._home_clear_last_phash = _cur_phash
        state._home_clear_count += 1
        if state._home_clear_count < 3:
            logger.info(">>> ホーム画面 指/金枠/暗転なし (%d/3) → 確認待ち", state._home_clear_count)
            return "HOME_CLEAR_CHECK", 0.5
        logger.info(">>> ホーム画面 指/金枠/暗転なし 3フレーム連続 → チュートリアル完了!")
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
        _log_milestone(state, "HOME_REACHED")
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 (セカンダリチェック) ───
    # ※ メインの厳格判定は関数冒頭の【絶対最優先 #-3】で実施済み。
    # ここではフォールバックとして「ダウンロード」(日本語) + 進捗テキスト の組み合わせのみ検出。
    # 通信速度やネットワーク状態による推測は一切行わない。
    _dl_jp = has_any(ocr, ["ダウンロード", "追加データ"])
    _dl_progress = any("MB" in t or "GB" in t for t in texts)
    if _dl_jp and _dl_progress:
        logger.info(">>> [DOWNLOAD_STRICT_JP] %s + 進捗あり — 待機", _dl_jp["text"])
        state.download_active = True
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
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技", "巫殺技"])
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
    _STORY_TAP_EXCLUDE = {"Rank", "Pank", "Runk", "AUTO", "SKIP", ">>", ">|"}
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6
                   and r["text"] not in _STORY_TAP_EXCLUDE]
    if lower_texts and len(ocr) <= 15 and not state.download_active:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── 右上吹き出しセリフ (メニュー画面上のキャラガイダンス) ───
    # 右上エリア (x>55%, y<35%) にテキストがあり、AUTO/>> ボタン等のUI要素と共存
    # → セリフが止まっている (前回と同一テキスト or phash安定) ならタップで送る
    _BUBBLE_EXCLUDE_EXACT_2 = {"AUTO", ">>", ">|", "D1", "×", "+", "■", "畄", "目", "SKIP"}
    _BUBBLE_EXCLUDE_SUBSTR_2 = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora")
    _BUBBLE_NUM_RE_2 = re.compile(r'^[\d,./:%+\-・\s]+$')
    _bubble_region = [r for r in ocr
                      if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                      and r["text"] not in _BUBBLE_EXCLUDE_EXACT_2
                      and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR_2)
                      and not _BUBBLE_NUM_RE_2.match(r["text"])
                      and len(r["text"]) > 2]
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


# ─── マイルストーン到達時間ログ ────────────────────────
def _log_milestone(state: PilotState, milestone: str) -> None:
    """目標到達時の経過時間をログ出力する。同一マイルストーンは初回のみ記録。"""
    if milestone in state.milestone_logged:
        return  # 重複防止: 同じマイルストーンは初回のみ
    _now = time.time()
    _elapsed = _now - state.launch_time
    _m, _s = divmod(int(_elapsed), 60)
    _h, _m = divmod(_m, 60)
    if state.is_fresh_start:
        logger.info("  [TIMER] %s — 起動から %d時間%02d分%02d秒 (新規スタート)",
                    milestone, _h, _m, _s)
    else:
        logger.info("  [TIMER] %s — 起動から %d時間%02d分%02d秒 (途中再開のため総所要時間は計測不可)",
                    milestone, _h, _m, _s)
    state.milestone_logged[milestone] = _elapsed


# ─── フェーズタイムライン生成 ────────────────────────────
_PHASE_LABELS: dict[str, str] = {
    "APP_LAUNCH":     "アプリ起動",
    "NOTICE_DISMISS": "ご注意画面",
    "TITLE_TAP":      "タイトル画面",
    "FIRST_BATTLE":   "初回バトル開始",
    "DL_START":       "ダウンロード開始",
    "DL_END":         "ダウンロード完了",
    "NAME_INPUT":     "名前入力",
    "HOME_REACHED":   "ホーム画面到達",
}


def _build_phase_timeline(state: PilotState) -> list[str]:
    """マイルストーンから Markdown テーブル形式のフェーズタイムラインを生成する。"""
    milestones = state.milestone_logged
    if not milestones:
        return ["## フェーズタイムライン", "(マイルストーン未記録)"]

    def _fmt_time(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    lines = [
        "## フェーズタイムライン",
        "",
        "| フェーズ | 到達時刻 | 区間時間 |",
        "|---------|---------|---------|",
    ]
    # マイルストーンを到達時刻順にソート
    sorted_ms = sorted(milestones.items(), key=lambda x: x[1])
    prev_time = 0.0
    for name, elapsed in sorted_ms:
        label = _PHASE_LABELS.get(name, name)
        delta = elapsed - prev_time
        lines.append(f"| {label} | {_fmt_time(elapsed)} | +{_fmt_time(delta)} |")
        prev_time = elapsed
    # 合計行
    total = sorted_ms[-1][1] if sorted_ms else 0.0
    lines.append(f"| **合計** | **{_fmt_time(total)}** | |")
    return lines


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
        f"- 起動種別             : {'新規スタート (--fresh-install)' if state.is_fresh_start else '途中再開'}",
        "",
        *_build_phase_timeline(state),
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

    優先順位 (特定要素が多い順):
    1. MOVIE 慣性 (前回 MOVIE + ADV/BATTLE 証拠なし) → MOVIE
    2. BATTLE 継続 (前回 BATTLE + phash 小変化) → BATTLE
    3. ADV 継続 (前回 ADV + AUTO アイコン) → ADV
    4. ADV ツールバー初回検出 (3/5 アイコン) → ADV
    5. MOVIE 初回検出 (⏭ 必須) → MOVIE  ← 最後 (特定要素が最も少ない)
    6. それ以外 → UNKNOWN (フルOCR 必要)

    Returns: "MOVIE" | "BATTLE" | "ADV" | "UNKNOWN"
    """

    # MOVIE 慣性: 直前が動画アクション → 毎フレーム ⏭ ボタンで継続/終了を判定
    # TTL は使わない。⏭ が見えている限り MOVIE を維持 (一時停止含む)。
    # ⏭ が消えたら動画終了 → UNKNOWN に脱出。
    _MOVIE_ACTIONS = ("MOVIE_WAIT", "MOVIE_SKIP", "MOVIE_RESUME_TAP", "MOVIE_SKIP_ESCAPE")
    if state.last_action in _MOVIE_ACTIONS and img_path:
        # ── ポップアップ脱出 (MOVIE慣性より優先) ──
        # ただし phash 変化中 (動画再生中) はポップアップ誤検出の可能性が高いためスキップ
        # 動画の映像がドット+ぼかしに誤検出されるケースを防止
        _inertia_dots = count_page_dots(img_path)
        if _inertia_dots >= 2 and dist < PHASH_THRESHOLD:
            _img_blur = imread_cached(img_path)
            _is_popup = _img_blur is not None and _detect_background_blur(
                _img_blur, _img_blur.shape[0], _img_blur.shape[1])
            if _is_popup:
                logger.info("[SCENE_EARLY] MOVIE慣性中だがドット=%d+背景ぼかし+静止 → ポップアップ脱出", _inertia_dots)
                state.last_action = "SCENE_TAP"
                state.movie_wait_consecutive = 0; state.movie_static_count = 0
                return "UNKNOWN"

        # ── チュートリアル中のDL完了チェック: MOVIE脱出してフルOCRへ ──
        if state.download_active and not state.home_reached:
            logger.info("[MOVIE_INERTIA] download_active=True (チュートリアル) → MOVIE脱出 (DL完了チェック優先)")
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            # → detect_scene_early は UNKNOWN を返す → フルOCRへ

        # ── ハードリミット: 300回超 → 探索画面等の誤判定、強制脱出 ──
        if state.movie_wait_consecutive >= 300:
            logger.warning("[MOVIE_INERTIA] ハードリミット %d 回到達 → MOVIE強制脱出",
                           state.movie_wait_consecutive)
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_action = "SCENE_TAP"
            return "UNKNOWN"

        # ── phash 変化中 = 動画再生中 → 無条件で MOVIE 継続 (タップ厳禁) ──
        if dist >= PHASH_THRESHOLD:
            state.movie_static_count = 0  # 動的フレーム → 静止カウンタリセット
            logger.info("[MOVIE_INERTIA] phash_dist=%d (動画再生中) → MOVIE継続", dist)
            return "MOVIE"

        # ── 以下は画面静止時 (dist < PHASH_THRESHOLD) ──
        # 静止 = 動画終了ではない。他シーンへの遷移が確認できた場合のみ脱出する。
        state.movie_static_count += 1

        # ── ⏭ ボタン確認 → あれば動画継続 (一時停止 or 暗転シーン) ──
        _movie_chk = detect_movie_scene(
            img_path, adv_result=None, phash_dist=dist)
        if _movie_chk.is_movie and _movie_chk.has_skip_btn:
            return "MOVIE"

        # ── 他シーンへの遷移チェック: 肯定的な証拠がある場合のみ MOVIE 脱出 ──
        adv = detect_adv_scene(img_path, roi=state.game_roi)
        if adv.is_adv:
            logger.info("[MOVIE_INERTIA] ADVツールバー検出 → ADV遷移確定, MOVIE脱出")
            state.last_action = "SCENE_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state._movie_resume_used = False
        elif state.current_scene in ("BATTLE", "MENU"):
            logger.info("[MOVIE_INERTIA] BATTLE/MENU シーン → MOVIE脱出")
            state.last_action = "SCENE_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
        elif detect_adv_advance_icon(img_path):
            logger.info("[MOVIE_INERTIA] ↓ボタン検出 → ADV遷移確定, MOVIE脱出")
            state.last_action = "SCENE_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
        else:
            # 他シーンの証拠なし → MOVIE 継続 (暗転・字幕・クレジット等)
            logger.info("[MOVIE_INERTIA] ⏭未検出+静止 %d回 だが他シーン証拠なし → MOVIE継続",
                        state.movie_static_count)
            return "MOVIE"

    # BATTLE: 前回シーン == BATTLE + phash 小変化 (シーン継続)
    # 10回に1回テンプレートで実在確認 (Result画面等での誤BATTLE継続を防止)
    if state.current_scene == "BATTLE" and dist < 30:
        if state.battle_rapid_consecutive.count > 0 and state.battle_rapid_consecutive.count % 3 == 0:
            from tools.ap.image_proc import ASSET_MANAGER as _AM_verify
            _verify_roi = (int(ANALYSIS_W * 0.75), int(ANALYSIS_H * 0.60),
                           int(ANALYSIS_W * 0.25), int(ANALYSIS_H * 0.40))
            _v_atk = _AM_verify.match_single("battle_normal_attack", img_path, roi=_verify_roi)
            _v_skl = _AM_verify.match_single("battle_skill", img_path, roi=_verify_roi)
            _v_best = max((_v_atk[2] if _v_atk else 0), (_v_skl[2] if _v_skl else 0))
            if _v_best < 0.70:
                logger.info("[SCENE_EARLY] BATTLE継続チェック: テンプレ未検出 (best=%.2f) → UNKNOWN", _v_best)
                return "UNKNOWN"
        return "BATTLE"

    # BATTLE 初回/再検出: 右下の「通常攻撃」or「戦闘スキル」ボタンアイコンで判定
    # ADV ツールバーの AUTO/FF がバトル画面にも存在するため、ADV 判定より先に実行
    # NOTE: ADV 継続チェックより先に実行 — 一度 ADV と誤分類されても
    # バトルテンプレが見つかれば即 BATTLE に復帰する
    from tools.ap.image_proc import ASSET_MANAGER as _AM_battle
    try:
        _battle_roi = (int(ANALYSIS_W * 0.75), int(ANALYSIS_H * 0.60),
                       int(ANALYSIS_W * 0.25), int(ANALYSIS_H * 0.40))
        for _btn_name in ("battle_normal_attack", "battle_skill"):
            _battle_m = _AM_battle.match_single(_btn_name, img_path, roi=_battle_roi)
            if _battle_m and _battle_m[2] >= 0.65:
                logger.info("[SCENE_EARLY] Battle初回検出 (%s score=%.2f) → BATTLE",
                            _btn_name, _battle_m[2])
                return "BATTLE"
    except Exception:
        pass
    # BATTLE 補助判定: 金枠オーバーレイでテンプレが失敗する場合
    # 右下 (バトルボタン領域) に金枠が存在 → BATTLE 継続
    # ※初回 BATTLE 判定には使わない (ホーム画面のナビバー金枠で偽陽性)
    if state.current_scene == "BATTLE":
        try:
            from tools.ap.image_proc import find_gold_frame_near
            _battle_gold_cx = int(ANALYSIS_W * 0.88)
            _battle_gold_cy = int(ANALYSIS_H * 0.80)
            _bg_result = find_gold_frame_near(
                img_path, _battle_gold_cx, _battle_gold_cy, search_radius=200)
            if _bg_result is not None:
                _bg_cx, _bg_cy, _bg_w, _bg_h = _bg_result
                logger.info("[SCENE_EARLY] Battle補助: 右下金枠(%d,%d %dx%d) → BATTLE",
                            _bg_cx, _bg_cy, _bg_w, _bg_h)
                return "BATTLE"
        except Exception:
            pass

    # ADV 継続: 前回 ADV + phash 小変化 + AUTO アイコン + ADV ツールバー → ADV 高速パス
    # dist ガード: ADV→BATTLE 遷移時 (dist>=20) はフォールスルーして再評価する
    # NOTE: AUTO アイコン単独ではバトル画面の AUTO ボタンと区別不能 (score=0.91 で誤一致)
    #        → ADV ツールバー全体も確認して確定する
    if state.current_scene == "ADV" and dist < 20:
        from tools.ap.image_proc import ASSET_MANAGER as _AM_adv
        try:
            _auto_roi = (0, 0, ANALYSIS_W, int(ANALYSIS_H * 0.15))
            _auto_m = _AM_adv.match_single("adv_icon_auto", img_path, roi=_auto_roi)
            if _auto_m and _auto_m[2] >= 0.50:
                # AUTO あり → ADV ツールバーも確認 (バトル画面の AUTO 誤一致を排除)
                _adv_check = detect_adv_scene(img_path, roi=state.game_roi)
                if _adv_check.is_adv:
                    return "ADV"
                # ツールバーなし + AUTO あり → BATTLE の可能性。フォールスルー
                logger.info("[SCENE_EARLY] ADV継続: AUTO(%.2f)だがツールバーなし → UNKNOWN",
                            _auto_m[2])
        except Exception:
            pass

    # ADV: ツールバー検出 (MENU シーンは OCR でボタン検出が必要)
    if state.current_scene != "MENU":
        adv = detect_adv_scene(img_path, roi=state.game_roi)
        if adv.is_adv:
            return "ADV"

    # ── ポップアップ検出: MOVIE判定より先にチェック ──
    # ページドット≥2 AND 背景ぼかし → ポップアップ確定 → MOVIE にしない
    # (ドット単体はホーム画面UIアイコンで誤検出多い → 背景ぼかし必須)
    if img_path:
        _popup_dots = count_page_dots(img_path)
        if _popup_dots >= 3:
            _popup_img = imread_cached(img_path)
            _popup_blur = _popup_img is not None and _detect_background_blur(
                _popup_img, _popup_img.shape[0], _popup_img.shape[1])
            if _popup_blur:
                logger.info("[SCENE_EARLY] ドット=%d+背景ぼかし → ポップアップ確定, MOVIE判定スキップ", _popup_dots)
                return "UNKNOWN"

    # MOVIE 初回検出 (最後): 特定要素が最も少ないため他シーンを先に排除
    # チュートリアル中 + download_active → MOVIE 判定スキップ (DL完了ダイアログ優先)
    if state.download_active and not state.home_reached:
        logger.info("[SCENE_EARLY] download_active=True (チュートリアル) → MOVIE判定スキップ (DL完了ダイアログ優先)")
        return "UNKNOWN"
    if state.last_action not in _MOVIE_ACTIONS:
        _adv = detect_adv_scene(img_path, roi=state.game_roi)
        _movie = detect_movie_scene(img_path, adv_result=_adv, phash_dist=dist)
        if _movie.is_movie and _movie.has_skip_btn:
            logger.info("[SCENE_EARLY] Movie初回検出 (conf=%.2f, ⏭あり) → MOVIE", _movie.confidence)
            return "MOVIE"

    return "UNKNOWN"


def handle_movie(img_path: Path, state: PilotState, dist: int,
                 cur_phash: str) -> bool:
    """動画シーン専用ハンドラ。指アイコン / GoldSwipe / 金枠ボタン検出なし。

    - post_download + SKIP 表示 → DL完了後ループなので SKIP タップで脱出
    - それ以外の動画 → 待機のみ (タップで一時停止/再開ループに陥る)

    Returns: True if handled (caller should continue), False for fallthrough.
    """
    W, H = ANALYSIS_W, ANALYSIS_H

    # ── チュートリアル中のDL完了チェック: MOVIEハンドラをバイパス ──
    if state.download_active and not state.home_reached:
        logger.info("[MOVIE] download_active=True (チュートリアル) → MOVIEハンドラ脱出 (フルOCRへ)")
        state.movie_wait_consecutive = 0; state.movie_static_count = 0
        return False  # フルOCRへ

    # ── DL直後 + SKIP 表示 → DL完了後ループ脱出 ──
    # ダウンロード完了後に動画がループする現象: SKIP をタップして抜ける
    if state.post_download:
        _skip_pos = detect_movie_skip_button(img_path)
        if _skip_pos:
            _sk_x, _sk_y = roi_to_device(_skip_pos[0], _skip_pos[1], state.game_roi)
            logger.info("[MOVIE] DL直後 + SKIP検出 → SKIPタップ (%d,%d) でループ脱出", _sk_x, _sk_y)
            tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP")
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_phash = ""
            return True
        logger.info("[MOVIE] DL直後だが SKIP 未検出 → 待機")

    # ── 待機カウンタ ──
    state.movie_wait_consecutive += 1

    # ── 一時停止検知: phash 変化なし (静止) が連続 → 誤って一時停止された可能性 ──
    _MOVIE_PAUSE_THRESHOLD = 8  # 8回静止 (~4秒) で一時停止と判定
    if dist < PHASH_THRESHOLD:
        state.movie_static_count += 1
        # 動的フレームカウンタリセット (RESUME 成功判定用)
        state._movie_dynamic_frames = 0  # type: ignore[attr-defined]
    else:
        state.movie_static_count = 0  # 動画再生中 (画面変化あり) → リセット
        # RESUME 後に動的フレームが5回連続 → 本当に再生再開 → 空振りカウンタリセット
        _dyn = getattr(state, '_movie_dynamic_frames', 0) + 1
        state._movie_dynamic_frames = _dyn  # type: ignore[attr-defined]
        if _dyn >= 5:
            state._movie_resume_count = 0  # type: ignore[attr-defined]
            state._movie_dynamic_frames = 0  # type: ignore[attr-defined]

    if state.movie_static_count >= _MOVIE_PAUSE_THRESHOLD:
        # MOVIE_RESUME 連続空振りチェック: 3回タップしても静止に戻るなら動画ではない
        _resume_count = getattr(state, '_movie_resume_count', 0) + 1
        state._movie_resume_count = _resume_count  # type: ignore[attr-defined]
        if _resume_count >= 3:
            logger.warning("[MOVIE_PAUSE] RESUME %d 回空振り → 動画ではない、MOVIE強制脱出",
                           _resume_count)
            state.movie_static_count = 0
            state.movie_wait_consecutive = 0
            state._movie_resume_count = 0  # type: ignore[attr-defined]
            state.current_scene = "UNKNOWN"
            state.last_action = "SCENE_TAP"
            state.last_phash = ""
            return False  # MOVIE ハンドラ脱出 → フルOCRへ
        logger.warning("[MOVIE_PAUSE] 静止 %d 回連続 (dist=%d) → 一時停止と判定、タップで再開 (%d/3)",
                       state.movie_static_count, dist, _resume_count)
        tap_device(int(W * 0.5), int(H * 0.5), state, "MOVIE_RESUME")
        state.movie_static_count = 0
        state.last_phash = ""  # phash リセットして次フレームで変化を見る
        state.last_action = "MOVIE_WAIT"
        state.stall_start = 0.0
        time.sleep(1.0)
        return True

    # ── 長時間待機: ハードリミット (探索画面等の誤MOVIE判定を脱出) ──
    _MOVIE_HARD_LIMIT = 300  # ~3分: これ以上は動画ではない
    if state.movie_wait_consecutive >= _MOVIE_HARD_LIMIT:
        logger.warning("[MOVIE] ハードリミット %d 回到達 → 動画ではない、MOVIE強制脱出",
                       state.movie_wait_consecutive)
        state.movie_static_count = 0
        state.movie_wait_consecutive = 0
        state._movie_resume_count = 0  # type: ignore[attr-defined]
        state.current_scene = "UNKNOWN"
        state.last_action = "SCENE_TAP"
        state.last_phash = ""
        return False  # MOVIE ハンドラ脱出 → フルOCRへ

    if state.movie_wait_consecutive >= 30 and state.movie_wait_consecutive % 30 == 0:
        logger.info("[MOVIE] 長時間待機 %d 回 — 動画自動終了を待機中",
                    state.movie_wait_consecutive)

    # ── 通常待機 (動画は自動終了するのでタップせず待つ) ──
    logger.info("[MOVIE] 待機 (%d) dist=%d static=%d/%d",
                state.movie_wait_consecutive, dist,
                state.movie_static_count, _MOVIE_PAUSE_THRESHOLD)
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

    # ── バトルUIガード: 通常攻撃ボタンが見えなければ Result/ADV の可能性 → OCR へ ──
    # 3回に1回チェック (テンプレートマッチ ~10ms のコストは無視できる)
    if state.battle_rapid_consecutive.count > 0 and state.battle_rapid_consecutive.count % 3 == 0:
        _atk_m = ASSET_MANAGER.match_single("battle_normal_attack", analysis_path)
        if not _atk_m or _atk_m[2] < 0.70:
            logger.info("[BATTLE] 通常攻撃ボタン未検出 (count=%d) → OCR で再評価",
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

    # ── Phase 0: チュートリアル金枠 → 最優先タップ ──
    # 指ブロブ有無に関わらず金枠を常時チェック (~10ms)
    # scrcpy キャプチャでは指ブロブ面積が変動するためゲート緩和
    # NOTE: character_selected でもスキップしない — 金枠検出の extent<0.55 フィルタで
    # 通常のボタン発光と区別可能。ガードすると戦闘スキル等のチュートリアル金枠を見逃す。
    # BATTLE: 右半分のみ (左側キャラアイコンの菱形装飾を金枠と誤検出するため)
    # handle_battle() はバトル専用関数なので常に right_half_only=True
    # ── ただしチュートリアル暗転中は全画面探索 (速度ボタン等の左上UIも検出) ──
    _is_overlay = detect_tutorial_overlay(analysis_path)
    _gold_tap = detect_tutorial_gold_button_tap(
        analysis_path, right_half_only=True, overlay_mode=_is_overlay)
    if _gold_tap:
        _rapid_tx, _rapid_ty = _gold_tap
        # overlay_mode で見つけた金枠がキャラカード領域 (左下) なら偽検出 → スキップ
        if _is_overlay and _rapid_tx < ANALYSIS_W * 0.5 and _rapid_ty > ANALYSIS_H * 0.55:
            logger.info("[BATTLE_RAPID] overlay金枠 (%d,%d) はキャラカード領域 → スキップ",
                        _rapid_tx, _rapid_ty)
        else:
            _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"
            if _is_overlay:
                logger.info("[BATTLE_RAPID] 暗転オーバーレイ検出 → 全画面金枠探索で (%d,%d) 発見",
                            _rapid_tx, _rapid_ty)
    # フォールバック: detect_tutorial_gold_button_tap が条件で弾いた場合でも
    # 大面積ブロブ + find_gold_frame_near で金枠が見つかればそちらを使用
    # バトル: 右半分 (x>W/2) かつ y>35% のみ (左キャラアイコン・上部UI排除)
    # 暗転オーバーレイ中は全画面許可
    if not _rapid_action and _rapid_blobs:
        for _rb in _rapid_blobs:
            if _rb[2] >= 15000:  # 大面積ブロブ
                _gf = find_gold_frame_near(analysis_path, _rb[0], _rb[1], search_radius=200)
                if _gf is not None:
                    # バトル中: 右半分・下部のみ有効 (上部UI・左キャラ排除)
                    # 暗転オーバーレイ中はバイパス
                    if not _is_overlay and (_gf[0] < ANALYSIS_W * 0.5 or _gf[1] < ANALYSIS_H * 0.35):
                        logger.debug("[BATTLE_RAPID] 金枠フォールバック排除: gold(%d,%d) 左側/上部",
                                     _gf[0], _gf[1])
                        continue
                    _rapid_tx, _rapid_ty = _gf[0], _gf[1]
                    _rapid_action = "BATTLE_RAPID_GOLD_FRAME_FALLBACK"
                    logger.info("[BATTLE_RAPID] 金枠フォールバック: blob(%d,%d) → gold(%d,%d)",
                                _rb[0], _rb[1], _gf[0], _gf[1])
                    break

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
            # B-0: テンプレートで battle_skill / battle_normal_attack を探す (精度最優先)
            for _btn_name in ("battle_skill", "battle_normal_attack"):
                _btn_m = ASSET_MANAGER.match_single(_btn_name, analysis_path)
                if _btn_m and _btn_m[2] >= 0.60:
                    _tmpl_action = f"BATTLE_RAPID_TMPL_{_btn_name.upper()}"
                    logger.info("[BATTLE_RAPID] テンプレ %s (%.2f) → tap(%d,%d)",
                                _btn_name, _btn_m[2], _btn_m[0], _btn_m[1])
                    tap_device(_btn_m[0], _btn_m[1], state, _tmpl_action)
                    state.character_selected = False
                    state.char_just_selected = False
                    state.finger_detections += 1
                    state.battle_rapid_consecutive.tick()
                    return True
            # B-1: テンプレ未検出 → glow フォールバック
            if not _rapid_action and _rapid_right_g:
                _rr = max(_rapid_right_g, key=lambda g: g["area"])
                _rapid_tx = _rr["cx"]
                _rapid_ty = max(1, _rr["by"] + _rr["bh"] * 2 // 3)
                _rapid_action = "BATTLE_RAPID_GLOW_P2"
            elif not _rapid_action and _right_panel:
                _tb = max(_right_panel, key=lambda b: b[2])
                _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                _rapid_action = "BATTLE_RAPID_MOYA_P2"
            elif not _rapid_action:
                _rapid_tx, _rapid_ty = roi_to_device(
                    int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                _rapid_action = "BATTLE_RAPID_NORMATK_P2"

    # ── Phase C: フォールバック → 0.5秒待機+再確認 → 右側攻撃ボタン ──
    if not _rapid_action:
        if state.normatk_fallback.stalled:
            logger.info("[BATTLE] FALLBACK %d回連続 → OCR で再評価",
                        state.normatk_fallback.count)
            state.normatk_fallback.reset()
            return False
        # 敵ターン/アニメーション中の可能性 → 待機+再スクショで確認
        time.sleep(0.5)
        _retry_path, _retry_w, _retry_h, _ = take_screenshot()
        if _retry_path:
            _retry_analysis = prepare_analysis_image(_retry_path, _retry_w, _retry_h)
            _retry_active = detect_active_battle_char(
                _retry_analysis, ANALYSIS_W, ANALYSIS_H)
            if _retry_active:
                # プレイヤーターンに切り替わった → 再解析して正規パスへ
                logger.info("[BATTLE] 再確認でACTIVE_CHAR検出 → 次ループで正規処理")
                return True  # 次ループで handle_battle が再呼出される
        # テンプレマッチで正しいボタン位置を探す (character_selected 不問)
        for _fb_btn in ("battle_skill", "battle_normal_attack"):
            _fb_m = ASSET_MANAGER.match_single(_fb_btn, analysis_path)
            if _fb_m and _fb_m[2] >= 0.60:
                _rapid_tx, _rapid_ty = _fb_m[0], _fb_m[1]
                _rapid_action = f"BATTLE_RAPID_TMPL_{_fb_btn.upper()}"
                logger.info("[BATTLE_RAPID] FALLBACK テンプレ %s (%.2f) → tap(%d,%d)",
                            _fb_btn, _fb_m[2], _rapid_tx, _rapid_ty)
                break
        if not _rapid_action:
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
    adv = detect_adv_scene(img_path, roi=state.game_roi)
    _adv_tap_x = int(W * 0.93)
    _adv_tap_y = int(H * 0.91)

    # ── ADV ツールバー確認 → 1回タップ ──
    # NOTE: detect_adv_advance_icon() 単独ではバトル画面で偽陽性が出るため
    # ADVツールバー判定 (is_adv) を必須条件にする
    if adv.is_adv:
        if detect_adv_advance_icon(img_path):
            logger.info("[ADV] ↓検出 → タップ (%d,%d)", _adv_tap_x, _adv_tap_y)
            tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
            state.last_action = "ADV_RAPID_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_phash = ""
            return True
        elif adv.next_btn_pos:
            _adv_nx = int(adv.next_btn_pos[0] * W / actual_w)
            _adv_ny = int(adv.next_btn_pos[1] * H / actual_h)
            logger.info("[ADV] ↓ボタン座標 (%d,%d)", _adv_nx, _adv_ny)
            tap_device(_adv_nx, _adv_ny, state, "ADV_RAPID_TAP")
            state.last_action = "ADV_RAPID_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_phash = ""
            return True

    # ── ミニ会話タップ (1回) ──
    _mc = detect_mini_conversation(img_path)
    if _mc is not None:
        _mc_cx, _mc_cy, _mc_side = _mc
        logger.info("[ADV] 吹き出し(%s) → タップ (%d,%d)", _mc_side, _mc_cx, _mc_cy)
        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
        state.last_action = "MINI_CONV_TAP"
        state.movie_wait_consecutive = 0; state.movie_static_count = 0
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

    v2 改善点 (2026-03-13):
    - 画面ウェイク+ロック解除を最初に実行
    - uiautomator dump 前に stale XML を削除
    - uiautomator dump 結果の全テキストをログ出力 (診断用)
    - Play Store ページ読み込み待機フェーズ追加 (BACK 乱発防止)
    - 試行回数に応じた待機時間エスカレーション
    - 診断スクリーンショットを storage/fresh_install/ に保存
    - Google Play Protect / TOS 画面のハンドリング
    - フォアグラウンドアプリ検証 (BACK が Store を閉じた場合のリカバリ)
    """
    INSTALL_KEYWORDS = ["インストール", "Install", "install"]
    ACCEPT_KEYWORDS = ["同意する", "Accept", "OK", "続行", "Continue"]
    OPEN_KEYWORDS = ["開く", "Open", "プレイ", "Play"]  # uiautomator用 (完全一致)
    _OCR_OPEN_KEYWORDS = ["開く", "プレイ"]  # OCR用 (部分一致なので "Play"/"Open" は除外)
    # ページ読み込み中の兆候 (これらが見えたら待機)
    _LOADING_HINTS = ["読み込み中", "Loading", "接続しています", "Connecting"]
    # ポップアップ / ダイアログを閉じるべきキーワード
    _POPUP_DISMISS_KWS = ["後で", "後で行う", "スキップ", "いいえ", "No thanks",
                          "Not now", "No, thanks", "閉じる", "DISMISS",
                          "GOT IT", "OK", "了解"]
    MAX_ATTEMPTS = 15  # 10→15 に増加 (遅い接続対応)
    POLL_INTERVAL_SEC = 5
    MAX_POLL_COUNT = 120  # 5秒 × 120 = 10分 (大容量アプリ対応)
    _PNG_HEADER = b"\x89PNG\r\n\x1a\n"
    MAX_REINSTALL = 2  # 再アンインストール上限
    PLAY_STORE_PKG = "com.android.vending"

    # 診断スクリーンショット保存先
    _diag_dir = Path(__file__).parent.parent / "storage" / "fresh_install"
    _diag_dir.mkdir(parents=True, exist_ok=True)

    def _adb_tap(x: int, y: int) -> None:
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
            capture_output=True, timeout=5,
        )

    def _adb_key(keycode: str) -> None:
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", keycode],
            capture_output=True, timeout=5,
        )

    def _adb_screenshot(path: str) -> bool:
        """スクリーンショット取得 + PNG ヘッダ検証。3回リトライ。"""
        for _ss_try in range(3):
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=10,
                )
                if (r.returncode == 0 and len(r.stdout) >= 10_000
                        and r.stdout[:8] == _PNG_HEADER):
                    Path(path).write_bytes(r.stdout)
                    return True
            except (subprocess.TimeoutExpired, Exception):
                pass
            time.sleep(0.5)
        return False

    def _save_diag_screenshot(label: str) -> None:
        """診断用スクリーンショットを保存 (失敗しても無視)。"""
        ts = time.strftime("%H%M%S")
        path = str(_diag_dir / f"{label}_{ts}.png")
        if _adb_screenshot(path):
            logger.info("[FRESH_INSTALL] 診断SS保存: %s", path)

    def _get_device_screen_height() -> int:
        """wm size でデバイス画面の高さを取得 (portrait 基準)。"""
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"(\d+)x(\d+)", r.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                return max(w, h)
        except Exception as e:
            logger.debug("[FRESH_INSTALL] wm size 取得失敗: %s", e)
        return 1920  # 安全なフォールバック (FHD 相当)

    def _is_play_store_foreground() -> bool:
        """Play Store がフォアグラウンドにあるか確認。"""
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "window", "displays"],
                capture_output=True, text=True, timeout=5,
            )
            return PLAY_STORE_PKG in r.stdout
        except Exception:
            return False

    def _ensure_play_store_open() -> None:
        """Play Store がフォアグラウンドにない場合、再表示する。"""
        if not _is_play_store_foreground():
            logger.warning("[FRESH_INSTALL] Play Store がフォアグラウンドにない → 再表示")
            open_play_store(serial, package)
            time.sleep(5)

    def _uiautomator_dump_xml() -> Optional[str]:
        """uiautomator dump を実行し XML テキストを返す。stale データ防止付き。"""
        try:
            # stale XML を削除してからダンプ
            subprocess.run(
                ["adb", "-s", serial, "shell", "rm", "-f", "/sdcard/ui.xml"],
                capture_output=True, timeout=5,
            )
            dr = subprocess.run(
                ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                capture_output=True, timeout=15, text=True,
            )
            # dump 成功判定: 出力に "dumped to" が含まれる
            if "dumped" not in dr.stdout.lower() and "dumped" not in dr.stderr.lower():
                logger.debug("[FRESH_INSTALL] uiautomator dump 応答なし: stdout=%s stderr=%s",
                             dr.stdout.strip()[:80], dr.stderr.strip()[:80])
                return None
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "cat", "/sdcard/ui.xml"],
                capture_output=True, timeout=10, text=True,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            return r.stdout
        except Exception as e:
            logger.warning("[FRESH_INSTALL] uiautomator dump 失敗: %s", e)
            return None

    def _uiautomator_find_button(keywords: list, xml_text: str = None) -> Optional[tuple]:
        """uiautomator XML からボタン中心座標を取得。

        xml_text が渡された場合はそれを使う (dump 不要)。
        """
        import xml.etree.ElementTree as ET
        if xml_text is None:
            xml_text = _uiautomator_dump_xml()
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as _pe:
            logger.debug("[FRESH_INSTALL] XML パースエラー: %s", _pe)
            return None

        for kw in keywords:
            for node in root.iter("node"):
                text_val = node.get("text", "")
                desc_val = node.get("content-desc", "")
                bounds_str = node.get("bounds", "")
                matched_text = ""
                if kw in text_val:
                    matched_text = text_val
                elif kw in desc_val:
                    matched_text = desc_val
                else:
                    continue
                # 「インストール」→「アンインストール」除外
                if kw == "インストール" and "アン" in matched_text:
                    continue
                bm = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
                if len(bm) < 2:
                    continue
                x1, y1 = int(bm[0][0]), int(bm[0][1])
                x2, y2 = int(bm[1][0]), int(bm[1][1])
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                logger.info("[FRESH_INSTALL] uiautomator '%s' (text='%s'): "
                            "bounds=[%d,%d][%d,%d] → (%d,%d)",
                            kw, matched_text[:30], x1, y1, x2, y2, cx, cy)
                return (cx, cy)
        return None

    def _log_ui_texts(xml_text: str) -> list[str]:
        """uiautomator XML から全テキストを抽出してログ出力 (診断用)。"""
        import xml.etree.ElementTree as ET
        texts = []
        try:
            root = ET.fromstring(xml_text)
            for node in root.iter("node"):
                t = node.get("text", "").strip()
                if t:
                    texts.append(t)
        except ET.ParseError:
            pass
        if texts:
            logger.info("[FRESH_INSTALL] UI texts: %s", texts[:20])
        return texts

    def _verify_install_started() -> bool:
        """インストール開始を検証 (5秒待ち + 3回リトライ)。"""
        for _vt in range(3):
            time.sleep(5)
            xml = _uiautomator_dump_xml()
            if xml:
                # 1) 進捗系キーワード
                _progress = _uiautomator_find_button(
                    ["インストール中", "キャンセル", "Cancel",
                     "Installing", "Pending", "ダウンロード中", "Downloading"],
                    xml_text=xml)
                if _progress:
                    logger.info("[FRESH_INSTALL] インストール開始を確認 (進捗検出, 試行%d)", _vt + 1)
                    return True
                # 2) 「インストール」ボタン消失
                if _uiautomator_find_button(INSTALL_KEYWORDS, xml_text=xml) is None:
                    logger.info("[FRESH_INSTALL] インストール開始を確認 (ボタン消失, 試行%d)", _vt + 1)
                    return True
            # 3) pm で既にインストール済み
            if is_app_installed(serial, package):
                logger.info("[FRESH_INSTALL] インストール完了を確認 (pm, 試行%d)", _vt + 1)
                return True
            logger.debug("[FRESH_INSTALL] 検証リトライ %d/3", _vt + 1)
        return False

    def _handle_accept_dialogs() -> None:
        """権限/同意ダイアログを最大5回リトライで処理。"""
        for _ad in range(5):
            time.sleep(2)
            xml = _uiautomator_dump_xml()
            if xml:
                _acc_pos = _uiautomator_find_button(ACCEPT_KEYWORDS, xml_text=xml)
                if _acc_pos:
                    logger.info("[FRESH_INSTALL] 権限ダイアログ → タップ (%d,%d) [%d回目]",
                                _acc_pos[0], _acc_pos[1], _ad + 1)
                    _adb_tap(_acc_pos[0], _acc_pos[1])
                    continue
                # ポップアップ解除キーワード
                _dismiss_pos = _uiautomator_find_button(_POPUP_DISMISS_KWS, xml_text=xml)
                if _dismiss_pos:
                    logger.info("[FRESH_INSTALL] ポップアップ解除 → タップ (%d,%d) [%d回目]",
                                _dismiss_pos[0], _dismiss_pos[1], _ad + 1)
                    _adb_tap(_dismiss_pos[0], _dismiss_pos[1])
                    continue
            # uiautomator で見つからない → OCR フォールバック
            tmp_ss = str(Path(tempfile.gettempdir()) / "fresh_install_ss.png")
            if _adb_screenshot(tmp_ss):
                try:
                    ocr2 = run_ocr(tmp_ss)
                    _found = False
                    for akw in ACCEPT_KEYWORDS:
                        ahit = find_best(ocr2, akw)
                        if ahit:
                            ax, ay = ahit["center"]
                            logger.info("[FRESH_INSTALL] OCR「%s」→ タップ (%d,%d)", akw, ax, ay)
                            _adb_tap(ax, ay)
                            _found = True
                            break
                    if _found:
                        continue
                except Exception:
                    pass
            logger.debug("[FRESH_INSTALL] 権限ダイアログなし [%d回目] → 完了", _ad + 1)
            break

    def _try_dismiss_popup(xml: str) -> bool:
        """Play Games / Protect / TOS 等のポップアップを検出して閉じる。
        閉じた場合 True を返す。"""
        # uiautomator でポップアップ閉じるボタンを探す
        _dismiss = _uiautomator_find_button(_POPUP_DISMISS_KWS, xml_text=xml)
        if _dismiss:
            logger.info("[FRESH_INSTALL] ポップアップ検出 → dismiss タップ (%d,%d)",
                        _dismiss[0], _dismiss[1])
            _adb_tap(_dismiss[0], _dismiss[1])
            time.sleep(2)
            return True
        return False

    # ─── 実行開始 ───

    logger.info("[FRESH_INSTALL] === アプリ再インストール開始 ===")

    # --- Step 0: 画面ウェイク + ロック解除 ---
    _adb_key("KEYCODE_WAKEUP")
    time.sleep(1)
    # スワイプでロック解除 (パスワードなし前提)
    subprocess.run(
        ["adb", "-s", serial, "shell", "input", "swipe", "360", "1200", "360", "400", "300"],
        capture_output=True, timeout=5,
    )
    time.sleep(1)

    # --- Step 1: アンインストール ---
    uninstall_app(serial, package)
    time.sleep(2)

    # --- Step 2: Play Store を開く ---
    if not open_play_store(serial, package):
        logger.error("[FRESH_INSTALL] Play Store を開けませんでした。手動で対応してください。")
        return
    time.sleep(5)

    # デバイス画面高さを動的取得 (Y座標フィルタ用)
    _screen_h = _get_device_screen_height()
    logger.info("[FRESH_INSTALL] デバイス画面高さ (portrait): %dpx", _screen_h)

    # --- Step 2.5: Play Store ページ読み込み待機 ---
    # 初回は十分に待つ (BACK 乱発でページロードが途切れるのを防止)
    # uiautomator + OCR 両方で読み込み完了を検出 (Play Store は WebView ベースのため uiautomator が効かない場合がある)
    _PAGE_READY_KWS = ["インストール", "Install", "install",
                       "開く", "Open", "プレイ", "Play", "更新", "Update"]
    logger.info("[FRESH_INSTALL] Play Store ページ読み込み待機中...")
    _page_loaded = False
    for _wait in range(6):  # 最大 30 秒 (5秒 × 6)
        # --- uiautomator で確認 ---
        xml = _uiautomator_dump_xml()
        if xml:
            ui_texts = _log_ui_texts(xml)
            if any(kw in t for t in ui_texts for kw in _PAGE_READY_KWS):
                logger.info("[FRESH_INSTALL] ページ読み込み完了 [uiautomator] (%d秒)", (_wait + 1) * 5)
                _page_loaded = True
                break
            if _try_dismiss_popup(xml):
                continue
            if any(kw in t for t in ui_texts for kw in _LOADING_HINTS):
                logger.info("[FRESH_INSTALL] 読み込み中... (%d秒)", (_wait + 1) * 5)
                time.sleep(5)
                continue
        else:
            logger.debug("[FRESH_INSTALL] uiautomator dump 失敗 — OCR フォールバック")
        # --- OCR フォールバック ---
        _wait_ss = str(Path(tempfile.gettempdir()) / "fresh_install_wait.png")
        if _adb_screenshot(_wait_ss):
            try:
                _wait_ocr = run_ocr(_wait_ss)
                _wait_texts = [r["text"] for r in _wait_ocr]
                logger.info("[FRESH_INSTALL] OCR (待機中): %s", _wait_texts[:10])
                if any(kw in t for t in _wait_texts for kw in _PAGE_READY_KWS):
                    logger.info("[FRESH_INSTALL] ページ読み込み完了 [OCR] (%d秒)", (_wait + 1) * 5)
                    _page_loaded = True
                    break
            except Exception:
                pass
        time.sleep(5)

    if not _page_loaded:
        logger.warning("[FRESH_INSTALL] ページ読み込みタイムアウト — インストール検出を開始")
        _save_diag_screenshot("page_load_timeout")

    # --- Step 3: インストールボタン検出 + タップ ---
    tmp_ss = str(Path(tempfile.gettempdir()) / "fresh_install_ss.png")
    installed_via_tap = False
    _reinstall_count = 0

    for attempt in range(MAX_ATTEMPTS):
        # 待機時間エスカレーション: 試行5回目以降は長めに待つ
        if attempt > 0:
            _wait_sec = 3 if attempt < 5 else 5 if attempt < 10 else 8
            time.sleep(_wait_sec)

        logger.info("[FRESH_INSTALL] 試行 %d/%d", attempt + 1, MAX_ATTEMPTS)

        # Play Store がフォアグラウンドにあるか確認
        _ensure_play_store_open()

        # uiautomator dump (1回だけ取得して使い回す)
        xml = _uiautomator_dump_xml()

        if xml:
            ui_texts = _log_ui_texts(xml)

            # --- 0th-pre: ダウンロード進行中チェック (再タップ防止) ---
            _dl_progress_kws = ["キャンセル", "Cancel", "インストール中", "Installing",
                                "Pending", "ダウンロード中", "Downloading", "待機中"]
            _is_downloading = any(
                kw in t for t in ui_texts for kw in _dl_progress_kws
            )
            if _is_downloading:
                logger.info("[FRESH_INSTALL] ダウンロード進行中 (uiautomator) → タップせず待機")
                if _verify_install_started():
                    installed_via_tap = True
                    break
                continue

            # --- 0th: 既インストール済みチェック ---
            _ui_open = _uiautomator_find_button(OPEN_KEYWORDS, xml_text=xml)
            if _ui_open:
                if _reinstall_count >= MAX_REINSTALL:
                    logger.warning("[FRESH_INSTALL] 再アンインストール上限(%d回)到達 → BACK + 再表示",
                                   MAX_REINSTALL)
                    _adb_key("4")
                    time.sleep(2)
                    open_play_store(serial, package)
                    time.sleep(5)
                    continue
                logger.info("[FRESH_INSTALL] 既インストール済み（'プレイ'/'開く'検出）→ 再アンインストール (%d/%d)",
                            _reinstall_count + 1, MAX_REINSTALL)
                _reinstall_count += 1
                uninstall_app(serial, package)
                time.sleep(2)
                open_play_store(serial, package)
                time.sleep(5)
                continue

            # --- 1st: uiautomator でインストールボタン ---
            _ui_pos = _uiautomator_find_button(INSTALL_KEYWORDS, xml_text=xml)
            if _ui_pos:
                if _ui_pos[1] < _screen_h * 0.75:
                    _adb_tap(_ui_pos[0], _ui_pos[1])
                    if _verify_install_started():
                        installed_via_tap = True
                        break
                    logger.warning("[FRESH_INSTALL] uiautomator タップ空振り — OCR フォールバック")
                else:
                    logger.debug("[FRESH_INSTALL] uiautomator (%d,%d) y>75%% → 他端末ボタン除外",
                                 _ui_pos[0], _ui_pos[1])

            # --- ポップアップ / ダイアログを閉じる試行 ---
            if _try_dismiss_popup(xml):
                continue

        # --- 2nd: OCR フォールバック ---
        if not _adb_screenshot(tmp_ss):
            logger.warning("[FRESH_INSTALL] スクリーンショット取得失敗 — リトライ")
            continue

        try:
            ocr_results = run_ocr(tmp_ss)
        except Exception as e:
            logger.warning("[FRESH_INSTALL] OCR 失敗: %s", e)
            continue

        _ocr_texts = [r["text"] for r in ocr_results]
        logger.info("[FRESH_INSTALL] OCR texts: %s", _ocr_texts[:15])

        # 「開く」「プレイ」検出 → pm 確認 → 再アンインストール
        _ocr_open_hit = False
        for kw in _OCR_OPEN_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                if is_app_installed(serial, package):
                    if _reinstall_count >= MAX_REINSTALL:
                        logger.warning("[FRESH_INSTALL] 再アンインストール上限到達 → スキップ")
                    else:
                        logger.info("[FRESH_INSTALL] 「%s」+ pm確認 → 再アンインストール (%d/%d)",
                                    kw, _reinstall_count + 1, MAX_REINSTALL)
                        _reinstall_count += 1
                        uninstall_app(serial, package)
                        time.sleep(2)
                        open_play_store(serial, package)
                        time.sleep(5)
                        _ocr_open_hit = True
                else:
                    logger.info("[FRESH_INSTALL] 「%s」検出だがpm未確認 → 継続", kw)
                break
        if _ocr_open_hit:
            continue

        # --- ダウンロード進行中チェック (OCR) ---
        _DL_PROGRESS_OCR_KWS = ["キャンセル", "Cancel", "MB", "ダウンロード中",
                                "インストール中", "Installing", "Downloading"]
        _ocr_downloading = any(
            kw in t for t in _ocr_texts for kw in _DL_PROGRESS_OCR_KWS
        )
        if _ocr_downloading:
            logger.info("[FRESH_INSTALL] ダウンロード進行中 (OCR) → タップせず待機")
            if _verify_install_started():
                installed_via_tap = True
                break
            continue

        # --- エラーダイアログ検出 (「インストールできません」) → BACK + 再表示 ---
        _install_error = any("できません" in t for t in _ocr_texts)
        if _install_error:
            logger.info("[FRESH_INSTALL] エラーダイアログ検出 → BACK + Play Store 再表示")
            _save_diag_screenshot(f"install_error_{attempt}")
            _adb_key("4")  # BACK
            time.sleep(2)
            open_play_store(serial, package)
            time.sleep(5)
            continue

        # 「インストール」を OCR 検出
        _install_found = False
        for kw in INSTALL_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                # 「アンインストール」「インストールできません」除外
                if "アン" in hit["text"] or "できません" in hit["text"]:
                    continue
                cx, cy = hit["center"]
                logger.info("[FRESH_INSTALL] OCR「%s」検出 → タップ (%d,%d)", kw, cx, cy)
                _adb_tap(cx, cy)
                if _verify_install_started():
                    installed_via_tap = True
                _install_found = True
                break
        if installed_via_tap:
            break
        if _install_found:
            continue  # タップしたが検証失敗 → リトライ

        # インストールボタン未検出 — BACK は最終手段 (試行 8回目以降のみ)
        if attempt >= 7:
            logger.info("[FRESH_INSTALL] インストールボタン未検出 → BACK + Play Store 再表示")
            _save_diag_screenshot(f"no_install_btn_{attempt}")
            _adb_key("4")
            time.sleep(2)
            open_play_store(serial, package)
            time.sleep(5)
        else:
            logger.info("[FRESH_INSTALL] インストールボタン未検出 — ページ読み込み待機 (試行%d)", attempt + 1)
            _save_diag_screenshot(f"waiting_{attempt}")

    # --- 権限ダイアログ処理 ---
    if installed_via_tap:
        _handle_accept_dialogs()
    else:
        logger.error("[FRESH_INSTALL] %d回の試行でインストールボタンをタップできませんでした", MAX_ATTEMPTS)
        _save_diag_screenshot("final_failure")
        # 最終手段: pm install-existing (キャッシュ残りで復活することがある)
        logger.info("[FRESH_INSTALL] pm install-existing を試行...")
        subprocess.run(
            ["adb", "-s", serial, "shell", "pm", "install-existing", package],
            capture_output=True, timeout=30,
        )

    # --- Step 4: インストール完了ポーリング ---
    if not is_app_installed(serial, package):
        logger.info("[FRESH_INSTALL] インストール完了を待機中... (最大%d秒)",
                    POLL_INTERVAL_SEC * MAX_POLL_COUNT)
        for i in range(MAX_POLL_COUNT):
            if is_app_installed(serial, package):
                logger.info("[FRESH_INSTALL] インストール完了を確認 (%d秒経過)",
                            (i + 1) * POLL_INTERVAL_SEC)
                break
            # 30秒毎にダイアログチェック (Play Protect 等がインストールを止めている可能性)
            if (i + 1) % 6 == 0:
                xml = _uiautomator_dump_xml()
                if xml:
                    _try_dismiss_popup(xml)
                    _handle_accept_dialogs()
            time.sleep(POLL_INTERVAL_SEC)
        else:
            logger.error("[FRESH_INSTALL] タイムアウト — 手動でインストールを完了してください")
            _save_diag_screenshot("install_timeout")
            return
    else:
        logger.info("[FRESH_INSTALL] 既にインストール完了済み")

    # --- Step 5: Play Store を閉じる ---
    time.sleep(2)
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "force-stop", PLAY_STORE_PKG],
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

    # ─── scrcpy 早期起動: fresh-install 含めた全工程を画面で確認可能にする ───
    _scrcpy_proc = manage_scrcpy()
    if _scrcpy_proc is not None:
        logger.info("[SCRCPY] ウィンドウ生成待ち (3秒)...")
        time.sleep(3)

    # ─── --fresh-install: アンインストール → Play Store 再インストール ───
    if args.fresh_install:
        # 永続状態をクリア (新規アカウントでは前回の状態は無効)
        try:
            conn = sqlite3.connect(str(_STATE_DB_PATH))
            conn.execute("DELETE FROM auto_pilot_state")
            conn.commit()
            conn.close()
            logger.info("[PERSIST] fresh-install → auto_pilot_state テーブルクリア")
        except Exception:
            pass
        _fresh_install_from_play_store(_ap_device.DEVICE_SERIAL, APP_PACKAGE)

    # ─── ゲーム未インストール → 自動インストール ───
    if not is_app_installed(_ap_device.DEVICE_SERIAL, APP_PACKAGE):
        logger.info("[AUTO_INSTALL] ゲーム '%s' 未インストール → Play Store から自動インストール", APP_PACKAGE)
        _fresh_install_from_play_store(_ap_device.DEVICE_SERIAL, APP_PACKAGE)
        if not is_app_installed(_ap_device.DEVICE_SERIAL, APP_PACKAGE):
            logger.error("[ABORT] 自動インストール失敗。手動でインストールしてから再実行してください。")
            sys.exit(1)
        # 新規インストール = fresh start 扱い
        args.fresh_install = True

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
    state.is_fresh_start = args.fresh_install
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # ── SQLite から永続状態を復元 ──
    if load_state("post_download") == "1":
        state.post_download = True
        logger.info("[PERSIST] post_download=True を復元 (前回 DL 中断からの再開)")

    # ─── Ctrl+C シグナルハンドラ登録 (レポート自動生成) ───
    global _pilot_state_ref
    _pilot_state_ref = state

    def _sigint_handler(signum, frame):
        logger.info("\n[Ctrl+C] 手動停止 — レポートを生成します...")
        generate_and_copy_report(_pilot_state_ref, "手動停止 (Ctrl+C / SIGINT)")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    # ─── scrcpy 再確認: fresh-install 中に死んだ場合のリカバリ ───
    if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
        logger.info("[SCRCPY] fresh-install 中に終了 → 再起動")
        _scrcpy_proc = manage_scrcpy()
        if _scrcpy_proc is not None:
            time.sleep(3)

    # 実機物理解像度を取得してログ出力
    _dev_w, _dev_h = get_device_resolution()
    logger.info("[DEVICE_RES] wm size: %dx%d / 解析基準: %dx%d (ROI補正で座標変換)",
                _dev_w, _dev_h, ANALYSIS_W, ANALYSIS_H)

    # ─── 初回アプリ起動: mCurrentFocus で正確な前面アプリ判定 ───
    # NOTE: mResumedActivity はバックグラウンドスタックも含む複数行を返すことがあり
    #       誤って「既に前面」と判定する問題があった。mCurrentFocus は1行のみ確実。
    try:
        adb("shell input keyevent KEYCODE_WAKEUP")
        time.sleep(0.5)
        # ロック画面をスワイプ解除 (PIN/パターンなし端末)
        adb("shell input keyevent 82")  # KEYCODE_MENU でロック解除
        time.sleep(0.5)
        adb("shell input swipe 540 1800 540 500 300")  # スワイプ解除
        time.sleep(0.5)
        _focus = adb("shell dumpsys window | grep mCurrentFocus")
        if APP_PACKAGE not in _focus:
            logger.info("[STARTUP] アプリが前面にない → am start で起動します (mCurrentFocus: %s)",
                        _focus.strip())
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
            # ポーリングで起動確認 (mCurrentFocus が空の端末もあるため ps も併用)
            _app_started = False
            for _poll in range(10):
                time.sleep(2)
                _focus2 = adb("shell dumpsys window | grep mCurrentFocus")
                if APP_PACKAGE in _focus2:
                    logger.info("[STARTUP] アプリ前面確認 (%.1f秒)", (_poll + 1) * 2)
                    _app_started = True
                    break
                # mCurrentFocus が空でもプロセスが起動していれば OK
                _ps = adb(f"shell pidof {APP_PACKAGE}")
                if _ps.strip():
                    logger.info("[STARTUP] アプリプロセス検出 PID=%s (%.1f秒)",
                                _ps.strip(), (_poll + 1) * 2)
                    _app_started = True
                    break
                logger.info("[STARTUP] 起動待ち (%d/10)... mCurrentFocus: %s",
                            _poll + 1, _focus2.strip())
            if not _app_started:
                # 10回失敗 → 再度 am start (Play Store 終了直後のタイミング問題対策)
                logger.warning("[STARTUP] 10回待機後もアプリ未検出 → am start を再試行")
                adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
                time.sleep(5)
        else:
            logger.info("[STARTUP] アプリ既に前面: %s", APP_PACKAGE)
    except Exception as _e:
        logger.warning("[STARTUP] フォーカス確認失敗: %s — am start で起動を試行", _e)
        adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(5)

    _log_milestone(state, "APP_LAUNCH")
    logger.info("[TOKEN_SAVE] 節約モード稼働中。バトル発光検知で OCR スキップ → 爆速モードで進行します")

    # ─── ランドスケープ待機: ポートレートならアプリ起動待ち ───
    for _orient_wait in range(10):
        _ss_check = take_screenshot()
        if _ss_check[0] is not None and _ss_check[1] > _ss_check[2]:
            logger.info("[STARTUP] ランドスケープ確認 (%dx%d)", _ss_check[1], _ss_check[2])
            break
        logger.info("[STARTUP] ポートレート検出 (%dx%d) — アプリ起動待ち (%d/10)",
                    _ss_check[1], _ss_check[2], _orient_wait + 1)
        if _orient_wait == 4:
            logger.info("[STARTUP] アプリ再起動を試行")
            adb(f"shell am force-stop {APP_PACKAGE}")
            time.sleep(2)
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(2)

    for i in range(MAX_ITERATIONS):
        state.iteration = i
        _loop_t0 = time.time()  # [PERF] ループ開始時刻
        clear_imread_cache()    # 前イテレーションのキャッシュを破棄

        # ── 定期健診 (100 iter ごと) ──
        if i > 0 and i % 100 == 0:
            logger.info("[WATCHDOG] Periodic check (iter=%d). Running physical diagnostics...", i)
            if not check_adb_liveness():
                logger.warning("[WATCHDOG] Periodic check FAILED → attempting reconnect")
                subprocess.run(["adb", "kill-server"], timeout=5)
                time.sleep(2)
                subprocess.run(["adb", "start-server"], timeout=5)
                time.sleep(2)
                # USB シリアル (`:` なし) には adb connect 不要 (DNS解決失敗する)
                if _ap_device.DEVICE_SERIAL and ":" in _ap_device.DEVICE_SERIAL:
                    subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5)
                    time.sleep(1)
                # scrcpy が ADB 再起動で死んだ場合は再起動
                if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] WATCHDOG ADB reconnect 後に再起動")
                    _scrcpy_proc = manage_scrcpy()
            else:
                logger.info("[WATCHDOG] Periodic check OK")

        # ── フォアグラウンドアプリ監視 (20 iter 毎) ──
        # Chrome 等が誤起動していたら am start でゲームに復帰
        if i > 0 and i % 20 == 0:
            if check_foreground_app():
                logger.info("[FOREGROUND] ゲーム復帰完了 → 次イテレーションへ")
                state.last_phash = ""
                state.same_phash_count = 0
                continue

        # ── 0.5) キャプチャ前クールダウン ──
        # a) タップ後: MIN_TAP_INTERVAL 未満なら残り時間を待つ
        #    安全弁: 最大 3.0s キャップ (不具合で無限停止しない)
        if state.last_action_time > 0:
            _cooldown_remaining = MIN_TAP_INTERVAL - (time.time() - state.last_action_time)
            if 0 < _cooldown_remaining <= 3.0:
                time.sleep(_cooldown_remaining)
        # b) キャプチャ間隔: MIN_CAPTURE_INTERVAL 未満なら残り時間を待つ
        #    scrcpy 高速キャプチャ (~100ms) でのCPU/メモリ浪費を抑制
        if state.last_capture_time > 0:
            _cap_remaining = MIN_CAPTURE_INTERVAL - (time.time() - state.last_capture_time)
            if 0 < _cap_remaining <= 3.0:
                time.sleep(_cap_remaining)

        # ── 1) スクリーンショット取得 ──
        img_path, actual_w, actual_h, _ss_retries = take_screenshot()
        state.last_capture_time = time.time()
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
        # ── Portrait 検出: ブラウザ等の外部アプリ → BACK キーで復帰 ──
        if actual_w > 0 and actual_w < actual_h:
            logger.warning("[PORTRAIT] 縦画面 (%dx%d) → BACK キー", actual_w, actual_h)
            adb("shell input keyevent 4")  # KEYCODE_BACK
            time.sleep(1.5)
            state.last_phash = ""
            continue
        # メモリ上に最新画像を保持 + ROI更新 (スロットル: 画面変化時 or 50iter毎)
        try:
            _cached_bgr = pop_last_scrcpy_bgr()
            state.last_screen = _cached_bgr if _cached_bgr is not None else imread_cached(img_path)
            _roi_needed = (state.game_roi is None or i % 50 == 0
                           or state.same_phash_count == 0)  # phash変化直後
            if state.last_screen is not None and _roi_needed:
                _new_roi = detect_game_roi(state.last_screen)
                # 非黒画面のときのみ ROI を更新 (暗転中は前の ROI を維持)
                _img_h, _img_w = state.last_screen.shape[:2]
                if _new_roi[2] >= _img_w * 0.5:
                    # 画像空間 → 解析空間に正規化
                    # NOTE: _img_w は scrcpy (~1440) or adb (2160) で異なるため
                    #        actual_w (wm size) ではなく実画像サイズを使う
                    if _img_w != ANALYSIS_W or _img_h != ANALYSIS_H:
                        _sx = ANALYSIS_W / _img_w
                        _sy = ANALYSIS_H / _img_h
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

        # ── 1.5) フォアグラウンドアプリ監視 (20iter毎 ≈ ~40秒) ──
        if i > 0 and i % 20 == 0:
            if check_foreground_app():
                logger.info("[FOREGROUND_GUARD] 別アプリ検出 → ゲーム復帰、次ループへ")
                state.last_phash = ""
                time.sleep(3.0)
                continue

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            state.total_blackout_skipped += 1
            state.consecutive_blackouts += 1
            # ── 暗い ADV シーン救済: 連続5回 (~2.5秒) で画面中央タップ → 脱出試行 ──
            # 暗いが内容がある画面 (p90≈5-15) を真の暗転 (p90≈0-3) と区別
            # MOVIE シーン中は暗いシーンが頻発するためタップ禁止 (一時停止してしまう)
            if state.consecutive_blackouts == 5:
                if state.current_scene == "MOVIE" or state.movie_wait_consecutive > 0:
                    logger.info("[DARK_SCENE_ESCAPE] 連続暗転 %d 回だが MOVIE中/直後 (scene=%s, mwc=%d) → タップ抑制",
                                state.consecutive_blackouts, state.current_scene,
                                state.movie_wait_consecutive)
                    state.consecutive_blackouts = 0
                else:
                    logger.info("[DARK_SCENE_ESCAPE] 連続暗転 %d 回 → 暗い ADV 疑い、画面中央タップで脱出試行",
                                state.consecutive_blackouts)
                    tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "DARK_SCENE_TAP")
                    state.consecutive_blackouts = 0
                    state.last_phash = ""
                    state.same_phash_count = 0
                    time.sleep(1.0)
                    continue
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転 — 3s 待機 (連続: %d)",
                            i, state.consecutive_blackouts)
            # ── 暗転復帰 ──
            # 連続30回超 → スリープ延長 (0.5s→3.0s) + フォアグラウンドチェック
            if state.consecutive_blackouts >= 30:
                if state.consecutive_blackouts % 10 == 0:
                    # 画面消灯の可能性 → フォアグラウンドチェック + WAKEUP
                    if check_foreground_app():
                        logger.info("[BLACKOUT_RECOVER] 別アプリ前面 → ゲーム復帰")
                    else:
                        logger.info("[BLACKOUT_RECOVER] 連続暗転 %d 回 → WAKEUP + 画面中央タップ",
                                    state.consecutive_blackouts)
                        adb("shell input keyevent KEYCODE_WAKEUP")
                        time.sleep(0.5)
                        tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "BLACKOUT_RECOVER")
                else:
                    time.sleep(3.0)  # 暗転ポーリング延長 (コールドスタート最適化)
            else:
                time.sleep(0.5)
            state.last_phash = ""
            state.same_phash_count = 0
            continue

        # ── 暗転解除 ──
        if state.consecutive_blackouts > 0:
            state.consecutive_blackouts = 0

        # ── 2.5) ダウンロード/ロード中ショートカット ──
        # 前回アクションが DOWNLOAD_WAIT/LOADING_WAIT → phash/シーン判定をスキップ
        # phash だけ更新して detect_and_act へ直行 (DL 完了判定は detect_and_act 内で行う)
        _dl_force_ocr = False
        if state.last_action in ("DOWNLOAD_WAIT", "LOADING_WAIT", "MAIN_STORY_LOADING"):
            try:
                cur_phash = compute_phash(img_path)
            except Exception:
                cur_phash = ""
            if state.last_phash and cur_phash:
                dist = phash_distance(state.last_phash, cur_phash)
            else:
                dist = 999
            state.last_phash_dist = dist
            if dist < PHASH_THRESHOLD:
                # 画面変化なし → DL/ロード継続中、解析スキップ
                state.same_phash_count += 1
                state.last_phash = cur_phash
                state.last_screen_change_time = time.time()  # Watchdog抑制
                # ── 10回(30秒)変化なし → DL完了/失敗ダイアログの可能性。OCR解析へ ──
                if state.same_phash_count >= 10:
                    logger.info("[iter %d] DL/ロード中: %d回変化なし → 完了ダイアログ確認のため通常解析へ",
                                i, state.same_phash_count)
                    # ── 累積60回(3分)変化なし → DLモード強制解除 ──
                    _dl_cumulative = getattr(state, "_dl_static_cumulative", 0) + state.same_phash_count
                    state._dl_static_cumulative = _dl_cumulative  # type: ignore[attr-defined]
                    if _dl_cumulative >= 60:
                        logger.warning("[iter %d] DL静止 %d回累積 → DLモード強制解除", i, _dl_cumulative)
                        state.download_active = False
                        state.last_action = "DL_STALL_ESCAPE"
                        state._dl_static_cumulative = 0  # type: ignore[attr-defined]
                    state.same_phash_count = 0
                    _dl_force_ocr = True   # 完了ダイアログ即検出のため強制 OCR
                    # fall through to detect_and_act
                else:
                    logger.debug("[iter %d] DL/ロード中: phash変化なし(dist=%d) → 3秒待機", i, dist)
                    time.sleep(3.0)
                    _fms = (time.time() - _loop_t0) * 1000
                    state.total_loop_ms += _fms
                    continue
            # 画面変化あり → DL 完了の可能性。通常フローで判定
            logger.info("[iter %d] DL/ロード中: 画面変化(dist=%d) → 通常解析へ", i, dist)
            state.last_phash = cur_phash
            # DLショートカットで既に phash 計算済み → 通常 phash 計算をスキップ
            state.last_phash_dist = dist
            screen_changed = dist >= PHASH_THRESHOLD or _dl_force_ocr
            state.same_phash_count = 0
            _dl_shortcut_fell_through = True
        else:
            _dl_shortcut_fell_through = False

        if not _dl_shortcut_fell_through:
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

        # ── ダウンロード保護フラグ: phash変化だけではクリアしない ──
        # DL進行中もゲージ更新でphash変化20-38が発生するため、phashベースの解除は不適切。
        # download_active のクリアは OCR でDLテキストが消えた場合のみ (detect_and_act 内)。

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
        # SCENE_EARLY が UNKNOWN → ポップアップ等で前シーンが無効化された
        # state.current_scene を UNKNOWN にリセットして BATTLE_RAPID を阻止
        if _early_scene == "UNKNOWN" and state.current_scene == "BATTLE":
            logger.info("[SCENE_EARLY] BATTLE→UNKNOWN 遷移 → BATTLE_RAPID 中断, OCR へ")
            state.current_scene = "UNKNOWN"
            state.battle_rapid_consecutive.reset()
        # ADV 連続検出カウンタ (phash 動的拡大用)
        if _early_scene == "ADV":
            state.adv_confirmed_count += 1
        elif _early_scene not in ("UNKNOWN",):
            state.adv_confirmed_count = 0
            state.adv_early_consecutive = 0  # ADV 以外のシーン → カウンタリセット

        if _early_scene == "MOVIE":
            if state.current_scene == "BATTLE":
                state.character_selected = False
                state.battle_rapid_consecutive.reset()
                logger.debug("[SCENE_CHANGE_EARLY] BATTLE→MOVIE: バトルフラグリセット")
            state.current_scene = "MOVIE"
            if handle_movie(img_path, state, dist, cur_phash):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (MOVIE_EARLY)", _fms)
                continue

        elif _early_scene == "BATTLE":
            state.current_scene = "BATTLE"
            _early_analysis = prepare_analysis_image(img_path, actual_w, actual_h)
            if handle_battle(_early_analysis, state, dist):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_EARLY)", _fms)
                continue
            _skip_rapid = True  # BATTLE ハンドラがフォールスルー → OCR へ直行

        elif _early_scene == "ADV":
            # ADV_EARLY スタック脱出: 15回連続ハンドル成功 → OCR フォールスルー
            _ADV_EARLY_STALL = 15
            if state.adv_early_consecutive >= _ADV_EARLY_STALL:
                logger.warning("[ADV_EARLY] %d 回連続 → OCR フォールスルー",
                               state.adv_early_consecutive)
                state.adv_early_consecutive = 0
                state.adv_confirmed_count = 0
                state.current_scene = "UNKNOWN"
                _skip_rapid = True
            elif handle_adv(img_path, state, dist, cur_phash, actual_w, actual_h):
                state.adv_early_consecutive += 1
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (ADV_EARLY) [%d/%d]",
                            _fms, state.adv_early_consecutive, _ADV_EARLY_STALL)
                continue
            else:
                state.adv_early_consecutive = 0
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
            # ADV 連続3回以上確認 → phash 上限拡大 (背景変更でも OCR スキップ)
            _adv_phash_max = 40 if state.adv_confirmed_count >= 3 else ADV_RAPID_PHASH_MAX
            if (not _skip_rapid and
                    not state.download_active and
                    state.last_action in ("STORY_TAP", "ADV_RAPID_TAP", "ADV_NEXT_TAP", "ADV_WAIT",
                                      "ADV_NEXT_FALLBACK", "ADV_SKIP_TAP",
                                      "STORY_TAP_HINT", "BUBBLE_TAP",
                                      "MINI_CONV_TAP", "MOYA_TAP", "MOVIE_SKIP", "MOVIE_WAIT",
                                      "ANIM_WAIT", "SCENE_TAP") and
                    PHASH_THRESHOLD <= dist <= _adv_phash_max and
                    state.current_scene not in ("MENU", "BATTLE", "MOVIE")):
                # ── MOVIE_WAIT 脱出: 8回連続 (~24秒) 動画待機ならフルOCRへフォールスルー ──
                _MOVIE_WAIT_ESCAPE = 8
                if state.movie_wait_consecutive >= _MOVIE_WAIT_ESCAPE:
                    logger.warning(
                        "[MOVIE_ESCAPE] 動画待機 %d 回連続 → フルOCR解析にフォールスルー",
                        state.movie_wait_consecutive)
                    state.movie_wait_consecutive = 0; state.movie_static_count = 0
                    # continue しない → 下の OCR パスへ落ちる
                else:
                    _rapid_adv = detect_adv_scene(img_path, roi=state.game_roi)
                    # ── ADV↓アイコン検出 → 1回タップ ──
                    # NOTE: detect_adv_advance_icon() 単独ではバトル画面の「通常攻撃」
                    # ボタン領域の明るいピクセルを↓と誤検出するため、ADVツールバー判定
                    # (is_adv) を必須条件にする。↓単独ではADVに入らない。
                    _adv_tap_x = int(ANALYSIS_W * 0.93)
                    _adv_tap_y = int(ANALYSIS_H * 0.91)
                    if _rapid_adv.is_adv:
                        if detect_adv_advance_icon(img_path):
                            logger.info("[ADV_RAPID][iter %d] ↓検出 → タップ (%d,%d)",
                                        i, _adv_tap_x, _adv_tap_y)
                            tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                            state.last_action = "ADV_RAPID_TAP"
                        elif _rapid_adv.next_btn_pos:
                            _adv_nx = int(_rapid_adv.next_btn_pos[0] * ANALYSIS_W / actual_w)
                            _adv_ny = int(_rapid_adv.next_btn_pos[1] * ANALYSIS_H / actual_h)
                            logger.info("[iter %d] ADV_RAPID → ↓ボタン座標 (%d,%d)", i, _adv_nx, _adv_ny)
                            tap_device(_adv_nx, _adv_ny, state, "ADV_RAPID_TAP")
                            state.last_action = "ADV_RAPID_TAP"
                        state.movie_wait_consecutive = 0; state.movie_static_count = 0
                        state.last_phash = ""
                        continue
                    # ── ミニ会話タップ (1回) ──
                    _mc = detect_mini_conversation(img_path)
                    if _mc is not None:
                        _mc_cx, _mc_cy, _mc_side = _mc
                        logger.info("[MINI_CONV][iter %d] 吹き出し(%s) → タップ (%d,%d)",
                                    i, _mc_side, _mc_cx, _mc_cy)
                        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                        state.last_action = "MINI_CONV_TAP"
                        state.movie_wait_consecutive = 0; state.movie_static_count = 0
                        state.last_phash = ""
                        continue
                    # ツールバーなし + ↓なし + 吹き出しなし → スコアリング動画判定
                    _adv_for_movie = detect_adv_scene(img_path, roi=state.game_roi)
                    _rapid_movie = detect_movie_scene(
                        img_path, adv_result=_adv_for_movie,
                        ocr_texts=state.last_ocr_texts, phash_dist=dist)
                    if _rapid_movie.is_movie:
                        state.movie_wait_consecutive += 1
                        logger.info("[iter %d] phash_dist=%d 動画検出(conf=%.2f) → 待機 (%d/%d)",
                                    i, dist, _rapid_movie.confidence,
                                    state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                        state.last_action = "MOVIE_WAIT"
                        state.last_phash = cur_phash
                        continue
                    # スコアリングで動画なし
                    # → 動画ではない静止画面 (ガチャ演出等) → 画面タップで進む
                    # ただし連続 phash 大変化中はタップを保留 (動画の⏭非表示期間の可能性)
                    # movie_wait_consecutive > 0 = 直前まで MOVIE 判定 → まだ動画かも
                    if state.movie_wait_consecutive > 0 and dist >= 8:
                        logger.info("[iter %d] phash_dist=%d 動画スコア不足だが直前MOVIE+phash変化大 → 待機続行 (%d)",
                                    i, dist, state.movie_wait_consecutive)
                        state.movie_wait_consecutive += 1
                        state.last_action = "MOVIE_WAIT"
                        state.last_phash = cur_phash
                        continue
                    # アニメーション検出: 0.5s後に再スクショしphash比較
                    # 動画再生中ならphashが変化する → タップせず待機
                    # 静止画面ならphash変化なし → SCENE_TAP実行
                    time.sleep(0.5)
                    _st_retry_path, _st_retry_w, _st_retry_h, _ = take_screenshot()
                    if _st_retry_path:
                        _st_retry_img = prepare_analysis_image(_st_retry_path, _st_retry_w, _st_retry_h)
                        _st_retry_ph = compute_phash(_st_retry_img) if _st_retry_img else ""
                        _st_retry_dist = phash_distance(cur_phash, _st_retry_ph) if cur_phash and _st_retry_ph else 0
                        if _st_retry_dist >= PHASH_THRESHOLD:
                            logger.info("[iter %d] SCENE_TAP前検査: 0.5s後phash_dist=%d → アニメーション中 → 待機",
                                        i, _st_retry_dist)
                            state.last_action = "ANIM_WAIT"
                            state.last_phash = _st_retry_ph
                            continue
                    # SCENE_TAP 連続上限: 15回連続で画面が進まない → 強制 OCR へ
                    _scene_tap_count = getattr(state, "_scene_tap_count", 0) + 1
                    state._scene_tap_count = _scene_tap_count
                    if _scene_tap_count >= 15:
                        logger.warning("[iter %d] SCENE_TAP %d回連続 → 強制 OCR へフォールスルー",
                                       i, _scene_tap_count)
                        state._scene_tap_count = 0
                        state.same_phash_count = FORCE_ANALYZE_AFTER
                        # OCR パスへ落とすため continue しない
                    else:
                        _st_x = int(ANALYSIS_W * 0.5)
                        _st_y = int(ANALYSIS_H * 0.5)
                        logger.info("[iter %d] phash_dist=%d 非動画静止画面 → SCENE_TAP (%d,%d)",
                                    i, dist, _st_x, _st_y)
                        tap_device(_st_x, _st_y, state, "SCENE_TAP")
                        state.last_action = "SCENE_TAP"
                        state.movie_wait_consecutive = 0; state.movie_static_count = 0
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

            # ── 候補リトライ: 残り候補があれば次の候補をタップ ──
            # ホーム画面到達判定中は候補タップ禁止 (画面遷移で HOME_CLEAR_CHECK が中断される)
            if state.home_reached and not state.grind_mode:
                state.pending_candidates = []
                state.pending_candidate_idx = 0
            # シーン変化チェック: フレッシュスクショで phash 比較し、
            # 画面が変わっていれば古い候補を破棄
            if (state.pending_candidates
                    and state.pending_candidate_idx < len(state.pending_candidates)):
                _cr_path, _cr_w, _cr_h, _ = take_screenshot()
                _cr_stale = False
                if _cr_path:
                    _cr_img = prepare_analysis_image(_cr_path, _cr_w, _cr_h)
                    try:
                        _cr_ph = compute_phash(_cr_img)
                        _cr_dist = phash_distance(cur_phash, _cr_ph) if cur_phash and _cr_ph else 0
                    except Exception:
                        _cr_dist = 0
                    if _cr_dist >= PHASH_THRESHOLD:
                        logger.info("[CANDIDATE_RETRY] phash_dist=%d → シーン変化検出、候補破棄", _cr_dist)
                        state.pending_candidates = []
                        state.pending_candidate_idx = 0
                        state.last_phash = _cr_ph
                        _cr_stale = True
                        img_path = _cr_img
                        actual_w, actual_h = _cr_w, _cr_h
                if not _cr_stale and state.pending_candidates:
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
                # ── ADV送り待ちアイコン検知: phash 安定中でも1回タップ ──
                _adv_tap_x = int(ANALYSIS_W * 0.93)
                _adv_tap_y = int(ANALYSIS_H * 0.91)
                if detect_adv_advance_icon(img_path):
                    logger.info("[ADV][iter %d] ↓検出 → タップ (%d,%d)", i, _adv_tap_x, _adv_tap_y)
                    tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                    state.last_action = "ADV_RAPID_TAP"
                    state.last_phash = ""
                    state.same_phash_count = 0
                    state.stall_start = 0.0
                    continue
                # ── ミニ会話タップ (phash安定時, 1回) ──
                _mc = detect_mini_conversation(img_path)
                if _mc is not None:
                    _mc_cx, _mc_cy, _mc_side = _mc
                    logger.info("[MINI_CONV][iter %d] 吹き出し(%s) → タップ (%d,%d)",
                                i, _mc_side, _mc_cx, _mc_cy)
                    tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                    state.last_action = "MINI_CONV_TAP"
                    state.last_phash = ""
                    state.same_phash_count = 0
                    state.stall_start = 0.0
                    continue
                # 動画シーンでは ADV ツールバーが無いためタップ抑制
                if state.current_scene in ("STORY", "ADV", "UNKNOWN"):
                    _aa_adv = detect_adv_scene(img_path, roi=state.game_roi)
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
                        if _movie_btn and len(state.last_ocr_texts) >= 2:
                            logger.info("[iter %d] >|検出だがOCR%d件+レターボックスなし → UI画面 → SCENE_TAP",
                                        i, len(state.last_ocr_texts))
                            _movie_btn = None
                        if _movie_btn:
                            logger.info("[MOVIE_WAIT] 動画検出(>|のみ) → 待機 (phash stable)")
                            state.last_action = "MOVIE_WAIT"
                            state.stall_start = 0.0  # ムービー待機中はスタックタイマー抑制
                            continue
                        # 金色⏭なし → アニメーション検査後にSCENE_TAP
                        time.sleep(0.5)
                        _st2_path, _st2_w, _st2_h, _ = take_screenshot()
                        if _st2_path:
                            _st2_img = prepare_analysis_image(_st2_path, _st2_w, _st2_h)
                            _st2_ph = compute_phash(_st2_img) if _st2_img else ""
                            _st2_dist = phash_distance(cur_phash, _st2_ph) if cur_phash and _st2_ph else 0
                            if _st2_dist >= PHASH_THRESHOLD:
                                logger.info("[iter %d] SCENE_TAP前検査: 0.5s後phash_dist=%d → アニメーション中 → 待機",
                                            i, _st2_dist)
                                state.last_action = "ANIM_WAIT"
                                state.stall_start = 0.0
                                state.last_phash = _st2_ph
                                continue
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

            # DL中はスタック介入をスキップ (phash 変化小はDLの正常動作)
            if state.download_active:
                logger.debug("[DL_PROTECT] DL中 → STALL_CORNER スキップ")
                state.stall_start = time.time()  # タイマーリセット
                state.last_phash = cur_phash
                time.sleep(3.0)
                continue

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                # ADVシーン中は右上タップ禁止 (ツールバー >| スキップを押してしまうため)
                _stall_is_adv = detect_adv_scene(img_path, roi=state.game_roi).is_adv if img_path else False
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
        # ── バトルUIガード (メインループ BATTLE_RAPID): 3回に1回、通常攻撃ボタンの存在を確認 ──
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override and not _skip_rapid
                and state.battle_rapid_consecutive.count > 0
                and state.battle_rapid_consecutive.count % 3 == 0):
            _atk_m = ASSET_MANAGER.match_single("battle_normal_attack", analysis_path)
            if not _atk_m or _atk_m[2] < 0.70:
                logger.info("[BATTLE_RAPID] 通常攻撃ボタン未検出 (count=%d) → OCR で再評価",
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

            # ── Phase 0: チュートリアル金枠 → 最優先タップ ──
            # 指ブロブ有無に関わらず金枠を常時チェック (extent<0.55 で通常ボタンと区別)
            # チュートリアル暗転中は空間フィルタをバイパスして全画面探索
            _gold_rho2 = True if state.current_scene == "BATTLE" else False
            _is_overlay2 = detect_tutorial_overlay(analysis_path)
            _gold_tap = detect_tutorial_gold_button_tap(
                analysis_path, right_half_only=_gold_rho2, overlay_mode=_is_overlay2)
            if _gold_tap:
                _rapid_tx, _rapid_ty = _gold_tap
                _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"
                if _is_overlay2:
                    logger.info("[BATTLE_RAPID] 暗転オーバーレイ → 全画面金枠 (%d,%d)",
                                _rapid_tx, _rapid_ty)

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
                    # B-0: テンプレートで battle_skill / battle_normal_attack を探す
                    for _btn_name in ("battle_skill", "battle_normal_attack"):
                        _btn_m = ASSET_MANAGER.match_single(_btn_name, analysis_path)
                        if _btn_m and _btn_m[2] >= 0.60:
                            _tmpl_action = f"BATTLE_RAPID_TMPL_{_btn_name.upper()}"
                            logger.info("[BATTLE_RAPID] テンプレ %s (%.2f) → tap(%d,%d)",
                                        _btn_name, _btn_m[2], _btn_m[0], _btn_m[1])
                            tap_device(_btn_m[0], _btn_m[1], state, _tmpl_action)
                            state.character_selected = False
                            state.char_just_selected = False
                            state.finger_detections += 1
                            state.battle_rapid_consecutive.tick()
                            _rapid_action = _tmpl_action
                            _rapid_tx, _rapid_ty = _btn_m[0], _btn_m[1]
                            break
                    # B-1: テンプレ未検出 → glow フォールバック
                    if not _rapid_action and _rapid_right_g:
                        _rr = max(_rapid_right_g, key=lambda g: g["area"])
                        _rapid_tx = _rr["cx"]
                        _rapid_ty = max(1, _rr["by"] + _rr["bh"] * 2 // 3)
                        _rapid_action = "BATTLE_RAPID_GLOW_P2"
                    elif not _rapid_action and _right_panel:
                        _tb = max(_right_panel, key=lambda b: b[2])
                        _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                        _rapid_action = "BATTLE_RAPID_MOYA_P2"
                    elif not _rapid_action:
                        _rapid_tx, _rapid_ty = roi_to_device(
                            int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                        _rapid_action = "BATTLE_RAPID_NORMATK_P2"

            # ── Phase C: 左モヤなしフォールバック → 待機+再確認 → 右側攻撃ボタン ──
            # 【永続ルール】左キャラにモヤがない場合は常に右側の通常攻撃/戦闘スキルをタップ
            # 安全弁: 連続10回フォールバック → バトル以外のシーンの可能性 → OCR 再評価
            if not _rapid_action:
                if state.normatk_fallback.stalled:
                    logger.info("[BATTLE_RAPID] FALLBACK %d回連続 → OCR で再評価",
                                state.normatk_fallback.count)
                    state.normatk_fallback.reset()
                    # BATTLE_RAPID を抜けて OCR に回す (continue しない)
                else:
                    # 敵ターン/アニメーション中の可能性 → 待機+再スクショで確認
                    time.sleep(0.5)
                    _fb_path, _fb_w, _fb_h, _ = take_screenshot()
                    if _fb_path:
                        _fb_analysis = prepare_analysis_image(_fb_path, _fb_w, _fb_h)
                        _fb_active = detect_active_battle_char(
                            _fb_analysis, ANALYSIS_W, ANALYSIS_H)
                        if _fb_active:
                            logger.info("[BATTLE_RAPID] 再確認でACTIVE_CHAR検出 → 次ループで正規処理")
                            img_path = _fb_analysis
                            continue
                    # テンプレマッチで正しいボタン位置を探す (character_selected 不問)
                    for _fb_btn in ("battle_skill", "battle_normal_attack"):
                        _fb_m = ASSET_MANAGER.match_single(_fb_btn, analysis_path)
                        if _fb_m and _fb_m[2] >= 0.60:
                            _rapid_tx, _rapid_ty = _fb_m[0], _fb_m[1]
                            _rapid_action = f"BATTLE_RAPID_TMPL_{_fb_btn.upper()}"
                            logger.info("[BATTLE_RAPID] FALLBACK テンプレ %s (%.2f) → tap(%d,%d)",
                                        _fb_btn, _fb_m[2], _rapid_tx, _rapid_ty)
                            break
                    if not _rapid_action:
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

        # ── ADVシーン検出 (毎回フレッシュ) ──
        _adv_result = detect_adv_scene(
            analysis_path or img_path, ocr_items=ocr_results, roi=state.game_roi)

        # ── シーン分類 ──
        scene, next_interval = classify_scene(
            texts, state.last_action, adv_detected=_adv_result.is_adv)
        # BATTLE 保持: SCENE_EARLY で BATTLE 確定後、OCR 分類が ADV/UNKNOWN でも
        # バトルキーワードが残っていれば BATTLE を維持 (攻撃アニメ中にテンプレ不一致になるため)
        _joined_for_scene = " ".join(texts)
        if (state.current_scene == "BATTLE" and scene not in ("BATTLE", "LOADING", "STORY")
                and any(kw in _joined_for_scene for kw in _BATTLE_UI_KWS)):
            logger.info("[SCENE_STICKY] %s→BATTLE保持 (バトルKW残存)", scene)
            scene = "BATTLE"
        # ── シーン遷移時フラグリセット ──
        if scene != state.current_scene:
            if state.current_scene == "BATTLE" and scene != "BATTLE":
                state.character_selected = False
                state.battle_rapid_consecutive.reset()
                logger.debug("[SCENE_CHANGE] BATTLE→%s: バトルフラグリセット", scene)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── DL直後 SKIP 検出 (OCRベース, テンプレート不要) ──
        # post_download 中は動画判定・テンプレートに依存せず OCR の "SKIP" を直接タップ
        if state.post_download and scene not in ("BATTLE", "MENU"):
            _skip_item = next(
                (item for item in ocr_results
                 if "SKIP" in item.get("text", "").upper()
                 or item.get("text", "").upper() == "SK"),
                None)
            if _skip_item:
                _sk_x, _sk_y = _skip_item["center"]
                _sk_x, _sk_y = roi_to_device(_sk_x, _sk_y, state.game_roi)
                logger.info(
                    "[DL_SKIP_OCR] DL直後 SKIP '%s' → タップ (%d,%d)",
                    _skip_item["text"], _sk_x, _sk_y)
                tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP_OCR")
                state.last_action = "MOVIE_SKIP"
                state.movie_wait_consecutive = 0; state.movie_static_count = 0
                state.last_phash = ""
                continue

        # ── キャラ獲得画面検出: MOVIE と混同しやすいため先に判定 ──
        # 特徴: 左下にキャラ名 + "のキオク"(メモリア名) + 属性アイコン2つ
        # NEW! は初回のみ表示されるため判定に使わない
        # 判定: "XXXのキオク" パターン (OCR単体で "のキオク" を含むテキスト)
        # 除外: OK ボタンがある / 交換所説明 / 長文テキスト (ガチャ説明ダイアログ)
        _kioku_item = next(
            (item for item in ocr_results
             if "のキオク" in item.get("text", "") and len(item.get("text", "")) <= 15),
            None)
        _has_ok_btn = any("OK" in t for t in texts)
        _has_result = any("Result" in t or "result" in t for t in texts)
        # キャラ詳細画面は CHARA_GET ではない (詳細/限界突破/スキル/3D 等が見える)
        _is_chara_detail = any(kw in t for t in texts for kw in ("詳細", "限界突破", "スキル"))
        if _kioku_item and not _has_ok_btn and not _has_result and not _is_chara_detail and scene not in ("BATTLE",):
            _tap_x, _tap_y = roi_to_device(
                int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state.game_roi)
            logger.info("[CHARA_GET] キャラ獲得画面検出 (キオク) → 中央タップ (%d,%d)", _tap_x, _tap_y)
            tap_device(_tap_x, _tap_y, state, "CHARA_GET_TAP")
            state.last_action = "CHARA_GET_TAP"
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            time.sleep(1.0)
            state.last_phash = ""
            continue

        # ── 動画シーン検出 (スコアリング方式): detect_and_act 前にガード ──
        # 動画中にタップするとUIが一時停止/再生を繰り返すため抑制する
        _movie_detect = detect_movie_scene(
            analysis_path, adv_result=_adv_result,
            ocr_texts=texts, phash_dist=dist)
        if _movie_detect.is_movie and _movie_detect.has_skip_btn and scene not in ("BATTLE", "MENU"):
            state.movie_wait_consecutive += 1
            _MOVIE_WAIT_ESCAPE = 8
            if state.movie_wait_consecutive >= _MOVIE_WAIT_ESCAPE:
                if state.post_download:
                    logger.warning(
                        "[MOVIE_GUARD_ESCAPE] DL直後+動画待機 %d 回 → SKIPタップ",
                        state.movie_wait_consecutive)
                    state.movie_wait_consecutive = 0; state.movie_static_count = 0
                    _resume_x, _resume_y = roi_to_device(
                        int(ANALYSIS_W * 0.93), int(ANALYSIS_H * 0.06), state.game_roi)
                    tap_device(_resume_x, _resume_y, state, "MOVIE_SKIP_ESCAPE")
                    state.last_action = "MOVIE_SKIP"
                else:
                    logger.warning(
                        "[MOVIE_GUARD_ESCAPE] 動画待機 %d 回 → 画面中央タップ",
                        state.movie_wait_consecutive)
                    state.movie_wait_consecutive = 0; state.movie_static_count = 0
                    _resume_x, _resume_y = roi_to_device(
                        int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state.game_roi)
                    tap_device(_resume_x, _resume_y, state, "MOVIE_RESUME_TAP")
                    # MOVIE慣性を断ち切る: last_action を非MOVIE系にして
                    # detect_scene_early() がフルOCRに落ちるようにする
                    state.last_action = "SCENE_TAP"
                state.last_phash = ""
                state.same_phash_count = 0
                continue
            logger.info(
                "[MOVIE_GUARD] スコアリング動画検出 (conf=%.2f) → 待機 (%d/%d)",
                _movie_detect.confidence, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
            state.last_action = "MOVIE_WAIT"
            state.stall_start = 0.0
            time.sleep(0.5)
            state.last_phash = cur_phash
            continue

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
            state.movie_wait_consecutive = 0; state.movie_static_count = 0

        # ── WAIT_FOR_CHANGE スタック脱出: 3 回連続で中央タップ ──
        if action == "WAIT_FOR_CHANGE":
            state._wfc_consecutive = getattr(state, "_wfc_consecutive", 0) + 1
            if state._wfc_consecutive >= 3:
                # アニメーション検査: 0.5s後に再スクショしphash比較
                time.sleep(0.5)
                _wfc_path, _wfc_w, _wfc_h, _ = take_screenshot()
                _wfc_is_anim = False
                if _wfc_path:
                    _wfc_img = prepare_analysis_image(_wfc_path, _wfc_w, _wfc_h)
                    _wfc_ph = compute_phash(_wfc_img) if _wfc_img else ""
                    _wfc_dist = phash_distance(cur_phash, _wfc_ph) if cur_phash and _wfc_ph else 0
                    if _wfc_dist >= PHASH_THRESHOLD:
                        _wfc_is_anim = True
                if _wfc_is_anim:
                    logger.info("[WFC_ESCAPE] 0.5s後phash_dist=%d → アニメーション中 → タップ抑制",
                                _wfc_dist)
                    state._wfc_consecutive = 0
                    state.last_action = "MOVIE_WAIT"
                    state.last_phash = _wfc_ph
                    continue
                logger.warning(
                    "[WFC_ESCAPE] WAIT_FOR_CHANGE %d 回連続 → 中央タップでエスケープ",
                    state._wfc_consecutive,
                )
                tap_device(ANALYSIS_W // 2, ANALYSIS_H // 2, state,
                           desc="WFC_CENTER_ESCAPE")
                state._wfc_consecutive = 0
                state.last_phash = ""
                continue
        else:
            state._wfc_consecutive = 0

        # ── シーン再評価: 同一アクション連続時にシーン認識を疑う ──
        if action == state.last_action and action not in (
            "WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT",
            "MOVIE_WAIT", "LOADING_WAIT", "ADV_WAIT",
            "HOME_CLEAR_CHECK",
        ):
            state.action_repeat_count += 1
        else:
            state.action_repeat_count = 0
            state.scene_reeval_mode = False

        if state.action_repeat_count >= _SCENE_REEVAL_THRESHOLD:
            _pre_reeval_action = action
            logger.warning(
                "[SCENE_REEVAL] '%s' が %d 回連続 → シーン再評価 (ガード緩和)",
                action, state.action_repeat_count,
            )
            state.scene_reeval_mode = True
            # 新しいスクリーンショットでフル再判定 (常にフレッシュOCR)
            try:
                _re_img, _re_w, _re_h, _ = take_screenshot()
                _re_analysis = prepare_analysis_image(_re_img, _re_w, _re_h)
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
                # レターボックスガードは廃止 (2:1デバイスで常時誤検出するため)
                action, wait_sec = detect_and_act(_re_ocr, state, _re_analysis)
                state.last_action = action
                state.action_repeat_count = 0
                logger.info("[SCENE_REEVAL] 再判定結果: %s", action)
                # 再判定後も同じアクション → 動画字幕等でスタックの可能性
                # → 画面中央タップでエスケープ (動画は字幕位置タップでは進まない)
                if action == _pre_reeval_action and action in (
                    "STORY_TAP", "MOYA_TAP", "ADV_NEXT_FALLBACK",
                    "ASSET_TUTORIAL_DIALOG_NEXT", "GOLD_BTN_TAP",
                    "FINGER_GOLD_TAP",
                ):
                    # DIALOG_NEXT スタック → BACK キーで閉じる
                    # GOLD_BTN/MOYA は ADV 中の装飾誤検出が多いため中央タップ
                    if action == "ASSET_TUTORIAL_DIALOG_NEXT":
                        logger.warning(
                            "[SCENE_REEVAL_ESCAPE] '%s' スタック → BACK キーで脱出", action)
                        adb("shell input keyevent KEYCODE_BACK")
                        state.last_action = "REEVAL_BACK_ESCAPE"
                    else:
                        # 代替候補があれば最優先で試行 (金枠の空振り脱出)
                        # STORY_TAP 系候補を優先 (CONFIRM_OK は金枠と近い位置のことが多い)
                        _esc_cand = None
                        for _c in state.pending_candidates:
                            if "STORY" in _c.action or "GOLD_BTN" in _c.action:
                                _esc_cand = _c
                                break
                        if _esc_cand is None and state.pending_candidates:
                            _esc_cand = state.pending_candidates[-1]  # 最後の候補にフォールバック
                        if _esc_cand:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] '%s' スタック → 代替候補 %s (%d,%d) にフォールバック",
                                action, _esc_cand.action, _esc_cand.x, _esc_cand.y)
                            tap_device(_esc_cand.x, _esc_cand.y, state,
                                       desc=f"REEVAL_CAND_{_esc_cand.action}")
                            state.pending_candidates = []
                            state.pending_candidate_idx = 0
                        else:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] 再判定でも '%s' → 中央タップでエスケープ",
                                action,
                            )
                            tap_device(ANALYSIS_W // 2, ANALYSIS_H // 2, state,
                                       desc="REEVAL_CENTER_ESCAPE")
                        state.last_action = "REEVAL_CENTER_ESCAPE"
            except Exception as _re_err:
                logger.debug("[SCENE_REEVAL] 再評価例外: %s", _re_err)
            state.scene_reeval_mode = False

        # タップ成功時: スタックカウンタリセット
        if action not in ("WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT"):
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.same_phash_count = 0

        # ── ダウンロード中フラグ管理 (SQLite 永続化) ──
        # ON: DL画面検出時 → 永続化 (プロセス再起動でも復元される)
        # OFF: DL/動画/ロード以外のシーンに遷移した時 → 削除
        _DL_MOVIE_ACTIONS = frozenset((
            "DOWNLOAD_WAIT", "MOVIE_WAIT", "MOVIE_SKIP", "MOVIE_RESUME_TAP",
            "MOVIE_SKIP_ESCAPE", "LOADING_WAIT", "WAIT_FOR_CHANGE",
            "DARK_SCENE_TAP", "MAIN_STORY_LOADING",
            "DL_COMPLETE_OK", "SYSTEM_DLG_OK", "MOVIE_SKIP_OCR",
            "ADV_CHOICE", "SCENE_TAP", "STORY_TAP",
        ))
        _POST_DL_GRACE = 20  # DL完了後20イテレーションはpost_downloadを維持
        if action == "DOWNLOAD_WAIT":
            if not state.post_download:
                logger.info("[PERSIST] post_download=True (DL画面検出)")
                state.post_download = True
                state.post_download_ttl = _POST_DL_GRACE
                persist_state("post_download", "1")
            else:
                # DL中は TTL をリセットし続ける
                state.post_download_ttl = _POST_DL_GRACE
        elif action == "DL_COMPLETE_OK":
            # DL完了ダイアログOK → TTL リセット (この後の動画SKIPに備える)
            state.post_download_ttl = _POST_DL_GRACE
            logger.info("[PERSIST] post_download TTL リセット (DL_COMPLETE_OK)")
        elif state.post_download and action not in _DL_MOVIE_ACTIONS:
            state.post_download_ttl -= 1
            if state.post_download_ttl <= 0:
                logger.info("[PERSIST] post_download クリア (TTL切れ, action=%s)", action)
                state.post_download = False
                delete_state("post_download")
            else:
                logger.debug("[PERSIST] post_download 維持 (TTL=%d, action=%s)",
                             state.post_download_ttl, action)

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
            _log_milestone(state, _reason)
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
                            # DL完了ダイアログの可能性 → 次イテレーションで即OCR
                            state.same_phash_count = 9  # 次の +1 で 10 到達 → 即 fallthrough
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
        # scrcpy 高速ループ (~5iter/sec) では CF/numpy 一時オブジェクトが蓄積しやすい
        if i % 10 == 0:
            gc.collect()
            # scrcpy 不死身モード: 50イテレーションごとにチェック
            if i > 0 and i % 50 == 0:
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
