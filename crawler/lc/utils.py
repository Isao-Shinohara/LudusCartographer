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


# ─── ADB パス自動解決 ───────────────────────────────────────
def _ensure_adb_in_path() -> None:
    """ANDROID_HOME / ANDROID_SDK_ROOT から platform-tools を PATH に追加する。"""
    for env_key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(env_key, "").strip()
        if not sdk:
            continue
        pt = os.path.join(sdk, "platform-tools")
        if os.path.isdir(pt) and pt not in os.environ.get("PATH", ""):
            os.environ["PATH"] = pt + os.pathsep + os.environ.get("PATH", "")
            logger.debug("[ADB_PATH] %s を PATH に追加", pt)
            return
    # macOS デフォルトパス
    default = os.path.expanduser("~/Library/Android/sdk/platform-tools")
    if os.path.isdir(default) and default not in os.environ.get("PATH", ""):
        os.environ["PATH"] = default + os.pathsep + os.environ.get("PATH", "")
        logger.debug("[ADB_PATH] デフォルト %s を PATH に追加", default)


_ensure_adb_in_path()


# ─── ADB 自動接続 ──────────────────────────────────────────
WIFI_DEVICE_ADDR = os.environ.get("ANDROID_WIFI_ADDR", "").strip()


def _find_usb_device(timeout: int = 5) -> Optional[str]:
    """adb devices から USB デバイス (ポート番号なし = 非Wi-Fi) を返す。"""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=timeout,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1].strip() == "device":
                serial = parts[0].strip()
                # Wi-Fi デバイスは "IP:PORT" 形式 → ":" を含まないものが USB
                if serial and ":" not in serial:
                    return serial
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _find_wifi_device(addr: str, timeout: int = 5) -> Optional[str]:
    """adb devices から指定 Wi-Fi アドレスに一致するオンラインデバイスを返す。"""
    try:
        result = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=timeout,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split("\t")
            if len(parts) == 2 and parts[1].strip() == "device":
                serial = parts[0].strip()
                if serial == addr:
                    return serial
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def _switch_to_tcpip(usb_serial: str, port: int = 5555, timeout: int = 10) -> bool:
    """USB デバイスを adb tcpip モードに切り替える。成功で True。"""
    try:
        r = subprocess.run(
            ["adb", "-s", usb_serial, "tcpip", str(port)],
            capture_output=True, text=True, timeout=timeout,
        )
        ok = "restarting" in r.stdout.lower() or r.returncode == 0
        if ok:
            logger.info("[ADB_TCPIP] USB デバイス %s → tcpip %d に切り替え成功", usb_serial, port)
        else:
            logger.warning("[ADB_TCPIP] tcpip 切り替え失敗: %s %s", r.stdout.strip(), r.stderr.strip())
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("[ADB_TCPIP] tcpip 切り替え失敗: %s", e)
        return False


def _adb_connect(addr: str, timeout: int = 10) -> bool:
    """adb connect を実行。成功で True。"""
    try:
        r = subprocess.run(
            ["adb", "connect", addr],
            capture_output=True, text=True, timeout=timeout,
        )
        out = r.stdout.lower()
        ok = "connected" in out and "cannot" not in out
        if ok:
            logger.info("[ADB_CONNECT] Wi-Fi 接続成功: %s", addr)
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("[ADB_CONNECT] adb connect 失敗: %s", e)
        return False


def _adb_pair(host: str, port: int, code: str, timeout: int = 15) -> bool:
    """adb pair を実行 (Android 11+)。成功で True。"""
    addr = f"{host}:{port}"
    try:
        r = subprocess.run(
            ["adb", "pair", addr, code],
            capture_output=True, text=True, timeout=timeout,
        )
        ok = "successfully" in r.stdout.lower() or r.returncode == 0
        if ok:
            logger.info("[ADB_PAIR] ペアリング成功: %s", addr)
        else:
            logger.warning("[ADB_PAIR] ペアリング失敗: %s %s (コード期限切れ?)", r.stdout.strip(), r.stderr.strip())
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("[ADB_PAIR] adb pair 失敗: %s", e)
        return False


def ensure_adb_connection(
    wifi_addr: str = WIFI_DEVICE_ADDR,
    pairing_code: Optional[str] = None,
    pairing_port: Optional[int] = None,
) -> str:
    """
    ADB デバイス接続を自動検出・確立する。

    検出順:
      1. adb devices で Wi-Fi デバイスが見つかった → return
      2a. USB デバイスあり → adb tcpip → adb connect wifi_addr
      2b. デバイスなし → adb connect 直接試行 (既に tcpip モードの可能性)
      2c. 失敗 + pairing_code あり → adb pair → adb connect
      3. すべて失敗 → RuntimeError

    Args:
        wifi_addr: Wi-Fi 接続先アドレス (IP:PORT)
        pairing_code: adb pair 用コード (Android 11+, Optional)
        pairing_port: adb pair 用ポート (Android 11+, Optional)

    Returns: デバイスシリアル文字列
    """
    import time as _time  # sleep 用 (モジュール上位の time と衝突回避)

    # wifi_addr が空の場合、Wi-Fi 固定接続をスキップし USB/既存デバイス自動検出にフォールバック
    if not wifi_addr:
        logger.info("[ADB_CONNECT] Wi-Fi アドレス未設定 → USB/既存デバイス自動検出モード")
        any_serial = _try_adb(timeout=5)
        if any_serial:
            logger.info("[ADB_CONNECT] デバイス検出: %s", any_serial)
            return any_serial
        raise RuntimeError(
            "ADB デバイスが見つかりません。\n"
            "  [USB] USBデバッグを有効にしてケーブル接続してください\n"
            "  [Wi-Fi] ANDROID_WIFI_ADDR 環境変数または --wifi-addr を指定してください"
        )

    # Step 1: 既にオンラインの Wi-Fi デバイスがあるか
    wifi_serial = _find_wifi_device(wifi_addr)
    if wifi_serial:
        logger.info("[ADB_CONNECT] Wi-Fi デバイス検出済み: %s", wifi_serial)
        return wifi_serial

    # USB デバイスも含めて任意のオンラインデバイスを確認
    any_serial = _try_adb(timeout=5)
    if any_serial and ":" not in any_serial:
        # Step 2a: USB デバイスあり → tcpip 切り替え → Wi-Fi 接続
        logger.info("[ADB_CONNECT] USB デバイス検出: %s → Wi-Fi 切り替えを試行", any_serial)
        if _switch_to_tcpip(any_serial):
            _time.sleep(2)
            if _adb_connect(wifi_addr):
                # 接続確認
                _time.sleep(1)
                confirmed = _find_wifi_device(wifi_addr)
                if confirmed:
                    logger.info("[ADB_CONNECT] USB→Wi-Fi 切り替え完了: %s (USBケーブルは抜いてOK)", wifi_addr)
                    return wifi_addr
        # tcpip 切り替え失敗でも USB デバイスは使える
        logger.info("[ADB_CONNECT] Wi-Fi 切り替え失敗 — USB デバイスをそのまま使用: %s", any_serial)
        return any_serial
    elif any_serial:
        # 何らかの Wi-Fi デバイスが接続中 (アドレス不一致だが使えるデバイスあり)
        return any_serial

    # Step 2b: デバイスなし → Wi-Fi 直接接続試行 (既に tcpip モードの可能性)
    logger.info("[ADB_CONNECT] デバイス未検出 → Wi-Fi 直接接続を試行: %s", wifi_addr)
    if _adb_connect(wifi_addr):
        return wifi_addr

    # Step 2c: adb pair (Android 11+)
    if pairing_code and pairing_port:
        # wifi_addr から host 部分を抽出
        host = wifi_addr.split(":")[0]
        logger.info("[ADB_CONNECT] adb pair を試行: %s:%d", host, pairing_port)
        if _adb_pair(host, pairing_port, pairing_code):
            _time.sleep(1)
            if _adb_connect(wifi_addr):
                return wifi_addr
        raise RuntimeError(
            f"adb pair 失敗。ペアリングコード ({pairing_code}) が期限切れの可能性があります。\n"
            f"デバイスの「ワイヤレスデバッグ」→「ペアリングコードによるデバイスのペアリング」で新しいコードを取得してください。"
        )

    raise RuntimeError(
        f"ADB デバイスが見つかりません。\n"
        f"  [USB] USBデバッグを有効にしてケーブル接続してください\n"
        f"  [Wi-Fi] adb connect {wifi_addr} または --pairing-code/--pairing-port を指定してください"
    )


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
# アプリ管理 (インストール / アンインストール / Play Store)
# ============================================================

def uninstall_app(serial: str, package: str, timeout: int = 30) -> bool:
    """adb shell pm uninstall でアプリを削除する。成功 or 未インストールで True。"""
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "pm", "uninstall", package],
            capture_output=True, text=True, timeout=timeout,
        )
        out = r.stdout.strip().lower()
        if "success" in out:
            logger.info("[UNINSTALL] %s を削除しました", package)
            return True
        # "Unknown package" = 未インストール → 成功扱い
        logger.info("[UNINSTALL] %s は未インストール (出力: %s)", package, r.stdout.strip())
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("[UNINSTALL] 失敗: %s", e)
        return False


def is_app_installed(serial: str, package: str, timeout: int = 10) -> bool:
    """adb shell pm list packages でインストール済みか確認する。"""
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "pm", "list", "packages", package],
            capture_output=True, text=True, timeout=timeout,
        )
        return f"package:{package}" in r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def open_play_store(serial: str, package: str, timeout: int = 10) -> bool:
    """Play Store のアプリ詳細ページを開く (market:// intent)。"""
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "am", "start", "-a",
             "android.intent.action.VIEW", "-d", f"market://details?id={package}"],
            capture_output=True, text=True, timeout=timeout,
        )
        ok = r.returncode == 0
        if ok:
            logger.info("[PLAY_STORE] %s の詳細ページを開きました", package)
        else:
            logger.warning("[PLAY_STORE] 起動失敗: %s", r.stderr.strip())
        return ok
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.error("[PLAY_STORE] 起動失敗: %s", e)
        return False


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
