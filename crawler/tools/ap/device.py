"""
ap/device.py — ADB / デバイス操作 (tap, swipe, screenshot, scrcpy)
"""
from __future__ import annotations

import cv2
import logging
import numpy as np
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H, MIN_TAP_INTERVAL, SCREENSHOT_PATH,
    REMOTE_PATH, _DEBUG_SAVE_IMAGES,
    APP_PACKAGE, APP_ACTIVITY,
)

logger = logging.getLogger("auto_pilot")

# ─── Quartz (macOS window capture) ───
_HAS_QUARTZ = False
try:
    if sys.platform == "darwin":
        import Quartz as _Quartz
        _HAS_QUARTZ = True
except ImportError:
    pass

# ─── 設定 (main() で動的に設定) ───
DEVICE_SERIAL = ""   # main() で設定される
SCRCPY_DEVICE = ""   # main() で DEVICE_SERIAL から動的設定
_SCRCPY_WINDOW_ID: int = 0   # キャッシュ (0=未取得)
_LAST_SCRCPY_BGR: Optional[np.ndarray] = None  # scrcpy キャプチャの BGR キャッシュ (二重読み防止)
_SCRCPY_FAIL_COUNT: int = 0  # scrcpy キャプチャ連続失敗回数 (自動復帰用)
_SCRCPY_FAIL_RESTART_THRESHOLD: int = 3  # N回連続失敗でscrcpy再起動
_SCRCPY_LAST_RESTART: float = 0.0  # 最後にscrcpyを再起動した時刻
_SCRCPY_RESTART_COOLDOWN: float = 30.0  # 再起動後のクールダウン (秒)


def set_device_serial(s: str) -> None:
    """DEVICE_SERIAL をモジュール外から設定する。"""
    global DEVICE_SERIAL
    DEVICE_SERIAL = s


def set_scrcpy_device(s: str) -> None:
    """SCRCPY_DEVICE をモジュール外から設定する。"""
    global SCRCPY_DEVICE
    SCRCPY_DEVICE = s


def _build_scrcpy_args(device_serial: str) -> list:
    """
    scrcpy 起動引数を動的に構築する。

    映像品質: 解析基準 (ANALYSIS_W=1520) に合わせて --max-size を設定。
    ウィンドウ表示: --window-width=720 で Mac 上のウィンドウサイズを小さく保つ。
    これにより拡大リサイズによるテンプレートマッチ精度低下を防止。
    """
    dev_w, dev_h = get_device_resolution()
    land_w = max(dev_w, dev_h)
    land_h = min(dev_w, dev_h)
    # 映像エンコード解像度: 解析基準幅 (1440) に固定。
    # フル解像度 (2160) だとUSB帯域を圧迫し scrcpy のコマ落ちが発生する。
    # 1440 なら解析リサイズが不要で精度劣化なし。720の2倍で一般的な解像度。
    _MAX_SIZE = ANALYSIS_W
    logger.info("[SCRCPY] 実機解像度 %dx%d → landscape %dx%d → max-size %d",
                dev_w, dev_h, land_w, land_h, _MAX_SIZE)
    # scrcpy バイナリ: PATH 検索で絶対パスを解決 (子プロセスの PATH 差異を回避)
    _scrcpy_bin = shutil.which("scrcpy") or "scrcpy"
    # --window-width を max-size と同じ値に固定。
    # Quartz キャプチャはウィンドウ表示サイズに依存するため、
    # ウィンドウが小さいとテンプレマッチ精度が劣化する。
    return [
        _scrcpy_bin,
        "-s", device_serial,
        "--turn-screen-off",
        "--stay-awake",
        "--max-size", str(_MAX_SIZE),
        "--window-width", str(_MAX_SIZE),
        # NOTE: --orientation は使わない。ポートレート画面も回転してしまうため。
        # ランドスケープ確認後に scrcpy を再起動してウィンドウサイズを復帰する。
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


def _find_scrcpy_window_id() -> int:
    """Quartz API で scrcpy ウィンドウ ID を取得。見つからなければ 0。

    scrcpy は複数のサブウィンドウ (タイトルバー/装飾) を持つため、
    最も大きい映像ウィンドウ (onscreen かつ幅>100) を優先選択する。
    """
    global _SCRCPY_WINDOW_ID
    if not _HAS_QUARTZ:
        return 0
    try:
        windows = _Quartz.CGWindowListCopyWindowInfo(
            _Quartz.kCGWindowListOptionAll, _Quartz.kCGNullWindowID)
        best_wid = 0
        best_area = 0
        for w in windows:
            if "scrcpy" not in w.get("kCGWindowOwnerName", "").lower():
                continue
            wid = w.get("kCGWindowNumber", 0)
            if not wid:
                continue
            bounds = w.get("kCGWindowBounds", {})
            bw = int(bounds.get("Width", 0))
            bh = int(bounds.get("Height", 0))
            area = bw * bh
            # 映像ウィンドウ: 幅>100px (タイトルバーや装飾を除外)
            if bw > 100 and bh > 100 and area > best_area:
                best_area = area
                best_wid = wid
        if best_wid:
            _SCRCPY_WINDOW_ID = best_wid
            return best_wid
    except Exception:
        pass
    _SCRCPY_WINDOW_ID = 0
    return 0


def _capture_scrcpy_window(wid: int, path: Path) -> Optional[np.ndarray]:
    """Quartz で scrcpy ウィンドウをキャプチャ → BGR numpy + PNG 保存。

    macOS のタイトルバー (~64 Retina px) が含まれるため、
    デバイスのアスペクト比から期待コンテンツ高さを算出してクロップする。
    """
    try:
        image = _Quartz.CGWindowListCreateImage(
            _Quartz.CGRectNull,
            _Quartz.kCGWindowListOptionIncludingWindow,
            wid,
            _Quartz.kCGWindowImageBoundsIgnoreFraming,
        )
        if image is None:
            return None
        width = _Quartz.CGImageGetWidth(image)
        height = _Quartz.CGImageGetHeight(image)
        if width < 100 or height < 100:
            return None
        bpr = _Quartz.CGImageGetBytesPerRow(image)
        data_provider = _Quartz.CGImageGetDataProvider(image)
        data = _Quartz.CGDataProviderCopyData(data_provider)
        arr = np.frombuffer(data, dtype=np.uint8).reshape(height, bpr // 4, 4)[:, :width, :]
        bgr = arr[:, :, :3].copy()  # BGRA → BGR (macOS Quartz は BGRA 順)
        # ── Quartz CF オブジェクトを即座に解放 (メモリリーク防止) ──
        del arr, data, data_provider, image
        # ── タイトルバークロップ: デバイスアスペクト比から期待高さを算出 ──
        dev_w, dev_h = _get_cached_device_resolution()
        if dev_w > 0 and dev_h > 0:
            expected_h = int(width * dev_h / dev_w)
            if height > expected_h + 10:
                _title_bar_h = height - expected_h
                bgr = bgr[_title_bar_h:, :, :]
                logger.debug("[SCRCPY_CAPTURE] タイトルバークロップ: %dpx (raw %dx%d → %dx%d)",
                             _title_bar_h, width, height, width, bgr.shape[0])
        cv2.imwrite(str(path), bgr)
        return bgr
    except Exception:
        return None


# ─── デバイス解像度キャッシュ (wm size 結果) ───
_CACHED_DEVICE_RES: tuple[int, int] = (0, 0)


def _get_cached_device_resolution() -> tuple[int, int]:
    """wm size の結果をランドスケープ正規化してキャッシュ。

    wm size はポートレート値 (1080x2160) を返すが、ゲームはランドスケープ。
    adb input tap はカレント座標系を使うため、長辺=width, 短辺=height に正規化する。
    """
    global _CACHED_DEVICE_RES
    if _CACHED_DEVICE_RES[0] > 0:
        return _CACHED_DEVICE_RES
    w, h = get_device_resolution()
    if w > 0 and h > 0:
        # ランドスケープ正規化: 長辺=width, 短辺=height
        land_w = max(w, h)
        land_h = min(w, h)
        _CACHED_DEVICE_RES = (land_w, land_h)
    return _CACHED_DEVICE_RES if _CACHED_DEVICE_RES[0] > 0 else (w, h)


def invalidate_device_resolution_cache() -> None:
    """デバイス解像度キャッシュを無効化する (デバイス変更時用)。"""
    global _CACHED_DEVICE_RES
    _CACHED_DEVICE_RES = (0, 0)


def _take_screenshot_scrcpy(path: Path) -> Optional[tuple[Path, int, int]]:
    """scrcpy ウィンドウキャプチャ (Quartz, ~100ms)。

    Returns: (path, device_w, device_h) or None
    ※ device_w/h は実機解像度 (wm size) を返す (scrcpy ウィンドウサイズではない)
    """
    global _LAST_SCRCPY_BGR
    if not _HAS_QUARTZ or os.environ.get("SCRCPY_DISABLED"):
        return None
    wid = _SCRCPY_WINDOW_ID or _find_scrcpy_window_id()
    if not wid:
        return None
    bgr = _capture_scrcpy_window(wid, path)
    if bgr is None:
        # キャプチャ失敗 → ウィンドウ ID リセット (次回再取得)
        _find_scrcpy_window_id()
        _LAST_SCRCPY_BGR = None
        return None
    # 真っ黒チェック: Quartz が映像を取得できていない場合 (別デスクトップ等)
    if float(bgr.mean()) < 0.5:
        logger.warning("[SCRCPY] キャプチャが真っ黒 (mean=%.1f) → ADB フォールバック", float(bgr.mean()))
        _LAST_SCRCPY_BGR = None
        return None
    # 最低サイズチェック: ウィンドウリサイズで解像度不足 → scrcpy 再起動
    _MIN_CAPTURE_W = 720  # 最低キャプチャ幅 (ANALYSIS_W の 50%)
    _h, _w = bgr.shape[:2]
    if _w < _MIN_CAPTURE_W:
        logger.warning("[SCRCPY] キャプチャ解像度不足 (%dx%d < min %d) → scrcpy 再起動",
                       _w, _h, _MIN_CAPTURE_W)
        _LAST_SCRCPY_BGR = None
        manage_scrcpy()
        return None
    _LAST_SCRCPY_BGR = bgr  # cv2.imread 不要にするキャッシュ
    # 実機解像度を返す (adb input tap はデバイス物理座標を使用)
    dev_w, dev_h = _get_cached_device_resolution()
    if dev_w <= 0 or dev_h <= 0:
        dev_w, dev_h = _w, _h
    return path, dev_w, dev_h


def pop_last_scrcpy_bgr() -> Optional[np.ndarray]:
    """scrcpy キャプチャ済み BGR を取得してキャッシュをクリア。

    take_screenshot 後に1度だけ呼び、cv2.imread の二重読みを回避する。
    ADB フォールバック時は None を返す。
    """
    global _LAST_SCRCPY_BGR
    bgr = _LAST_SCRCPY_BGR
    _LAST_SCRCPY_BGR = None
    return bgr


def _take_screenshot_adb(path: Path, retries: int = 3,
                         min_bytes: int = 5_000) -> tuple[Optional[Path], int, int, int]:
    """adb screencap (~1.5-2s)。Wi-Fi 破損リトライ付き。

    Returns: (path, width, height, retry_count) — adb は実機解像度そのまま
    """
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
            logger.warning("[SCREENSHOT] 取得例外: %s (attempt %d/%d)",
                           _ss_exc, _attempt + 1, retries)
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
    logger.error("[WIFI_ERROR] Corrupted frame dropped (%d retries exhausted). Returning None.",
                 retries)
    return None, 0, 0, _retried


def take_screenshot(retries: int = 3, min_bytes: int = 5_000) -> tuple[Optional[Path], int, int, int]:
    """スクリーンショット取得 (2段構え)。

    1. scrcpy ウィンドウキャプチャ (Quartz, ~100ms)
    2. adb screencap フォールバック (~1.5-2s)

    scrcpy キャプチャが連続失敗した場合、自動的に scrcpy を再起動する。

    Returns: (path, device_w, device_h, retry_count)
    ※ device_w/h は常に実機の物理解像度 (wm size) を返す
    """
    global _SCRCPY_FAIL_COUNT, _SCRCPY_LAST_RESTART
    path = Path(SCREENSHOT_PATH)

    # ── Tier 1: scrcpy ウィンドウキャプチャ ──
    _scrcpy = _take_screenshot_scrcpy(path)
    if _scrcpy is not None:
        _SCRCPY_FAIL_COUNT = 0
        return _scrcpy[0], _scrcpy[1], _scrcpy[2], 0

    # scrcpy 失敗カウント: 連続失敗で自動復帰
    # クールダウン中 (再起動後30秒) はカウントせず adb フォールバックに任せる
    _now = time.time()
    if _now - _SCRCPY_LAST_RESTART < _SCRCPY_RESTART_COOLDOWN:
        pass  # クールダウン中 — scrcpy 起動待ち、adb で凌ぐ
    else:
        _SCRCPY_FAIL_COUNT += 1
        if _SCRCPY_FAIL_COUNT >= _SCRCPY_FAIL_RESTART_THRESHOLD:
            logger.warning("[SCRCPY] %d 回連続キャプチャ失敗 → scrcpy 自動再起動",
                           _SCRCPY_FAIL_COUNT)
            manage_scrcpy()
            _SCRCPY_FAIL_COUNT = 0
            _SCRCPY_LAST_RESTART = _now

    # ── Tier 2: adb screencap フォールバック ──
    return _take_screenshot_adb(path, retries=retries, min_bytes=min_bytes)


def manage_scrcpy() -> Optional[subprocess.Popen]:
    """scrcpy を規定オプションで起動。不整合プロセスは Kill → 再起動。"""
    try:
        ps = subprocess.run(
            ["/bin/ps", "aux"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        logger.warning("[SCRCPY] ps aux 失敗: %s", e)
        return None

    # 期待する --max-size を事前計算 (ループ外で1回だけ)
    _expected_args = _build_scrcpy_args(SCRCPY_DEVICE)
    _expected_ms = _expected_args[_expected_args.index("--max-size") + 1]

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
        has_correct_size = f"--max-size {_expected_ms}" in line
        if has_device and has_correct_size:
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
        # プロセスが本当に生存しているか確認
        try:
            os.kill(conforming_pid, 0)  # シグナル0 = 生存確認のみ
            return None
        except OSError:
            logger.warning("[SCRCPY] PID=%d は既に終了 — 再起動します", conforming_pid)
            # fall through to launch new process

    try:
        proc = subprocess.Popen(
            _expected_args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[SCRCPY] 規定オプションで起動 PID=%d (device=%s)",
                    proc.pid, SCRCPY_DEVICE)
        # ウィンドウ ID キャッシュをリセット (次回キャプチャ時に再取得)
        global _SCRCPY_WINDOW_ID
        _SCRCPY_WINDOW_ID = 0
        return proc
    except FileNotFoundError:
        logger.warning("[SCRCPY] scrcpy が見つかりません — Stay Awake なしで続行 "
                       "(brew install scrcpy で導入可能)")
    except Exception as e:
        logger.warning("[SCRCPY] 起動失敗: %s — Stay Awake なしで続行", e)
    return None


def _to_device(x: int, y: int, state=None) -> tuple[int, int]:
    """解析座標 (ANALYSIS_W×ANALYSIS_H) → デバイス実座標。

    キャッシュ済みデバイス解像度を優先使用。state.device_w/h はフォールバック。
    scrcpy/adb どちらの取得方法でも同じ変換結果を保証する。
    """
    # 優先: モジュールキャッシュ (wm size ベース、ランドスケープ正規化済み)
    dev_w, dev_h = _CACHED_DEVICE_RES
    # フォールバック: state 経由
    if dev_w <= 0 and state and state.device_w and state.device_h:
        dev_w, dev_h = state.device_w, state.device_h
    if dev_w > 0 and dev_h > 0:
        return (int(x * dev_w / ANALYSIS_W),
                int(y * dev_h / ANALYSIS_H))
    return x, y


def tap_device(x: int, y: int, state, desc: str = "",
               finger_box=None, gold_box=None,
               post_wait: float = 0.0,
               rapid: bool = False) -> None:
    # ── 最低タップ間隔の強制 (rapid=True でスキップ: ダブルタップ等) ──
    if not rapid and state.last_action_time > 0:
        _elapsed = time.time() - state.last_action_time
        if _elapsed < MIN_TAP_INTERVAL:
            _wait = MIN_TAP_INTERVAL - _elapsed
            time.sleep(_wait)
    real_x, real_y = _to_device(x, y, state)
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
    rx1, ry1 = _to_device(x1, y1, state)
    rx2, ry2 = _to_device(x2, y2, state)
    adb(f"shell input swipe {rx1} {ry1} {rx2} {ry2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", rx1, ry1, rx2, ry2, duration_ms)


def swipe_device(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
                 state=None, desc: str = "") -> None:
    """解析座標 → デバイス実座標に変換してスワイプ (tap_device のスワイプ版)。"""
    rx1, ry1 = _to_device(x1, y1, state)
    rx2, ry2 = _to_device(x2, y2, state)
    logger.info("  SWIPE_DEVICE (%d,%d)->(%d,%d) %dms | %s", rx1, ry1, rx2, ry2, duration_ms, desc)
    adb(f"shell input swipe {rx1} {ry1} {rx2} {ry2} {duration_ms}")


def check_adb_liveness() -> bool:
    """
    ADB 接続の物理的な生存を確認する。
    echo コマンドのみで軽量チェック (screencap は USB で 3s 超えることがあり除外)。
    Returns: True=接続正常, False=タイムアウト/エラー(要再起動)
    """
    _serial_arg = ["-s", DEVICE_SERIAL] if DEVICE_SERIAL else []
    try:
        _r1 = subprocess.run(
            ["adb"] + _serial_arg + ["shell", "echo", "1"],
            capture_output=True, timeout=5, text=True,
        )
        if _r1.returncode != 0 or _r1.stdout.strip() != "1":
            logger.warning("[WATCHDOG] echo 応答異常: rc=%d out=%r", _r1.returncode, _r1.stdout.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.warning("[WATCHDOG] ADB コマンドタイムアウト — 物理診断失敗")
        return False
    except Exception as _e:
        logger.warning("[WATCHDOG] 物理診断例外: %s", _e)
        return False


def check_foreground_app() -> bool:
    """
    ゲームアプリがフォアグラウンドかチェック。
    明確に別アプリ (Chrome 等) が前面にいる場合のみ am start で復帰し True を返す。
    ゲームが前面 or 判定不能 (null/ロード中) なら False を返す (復帰不要)。
    """
    _serial_arg = ["-s", DEVICE_SERIAL] if DEVICE_SERIAL else []
    try:
        # ---- 1) ポートレート判定: Override size のみチェック ----
        # Physical size はデバイス固定 (常にポートレート) のため誤判定する。
        # Override size が設定されていてポートレートなら非ゲーム状態。
        try:
            _sz = subprocess.run(
                ["adb"] + _serial_arg + ["shell", "wm", "size"],
                capture_output=True, timeout=3, text=True,
            )
            for _sl in _sz.stdout.splitlines():
                if "Override" in _sl:
                    _parts = _sl.split()[-1].split("x")
                    if len(_parts) == 2:
                        _sw, _sh = int(_parts[0]), int(_parts[1])
                        if _sw < _sh:  # portrait override = ゲームではない
                            logger.warning(
                                "[FOREGROUND] ポートレート検出 (%dx%d) → ゲーム非前面、am start で復帰",
                                _sw, _sh)
                            subprocess.run(
                                ["adb"] + _serial_arg + ["shell", "am", "start", "-n",
                                 f"{APP_PACKAGE}/{APP_ACTIVITY}"],
                                capture_output=True, timeout=5,
                            )
                            time.sleep(1)
                            return True
                    break
        except Exception:
            pass  # wm size 失敗は無視して従来ロジックへ

        # ---- 2) mCurrentFocus / mFocusedApp 判定 ----
        # 端末によって `dumpsys window displays` に mCurrentFocus が含まれない
        # (例: Xperia)。まず軽量な `displays` を試し、取れなければフルダンプにフォールバック。
        _focus_lines = []
        for _subcmd in (["dumpsys", "window", "displays"], ["dumpsys", "window"]):
            _r = subprocess.run(
                ["adb"] + _serial_arg + ["shell"] + _subcmd,
                capture_output=True, timeout=5, text=True,
            )
            if _r.returncode != 0:
                continue
            for line in _r.stdout.splitlines():
                if "mCurrentFocus" in line or "mFocusedApp" in line:
                    _focus_lines.append(line.strip())
                    if APP_PACKAGE in line:
                        return False  # ゲームが前面 → 復帰不要
            if _focus_lines:
                break  # focus 情報が取れたらフォールバック不要
        if not _focus_lines and _r.returncode != 0:
            return False
        # focus 情報が取れない or null → ロード中の可能性 → 復帰しない
        if not _focus_lines:
            return False
        # mCurrentFocus=null はゲームのロード/遷移中で発生する → 復帰しない
        _all_null = all("null" in l for l in _focus_lines)
        if _all_null:
            logger.debug("[FOREGROUND] mCurrentFocus=null → ロード中と推定、復帰スキップ")
            return False
        # 明確に別パッケージが前面 → 復帰
        # ただしランチャーは除外 (ゲーム起動直後に一瞬ランチャーが見えることがある)
        _launcher_kws = ("launcher", "Launcher", "com.android.systemui")
        if all(any(lk in l for lk in _launcher_kws) for l in _focus_lines):
            logger.debug("[FOREGROUND] ランチャー/SystemUI → ゲーム起動中と推定、復帰スキップ")
            return False
        logger.warning("[FOREGROUND] 別アプリが前面: %s → am start で復帰",
                       _focus_lines[0][:80] if _focus_lines else "?")
        subprocess.run(
            ["adb"] + _serial_arg + ["shell", "am", "start", "-n",
             f"{APP_PACKAGE}/{APP_ACTIVITY}"],
            capture_output=True, timeout=5,
        )
        time.sleep(1)
        return True  # 復帰実行した
    except subprocess.TimeoutExpired:
        logger.warning("[FOREGROUND] dumpsys タイムアウト")
        return False
    except Exception as _e:
        logger.warning("[FOREGROUND] チェック例外: %s", _e)
        return False
