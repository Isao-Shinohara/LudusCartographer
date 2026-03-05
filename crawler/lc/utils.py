"""
utils.py — デバイス接続ユーティリティ + 画像ハッシュ (phash)

detect_connected_device() は以下の順序で iOS / Android デバイスを自動検出する:

  1. 環境変数  : IOS_UDID → iOS,  ANDROID_SERIAL → Android (最優先)
  2. adb      : adb devices から最初のオンライン Android デバイスを取得
  3. idevice_id: libimobiledevice (iOS — ペアリング済みが必要)
  4. ioreg    : IORegistry から USB Serial Number を取得 → iOS デバイス
                （「このコンピュータを信頼しますか？」未承認でも取得可能）

get_device_udid() は後方互換 API として維持（iOS 専用）。

【iOS UDID フォーマット】
  ioreg から取得した 24 文字 HEX シリアル (例: 0000814000061C16222B001C) を
  Appium XCUITest が認識する XXXXXXXX-XXXXXXXXXXXXXXXX 形式に変換する。
  例: 0000814000061C16222B001C → 00008140-00061C16222B001C

【idevice_id が空を返す主な原因】
  iPhone 側で「このコンピュータを信頼しますか？」がまだ承認されていない。
  → iPhone の「信頼」をタップすると idevice_id が UDID を返すようになる。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ioreg で取得できる USB Serial Number のパターン (Apple: 24文字16進数)
_IOREG_SERIAL_PATTERN = re.compile(
    r'"USB Serial Number"\s*=\s*"([0-9A-Fa-f]{24})"'
)
# idevice_id / 環境変数で渡される UDID のパターン
# XXXXXXXX-XXXXXXXXXXXXXXXX (25文字) または 24-40 文字 HEX
_UDID_PATTERN = re.compile(
    r'^([0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{24,40})$'
)


# ============================================================
# UDID フォーマット変換
# ============================================================

def _format_ios_udid(raw: str) -> str:
    """
    24 文字の iOS USB Serial Number を Appium 形式に変換する。

    Appium XCUITest ドライバーは XXXXXXXX-XXXXXXXXXXXXXXXX 形式を要求する。
    既にダッシュが含まれている場合はそのまま大文字化して返す。
    """
    raw = raw.upper()
    if '-' in raw:
        return raw
    if len(raw) == 24:
        return f"{raw[:8]}-{raw[8:]}"
    return raw


# ============================================================
# メイン API
# ============================================================

def get_android_serial(timeout: int = 5) -> str:
    """
    接続中の Android デバイスのシリアルを自動取得する。

    検出の優先順位:
      1. 環境変数 ANDROID_UDID  (最優先)
      2. 環境変数 ANDROID_SERIAL
      3. adb devices から最初のオンラインデバイス (USB / Wi-Fi 両対応)

    Returns:
        デバイスシリアル文字列 (例: "f6b8cef7", "192.168.10.118:5555")

    Raises:
        RuntimeError: デバイスが見つからなかった場合
    """
    # 1. ANDROID_UDID (auto_pilot.py 等で使用)
    env_udid = os.environ.get("ANDROID_UDID", "").strip()
    if env_udid:
        logger.info(f"[DEVICE] Android serial (ANDROID_UDID): {env_udid}")
        return env_udid

    # 2. ANDROID_SERIAL
    env_serial = os.environ.get("ANDROID_SERIAL", "").strip()
    if env_serial:
        logger.info(f"[DEVICE] Android serial (ANDROID_SERIAL): {env_serial}")
        return env_serial

    # 3. adb devices 自動検出
    serial = _try_adb(timeout)
    if serial:
        logger.info(f"[DEVICE] Android detected via adb: {serial}")
        return serial

    raise RuntimeError(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  Android デバイスが見つかりませんでした。\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  [USB 接続]\n"
        "    1. USB デバッグを有効にしてデバイスを接続してください。\n"
        "    2. adb devices でデバイスが 'device' 状態か確認してください。\n"
        "  [Wi-Fi 接続]\n"
        "    1. adb tcpip 5555\n"
        "    2. adb connect <デバイスIP>:5555\n"
        "    3. adb devices で接続を確認してください。\n"
        "  [環境変数で手動設定]\n"
        "    export ANDROID_UDID='シリアルまたはIP:ポート'\n"
        "    export ANDROID_SERIAL='シリアルまたはIP:ポート'\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


def get_device_udid(timeout: int = 5) -> str:
    """
    接続中の iOS デバイスの UDID を自動取得する（後方互換 API）。

    iOS 専用。iOS/Android 両対応が必要な場合は detect_connected_device() を使用。

    Returns:
        XXXXXXXX-XXXXXXXXXXXXXXXX 形式の UDID 文字列

    Raises:
        RuntimeError: UDID が取得できなかった場合
    """
    udid, _ = detect_connected_device(timeout=timeout, ios_only=True)
    return udid


def detect_connected_device(
    timeout: int = 5,
    ios_only: bool = False,
) -> Tuple[str, str]:
    """
    接続中のデバイスを自動検出し (udid, platform) を返す。

    検出の優先順位:
      1. 環境変数 IOS_UDID          → ("UDID", "ios")
      2. 環境変数 ANDROID_SERIAL    → ("serial", "android")  ※ios_only=False 時
      3. adb devices               → ("serial", "android")   ※ios_only=False 時
      4. idevice_id -l             → ("UDID", "ios")
      5. ioreg -p IOUSB            → ("UDID", "ios")

    Returns:
        (udid, platform): platform は "ios" または "android"

    Raises:
        RuntimeError: デバイスが見つからなかった場合
    """
    # ----------------------------------------------------------
    # 1. iOS 環境変数 (最優先)
    # ----------------------------------------------------------
    env_ios = os.environ.get("IOS_UDID", "").strip()
    if env_ios:
        logger.info(f"[DEVICE] iOS UDID (環境変数): {env_ios}")
        return env_ios, "ios"

    # ----------------------------------------------------------
    # 2. Android 環境変数 + adb
    # ----------------------------------------------------------
    if not ios_only:
        env_android = os.environ.get("ANDROID_SERIAL", "").strip()
        if env_android:
            logger.info(f"[DEVICE] Android serial (環境変数): {env_android}")
            return env_android, "android"

        android_serial = _try_adb(timeout)
        if android_serial:
            logger.info(f"[DEVICE] Android detected via adb: {android_serial}")
            return android_serial, "android"

    # ----------------------------------------------------------
    # 3. idevice_id -l (iOS)
    # ----------------------------------------------------------
    udid = _try_idevice_id(timeout)
    if udid:
        logger.info(f"[DEVICE] iOS detected via idevice_id: {udid}")
        return udid, "ios"
    logger.debug("[DEVICE] idevice_id は空を返した（未ペアリングの可能性）")

    # ----------------------------------------------------------
    # 4. ioreg -p IOUSB (iOS 最終手段)
    # ----------------------------------------------------------
    udid = _try_ioreg(timeout)
    if udid:
        logger.warning(
            f"[DEVICE] iOS detected via ioreg: {udid}\n"
            "         ⚠️  idevice_id が空です。iPhone 側で「信頼」をタップしましたか?\n"
            "         ioreg UDID で Appium 接続を試みますが、\n"
            "         WDA インストールにはペアリングが必要です。"
        )
        return udid, "ios"

    # ----------------------------------------------------------
    # 取得失敗
    # ----------------------------------------------------------
    raise RuntimeError(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  デバイスが見つかりませんでした。\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  [iOS]\n"
        "    1. USB ケーブルで iPhone を接続していますか?\n"
        "    2. iPhone 画面に「このコンピュータを信頼しますか?」\n"
        "       が表示されている場合は「信頼」をタップしてください。\n"
        "    3. 手動設定: export IOS_UDID='あなたのUDID'\n"
        "       (UDID は Xcode > Devices or idevice_id -l で確認)\n"
        "  [Android]\n"
        "    1. USB デバッグを有効にしてデバイスを接続してください。\n"
        "    2. adb devices でデバイスが 'device' 状態か確認してください。\n"
        "    3. 手動設定: export ANDROID_SERIAL='あなたのシリアル'\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


# ============================================================
# プライベート: 各検出手段
# ============================================================

def _try_adb(timeout: int) -> Optional[str]:
    """
    adb devices から最初のオンライン Android デバイスのシリアルを返す。
    失敗時は None。
    """
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        for line in result.stdout.splitlines():
            # "SERIAL\tdevice" がオンライン状態 ("offline" は除外)
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1].strip() == "device":
                serial = parts[0].strip()
                if serial:
                    logger.debug(f"[DEVICE] adb serial: {serial}")
                    return serial
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"[DEVICE] adb 実行失敗: {e}")
    return None


def _try_idevice_id(timeout: int) -> Optional[str]:
    """idevice_id -l を実行して最初の UDID を返す。失敗時は None。"""
    try:
        result = subprocess.run(
            ["idevice_id", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
        for line in lines:
            if _UDID_PATTERN.match(line):
                return line
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"[DEVICE] idevice_id 実行失敗: {e}")
    return None


def _try_ioreg(timeout: int) -> Optional[str]:
    """
    ioreg -p IOUSB から USB Serial Number を取得し Appium 形式 UDID に変換する。

    Apple の USB Serial Number は 24 文字の16進数。
    _format_ios_udid() で XXXXXXXX-XXXXXXXXXXXXXXXX 形式に変換して返す。

    注意: ioreg -l の出力では "USB Serial Number" が "idVendor" より
    前に現れることがある。全テキストを一括検索することで順序に依存しない。
    """
    try:
        result = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-w", "0", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout
        # Apple VendorID (0x05ac = 1452) のデバイスが存在する場合のみ処理
        if '"idVendor" = 1452' not in output:
            return None
        m = _IOREG_SERIAL_PATTERN.search(output)
        if m:
            raw_serial = m.group(1).upper()
            udid = _format_ios_udid(raw_serial)
            logger.debug(f"[DEVICE] ioreg USB Serial → UDID: {udid}")
            return udid
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"[DEVICE] ioreg 実行失敗: {e}")
    return None


# ============================================================
# 診断
# ============================================================

def diagnose_device_connection() -> dict:
    """
    デバイス接続状況を診断してレポートを返す。
    iOS / Android 両方の接続状態を確認する。トラブルシューティング用。
    """
    report = {
        "env_udid":     os.environ.get("IOS_UDID", ""),
        "env_android":  os.environ.get("ANDROID_SERIAL", ""),
        "idevice_id":   None,
        "ioreg_serial": None,
        "adb_serial":   None,
        "usbmuxd_pid":  None,
        "trusted":      False,
        "platform":     None,
    }

    # iOS: idevice_id
    report["idevice_id"] = _try_idevice_id(timeout=5)
    report["trusted"] = bool(report["idevice_id"])

    # iOS: ioreg
    report["ioreg_serial"] = _try_ioreg(timeout=5)

    # Android: adb
    report["adb_serial"] = _try_adb(timeout=5)

    # usbmuxd PID (macOS)
    try:
        r = subprocess.run(["pgrep", "usbmuxd"], capture_output=True, text=True, timeout=3)
        pids = r.stdout.strip().splitlines()
        report["usbmuxd_pid"] = pids[0] if pids else None
    except Exception:
        pass

    # プラットフォーム推定
    if report["idevice_id"] or report["ioreg_serial"] or report["env_udid"]:
        report["platform"] = "ios"
    elif report["adb_serial"] or report["env_android"]:
        report["platform"] = "android"

    return report


# ============================================================
# 画像ハッシュ (phash)
# ============================================================

def compute_phash(image_path: "Path", hash_size: int = 8) -> str:
    """
    DCT phash (64-bit) を計算して 16 文字 hex 文字列で返す。

    opencv-contrib-python の cv2.dct() を使用。imagehash パッケージ不要。
    hash_size=8 → 8×8 DCT → 64 bit のハッシュ → 16 桁 hex。

    Args:
        image_path: 画像ファイルパス
        hash_size:  ハッシュサイズ (デフォルト 8 → 64bit)

    Returns:
        16 文字の hex 文字列 (例: "a3f0c2e1b4d59876")

    Raises:
        ValueError: 画像を読み込めない場合
        ImportError: cv2 / numpy が利用できない場合
    """
    import cv2
    import numpy as np

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"画像を読み込めません: {image_path}")
    img = cv2.resize(img, (hash_size * 4, hash_size * 4))
    dct = cv2.dct(np.float32(img))
    top = dct[:hash_size, :hash_size]
    avg = top.mean()
    bits = top.flatten() > avg
    return format(int("".join("1" if b else "0" for b in bits), 2), "016x")


def phash_distance(h1: str, h2: str) -> int:
    """
    2 つの phash 文字列のハミング距離を返す。

    距離 < 8 → ほぼ同一画面（重複とみなす閾値として使用）。

    Args:
        h1, h2: compute_phash() が返す 16 文字 hex 文字列

    Returns:
        ハミング距離 (0 〜 64)
    """
    a, b = int(h1, 16), int(h2, 16)
    return bin(a ^ b).count("1")
