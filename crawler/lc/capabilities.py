"""
capabilities.py — Appium Capabilities 設定モジュール

iOS / Android それぞれの Capabilities をビルドするファクトリ関数を提供する。
UDID・Bundle ID等の実機依存値は環境変数または引数で注入する。
"""
from __future__ import annotations

import os
import subprocess
import json
from dataclasses import dataclass, field
from typing import Any, Tuple, Union


# ============================================================
# データクラス: デバイス設定
# ============================================================

@dataclass
class iOSDeviceConfig:
    """iOS 実機向け設定"""
    udid:           str                        # e.g. "00008120-000A1234ABCD1234"
    bundle_id:      str                        # e.g. "com.example.mygame"
    device_name:    str   = "iPhone"           # e.g. "iPhone 15 Pro"
    platform_version: str = ""                 # e.g. "17.4" (空の場合は自動検出)
    appium_host:    str   = "127.0.0.1"
    appium_port:    int   = 4723
    wda_port:       int   = 8100              # WebDriverAgent のポート
    wda_local_port: int   = 8100
    no_reset:       bool  = True              # アプリ状態を保持してセッション開始


@dataclass
class iOSSimulatorConfig:
    """iOS シミュレータ向け設定"""
    udid:             str                      # xcrun simctl list devices から取得
    bundle_id:        str                      # e.g. "com.apple.Preferences"
    device_name:      str   = "iPhone 16"     # e.g. "iPhone 16"
    platform_version: str   = "18.5"          # e.g. "18.5"
    appium_host:      str   = "127.0.0.1"
    appium_port:      int   = 4723
    no_reset:         bool  = True


@dataclass
class AndroidDeviceConfig:
    """Android 実機向け設定"""
    udid:             str                      # e.g. "emulator-5554" or serial
    app_package:      str                      # e.g. "com.example.mygame"
    app_activity:     str                      # e.g. ".MainActivity"
    device_name:      str   = "Android"
    platform_version: str   = ""              # e.g. "13"
    appium_host:      str   = "127.0.0.1"
    appium_port:      int   = 4723
    no_reset:         bool  = True


# ============================================================
# Capabilities ビルダー
# ============================================================

def build_ios_capabilities(cfg: iOSDeviceConfig) -> dict[str, Any]:
    """
    iOS 実機用 Appium Capabilities を構築する。

    最小限の Capabilities のみ設定し、
    実機接続後に必要に応じて追加する。
    """
    caps: dict[str, Any] = {
        # --- Appium W3C プロトコル ---
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": cfg.udid,
        "appium:bundleId": cfg.bundle_id,
        "appium:deviceName": cfg.device_name,
        # --- 動作設定 ---
        "appium:noReset": cfg.no_reset,
        "appium:newCommandTimeout": 120,        # コマンドタイムアウト (秒)
        "appium:wdaLocalPort": cfg.wda_local_port,
        "appium:wdaConnectionTimeout": 60000,   # WDA接続タイムアウト (ms)
        # --- スクリーンショット ---
        "appium:screenshotQuality": 1,          # 0=最高, 2=最低
        # --- 描画待ち (ゲーム向け) ---
        "appium:waitForIdleTimeout": 0,         # UIの「アイドル待ち」を無効化
        "appium:animationCoolOffTimeout": 0,
        # --- WebDriverAgent ---
        "appium:useNewWDA": False,              # 既存WDAを再利用
        "appium:wdaLaunchTimeout": 120000,
        "appium:wdaStartupRetries": 3,
        "appium:wdaStartupRetryInterval": 1000,
    }

    if cfg.platform_version:
        caps["appium:platformVersion"] = cfg.platform_version

    return caps


def build_android_capabilities(cfg: AndroidDeviceConfig) -> dict[str, Any]:
    """
    Android 実機用 Appium Capabilities を構築する。
    """
    caps: dict[str, Any] = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:udid": cfg.udid,
        "appium:appPackage": cfg.app_package,
        "appium:appActivity": cfg.app_activity,
        "appium:deviceName": cfg.device_name,
        "appium:noReset": cfg.no_reset,
        "appium:newCommandTimeout": 120,
        # --- ゲーム向け設定 ---
        "appium:skipServerInstallation": False,
        "appium:uiautomator2ServerLaunchTimeout": 60000,
        "appium:adbExecTimeout": 30000,
        # --- スクリーンショット ---
        "appium:androidScreenshotPath": "/sdcard/",
    }

    if cfg.platform_version:
        caps["appium:platformVersion"] = cfg.platform_version

    return caps


# ============================================================
# 環境変数からデバイス設定を構築するファクトリ
# ============================================================

def ios_config_from_env() -> iOSDeviceConfig:
    """
    環境変数 + 自動デバイス検出から iOSDeviceConfig を生成する。

    UDID の取得順序 (get_device_udid() に委譲):
        1. 環境変数 IOS_UDID (最優先)
        2. idevice_id -l
        3. ioreg USB Serial Number (ペアリング前でも取得可能)

    必須環境変数:
        IOS_BUNDLE_ID  : ターゲットアプリの Bundle ID

    任意環境変数:
        IOS_UDID            : UDID (未設定時は自動検出)
        IOS_DEVICE_NAME     : デバイス名 (デフォルト: "iPhone")
        IOS_PLATFORM_VERSION: iOS バージョン
        APPIUM_HOST         : Appium サーバーホスト (デフォルト: 127.0.0.1)
        APPIUM_PORT         : Appium サーバーポート (デフォルト: 4723)
    """
    from .utils import get_device_udid

    # UDID: 自動検出（環境変数 → idevice_id → ioreg の順）
    udid = get_device_udid()

    bundle_id = os.environ.get("IOS_BUNDLE_ID", "")
    if not bundle_id:
        raise ValueError(
            "環境変数 IOS_BUNDLE_ID が設定されていません。\n"
            "例: export IOS_BUNDLE_ID='com.example.mygame'"
        )

    return iOSDeviceConfig(
        udid=udid,
        bundle_id=bundle_id,
        device_name=os.environ.get("IOS_DEVICE_NAME", "iPhone"),
        platform_version=os.environ.get("IOS_PLATFORM_VERSION", ""),
        appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
        appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
    )


def build_ios_simulator_capabilities(cfg: iOSSimulatorConfig) -> dict[str, Any]:
    """
    iOS シミュレータ用 Appium Capabilities を構築する。

    実機と異なり WDA ポート設定が不要で、isSimulator フラグを付与する。
    """
    return {
        "platformName": "iOS",
        "appium:automationName": "XCUITest",
        "appium:udid": cfg.udid,
        "appium:bundleId": cfg.bundle_id,
        "appium:deviceName": cfg.device_name,
        "appium:platformVersion": cfg.platform_version,
        "appium:isSimulator": True,
        "appium:noReset": cfg.no_reset,
        "appium:newCommandTimeout": 120,
        "appium:screenshotQuality": 1,
        "appium:waitForIdleTimeout": 0,
        "appium:animationCoolOffTimeout": 0,
        "appium:wdaLaunchTimeout": 120000,
        "appium:wdaStartupRetries": 3,
        "appium:wdaStartupRetryInterval": 1000,
    }


def _pick_simulator(prefer_name: str = "", prefer_version: str = "") -> Tuple[str, str, str]:
    """
    利用可能な iPhone シミュレータから最適なものを自動選択する。

    Returns:
        (udid, device_name, platform_version)

    Raises:
        RuntimeError: 利用可能なシミュレータが見つからない場合
    """
    result = subprocess.run(
        ["xcrun", "simctl", "list", "devices", "available", "--json"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    data = json.loads(result.stdout)

    candidates = []
    for runtime_id, devices in data["devices"].items():
        if "iOS" not in runtime_id or "iOS-" not in runtime_id:
            continue
        version = runtime_id.split("iOS-")[-1].replace("-", ".")
        for dev in devices:
            if "iPhone" in dev["name"]:
                candidates.append({
                    "udid": dev["udid"],
                    "name": dev["name"],
                    "version": version,
                })

    if not candidates:
        raise RuntimeError(
            "利用可能な iPhone シミュレータが見つかりません。\n"
            "Xcode > Window > Devices and Simulators からシミュレータを追加してください。"
        )

    if prefer_name:
        filtered = [c for c in candidates if prefer_name in c["name"]]
        if filtered:
            candidates = filtered

    if prefer_version:
        filtered = [c for c in candidates if c["version"].startswith(prefer_version)]
        if filtered:
            candidates = filtered

    def _sort_key(c: dict) -> Tuple:
        ver_parts = tuple(int(x) for x in c["version"].split(".") if x.isdigit())
        major = ver_parts[0] if ver_parts else 0
        # iOS 20+ はベータ扱い → stable (< 20) を優先
        is_stable = major < 20
        # iPhone 16 (Pro/Plus なし) を優先
        is_plain_16 = c["name"] == "iPhone 16"
        return (is_stable, ver_parts, is_plain_16)

    candidates.sort(key=_sort_key, reverse=True)
    best = candidates[0]
    return best["udid"], best["name"], best["version"]


def simulator_config_from_env() -> iOSSimulatorConfig:
    """
    環境変数から iOSSimulatorConfig を生成する。

    IOS_SIMULATOR_UDID が未設定の場合、xcrun simctl で最新の iPhone を自動選択する。

    必須環境変数:
        IOS_BUNDLE_ID       : ターゲットアプリの Bundle ID

    任意環境変数:
        IOS_SIMULATOR_UDID  : シミュレータ UDID (未設定時は自動選択)
        IOS_DEVICE_NAME     : デバイス名 (自動選択のフィルタに使用)
        IOS_PLATFORM_VERSION: iOS バージョン (自動選択のフィルタに使用)
        APPIUM_HOST         : Appium ホスト (デフォルト: 127.0.0.1)
        APPIUM_PORT         : Appium ポート (デフォルト: 4723)
    """
    bundle_id = os.environ.get("IOS_BUNDLE_ID", "")
    if not bundle_id:
        raise ValueError(
            "環境変数 IOS_BUNDLE_ID が設定されていません。\n"
            "例: export IOS_BUNDLE_ID='com.apple.Preferences'"
        )

    udid = os.environ.get("IOS_SIMULATOR_UDID", "").strip()
    device_name = os.environ.get("IOS_DEVICE_NAME", "")
    platform_version = os.environ.get("IOS_PLATFORM_VERSION", "")

    if udid:
        # UDID が明示されている場合はそのまま使用
        if not device_name:
            device_name = "iPhone 16"
        if not platform_version:
            platform_version = "18.5"
    else:
        udid, device_name, platform_version = _pick_simulator(device_name, platform_version)

    return iOSSimulatorConfig(
        udid=udid,
        bundle_id=bundle_id,
        device_name=device_name,
        platform_version=platform_version,
        appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
        appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
    )


def android_config_from_env() -> AndroidDeviceConfig:
    """
    環境変数から AndroidDeviceConfig を生成する。

    必須環境変数:
        ANDROID_UDID       : デバイスシリアル (adb devices で確認)
        ANDROID_APP_PACKAGE: アプリパッケージ名
        ANDROID_APP_ACTIVITY: アクティビティ名
    """
    return AndroidDeviceConfig(
        udid=os.environ.get("ANDROID_UDID", ""),
        app_package=os.environ.get("ANDROID_APP_PACKAGE", ""),
        app_activity=os.environ.get("ANDROID_APP_ACTIVITY", ".MainActivity"),
        device_name=os.environ.get("ANDROID_DEVICE_NAME", "Android"),
        platform_version=os.environ.get("ANDROID_PLATFORM_VERSION", ""),
        appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
        appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
    )


def auto_config_from_env() -> Tuple[Union[iOSDeviceConfig, AndroidDeviceConfig], str]:
    """
    接続中のデバイスを自動検出し、適切な DeviceConfig とプラットフォーム名を返す。

    iOS / Android を自動判別して設定を生成する統合エントリポイント。

    Returns:
        (config, platform): platform は "ios" または "android"

    Raises:
        RuntimeError: デバイスが見つからない場合
        ValueError: 必須環境変数 (IOS_BUNDLE_ID 等) が未設定の場合
    """
    from .utils import detect_connected_device

    udid, platform = detect_connected_device()

    if platform == "ios":
        bundle_id = os.environ.get("IOS_BUNDLE_ID", "")
        if not bundle_id:
            raise ValueError(
                "環境変数 IOS_BUNDLE_ID が設定されていません。\n"
                "例: export IOS_BUNDLE_ID='com.apple.Preferences'"
            )
        cfg: Union[iOSDeviceConfig, AndroidDeviceConfig] = iOSDeviceConfig(
            udid=udid,
            bundle_id=bundle_id,
            device_name=os.environ.get("IOS_DEVICE_NAME", "iPhone"),
            platform_version=os.environ.get("IOS_PLATFORM_VERSION", ""),
            appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
            appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
        )
        return cfg, "ios"

    # platform == "android"
    cfg = AndroidDeviceConfig(
        udid=udid,
        app_package=os.environ.get("ANDROID_APP_PACKAGE", ""),
        app_activity=os.environ.get("ANDROID_APP_ACTIVITY", ".MainActivity"),
        device_name=os.environ.get("ANDROID_DEVICE_NAME", "Android"),
        platform_version=os.environ.get("ANDROID_PLATFORM_VERSION", ""),
        appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
        appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
    )
    return cfg, "android"
