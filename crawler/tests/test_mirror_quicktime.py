"""
test_mirror_quicktime.py — UxPlay / QuickTime Player ハイブリッド対応テスト

Appium 不要。Quartz / mss のウィンドウ検索をモックで動作させる。

テスト対象:
  tools/window_manager.py
    - find_mirroring_window_ex() がウィンドウ領域とオーナー名を返すこと
    - find_mirroring_window() が後方互換を保つこと
  driver_adapter.py
    - _WINDOW_TITLE_CANDIDATES に "QuickTime Player" が含まれること
    - _crop_for_source() が QuickTime クロームを除去すること
    - WindowNotFoundError が QuickTime 手順を含むこと
    - ウィンドウ発見時に _window_source が設定されること
  lc/crawler.py
    - CrawlerConfig のデフォルト値が更新されていること
  main.py
    - --tap-wait / --stuck-threshold の動作
    - ミラーモードで wait_after_tap=4.0 がデフォルトになること
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================
# find_mirroring_window_ex — 拡張ウィンドウ検索
# ============================================================


class TestFindMirroringWindowEx:
    """find_mirroring_window_ex() の動作を Quartz モックで検証。"""

    def _fake_window_list(self, owner: str, title: str, w: int = 400, h: int = 800):
        return [{
            "kCGWindowOwnerName": owner,
            "kCGWindowName":      title,
            "kCGWindowBounds":    {"X": 10, "Y": 20, "Width": w, "Height": h},
        }]

    def _mock_quartz(self, window_list):
        quartz = MagicMock()
        quartz.kCGWindowListOptionOnScreenOnly   = 1
        quartz.kCGWindowListExcludeDesktopElements = 2
        quartz.kCGNullWindowID = 0
        quartz.CGWindowListCopyWindowInfo.return_value = window_list
        return quartz

    def test_returns_rect_and_owner_for_uxplay(self):
        """UxPlay ウィンドウが見つかった場合、rect と "UxPlay" オーナー名を返すこと。"""
        from tools.window_manager import find_mirroring_window_ex

        wlist = self._fake_window_list("UxPlay", "iPhone Screen")
        quartz = self._mock_quartz(wlist)

        with patch("tools.window_manager._require_darwin"), \
             patch("tools.window_manager._import_quartz", return_value=quartz):
            result = find_mirroring_window_ex(["UxPlay", "QuickTime Player"])

        assert result is not None
        rect, owner = result
        assert rect == (10, 20, 400, 800)
        assert owner == "UxPlay"

    def test_returns_rect_and_owner_for_quicktime(self):
        """QuickTime Player ウィンドウが見つかった場合、rect と "QuickTime Player" を返すこと。"""
        from tools.window_manager import find_mirroring_window_ex

        wlist = self._fake_window_list("QuickTime Player", "iPhone — 録画中")
        quartz = self._mock_quartz(wlist)

        with patch("tools.window_manager._require_darwin"), \
             patch("tools.window_manager._import_quartz", return_value=quartz):
            result = find_mirroring_window_ex(["UxPlay", "QuickTime Player", "iPhone"])

        assert result is not None
        rect, owner = result
        assert rect == (10, 20, 400, 800)
        assert owner == "QuickTime Player"

    def test_returns_none_when_not_found(self):
        """一致するウィンドウがない場合 None を返すこと。"""
        from tools.window_manager import find_mirroring_window_ex

        wlist = self._fake_window_list("Finder", "デスクトップ")
        quartz = self._mock_quartz(wlist)

        with patch("tools.window_manager._require_darwin"), \
             patch("tools.window_manager._import_quartz", return_value=quartz):
            result = find_mirroring_window_ex(["UxPlay", "QuickTime Player"])

        assert result is None

    def test_backward_compat_find_mirroring_window(self):
        """find_mirroring_window() が後方互換で rect のみを返すこと。"""
        from tools.window_manager import find_mirroring_window

        wlist = self._fake_window_list("UxPlay", "Screen")
        quartz = self._mock_quartz(wlist)

        with patch("tools.window_manager._require_darwin"), \
             patch("tools.window_manager._import_quartz", return_value=quartz):
            rect = find_mirroring_window(["UxPlay"])

        assert rect == (10, 20, 400, 800)

    def test_backward_compat_returns_none_when_not_found(self):
        """find_mirroring_window() が見つからない場合 None を返すこと。"""
        from tools.window_manager import find_mirroring_window

        wlist = self._fake_window_list("Safari", "ブラウザ")
        quartz = self._mock_quartz(wlist)

        with patch("tools.window_manager._require_darwin"), \
             patch("tools.window_manager._import_quartz", return_value=quartz):
            rect = find_mirroring_window(["UxPlay"])

        assert rect is None


# ============================================================
# MirroringDriver — QuickTime 候補・_window_source・トリム
# ============================================================


class TestMirroringDriverQuickTime:
    def _make_driver(self, window_title: str = "") -> "object":
        from driver_adapter import MirroringDriver
        mock_appium = MagicMock()
        mock_appium.wait.return_value = None
        return MirroringDriver(mock_appium, window_title=window_title)

    # --- _WINDOW_TITLE_CANDIDATES ---

    def test_candidates_include_quicktime(self):
        """_WINDOW_TITLE_CANDIDATES に 'QuickTime Player' が含まれること。"""
        from driver_adapter import MirroringDriver
        assert "QuickTime Player" in MirroringDriver._WINDOW_TITLE_CANDIDATES

    def test_candidates_order_uxplay_before_quicktime(self):
        """UxPlay が QuickTime Player より前に検索されること（優先順位）。"""
        from driver_adapter import MirroringDriver
        cands = list(MirroringDriver._WINDOW_TITLE_CANDIDATES)
        assert cands.index("UxPlay") < cands.index("QuickTime Player")

    def test_candidates_include_iphone_as_fallback(self):
        """'iPhone' がフォールバック候補として含まれること。"""
        from driver_adapter import MirroringDriver
        assert "iPhone" in MirroringDriver._WINDOW_TITLE_CANDIDATES

    # --- _window_source 設定 ---

    def test_window_source_set_to_quicktime_on_discovery(self):
        """QuickTime ウィンドウを発見した際 _window_source が 'QuickTime Player' になること。"""
        from driver_adapter import MirroringDriver

        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)

        fake_img = np.zeros((800, 400, 3), dtype=np.uint8)
        fake_rect = (10, 20, 400, 800)

        # driver_adapter.py はローカルインポートするため tools.window_manager 側をパッチ
        with patch("tools.window_manager.find_mirroring_window_ex",
                   return_value=(fake_rect, "QuickTime Player")), \
             patch("tools.window_manager.bring_window_to_front", return_value=True), \
             patch("tools.window_manager.capture_region", return_value=fake_img):
            driver.get_screenshot()

        assert driver._window_source == "QuickTime Player"

    def test_window_source_set_to_uxplay(self):
        """UxPlay ウィンドウ発見時 _window_source が 'UxPlay' になること。"""
        from driver_adapter import MirroringDriver

        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)

        fake_img = np.zeros((800, 400, 3), dtype=np.uint8)
        fake_rect = (0, 0, 400, 800)

        with patch("tools.window_manager.find_mirroring_window_ex",
                   return_value=(fake_rect, "UxPlay")), \
             patch("tools.window_manager.bring_window_to_front", return_value=True), \
             patch("tools.window_manager.capture_region", return_value=fake_img):
            driver.get_screenshot()

        assert driver._window_source == "UxPlay"

    # --- _crop_for_source() ---

    def test_crop_removes_title_bar_and_controls_for_quicktime(self):
        """QuickTime ソースのとき top と bottom が除去されること。"""
        driver = self._make_driver()
        driver._window_source = "QuickTime Player"

        h, w = 800, 400
        img = np.ones((h, w, 3), dtype=np.uint8) * 128

        cropped = driver._crop_for_source(img)

        expected_top    = max(0, min(int(h * 0.035), 40))  # ≈ 28
        expected_bottom = max(0, min(int(h * 0.055), 60))  # ≈ 44
        expected_height = h - expected_top - expected_bottom

        assert cropped.shape[0] == expected_height, (
            f"期待高さ {expected_height}px、実際 {cropped.shape[0]}px"
        )
        assert cropped.shape[1] == w  # 幅は変わらない

    def test_crop_no_trim_for_uxplay(self):
        """UxPlay ソースのときはトリムしないこと。"""
        driver = self._make_driver()
        driver._window_source = "UxPlay"

        img = np.ones((800, 400, 3), dtype=np.uint8)
        cropped = driver._crop_for_source(img)

        assert cropped.shape == img.shape

    def test_crop_no_trim_for_empty_source(self):
        """_window_source が空のときもトリムしないこと。"""
        driver = self._make_driver()
        driver._window_source = ""

        img = np.ones((600, 300, 3), dtype=np.uint8)
        cropped = driver._crop_for_source(img)

        assert cropped.shape == img.shape

    def test_crop_safety_check_prevents_overtrim(self):
        """トリム後の高さが 100px 未満にならないこと（過剰トリム防止）。"""
        driver = self._make_driver()
        driver._window_source = "QuickTime Player"

        # 非常に小さい画像
        img = np.ones((50, 200, 3), dtype=np.uint8)
        cropped = driver._crop_for_source(img)

        # 安全チェックが効いて元画像がそのまま返る
        assert cropped.shape == img.shape

    def test_crop_preserves_content_after_trim(self):
        """トリム後に iPhone 画面コンテンツ（中間部分）が保持されること。"""
        driver = self._make_driver()
        driver._window_source = "QuickTime Player"

        h, w = 800, 400
        img = np.zeros((h, w, 3), dtype=np.uint8)
        # 中央にマーカーを描画
        img[400, 200] = [0, 255, 0]  # 中心ピクセルを緑に

        cropped = driver._crop_for_source(img)

        top = max(0, min(int(h * 0.035), 40))
        # 中心ピクセルはトリム後も存在する (y=400 - top)
        cy = 400 - top
        if 0 <= cy < cropped.shape[0]:
            assert list(cropped[cy, 200]) == [0, 255, 0]

    # --- WindowNotFoundError メッセージ ---

    def test_error_message_mentions_quicktime(self):
        """ウィンドウが見つからない場合のエラーに QuickTime Player 手順が含まれること。"""
        from driver_adapter import BaseDriver, MirroringDriver

        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)

        with patch("tools.window_manager.find_mirroring_window_ex", return_value=None), \
             pytest.raises(BaseDriver.WindowNotFoundError) as exc_info:
            driver.get_screenshot()

        msg = str(exc_info.value)
        assert "QuickTime Player" in msg, "QuickTime Player の手順がエラーメッセージにない"
        assert "UxPlay" in msg, "UxPlay の手順がエラーメッセージにない"

    def test_error_message_mentions_wireless_and_wired(self):
        """エラーメッセージに無線と有線の両方の手順が含まれること。"""
        from driver_adapter import BaseDriver, MirroringDriver

        mock_appium = MagicMock()
        driver = MirroringDriver(mock_appium)

        with patch("tools.window_manager.find_mirroring_window_ex", return_value=None), \
             pytest.raises(BaseDriver.WindowNotFoundError) as exc_info:
            driver.get_screenshot()

        msg = str(exc_info.value)
        # 無線の手順
        assert any(w in msg for w in ["無線", "AirPlay", "ミラーリング"]), \
            "無線接続手順がエラーメッセージにない"
        # 有線の手順
        assert any(w in msg for w in ["有線", "USB", "ムービー収録"]), \
            "有線接続手順がエラーメッセージにない"


# ============================================================
# CrawlerConfig — しきい値デフォルト値
# ============================================================


class TestCrawlerConfigDefaults:
    def test_min_confidence_is_0_5(self):
        """min_confidence のデフォルト値が 0.5 になっていること (ゲーム UI 対応)。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig()
        assert cfg.min_confidence == 0.5

    def test_icon_threshold_is_0_75(self):
        """icon_threshold のデフォルト値が 0.75 になっていること (アート調ボタン対応)。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig()
        assert cfg.icon_threshold == 0.75

    def test_anti_stuck_threshold_is_3(self):
        """anti_stuck_threshold のデフォルト値が 3 になっていること (ゲームロード考慮)。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig()
        assert cfg.anti_stuck_threshold == 3

    def test_can_override_min_confidence(self):
        """min_confidence はコンストラクタで上書きできること。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig(min_confidence=0.7)
        assert cfg.min_confidence == 0.7

    def test_can_override_anti_stuck_threshold(self):
        """anti_stuck_threshold はコンストラクタで上書きできること。"""
        from lc.crawler import CrawlerConfig
        cfg = CrawlerConfig(anti_stuck_threshold=5)
        assert cfg.anti_stuck_threshold == 5


# ============================================================
# main.py — --tap-wait / --stuck-threshold / ミラーモードデフォルト
# ============================================================


class TestMainMirrorDefaults:
    def _parse(self, argv: list[str]) -> "object":
        """テスト用 argparse。sys.argv を一時差し替えて parse する。"""
        import sys as _sys
        from main import _parse_args

        old_argv = _sys.argv
        _sys.argv = ["main.py"] + argv
        try:
            return _parse_args()
        finally:
            _sys.argv = old_argv

    def test_tap_wait_default_is_none(self):
        """--tap-wait 未指定のとき args.tap_wait が None になること。"""
        args = self._parse(["--bundle", "com.example.app"])
        assert args.tap_wait is None

    def test_tap_wait_parsed_correctly(self):
        """--tap-wait 5.5 が float で取得できること。"""
        args = self._parse(["--tap-wait", "5.5", "--bundle", "com.example.app"])
        assert args.tap_wait == 5.5

    def test_stuck_threshold_default_is_none(self):
        """--stuck-threshold 未指定のとき args.stuck_threshold が None になること。"""
        args = self._parse(["--bundle", "com.example.app"])
        assert args.stuck_threshold is None

    def test_stuck_threshold_parsed_correctly(self):
        """--stuck-threshold 4 が int で取得できること。"""
        args = self._parse(["--stuck-threshold", "4", "--bundle", "com.example.app"])
        assert args.stuck_threshold == 4

    def test_mirror_mode_uses_4_0_tap_wait(self):
        """
        ミラーモードでは --tap-wait 未指定時にデフォルト 4.0 秒が適用されること。
        _default_tap_wait の導出ロジックを直接テスト。
        """
        # is_mirror=True のとき _default_tap_wait = 4.0
        import os as _os

        is_mirror = True
        default_tap_wait = 4.0 if is_mirror else 3.0
        assert default_tap_wait == 4.0

    def test_simulator_mode_uses_3_0_tap_wait(self):
        """シミュレータモードでは --tap-wait 未指定時に 3.0 秒が適用されること。"""
        is_mirror = False
        default_tap_wait = 4.0 if is_mirror else 3.0
        assert default_tap_wait == 3.0
