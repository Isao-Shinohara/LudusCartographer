"""
driver_factory.py — 環境変数に基づいて適切な Driver セッションを生成する

環境変数:
  DEVICE_MODE        : "SIMULATOR" (デフォルト) / "MIRROR" / "ANDROID"
  IOS_USE_SIMULATOR  : "1" でシミュレータモード (DEVICE_MODE の代替)
  IOS_SIMULATOR_UDID : シミュレータ UDID (設定時は自動的に SIMULATOR モード)

  # MIRROR モード専用
  MIRROR_WINDOW_TITLE  : キャプチャ対象ウィンドウタイトル (部分一致)
                         例: "UxPlay" / "iPhone" / "scrcpy"
  MIRROR_DEVICE_WIDTH  : デバイス論理幅 pt (デフォルト 393 — iPhone 16)
  MIRROR_DEVICE_HEIGHT : デバイス論理高さ pt (デフォルト 852 — iPhone 16)

  # ANDROID モード専用
  ANDROID_UDID         : デバイスシリアル (adb devices で確認)
  ANDROID_APP_PACKAGE  : アプリパッケージ名 (例: com.aniplex.magia.exedra.jp)
  ANDROID_APP_ACTIVITY : アクティビティ名 (例: com.google.firebase.MessagingUnityPlayerActivity)
  ANDROID_DEVICE_NAME  : デバイス名 (デフォルト: Android)
  ANDROID_PLATFORM_VERSION: Android バージョン (例: 10)

  APPIUM_HOST          : Appium ホスト (デフォルト 127.0.0.1)
  APPIUM_PORT          : Appium ポート (デフォルト 4723)

使い方:
  from driver_factory import create_driver_session
  from lc.crawler import ScreenCrawler, CrawlerConfig

  with create_driver_session() as driver:
      print(driver.is_simulator())
      crawler = ScreenCrawler(driver, CrawlerConfig())
      crawler.crawl()
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from driver_adapter import BaseDriver, MirroringDriver, SimulatorDriver


def _resolve_device_mode() -> str:
    """
    環境変数から動作モードを解決して返す。

    優先順位:
      1. DEVICE_MODE=MIRROR  → "MIRROR"
      2. DEVICE_MODE=ANDROID → "ANDROID"
      3. ANDROID_UDID が設定  → "ANDROID"
      4. IOS_USE_SIMULATOR=1 / IOS_SIMULATOR_UDID が設定 → "SIMULATOR"
      5. デフォルト → "SIMULATOR"
    """
    mode_env = os.environ.get("DEVICE_MODE", "").upper()
    if mode_env == "MIRROR":
        return "MIRROR"
    if mode_env == "ANDROID":
        return "ANDROID"
    if os.environ.get("ANDROID_UDID", "").strip():
        return "ANDROID"
    if (
        os.environ.get("IOS_USE_SIMULATOR", "").strip() in ("1", "true", "yes")
        or os.environ.get("IOS_SIMULATOR_UDID", "").strip()
    ):
        return "SIMULATOR"
    return "SIMULATOR"


@contextmanager
def create_driver_session(
    device_mode: str | None = None,
) -> Generator[BaseDriver, None, None]:
    """
    環境変数に基づいて適切な Driver セッションを生成し、
    コンテキストマネージャとして提供する。

    Args:
        device_mode: "SIMULATOR" または "MIRROR"。
                     None の場合は環境変数 DEVICE_MODE / IOS_USE_SIMULATOR から解決。

    Yields:
        BaseDriver のインスタンス (SimulatorDriver または MirroringDriver)

    例:
        with create_driver_session() as driver:
            print(driver.is_simulator())   # True (デフォルト)
            img = driver.get_screenshot()  # numpy.ndarray (BGR)

        with create_driver_session("MIRROR") as driver:
            print(driver.is_simulator())   # False
    """
    mode = (device_mode or _resolve_device_mode()).upper()

    if mode == "MIRROR":
        with _create_mirroring_session() as driver:
            yield driver
    elif mode == "ANDROID":
        with _create_android_session() as driver:
            yield driver
    else:
        with _create_simulator_session() as driver:
            yield driver


@contextmanager
def _create_simulator_session() -> Generator[SimulatorDriver, None, None]:
    """iOS シミュレータ用の AppiumDriver セッションを生成する。"""
    from lc.capabilities import simulator_config_from_env
    from lc.driver import ios_simulator_session

    sim_cfg = simulator_config_from_env()
    with ios_simulator_session(sim_cfg) as appium_driver:
        yield SimulatorDriver(appium_driver)


@contextmanager
def _create_android_session() -> Generator[SimulatorDriver, None, None]:
    """Android 実機用の AppiumDriver (UiAutomator2) セッションを生成する。"""
    from lc.capabilities import AndroidDeviceConfig, build_android_capabilities
    from lc.driver import android_session

    cfg = AndroidDeviceConfig(
        udid=os.environ.get("ANDROID_UDID", ""),
        app_package=os.environ.get("ANDROID_APP_PACKAGE", ""),
        app_activity=os.environ.get("ANDROID_APP_ACTIVITY", ".MainActivity"),
        device_name=os.environ.get("ANDROID_DEVICE_NAME", "Android"),
        platform_version=os.environ.get("ANDROID_PLATFORM_VERSION", ""),
        appium_host=os.environ.get("APPIUM_HOST", "127.0.0.1"),
        appium_port=int(os.environ.get("APPIUM_PORT", "4723")),
    )
    with android_session(cfg) as appium_driver:
        yield SimulatorDriver(appium_driver)


@contextmanager
def _create_mirroring_session() -> Generator[MirroringDriver, None, None]:
    """
    ミラーリング用セッションを生成する。

    Appium は Wi-Fi 経由で実機に接続する。
    画面キャプチャは UxPlay / scrcpy ウィンドウから取得する。

    注意: MIRROR モードでは Appium が Wi-Fi 経由で実機接続できる
          必要があります。APPIUM_HOST / APPIUM_PORT 環境変数を
          適切に設定してください。
    """
    from lc.capabilities import simulator_config_from_env
    from lc.driver import ios_simulator_session

    window_title  = os.environ.get("MIRROR_WINDOW_TITLE",  "")
    device_width  = int(os.environ.get("MIRROR_DEVICE_WIDTH",  "393"))
    device_height = int(os.environ.get("MIRROR_DEVICE_HEIGHT", "852"))

    # 既存の simulator_config_from_env() でベース設定を取得し、
    # Appium 接続先を環境変数で上書きする
    sim_cfg = simulator_config_from_env()
    sim_cfg.appium_host = os.environ.get("APPIUM_HOST", "127.0.0.1")
    sim_cfg.appium_port = int(os.environ.get("APPIUM_PORT", "4723"))

    with ios_simulator_session(sim_cfg) as appium_driver:
        yield MirroringDriver(
            appium_driver        = appium_driver,
            window_title         = window_title,
            device_logical_width = device_width,
            device_logical_height= device_height,
        )
