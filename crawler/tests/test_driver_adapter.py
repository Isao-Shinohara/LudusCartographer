"""
test_driver_adapter.py — Driver Adapter テストスイート

Appium 不要。すべてのテストはモックで動作する。

テスト対象:
  driver_adapter.py   — BaseDriver / SimulatorDriver / MirroringDriver
  tools/window_manager.py — find_mirroring_window / capture_region
  driver_factory.py   — _resolve_device_mode / create_driver_session
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest

# crawler/ をパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# BaseDriver — 抽象クラス検証
# ============================================================

class TestBaseDriver:
    def test_cannot_instantiate_directly(self):
        """BaseDriver は抽象クラスのため直接インスタンス化不可。"""
        from driver_adapter import BaseDriver
        with pytest.raises(TypeError):
            BaseDriver()  # type: ignore[abstract]

    def test_subclass_must_implement_all_methods(self):
        """必要メソッドを実装しないサブクラスはインスタンス化不可。"""
        from driver_adapter import BaseDriver

        class Incomplete(BaseDriver):
            def get_screenshot(self):
                return np.zeros((100, 100, 3), dtype=np.uint8)
            # tap() と is_simulator() が未実装

        with pytest.raises(TypeError):
            Incomplete()


# ============================================================
# SimulatorDriver
# ============================================================

class TestSimulatorDriver:
    """SimulatorDriver の各メソッドをモックでテスト。"""

    def _make(self) -> tuple:
        """SimulatorDriver と mock_appium のペアを返す。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        return SimulatorDriver(mock_appium), mock_appium

    # --- BaseDriver 抽象メソッド ---

    def test_is_simulator_returns_true(self):
        driver, _ = self._make()
        assert driver.is_simulator() is True

    def test_get_screenshot_returns_ndarray(self, tmp_path):
        """get_screenshot() は BGR numpy.ndarray を返す。"""
        import cv2
        from driver_adapter import SimulatorDriver

        # 疑似 PNG ファイルを作成
        fake_img = np.zeros((200, 100, 3), dtype=np.uint8)
        fake_img[:, :, 0] = 128  # B チャネルに値を設定
        img_path = tmp_path / "fake_shot.png"
        cv2.imwrite(str(img_path), fake_img)

        mock_appium = MagicMock()
        mock_appium.screenshot.return_value = img_path

        driver = SimulatorDriver(mock_appium)
        result = driver.get_screenshot()

        assert isinstance(result, np.ndarray)
        assert result.shape == (200, 100, 3)
        mock_appium.screenshot.assert_called_once_with("_adapter")

    def test_get_screenshot_raises_on_invalid_path(self):
        """存在しないパスを返した場合 RuntimeError を送出する。"""
        from driver_adapter import SimulatorDriver

        mock_appium = MagicMock()
        mock_appium.screenshot.return_value = Path("/nonexistent/shot.png")

        driver = SimulatorDriver(mock_appium)
        with pytest.raises(RuntimeError, match="読み込みに失敗"):
            driver.get_screenshot()

    def test_tap_delegates_to_tap_coordinate(self):
        """tap(x, y) は AppiumDriver.tap_coordinate(x, y) に委譲する。"""
        driver, mock_appium = self._make()
        driver.tap(150, 300)
        mock_appium.tap_coordinate.assert_called_once_with(150, 300)

    # --- __getattr__ 委譲 ---

    def test_getattr_delegates_screenshot_method(self):
        """AppiumDriver.screenshot() が透過的に呼べる。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium.screenshot.return_value = Path("/tmp/x.png")

        driver = SimulatorDriver(mock_appium)
        result = driver.screenshot("test_name")

        mock_appium.screenshot.assert_called_once_with("test_name")
        assert result == Path("/tmp/x.png")

    def test_getattr_delegates_back(self):
        """driver.back() は AppiumDriver.back() に委譲する。"""
        driver, mock_appium = self._make()
        driver.back()
        mock_appium.back.assert_called_once()

    def test_getattr_delegates_wait(self):
        """driver.wait(n) は AppiumDriver.wait(n) に委譲する。"""
        driver, mock_appium = self._make()
        driver.wait(2.5)
        mock_appium.wait.assert_called_once_with(2.5)

    def test_getattr_delegates_session_id(self):
        """driver.session_id は AppiumDriver.session_id を返す。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium.session_id = "20260304_120000"

        driver = SimulatorDriver(mock_appium)
        assert driver.session_id == "20260304_120000"

    def test_getattr_delegates_evidence_dir(self):
        """driver._evidence_dir は AppiumDriver._evidence_dir を返す。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium._evidence_dir = Path("/tmp/evidence")

        driver = SimulatorDriver(mock_appium)
        assert driver._evidence_dir == Path("/tmp/evidence")

    def test_getattr_delegates_driver_property(self):
        """driver.driver は AppiumDriver.driver (生 WebDriver) を返す。"""
        from driver_adapter import SimulatorDriver
        mock_raw_wd = MagicMock(name="RawWebDriver")
        mock_appium = MagicMock()
        mock_appium.driver = mock_raw_wd

        driver = SimulatorDriver(mock_appium)
        assert driver.driver is mock_raw_wd

    def test_getattr_delegates_tap_ocr_coordinate(self):
        """driver.tap_ocr_coordinate() は AppiumDriver に委譲する。"""
        driver, mock_appium = self._make()
        driver.tap_ocr_coordinate(600, 1200, "test_tap")
        mock_appium.tap_ocr_coordinate.assert_called_once_with(600, 1200, "test_tap")

    def test_getattr_delegates_wait_until_stable(self):
        """driver.wait_until_stable() は AppiumDriver に委譲する。"""
        driver, mock_appium = self._make()
        mock_appium.wait_until_stable.return_value = True
        result = driver.wait_until_stable()
        mock_appium.wait_until_stable.assert_called_once()
        assert result is True


# ============================================================
# MirroringDriver
# ============================================================

class TestMirroringDriver:
    """MirroringDriver の各メソッドをモックでテスト。"""

    def _make(
        self,
        window_title: str = "",
        device_w: int = 393,
        device_h: int = 852,
    ) -> tuple:
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        driver = MirroringDriver(
            mock_appium,
            window_title=window_title,
            device_logical_width=device_w,
            device_logical_height=device_h,
        )
        return driver, mock_appium

    # --- BaseDriver 抽象メソッド ---

    def test_is_simulator_returns_false(self):
        driver, _ = self._make()
        assert driver.is_simulator() is False

    def test_get_screenshot_raises_when_no_window(self):
        """ウィンドウが見つからない場合 RuntimeError を送出する。"""
        driver, _ = self._make()
        with patch("tools.window_manager.find_mirroring_window", return_value=None):
            with pytest.raises(RuntimeError, match="ミラーリングウィンドウが見つかりません"):
                driver.get_screenshot()

    def test_get_screenshot_returns_ndarray(self):
        """ウィンドウが見つかった場合、numpy.ndarray を返す。"""
        driver, _ = self._make()
        fake_img = np.zeros((852, 393, 3), dtype=np.uint8)
        rect = (10, 20, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=fake_img):
                    result = driver.get_screenshot()

        assert isinstance(result, np.ndarray)
        assert result.shape == (852, 393, 3)
        assert driver._window_rect == rect  # キャッシュ済み

    def test_get_screenshot_caches_window_rect(self):
        """2回目の get_screenshot() ではウィンドウ検索を再実行しない。"""
        driver, _ = self._make()
        fake_img  = np.zeros((100, 100, 3), dtype=np.uint8)
        rect      = (0, 0, 100, 100)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect) as mock_find:
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=fake_img):
                    driver.get_screenshot()
                    driver.get_screenshot()

        # find_mirroring_window は初回のみ呼ばれる
        mock_find.assert_called_once()

    def test_tap_converts_window_coords_to_device_coords(self):
        """
        ウィンドウピクセル座標 → デバイス論理座標変換の検証。
        ウィンドウが 786×1704 (2x スケール) なら中央タップが half になる。
        """
        driver, mock_appium = self._make(device_w=393, device_h=852)
        driver._window_rect = (0, 0, 786, 1704)

        # ウィンドウ座標 (393, 852) → デバイス座標 (196, 426)
        driver.tap(393, 852)
        mock_appium.tap_coordinate.assert_called_once_with(196, 426)

    def test_tap_1_to_1_scale(self):
        """ウィンドウとデバイスが同サイズなら座標変換なし。"""
        driver, mock_appium = self._make(device_w=393, device_h=852)
        driver._window_rect = (0, 0, 393, 852)

        driver.tap(100, 200)
        mock_appium.tap_coordinate.assert_called_once_with(100, 200)

    def test_getattr_delegates_back(self):
        """driver.back() は AppiumDriver.back() に委譲する。"""
        driver, mock_appium = self._make()
        driver.back()
        mock_appium.back.assert_called_once()

    def test_custom_window_title_used_in_search(self):
        """window_title が設定されていればその値で検索する。"""
        driver, _ = self._make(window_title="MyMirror")
        fake_img = np.zeros((100, 100, 3), dtype=np.uint8)

        with patch("tools.window_manager.find_mirroring_window", return_value=(0, 0, 100, 100)) as mock_find:
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=fake_img):
                    driver.get_screenshot()

        # ["MyMirror"] で検索されること
        mock_find.assert_called_once_with(["MyMirror"])

    def test_default_candidates_used_when_no_title(self):
        """window_title 未指定時は既定候補で検索する。"""
        driver, _ = self._make(window_title="")
        fake_img = np.zeros((100, 100, 3), dtype=np.uint8)

        with patch("tools.window_manager.find_mirroring_window", return_value=(0, 0, 100, 100)) as mock_find:
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=fake_img):
                    driver.get_screenshot()

        called_candidates = mock_find.call_args[0][0]
        assert "UxPlay" in called_candidates


# ============================================================
# WindowManager
# ============================================================

class TestWindowManager:
    """tools/window_manager.py を Quartz モックでテスト。"""

    def _fake_window(self, owner: str, title: str, x=0, y=0, w=393, h=852) -> dict:
        return {
            "kCGWindowOwnerName": owner,
            "kCGWindowName": title,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
        }

    def test_find_returns_none_when_no_windows(self):
        """ウィンドウが 0 件のとき None を返す。"""
        from tools.window_manager import find_mirroring_window

        mock_quartz = MagicMock()
        mock_quartz.CGWindowListCopyWindowInfo.return_value = []

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            # 再インポートさせるためキャッシュをクリア
            import importlib
            import tools.window_manager as wm
            importlib.reload(wm)
            result = wm.find_mirroring_window(["UxPlay"])

        assert result is None

    def test_find_returns_rect_for_uxplay(self):
        """UxPlay ウィンドウが存在するとき (x, y, w, h) を返す。"""
        from tools.window_manager import find_mirroring_window

        fake_wins = [
            self._fake_window("UxPlay", "iPhone 16 Plus", x=100, y=200, w=393, h=852)
        ]
        mock_quartz = MagicMock()
        mock_quartz.CGWindowListCopyWindowInfo.return_value = fake_wins
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 4
        mock_quartz.kCGNullWindowID = 0

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import importlib
            import tools.window_manager as wm
            importlib.reload(wm)
            result = wm.find_mirroring_window(["UxPlay"])

        assert result == (100, 200, 393, 852)

    def test_find_skips_zero_size_windows(self):
        """サイズが 0 のウィンドウはスキップする。"""
        fake_wins = [
            self._fake_window("UxPlay", "invisible", w=0, h=0),
        ]
        mock_quartz = MagicMock()
        mock_quartz.CGWindowListCopyWindowInfo.return_value = fake_wins
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 4
        mock_quartz.kCGNullWindowID = 0

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import importlib
            import tools.window_manager as wm
            importlib.reload(wm)
            result = wm.find_mirroring_window(["UxPlay"])

        assert result is None

    def test_capture_region_returns_bgr_ndarray(self):
        """capture_region() は (H, W, 3) BGR ndarray を返す。"""
        # mss は venv 未インストールのため sys.modules へモックを注入する
        import importlib

        # BGRA 4チャネル画像を返す mock_sct
        fake_bgra = np.zeros((100, 200, 4), dtype=np.uint8)
        fake_bgra[:, :, 0] = 50   # B
        fake_bgra[:, :, 1] = 100  # G
        fake_bgra[:, :, 2] = 150  # R
        fake_bgra[:, :, 3] = 255  # A (drop される)

        mock_sct = MagicMock()
        mock_sct.grab.return_value = fake_bgra

        mock_mss_cm = MagicMock()
        mock_mss_cm.__enter__ = MagicMock(return_value=mock_sct)
        mock_mss_cm.__exit__ = MagicMock(return_value=False)

        mock_mss_module = MagicMock()
        mock_mss_module.mss.return_value = mock_mss_cm

        with patch.dict(sys.modules, {"mss": mock_mss_module}):
            # モジュールを再ロードして import mss を再実行させる
            import tools.window_manager as wm
            importlib.reload(wm)
            result = wm.capture_region((10, 20, 200, 100))

        assert isinstance(result, np.ndarray)
        assert result.shape == (100, 200, 3)   # アルファチャネルなし
        assert result[0, 0, 0] == 50           # B チャネル


# ============================================================
# DriverFactory
# ============================================================

class TestDriverFactory:
    """driver_factory.py のモードResolution テスト。"""

    def test_simulator_mode_via_ius(self):
        """IOS_USE_SIMULATOR=1 は SIMULATOR モードを返す。"""
        from driver_factory import _resolve_device_mode

        with patch.dict(os.environ, {"IOS_USE_SIMULATOR": "1"}, clear=False):
            assert _resolve_device_mode() == "SIMULATOR"

    def test_simulator_mode_via_udid(self):
        """IOS_SIMULATOR_UDID が設定されていれば SIMULATOR を返す。"""
        from driver_factory import _resolve_device_mode

        with patch.dict(
            os.environ,
            {"IOS_SIMULATOR_UDID": "BA7E719D-8EBA-4049-996C-AC51945A7AE4"},
            clear=False,
        ):
            assert _resolve_device_mode() == "SIMULATOR"

    def test_mirror_mode_via_device_mode(self):
        """DEVICE_MODE=MIRROR は MIRROR モードを返す。"""
        from driver_factory import _resolve_device_mode

        with patch.dict(os.environ, {"DEVICE_MODE": "MIRROR"}, clear=False):
            assert _resolve_device_mode() == "MIRROR"

    def test_mirror_mode_case_insensitive(self):
        """DEVICE_MODE=mirror (小文字) でも MIRROR モードを返す。"""
        from driver_factory import _resolve_device_mode

        with patch.dict(os.environ, {"DEVICE_MODE": "mirror"}, clear=False):
            assert _resolve_device_mode() == "MIRROR"

    def test_device_mode_mirror_overrides_ius(self):
        """DEVICE_MODE=MIRROR は IOS_USE_SIMULATOR より優先される。"""
        from driver_factory import _resolve_device_mode

        with patch.dict(
            os.environ,
            {"DEVICE_MODE": "MIRROR", "IOS_USE_SIMULATOR": "1"},
            clear=False,
        ):
            assert _resolve_device_mode() == "MIRROR"

    def test_default_is_simulator(self):
        """環境変数が何もない場合デフォルトは SIMULATOR。"""
        from driver_factory import _resolve_device_mode

        env_clean = {
            k: v for k, v in os.environ.items()
            if k not in {"DEVICE_MODE", "IOS_USE_SIMULATOR", "IOS_SIMULATOR_UDID"}
        }
        with patch.dict(os.environ, env_clean, clear=True):
            assert _resolve_device_mode() == "SIMULATOR"

    def test_create_driver_session_yields_simulator_driver(self):
        """create_driver_session() が SimulatorDriver を yield する。"""
        from driver_adapter import SimulatorDriver
        from driver_factory import create_driver_session

        mock_appium = MagicMock()

        with patch(
            "driver_factory._create_simulator_session",
        ) as mock_session_fn:
            from contextlib import contextmanager

            @contextmanager
            def _mock_sim_session():
                yield SimulatorDriver(mock_appium)

            mock_session_fn.return_value = _mock_sim_session()

            with create_driver_session("SIMULATOR") as driver:
                assert isinstance(driver, SimulatorDriver)
                assert driver.is_simulator() is True

    def test_create_driver_session_yields_mirroring_driver(self):
        """create_driver_session("MIRROR") が MirroringDriver を yield する。"""
        from driver_adapter import MirroringDriver
        from driver_factory import create_driver_session

        mock_appium = MagicMock()

        with patch(
            "driver_factory._create_mirroring_session",
        ) as mock_session_fn:
            from contextlib import contextmanager

            @contextmanager
            def _mock_mirror_session():
                yield MirroringDriver(mock_appium)

            mock_session_fn.return_value = _mock_mirror_session()

            with create_driver_session("MIRROR") as driver:
                assert isinstance(driver, MirroringDriver)
                assert driver.is_simulator() is False
