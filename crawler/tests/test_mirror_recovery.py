"""
test_mirror_recovery.py — ミラーリング最適化・リカバリ機構テストスイート

Appium 不要。すべてのテストはモックで動作する。

テスト対象:
  window_manager.py   — bring_window_to_front / _find_window_owner
  driver_adapter.py   — WindowNotFoundError / 解像度チェック / screenshot() 検証
  crawler.py          — CrawlerConfig.device_mode / save_summary_json device_mode 出力
  main.py (統合)      — WindowNotFoundError → 中断保存フロー
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import numpy as np
import pytest

# crawler/ をパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))

# tools.window_manager を sys.modules に登録しておく。
# TestBringWindowToFront が patch.dict(sys.modules, {"Quartz": ...}) 内で
# importlib.reload() を呼ぶとき、モジュールが事前登録されていないと
# patch.dict の終了時に sys.modules から除去されてしまい、
# 後続テストの patch("tools.window_manager.*") が機能しなくなるため。
import tools.window_manager as _wm_module  # noqa: F401 (side effect: keep in sys.modules)


# ============================================================
# window_manager — bring_window_to_front
# ============================================================

class TestBringWindowToFront:
    """bring_window_to_front() のテスト。"""

    def _make_quartz_mock(self, owner: str = "UxPlay", title: str = "iPhone 16") -> MagicMock:
        """Quartz モックとウィンドウリストを生成する。"""
        mock_quartz = MagicMock()
        mock_quartz.kCGWindowListOptionOnScreenOnly = 1
        mock_quartz.kCGWindowListExcludeDesktopElements = 4
        mock_quartz.kCGNullWindowID = 0
        mock_quartz.CGWindowListCopyWindowInfo.return_value = [
            {
                "kCGWindowOwnerName": owner,
                "kCGWindowName": title,
                "kCGWindowBounds": {"X": 0, "Y": 0, "Width": 393, "Height": 852},
            }
        ]
        return mock_quartz

    def test_calls_osascript_with_correct_app_name(self):
        """UxPlay ウィンドウが存在する場合、osascript で activate を呼ぶ。"""
        import importlib
        mock_quartz = self._make_quartz_mock(owner="UxPlay")

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import tools.window_manager as wm
            importlib.reload(wm)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = wm.bring_window_to_front(["UxPlay"])

        assert result is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "osascript" in cmd_args
        assert any("UxPlay" in arg for arg in cmd_args)

    def test_returns_false_when_window_not_found(self):
        """ウィンドウが見つからない場合 False を返す。"""
        import importlib
        mock_quartz = MagicMock()
        mock_quartz.CGWindowListCopyWindowInfo.return_value = []

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import tools.window_manager as wm
            importlib.reload(wm)
            with patch("subprocess.run") as mock_run:
                result = wm.bring_window_to_front(["NoSuchApp"])

        assert result is False
        mock_run.assert_not_called()

    def test_returns_false_on_osascript_failure(self):
        """osascript が非ゼロで返った場合 False を返す。"""
        import importlib
        mock_quartz = self._make_quartz_mock(owner="UxPlay")

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import tools.window_manager as wm
            importlib.reload(wm)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(
                    returncode=1,
                    stderr=b"error: application not found",
                )
                result = wm.bring_window_to_front(["UxPlay"])

        assert result is False

    def test_returns_false_on_timeout(self):
        """osascript がタイムアウトした場合 False を返す。"""
        import importlib
        import subprocess
        mock_quartz = self._make_quartz_mock(owner="UxPlay")

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import tools.window_manager as wm
            importlib.reload(wm)
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("osascript", 3)):
                result = wm.bring_window_to_front(["UxPlay"])

        assert result is False

    def test_case_insensitive_title_match(self):
        """大文字小文字を区別しないタイトル一致で前面表示を呼ぶ。"""
        import importlib
        mock_quartz = self._make_quartz_mock(owner="UxPlay", title="iphone 16")

        with patch.dict(sys.modules, {"Quartz": mock_quartz}):
            import tools.window_manager as wm
            importlib.reload(wm)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                result = wm.bring_window_to_front(["UXPLAY"])

        assert result is True


# ============================================================
# MirroringDriver — WindowNotFoundError
# ============================================================

class TestWindowNotFoundError:
    """WindowNotFoundError の送出・捕捉を検証する。"""

    def _make_driver(self):
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        return MirroringDriver(mock_appium), mock_appium

    def test_error_is_subclass_of_runtime_error(self):
        """WindowNotFoundError は RuntimeError のサブクラスであること。"""
        from driver_adapter import BaseDriver
        assert issubclass(BaseDriver.WindowNotFoundError, RuntimeError)

    def test_get_screenshot_raises_on_initial_not_found(self):
        """初回 get_screenshot() でウィンドウが見つからない → WindowNotFoundError。"""
        driver, _ = self._make_driver()
        with patch("tools.window_manager.find_mirroring_window", return_value=None):
            with patch("tools.window_manager.bring_window_to_front"):
                from driver_adapter import BaseDriver
                with pytest.raises(BaseDriver.WindowNotFoundError, match="見つかりません"):
                    driver.get_screenshot()

    def test_get_screenshot_raises_on_capture_failure_and_recheck_fails(self):
        """キャプチャ失敗後の再検索もNone → WindowNotFoundError。"""
        driver, _ = self._make_driver()
        rect = (0, 0, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front"):
                with patch(
                    "tools.window_manager.capture_region",
                    side_effect=Exception("capture failed"),
                ):
                    # 2回目の find も None を返すよう再設定
                    with patch(
                        "tools.window_manager.find_mirroring_window",
                        side_effect=[rect, None],  # 1回目: 初期化, 2回目: 再検索
                    ):
                        from driver_adapter import BaseDriver
                        with pytest.raises(BaseDriver.WindowNotFoundError):
                            driver.get_screenshot()

    def test_screenshot_raises_window_not_found_on_verify(self):
        """screenshot() の定期検証で消失を検知 → WindowNotFoundError。"""
        from driver_adapter import BaseDriver
        driver, mock_appium = self._make_driver()
        driver._screenshot_count = 0

        # _verify_window_exists() が消失を検知するよう設定
        # 1回目の呼び出しは VERIFY_INTERVAL の 1回目のチェック (count=1)
        with patch.object(driver, "_verify_window_exists",
                          side_effect=BaseDriver.WindowNotFoundError("消失")):
            with pytest.raises(BaseDriver.WindowNotFoundError):
                driver.screenshot("test")

    def test_screenshot_does_not_verify_every_call(self):
        """VERIFY_INTERVAL の間隔外では _verify_window_exists を呼ばない。"""
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        mock_appium.screenshot.return_value = Path("/tmp/shot.png")
        driver = MirroringDriver(mock_appium)
        # 1回目チェック済みとして count=1 にセット
        # 次のチェックは count=6 (6%5==1) になるので、4回呼んでも発火しない
        driver._screenshot_count = 1

        with patch.object(driver, "_verify_window_exists") as mock_verify:
            # count: 1→2(2%5=2), 2→3(3%5=3), 3→4(4%5=4), 4→5(5%5=0)
            driver.screenshot("test2")
            driver.screenshot("test3")
            driver.screenshot("test4")
            driver.screenshot("test5")

        # 4回呼んだが VERIFY_INTERVAL 内なのでチェックなし
        mock_verify.assert_not_called()

    def test_verify_window_exists_raises_when_not_found(self):
        """_verify_window_exists() はウィンドウ消失時に WindowNotFoundError。"""
        from driver_adapter import BaseDriver, MirroringDriver
        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)
        driver._window_rect = (0, 0, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=None):
            with pytest.raises(BaseDriver.WindowNotFoundError):
                driver._verify_window_exists()

        # キャッシュが無効化されていること
        assert driver._window_rect is None

    def test_verify_window_exists_updates_moved_window(self):
        """ウィンドウが移動した場合、キャッシュを新位置に更新する。"""
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)
        driver._window_rect = (0, 0, 393, 852)
        new_rect = (100, 200, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=new_rect):
            driver._verify_window_exists()

        assert driver._window_rect == new_rect


# ============================================================
# MirroringDriver — 解像度チェック・リサイズ
# ============================================================

class TestResolutionCheck:
    """低解像度キャプチャの警告・リサイズをテストする。"""

    def _make_driver(self, device_w=393, device_h=852):
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        return MirroringDriver(
            mock_appium,
            device_logical_width=device_w,
            device_logical_height=device_h,
        ), mock_appium

    def test_small_image_is_resized(self):
        """MIN_CAPTURE_WIDTH × MIN_CAPTURE_HEIGHT 未満の画像をリサイズする。"""
        driver, _ = self._make_driver(device_w=393, device_h=852)
        # 非常に小さい画像 (100×200)
        small_img = np.zeros((200, 100, 3), dtype=np.uint8)
        rect = (0, 0, 100, 200)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=small_img):
                    result = driver.get_screenshot()

        # デバイス論理サイズ (852×393) にリサイズされていること
        assert result.shape == (852, 393, 3)

    def test_small_image_triggers_warning(self, caplog):
        """低解像度画像が検出された場合、WARNING ログを出力する。"""
        import logging
        driver, _ = self._make_driver(device_w=393, device_h=852)
        small_img = np.zeros((200, 100, 3), dtype=np.uint8)
        rect = (0, 0, 100, 200)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=small_img):
                    with caplog.at_level(logging.WARNING):
                        driver.get_screenshot()

        assert any("低すぎます" in r.message for r in caplog.records)

    def test_adequate_image_is_not_resized(self):
        """十分な解像度の画像はリサイズしない。"""
        driver, _ = self._make_driver(device_w=393, device_h=852)
        # 十分な解像度 (393×852)
        normal_img = np.zeros((852, 393, 3), dtype=np.uint8)
        rect = (0, 0, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front"):
                with patch("tools.window_manager.capture_region", return_value=normal_img):
                    result = driver.get_screenshot()

        assert result.shape == (852, 393, 3)

    def test_bring_to_front_called_on_initial_discovery(self):
        """ウィンドウ初回発見時に bring_window_to_front() が呼ばれる。"""
        driver, _ = self._make_driver()
        normal_img = np.zeros((852, 393, 3), dtype=np.uint8)
        rect = (0, 0, 393, 852)

        with patch("tools.window_manager.find_mirroring_window", return_value=rect):
            with patch("tools.window_manager.bring_window_to_front") as mock_btf:
                with patch("tools.window_manager.capture_region", return_value=normal_img):
                    driver.get_screenshot()

        mock_btf.assert_called_once()

    def test_bring_to_front_not_called_on_subsequent_calls(self):
        """キャッシュ済みの 2 回目以降は bring_window_to_front() を呼ばない。"""
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)
        # すでにキャッシュ済みとして設定
        driver._window_rect = (0, 0, 393, 852)
        normal_img = np.zeros((852, 393, 3), dtype=np.uint8)

        with patch("tools.window_manager.bring_window_to_front") as mock_btf:
            with patch("tools.window_manager.capture_region", return_value=normal_img):
                driver.get_screenshot()

        mock_btf.assert_not_called()


# ============================================================
# CrawlerConfig — device_mode フィールド
# ============================================================

class TestCrawlerConfigDeviceMode:
    """CrawlerConfig.device_mode と crawl_summary.json への出力をテストする。"""

    def test_default_device_mode_is_simulator(self):
        """デフォルト値は "SIMULATOR"。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig()
        assert cfg.device_mode == "SIMULATOR"

    def test_device_mode_can_be_set_to_mirror(self):
        """MIRROR モードを設定できる。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig(device_mode="MIRROR")
        assert cfg.device_mode == "MIRROR"

    def test_save_summary_json_includes_device_mode_simulator(self, tmp_path):
        """SIMULATOR モードが crawl_summary.json に記録される。"""
        self._check_device_mode_in_json(tmp_path, "SIMULATOR")

    def test_save_summary_json_includes_device_mode_mirror(self, tmp_path):
        """MIRROR モードが crawl_summary.json に記録される。"""
        self._check_device_mode_in_json(tmp_path, "MIRROR")

    def _check_device_mode_in_json(self, tmp_path: Path, mode: str) -> None:
        from lc.crawler import CrawlerConfig, ScreenCrawler

        mock_driver = MagicMock()
        mock_driver._evidence_dir = tmp_path
        mock_driver.session_id = "test_session"

        cfg = CrawlerConfig(device_mode=mode, game_title="TestGame")
        crawler = ScreenCrawler(mock_driver, cfg)

        summary_path = tmp_path / "crawl_summary.json"
        crawler.save_summary_json(summary_path)

        data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert data["device_mode"] == mode
        assert data["game_title"] == "TestGame"

    def test_save_summary_json_includes_session_id(self, tmp_path):
        """session_id が crawl_summary.json に記録される。"""
        from lc.crawler import CrawlerConfig, ScreenCrawler

        mock_driver = MagicMock()
        mock_driver._evidence_dir = tmp_path
        mock_driver.session_id = "test_session"

        session_dir = tmp_path / "20260304_120000"
        session_dir.mkdir()
        summary_path = session_dir / "crawl_summary.json"

        cfg = CrawlerConfig(device_mode="MIRROR")
        crawler = ScreenCrawler(mock_driver, cfg)
        crawler.save_summary_json(summary_path)

        data = json.loads(summary_path.read_text(encoding="utf-8"))
        assert data["session_id"] == "20260304_120000"
        assert data["device_mode"] == "MIRROR"


# ============================================================
# main.py 統合 — WindowNotFoundError → 中断保存フロー
# ============================================================

class TestMainWindowLossRecovery:
    """
    DEVICE_MODE=MIRROR でウィンドウ消失が発生した際に、
    クロールが安全に中断されデータが保存されることを検証する。
    """

    def test_window_not_found_error_is_caught_and_summary_saved(self, tmp_path):
        """
        crawler.crawl() が WindowNotFoundError を送出したとき、
        save_summary_json() が呼ばれ正常終了すること。
        """
        from driver_adapter import BaseDriver
        from lc.crawler import CrawlerConfig, CrawlStats

        # Crawler モック: crawl() で WindowNotFoundError を送出
        mock_crawler = MagicMock()
        mock_crawler.crawl.side_effect = BaseDriver.WindowNotFoundError("テスト消失")
        mock_crawler._stats = CrawlStats(screens_found=3, taps_total=5)
        mock_crawler.save_summary_json = MagicMock()

        evidence_dir = tmp_path / "evidence"
        session_dir  = evidence_dir / "20260304_120000"
        session_dir.mkdir(parents=True)

        mock_driver = MagicMock()
        mock_driver.dismiss_any_modal.return_value = False

        with patch("driver_factory.create_driver_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_driver)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            with patch("lc.crawler.ScreenCrawler", return_value=mock_crawler):
                with patch("driver_factory._resolve_device_mode", return_value="MIRROR"):
                    with patch(
                        "pathlib.Path.glob",
                        return_value=iter([session_dir]),
                    ):
                        import importlib
                        import main as m
                        importlib.reload(m)

                        with patch.dict(
                            os.environ,
                            {"IOS_BUNDLE_ID": "com.example.test", "DEVICE_MODE": "MIRROR"},
                            clear=False,
                        ):
                            # main() を呼ぶと例外なく終了すること
                            m.main()

        # save_summary_json が呼ばれたことを確認
        mock_crawler.save_summary_json.assert_called_once()

    def test_window_not_found_before_crawler_init_does_not_crash(self, tmp_path):
        """
        crawler が初期化される前に WindowNotFoundError が起きても
        クラッシュしないこと。
        """
        from driver_adapter import BaseDriver

        mock_driver = MagicMock()
        mock_driver.dismiss_any_modal.side_effect = BaseDriver.WindowNotFoundError("事前消失")

        with patch("driver_factory.create_driver_session") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_driver)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            with patch("lc.crawler.ScreenCrawler"):
                with patch("driver_factory._resolve_device_mode", return_value="MIRROR"):
                    import importlib
                    import main as m
                    importlib.reload(m)

                    with patch.dict(
                        os.environ,
                        {"IOS_BUNDLE_ID": "com.example.test"},
                        clear=False,
                    ):
                        # クラッシュなく終了すること
                        m.main()
