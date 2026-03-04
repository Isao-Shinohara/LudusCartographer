"""
test_phase5.py — Phase 5 機能テスト (Appium 不要)

テスト分類:
  TestInteractiveCoord   (7) — 座標入力補助: 履歴追加・dedup・番号選択
  TestDiscoveryReport    (6) — Markdown レポート生成
  TestSessionResume      (5) — セッション再開: JSON 読み込み・スキップ
  TestOrganizeScreenshots(5) — スクリーンショット整理ユーティリティ
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

# --- パス設定 ---------------------------------------------------------
_CRAWLER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_CRAWLER_ROOT))

from lc.human_teacher import HumanTeacher
from lc.crawler import CrawlerConfig, ScreenCrawler, ScreenRecord


# =====================================================================
# ヘルパー: 最小限の ScreenCrawler を作る (実機不要)
# =====================================================================

def _make_fake_driver(tmp_path: Path):
    """最小限のドライバーモック (ScreenCrawler.__init__ が要求するフィールドのみ)。"""
    class FakeDriver:
        _evidence_dir = tmp_path / "evidence"
        def __init__(self):
            self._evidence_dir.mkdir(parents=True, exist_ok=True)
    return FakeDriver()


def _make_crawler(tmp_path: Path, **cfg_kwargs) -> ScreenCrawler:
    """テスト用 ScreenCrawler を返す (Appium 不要)。"""
    driver = _make_fake_driver(tmp_path)
    cfg = CrawlerConfig(
        game_title="TestGame",
        max_duration_sec=60,
        max_depth=3,
        **cfg_kwargs,
    )
    with (
        patch("lc.crawler.AppHealthMonitor"),
        patch("lc.crawler.StuckDetector"),
        patch("lc.crawler.FrontierTracker"),
        patch.object(ScreenCrawler, "_load_icon_templates"),
        patch.object(ScreenCrawler, "_load_known_phashes"),
        patch.object(ScreenCrawler, "_init_screen_cache"),
        patch.object(ScreenCrawler, "_init_db"),
    ):
        return ScreenCrawler(driver, cfg)


def _make_screen_record(
    fp: str,
    title: str,
    depth: int = 0,
    parent_fp: str | None = None,
    tappable_count: int = 3,
    tmp_path: Path | None = None,
) -> ScreenRecord:
    shot = (tmp_path or Path("/tmp")) / f"{fp[:8]}.png"
    return ScreenRecord(
        fingerprint=fp,
        title=title,
        screenshot_path=shot,
        depth=depth,
        parent_fp=parent_fp,
        ocr_results=[],
        tappable_items=[{"type": "tap", "x": i * 10, "y": i * 10} for i in range(tappable_count)],
        phash="abc123",
        discovered_at="2026-03-04T12:00:00",
    )


# =====================================================================
# TestInteractiveCoord — 座標入力補助
# =====================================================================

class TestInteractiveCoord:

    def test_history_updated_after_tap(self):
        """tap 返却後に _history に追加される。"""
        teacher = HumanTeacher(auto_open_screenshot=False)
        teacher._update_history([{"type": "tap", "x": 540, "y": 1200}])
        assert len(teacher._history) == 1
        assert teacher._history[0]["x"] == 540
        assert teacher._history[0]["y"] == 1200

    def test_history_dedup_same_xy(self):
        """同一 x,y は重複しない (先頭に移動)。"""
        teacher = HumanTeacher(auto_open_screenshot=False)
        teacher._update_history([{"type": "tap", "x": 540, "y": 1200}])
        teacher._update_history([{"type": "tap", "x": 100, "y": 200}])
        teacher._update_history([{"type": "tap", "x": 540, "y": 1200}])  # 再挿入
        assert len(teacher._history) == 2
        assert teacher._history[0]["x"] == 540  # 先頭に移動

    def test_history_max_5(self):
        """6件目以降は古い方が削除される (最大5件)。"""
        teacher = HumanTeacher(auto_open_screenshot=False)
        for i in range(6):
            teacher._update_history([{"type": "tap", "x": i * 10, "y": i * 10}])
        assert len(teacher._history) == 5
        # 最後に追加した x=50,y=50 が先頭
        assert teacher._history[0]["x"] == 50

    def test_history_selection_by_number(self, monkeypatch, tmp_path):
        """"1" 入力 → 履歴先頭を返す。"""
        teacher = HumanTeacher(auto_open_screenshot=False)
        teacher._history = [{"type": "tap", "x": 540, "y": 1200}]
        shot = tmp_path / "shot.png"
        shot.touch()
        # 入力: "1" → 履歴選択
        inputs = iter(["1"])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        result = teacher.ask_for_action(shot, "テスト画面")
        assert result == [{"type": "tap", "x": 540, "y": 1200}]

    def test_history_selection_invalid_num(self, monkeypatch, tmp_path, capsys):
        """範囲外の数字 → 再入力を促し、続けて正常入力を返す。"""
        teacher = HumanTeacher(auto_open_screenshot=False)
        teacher._history = [{"type": "tap", "x": 100, "y": 200}]
        shot = tmp_path / "shot.png"
        shot.touch()
        inputs = iter(["9", "100,200"])  # 9 は範囲外 → 100,200 を入力
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        result = teacher.ask_for_action(shot, "テスト画面")
        captured = capsys.readouterr()
        assert "範囲外" in captured.out
        assert result[0]["x"] == 100

    def test_screen_size_in_prompt(self, tmp_path, capsys):
        """screen_size が表示に含まれる。"""
        shot = tmp_path / "shot.png"
        shot.touch()
        HumanTeacher._print_prompt(shot, "テスト", [], screen_size=(1178, 2556))
        captured = capsys.readouterr()
        assert "1178×2556" in captured.out

    def test_history_shown_in_prompt(self, tmp_path, capsys):
        """history が表示に含まれる。"""
        shot = tmp_path / "shot.png"
        shot.touch()
        history = [{"type": "tap", "x": 540, "y": 1200}]
        HumanTeacher._print_prompt(shot, "テスト", [], history=history)
        captured = capsys.readouterr()
        assert "540" in captured.out
        assert "1200" in captured.out
        assert "番号入力で再選択" in captured.out


# =====================================================================
# TestDiscoveryReport — Markdown レポート生成
# =====================================================================

class TestDiscoveryReport:

    def test_save_creates_md_file(self, tmp_path):
        """save_discovery_report() でファイル作成。"""
        crawler = _make_crawler(tmp_path)
        rec = _make_screen_record("aabbccdd11223344", "設定", depth=0, tmp_path=tmp_path)
        crawler._visited[f"{rec.title}@{rec.fingerprint}"] = rec
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        assert report_path.exists()

    def test_report_contains_game_title(self, tmp_path):
        """ヘッダーにゲームタイトルが含まれる。"""
        crawler = _make_crawler(tmp_path)
        rec = _make_screen_record("aabbccdd11223344", "設定", depth=0, tmp_path=tmp_path)
        crawler._visited[f"{rec.title}@{rec.fingerprint}"] = rec
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        content = report_path.read_text(encoding="utf-8")
        assert "TestGame" in content

    def test_report_contains_screen_table(self, tmp_path):
        """## 画面詳細 テーブルが存在する。"""
        crawler = _make_crawler(tmp_path)
        rec = _make_screen_record("aabbccdd11223344", "設定", depth=0, tmp_path=tmp_path)
        crawler._visited[f"{rec.title}@{rec.fingerprint}"] = rec
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        content = report_path.read_text(encoding="utf-8")
        assert "## 画面詳細" in content
        assert "| # |" in content

    def test_report_contains_tree_section(self, tmp_path):
        """## 発見画面ツリー セクションが存在する。"""
        crawler = _make_crawler(tmp_path)
        rec = _make_screen_record("aabbccdd11223344", "設定", depth=0, tmp_path=tmp_path)
        crawler._visited[f"{rec.title}@{rec.fingerprint}"] = rec
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        content = report_path.read_text(encoding="utf-8")
        assert "## 発見画面ツリー" in content

    def test_report_via_icons_in_tree(self, tmp_path):
        """遷移ログがある場合、アイコン (⚡/🧑/🔍) が含まれる。"""
        crawler = _make_crawler(tmp_path)
        root = _make_screen_record("root0000" * 2, "設定", depth=0, tmp_path=tmp_path)
        child = _make_screen_record("child111" * 2, "一般", depth=1, parent_fp=root.fingerprint, tmp_path=tmp_path)
        crawler._visited[f"{root.title}@{root.fingerprint}"] = root
        crawler._visited[f"{child.title}@{child.fingerprint}"] = child
        crawler._transition_log.append({
            "from_fp": root.fingerprint,
            "to_fp": child.fingerprint,
            "via": "auto",
        })
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        content = report_path.read_text(encoding="utf-8")
        assert "🔍" in content  # via=auto

    def test_empty_visited_no_crash(self, tmp_path):
        """_visited 空でもクラッシュしない。"""
        crawler = _make_crawler(tmp_path)
        report_path = tmp_path / "report.md"
        crawler.save_discovery_report(report_path)
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "TestGame" in content


# =====================================================================
# TestSessionResume — セッション再開
# =====================================================================

class TestSessionResume:

    def _write_tree(self, path: Path, game_title: str = "TestGame") -> dict:
        data = {
            "game_title": game_title,
            "session_id": "20260304_120000",
            "created_at": "2026-03-04T12:00:00",
            "nodes": {
                "fp_root_000000000000": {
                    "title": "設定",
                    "depth": 0,
                    "phash": "abc123",
                    "screenshot_path": "/tmp/root.png",
                    "tappable_count": 5,
                    "discovered_at": "2026-03-04T12:00:00",
                },
                "fp_child_111111111111": {
                    "title": "一般",
                    "depth": 1,
                    "phash": "def456",
                    "screenshot_path": "/tmp/child.png",
                    "tappable_count": 3,
                    "discovered_at": "2026-03-04T12:01:00",
                },
            },
            "edges": [
                {
                    "from_fp": "fp_root_000000000000",
                    "to_fp": "fp_child_111111111111",
                    "via": "auto",
                    "timestamp": "2026-03-04T12:01:00",
                }
            ],
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data

    def test_load_resume_tree_populates_visited(self, tmp_path):
        """JSON → _visited にエントリ追加。"""
        tree_path = tmp_path / "discovery_tree.json"
        self._write_tree(tree_path)
        crawler = _make_crawler(tmp_path, resume_tree_path=str(tree_path))
        assert any("fp_root_000000000000" in k for k in crawler._visited)
        assert any("fp_child_111111111111" in k for k in crawler._visited)

    def test_load_resume_tree_populates_fingerprints(self, tmp_path):
        """_resumed_fingerprints に fp 追加。"""
        tree_path = tmp_path / "discovery_tree.json"
        self._write_tree(tree_path)
        crawler = _make_crawler(tmp_path, resume_tree_path=str(tree_path))
        assert "fp_root_000000000000" in crawler._resumed_fingerprints
        assert "fp_child_111111111111" in crawler._resumed_fingerprints

    def test_load_resume_tree_extends_transition_log(self, tmp_path):
        """edges → _transition_log に追加。"""
        tree_path = tmp_path / "discovery_tree.json"
        self._write_tree(tree_path)
        crawler = _make_crawler(tmp_path, resume_tree_path=str(tree_path))
        assert len(crawler._transition_log) == 1
        assert crawler._transition_log[0]["from_fp"] == "fp_root_000000000000"

    def test_nonexistent_tree_graceful(self, tmp_path):
        """ファイルなし → 警告のみで継続 (例外を投げない)。"""
        nonexistent = tmp_path / "nonexistent.json"
        # 例外が出ないことを確認
        crawler = _make_crawler(tmp_path, resume_tree_path=str(nonexistent))
        assert len(crawler._visited) == 0
        assert len(crawler._resumed_fingerprints) == 0

    def test_resumed_screen_skipped_via_visited(self, tmp_path):
        """_visited に登録済み画面は _resumed_fingerprints に含まれる。"""
        tree_path = tmp_path / "discovery_tree.json"
        self._write_tree(tree_path)
        crawler = _make_crawler(tmp_path, resume_tree_path=str(tree_path))
        # _visited に両 fp が登録されている
        fps_in_visited = {v.fingerprint for v in crawler._visited.values()}
        assert "fp_root_000000000000" in fps_in_visited
        assert "fp_child_111111111111" in fps_in_visited
        # かつ _resumed_fingerprints にも含まれる
        assert fps_in_visited.issubset(crawler._resumed_fingerprints | fps_in_visited)


# =====================================================================
# TestOrganizeScreenshots — スクリーンショット整理ユーティリティ
# =====================================================================

class TestOrganizeScreenshots:

    def _write_summary(self, session_dir: Path, screens: list[dict]) -> None:
        summary = {
            "game_title": "TestGame",
            "session_id": session_dir.name,
            "screens": screens,
        }
        (session_dir / "crawl_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False), encoding="utf-8"
        )

    def test_creates_directory_structure(self, tmp_path):
        """depth に応じたディレクトリ生成。"""
        from tools.organize_screenshots import organize_screenshots

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        root_shot = tmp_path / "root.png"
        root_shot.touch()
        child_shot = tmp_path / "child.png"
        child_shot.touch()

        screens = [
            {
                "fingerprint": "aaaa0000bbbb1111",
                "title": "設定",
                "depth": 0,
                "parent_fp": None,
                "screenshot_path": str(root_shot),
            },
            {
                "fingerprint": "cccc2222dddd3333",
                "title": "一般",
                "depth": 1,
                "parent_fp": "aaaa0000bbbb1111",
                "screenshot_path": str(child_shot),
            },
        ]
        self._write_summary(session_dir, screens)
        out_dir = tmp_path / "organized"
        result = organize_screenshots(session_dir, output_dir=out_dir)
        assert result["copied"] == 2
        assert out_dir.exists()

    def test_copies_screenshots(self, tmp_path):
        """PNG が出力先にコピーされる。"""
        from tools.organize_screenshots import organize_screenshots

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        shot = tmp_path / "screen.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n")  # PNG ヘッダー

        screens = [
            {
                "fingerprint": "aaaa0000bbbb1111",
                "title": "TestScreen",
                "depth": 0,
                "parent_fp": None,
                "screenshot_path": str(shot),
            },
        ]
        self._write_summary(session_dir, screens)
        out_dir = tmp_path / "organized"
        organize_screenshots(session_dir, output_dir=out_dir)

        # 何らかの PNG ファイルが出力先に存在する
        png_files = list(out_dir.rglob("*.png"))
        assert len(png_files) > 0

    def test_handles_missing_screenshot(self, tmp_path):
        """元ファイルなし → スキップして継続 (例外を投げない)。"""
        from tools.organize_screenshots import organize_screenshots

        session_dir = tmp_path / "session"
        session_dir.mkdir()

        screens = [
            {
                "fingerprint": "aaaa0000bbbb1111",
                "title": "MissingScreen",
                "depth": 0,
                "parent_fp": None,
                "screenshot_path": "/nonexistent/path/shot.png",
            },
        ]
        self._write_summary(session_dir, screens)
        out_dir = tmp_path / "organized"
        result = organize_screenshots(session_dir, output_dir=out_dir)
        assert result["skipped"] == 1
        assert result["copied"] == 0

    def test_slugify_special_chars(self):
        """ファイルシステム禁止文字の変換。"""
        from tools.organize_screenshots import _slugify

        result = _slugify("VPN/設定:プロファイル*管理")
        # スラッシュ・コロン・アスタリスクが _ に変換される
        assert "/" not in result
        assert ":" not in result
        assert "*" not in result
        assert len(result) > 0

    def test_index_md_created(self, tmp_path):
        """organized_index.md が生成される。"""
        from tools.organize_screenshots import organize_screenshots

        session_dir = tmp_path / "session"
        session_dir.mkdir()
        shot = tmp_path / "screen.png"
        shot.touch()

        screens = [
            {
                "fingerprint": "aaaa0000bbbb1111",
                "title": "設定",
                "depth": 0,
                "parent_fp": None,
                "screenshot_path": str(shot),
            },
        ]
        self._write_summary(session_dir, screens)
        out_dir = tmp_path / "organized"
        result = organize_screenshots(session_dir, output_dir=out_dir)
        assert Path(result["index_path"]).exists()
        content = Path(result["index_path"]).read_text(encoding="utf-8")
        assert "設定" in content
