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
)

# ─── 設定 ────────────────────────────────────────────
# ADB 接続は main() 内で実行する (CLI 引数 --pairing-code 等を受け取るため)
DEVICE_SERIAL = ""  # main() で設定される

# 排除された偽の指ブロブキャッシュ (debug_latest_tap.png への [REJECTED] 描画用)
_rejected_finger_blobs: list = []

# ─── scrcpy 管理 ───
SCRCPY_DEVICE = ""  # main() で DEVICE_SERIAL から動的設定


def _build_scrcpy_args(device_serial: str) -> list:
    """scrcpy 起動引数を動的に構築する。"""
    return [
        "scrcpy",
        "-s", device_serial,
        "--turn-screen-off",   # 物理画面消灯
        "--stay-awake",
    ]

# ─── 状態クラス: ap/state.py から import ───
from tools.ap.state import PilotState, StallCounter  # noqa: E402

# Ctrl+C シグナルハンドラ用: main() で設定する PilotState への参照
_pilot_state_ref: Optional["PilotState"] = None


# ─── ヘルパー: ap/helpers.py から import ───
from tools.ap.helpers import (  # noqa: E402
    classify_scene, text_core_center, save_evidence,
    has_any, has_text, all_texts,
)


# ─── ADB ユーティリティ ─────────────────────────────
def adb(cmd: str) -> str:
    full = f"adb -s {DEVICE_SERIAL} {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("adb timeout: %s", cmd)
        return ""


def detect_game_roi(img) -> tuple[int, int, int, int]:
    """
    スクリーンショットの黒帯（レターボックス）を検出し、純粋なゲーム描画領域を返す。

    アルゴリズム:
      1. グレースケール変換し、輝度 > 12 の「非黒」ピクセルを検出
      2. 列合計 / 行合計から黒帯の始終端を特定
      3. ROI サイズが全体の50%未満の場合はフォールバック (全画面)

    Returns: (roi_x, roi_y, roi_w, roi_h) in analysis image pixel coordinates
    """
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _H, _W = img.shape[:2]
        # 列/行ごとの輝度ピクセル数
        col_bright = (np.array(gray, dtype=np.int32) > 12).sum(axis=0)
        row_bright = (np.array(gray, dtype=np.int32) > 12).sum(axis=1)
        # 各辺の黒帯を検出 (ノイズ耐性: min 3px 以上の明るい列/行)
        x0 = next((x for x in range(_W) if col_bright[x] > 3), 0)
        x1 = next((x for x in range(_W - 1, -1, -1) if col_bright[x] > 3), _W - 1)
        y0 = next((y for y in range(_H) if row_bright[y] > 3), 0)
        y1 = next((y for y in range(_H - 1, -1, -1) if row_bright[y] > 3), _H - 1)
        roi_w = x1 - x0 + 1
        roi_h = y1 - y0 + 1
        # 全黒画面 or ROI が異常に小さい場合は全画面を返す
        if roi_w < _W * 0.5 or roi_h < _H * 0.5:
            return 0, 0, _W, _H
        return x0, y0, roi_w, roi_h
    except Exception:
        return 0, 0, ANALYSIS_W, ANALYSIS_H


def roi_to_device(ax: int, ay: int, roi: tuple) -> tuple[int, int]:
    """
    解析座標（比率ベース・ANALYSIS_W×ANALYSIS_H 空間）を
    ROI オフセットを考慮した実機タップ座標に変換する。

    formula:
        real_x = (ax / ANALYSIS_W) * roi_w + roi_x
        real_y = (ay / ANALYSIS_H) * roi_h + roi_y

    使用場面:
      - ratio-based 座標 (int(ANALYSIS_W * 0.91) など) → 必ず本関数で変換
      - OCR / テンプレートマッチング座標 → 既に実機座標のため変換不要

    Args:
        ax, ay : 解析空間 (0..ANALYSIS_W, 0..ANALYSIS_H) の座標
        roi    : detect_game_roi() の戻り値 (roi_x, roi_y, roi_w, roi_h)
    Returns: (device_x, device_y)
    """
    roi_x, roi_y, roi_w, roi_h = roi
    return (
        int(ax / ANALYSIS_W * roi_w) + roi_x,
        int(ay / ANALYSIS_H * roi_h) + roi_y,
    )


def get_device_resolution() -> tuple[int, int]:
    """
    `adb shell wm size` で実機の物理解像度を取得する。

    - landscape デバイスでは "Physical size: 1520x720" のように返る
    - Override がある場合は "Override size: ..." が優先される
    - 取得失敗時は (ANALYSIS_W, ANALYSIS_H) をフォールバックとして返す

    Returns: (width, height)
    """
    try:
        _out = subprocess.run(
            ["adb", "-s", DEVICE_SERIAL, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # "Override size: 1520x720" を優先、なければ "Physical size: 1520x720"
        for _prefix in ("Override size:", "Physical size:"):
            _m = re.search(rf"{_prefix}\s*(\d+)x(\d+)", _out)
            if _m:
                _w, _h = int(_m.group(1)), int(_m.group(2))
                logger.info("[WM_SIZE] %s → %dx%d", _prefix.rstrip(":"), _w, _h)
                return _w, _h
        logger.warning("[WM_SIZE] パース失敗: %r — フォールバック %dx%d", _out, ANALYSIS_W, ANALYSIS_H)
    except Exception as _e:
        logger.warning("[WM_SIZE] 取得エラー: %s — フォールバック %dx%d", _e, ANALYSIS_W, ANALYSIS_H)
    return ANALYSIS_W, ANALYSIS_H


def _query_status_bar_height() -> int:
    """
    `adb shell dumpsys display` から mStable top inset (ステータスバー高さ) を取得する。

    非 immersive 画面 (ご注意画面等) での adb input tap Y座標補正に使用。
    取得失敗時は解析解像度ベースのフォールバック値 int(ANALYSIS_H * 0.067) を返す。

    Returns: ステータスバーの高さ (ピクセル, 解析空間ではなくデバイス空間)
    """
    _fallback = int(ANALYSIS_H * 0.067)  # 48/720 ≈ 0.067
    try:
        _out = subprocess.run(
            ["adb", "-s", DEVICE_SERIAL, "shell", "dumpsys", "display"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        # mStable=[0,48][1424,720] → top inset = 48
        _m = re.search(r"mStable=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", _out)
        if _m:
            _top = int(_m.group(2))
            if _top > 0:
                logger.debug("[STATUS_BAR] mStable top inset: %d", _top)
                return _top
        logger.debug("[STATUS_BAR] mStable パース失敗 → フォールバック %d", _fallback)
    except Exception as _e:
        logger.debug("[STATUS_BAR] dumpsys 取得エラー: %s → フォールバック %d", _e, _fallback)
    return _fallback


def take_screenshot(retries: int = 3, min_bytes: int = 5_000) -> tuple[Optional[Path], int, int, int]:
    """
    スクリーンショット取得。破損PNG によるSIGSEGV防止のためリトライ付き。

    - retries: 破損検出時の再試行回数
    - min_bytes: 正常PNGの最小ファイルサイズ (5KB未満は破損と判定。暗転シーン≈11KB)
    Returns: (path, width, height, retry_count)
            path=None の場合は全リトライ失敗（呼び出し側で continue すること）
    """
    path = Path(SCREENSHOT_PATH)
    _retried = 0
    for _attempt in range(retries):
        # exec-out で直接パイプ → ファイル転送の中間ステップを省略 (高速化)
        try:
            _result = subprocess.run(
                ["adb", "-s", DEVICE_SERIAL, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10,
            )
            if _result.returncode == 0 and len(_result.stdout) >= min_bytes:
                path.write_bytes(_result.stdout)
            else:
                # exec-out 失敗 → 従来の shell + pull にフォールバック
                adb(f"shell screencap -p {REMOTE_PATH}")
                subprocess.run(
                    f"adb -s {DEVICE_SERIAL} pull {REMOTE_PATH} {SCREENSHOT_PATH}",
                    shell=True, capture_output=True, timeout=10,
                )
        except Exception as _ss_exc:
            logger.warning("[SCREENSHOT] 取得例外: %s (attempt %d/%d)", _ss_exc, _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # ── 整合性チェック1: ファイルサイズ ──
        _fsize = path.stat().st_size if path.exists() else 0
        if _fsize < min_bytes:
            logger.warning("[SCREENSHOT] 破損疑い: size=%d bytes (attempt %d/%d) — 再取得",
                           _fsize, _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # ── 整合性チェック2: OpenCV で読み込み確認 ──
        _test = cv2.imread(str(path))
        if _test is None or _test.size == 0:
            logger.warning("[SCREENSHOT] cv2.imread 失敗/空 (attempt %d/%d) — 再取得",
                           _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # 正常
        _h, _w = _test.shape[:2]
        return path, _w, _h, _retried
    # 全リトライ失敗: クラッシュせず None を返す (呼び出し側で continue)
    logger.error("[WIFI_ERROR] Corrupted frame dropped (%d retries exhausted). Returning None.", retries)
    return None, 0, 0, _retried


def manage_scrcpy() -> Optional[subprocess.Popen]:
    """scrcpy を規定オプションで起動。不整合プロセスは Kill → 再起動。"""
    try:
        ps = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        logger.warning("[SCRCPY] ps aux 失敗: %s", e)
        return None

    conforming_pid = None
    for line in ps.stdout.splitlines():
        if "scrcpy" not in line or "grep" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        # adb 子プロセス (scrcpy-server.jar) をスキップ — Kill 対象外
        if "adb" in line and "scrcpy-server" in line:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        has_device = SCRCPY_DEVICE in line
        has_screen_off = "--turn-screen-off" in line
        if has_device and has_screen_off:
            conforming_pid = pid
            logger.info("[SCRCPY] 規定プロセス検出 PID=%d — 継続", pid)
        else:
            logger.info("[SCRCPY] 不整合プロセス Kill PID=%d (cmdline: ...%s)",
                        pid, line[-80:])
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    if conforming_pid is not None:
        return None  # 既に規定オプションで動作中

    # 新規起動
    scrcpy_args = _build_scrcpy_args(SCRCPY_DEVICE)
    try:
        proc = subprocess.Popen(
            scrcpy_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[SCRCPY] 規定オプションで起動 PID=%d (device=%s, --turn-screen-off)",
                    proc.pid, SCRCPY_DEVICE)
        return proc
    except FileNotFoundError:
        logger.warning("[SCRCPY] scrcpy が見つかりません — Stay Awake なしで続行 "
                       "(brew install scrcpy で導入可能)")
    except Exception as e:
        logger.warning("[SCRCPY] 起動失敗: %s — Stay Awake なしで続行", e)
    return None


def tap_device(x: int, y: int, state: PilotState, desc: str = "",
               finger_box: Optional[tuple] = None,
               gold_box: Optional[tuple] = None,
               post_wait: float = 0.5) -> None:
    # ── 最低タップ間隔の強制 ──
    if state.last_action_time > 0:
        _elapsed = time.time() - state.last_action_time
        if _elapsed < MIN_TAP_INTERVAL:
            _wait = MIN_TAP_INTERVAL - _elapsed
            time.sleep(_wait)
    if state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        real_x = int(x * sx)
        real_y = int(y * sy)
    else:
        real_x, real_y = x, y
    # ─── デバッグオーバーレイ描画 (--verbose 時のみ) ───
    if _DEBUG_SAVE_IMAGES:
        try:
            if state.last_screen is not None:
                _dbg = state.last_screen.copy()
                if finger_box is not None:
                    fbx, fby, fbw, fbh = finger_box
                    cv2.rectangle(_dbg, (fbx, fby), (fbx + fbw, fby + fbh),
                                    (255, 0, 0), 2)
                if gold_box is not None:
                    gbx, gby, gbw, gbh = gold_box
                    cv2.rectangle(_dbg, (gbx, gby), (gbx + gbw, gby + gbh),
                                    (0, 255, 0), 2)
                cv2.circle(_dbg, (x, y), 10, (0, 0, 255), -1)
                if _rejected_finger_blobs:
                    for _rx, _ry, _rr in _rejected_finger_blobs:
                        cv2.drawMarker(_dbg, (_rx, _ry), (0, 0, 255),
                                        cv2.MARKER_CROSS, 22, 2)
                        cv2.putText(_dbg, "[REJECTED]", (_rx - 42, _ry - 14),
                                     cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 255), 1)
                _out = str(Path(__file__).parent.parent / "debug_latest_tap.png")
                cv2.imwrite(_out, _dbg)
        except Exception:
            pass
    logger.info(
        "  [DEBUG] TAP: 解析座標=(%d,%d) → デバイス座標=(%d,%d) | %s",
        x, y, real_x, real_y, desc
    )
    adb(f"shell input tap {real_x} {real_y}")
    state.total_taps += 1
    state.last_action_time = time.time()
    time.sleep(post_wait)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
          state: "PilotState | None" = None) -> None:
    if state and state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        rx1, ry1 = int(x1 * sx), int(y1 * sy)
        rx2, ry2 = int(x2 * sx), int(y2 * sy)
    else:
        rx1, ry1, rx2, ry2 = x1, y1, x2, y2
    adb(f"shell input swipe {rx1} {ry1} {rx2} {ry2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", rx1, ry1, rx2, ry2, duration_ms)


def is_dark_screen(img_path: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            gray = img.convert("L")
            return float(np.mean(np.array(gray))) <= BLACKOUT_BRIGHTNESS
    except Exception:
        return False


def prepare_analysis_image(img_path: Path, actual_w: int, actual_h: int) -> Path:
    from PIL import Image
    needs_transform = (actual_w < actual_h) or \
        ((actual_w, actual_h) != (ANALYSIS_W, ANALYSIS_H) and
         (actual_h, actual_w) != (ANALYSIS_W, ANALYSIS_H))
    if not needs_transform:
        return img_path
    analysis_path = ANALYSIS_PATH
    img = Image.open(img_path)
    if img.width < img.height:
        img = img.rotate(90, expand=True)
    if img.size != (ANALYSIS_W, ANALYSIS_H):
        img = img.resize((ANALYSIS_W, ANALYSIS_H), Image.LANCZOS)
    img.save(analysis_path)
    return analysis_path


# ─── 指差しアイコン (肌色ブロブ) 検出 ──────────────
def find_finger_blobs(img_path: Path, min_area: int = 400,
                      max_area: int = 15000,
                      dark_mode: bool = False) -> list[tuple[int, int, float, int, int, int, int]]:
    """
    指差しアイコン（肌色）の大きいブロブを検出。
    battle_loop.py と同じ HSV マスク手法。
    max_area: 金色カード等の大面積誤検出を除外（UI カードは 15000px² 超）
    dark_mode: バトル背景など暗い状況では輝度閾値を緩和（V:150→100, S:40→25）
    返値: [(cx, cy, area, bbox_x, bbox_y, bbox_w, bbox_h), ...] 面積降順
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return []
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # dark_mode: バトル暗背景向けに輝度・彩度閾値を緩和
        if dark_mode:
            lower = np.array([5, 25, 100])
        else:
            lower = np.array([5, 40, 150])
        upper = np.array([25, 180, 255])
        mask = cv2.inRange(hsv, lower, upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        img_h_fb = img.shape[0]
        blobs = []
        global _rejected_finger_blobs
        _rejected_finger_blobs = []  # 毎回リセット
        # max_area 超のブロブを一時保存 (後で金枠チェックで救済する候補)
        _oversized: list[tuple] = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            if area > max_area:
                # 指アイコン+隣接ゴールドUIが融合した巨大ブロブ候補 → 一時保存
                M_ov = cv2.moments(c)
                if M_ov["m00"] > 0:
                    _ov_cx = int(M_ov["m10"] / M_ov["m00"])
                    _ov_cy = int(M_ov["m01"] / M_ov["m00"])
                    _ov_bx, _ov_by, _ov_bw, _ov_bh = cv2.boundingRect(c)
                    _oversized.append((_ov_cx, _ov_cy, area, _ov_bx, _ov_by, _ov_bw, _ov_bh))
                continue
            M = cv2.moments(c)
            if M["m00"] <= 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            bx, by, bw, bh = cv2.boundingRect(c)

            # ── 【形状検証 1】Solidity（充填率）チェック ───────────────────────
            # 蝶の王冠/トゲトゲ形状は solidity 低い。指アイコンは輪郭が滑らかで高い。
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull)
            solidity = area / hull_area if hull_area > 0 else 0.0
            if solidity < 0.35:
                _rejected_finger_blobs.append((cx, cy, "SHAPE(sol=%.2f)" % solidity))
                continue

            # ── 【形状検証 2】アスペクト比チェック ─────────────────────────────
            # 指アイコンは概ね 0.28〜3.5 の範囲。過度に横長な蝶の羽を排除。
            asp = bw / bh if bh > 0 else 1.0
            if asp > 3.5 or asp < 0.28:
                _rejected_finger_blobs.append((cx, cy, "SHAPE(asp=%.1f)" % asp))
                continue

            # ── 【空間的バイアス 3】バトル(dark_mode)上部30%の小面積ブロブ排除 ────
            # 蝶エネミーは上部(バトルフィールド)に出現、チュートリアル指は下部UIに出現
            if dark_mode and cy < img_h_fb * 0.30 and area < 1500:
                _rejected_finger_blobs.append((cx, cy, "SPATIAL(y=%d,area=%.0f)" % (cy, area)))
                logger.info("[REJECTED: SPATIAL] (%d,%d) 上部30%%内 area=%.0f<1500 → エネミー誤検出排除",
                            cx, cy, area)
                continue

            blobs.append((cx, cy, area, bx, by, bw, bh))
        # ── 大面積ブロブ救済: 近傍に金枠があれば指+ゴールドUI融合と判定して採用 ──
        if not blobs and _oversized:
            for _ov in _oversized:
                _gf = find_gold_frame_near(img_path, _ov[0], _ov[1], search_radius=200)
                if _gf is not None:
                    logger.info("[FINGER_OVERSIZED_RESCUE] (%d,%d) area=%.0f + 金枠(%d,%d) → 採用",
                                _ov[0], _ov[1], _ov[2], _gf[0], _gf[1])
                    blobs.append(_ov)
                    break  # 最初の1件で十分
        return sorted(blobs, key=lambda b: b[2], reverse=True)
    except ImportError:
        return []
    except Exception as e:
        logger.debug("find_finger_blobs error: %s", e)
        return []


def detect_white_hand_pointer(
    img_path: Path, threshold: float = 0.85
) -> Optional[tuple[int, int, float]]:
    """
    白いハンドポインタ（home_nav_finger / home_nav_finger_up）をテンプレートマッチングで検出。
    find_finger_blobs() が HSV 肌色のみ対象で白ポインタを見逃す問題を補完。
    Returns: (cx, cy, score) or None
    """
    try:
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        templates_dir = _CRAWLER_ROOT / "assets" / "templates"
        best: Optional[tuple[int, int, float]] = None
        for name in ("home_nav_finger", "home_nav_finger_up"):
            tpl_path = templates_dir / f"{name}.png"
            if not tpl_path.exists():
                continue
            tmpl = cv2.imread(str(tpl_path), cv2.IMREAD_GRAYSCALE)
            if tmpl is None or tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= threshold and (best is None or max_val > best[2]):
                h, w = tmpl.shape
                best = (max_loc[0] + w // 2, max_loc[1] + h // 2, max_val)
        if best:
            logger.info("[WHITE_HAND] 白ハンドポインタ検出 (%d,%d) score=%.3f", best[0], best[1], best[2])
        return best
    except Exception as e:
        logger.debug("detect_white_hand_pointer error: %s", e)
        return None


def create_finger_mask_image(img_path: Path, cx: int, cy: int, half: int = 175) -> Path:
    """
    指アイコン周囲 350×350px (half=175) 以外を純黒に塗りつぶした一時画像を生成して返す。
    Hard Masking 2.0: 右側スキルボタン等の誤検出を物理的に排除。
    失敗した場合は元の img_path を返す。
    """
    try:
        _img_hm = cv2.imread(str(img_path))
        if _img_hm is None:
            return img_path
        _H_hm, _W_hm = _img_hm.shape[:2]
        _masked = np.zeros_like(_img_hm)
        _x1 = max(0, cx - half)
        _x2 = min(_W_hm, cx + half)
        _y1 = max(0, cy - half)
        _y2 = min(_H_hm, cy + half)
        _masked[_y1:_y2, _x1:_x2] = _img_hm[_y1:_y2, _x1:_x2]
        _tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        cv2.imwrite(_tmp.name, _masked)
        _tmp.close()
        return Path(_tmp.name)
    except Exception as _e_hm:
        logger.debug("create_finger_mask_image error: %s", _e_hm)
        return img_path


def detect_guide_glow(img_path: Path, W: int, H: int,
                      footer_ratio: float = 0.30,
                      min_area: int = 800) -> list[dict]:
    """
    チュートリアルガイドの「発光（モヤ）エフェクト」をフッター領域で検知する。
    フッター = 画面下部 footer_ratio (デフォルト30%) に限定。
    白〜金色の高輝度ブロブを検出し、左側(left)/右側(right)を分類して返す。
    返値: [{"cx":int,"cy":int,"area":float,"side":"left"|"right",
            "bx":int,"by":int,"bw":int,"bh":int}, ...] 面積降順
    """
    try:
        _img_gw = cv2.imread(str(img_path))
        if _img_gw is None:
            return []
        _Hg, _Wg = _img_gw.shape[:2]
        _footer_y = int(_Hg * (1.0 - footer_ratio))
        _footer = _img_gw[_footer_y:_Hg, 0:_Wg]
        if _footer.size == 0:
            return []
        _hsv_gw = cv2.cvtColor(_footer, cv2.COLOR_BGR2HSV)
        # 白発光: 低彩度・高輝度 (白いハイライト/ハロー)
        _mask_w = cv2.inRange(_hsv_gw,
                              np.array([0, 0, 215], dtype=np.uint8),
                              np.array([180, 65, 255], dtype=np.uint8))
        # 金発光: 金/黄色系・高輝度 (ゴールドハイライト)
        _mask_g = cv2.inRange(_hsv_gw,
                              np.array([15, 50, 195], dtype=np.uint8),
                              np.array([50, 210, 255], dtype=np.uint8))
        _mask_gw = cv2.bitwise_or(_mask_w, _mask_g)
        # ノイズ除去: 小さいスポット・HPバー等の細線を排除
        _kern = np.ones((4, 4), np.uint8)
        _mask_gw = cv2.morphologyEx(_mask_gw, cv2.MORPH_OPEN, _kern)
        _cnts_gw, _ = cv2.findContours(_mask_gw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        _glows = []
        for _c_gw in _cnts_gw:
            _a_gw = cv2.contourArea(_c_gw)
            if _a_gw < min_area:
                continue
            # HPバー等の細長いブロブを除外: アスペクト比 > 8 はバー状
            _bx_gw, _by_gw, _bw_gw, _bh_gw = cv2.boundingRect(_c_gw)
            _asp_gw = _bw_gw / _bh_gw if _bh_gw > 0 else 1.0
            if _asp_gw > 8.0 or _asp_gw < 0.12:
                continue
            _M_gw = cv2.moments(_c_gw)
            if _M_gw["m00"] <= 0:
                continue
            _cx_gw = int(_M_gw["m10"] / _M_gw["m00"])
            _cy_gw = int(_M_gw["m01"] / _M_gw["m00"]) + _footer_y
            _by_abs = _by_gw + _footer_y
            _side = "left" if _cx_gw < _Wg // 2 else "right"
            _glows.append({
                "cx": _cx_gw, "cy": _cy_gw, "area": _a_gw, "side": _side,
                "bx": _bx_gw, "by": _by_abs, "bw": _bw_gw, "bh": _bh_gw,
            })
        return sorted(_glows, key=lambda g: g["area"], reverse=True)
    except Exception as _e_gw:
        logger.debug("detect_guide_glow error: %s", _e_gw)
        return []


def _run_battle_glow_sm(
    analysis_path: Path,
    W: int, H: int,
    state: "PilotState",
    ocr: list,
    tag: str = "GLOW_SM",
) -> Optional[tuple]:
    """
    バトル発光ステートマシン (統一版)。#0-PRE と #1-pre の共通ロジック。

    P1: 左キャラ発光 (character_selected=False) → タップ → character_selected=True
    P2: 右スキル発光 (character_selected=True)  → タップ → character_selected=False
    P3: 発光なし + character_selected → 通常攻撃 OCR フォールバック

    Returns: (action, wait_sec) or None (発光なし/バトルでない)
    """
    glows = detect_guide_glow(analysis_path, W, H, footer_ratio=0.30)
    left = [g for g in glows if g["side"] == "left"]
    right = [g for g in glows if g["side"] == "right"]
    if glows:
        logger.info("[%s] フッター発光: 左%d個(最大%.0f) 右%d個(最大%.0f)", tag,
                    len(left), left[0]["area"] if left else 0,
                    len(right), right[0]["area"] if right else 0)

    # P1: 左キャラ発光 (キャラ未選択)
    if not state.character_selected and left:
        g = max(left, key=lambda g: g["area"])
        gx, gy = g["cx"], max(1, g["cy"] - _GLOW_CENTER_Y_OFFSET)
        logger.info("[%s P1] 左キャラ発光(%d,%d)→tap(%d,%d)", tag, g["cx"], g["cy"], gx, gy)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR", post_wait=0.3)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR")  # ダブルタップ
        state.character_selected = True
        state.char_just_selected = True
        state.finger_detections += 1
        return "GLOW_LEFT_CHAR", 0.3

    # P2: 右スキル発光 (キャラ選択済み)
    if state.character_selected and right:
        g = max(right, key=lambda g: g["area"])
        gx, gy = g["cx"], max(1, g["cy"] - _GLOW_CENTER_Y_OFFSET)
        logger.info("[%s P2] 右発光(%d,%d)→tap(%d,%d)", tag, g["cx"], g["cy"], gx, gy)
        tap_device(gx, gy, state, "GLOW_RIGHT_SKILL")
        state.character_selected = False
        state.char_just_selected = False
        state.finger_detections += 1
        return "GLOW_RIGHT_SKILL", 0.3

    # P3: キャラ選択済み + 発光なし → 通常攻撃 OCR フォールバック
    if state.character_selected and not right:
        na = has_any(ocr, ["通常攻撃", "单体攻撃", "単体攻撃"])
        if na:
            nx, ny = na["center"]
            if nx > W * 0.5 and ny > H * 0.5:
                ny = max(1, ny - _GLOW_CENTER_Y_OFFSET)
                logger.info("[%s P3] 攻撃ボタンOCR '%s'(%d,%d) → tap", tag, na["text"], nx, ny)
                tap_device(nx, ny, state, "NORMATK_TAP")
                state.character_selected = False
                state.char_just_selected = False
                return "NORMATK_TAP", 1.0

    return None


def detect_active_battle_char(
    img_path: Path,
    analysis_w: int = 1520,
    analysis_h: int = 720,
) -> Optional[tuple[int, int, float]]:
    """
    【永続バトルルール】バトル画面で選択待ちモヤ（赤/ピンク発光ハロー）が
    あるキャラクターを検出する。

    アクティブキャラの特徴:
      - 赤/ピンクの発光ハロー（非アクティブにはない）
      - 肖像周辺の全体明度が著しく高い

    方式:
      1. フッター左領域（キャラ肖像エリア）を等幅カラムに分割
      2. 各カラムの「暖色発光ピクセル数」と「平均明度」を計算
      3. 中央値比で突出しているカラムをアクティブキャラと判定

    Returns: (cx, cy, brightness_ratio) or None
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _h, _w = _img.shape[:2]

        # キャラ肖像エリア: 画面下部25%, 左側 x=100~760
        _y0 = int(_h * 0.75)
        _x0 = 100
        _x1 = min(760, _w)
        _footer = _img[_y0:_h, _x0:_x1]
        if _footer.size == 0:
            return None

        _hsv = cv2.cvtColor(_footer, cv2.COLOR_BGR2HSV)
        _fh, _fw = _footer.shape[:2]

        # 暖色発光マスク: 赤/ピンク/マゼンタ (H:0-20 or 155-180, S>=35, V>=100)
        _m1 = cv2.inRange(_hsv,
                          np.array([0, 35, 100], dtype=np.uint8),
                          np.array([20, 255, 255], dtype=np.uint8))
        _m2 = cv2.inRange(_hsv,
                          np.array([155, 35, 100], dtype=np.uint8),
                          np.array([180, 255, 255], dtype=np.uint8))
        _warm_mask = cv2.bitwise_or(_m1, _m2)

        # 5カラム分割（キャラ5人想定、各カラム ~132px）
        _n_cols = 5
        _col_w = _fw // _n_cols
        _stats = []  # (warm_count, avg_brightness, col_center_x, col_idx)

        for _ci in range(_n_cols):
            _cx0 = _ci * _col_w
            _cx1 = (_ci + 1) * _col_w
            _col_warm = _warm_mask[:, _cx0:_cx1]
            _col_v = _hsv[:, _cx0:_cx1, 2]  # V channel
            _warm_count = int(cv2.countNonZero(_col_warm))
            _avg_v = float(np.mean(_col_v))
            _center_x = _x0 + _cx0 + _col_w // 2
            _stats.append((_warm_count, _avg_v, _center_x, _ci))

        if not _stats:
            return None

        # 中央値の計算
        _warm_counts = [s[0] for s in _stats]
        _avg_vs = [s[1] for s in _stats]
        _med_warm = float(np.median(_warm_counts))
        _med_v = float(np.median(_avg_vs))

        # アクティブキャラ判定: 暖色ピクセルが中央値の3倍以上 OR 明度が中央値の1.4倍以上
        _best = None
        for _wc, _av, _ccx, _ci in _stats:
            _warm_ratio = _wc / max(_med_warm, 1.0)
            _v_ratio = _av / max(_med_v, 1.0)
            _is_active = (_warm_ratio >= 3.0) or (_v_ratio >= 1.4 and _wc > _med_warm)
            if _is_active:
                _score = _warm_ratio + _v_ratio
                if _best is None or _score > _best[3]:
                    _cy = _y0 + _fh // 2
                    _best = (_ccx, _cy, _v_ratio, _score)

        if _best:
            logger.info(
                "[ACTIVE_CHAR] 選択待ちキャラ検出 (%d,%d) brightness_ratio=%.2f",
                _best[0], _best[1], _best[2]
            )
            return (_best[0], _best[1], _best[2])

        return None

    except Exception as _e_abc:
        logger.debug("detect_active_battle_char error: %s", _e_abc)
        return None


def find_gold_frame_near(img_path: Path, cx: int, cy: int,
                         search_radius: int = 150) -> Optional[tuple[int, int, int, int]]:
    """
    指アイコン中心(cx,cy)の近傍150px以内で金枠（装飾ボタン枠）を検索。
    スワイプポインター（縦長細い）は除外し、ボタン形状の金枠を返す。
    Returns: (frame_cx, frame_cy, frame_w, frame_h) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        H_img, W_img = img.shape[:2]
        x1 = max(0, cx - search_radius)
        y1 = max(0, cy - search_radius)
        x2 = min(W_img, cx + search_radius)
        y2 = min(H_img, cy + search_radius)
        roi = img[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)
        k5 = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k5)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0
        for c in contours:
            area = cv2.contourArea(c)
            if area < 3000:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < 60:
                continue
            aspect = w / max(h, 1)
            if not (0.3 < aspect < 5.5):
                continue
            # スワイプポインター（縦長細い: h>w*3.5 かつ w<100）は除外
            if h > w * 3.5 and w < 100:
                continue
            if area > best_area:
                best_area = area
                frame_cx = x1 + x + w // 2
                frame_cy = y1 + y + h // 2
                best = (frame_cx, frame_cy, w, h)
        return best
    except Exception as e:
        logger.debug("find_gold_frame_near error: %s", e)
        return None


def is_adv_toolbar_cached(img_path: Path, state: "PilotState") -> bool:
    """is_adv_toolbar_visible() の phash キャッシュ付きラッパー。同一 phash なら再計算しない。"""
    cur = state.last_phash
    if cur and cur == state._adv_toolbar_cache_phash:
        return state._adv_toolbar_cache_result
    result = is_adv_toolbar_visible(img_path)
    state._adv_toolbar_cache_phash = cur
    state._adv_toolbar_cache_result = result
    return result


def detect_adv_advance_icon(img_path: Path,
                             roi_x: int = int(ANALYSIS_W * 0.875),
                             roi_y: int = int(ANALYSIS_H * 0.847),
                             roi_w: int = int(ANALYSIS_W * 0.112),
                             roi_h: int = int(ANALYSIS_H * 0.125),
                             min_bright: int = 20) -> bool:
    """
    ADV送り待ちアイコン（◆/▼）を検出。
    テキストボックス右下 ROI 内に孤立した明るい小クラスターを探す。

    ROI デフォルト: x=1330-1500, y=610-700 (landscape 1520x720)
    明るい白/淡色ピクセル: HSV V>210, S<60 が min_bright 個以上 → True
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return False
        _H, _W = _img.shape[:2]
        _x1 = max(0, roi_x)
        _y1 = max(0, roi_y)
        _x2 = min(_W, roi_x + roi_w)
        _y2 = min(_H, roi_y + roi_h)
        if _x2 <= _x1 or _y2 <= _y1:
            return False
        _roi = _img[_y1:_y2, _x1:_x2]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        _mask = cv2.inRange(_hsv, (0, 0, 210), (180, 60, 255))
        _bright = int(cv2.countNonZero(_mask))
        if _bright >= min_bright:
            logger.debug("[ADV_ADVANCE] 明るいピクセル %d 個 @ ROI(%d,%d,%d,%d)",
                         _bright, roi_x, roi_y, roi_w, roi_h)
            return True
        return False
    except Exception as _e:
        logger.debug("detect_adv_advance_icon error: %s", _e)
        return False


def is_adv_toolbar_visible(img_path: Path) -> bool:
    """
    ADVパートの右上ツールバー（5個のアイコン列: メニュー, ログ, AUTO, >>, >|）を検出。
    動画シーン（⏭ 1個のみ）と区別するために使用。

    手法: 右上ROI内でCanny edge密度を計測。
    ADVツールバー: 複数アイコンの輪郭でedge密度が高い (>=0.04)
    動画シーン: アイコン1個のみ or 空で低密度
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return False
        _H, _W = _img.shape[:2]
        # ROI: 右上 78%~100% x, 0~10% y
        _x1 = int(_W * 0.78)
        _y2 = int(_H * 0.10)
        if _y2 < 10 or _W - _x1 < 10:
            return False
        _roi = _img[0:_y2, _x1:_W]
        _gray = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)
        _edges = cv2.Canny(_gray, 50, 150)
        _total = _roi.shape[0] * _roi.shape[1]
        if _total == 0:
            return False
        _edge_ratio = cv2.countNonZero(_edges) / _total
        _visible = _edge_ratio >= 0.04
        if _visible:
            logger.debug("[ADV_TOOLBAR] edge密度=%.3f → ADVパート確定", _edge_ratio)
        return _visible
    except Exception:
        return False


def detect_movie_skip_button(img_path: Path) -> Optional[tuple]:
    """
    動画シーンの⏭スキップボタン（右上の金色円形アイコン）を検出。
    返り値: (cx, cy) or None
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _H, _W = _img.shape[:2]
        # ROI: 右上コーナー (88%~100% x, 0~12% y)
        _x1 = int(_W * 0.88)
        _y2 = int(_H * 0.12)
        if _y2 < 5 or _W - _x1 < 5:
            return None
        _roi = _img[0:_y2, _x1:_W]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        # 金色: H=15-40, S>50, V>130
        _mask = cv2.inRange(_hsv, (15, 50, 130), (40, 255, 255))
        _gold_count = int(cv2.countNonZero(_mask))
        if _gold_count >= 30:
            _coords = cv2.findNonZero(_mask)
            if _coords is not None:
                _mx = int(np.mean(_coords[:, 0, 0])) + _x1
                _my = int(np.mean(_coords[:, 0, 1]))
                logger.debug("[MOVIE_SKIP_BTN] 金色ボタン検出 (%d,%d) gold_px=%d", _mx, _my, _gold_count)
                return (_mx, _my)
        return None
    except Exception:
        return None


# ─── チュートリアルダイアログ ページ送り/閉じるボタン検出 ─────────────────
# ダイアログにはページング可能な間 ◁▷ 矢印が表示され、
# 最終ページでは × ボタンが右上に出現して閉じることができる。
#
# 検出優先順位:
#   1. assets/templates/tutorial_dialog_close.png が存在 → テンプレートマッチで × 位置を返す
#   2. assets/templates/tutorial_dialog_next.png が存在 → テンプレートマッチで ▷ 位置を返す
#   3. どちらも存在しない → ("close", 固定座標) or ("next", 固定座標) をフォールバック
#
# 戻り値: ("next", cx, cy) | ("close", cx, cy) | None

_DIALOG_CLOSE_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_dialog_close.png"
_DIALOG_NEXT_TEMPLATE  = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_dialog_next.png"


def detect_tutorial_dialog_nav(img_path: Path,
                                W: int = 1520, H: int = 720,
                                threshold: float = 0.75) -> Optional[tuple[str, int, int]]:
    """
    チュートリアルダイアログの ▷(次へ) または ×(閉じる) ボタンを検出する。

    テンプレート画像が存在する場合はテンプレートマッチング、
    存在しない場合は固定座標フォールバックを返す。

    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _H, _W = _img.shape[:2]

        def _match_template(tmpl_path: Path, roi_x1: int, roi_y1: int,
                            roi_x2: int, roi_y2: int) -> Optional[tuple[int, int]]:
            _tmpl = cv2.imread(str(tmpl_path))
            if _tmpl is None:
                return None
            _roi = _img[roi_y1:roi_y2, roi_x1:roi_x2]
            _res = cv2.matchTemplate(_roi, _tmpl, cv2.TM_CCOEFF_NORMED)
            _, _max_val, _, _max_loc = cv2.minMaxLoc(_res)
            if _max_val >= threshold:
                _th, _tw = _tmpl.shape[:2]
                _cx = roi_x1 + _max_loc[0] + _tw // 2
                _cy = roi_y1 + _max_loc[1] + _th // 2
                return (_cx, _cy)
            return None

        # 1. × ボタン (右上隅: x=W*0.92~W, y=0~H*0.15)
        if _DIALOG_CLOSE_TEMPLATE.exists():
            _r = _match_template(
                _DIALOG_CLOSE_TEMPLATE,
                int(_W * 0.92), 0, _W, int(_H * 0.15),
            )
            if _r:
                logger.debug("[DialogNav] × ボタン検出 (template): (%d,%d)", *_r)
                return ("close", _r[0], _r[1])

        # 2. ▷ 矢印 (右エッジ: x=W*0.85~W, y=H*0.25~H*0.75)
        if _DIALOG_NEXT_TEMPLATE.exists():
            _r2 = _match_template(
                _DIALOG_NEXT_TEMPLATE,
                int(_W * 0.85), int(_H * 0.25), _W, int(_H * 0.75),
            )
            if _r2:
                logger.debug("[DialogNav] ▷ 矢印検出 (template): (%d,%d)", *_r2)
                return ("next", _r2[0], _r2[1])

        # 3. テンプレートなし → 判断できないため None を返し、呼び出し側のシーケンスに委ねる
        return None

    except Exception as _e:
        logger.debug("detect_tutorial_dialog_nav error: %s", _e)
        return None


# ─── ダイアログ枠検出 + × / ▷ ボタン探索 ──────────────────────────────────
def detect_dialog_frame_and_nav(
    img_path: Path, W: int = 1520, H: int = 720,
    ocr_texts: Optional[list] = None,
    roi: Optional[tuple] = None,
) -> Optional[tuple]:
    """
    ダイアログ枠（形状）を視覚的に検出し、その内部/周辺で ×(閉じる)/▷(次へ) を探す。

    トリガー優先順:
      1. HSV金色枠の大矩形検出 (主: 形状ベース)
      2. OCR キーワード補助     (副: テキストベース、枠検出失敗時フォールバック)

    ボタン探索優先順:
      ×: 1.テンプレート 2.Canny+Hough 3.輝度   → 固定座標 (W*0.975, H*0.055)
      ▷: 1.テンプレート 2.Canny+Hough 3.輝度   → 固定座標 (W*0.91,  H*0.49)
      未特定時: ダイアログ下部中央 ("bottom")

    Returns: ("close",  cx, cy)
             ("next",   cx, cy)
             ("bottom", cx, cy)
             None  — ダイアログ未検出
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        _H, _W = img.shape[:2]

        # ──────────────────────────────────────────────────────────────
        # STEP 0: × ボタン先行検出 (無条件)
        #   画面右上に × があれば「ダイアログ」と即断定して close を返す。
        #   これにより金枠装飾がある画面でも × を見逃さない。
        # ──────────────────────────────────────────────────────────────
        def _find_close_x(img_full, _H, _W):
            """画面右上領域でテンプレートマッチにより × ボタンを探す。"""
            if not _DIALOG_CLOSE_TEMPLATE.exists():
                return None
            # 探索 ROI: 右端 15%, 上端 15%
            _rx1 = int(_W * 0.85)
            _ry2 = int(_H * 0.15)
            _roi_x = img_full[0:_ry2, _rx1:_W]
            if _roi_x.size == 0:
                return None
            _tpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
            if (_roi_x.shape[0] < _tpl.shape[0]
                    or _roi_x.shape[1] < _tpl.shape[1]):
                return None
            _r = cv2.matchTemplate(_roi_x, _tpl, cv2.TM_CCOEFF_NORMED)
            _, _mv, _, _ml = cv2.minMaxLoc(_r)
            if _mv >= 0.65:
                _tw, _th = _tpl.shape[1], _tpl.shape[0]
                return (_rx1 + _ml[0] + _tw // 2, _ml[1] + _th // 2)
            return None

        def _has_page_arrow(img_full, _H, _W) -> Optional[tuple[int, int]]:
            """右サイドにページング矢印 (>) が存在するか確認。
            狭いストリップ (右端3%) × 中央帯 (30%-70%) で白/明るい矢印を検出。
            """
            _rx1n = int(_W * 0.94)  # 右端6%のみ
            _ry1n, _ry2n = int(_H * 0.30), int(_H * 0.70)
            _roi_n = img_full[_ry1n:_ry2n, _rx1n:_W]
            if _roi_n.size == 0:
                return None
            _g = cv2.cvtColor(_roi_n, cv2.COLOR_BGR2GRAY)
            # 高閾値で白い矢印のみ検出 (金色背景ノイズを排除)
            _, _thr = cv2.threshold(_g, 180, 255, cv2.THRESH_BINARY)
            _bright = cv2.countNonZero(_thr)
            if _bright >= 15:
                # 矢印の固定位置 (右端中央): ページ送り座標
                _ax = int(_W * 0.97)
                _ay = _H // 2
                return (_ax, _ay)
            return None

        _close_x_pos = _find_close_x(img, _H, _W)
        if _close_x_pos is not None:
            # ページング矢印 (>) チェック: 矢印があれば close ではなく next を優先
            _arrow_pos = _has_page_arrow(img, _H, _W)
            if _arrow_pos is not None:
                logger.debug("[Dialog] STEP0: × 検出(%d,%d) + 矢印(%d,%d) → next 優先 (ページング)",
                             _close_x_pos[0], _close_x_pos[1], _arrow_pos[0], _arrow_pos[1])
                return ("next", _arrow_pos[0], _arrow_pos[1])
            logger.debug("[Dialog×] STEP0 先行検出: (%d,%d)", _close_x_pos[0], _close_x_pos[1])
            return ("close", _close_x_pos[0], _close_x_pos[1])

        # ──────────────────────────────────────────────────────────────
        # STEP 1: HSV 金色枠で大矩形ダイアログを検出
        # ──────────────────────────────────────────────────────────────
        _hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        _mask_g = cv2.inRange(
            _hsv,
            np.array([12, 50, 140], np.uint8),
            np.array([55, 255, 255], np.uint8),
        )
        _k3 = np.ones((3, 3), np.uint8)
        _mask_g = cv2.dilate(_mask_g, _k3, iterations=2)
        _cnts, _ = cv2.findContours(_mask_g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        _frame: Optional[tuple] = None  # (x, y, w, h)
        _best_area = 0
        _scx, _scy = _W // 2, _H // 2   # 画面中心

        for _c in _cnts:
            _a = cv2.contourArea(_c)
            if _a < 8000:
                continue
            _x, _y, _w, _h = cv2.boundingRect(_c)
            if _w < 280 or _h < 160:          # 小さすぎ → カード等を除外
                continue
            if _w > _W * 0.97 or _h > _H * 0.97:  # 全画面 → 除外
                continue
            _asp = _w / max(_h, 1)
            if not (0.3 < _asp < 5.5):
                continue
            # Golden Rule 3: ダイアログ中心 X が 20%〜80% 範囲内のみ有効
            # 右端パネル・装飾要素による誤タップを防止
            _dcx = _x + _w // 2
            _dcy = _y + _h // 2
            if not (_W * 0.20 <= _dcx <= _W * 0.80):
                continue
            if abs(_dcy - _scy) > _H * 0.45:
                continue
            if _a > _best_area:
                _best_area = _a
                _frame = (_x, _y, _w, _h)

        _frame_detected = _frame is not None

        # OCR キーワード補助: 枠未検出でもキーワードがあればフォールバック実行
        _ocr_trigger = False
        if not _frame_detected and ocr_texts:
            _joined_ocr = " ".join(ocr_texts)
            _ocr_trigger = any(kw in _joined_ocr for kw in _DIALOG_FIRST_KWS)
        if not _frame_detected and not _ocr_trigger:
            return None                           # ダイアログ未検出

        # ──────────────────────────────────────────────────────────────
        # STEP 2: フレーム内/周辺で × と ▷ を探す
        # ──────────────────────────────────────────────────────────────
        def _canny_lines(roi_img, thr_lo=40, thr_hi=120, min_len=6, max_gap=4):
            _g = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
            _e = cv2.Canny(_g, thr_lo, thr_hi)
            return (
                cv2.HoughLinesP(_e, 1, np.pi / 180,
                                 threshold=8, minLineLength=min_len, maxLineGap=max_gap),
                _g,
            )

        def _chevron_tip(lines):
            """HoughLinesP 結果から ▷ 形状の先端を返す"""
            if lines is None or len(lines) < 2:
                return None
            _ul, _dl = [], []
            for _ln in lines:
                _x1, _y1, _x2, _y2 = _ln[0]
                if _x2 == _x1:
                    continue
                if _x1 > _x2:
                    _x1, _y1, _x2, _y2 = _x2, _y2, _x1, _y1
                _ang = np.degrees(np.arctan2(_y2 - _y1, _x2 - _x1))
                if -70 < _ang < -20:
                    _ul.append((_x1, _y1, _x2, _y2))
                elif 20 < _ang < 70:
                    _dl.append((_x1, _y1, _x2, _y2))
            if _ul and _dl:
                _ur = max(_ul, key=lambda l: l[2])
                _dr = max(_dl, key=lambda l: l[2])
                return (int((_ur[2] + _dr[2]) / 2), int((_ur[3] + _dr[3]) / 2))
            return None

        # 検索 ROI: テンプレートなければ Canny、それも失敗したら輝度、最後に固定座標
        # フレーム検出時はフレーム右上を優先、画面右上はフォールバック

        # ── × ボタン検索 ──────────────────────────────────────────────
        # Phase A: フレーム検出時 → フレーム右上隅で × を探す
        if _frame_detected:
            _fx, _fy, _fw, _fh = _frame
            # フレーム右上角周辺を探索 (±40px マージン)
            _frx1 = max(0, _fx + _fw - 60)
            _fry1 = max(0, _fy - 30)
            _frx2 = min(_W, _fx + _fw + 40)
            _fry2 = min(_H, _fy + 50)
            _froi = img[_fry1:_fry2, _frx1:_frx2]
            if _froi.size > 0:
                # テンプレートマッチング
                if _DIALOG_CLOSE_TEMPLATE.exists():
                    _tpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
                    if _froi.shape[0] >= _tpl.shape[0] and _froi.shape[1] >= _tpl.shape[1]:
                        _r_f = cv2.matchTemplate(_froi, _tpl, cv2.TM_CCOEFF_NORMED)
                        _, _mv_f, _, _ml_f = cv2.minMaxLoc(_r_f)
                        if _mv_f >= 0.65:
                            _tw_f = _tpl.shape[1]
                            _th_f = _tpl.shape[0]
                            _cx_f = _frx1 + _ml_f[0] + _tw_f // 2
                            _cy_f = _fry1 + _ml_f[1] + _th_f // 2
                            logger.debug("[Dialog×] フレーム右上テンプレ: (%d,%d) score=%.2f", _cx_f, _cy_f, _mv_f)
                            return ("close", _cx_f, _cy_f)
                # Note: Canny / 輝度フォールバックは誤検出率が高いため廃止。
                # × 検出は STEP 0 テンプレートマッチングのみが権威ある判定。

        # Phase B: フレーム未検出 or フレーム右上で × 未発見 → 画面右上隅で探す
        if _DIALOG_CLOSE_TEMPLATE.exists():
            _close_tmpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
            _r = cv2.matchTemplate(
                cv2.imread(str(img_path), cv2.IMREAD_COLOR)[0: int(_H * 0.14), int(_W * 0.88):],
                _close_tmpl,
                cv2.TM_CCOEFF_NORMED,
            )
            _, _mv, _, _ml = cv2.minMaxLoc(_r)
            if _mv >= 0.65:
                _th, _tw = _close_tmpl.shape[:2]
                return ("close",
                        int(_W * 0.88) + _ml[0] + _tw // 2,
                        _ml[1] + _th // 2)

        # Note: Phase B Canny / 輝度フォールバックは廃止 (ホーム画面バナー誤検出防止)。
        # × 検出は STEP 0 テンプレートマッチングに一元化。

        # ── ▷ ボタン (スクリーン右エッジ) ────────────────────────────────
        if _DIALOG_NEXT_TEMPLATE.exists():
            _next_tmpl = cv2.imread(str(_DIALOG_NEXT_TEMPLATE))
            _r2 = cv2.matchTemplate(
                img[int(_H * 0.22): int(_H * 0.78), int(_W * 0.83):],
                _next_tmpl,
                cv2.TM_CCOEFF_NORMED,
            )
            _, _mv2, _, _ml2 = cv2.minMaxLoc(_r2)
            if _mv2 >= 0.75:
                _th2, _tw2 = _next_tmpl.shape[:2]
                return ("next",
                        int(_W * 0.83) + _ml2[0] + _tw2 // 2,
                        int(_H * 0.22) + _ml2[1] + _th2 // 2)

        _rx1n, _ry1n = int(_W * 0.83), int(_H * 0.22)
        _rx2n, _ry2n = _W, int(_H * 0.78)
        _roi_n = img[_ry1n:_ry2n, _rx1n:_rx2n]
        if _roi_n.size > 0:
            _lns_n, _gray_n = _canny_lines(_roi_n)
            _np_tip = _chevron_tip(_lns_n)
            if _np_tip:
                logger.debug("[Dialog▷] Canny検出: (%d,%d)", _rx1n + _np_tip[0], _ry1n + _np_tip[1])
                return ("next", _rx1n + _np_tip[0], _ry1n + _np_tip[1])
            if cv2.countNonZero(cv2.threshold(_gray_n, 140, 255, cv2.THRESH_BINARY)[1]) >= 20:
                _r = roi if roi else (0, 0, _W, _H)
                _nx_fb, _ny_fb = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), _r)
                logger.debug("[Dialog▷] 輝度FB(ROI補正): (%d,%d)", _nx_fb, _ny_fb)
                return ("next", _nx_fb, _ny_fb)

        # ── フォールバック: 固定座標 ▷ ──────────────────────────────────
        if _frame_detected:
            # 枠が確認できている場合は枠下部中央を安全タップ
            _fx, _fy, _fw, _fh = _frame
            _fb_x, _fb_y = _fx + _fw // 2, _fy + int(_fh * 0.85)
            logger.debug("[Dialog] 枠下部フォールバック: (%d,%d)", _fb_x, _fb_y)
            return ("bottom", _fb_x, _fb_y)

        # OCR キーワードのみで枠未検出 → ROI 補正済み固定座標 ▷
        _r = roi if roi else (0, 0, _W, _H)
        _nx_ocr, _ny_ocr = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), _r)
        return ("next", _nx_ocr, _ny_ocr)

    except Exception as _e:
        logger.debug("detect_dialog_frame_and_nav error: %s", _e)
        return None


# ─── ページング式ダイアログ完全処理 ────────────────────────────────────────
def process_paging_dialog(
    analysis_path: Path, W: int, H: int,
    state: "PilotState", max_pages: int = 10,
    initial_dlg: Optional[tuple] = None,
    ocr_texts: Optional[list] = None,
) -> str:
    """
    ▷ → ▷ → … → × のシーケンスを一括処理する。

    - "next"/"bottom" を検出するたびにタップ → 次ページスクリーンショット取得
    - "close" を検出したらタップして終了
    - ダイアログが消えたら完了扱い
    - phash変化なし → ループ中断 (誤検出▷への無限タップ防止)
    - × ROI bright_pixels=0 が2回続く → 枠外タップで強制脱出
    - max_pages 超過でタイムアウト

    Returns: "DIALOG_CLOSED" | "DIALOG_PAGING_TIMEOUT"
    """
    _roi = state.game_roi
    _prev_phash = compute_phash(analysis_path)
    _no_close_streak = 0  # × ROI bright_pixels=0 の連続回数
    for _page in range(max_pages):
        # page=0 かつ initial_dlg が渡されている場合は外側の検出結果を再利用
        if _page == 0 and initial_dlg is not None:
            _dlg = initial_dlg
        else:
            _dlg = detect_dialog_frame_and_nav(
                analysis_path, W, H, roi=_roi,
                ocr_texts=ocr_texts,
            )
        if _dlg is None:
            logger.info("[PAGING] ダイアログ消失 (page=%d) → 完了", _page)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        _kind, _dx, _dy = _dlg
        if _kind == "close":
            tap_device(_dx, _dy, state, "PAGING_CLOSE")
            logger.info("[PAGING] ×タップ (page=%d) → クローズ完了", _page + 1)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        # × ROI 輝度チェック: bright_pixels=0 が続く場合は強制脱出
        try:
            _img_c = cv2.imread(str(analysis_path))
            if _img_c is not None:
                _Hc, _Wc = _img_c.shape[:2]
                _close_roi_c = _img_c[0:int(_Hc * 0.14), int(_Wc * 0.88):]
                _gray_cl = cv2.cvtColor(_close_roi_c, cv2.COLOR_BGR2GRAY)
                _bright_cl = cv2.countNonZero(
                    cv2.threshold(_gray_cl, 155, 255, cv2.THRESH_BINARY)[1]
                )
                if _bright_cl == 0:
                    _no_close_streak += 1
                else:
                    _no_close_streak = 0
        except Exception as _e:
            logger.debug("[PAGING] × ROI 判定例外: %s", _e)
        if _no_close_streak >= 8:
            # × ボタンが画面右上に存在しない → 枠外 or 下部中央を叩いて強制脱出
            _esc_x, _esc_y = W // 2, H - 60
            logger.info(
                "[PAGING] × ROI 暗(%d回連続) → 強制脱出タップ(%d,%d)",
                _no_close_streak, _esc_x, _esc_y,
            )
            tap_device(_esc_x, _esc_y, state, "PAGING_ESCAPE")
            return "DIALOG_PAGING_TIMEOUT"
        # "next" or "bottom" → ▷ タップして次ページ
        tap_device(_dx, _dy, state, "PAGING_NEXT")
        logger.info("[PAGING] ▷タップ (page=%d/%d)", _page + 1, max_pages)
        state.dialog_detections += 1
        time.sleep(0.4)
        # 次ページのスクリーンショットを取得して解析
        _img_path, _aw, _ah, _ = take_screenshot()
        analysis_path = prepare_analysis_image(_img_path, _aw, _ah)
        # phash変化監視: 変化なし → ページが進んでいない → ループ中断
        _new_phash = compute_phash(analysis_path)
        if _prev_phash and _new_phash:
            _ph_dist = phash_distance(_prev_phash, _new_phash)
            if _ph_dist < 4:
                logger.info(
                    "[PAGING] ▷タップ後 phash変化なし(dist=%d<4) → 誤検出▷ → ループ中断",
                    _ph_dist,
                )
                return "DIALOG_PAGING_TIMEOUT"
        _prev_phash = _new_phash
    logger.warning("[PAGING] max_pages=%d 超過 → タイムアウト", max_pages)
    return "DIALOG_PAGING_TIMEOUT"


# ─── テキスト入力エリア検出 ────────────────────────────────────────────────
def detect_text_input_area(
    img_path: Path,
    W: int = 1520,
    H: int = 720,
    ocr_items: Optional[list] = None,
) -> Optional[tuple[int, int]]:
    """
    テキスト入力エリア（横長の暗い矩形 + 文字数カウンター）を検出してフィールド中心座標を返す。

    検出手順:
    1. OCR で "0/N" パターン（文字数カウンター）を検索 → カウンター位置からフィールド中心を推定
    2. OCR で入力プレースホルダー（"を入力", "Enter" 等）を含む項目を探す
    3. 上記いずれも失敗した場合、HSV で暗い横長矩形を探す

    Returns: (field_cx, field_cy) or None
    """
    # --- 1. OCR 文字数カウンター "0/N" パターン ---
    if ocr_items:
        for _item in ocr_items:
            _txt = _item.get("text", "").strip()
            if re.match(r"^0/\d+$", _txt):
                _cx, _cy = _item["center"]
                # カウンターはフィールド右端にある → フィールド中心は左へ ~13% (200px / 1520)
                return max(0, _cx - int(W * 0.131)), _cy
        # --- 2. プレースホルダーテキスト検出 ---
        for _item in ocr_items:
            _txt = _item.get("text", "").strip()
            if "を入力" in _txt or "Enter" in _txt.lower():
                return _item["center"][0], _item["center"][1]
    # --- 3. HSV 暗い横長矩形 ---
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _roi_y1, _roi_y2 = int(H * 0.3), int(H * 0.75)
        _roi = _img[_roi_y1:_roi_y2, :]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        # 入力フィールド特有の暗めの背景 (S低め、V中〜低)
        _dark = cv2.inRange(_hsv, np.array([0, 0, 20]), np.array([180, 80, 110]))
        _cnts, _ = cv2.findContours(_dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for _cnt in sorted(_cnts, key=cv2.contourArea, reverse=True)[:8]:
            _x, _y, _w, _h = cv2.boundingRect(_cnt)
            if _w > W * 0.25 and 25 < _h < 100 and _w / max(_h, 1) > 3.5:
                return _x + _w // 2, _roi_y1 + _y + _h // 2
    except Exception as _e:
        logger.debug("detect_text_input_area error: %s", _e)
    return None


# ─── HSV金色チュートリアルポインター検出 → ホールドスワイプ ─────────────
def detect_tutorial_gold_swipe(img_path: Path) -> Optional[tuple[str, int, int, int, int]]:
    """
    HSVフィルタで金色チュートリアルポインター（手アイコン+軌跡）を検出し
    スワイプ方向と座標を返す。

    ユーザー指定HSV: Hue~30-50, Sat~100-250, Val~200-255
    OpenCV HSV では H は 0-180 (標準360°の半分)なのでH=15-50を使用。

    縦長領域(h>=w*2.5) のみ有効 (ボタン等との誤検出防止)。
    手アイコン(幅広部)が上半分 → SWIPE_UP、下半分 → SWIPE_DOWN。

    デバッグ画像: crawler/templates/debug/gold_detect_HHMMSS.png に自動保存。

    Returns: (direction, swipe_x, from_y, to_y, duration_ms) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 金色 (手アイコン+軌跡): H=15-50, S=60-255, V=180-255
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        # モルフォロジー: 小ノイズ除去 → 拡張で手+軌跡を繋ぐ
        k3 = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k3)
        k7 = np.ones((7, 7), np.uint8)
        mask = cv2.dilate(mask, k7, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # 最大輪郭を選択
        largest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(largest)

        # 面積フィルタ: 2000~100000px (ポインター想定範囲)
        if area < 2000 or area > 100000:
            return None

        x_bb, y_bb, w_bb, h_bb = cv2.boundingRect(largest)

        # アスペクト比チェック: 縦長(h>=w*3.5)のみ有効
        # 2.0→3.5に引き上げ: キャラカード金装飾(h/w≈2.0-2.5)や金枠ボタン(h/w≈1.0)との誤検出防止
        # さらに幅制限: w>100px の太いものはボタン/カード → スワイプポインターは細い
        if h_bb < w_bb * 3.5 or w_bb > 100:
            return None

        cx_bb = x_bb + w_bb // 2

        # ── デバッグ画像保存 (--verbose 時のみ) ──
        if _DEBUG_SAVE_IMAGES:
            debug_dir = _CRAWLER_ROOT / "templates" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            vis = img.copy()
            cv2.rectangle(vis, (x_bb, y_bb), (x_bb + w_bb, y_bb + h_bb), (0, 0, 255), 3)
            cv2.putText(vis, f"GoldSwipe area={int(area)} h/w={h_bb/max(w_bb,1):.1f}",
                        (x_bb, max(0, y_bb - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imwrite(str(debug_dir / f"gold_detect_{ts}.png"), vis)

        # ── 方向判定: 上半分 vs 下半分のゴールドピクセル面積で判断 ──
        # 手アイコン(幅広・濃い)が多い方が「手」の端 → その逆方向へスワイプ
        mask_roi = mask[y_bb:y_bb + h_bb, x_bb:x_bb + w_bb]
        mid_y = h_bb // 2
        upper_area = int(np.sum(mask_roi[:mid_y] > 0))
        lower_area = int(np.sum(mask_roi[mid_y:] > 0))

        # 上半分が大きい → 手が上 → SWIPE_UP
        if upper_area >= lower_area:
            direction = "UP"
            from_y = min(H_img - 60, y_bb + h_bb + 100)
            to_y   = max(50, y_bb - 80)
        else:
            direction = "DOWN"
            from_y = max(50, y_bb - 80)
            to_y   = min(H_img - 60, y_bb + h_bb + 100)

        logger.info(
            "[GoldSwipe] 検出OK: area=%d bbox=(%d,%d,%d,%d) h/w=%.1f "
            "upper=%d lower=%d → %s  swipe_x=%d from_y=%d to_y=%d",
            area, x_bb, y_bb, w_bb, h_bb, h_bb / max(w_bb, 1),
            upper_area, lower_area, direction, cx_bb, from_y, to_y,
        )
        return direction, cx_bb, from_y, to_y, 10000

    except ImportError:
        return None
    except Exception as e:
        logger.debug("detect_tutorial_gold_swipe error: %s", e)
        return None


# ─── Type B: 金枠ハイライトボタン検出 → 中心タップ ─────────────────────
def detect_tutorial_gold_button_tap(img_path: Path,
                                    right_half_only: bool = True
                                    ) -> Optional[tuple[int, int]]:
    """
    チュートリアルバトルで指アイコンが指し示す「金枠ハイライトボタン」を検出し
    タップ座標（ボタン中心）を返す。

    条件:
    - アスペクト比 0.5~2.0 (正方形〜縦長のボタン形状)
    - 面積 8000~150000px² (ボタン相当の大きさ)
    - 幅 100px以上 (細い軌跡線は除外)
    - right_half_only=True の場合: x中心 > W/2 のみ有効 (右側ボタン優先)

    デバッグ画像: crawler/templates/tutorial/gold_btn_HHMMSS.png に自動保存。
    Returns: (tap_x, tap_y) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        # モルフォロジー: 枠線の隙間を埋めて矩形を繋ぐ
        k7 = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k7)
        mask = cv2.dilate(mask, k7, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # ボタン候補: アスペクト比0.5~2.0 かつ面積8000~150000 かつ幅100px以上
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 8000 or area > 150000:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < 100:
                continue
            aspect = h / max(w, 1)
            if 0.5 <= aspect <= 2.0:
                cx = x + w // 2
                cy = y + h // 2
                # 右半分のみフィルタ
                if right_half_only and cx < W_img * 0.5:
                    continue
                candidates.append((cx, cy, area, x, y, w, h))

        if not candidates:
            return None

        # 最大面積のボタン候補を選択
        best = max(candidates, key=lambda c: c[2])
        tap_x, tap_y, area_b, x_b, y_b, w_b, h_b = best

        # ── デバッグ/テンプレート保存 (--verbose 時のみ) ──
        if _DEBUG_SAVE_IMAGES:
            tut_dir = _CRAWLER_ROOT / "templates" / "tutorial"
            tut_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            vis = img.copy()
            cv2.rectangle(vis, (x_b, y_b), (x_b + w_b, y_b + h_b), (255, 0, 0), 3)
            cv2.circle(vis, (tap_x, tap_y), 12, (0, 255, 255), -1)
            cv2.putText(vis, f"GoldBtn area={int(area_b)} asp={h_b/max(w_b,1):.1f}",
                        (x_b, max(0, y_b - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
            cv2.imwrite(str(tut_dir / f"gold_btn_{ts}.png"), vis)
            roi = img[y_b:y_b + h_b, x_b:x_b + w_b]
            if roi.size > 0:
                cv2.imwrite(str(tut_dir / f"gold_btn_roi_{ts}.png"), roi)

        logger.info("[GoldBtn] 検出OK: area=%d bbox=(%d,%d,%d,%d) asp=%.1f → tap(%d,%d)",
                    area_b, x_b, y_b, w_b, h_b, h_b / max(w_b, 1), tap_x, tap_y)
        return tap_x, tap_y

    except ImportError:
        return None
    except Exception as e:
        logger.debug("detect_tutorial_gold_button_tap error: %s", e)
        return None


# ─── Smart Tap: 金色ボタン矩形の幾何学的中心を検出 ──────────────────


def smart_tap_button(
    img_path: Path,
    ocr_cx: int,
    ocr_cy: int,
    search_r: int = 120,
    ocr_items: list[dict] | None = None,
) -> tuple[int, int]:
    """Text-Core 対応 SmartTap: 金色ボタン枠を検出し、テキスト中心優先でタップ座標を返す。

    1. OCR 中心周辺から HSV で金色ボタン枠 (B) を検出
    2. B が見つかったら text_core_center() でテキスト中心優先の座標を返す
    3. B が見つからない場合は OCR 座標をそのまま返す

    返値: (tap_x, tap_y)
    """
    try:
        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            raise ValueError("imread failed")
        h_img, w_img = img_bgr.shape[:2]

        # 探索エリア: OCR 中心から search_r px の矩形
        x1 = max(0, ocr_cx - search_r)
        y1 = max(0, ocr_cy - search_r)
        x2 = min(w_img, ocr_cx + search_r)
        y2 = min(h_img, ocr_cy + search_r)

        roi = img_bgr[y1:y2, x1:x2]
        roi_hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # 金色ボタン枠の HSV レンジ
        lower_gold = np.array([15, 50, 120], dtype=np.uint8)
        upper_gold = np.array([42, 190, 235], dtype=np.uint8)
        mask = cv2.inRange(roi_hsv, lower_gold, upper_gold)

        # モルフォロジー: ノイズ除去 + 枠の繋ぎ合わせ
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.dilate(mask, kernel, iterations=2)
        mask = cv2.erode(mask, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_rect = None
        best_area = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 2000:
                continue
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            if rw < 80 or rh < 20:
                continue
            aspect = rw / max(rh, 1)
            if aspect < 2.0 or aspect > 15.0:
                continue
            if area > best_area:
                best_area = area
                best_rect = (rx + x1, ry + y1, rw, rh)

        if best_rect:
            # Text-Core: ボタン枠 (B) 内のテキスト中心を優先
            return text_core_center(
                best_rect,
                ocr_items or [],
                label="SmartTap",
            )

    except Exception as e:
        logger.debug("  [SmartTap] エラー: %s", e)

    # フォールバック: OCR 座標をそのまま使用
    logger.debug("[SmartTap] fallback OCR-direct (%d,%d)", ocr_cx, ocr_cy)
    return ocr_cx, ocr_cy


# ─── チュートリアル: 金色ハイライトボタンを全画面スキャンで検出 ──────────────
def find_golden_highlighted_button(img_path: Path) -> Optional[tuple[int, int]]:
    """
    チュートリアル指差しアイコンが指す「金色ハイライトされたボタン/カード」を
    HSV 色域スキャンで検出する。
    指の向き（上下左右）に依存しない方向非依存のアプローチ。

    返値: (cx, cy) ― 最大輝度の金色領域の中心座標、検出失敗時は None
    """
    try:
        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            return None
        h_img, w_img = img_bgr.shape[:2]

        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

        # 金色グロー: H=15-42, S=80-220, V=150-255 (より高輝度)
        lower = np.array([15, 80, 150], dtype=np.uint8)
        upper = np.array([42, 220, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)

        # モルフォロジー: 枠線を繋げて矩形を再現
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        mask = cv2.dilate(mask, kernel, iterations=3)
        mask = cv2.erode(mask, kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # 最大面積の輪郭を採用 (小さなノイズを除外)
        valid = [(cv2.contourArea(c), c) for c in contours if cv2.contourArea(c) > 500]
        if not valid:
            return None

        _, best_cnt = max(valid, key=lambda x: x[0])
        rx, ry, rw, rh = cv2.boundingRect(best_cnt)
        cx = rx + rw // 2
        cy = ry + rh // 2
        logger.info("  [GoldHighlight] 金色ハイライト検出 rect=(%d,%d,%d,%d) → center=(%d,%d)",
                    rx, ry, rw, rh, cx, cy)
        return cx, cy

    except Exception as e:
        logger.debug("  [GoldHighlight] エラー: %s", e)
        return None


# ─── OCR テキスト検索ヘルパー ──────────────────────
# ─── 探索マップ 3D矢印 検出 ──────────────────────────
def find_3d_arrow(img_path: Path) -> Optional[tuple[int, int]]:
    """
    探索マップ上のキャラ頭上に浮かぶ3D矢印（白い曲線矢印）を検出。
    明るい白色コンターが最大のものを矢印とみなす。
    Returns: (cx, cy) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        # キャラ頭上エリア
        roi_y1, roi_y2 = _CHAR_HEAD_Y1, _CHAR_HEAD_Y2
        roi_x1, roi_x2 = _CHAR_HEAD_X1, _CHAR_HEAD_X2
        roi = img[roi_y1:roi_y2, roi_x1:roi_x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, bright = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        # サイズフィルタ: 30〜800px² の中からY座標が最も上（小）のものを矢印とみなす
        # (面積最大だとキャラの衣装/武器を誤検出するため)
        candidates = [(cv2.contourArea(c), c) for c in contours
                      if 30 <= cv2.contourArea(c) <= 800]
        if not candidates:
            return None
        # Y座標が最も小さい（画面上部に近い）ものを選択
        def top_y(pair):
            c = pair[1]
            M = cv2.moments(c)
            return (M["m01"] / M["m00"]) if M["m00"] > 0 else 9999
        area, best = min(candidates, key=top_y)
        if area < 30:
            return None
        M = cv2.moments(best)
        if M["m00"] == 0:
            return None
        cx = int(M["m10"] / M["m00"]) + roi_x1
        cy = int(M["m01"] / M["m00"]) + roi_y1
        logger.debug("[3D_ARROW] area=%.0f center=(%d,%d)", area, cx, cy)
        return (cx, cy)
    except Exception as e:
        logger.debug("find_3d_arrow error: %s", e)
        return None


# ─── UI資産ライブラリ (AssetManager) ──────────────
class AssetManager:
    """
    assets/templates/ 内のテンプレート画像を使った高速 UI マッチング。

    ファイル構成:
      assets/templates/{name}.png   — グレースケールテンプレート画像
      assets/templates/{name}.json  — メタデータ (threshold, action, offset)

    処理時間: ~0.1s (OCR比: 20-50倍高速)
    """

    TEMPLATES_DIR = _CRAWLER_ROOT / "assets" / "templates"
    DEFAULT_THRESHOLD = 0.80

    def __init__(self):
        self._templates: dict[str, dict] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        count = 0
        for png in sorted(self.TEMPLATES_DIR.glob("*.png")):
            name = png.stem
            img = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            meta: dict = {}
            meta_path = png.with_suffix(".json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass
            self._templates[name] = {
                "img": img,
                "threshold": float(meta.get("threshold", self.DEFAULT_THRESHOLD)),
                "action": meta.get("action", f"ASSET_{name.upper()}"),
                "offset": meta.get("offset", [0, 0]),
                "require_ocr": meta.get("require_ocr", []),
                "require_ocr_all": meta.get("require_ocr_all", []),
            }
            count += 1
        if count:
            logger.info("[AssetManager] %d テンプレート読込: %s",
                        count, list(self._templates.keys()))

    def match(self, screenshot_path: Path,
              ocr_texts: Optional[list[str]] = None,
              ) -> Optional[tuple[int, int, str, tuple[int, int, int, int]]]:
        """
        スクリーンショットと全テンプレートを比較。
        ocr_texts が渡された場合、require_ocr 条件を満たすテンプレートのみ照合。
        Returns: (tap_x, tap_y, action_name, button_region) or None
            button_region = (bx, by, bw, bh) — テンプレートマッチ領域
        """
        if not self._templates:
            return None
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        best_score = 0.0
        best_result: Optional[tuple[int, int, str, tuple[int, int, int, int]]] = None
        for name, data in self._templates.items():
            if name in _SINGLE_ONLY:
                continue
            # require_ocr チェック: いずれか1つのキーワードがOCRにあればOK (OR条件)
            required = data.get("require_ocr", [])
            if required and ocr_texts is not None:
                if not any(kw in t for kw in required for t in ocr_texts):
                    logger.debug("[Asset] '%s' skip: require_ocr not found in OCR", name)
                    continue
            # require_ocr_all チェック: すべてのキーワードがOCRに存在しなければスキップ (AND条件)
            required_all = data.get("require_ocr_all", [])
            if required_all and ocr_texts is not None:
                if not all(any(kw in t for t in ocr_texts) for kw in required_all):
                    logger.debug("[Asset] '%s' skip: require_ocr_all not all found in OCR", name)
                    continue
            tmpl = data["img"]
            if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            try:
                res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= data["threshold"] and max_val > best_score:
                    best_score = max_val
                    h, w = tmpl.shape
                    bx = max_loc[0] + int(data["offset"][0])
                    by = max_loc[1] + int(data["offset"][1])
                    cx = bx + w // 2
                    cy = by + h // 2
                    best_result = (cx, cy, data["action"], (bx, by, w, h))
                    logger.debug("[Asset] '%s' score=%.3f at (%d,%d)", name, max_val, cx, cy)
            except Exception as e:
                logger.debug("[Asset] match error '%s': %s", name, e)
        if best_result:
            cx, cy, action, _ = best_result
            logger.info("[Asset] HIT: '%s' score=%.3f → (%d,%d)", action, best_score, cx, cy)
        return best_result

    def match_single(self, name: str, screenshot_path: Path) -> Optional[tuple[int, int, float]]:
        """指定テンプレート1枚だけをマッチング。Returns (cx, cy, score) or None."""
        data = self._templates.get(name)
        if data is None:
            return None
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        tmpl = data["img"]
        if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
            return None
        try:
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= data["threshold"]:
                h, w = tmpl.shape
                cx = max_loc[0] + w // 2
                cy = max_loc[1] + h // 2
                return (cx, cy, max_val)
        except Exception:
            pass
        return None

    def save_template(self, screenshot_path: Path,
                      x1: int, y1: int, x2: int, y2: int,
                      name: str, action: str,
                      offset: tuple[int, int] = (0, 0),
                      threshold: float = DEFAULT_THRESHOLD,
                      require_ocr: list[str] | None = None) -> bool:
        """
        スクリーンショットの指定領域を切り抜いてテンプレートとして保存。
        次回起動時から [Asset Match] で高速検出可能になる。
        require_ocr: このテンプレートを使うのに必要なOCRキーワードリスト
        """
        img = cv2.imread(str(screenshot_path))
        if img is None:
            return False
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        out_png = self.TEMPLATES_DIR / f"{name}.png"
        meta_path = self.TEMPLATES_DIR / f"{name}.json"
        cv2.imwrite(str(out_png), crop)
        meta: dict = {"action": action, "offset": list(offset), "threshold": threshold}
        if require_ocr:
            meta["require_ocr"] = require_ocr
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        # インメモリキャッシュに即時追加
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        self._templates[name] = {
            "img": gray, "threshold": threshold,
            "action": action, "offset": list(offset),
            "require_ocr": require_ocr or [],
        }
        logger.info("[Asset] テンプレート自動保存: '%s' (%dx%d) action=%s require_ocr=%s",
                    name, crop.shape[1], crop.shape[0], action, require_ocr)
        return True



# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


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
            tap_device(cx, cy, state, "GACHA_RESULT_OK_1", post_wait=0.3)
            tap_device(cx, cy, state, "GACHA_RESULT_OK_2")
        else:
            _gc_x, _gc_y = roi_to_device(
                int(W * 0.5), int(H * 0.5), state.game_roi)
            logger.info(">>> 【ガチャ結果初期】 OK未検出 → 画面中央ダブルタップ (%d,%d)",
                        _gc_x, _gc_y)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_1",
                       post_wait=0.3)
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
) -> Optional[tuple[str, float]]:
    """ダイアログ検出ハンドラ (#0-DIALOG)。

    detect_dialog_frame_and_nav() で金色枠/×/▷ を検出し、
    Spatial Gate / White Hand ガード / エスカレーション を経てタップ実行。

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

    # ── 【#-2.5】SKIP ボタン汎用ハンドラ (カットシーン/ムービー) ──
    # "SKIP" / "スキップ" を検出 → 即タップでカットシーンをスキップ。
    # バトル中 ("通常攻撃" 等) は除外 (スキルボタンとの誤検出防止)。
    _in_battle_ctx = any(kw in joined for kw in _BATTLE_CORE_KWS)
    if not _in_battle_ctx:
        _skip_btn = has_any(ocr, ["SKIP", "スキップ", "SKP", "SKIR", "SKlP", "SKLP"])
        if _skip_btn:
            _sk_x, _sk_y = _skip_btn["center"]
            logger.info(">>> [SKIP] カットシーンスキップ '%s' (%d,%d) タップ",
                        _skip_btn["text"], _sk_x, _sk_y)
            tap_device(_sk_x, _sk_y, state, f"CUTSCENE_SKIP '{_skip_btn['text']}'")
            return "CUTSCENE_SKIP", 0.5

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
            logger.info(">>> 【ご注意→phash監視】 #%d タップ(%d,%d) → 1s待機",
                        _retry_i + 1, _tap_x, _tap_y)
            time.sleep(1.0)
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
            logger.info(">>> 【Unity初期化待機】 60秒 Watchdog停止 (NOTICE_DISMISS exempt)")
            return "NOTICE_DISMISS", 60.0
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

    # ── 【#0-DIALOG 前ガード】指ブロブ検出時はダイアログ検出をスキップ ──────
    _pre_dialog_finger = False
    _is_result_screen = any(
        any(k in t for k in ("Result", "リザルト", "次へ"))
        for t in texts
    )
    if analysis_path is not None and not _is_result_screen:
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
        state, analysis_path, ocr, texts, _is_battle_early, _pre_dialog_finger)
    if _dialog_result is not None:
        return _dialog_result

    # ─── 【最優先 #0-aa】HSV金色ポインター検出 → ホールドスワイプ (Type A) ───
    # 縦長金色領域 h/w>=3.5 かつ幅<=100px のみ有効 (ボタン/カード誤検出防止)。
    # チュートリアル3D移動シーン(チェッカー床/階段/廊下)で発火。
    # phash監視: スワイプ後2s待機 → 変化なければ再実行 (最大2回)
    # バトルUI（通常攻撃・単体攻撃・WAVE・Turn）が見えるとき はバトル中なのでスキップ
    _is_battle_ui = any(kw in joined for kw in _BATTLE_UI_KWS)
    if analysis_path is not None and not _is_battle_ui:
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
                for _gs_retry in range(2):
                    if _dir == "UP":
                        logger.info(">>> [GoldSwipe] SWIPE_UP (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
                    else:
                        logger.info(">>> [GoldSwipe] SWIPE_DOWN (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
                    time.sleep(1.0)
                    _new_ss, _, _, _ = take_screenshot()
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
                    sx = tmpl_meta.get("swipe_from_x", cx)
                    sy = tmpl_meta.get("swipe_from_y", H - 50)
                    ex = tmpl_meta.get("swipe_to_x", cx)
                    ey = tmpl_meta.get("swipe_to_y", 50)
                    dur = tmpl_meta.get("swipe_duration_ms", 10000)
                    logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms", sx, sy, ex, ey, dur)
                    swipe(sx, sy, ex, ey, dur, state=state)
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
                    time.sleep(1.0)
                    adb("shell input text MadoDora")
                    time.sleep(1.0)
                    # IME変換確定 (ENTER) → キーボード閉じる (BACK) → ダイアログOKタップ
                    adb("shell input keyevent 66")   # KEYCODE_ENTER: IME変換確定
                    time.sleep(0.5)
                    adb("shell input keyevent KEYCODE_BACK")  # keyboard dismiss
                    time.sleep(1.0)
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
            time.sleep(0.3)
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
            tap_device(_cn_x, _cn_y, state, "CAROUSEL_NAV_RIGHT", post_wait=0.3)
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
        # ホーム画面検出: ホームナビキーワードが2個以上 → キャラ画像のブロブ誤検出をスキップ
        _home_nav_kws = ["クエスト", "ショップ", "ガチャ", "ガシャ", "ユニオン",
                         "光の間", "パーティ", "プレイヤーマッチ", "お知らせ",
                         "イベント", "マイページ", "編成", "MAGIA EXEDRA"]
        _home_kw_count = sum(1 for h in _home_nav_kws if any(h in t for t in texts))
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
            logger.info("  ホーム画面検出 (nav×%d) → MOYA_TAP スキップ", _home_kw_count)
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
                left_char = [b for b in blobs if b[0] < 600 and b[1] > H * 0.76]
                # right_panel: スキルボタンは下半分(y>H*0.45)のみ。上部の蝶エネミーを排除
                right_panel = [b for b in blobs if b[0] > _RIGHT_PANEL_X and b[1] > H * 0.45]
                bottom_ui = [b for b in blobs if b[1] > H * 0.8 and b[0] >= 600]
                if state.char_just_selected:
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
                    swipe(fx, H - 50, fx, 50, 10000, state=state)
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
                        time.sleep(1.5)
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
                    logger.info(">>> [RECOVERY s10] 5秒待機 + カウンタリセット")
                    time.sleep(5.0)
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
                        swipe(fx, H - 50, fx, 50, 10000, state=state)
                        time.sleep(0.3)
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
        # 安全弁: HOME_TUTORIAL_TAP 15回超 → 偽検出と判断しチュートリアル完了扱い
        _tutorial_tap_limit = state.home_tutorial_tap_count >= 15
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
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL", post_wait=0.8)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.5

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL", post_wait=0.8)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.5

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
                tap_device(sx, sy, state, "STALL_SKILL", post_wait=0.8)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 1.0
            elif stall_phase == 4:
                hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU", post_wait=0.8)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 1.0
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

    # ─── スキップ ───
    # OCRがSKIPを'SK'と短縮検出するケースも考慮: 右上エリア(x>1000, y<100)に限定
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if not skip_match:
        _sk_match = has_any(ocr, ["SK", "Sk"])
        if _sk_match:
            _sk_cx, _sk_cy = _sk_match["center"]
            if _sk_cx > 1000 and _sk_cy < 100:
                skip_match = _sk_match
    if skip_match:
        cx, cy = skip_match["center"]
        text = skip_match["text"]
        logger.info(">>> スキップ '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"SKIP '{text}'")
        return "SKIP", 0.5

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
            swipe(int(W * 0.46), int(H * 0.69), int(W * 0.46), int(H * 0.28), 500, state=state)
            time.sleep(0.8)
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
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6]
    if lower_texts and len(ocr) <= 15:
        target = lower_texts[-1]
        cx, cy = target["center"]
        # ADVツールバー検出時 → ↓矢印ボタンをテンプレートマッチで優先
        if analysis_path and is_adv_toolbar_cached(analysis_path, state):
            _next_btn = ASSET_MANAGER.match_single("adv_next_btn", analysis_path)
            if _next_btn:
                cx, cy = _next_btn[0], _next_btn[1]
                logger.info(">>> ストーリー送り '%s' → ↓ボタン (%d,%d)", target["text"][:10], cx, cy)
                tap_device(cx, cy, state, "STORY_TAP")
                return "STORY_TAP", 0.3
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

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


# ─── Watchdog: 物理的 ADB 生存確認 ────────────────────────
def check_adb_liveness() -> bool:
    """
    ADB 接続の物理的な生存を確認する。
    - adb shell echo 1: 応答確認 (timeout 3s)
    - adb shell screencap: 転送サービスのハング確認 (timeout 3s)
    Returns: True=接続正常, False=タイムアウト/エラー(要再起動)
    """
    _serial_arg = ["-s", DEVICE_SERIAL] if DEVICE_SERIAL else []
    try:
        # echo テスト
        _r1 = subprocess.run(
            ["adb"] + _serial_arg + ["shell", "echo", "1"],
            capture_output=True, timeout=3, text=True,
        )
        if _r1.returncode != 0 or _r1.stdout.strip() != "1":
            logger.warning("[WATCHDOG] echo 応答異常: rc=%d out=%r", _r1.returncode, _r1.stdout.strip())
            return False
        # screencap パイプテスト (実際には読まない — ハングを検出するだけ)
        _r2 = subprocess.run(
            ["adb"] + _serial_arg + ["shell", "screencap", "-p", "/dev/null"],
            capture_output=True, timeout=3,
        )
        if _r2.returncode != 0:
            logger.warning("[WATCHDOG] screencap ハング検出: rc=%d", _r2.returncode)
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("[WATCHDOG] ADB コマンドタイムアウト — 物理診断失敗")
        return False
    except Exception as _e:
        logger.warning("[WATCHDOG] 物理診断例外: %s", _e)
        return False


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
        logger.info("[FAST] GoldSwipe %s → swipe (%d,%d)→(%d,%d) %dms",
                    _dir, _sx, _fy, _sx, _ty, _dur)
        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
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
    return parser.parse_args()


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
    MAX_OCR_ATTEMPTS = 5
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

        logger.info("[FRESH_INSTALL] インストールボタン未検出 — 待機")
        time.sleep(3)

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
    global DEVICE_SERIAL, SCRCPY_DEVICE

    args = parse_args()
    global _DEBUG_SAVE_IMAGES
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        _DEBUG_SAVE_IMAGES = True

    # ─── ADB 自動接続: USB → Wi-Fi フォールバック ───
    try:
        _detected = ensure_adb_connection(
            wifi_addr=args.wifi_addr or WIFI_DEVICE_ADDR,
            pairing_code=args.pairing_code,
            pairing_port=args.pairing_port,
        )
        if not os.environ.get("ANDROID_UDID") and not os.environ.get("ANDROID_SERIAL"):
            os.environ["ANDROID_UDID"] = _detected
        DEVICE_SERIAL = get_android_serial()
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # scrcpy デバイスを接続済みシリアルから動的設定
    SCRCPY_DEVICE = DEVICE_SERIAL

    # ─── --fresh-install: アンインストール → Play Store 再インストール ───
    if args.fresh_install:
        _fresh_install_from_play_store(DEVICE_SERIAL, APP_PACKAGE)

    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (ハイブリッド版)")
    logger.info("  デバイス: %s", DEVICE_SERIAL)
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
                if DEVICE_SERIAL:
                    subprocess.run(["adb", "connect", DEVICE_SERIAL], timeout=5)
                    time.sleep(1)
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
                    subprocess.run(["adb", "connect", DEVICE_SERIAL], timeout=5, capture_output=True)
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
                if _new_roi[2] >= ANALYSIS_W * 0.5:
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
            # ── ムービースキップ: 連続暗転 5回以上 → 右上スキップボタン試行 ──
            if state.consecutive_blackouts >= 5 and state.consecutive_blackouts % 5 == 0:
                skip_x = int(ANALYSIS_W * 0.97)  # 右上 ≈ (1474, 50)
                skip_y = int(ANALYSIS_H * 0.07)
                logger.info("[CINEMATIC_SKIP] 連続暗転 %d 回 → 右上スキップボタン試行 (%d,%d)",
                            state.consecutive_blackouts, skip_x, skip_y)
                tap_device(skip_x, skip_y, state, "CINEMATIC_SKIP")
                time.sleep(1.0)
                # スキップ確認ダイアログが出る可能性 → 中央タップ
                tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.6), state, "CINEMATIC_SKIP_CONFIRM")
                time.sleep(2.0)
            else:
                time.sleep(2.0)
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

        if screen_changed:
            # 画面変化あり → カウンタリセット & Watchdog タイマーリセット
            state.same_phash_count = 0
            state.consecutive_frozen_frames = 0
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.pre_popup_tap_count = 0  # ポップアップ試行カウンタもリセット
            state.dialog_close_total = 0  # ダイアログclose累計もリセット
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
            if (state.last_action in ("STORY_TAP", "ADV_RAPID_TAP", "STORY_TAP_HINT",
                                      "MOYA_TAP", "MOVIE_SKIP", "MOVIE_WAIT") and
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
                    # レターボックス最優先: 左黒帯>=80px → 動画確定 (ツールバー誤検出を防ぐ)
                    _rapid_roi_x = state.game_roi[0] if state.game_roi else 0
                    if _rapid_roi_x >= 80:
                        _movie_btn = detect_movie_skip_button(img_path)
                        if _movie_btn:
                            _ms_x, _ms_y = roi_to_device(_movie_btn[0], _movie_btn[1], state.game_roi)
                            tap_device(_ms_x, _ms_y, state, "MOVIE_SKIP")
                            logger.info("  ACTION_TAKEN MOVIE_SKIP (%d,%d) [ADV_RAPID letterbox]", _ms_x, _ms_y)
                            state.movie_wait_consecutive = 0
                        else:
                            state.movie_wait_consecutive += 1
                            logger.info("[iter %d] phash_dist=%d レターボックス動画 → 待機 (%d/%d)",
                                        i, dist, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                            state.last_action = "MOVIE_WAIT"
                            time.sleep(2.0)
                        state.last_phash = cur_phash
                        continue
                    # ADV vs 動画シーン判別: ツールバー有無で分岐
                    if is_adv_toolbar_cached(img_path, state):
                        # ↓矢印ボタンをテンプレートマッチで検出
                        _adv_btn = ASSET_MANAGER.match_single("adv_next_btn", img_path)
                        if _adv_btn:
                            _adv_x, _adv_y = _adv_btn[0], _adv_btn[1]
                            logger.info("[iter %d] phash_dist=%d ADV_RAPID → ↓ボタン (%.3f)", i, dist, _adv_btn[2])
                        else:
                            _adv_x, _adv_y = roi_to_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.9), state.game_roi)
                            logger.info("[iter %d] phash_dist=%d ADV_RAPID → 中央下フォールバック", i, dist)
                        tap_device(_adv_x, _adv_y, state, "ADV_RAPID_TAP", post_wait=0.3)
                        logger.info("  ACTION_TAKEN ADV_RAPID_TAP (%d,%d)", _adv_x, _adv_y)
                        state.movie_wait_consecutive = 0
                        state.last_phash = cur_phash
                        continue
                    else:
                        # 動画シーン → ⏭ スキップボタン探索 (タップ抑制)
                        _movie_btn = detect_movie_skip_button(img_path)
                        if _movie_btn:
                            _ms_x, _ms_y = roi_to_device(_movie_btn[0], _movie_btn[1], state.game_roi)
                            tap_device(_ms_x, _ms_y, state, "MOVIE_SKIP")
                            logger.info("  ACTION_TAKEN MOVIE_SKIP (%d,%d)", _ms_x, _ms_y)
                            state.movie_wait_consecutive = 0
                            state.last_phash = cur_phash
                            continue
                        else:
                            state.movie_wait_consecutive += 1
                            logger.info("[iter %d] phash_dist=%d 動画再生中 → 待機 (%d/%d)",
                                        i, dist, state.movie_wait_consecutive, _MOVIE_WAIT_ESCAPE)
                            state.last_action = "MOVIE_WAIT"
                            state.last_phash = cur_phash
                            time.sleep(2.0)
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
                    if DEVICE_SERIAL:
                        subprocess.run(["adb", "connect", DEVICE_SERIAL], timeout=5)
                        time.sleep(1)
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
                # ── ADV送り待ちアイコン検知: phash 安定中でも即タップ ──
                # 動画シーンでは ADV ツールバーが無いためタップ抑制
                if state.current_scene in ("STORY", "ADV"):
                    if is_adv_toolbar_cached(img_path, state):
                        if detect_adv_advance_icon(img_path):
                            logger.info("[ADV_ADVANCE][iter %d] 送り待ちアイコン検出 → 即タップ", i)
                            # ↓矢印ボタンをテンプレートマッチで検出
                            _aa_btn = ASSET_MANAGER.match_single("adv_next_btn", img_path)
                            if _aa_btn:
                                _aa_x, _aa_y = _aa_btn[0], _aa_btn[1]
                            else:
                                _aa_x, _aa_y = roi_to_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.9), state.game_roi)
                            tap_device(_aa_x, _aa_y, state, "ADV_ADVANCE")
                            state.last_phash = ""
                            state.same_phash_count = 0
                            state.stall_start = 0.0
                            time.sleep(0.5)
                            continue
                    else:
                        # 動画シーン → ⏭ スキップボタン探索
                        _movie_btn = detect_movie_skip_button(img_path)
                        if _movie_btn:
                            _ms_x, _ms_y = roi_to_device(_movie_btn[0], _movie_btn[1], state.game_roi)
                            tap_device(_ms_x, _ms_y, state, "MOVIE_SKIP")
                            logger.info("  ACTION_TAKEN MOVIE_SKIP (%d,%d) [phash stable]", _ms_x, _ms_y)
                            state.last_phash = ""
                            state.same_phash_count = 0
                            time.sleep(1.0)
                            continue
                        else:
                            logger.info("[MOVIE_WAIT] 動画再生中 → 待機 (phash stable)")
                            state.last_action = "MOVIE_WAIT"
                            time.sleep(2.0)
                            continue
                state.last_phash = cur_phash
                time.sleep(_poll)
                continue

            # ── スタック介入 (強制OCRでもタップできず続いた場合) ──
            if state.stall_start == 0.0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
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
                    subprocess.run(["adb", "-s", DEVICE_SERIAL, "shell", "am", "force-stop",
                                    APP_PACKAGE], timeout=5)
                    time.sleep(3)
                    subprocess.run(["adb", "-s", DEVICE_SERIAL, "shell", "am", "start", "-n",
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
        analysis_path = prepare_analysis_image(img_path, actual_w, actual_h)

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
            time.sleep(_result_action[1])
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
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override):
            _rapid_tx = _rapid_ty = 0
            _rapid_action = ""
            _rapid_double = False

            # ── Phase A: アクティブキャラ検出 (赤/ピンク発光ハロー) ──
            # 【永続ルール】キャラ選択モヤ = 赤/ピンクの発光。明度差で識別。
            _active_char = detect_active_battle_char(analysis_path, ANALYSIS_W, ANALYSIS_H)

            if not state.character_selected and _active_char is not None:
                _rapid_tx, _rapid_ty = _active_char[0], _active_char[1]
                _rapid_action = "BATTLE_RAPID_ACTIVE_P1"
                _rapid_double = True

            # ── Phase B: 右側スキル/攻撃ボタン ──
            if not _rapid_action:
                _rapid_glows = detect_guide_glow(
                    analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
                _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]

                _rapid_blobs = find_finger_blobs(analysis_path, min_area=200, dark_mode=True)
                _rapid_blobs = [b for b in _rapid_blobs
                                if b[1] > _SPATIAL_MARGIN_TOP and b[0] < ANALYSIS_W - _CLOSE_BTN_OFFSET]
                _right_panel = [b for b in _rapid_blobs
                                if b[0] > _RIGHT_PANEL_X and b[1] > ANALYSIS_H * 0.45]

                if state.character_selected or state.char_just_selected:
                    # キャラ選択済み → 右スキル優先
                    if _rapid_right_g:
                        _rr = max(_rapid_right_g, key=lambda g: g["area"])
                        _rapid_tx, _rapid_ty = _rr["cx"], max(1, _rr["cy"] - _GLOW_CENTER_Y_OFFSET)
                        _rapid_action = "BATTLE_RAPID_GLOW_P2"
                    elif _right_panel:
                        _tb = max(_right_panel, key=lambda b: b[2])
                        _rapid_tx, _rapid_ty = _tb[0], max(1, _tb[1] - _GLOW_CENTER_Y_OFFSET)
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
                          post_wait=0.5 if _rapid_double else 1.0)
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
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_RAPID)", _fms)
                continue  # OCR スキップ

        # ── 4.5) BATTLE 高速パス: OCR 前テンプレートマッチング ──
        # BATTLE シーンで GoldBtn/GoldSwipe が見つかれば OCR (6-8s) をスキップ
        # ※ 強制 OCR 時はスキップ (ダイアログ検出を優先)
        if state.current_scene == "BATTLE" and not _force_ocr_override:
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
            time.sleep(1)
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

        # ── シーン分類 ──
        scene, next_interval = classify_scene(texts, state.last_action)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── 動画シーン検出: detect_and_act 前にガード ──
        # 動画中にタップするとUIが一時停止/再生を繰り返すため抑制する
        # 検出条件:
        #   A) レターボックス (左黒帯>=80px) + ADVツールバーなし
        #   B) レターボックスなしでも ⏭スキップボタン検出 + ADVツールバーなし
        # ただし OCR で UI テキストが豊富な場合は動画ではない (利用規約画面等)
        _roi_x = state.game_roi[0] if state.game_roi else 0
        _is_movie_letterbox = _roi_x >= 80
        _has_ui_text = any(kw in _ocr_text_joined for kw in _UI_TEXT_KWS) or len(texts) >= 8
        _movie_candidate = (
            _is_movie_letterbox or (len(texts) <= 3 and scene not in ("BATTLE", "MENU"))
        )
        if _movie_candidate and not _has_ui_text and scene not in ("BATTLE", "MENU") and analysis_path:
            if not is_adv_toolbar_cached(analysis_path, state):
                _movie_btn = detect_movie_skip_button(analysis_path)
                if _movie_btn:
                    _ms_x, _ms_y = roi_to_device(
                        _movie_btn[0], _movie_btn[1], state.game_roi)
                    tap_device(_ms_x, _ms_y, state, "MOVIE_SKIP")
                    logger.info(
                        "  ACTION_TAKEN MOVIE_SKIP (%d,%d) [letterbox L=%d]",
                        _ms_x, _ms_y, _roi_x)
                    state.last_phash = cur_phash
                    continue
                elif _is_movie_letterbox:
                    # レターボックスあり + ⏭なし → 動画待機
                    logger.info(
                        "[MOVIE_GUARD] レターボックス(L=%d)+ツールバーなし → 待機",
                        _roi_x)
                    state.last_action = "MOVIE_WAIT"
                    time.sleep(2.0)
                    state.last_phash = cur_phash
                    continue
                # レターボックスなし + ⏭なし → 動画ではない → detect_and_act へ

        # ── 6) 判定 & アクション (finger blob も渡す) ──
        action, wait_sec = detect_and_act(ocr_results, state, analysis_path)
        state.last_action = action
        # フルOCR解析に到達 → MOVIE_WAIT脱出カウンタリセット
        if action != "MOVIE_WAIT":
            state.movie_wait_consecutive = 0

        # ── シーン再評価: 同一アクション連続時にシーン認識を疑う ──
        if action == state.last_action and action not in (
            "WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT",
            "MOVIE_WAIT", "LOADING_WAIT",
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
                _re_scene, _ = classify_scene(_re_texts, action)
                if _re_scene != state.current_scene:
                    logger.warning(
                        "[SCENE_REEVAL] シーン不一致: %s → %s → 切替+再判定",
                        state.current_scene, _re_scene,
                    )
                    state.current_scene = _re_scene
                # レターボックスガード (動画シーンでのdetect_and_actバイパス)
                _re_roi_x = state.game_roi[0] if state.game_roi else 0
                if _re_roi_x >= 80 and _re_scene not in ("BATTLE", "MENU"):
                    if not is_adv_toolbar_visible(_re_analysis):
                        logger.info("[SCENE_REEVAL] レターボックス動画 → MOVIE_WAIT")
                        state.last_action = "MOVIE_WAIT"
                        state.action_repeat_count = 0
                        state.scene_reeval_mode = False
                        time.sleep(2.0)
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

        # ── 8) 待機 (DOWNLOAD_WAIT は phash 監視付き適応ポーリング) ──
        if wait_sec > 0:
            if action == "DOWNLOAD_WAIT" and wait_sec >= 5.0:
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
                logger.info("  [%s][%s] wait %.1fs | next_check: %.1fs",
                            scene, action, wait_sec, next_interval)
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
