"""遷移グラフ Phase 2: build_graph() のテスト。"""
import sqlite3
from pathlib import Path

import pytest

from tools.batch_processor import BatchProcessor
from tools.ap.screen_recorder import ScreenRecorder


def _setup_db(db_path: Path):
    """テスト用にスキーマと最小限のデータを投入する。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lc_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            screens_found INTEGER DEFAULT 0,
            started_at TEXT,
            status TEXT DEFAULT 'running',
            game_title TEXT DEFAULT 'Test',
            device_mode TEXT DEFAULT 'SIM'
        );
        CREATE TABLE IF NOT EXISTS lc_screens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            title TEXT NOT NULL,
            depth INTEGER DEFAULT 0,
            parent_fp TEXT,
            phash TEXT,
            screenshot_path TEXT,
            ocr_text TEXT,
            discovered_at TEXT,
            thumbnail_path TEXT,
            scene TEXT
        );
        CREATE TABLE IF NOT EXISTS lc_tappable_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            screen_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            confidence REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lc_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            from_screen_id INTEGER NOT NULL,
            to_screen_id INTEGER,
            from_fp TEXT NOT NULL,
            to_fp TEXT,
            tap_x INTEGER,
            tap_y INTEGER,
            tap_label TEXT,
            action_name TEXT,
            discovered_at TEXT
        );
        INSERT INTO lc_sessions (session_id, started_at) VALUES ('s1', '2026-01-01');
    """)
    return conn


def _insert_screen(conn, fp, scene="MENU", phash=None):
    """テスト用画面を挿入し、ID を返す。"""
    cur = conn.execute(
        "INSERT INTO lc_screens (session_id, fingerprint, title, scene, phash, discovered_at)"
        " VALUES ('s1', ?, ?, ?, ?, '2026-01-01')",
        (fp, f"Title_{fp}", scene, phash or fp),
    )
    conn.commit()
    return cur.lastrowid


def _insert_transition(conn, from_fp, to_fp, action="TAP"):
    conn.execute(
        "INSERT INTO lc_transitions (session_id, from_screen_id, to_screen_id,"
        " from_fp, to_fp, action_name, discovered_at)"
        " VALUES ('s1', 1, 2, ?, ?, ?, '2026-01-01')",
        (from_fp, to_fp, action),
    )
    conn.commit()


class TestBuildGraph:
    """build_graph() の基本テスト。"""

    def test_empty_db(self, tmp_path):
        """遷移データなし → 0 を返す。"""
        db = tmp_path / "test.db"
        conn = _setup_db(db)
        conn.close()
        bp = BatchProcessor(db)
        assert bp.build_graph() == 0
        bp.close()

    def test_bfs_depth(self, tmp_path):
        """HOME → A → B の線形グラフで depth が正しいか。"""
        db = tmp_path / "test.db"
        conn = _setup_db(db)

        # 3画面: HOME(fp_home), A(fp_a), B(fp_b)
        _insert_screen(conn, "fp_home", "MENU", "1111111111111111")
        _insert_screen(conn, "fp_a", "MENU", "2222222222222222")
        _insert_screen(conn, "fp_b", "BATTLE", "3333333333333333")

        # 遷移: HOME → A → B, HOME は GOAL_HOME_REACHED で特定
        _insert_transition(conn, "fp_home", "fp_a", "STORY_TAP")
        _insert_transition(conn, "fp_a", "fp_b", "BATTLE_START")
        _insert_transition(conn, "fp_home", "fp_home", "GOAL_HOME_REACHED")  # 自己ループ (除外される)
        # HOME 特定用: to_fp が fp_home の GOAL_HOME_REACHED
        conn.execute(
            "UPDATE lc_transitions SET to_fp = 'fp_home'"
            " WHERE action_name = 'GOAL_HOME_REACHED'"
        )
        conn.commit()
        conn.close()

        bp = BatchProcessor(db)
        scc_count = bp.build_graph()

        # depth 確認
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r["fingerprint"]: r["bfs_depth"] for r in
                conn.execute("SELECT fingerprint, bfs_depth FROM lc_screens")}
        assert rows["fp_home"] == 0
        assert rows["fp_a"] == 1
        assert rows["fp_b"] == 2

        # 線形グラフ → SCC なし
        assert scc_count == 0
        conn.close()
        bp.close()

    def test_scc_detection(self, tmp_path):
        """A ⇔ B (同 depth の相互遷移) が SCC として検出される。"""
        db = tmp_path / "test.db"
        conn = _setup_db(db)

        _insert_screen(conn, "fp_home", "MENU", "1111111111111111")
        _insert_screen(conn, "fp_a", "BATTLE", "2222222222222222")
        _insert_screen(conn, "fp_b", "BATTLE", "3333333333333333")

        # HOME → A, HOME → B (A,B は同じ depth=1), A ⇔ B
        _insert_transition(conn, "fp_home", "fp_a", "TAP")
        _insert_transition(conn, "fp_home", "fp_b", "TAP")
        _insert_transition(conn, "fp_a", "fp_b", "TAP")
        _insert_transition(conn, "fp_b", "fp_a", "TAP")  # 同 depth → 順方向
        _insert_transition(conn, "fp_a", "fp_home", "BACK")  # 戻る → 除外
        # HOME 特定
        conn.execute(
            "INSERT INTO lc_transitions (session_id, from_screen_id, to_screen_id,"
            " from_fp, to_fp, action_name, discovered_at)"
            " VALUES ('s1', 1, 1, 'fp_home', 'fp_home', 'GOAL_HOME_REACHED', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        bp = BatchProcessor(db)
        scc_count = bp.build_graph()

        # A ⇔ B は同じ depth=1 なので戻るエッジにならず、SCC を形成
        assert scc_count == 1

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        scc_groups = conn.execute("SELECT * FROM lc_scc_groups").fetchall()
        assert len(scc_groups) == 1
        assert scc_groups[0]["screen_count"] == 2

        # A と B の scc_id が同じ
        rows = conn.execute(
            "SELECT fingerprint, scc_id FROM lc_screens WHERE scc_id IS NOT NULL"
        ).fetchall()
        scc_ids = {r["fingerprint"]: r["scc_id"] for r in rows}
        assert scc_ids.get("fp_a") == scc_ids.get("fp_b")
        conn.close()
        bp.close()

    def test_loading_bypass(self, tmp_path):
        """A → LOADING → B が A → B にバイパスされる。"""
        db = tmp_path / "test.db"
        conn = _setup_db(db)

        _insert_screen(conn, "fp_a", "MENU", "1111111111111111")
        _insert_screen(conn, "fp_load", "LOADING", "2222222222222222")
        _insert_screen(conn, "fp_b", "BATTLE", "3333333333333333")

        _insert_transition(conn, "fp_a", "fp_load", "TAP")
        _insert_transition(conn, "fp_load", "fp_b", "TAP")
        conn.close()

        bp = BatchProcessor(db)
        bp.build_graph()

        # fp_a と fp_b に depth が付いていて、fp_load には付いていない
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = {r["fingerprint"]: r["bfs_depth"] for r in
                conn.execute("SELECT fingerprint, bfs_depth FROM lc_screens WHERE bfs_depth IS NOT NULL")}
        assert "fp_a" in rows
        assert "fp_b" in rows
        assert "fp_load" not in rows  # LOADING はグラフから除去
        conn.close()
        bp.close()
