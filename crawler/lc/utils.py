"""
utils.py — デバイス接続ユーティリティ

get_device_udid() は以下の順序で iOS デバイスの UDID を取得する:

  1. 環境変数  : IOS_UDID が設定されていれば最優先
  2. idevice_id: libimobiledevice の標準ツール（ペアリング済みが必要）
  3. ioreg     : IORegistry から USB Serial Number を読み取り UDID に変換
                 （「このコンピュータを信頼しますか？」未承認でも取得可能）

【idevice_id が空を返す主な原因と対処】

  症状 : ioreg では iPhone@... が見えるが idevice_id -l が空
  原因 : iPhone側で「このコンピュータを信頼しますか？」がまだ承認されていない
  対処 : iPhone の画面に表示される「信頼」ダイアログをタップしてください。
         承認後は idevice_id が UDID を返すようになります。
         ioreg ルートで取得した UDID は Appium接続に使用可能ですが、
         WDA (WebDriverAgent) のインストールにはペアリングが必要です。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

# ioreg で取得できる USB Serial Number のパターン
# Apple デバイスのシリアルは 24 文字の16進数
_IOREG_SERIAL_PATTERN = re.compile(
    r'"USB Serial Number"\s*=\s*"([0-9A-Fa-f]{24})"'
)
# idevice_id が返す UDID のパターン（36文字 UUID または 24文字シリアル）
_UDID_PATTERN = re.compile(
    r'^([0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{24,40})$'
)


def get_device_udid(timeout: int = 5) -> str:
    """
    接続中の iOS デバイスの UDID を自動取得する。

    取得の優先順位:
      1. 環境変数 IOS_UDID
      2. idevice_id -l
      3. ioreg -p IOUSB (USB Serial Number → UDID 形式に変換)

    Args:
        timeout: 外部コマンドのタイムアウト秒数

    Returns:
        UDID 文字列

    Raises:
        RuntimeError: いずれの方法でも UDID が取得できなかった場合
    """
    # ----------------------------------------------------------
    # 方法1: 環境変数 (最優先)
    # ----------------------------------------------------------
    env_udid = os.environ.get("IOS_UDID", "").strip()
    if env_udid:
        logger.info(f"[UDID] 環境変数 IOS_UDID から取得: {env_udid}")
        return env_udid

    # ----------------------------------------------------------
    # 方法2: idevice_id -l
    # ----------------------------------------------------------
    udid = _try_idevice_id(timeout)
    if udid:
        logger.info(f"[UDID] idevice_id から取得: {udid}")
        return udid
    logger.debug("[UDID] idevice_id は空を返した（未ペアリングの可能性）")

    # ----------------------------------------------------------
    # 方法3: ioreg (最終手段)
    # ----------------------------------------------------------
    udid = _try_ioreg(timeout)
    if udid:
        logger.warning(
            f"[UDID] ioreg から取得: {udid}\n"
            "       ⚠️  idevice_id が空です。iPhone 側で「信頼」をタップしましたか?\n"
            "       ioreg UDID で Appium 接続を試みますが、\n"
            "       WDA インストールにはペアリングが必要です。"
        )
        return udid

    # ----------------------------------------------------------
    # 取得失敗
    # ----------------------------------------------------------
    raise RuntimeError(
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  iOS デバイスが見つかりませんでした。\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  確認事項:\n"
        "  1. USB ケーブルで iPhone を接続していますか?\n"
        "  2. iPhone 画面に「このコンピュータを信頼しますか?」\n"
        "     が表示されている場合は「信頼」をタップしてください。\n"
        "  3. 手動で UDID を設定することもできます:\n"
        "       export IOS_UDID='あなたのUDID'\n"
        "     (UDID は Xcode > Devices or idevice_id -l で確認)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )


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
        logger.debug(f"[UDID] idevice_id 実行失敗: {e}")
    return None


def _try_ioreg(timeout: int) -> Optional[str]:
    """
    ioreg -p IOUSB から USB Serial Number を取得し UDID 形式に変換する。

    Apple の USB Serial Number は 24 文字の16進数。
    これをそのまま UDID として使用する（Appium は受け付ける）。

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
            serial = m.group(1).upper()
            logger.debug(f"[UDID] ioreg USB Serial: {serial}")
            return serial
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"[UDID] ioreg 実行失敗: {e}")
    return None


def diagnose_device_connection() -> dict:
    """
    デバイス接続状況を診断してレポートを返す。
    トラブルシューティング用。
    """
    report = {
        "env_udid":     os.environ.get("IOS_UDID", ""),
        "idevice_id":   None,
        "ioreg_serial": None,
        "usbmuxd_pid":  None,
        "trusted":      False,
    }

    # idevice_id
    report["idevice_id"] = _try_idevice_id(timeout=5)
    report["trusted"] = bool(report["idevice_id"])

    # ioreg
    report["ioreg_serial"] = _try_ioreg(timeout=5)

    # usbmuxd PID
    try:
        r = subprocess.run(["pgrep", "usbmuxd"], capture_output=True, text=True, timeout=3)
        pids = r.stdout.strip().splitlines()
        report["usbmuxd_pid"] = pids[0] if pids else None
    except Exception:
        pass

    return report
