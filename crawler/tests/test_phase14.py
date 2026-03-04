"""
test_phase14.py — Phase 14「1ゲーム1プロジェクト」増分探索テスト

Appium 不要。すべてのテストはインメモリ SQLite / モックで動作する。

テスト対象:
  tools/import_to_sqlite.py
    - lc_projects テーブルの存在確認
    - upsert_project() の作成・冪等性
    - get_project_phashes() の正常取得
    - import_session() が project_id を設定すること
    - migrate() が project_id カラムを追加すること
  lc/crawler.py
    - _load_known_phashes() が SQLite から正しくロードすること
    - グローバル pHash 重複時にスキップされること
    - _annotate_screenshot() が DEBUG_DRAW_OPS=1 で赤円を描くこと
    - _escape_dead_end() が activate_app を呼ぶこと
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.import_to_sqlite import (
    SCHEMA,
    get_project_phashes,
    import_session,
    migrate,
    upsert_project,
)


# ============================================================
# フィクスチャ
# ============================================================


@pytest.fixture
def conn():
    """インメモリ SQLite 接続（SCHEMA 適用済み）。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _make_summary(tmp_path: Path, session_id: str, phash: str = "abcd1234abcd1234") -> Path:
    """テスト用 crawl_summary.json を作成して Path を返す。"""
    data = {
        "session_id": session_id,
        "game_title": "TestGame",
        "device_mode": "SIMULATOR",
        "screens": [
            {
                "fingerprint": f"fp_{session_id}",
                "title": "ホーム",
                "depth": 0,
                "parent_fp": None,
                "tappable_items": [{"text": "設定", "confidence": 0.95}],
                "phash": phash,
                "screenshot_path": "/tmp/shot.png",
                "discovered_at": "2026-03-04T12:00:00",
            }
        ],
        "stats": {"screens_found": 1, "screens_skipped": 0, "taps_total": 1, "elapsed_sec": 10.0},
    }
    path = tmp_path / session_id / "crawl_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ============================================================
# lc_projects テーブル
# ============================================================


class TestLcProjectsSchema:
    def test_lc_projects_table_exists(self, conn):
        """SCHEMA に lc_projects テーブルが存在すること。"""
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "lc_projects" in tables

    def test_lc_projects_has_game_title(self, conn):
        """lc_projects に game_title カラムが存在すること。"""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lc_projects)")]
        assert "game_title" in cols

    def test_lc_sessions_has_project_id(self, conn):
        """lc_sessions に project_id カラムが存在すること。"""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lc_sessions)")]
        assert "project_id" in cols


# ============================================================
# upsert_project()
# ============================================================


class TestUpsertProject:
    def test_creates_new_project(self, conn):
        """新規 game_title に対してプロジェクトが作成されること。"""
        pid = upsert_project(conn, "MyGame")
        assert isinstance(pid, int) and pid > 0

    def test_returns_same_id_on_duplicate(self, conn):
        """同一 game_title で 2 回呼んでも同じ id が返ること（冪等性）。"""
        pid1 = upsert_project(conn, "MyGame")
        pid2 = upsert_project(conn, "MyGame")
        assert pid1 == pid2

    def test_different_titles_get_different_ids(self, conn):
        """異なる game_title は別の project_id を持つこと。"""
        pid_a = upsert_project(conn, "GameA")
        pid_b = upsert_project(conn, "GameB")
        assert pid_a != pid_b

    def test_project_is_queryable(self, conn):
        """upsert 後に lc_projects から取得できること。"""
        upsert_project(conn, "QueryTest")
        row = conn.execute(
            "SELECT game_title FROM lc_projects WHERE game_title=?", ("QueryTest",)
        ).fetchone()
        assert row is not None
        assert row["game_title"] == "QueryTest"


# ============================================================
# get_project_phashes()
# ============================================================


class TestGetProjectPhashes:
    def test_returns_empty_when_no_sessions(self, conn):
        """セッションがない場合は空セットを返すこと。"""
        result = get_project_phashes(conn, "NoGame")
        assert result == set()

    def test_returns_phash_from_imported_session(self, conn, tmp_path):
        """import_session で登録した phash が取得できること。"""
        path = _make_summary(tmp_path, "sess_phash", phash="deadbeef12345678")
        import_session(conn, path)
        result = get_project_phashes(conn, "TestGame")
        assert "deadbeef12345678" in result

    def test_excludes_null_phash(self, conn, tmp_path):
        """phash が null の画面はセットに含まれないこと。"""
        # null phash を持つ summary を作成
        data = {
            "session_id": "sess_null_ph",
            "game_title": "TestGame",
            "device_mode": "SIMULATOR",
            "screens": [
                {
                    "fingerprint": "fp_null_ph",
                    "title": "ホーム",
                    "depth": 0,
                    "parent_fp": None,
                    "tappable_items": [],
                    "phash": None,
                    "screenshot_path": "/tmp/shot.png",
                    "discovered_at": "2026-03-04T12:00:00",
                }
            ],
            "stats": {"screens_found": 1, "screens_skipped": 0, "taps_total": 0, "elapsed_sec": 0},
        }
        path = tmp_path / "sess_null_ph" / "crawl_summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        import_session(conn, path)
        result = get_project_phashes(conn, "TestGame")
        # None は含まれない
        assert None not in result

    def test_excludes_other_game_phashes(self, conn, tmp_path):
        """別ゲームの phash は取得されないこと。"""
        # GameA の session
        data_a = {
            "session_id": "sess_game_a",
            "game_title": "GameA",
            "device_mode": "SIMULATOR",
            "screens": [{
                "fingerprint": "fp_a",
                "title": "A",
                "depth": 0,
                "parent_fp": None,
                "tappable_items": [],
                "phash": "aaaa111122223333",
                "screenshot_path": "/tmp/a.png",
                "discovered_at": "2026-03-04T12:00:00",
            }],
            "stats": {"screens_found": 1, "screens_skipped": 0, "taps_total": 0, "elapsed_sec": 0},
        }
        path_a = tmp_path / "sess_game_a" / "crawl_summary.json"
        path_a.parent.mkdir(parents=True, exist_ok=True)
        path_a.write_text(json.dumps(data_a), encoding="utf-8")
        import_session(conn, path_a)

        result = get_project_phashes(conn, "GameB")
        assert "aaaa111122223333" not in result


# ============================================================
# import_session() — project_id の設定
# ============================================================


class TestImportSessionProjectId:
    def test_sets_project_id(self, conn, tmp_path):
        """import_session() が lc_sessions.project_id を設定すること。"""
        path = _make_summary(tmp_path, "sess_pid")
        import_session(conn, path)
        row = conn.execute(
            "SELECT project_id FROM lc_sessions WHERE session_id=?", ("sess_pid",)
        ).fetchone()
        assert row is not None
        assert row["project_id"] is not None

    def test_same_game_same_project_id(self, conn, tmp_path):
        """同じ game_title のセッションは同じ project_id を持つこと。"""
        path1 = _make_summary(tmp_path, "sess_same_1")
        path2 = _make_summary(tmp_path, "sess_same_2")
        import_session(conn, path1)
        import_session(conn, path2)
        rows = conn.execute(
            "SELECT project_id FROM lc_sessions WHERE session_id IN (?,?)",
            ("sess_same_1", "sess_same_2"),
        ).fetchall()
        pids = {r["project_id"] for r in rows}
        assert len(pids) == 1  # 同一 project_id


# ============================================================
# migrate() — project_id カラム追加
# ============================================================


class TestMigrateProjectId:
    def _make_old_db(self) -> sqlite3.Connection:
        """project_id がない旧スキーマの DB を作成する。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE lc_sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT UNIQUE NOT NULL,
                screens_found INTEGER DEFAULT 0,
                started_at    TEXT,
                status        TEXT DEFAULT 'completed',
                game_title    TEXT DEFAULT 'Unknown Game',
                device_mode   TEXT DEFAULT 'SIMULATOR'
            );
        """)
        return c

    def test_adds_project_id_column(self):
        """migrate() が project_id カラムを追加すること。"""
        c = self._make_old_db()
        migrate(c)
        cols = [r[1] for r in c.execute("PRAGMA table_info(lc_sessions)")]
        assert "project_id" in cols
        c.close()

    def test_adds_lc_projects_table(self):
        """migrate() が lc_projects テーブルを作成すること。"""
        c = self._make_old_db()
        migrate(c)
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "lc_projects" in tables
        c.close()

    def test_migrate_idempotent(self, conn):
        """既存カラムがある DB で migrate() を 2 回実行してもエラーにならないこと。"""
        migrate(conn)
        migrate(conn)


# ============================================================
# ScreenCrawler — _load_known_phashes()
# ============================================================


class TestLoadKnownPhashes:
    """SQLite DB から既知 pHash をロードするテスト。"""

    def _make_crawler(self, sqlite_db_path: str) -> "object":
        """最小限のモックドライバーで ScreenCrawler を作成する。"""
        from lc.crawler import CrawlerConfig, ScreenCrawler

        mock_driver = MagicMock()
        mock_driver._evidence_dir = Path("/tmp/test_evidence")
        mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)

        cfg = CrawlerConfig(
            game_title="TestGame",
            sqlite_db_path=sqlite_db_path,
        )
        return ScreenCrawler(mock_driver, cfg)

    def test_loads_phashes_from_sqlite(self, tmp_path):
        """SQLite に phash が存在するとき _known_phashes に格納されること。"""
        # SQLite を用意してデータを投入
        db_path = tmp_path / "ludus.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA)
        summary = _make_summary(tmp_path, "known_sess", phash="cafebabe00001111")
        import_session(conn, summary)
        conn.close()

        crawler = self._make_crawler(str(db_path))
        assert "cafebabe00001111" in crawler._known_phashes

    def test_empty_when_db_not_exists(self, tmp_path):
        """SQLite ファイルが存在しない場合は _known_phashes が空になること。"""
        crawler = self._make_crawler(str(tmp_path / "nonexistent.db"))
        assert crawler._known_phashes == set()

    def test_empty_when_no_matching_game(self, tmp_path):
        """同一 DB でも game_title が異なる場合は _known_phashes が空になること。"""
        db_path = tmp_path / "ludus.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(SCHEMA)
        summary = _make_summary(tmp_path, "other_sess", phash="0000000011111111")
        # "TestGame" で登録されているが crawler は "OtherGame" で検索
        import_session(conn, summary)
        conn.close()

        from lc.crawler import CrawlerConfig, ScreenCrawler
        mock_driver = MagicMock()
        mock_driver._evidence_dir = Path("/tmp/test_evidence2")
        mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)
        cfg = CrawlerConfig(game_title="OtherGame", sqlite_db_path=str(db_path))
        crawler = ScreenCrawler(mock_driver, cfg)
        assert "0000000011111111" not in crawler._known_phashes


# ============================================================
# ScreenCrawler — _annotate_screenshot()
# ============================================================


class TestAnnotateScreenshot:
    def _make_crawler(self) -> "object":
        from lc.crawler import CrawlerConfig, ScreenCrawler
        mock_driver = MagicMock()
        mock_driver._evidence_dir = Path("/tmp/test_annotate")
        mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)
        return ScreenCrawler(mock_driver, CrawlerConfig())

    def test_no_effect_without_env(self, tmp_path):
        """DEBUG_DRAW_OPS が未設定のとき、ファイルは変更されないこと。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("opencv-python 未インストール")

        img = (255 * np.ones((200, 200, 3), dtype="uint8"))
        shot = tmp_path / "before.png"
        cv2.imwrite(str(shot), img)
        mtime_before = shot.stat().st_mtime

        crawler = self._make_crawler()
        crawler._annotate_screenshot(shot, 100, 100, "tap")
        # ファイルが変更されていないこと（mtime 同一）
        assert shot.stat().st_mtime == mtime_before

    def test_draws_when_env_set(self, tmp_path):
        """DEBUG_DRAW_OPS=1 のとき、スクリーンショットが変更されること。"""
        try:
            import cv2
            import numpy as np
        except ImportError:
            pytest.skip("opencv-python 未インストール")

        img_orig = (255 * np.ones((200, 200, 3), dtype="uint8"))
        shot = tmp_path / "before.png"
        cv2.imwrite(str(shot), img_orig)

        crawler = self._make_crawler()
        with patch.dict(os.environ, {"DEBUG_DRAW_OPS": "1"}):
            crawler._annotate_screenshot(shot, 100, 100, "tap")

        result = cv2.imread(str(shot))
        assert result is not None
        # 中心付近のピクセルが赤に変化していること (R > B)
        cy, cx = 100, 100
        b, g, r = result[cy, cx]
        assert r > b  # 赤円が描画された

    def test_skips_on_missing_file(self, tmp_path):
        """存在しないファイルパスが渡されてもエラーにならないこと。"""
        crawler = self._make_crawler()
        with patch.dict(os.environ, {"DEBUG_DRAW_OPS": "1"}):
            crawler._annotate_screenshot(tmp_path / "nonexistent.png", 10, 10, "tap")


# ============================================================
# ScreenCrawler — _escape_dead_end()
# ============================================================


class TestEscapeDeadEnd:
    def _make_crawler(self) -> "object":
        from lc.crawler import CrawlerConfig, ScreenCrawler
        mock_driver = MagicMock()
        mock_driver._evidence_dir = Path("/tmp/test_escape")
        mock_driver._evidence_dir.mkdir(parents=True, exist_ok=True)
        return ScreenCrawler(mock_driver, CrawlerConfig())

    def test_calls_activate_app_when_bundle_set(self):
        """IOS_BUNDLE_ID が設定されているとき activate_app() が呼ばれること。"""
        crawler = self._make_crawler()
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": "com.example.test"}):
            result = crawler._escape_dead_end()
        crawler.driver.driver.activate_app.assert_called_once_with("com.example.test")
        assert result is True

    def test_falls_back_to_home_button(self):
        """activate_app が失敗したとき、ホームボタン操作にフォールバックすること。"""
        crawler = self._make_crawler()
        crawler.driver.driver.activate_app.side_effect = RuntimeError("fail")
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": ""}):
            # bundle_id なし → activate_app スキップ → home ボタン
            crawler.driver.driver.execute_script.return_value = None
            result = crawler._escape_dead_end()
        crawler.driver.driver.execute_script.assert_called_once_with(
            "mobile: pressButton", {"name": "home"}
        )
        assert result is True

    def test_returns_false_when_all_fail(self):
        """activate_app もホームボタンも失敗したとき False を返すこと。"""
        crawler = self._make_crawler()
        crawler.driver.driver.activate_app.side_effect = RuntimeError("fail")
        crawler.driver.driver.execute_script.side_effect = RuntimeError("fail")
        with patch.dict(os.environ, {"IOS_BUNDLE_ID": ""}):
            result = crawler._escape_dead_end()
        assert result is False
