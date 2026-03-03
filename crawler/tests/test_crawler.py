"""
test_crawler.py — 3分間耐久クローリングテスト

iOS シミュレータの「設定 > 一般」からDFSで全サブ画面を探索する。
- 最大 3 分間 / 最大深さ 2（一般 → 子画面 → 孫画面）
- MySQL が利用可能なら screens / ui_elements / crawl_sessions に保存
- スクリーンショットと証拠ファイルを evidence/ に記録

【実行方法】
  xcrun simctl boot BA7E719D-8EBA-4049-996C-AC51945A7AE4
  PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723 &

  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \\
    venv/bin/python -m pytest tests/test_crawler.py -v -s
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.capabilities import simulator_config_from_env
from lc.crawler import CrawlerConfig, ScreenCrawler
from lc.driver import ios_simulator_session
from lc.ocr import find_best, run_ocr

# ============================================================
# 定数
# ============================================================

SIMULATOR_UDID  = "BA7E719D-8EBA-4049-996C-AC51945A7AE4"
BUNDLE_ID       = "com.apple.Preferences"
APPIUM_HOST     = os.environ.get("APPIUM_HOST", "127.0.0.1")
APPIUM_PORT     = int(os.environ.get("APPIUM_PORT", "4723"))
CRAWL_DURATION  = int(os.environ.get("CRAWL_DURATION_SEC", "180"))  # 3 分
CRAWL_MAX_DEPTH = int(os.environ.get("CRAWL_MAX_DEPTH", "2"))

# ============================================================
# インフラ確認ヘルパー
# ============================================================

def _is_appium_up() -> bool:
    try:
        with socket.create_connection((APPIUM_HOST, APPIUM_PORT), timeout=2):
            return True
    except OSError:
        return False


def _is_simulator_booted(udid: str = SIMULATOR_UDID) -> bool:
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "list", "devices"],
            capture_output=True, text=True, timeout=5,
        )
        return any(udid in l and "Booted" in l for l in r.stdout.splitlines())
    except Exception:
        return False


requires_appium    = pytest.mark.skipif(not _is_appium_up(),       reason="Appium 未起動")
requires_simulator = pytest.mark.skipif(not _is_simulator_booted(), reason="Simulator 未起動")


# ============================================================
# セッションスコープ fixture: 3分間クロール
# ============================================================

@pytest.fixture(scope="module")
def crawl_result():
    """
    「設定 > 一般」から 3 分間の DFS クロールを実行し、
    統計・発見画面リストを返す。
    """
    os.environ.setdefault("IOS_BUNDLE_ID",      BUNDLE_ID)
    os.environ.setdefault("IOS_SIMULATOR_UDID", SIMULATOR_UDID)
    os.environ.setdefault("IOS_USE_SIMULATOR",  "1")

    cfg      = simulator_config_from_env()
    db_host  = os.environ.get("DB_HOST", "")

    crawler_cfg = CrawlerConfig(
        max_duration_sec = CRAWL_DURATION,
        max_depth        = CRAWL_MAX_DEPTH,
        wait_after_tap   = 3.0,
        wait_after_back  = 1.5,
        min_confidence   = 0.6,
        db_host          = db_host,
    )

    with ios_simulator_session(cfg) as d:
        # --------------------------------------------------
        # STEP 0: モーダルダイアログを解除
        # --------------------------------------------------
        print("\n[SETUP] モーダルダイアログを確認・解除...")
        for _ in range(3):
            if not d.dismiss_any_modal():
                break
            d.wait(1.0)

        # --------------------------------------------------
        # STEP 1: 設定ルートに戻る
        # --------------------------------------------------
        print("[SETUP] 設定ルート画面へ移動...")
        d.navigate_back_to_root(root_keyword="設定", root_max_y=500)

        # --------------------------------------------------
        # STEP 2: 「一般」をタップして開始位置へ
        # --------------------------------------------------
        print("[SETUP] 「一般」を OCR 検索してタップ...")
        shot = d.screenshot("setup_root")
        ocr  = run_ocr(shot)
        general = find_best(ocr, "一般", min_confidence=0.6)

        if general:
            cx, cy = general["center"]
            print(f"  「一般」検出: pixel=({cx},{cy}), conf={general['confidence']:.3f}")
            d.tap_ocr_coordinate(cx, cy, "setup_tap_general")
            d.wait(3.0)
        else:
            pytest.skip("「一般」が初期画面で検出されませんでした")

        # --------------------------------------------------
        # STEP 3: クローラー実行
        # --------------------------------------------------
        crawler = ScreenCrawler(d, crawler_cfg)
        print(
            f"\n[CRAWL] 開始: 最大 {CRAWL_DURATION}秒 / 最大深さ {CRAWL_MAX_DEPTH}"
        )
        print("=" * 60)

        stats = crawler.crawl(depth=0, parent_fp=None)

        # サマリーを表示
        print(crawler.summary())
        print(
            f"\n[CRAWL] 完了: {stats.screens_found}画面発見"
            f" / {stats.taps_total}回タップ"
            f" / {stats.elapsed_sec:.1f}秒"
        )

        yield {
            "stats":    stats,
            "screens":  crawler.get_visited_screens(),
            "crawler":  crawler,
        }


# ============================================================
# テストクラス
# ============================================================

@requires_appium
@requires_simulator
class TestCrawlStats:
    """クロール統計の基本検証。"""

    def test_found_multiple_screens(self, crawl_result):
        """2 画面以上を発見していること（「一般」 + 少なくとも 1 子画面）"""
        found = crawl_result["stats"].screens_found
        assert found >= 2, (
            f"発見画面数が少なすぎます: {found}\n"
            f"  期待: >= 2 (「一般」サブ画面を少なくとも 1 つ探索できるはず)"
        )
        print(f"\n  発見画面数: {found}")

    def test_tapped_at_least_once(self, crawl_result):
        """少なくとも 1 回タップが実行されたこと"""
        taps = crawl_result["stats"].taps_total
        assert taps >= 1, f"タップが 0 回です: taps={taps}"
        print(f"\n  タップ総数: {taps}")

    def test_elapsed_within_limit(self, crawl_result):
        """実行時間が制限内に収まっていること（バッファ 30 秒）"""
        elapsed = crawl_result["stats"].elapsed_sec
        limit   = CRAWL_DURATION + 30
        assert elapsed <= limit, (
            f"実行時間が制限 {limit}秒 を超えました: {elapsed:.1f}秒"
        )
        print(f"\n  実行時間: {elapsed:.1f}秒 (制限: {CRAWL_DURATION}秒)")

    def test_no_duplicate_screens(self, crawl_result):
        """発見画面に重複（同じ指紋）がないこと"""
        screens = crawl_result["screens"]
        fps = [s.fingerprint for s in screens]
        assert len(fps) == len(set(fps)), (
            f"重複する指紋が含まれています: {len(fps)} 件中 {len(set(fps))} 件がユニーク"
        )


@requires_appium
@requires_simulator
class TestCrawlScreenContent:
    """発見した画面の内容検証。"""

    def test_first_screen_is_general(self, crawl_result):
        """最初に発見した画面が「一般」であること"""
        screens = crawl_result["screens"]
        assert screens, "画面が 1 件も発見されていません"
        first = screens[0]
        assert "一般" in first.title, (
            f"最初の画面タイトルが「一般」ではありません: {first.title!r}"
        )
        print(f"\n  最初の画面: {first.title!r} (深さ={first.depth})")

    def test_general_subscreen_items_explored(self, crawl_result):
        """「一般」の子画面が少なくとも 1 件探索されていること"""
        screens = crawl_result["screens"]
        depth1_screens = [s for s in screens if s.depth >= 1]
        assert depth1_screens, (
            "深さ 1 以上の画面が探索されていません。\n"
            f"  発見画面一覧: {[(s.title, s.depth) for s in screens]}"
        )
        print(f"\n  深さ1以上の画面: {[(s.title, s.depth) for s in depth1_screens]}")

    def test_each_screen_has_screenshot(self, crawl_result):
        """各発見画面にスクリーンショットファイルが存在すること"""
        for rec in crawl_result["screens"]:
            assert rec.screenshot_path.exists(), (
                f"スクリーンショットが見つかりません: {rec.screenshot_path}"
            )

    def test_each_screen_has_ocr_text(self, crawl_result):
        """各発見画面の OCR 結果が 1 件以上あること"""
        for rec in crawl_result["screens"]:
            assert len(rec.ocr_results) >= 1, (
                f"OCR 結果が空です: {rec.title!r}"
            )

    def test_print_all_discovered_screens(self, crawl_result):
        """発見した全画面をログ出力する（常に PASS）"""
        print("\n" + "=" * 62)
        print("  発見した画面一覧")
        print("=" * 62)
        for i, rec in enumerate(crawl_result["screens"], 1):
            indent = "  " * rec.depth
            print(
                f"  [{i:02d}] {indent}深さ={rec.depth}"
                f"  title={rec.title!r}"
                f"  items={len(rec.tappable_items)}件"
                f"  fp={rec.fingerprint[:8]}…"
            )
        print("=" * 62)
        assert True


@requires_appium
@requires_simulator
class TestCrawlTappableItems:
    """タップ候補検出ロジックの検証。"""

    def test_first_screen_has_tappable_items(self, crawl_result):
        """「一般」画面にタップ候補が複数あること"""
        screens = crawl_result["screens"]
        assert screens
        first = screens[0]
        items = first.tappable_items
        assert len(items) >= 3, (
            f"「一般」のタップ候補が少なすぎます: {len(items)} 件\n"
            f"  検出: {[r['text'] for r in items]}"
        )
        print(f"\n  「一般」のタップ候補 ({len(items)}件):")
        for item in items:
            cx, cy = item["center"]
            print(f"    {item['text']!r}  pixel=({cx},{cy})  conf={item['confidence']:.3f}")

    def test_tappable_items_have_valid_coordinates(self, crawl_result):
        """全タップ候補の座標が画像サイズ内にあること"""
        IMG_W, IMG_H = 1178, 2556
        for rec in crawl_result["screens"]:
            for item in rec.tappable_items:
                cx, cy = item["center"]
                assert 0 <= cx <= IMG_W, f"cx={cx} が範囲外: {item['text']!r}"
                assert 0 <= cy <= IMG_H, f"cy={cy} が範囲外: {item['text']!r}"


@requires_appium
@requires_simulator
class TestCrawlEvidenceFiles:
    """CLAUDE.md §9 証拠ファイルの存在確認。"""

    def test_evidence_directory_created(self, crawl_result):
        """evidence/ ディレクトリが作成されていること"""
        screens = crawl_result["screens"]
        assert screens
        evidence_root = Path(__file__).parent.parent / "evidence"
        assert evidence_root.exists(), f"evidence/ がありません: {evidence_root}"
        # セッションサブディレクトリの存在確認
        session_dirs = list(evidence_root.glob("*"))
        assert session_dirs, "evidence/ にセッションディレクトリがありません"

    def test_crawl_screenshots_saved(self, crawl_result):
        """クロール中に撮影したスクリーンショットが全て保存されていること"""
        for rec in crawl_result["screens"]:
            assert rec.screenshot_path.exists(), (
                f"スクリーンショット未保存: {rec.screenshot_path}"
            )
        print(f"\n  保存済みスクリーンショット: {len(crawl_result['screens'])} 枚")
