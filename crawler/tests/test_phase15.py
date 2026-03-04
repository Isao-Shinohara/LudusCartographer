"""
test_phase15.py — Phase 15「自己修復型探索」テスト

Appium 不要。すべてのテストはモックで動作する。

テスト対象:
  lc/core.py
    - AppHealthMonitor.check_and_heal()
    - AppHealthMonitor.is_alive()
    - StuckDetector の各メソッド
    - FrontierTracker の各メソッド
  lc/crawler.py
    - _try_unstuck_gestures()
    - _navigate_to_frontier()
    - _smart_backtrack_loop()
  driver_adapter.py
    - BaseDriver.is_app_alive()
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.core import AppHealthMonitor, FrontierTracker, StuckDetector


# ============================================================
# ヘルパー: ScreenCrawler モック
# ============================================================

def _make_crawler(config_kwargs: dict | None = None):
    """最小限のモックドライバーで ScreenCrawler を作成して返す。"""
    from lc.crawler import CrawlerConfig, ScreenCrawler

    mock_driver = MagicMock()
    mock_driver._evidence_dir = Path("/tmp/test_phase15_evidence")
    mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)
    cfg = CrawlerConfig(**(config_kwargs or {}))
    return ScreenCrawler(mock_driver, cfg)


# ============================================================
# AppHealthMonitor
# ============================================================


class TestAppHealthMonitor:
    def _make_driver(self, state: int | None = 4, raise_on_query: bool = False):
        """AppiumDriver を模倣するモックドライバーを返す。"""
        mock = MagicMock()
        if raise_on_query:
            mock.driver.query_app_state.side_effect = RuntimeError("query failed")
        else:
            mock.driver.query_app_state.return_value = state
        mock.driver.activate_app.return_value = None
        mock.wait.return_value = None
        return mock

    def test_returns_true_when_foreground(self):
        """アプリが FOREGROUND (state=4) のとき True を返すこと。"""
        driver = self._make_driver(state=4)
        monitor = AppHealthMonitor(driver, "com.example.app")
        assert monitor.check_and_heal() is True

    def test_returns_true_when_no_bundle_id(self):
        """bundle_id が空のとき、チェックせず True を返すこと。"""
        driver = self._make_driver(state=1)
        monitor = AppHealthMonitor(driver, "")
        assert monitor.check_and_heal() is True
        driver.driver.query_app_state.assert_not_called()

    def test_returns_true_on_query_exception(self):
        """query_app_state が例外を投げた場合、楽観的に True を返すこと。"""
        driver = self._make_driver(raise_on_query=True)
        monitor = AppHealthMonitor(driver, "com.example.app")
        assert monitor.check_and_heal() is True

    def test_calls_activate_app_when_not_foreground(self):
        """state != 4 のとき activate_app() が呼ばれること。"""
        driver = self._make_driver(state=1)
        # activate_app 後に state=4 になる想定
        driver.driver.query_app_state.side_effect = [1, 4]
        monitor = AppHealthMonitor(driver, "com.example.app", max_retries=1)
        result = monitor.check_and_heal()
        driver.driver.activate_app.assert_called_once_with("com.example.app")
        assert result is True

    def test_retries_up_to_max_retries(self):
        """max_retries の回数まで activate_app() を試みること。"""
        driver = self._make_driver(state=3)
        # すべての query 呼び出しで non-foreground を返す
        driver.driver.query_app_state.return_value = 3
        monitor = AppHealthMonitor(driver, "com.example.app", max_retries=3)
        monitor.check_and_heal()
        # 初回チェック (1回) + リトライ後チェック (3回) = 4回
        assert driver.driver.query_app_state.call_count == 4

    def test_returns_false_when_all_retries_fail(self):
        """max_retries 後も復帰しなければ False を返すこと。"""
        driver = self._make_driver(state=1)
        driver.driver.query_app_state.return_value = 1
        monitor = AppHealthMonitor(driver, "com.example.app", max_retries=2)
        assert monitor.check_and_heal() is False

    def test_is_alive_returns_true_when_foreground(self):
        """is_alive(): state=4 のとき True を返すこと。"""
        driver = self._make_driver(state=4)
        monitor = AppHealthMonitor(driver, "com.example.app")
        assert monitor.is_alive() is True

    def test_is_alive_returns_false_when_background(self):
        """is_alive(): state=3 (background) のとき False を返すこと。"""
        driver = self._make_driver(state=3)
        monitor = AppHealthMonitor(driver, "com.example.app")
        assert monitor.is_alive() is False

    def test_is_alive_returns_true_on_exception(self):
        """is_alive(): 例外発生時は楽観的に True を返すこと。"""
        driver = self._make_driver(raise_on_query=True)
        monitor = AppHealthMonitor(driver, "com.example.app")
        assert monitor.is_alive() is True


# ============================================================
# StuckDetector
# ============================================================


class TestStuckDetector:
    def test_record_starts_at_one(self):
        """初回 record() は 1 を返すこと。"""
        sd = StuckDetector(threshold=2)
        assert sd.record("fp_a") == 1

    def test_record_increments_on_repeated_calls(self):
        """同一 fp への連続 record() でカウントが増えること。"""
        sd = StuckDetector(threshold=2)
        sd.record("fp_a")
        sd.record("fp_a")
        assert sd.record("fp_a") == 3

    def test_get_count_returns_zero_for_unknown(self):
        """未記録 fp の get_count() は 0 を返すこと。"""
        sd = StuckDetector(threshold=2)
        assert sd.get_count("unknown_fp") == 0

    def test_should_swipe_false_below_threshold(self):
        """threshold 未満のとき should_swipe は False。"""
        sd = StuckDetector(threshold=3)
        sd.record("fp_a")
        sd.record("fp_a")  # count=2 < threshold=3
        assert sd.should_swipe("fp_a") is False

    def test_should_swipe_true_at_threshold(self):
        """count == threshold のとき should_swipe は True。"""
        sd = StuckDetector(threshold=2)
        sd.record("fp_a")
        sd.record("fp_a")  # count=2 == threshold=2
        assert sd.should_swipe("fp_a") is True

    def test_should_swipe_false_at_hopeless(self):
        """count >= threshold*3 のとき should_swipe は False (hopeless)。"""
        sd = StuckDetector(threshold=2)
        for _ in range(6):  # count=6 == threshold*3
            sd.record("fp_a")
        assert sd.should_swipe("fp_a") is False

    def test_should_long_press_false_below_threshold_times_2(self):
        """count < threshold*2 のとき should_long_press は False。"""
        sd = StuckDetector(threshold=2)
        sd.record("fp_a")
        sd.record("fp_a")
        sd.record("fp_a")  # count=3 < threshold*2=4
        assert sd.should_long_press("fp_a") is False

    def test_should_long_press_true_at_threshold_times_2(self):
        """count >= threshold*2 のとき should_long_press は True。"""
        sd = StuckDetector(threshold=2)
        for _ in range(4):  # count=4 == threshold*2
            sd.record("fp_a")
        assert sd.should_long_press("fp_a") is True

    def test_is_hopeless_false_below_threshold_times_3(self):
        """count < threshold*3 のとき is_hopeless は False。"""
        sd = StuckDetector(threshold=2)
        for _ in range(5):  # count=5 < threshold*3=6
            sd.record("fp_a")
        assert sd.is_hopeless("fp_a") is False

    def test_is_hopeless_true_at_threshold_times_3(self):
        """count >= threshold*3 のとき is_hopeless は True。"""
        sd = StuckDetector(threshold=2)
        for _ in range(6):  # count=6 == threshold*3
            sd.record("fp_a")
        assert sd.is_hopeless("fp_a") is True

    def test_reset_clears_count(self):
        """reset() でカウントがクリアされること。"""
        sd = StuckDetector(threshold=2)
        sd.record("fp_a")
        sd.record("fp_a")
        sd.reset("fp_a")
        assert sd.get_count("fp_a") == 0

    def test_reset_on_unknown_fp_is_noop(self):
        """未記録 fp の reset() はエラーにならないこと。"""
        sd = StuckDetector(threshold=2)
        sd.reset("unknown_fp")  # should not raise


# ============================================================
# FrontierTracker
# ============================================================


class TestFrontierTracker:
    def _make_tracker_with_path(self):
        """root → mid → leaf の 3 段階パスを持つ FrontierTracker を返す。"""
        ft = FrontierTracker()
        ft.record_nav("fp_root", None)
        ft.record_nav("fp_mid", "fp_root")
        ft.record_nav("fp_leaf", "fp_mid")
        ft.record_tap("fp_root", "設定", "fp_mid")
        ft.record_tap("fp_mid", "詳細", "fp_leaf")
        return ft

    def test_record_nav_stores_parent(self):
        """record_nav() が nav_map に正しく格納されること。"""
        ft = FrontierTracker()
        ft.record_nav("fp_child", "fp_parent")
        assert ft._nav_map["fp_child"] == "fp_parent"

    def test_record_nav_root_has_none_parent(self):
        """root 画面は parent_fp=None で記録されること。"""
        ft = FrontierTracker()
        ft.record_nav("fp_root", None)
        assert ft._nav_map["fp_root"] is None

    def test_record_tap_stores_mapping(self):
        """record_tap() が tap_map に "parent_fp::text" → child_fp で格納されること。"""
        ft = FrontierTracker()
        ft.record_tap("fp_parent", "ショップ", "fp_shop")
        assert ft._tap_map["fp_parent::ショップ"] == "fp_shop"

    def test_build_path_to_returns_root_to_target(self):
        """build_path_to() が [root, ..., target] の順で返すこと。"""
        ft = self._make_tracker_with_path()
        path = ft.build_path_to("fp_leaf")
        assert path == ["fp_root", "fp_mid", "fp_leaf"]

    def test_build_path_to_root_returns_single_element(self):
        """root 自身を target にしたとき、[root] のみが返ること。"""
        ft = FrontierTracker()
        ft.record_nav("fp_root", None)
        path = ft.build_path_to("fp_root")
        assert path == ["fp_root"]

    def test_build_path_to_unknown_treated_as_root(self):
        """nav_map 未登録の fingerprint は parent=None と同等扱い (root 相当の単要素リスト)。"""
        ft = FrontierTracker()
        # 未記録 fp は nav_map に存在しないため parent=None と同じ挙動になり
        # [fp] が返る（ループが1回で終了するため）
        result = ft.build_path_to("nonexistent_fp")
        assert result == ["nonexistent_fp"]

    def test_build_path_to_detects_cycle(self):
        """nav_map にサイクルがある場合、空リストを返してループしないこと。"""
        ft = FrontierTracker()
        ft._nav_map["fp_a"] = "fp_b"
        ft._nav_map["fp_b"] = "fp_a"  # サイクル
        result = ft.build_path_to("fp_a")
        assert result == []

    def test_get_tap_for_step_returns_correct_text(self):
        """get_tap_for_step() が正しいタップテキストを返すこと。"""
        ft = self._make_tracker_with_path()
        text = ft.get_tap_for_step("fp_root", "fp_mid")
        assert text == "設定"

    def test_get_tap_for_step_returns_none_for_unknown(self):
        """tap_map に存在しないペアでは None を返すこと。"""
        ft = FrontierTracker()
        assert ft.get_tap_for_step("fp_unknown", "fp_child") is None

    def test_get_nav_recipe_returns_correct_items(self):
        """get_nav_recipe() が [(text, item_dict), ...] を返すこと。"""
        ft = self._make_tracker_with_path()

        # visited を模倣 (vkey → ScreenRecord 相当)
        record_root = MagicMock()
        record_root.fingerprint = "fp_root"
        record_root.tappable_items = [
            {"text": "設定", "center": [100, 200], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}
        ]
        record_mid = MagicMock()
        record_mid.fingerprint = "fp_mid"
        record_mid.tappable_items = [
            {"text": "詳細", "center": [150, 300], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}
        ]

        visited = {"k_root": record_root, "k_mid": record_mid}

        path = ["fp_root", "fp_mid", "fp_leaf"]
        recipe = ft.get_nav_recipe(path, visited)

        assert len(recipe) == 2
        assert recipe[0][0] == "設定"
        assert recipe[1][0] == "詳細"

    def test_get_nav_recipe_returns_empty_when_text_not_found(self):
        """tap_map に記録がない遷移では空リストを返すこと。"""
        ft = FrontierTracker()
        ft.record_nav("fp_root", None)
        ft.record_nav("fp_child", "fp_root")
        # record_tap は呼ばない → get_tap_for_step が None を返す

        record_root = MagicMock()
        record_root.fingerprint = "fp_root"
        record_root.tappable_items = []

        visited = {"k": record_root}
        path = ["fp_root", "fp_child"]
        assert ft.get_nav_recipe(path, visited) == []

    def test_get_nav_recipe_empty_path_returns_empty(self):
        """path が 1 要素（root = target）のとき空レシピを返すこと。"""
        ft = FrontierTracker()
        assert ft.get_nav_recipe(["fp_root"], {}) == []

    def test_get_root_fp_returns_none_when_empty(self):
        """nav_map が空のとき get_root_fp() は None を返すこと。"""
        ft = FrontierTracker()
        assert ft.get_root_fp() is None

    def test_get_root_fp_returns_correct_root(self):
        """parent=None の画面が get_root_fp() で返ること。"""
        ft = self._make_tracker_with_path()
        assert ft.get_root_fp() == "fp_root"


# ============================================================
# BaseDriver.is_app_alive()
# ============================================================


class TestIsAppAlive:
    def test_returns_true_when_state_is_foreground(self):
        """query_app_state() が 4 を返すとき True。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium.driver.query_app_state.return_value = 4
        sd = SimulatorDriver(mock_appium)
        assert sd.is_app_alive("com.example.app") is True

    def test_returns_false_when_state_is_not_foreground(self):
        """query_app_state() が 1 を返すとき False。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium.driver.query_app_state.return_value = 1
        sd = SimulatorDriver(mock_appium)
        assert sd.is_app_alive("com.example.app") is False

    def test_returns_true_on_exception(self):
        """query_app_state() が例外を投げたとき True（楽観的継続）。"""
        from driver_adapter import SimulatorDriver
        mock_appium = MagicMock()
        mock_appium.driver.query_app_state.side_effect = RuntimeError("not available")
        sd = SimulatorDriver(mock_appium)
        assert sd.is_app_alive("com.example.app") is True


# ============================================================
# _try_unstuck_gestures()
# ============================================================


class TestUnstuckGestures:
    def _make_crawler_with_stuck(self, count: int, threshold: int = 2):
        """stuck_detector のカウントを指定値にした ScreenCrawler を返す。"""
        crawler = _make_crawler({"anti_stuck_threshold": threshold})
        for _ in range(count):
            crawler._stuck_detector.record("fp_test")
        return crawler

    def _make_record(self, fp: str = "fp_test"):
        rec = MagicMock()
        rec.fingerprint = fp
        return rec

    def test_returns_false_below_threshold(self):
        """stuck_count < threshold のとき False を返してジェスチャーを実行しないこと。"""
        crawler = self._make_crawler_with_stuck(1, threshold=2)
        record = self._make_record()
        result = crawler._try_unstuck_gestures(record)
        assert result is False
        crawler.driver.driver.swipe.assert_not_called()

    def test_returns_false_when_hopeless(self):
        """count >= threshold*3 のとき False を返すこと（諦め）。"""
        crawler = self._make_crawler_with_stuck(6, threshold=2)
        record = self._make_record()
        result = crawler._try_unstuck_gestures(record)
        assert result is False

    def test_calls_swipe_at_threshold(self):
        """count == threshold のときスワイプが実行されること。"""
        crawler = self._make_crawler_with_stuck(2, threshold=2)
        record = self._make_record()
        crawler.driver.driver.swipe.return_value = None
        result = crawler._try_unstuck_gestures(record)
        assert result is True
        crawler.driver.driver.swipe.assert_called_once()

    def test_calls_long_press_at_threshold_times_2(self):
        """count == threshold*2 のとき長押しも実行されること。"""
        crawler = self._make_crawler_with_stuck(4, threshold=2)
        record = self._make_record()
        crawler.driver.driver.swipe.return_value = None
        crawler.driver.driver.execute_script.return_value = None
        result = crawler._try_unstuck_gestures(record)
        assert result is True
        # execute_script が "mobile: touchAndHold" で呼ばれること
        calls = crawler.driver.driver.execute_script.call_args_list
        assert any("touchAndHold" in str(c) for c in calls)

    def test_returns_false_on_swipe_exception(self):
        """swipe が例外を投げてもクラッシュせず False を返すこと。"""
        crawler = self._make_crawler_with_stuck(2, threshold=2)
        record = self._make_record()
        crawler.driver.driver.swipe.side_effect = RuntimeError("swipe error")
        result = crawler._try_unstuck_gestures(record)
        assert result is False


# ============================================================
# _navigate_to_frontier()
# ============================================================


class TestNavigateToFrontier:
    def test_returns_false_when_no_bundle_id(self):
        """IOS_BUNDLE_ID 未設定のとき False を返すこと。"""
        crawler = _make_crawler()
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": ""}):
            assert crawler._navigate_to_frontier([]) is False

    def test_calls_activate_app_when_bundle_set(self):
        """IOS_BUNDLE_ID が設定されているとき activate_app() が呼ばれること。"""
        crawler = _make_crawler()
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.example.app"}):
            result = crawler._navigate_to_frontier([])  # 空レシピ = root が target
        crawler.driver.driver.activate_app.assert_called_once_with("com.example.app")
        assert result is True

    def test_returns_true_for_empty_recipe(self):
        """レシピが空（root = target）のとき activate_app 後に True を返すこと。"""
        crawler = _make_crawler()
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.test.app"}):
            assert crawler._navigate_to_frontier([]) is True

    def test_returns_false_when_activate_app_fails(self):
        """activate_app() が例外を投げたとき False を返すこと。"""
        crawler = _make_crawler()
        crawler.driver.driver.activate_app.side_effect = RuntimeError("fail")
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.example.app"}):
            assert crawler._navigate_to_frontier([]) is False

    def test_calls_tap_for_each_step(self):
        """レシピのステップ数だけ tap_ocr_coordinate() が呼ばれること。"""
        crawler = _make_crawler()
        recipe = [
            ("設定", {"center": [100, 200], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}),
            ("一般", {"center": [100, 300], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}),
        ]
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.apple.Preferences"}):
            result = crawler._navigate_to_frontier(recipe)
        assert result is True
        assert crawler.driver.tap_ocr_coordinate.call_count == 2

    def test_returns_false_when_tap_fails(self):
        """タップが例外を投げたとき False を返すこと。"""
        crawler = _make_crawler()
        crawler.driver.tap_ocr_coordinate.side_effect = RuntimeError("tap fail")
        recipe = [
            ("設定", {"center": [100, 200], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}),
        ]
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.apple.Preferences"}):
            result = crawler._navigate_to_frontier(recipe)
        assert result is False


# ============================================================
# _smart_backtrack_loop()
# ============================================================


class TestSmartBacktrackLoop:
    def test_skips_when_no_frontier(self):
        """フロンティアが存在しない場合、activate_app が呼ばれないこと。"""
        crawler = _make_crawler({"max_depth": 3, "smart_backtrack": True})
        # _visited は空なのでフロンティアなし
        crawler._smart_backtrack_loop()
        crawler.driver.driver.activate_app.assert_not_called()

    def test_processes_frontier_with_recipe(self):
        """フロンティアがある場合、_navigate_to_frontier() が呼ばれること。"""
        from lc.crawler import CrawlerConfig, ScreenCrawler

        crawler = _make_crawler({"max_depth": 2, "smart_backtrack": True})

        # フロンティア画面 (depth=1 == max_depth-1, tappable_items あり) を注入
        frontier_rec = MagicMock()
        frontier_rec.fingerprint = "fp_frontier"
        frontier_rec.title = "フロンティア"
        frontier_rec.depth = 1
        frontier_rec.tappable_items = [
            {"text": "詳細", "center": [100, 200], "box": [[0, 0], [200, 0], [200, 50], [0, 50]]}
        ]
        crawler._visited["k_frontier"] = frontier_rec

        # FrontierTracker に nav を登録 (build_path_to が [fp_frontier] を返す)
        crawler._frontier_tracker.record_nav("fp_frontier", None)

        # _navigate_to_frontier をモックに差し替えて失敗させる（crawl_impl の無限ループ防止）
        with patch.object(crawler, "_navigate_to_frontier", return_value=False) as nav_mock:
            crawler._smart_backtrack_loop()

        nav_mock.assert_called_once()

    def test_skips_when_time_up(self):
        """タイムアップ時はフロンティアが存在しても処理しないこと。"""
        import time
        crawler = _make_crawler({"max_depth": 2, "max_duration_sec": 0})

        frontier_rec = MagicMock()
        frontier_rec.fingerprint = "fp_f"
        frontier_rec.depth = 1
        frontier_rec.tappable_items = [{"text": "x", "center": [0, 0], "box": []}]
        crawler._visited["k"] = frontier_rec

        # 時間切れ状態にする
        crawler._start_time = time.time() - 10

        with patch.object(crawler, "_navigate_to_frontier") as nav_mock:
            crawler._smart_backtrack_loop()

        nav_mock.assert_not_called()


# ============================================================
# _annotate_screenshot() — Phase 15 マーカー品質
# ============================================================


class TestAnnotateScreenshotMarkerQuality:
    def _make_crawler(self):
        from lc.crawler import CrawlerConfig, ScreenCrawler
        mock_driver = MagicMock()
        mock_driver._evidence_dir = Path("/tmp/test_phase15_annotate")
        mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)
        return ScreenCrawler(mock_driver, CrawlerConfig())

    def test_white_ring_drawn_at_radius_24(self, tmp_path):
        """DEBUG_DRAW_OPS=1 のとき、半径 24 の白リングが描画されること。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("opencv-python 未インストール")

        # 黒画像 (200x200) の中央に描画
        img = np.zeros((200, 200, 3), dtype="uint8")
        shot = tmp_path / "before.png"
        cv2.imwrite(str(shot), img)

        crawler = self._make_crawler()
        cx, cy = 100, 100
        with patch.dict(os.environ, {"DEBUG_DRAW_OPS": "1"}):
            crawler._annotate_screenshot(shot, cx, cy, "tap")

        result = cv2.imread(str(shot))
        assert result is not None

        # 半径 24 付近 (x=124, y=100) が白またはほぼ白になっているか確認
        # 白リングの線幅は 7px なので x=121~127 に白ピクセルがあるはず
        white_found = False
        for offset in range(18, 28):
            b, g, r = result[cy, cx + offset]
            if b > 200 and g > 200 and r > 200:
                white_found = True
                break
        assert white_found, "半径24付近に白ピクセルが見つかりません"

    def test_red_ring_drawn(self, tmp_path):
        """DEBUG_DRAW_OPS=1 のとき、赤リングが描画されること。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("opencv-python 未インストール")

        img = np.zeros((200, 200, 3), dtype="uint8")
        shot = tmp_path / "before.png"
        cv2.imwrite(str(shot), img)

        crawler = self._make_crawler()
        cx, cy = 100, 100
        with patch.dict(os.environ, {"DEBUG_DRAW_OPS": "1"}):
            crawler._annotate_screenshot(shot, cx, cy, "tap")

        result = cv2.imread(str(shot))
        # 中心ドットが red channel が最大のピクセルを含むこと
        b, g, r = result[cy, cx]
        assert r > 100, f"中心ドットが赤ではない: B={b} G={g} R={r}"

    def test_center_dot_is_not_filled_circle_only(self, tmp_path):
        """中心ドット内側 (r<7) に白ピクセルがあること（白インナー確認）。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("opencv-python 未インストール")

        img = np.zeros((200, 200, 3), dtype="uint8")
        shot = tmp_path / "before.png"
        cv2.imwrite(str(shot), img)

        crawler = self._make_crawler()
        cx, cy = 100, 100
        with patch.dict(os.environ, {"DEBUG_DRAW_OPS": "1"}):
            crawler._annotate_screenshot(shot, cx, cy, "tap")

        result = cv2.imread(str(shot))
        # 中心付近 (cx±3, cy±3) にある白ピクセル確認
        white_inner = False
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                b, g, r = result[cy + dy, cx + dx]
                if b > 200 and g > 200 and r > 200:
                    white_inner = True
                    break
        assert white_inner, "中心インナー白ドットが見つかりません"
