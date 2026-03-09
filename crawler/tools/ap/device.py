"""
ap/device.py — ADB / デバイス操作 (tap, swipe, screenshot, scrcpy)
"""
from __future__ import annotations

import cv2
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H, MIN_TAP_INTERVAL, SCREENSHOT_PATH,
    REMOTE_PATH, _DEBUG_SAVE_IMAGES,
)

logger = logging.getLogger("auto_pilot")

# ─── 設定 (main() で動的に設定) ───
DEVICE_SERIAL = ""   # main() で設定される
SCRCPY_DEVICE = ""   # main() で DEVICE_SERIAL から動的設定


def set_device_serial(s: str) -> None:
    """DEVICE_SERIAL をモジュール外から設定する。"""
    global DEVICE_SERIAL
    DEVICE_SERIAL = s


def set_scrcpy_device(s: str) -> None:
    """SCRCPY_DEVICE をモジュール外から設定する。"""
    global SCRCPY_DEVICE
    SCRCPY_DEVICE = s


def _build_scrcpy_args(device_serial: str) -> list:
    """scrcpy 起動引数を動的に構築する。"""
    return [
        "scrcpy",
        "-s", device_serial,
        "--turn-screen-off",   # 物理画面消灯
        "--stay-awake",
        "--window-width", str(ANALYSIS_W),
        "--window-height", str(ANALYSIS_H),
    ]


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
    """
    _fallback = int(ANALYSIS_H * 0.067)  # 48/720 ≈ 0.067
    try:
        _out = subprocess.run(
            ["adb", "-s", DEVICE_SERIAL, "shell", "dumpsys", "display"],
            capture_output=True, text=True, timeout=5,
        ).stdout
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
    """
    path = Path(SCREENSHOT_PATH)
    _retried = 0
    for _attempt in range(retries):
        try:
            _result = subprocess.run(
                ["adb", "-s", DEVICE_SERIAL, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10,
            )
            if _result.returncode == 0 and len(_result.stdout) >= min_bytes:
                path.write_bytes(_result.stdout)
            else:
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
        _fsize = path.stat().st_size if path.exists() else 0
        if _fsize < min_bytes:
            logger.warning("[SCREENSHOT] 破損疑い: size=%d bytes (attempt %d/%d) — 再取得",
                           _fsize, _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        _test = cv2.imread(str(path))
        if _test is None or _test.size == 0:
            logger.warning("[SCREENSHOT] cv2.imread 失敗/空 (attempt %d/%d) — 再取得",
                           _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        _h, _w = _test.shape[:2]
        return path, _w, _h, _retried
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
        return None

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


def tap_device(x: int, y: int, state, desc: str = "",
               finger_box=None, gold_box=None,
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
                # _rejected_finger_blobs は auto_pilot.py 側で管理
                _out = str(Path(__file__).parent.parent.parent / "debug_latest_tap.png")
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
          state=None) -> None:
    if state and state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        rx1, ry1 = int(x1 * sx), int(y1 * sy)
        rx2, ry2 = int(x2 * sx), int(y2 * sy)
    else:
        rx1, ry1, rx2, ry2 = x1, y1, x2, y2
    adb(f"shell input swipe {rx1} {ry1} {rx2} {ry2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", rx1, ry1, rx2, ry2, duration_ms)


def check_adb_liveness() -> bool:
    """
    ADB 接続の物理的な生存を確認する。
    Returns: True=接続正常, False=タイムアウト/エラー(要再起動)
    """
    _serial_arg = ["-s", DEVICE_SERIAL] if DEVICE_SERIAL else []
    try:
        _r1 = subprocess.run(
            ["adb"] + _serial_arg + ["shell", "echo", "1"],
            capture_output=True, timeout=3, text=True,
        )
        if _r1.returncode != 0 or _r1.stdout.strip() != "1":
            logger.warning("[WATCHDOG] echo 応答異常: rc=%d out=%r", _r1.returncode, _r1.stdout.strip())
            return False
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
