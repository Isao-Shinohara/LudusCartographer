"""
test_capabilities.py — Appium Capabilities ユニットテスト

実機なしで Capabilities の構造・必須キー・デフォルト値を検証する。
"""
import os
import pytest

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from appium.capabilities import (
    iOSDeviceConfig,
    AndroidDeviceConfig,
    build_ios_capabilities,
    build_android_capabilities,
    ios_config_from_env,
    android_config_from_env,
)


# ============================================================
# iOS Capabilities テスト
# ============================================================

class TestIOSCapabilities:

    def _make_cfg(self, **kwargs) -> iOSDeviceConfig:
        defaults = dict(udid="test-udid-001", bundle_id="com.example.game")
        return iOSDeviceConfig(**{**defaults, **kwargs})

    def test_platform_name_is_iOS(self):
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["platformName"] == "iOS"

    def test_automation_name_is_xcuitest(self):
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["appium:automationName"] == "XCUITest"

    def test_udid_is_set(self):
        caps = build_ios_capabilities(self._make_cfg(udid="ABCD-1234"))
        assert caps["appium:udid"] == "ABCD-1234"

    def test_bundle_id_is_set(self):
        caps = build_ios_capabilities(self._make_cfg(bundle_id="com.test.app"))
        assert caps["appium:bundleId"] == "com.test.app"

    def test_no_reset_default_true(self):
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["appium:noReset"] is True

    def test_new_command_timeout_is_positive(self):
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["appium:newCommandTimeout"] > 0

    def test_platform_version_omitted_when_empty(self):
        caps = build_ios_capabilities(self._make_cfg(platform_version=""))
        assert "appium:platformVersion" not in caps

    def test_platform_version_included_when_set(self):
        caps = build_ios_capabilities(self._make_cfg(platform_version="17.4"))
        assert caps["appium:platformVersion"] == "17.4"

    def test_wda_startup_retries_is_3(self):
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["appium:wdaStartupRetries"] == 3

    def test_wait_for_idle_disabled_for_games(self):
        """ゲーム向け: UIアイドル待ちを無効化していること"""
        caps = build_ios_capabilities(self._make_cfg())
        assert caps["appium:waitForIdleTimeout"] == 0


# ============================================================
# Android Capabilities テスト
# ============================================================

class TestAndroidCapabilities:

    def _make_cfg(self, **kwargs) -> AndroidDeviceConfig:
        defaults = dict(
            udid="emulator-5554",
            app_package="com.example.game",
            app_activity=".MainActivity",
        )
        return AndroidDeviceConfig(**{**defaults, **kwargs})

    def test_platform_name_is_android(self):
        caps = build_android_capabilities(self._make_cfg())
        assert caps["platformName"] == "Android"

    def test_automation_name_is_uiautomator2(self):
        caps = build_android_capabilities(self._make_cfg())
        assert caps["appium:automationName"] == "UiAutomator2"

    def test_app_package_and_activity(self):
        caps = build_android_capabilities(self._make_cfg())
        assert caps["appium:appPackage"] == "com.example.game"
        assert caps["appium:appActivity"] == ".MainActivity"

    def test_no_reset_default_true(self):
        caps = build_android_capabilities(self._make_cfg())
        assert caps["appium:noReset"] is True

    def test_platform_version_omitted_when_empty(self):
        caps = build_android_capabilities(self._make_cfg(platform_version=""))
        assert "appium:platformVersion" not in caps

    def test_platform_version_included_when_set(self):
        caps = build_android_capabilities(self._make_cfg(platform_version="13"))
        assert caps["appium:platformVersion"] == "13"


# ============================================================
# 環境変数からの設定構築テスト
# ============================================================

class TestConfigFromEnv:

    def test_ios_config_raises_without_udid(self, monkeypatch):
        """IOS_UDID 未設定かつ実機未接続のとき RuntimeError を送出すること。
        (get_device_udid() が自動検出を試み、全手段失敗で RuntimeError)"""
        from unittest.mock import MagicMock, patch
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("IOS_BUNDLE_ID", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError, match="見つかりませんでした"):
                ios_config_from_env()

    def test_ios_config_raises_without_bundle_id(self, monkeypatch):
        monkeypatch.setenv("IOS_UDID", "test-udid")
        monkeypatch.delenv("IOS_BUNDLE_ID", raising=False)
        with pytest.raises(ValueError, match="IOS_BUNDLE_ID"):
            ios_config_from_env()

    def test_ios_config_from_env(self, monkeypatch):
        monkeypatch.setenv("IOS_UDID", "00008120-TESTUDID")
        monkeypatch.setenv("IOS_BUNDLE_ID", "com.example.testapp")
        monkeypatch.setenv("IOS_DEVICE_NAME", "iPhone 15 Pro")
        monkeypatch.setenv("IOS_PLATFORM_VERSION", "17.4")

        cfg = ios_config_from_env()
        assert cfg.udid == "00008120-TESTUDID"
        assert cfg.bundle_id == "com.example.testapp"
        assert cfg.device_name == "iPhone 15 Pro"
        assert cfg.platform_version == "17.4"

    def test_android_config_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.delenv("ANDROID_APP_PACKAGE", raising=False)
        monkeypatch.delenv("ANDROID_APP_ACTIVITY", raising=False)

        cfg = android_config_from_env()
        # 未設定でも空文字で返ること（Android は UDID 任意）
        assert cfg.udid == ""
        assert cfg.app_activity == ".MainActivity"  # デフォルト値


# ============================================================
# Capabilities の W3C プロトコル準拠テスト
# ============================================================

class TestW3CProtocol:
    """すべての Appium 独自キーが 'appium:' プレフィックスを持つことを確認。"""

    def test_ios_appium_keys_have_prefix(self):
        cfg = iOSDeviceConfig(udid="u", bundle_id="b")
        caps = build_ios_capabilities(cfg)
        non_standard = {
            k for k in caps
            if k != "platformName" and not k.startswith("appium:")
        }
        assert non_standard == set(), (
            f"W3C非準拠キーが含まれています: {non_standard}"
        )

    def test_android_appium_keys_have_prefix(self):
        cfg = AndroidDeviceConfig(udid="u", app_package="p", app_activity="a")
        caps = build_android_capabilities(cfg)
        non_standard = {
            k for k in caps
            if k != "platformName" and not k.startswith("appium:")
        }
        assert non_standard == set(), (
            f"W3C非準拠キーが含まれています: {non_standard}"
        )
