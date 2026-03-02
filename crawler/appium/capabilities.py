"""
capabilities.py — Appium Capabilities 設定モジュール

iOS / Android それぞれの Capabilities をビルドするファクトリ関数を提供する。
UDID・Bundle ID等の実機依存値は環境変数または引数で注入する。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


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
    環境変数から iOSDeviceConfig を生成する。

    必須環境変数:
        IOS_UDID       : デバイス UDID
        IOS_BUNDLE_ID  : ターゲットアプリの Bundle ID

    任意環境変数:
        IOS_DEVICE_NAME     : デバイス名 (デフォルト: "iPhone")
        IOS_PLATFORM_VERSION: iOS バージョン
        APPIUM_HOST         : Appium サーバーホスト (デフォルト: 127.0.0.1)
        APPIUM_PORT         : Appium サーバーポート (デフォルト: 4723)
    """
    udid = os.environ.get("IOS_UDID", "")
    bundle_id = os.environ.get("IOS_BUNDLE_ID", "")

    if not udid:
        raise ValueError(
            "環境変数 IOS_UDID が設定されていません。\n"
            "例: export IOS_UDID='00008120-000A1234ABCD1234'"
        )
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
