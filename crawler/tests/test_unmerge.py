"""未マージ (Unmerge) 機能のテスト。

テスト対象:
- can_unmerge: 可否チェック
- unmerge_session: 再構築 + 手動変更復元 + orphan チェック
"""
import sqlite3
from pathlib import Path

import pytest

from tools.cross_session_merger import CrossSessionMerger


def _setup_db(db_path: Path) -> sqlite3.Connection:
    """テスト用 DB スキーマ + 最小データを投入。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lc_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            screens_found INTEGER DEFAULT 0,
            started_at TEXT,
            status TEXT DEFAULT 'completed',
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
            thumbnail_path TEXT,
            ocr_text TEXT,
            ocr_text_hq TEXT,
            discovered_at TEXT,
            scene TEXT,
            is_representative BOOLEAN DEFAULT 0,
            cluster_id INTEGER
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
        CREATE TABLE IF NOT EXISTS lc_master_nodes (
            master_fp TEXT PRIMARY KEY,
            representative_screen_id INTEGER,
            title TEXT,
            scene TEXT,
            phash TEXT,
            ocr_text TEXT,
            visit_count INTEGER DEFAULT 0,
            first_seen_at TEXT,
            last_seen_at TEXT,
            bfs_depth INTEGER,
            scc_id INTEGER,
            scc_label TEXT,
            sort_order INTEGER,
            user_excluded BOOLEAN DEFAULT 0,
            manual_group_id INTEGER,
            is_group_representative BOOLEAN DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lc_master_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_master_fp TEXT NOT NULL,
            to_master_fp TEXT NOT NULL,
            tap_label TEXT,
            action_name TEXT,
            count INTEGER DEFAULT 1,
            first_seen_at TEXT,
            last_seen_at TEXT,
            UNIQUE(from_master_fp, to_master_fp, tap_label)
        );
        CREATE TABLE IF NOT EXISTS lc_node_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            session_fp TEXT NOT NULL,
            master_fp TEXT NOT NULL,
            match_method TEXT,
            match_score REAL,
            UNIQUE(session_id, session_fp)
        );
        CREATE TABLE IF NOT EXISTS lc_session_graphs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT UNIQUE NOT NULL,
            node_count INTEGER DEFAULT 0,
            edge_count INTEGER DEFAULT 0,
            scc_count INTEGER DEFAULT 0,
            home_fp TEXT,
            built_at TEXT
        );
        CREATE TABLE IF NOT EXISTS lc_scc_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            screen_count INTEGER DEFAULT 0,
            root_fp TEXT
        );
        CREATE TABLE IF NOT EXISTS auto_pilot_state (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    return conn


def _insert_screen(conn, session_id, fp, title=None, scene="MENU",
                    phash=None, is_rep=1):
    """テスト用画面を挿入し ID を返す。"""
    cur = conn.execute(
        "INSERT INTO lc_screens"
        " (session_id, fingerprint, title, scene, phash, discovered_at,"
        "  is_representative, screenshot_path)"
        " VALUES (?, ?, ?, ?, ?, '2026-01-01', ?, '')",
        (session_id, fp, title or f"Title_{fp}", scene, phash or fp, is_rep),
    )
    conn.commit()
    return cur.lastrowid


def _insert_transition(conn, session_id, from_fp, to_fp):
    conn.execute(
        "INSERT INTO lc_transitions"
        " (session_id, from_screen_id, to_screen_id, from_fp, to_fp,"
        "  action_name, discovered_at)"
        " VALUES (?, 1, 2, ?, ?, 'TAP', '2026-01-01')",
        (session_id, from_fp, to_fp),
    )
    conn.commit()


def _setup_two_sessions(db_path: Path):
    """2セッション + マージ済み状態を構築。

    Session A: home → menu → shop (3ノード, 2エッジ)
    Session B: home → menu → battle (3ノード, 2エッジ, battle は B 固有)
    """
    conn = _setup_db(db_path)

    # Session A
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at)"
        " VALUES ('sA', '2026-01-01')"
    )
    _insert_screen(conn, "sA", "home", phash="ph_home")
    _insert_screen(conn, "sA", "menu", phash="ph_menu")
    _insert_screen(conn, "sA", "shop", phash="ph_shop")
    _insert_transition(conn, "sA", "home", "menu")
    _insert_transition(conn, "sA", "menu", "shop")

    # Session B
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at)"
        " VALUES ('sB', '2026-01-02')"
    )
    _insert_screen(conn, "sB", "home", phash="ph_home")
    _insert_screen(conn, "sB", "menu", phash="ph_menu")
    _insert_screen(conn, "sB", "battle", phash="ph_battle")
    _insert_transition(conn, "sB", "home", "menu")
    _insert_transition(conn, "sB", "menu", "battle")

    conn.close()

    # マージ実行
    merger = CrossSessionMerger(db_path)
    # session_graphs を登録
    merger._conn.execute(
        "INSERT INTO lc_session_graphs (session_id, node_count, edge_count, built_at)"
        " VALUES ('sA', 3, 2, '2026-01-01')"
    )
    merger._conn.execute(
        "INSERT INTO lc_session_graphs (session_id, node_count, edge_count, built_at)"
        " VALUES ('sB', 3, 2, '2026-01-02')"
    )
    merger._conn.commit()

    merger.merge_to_master("sA")  # seed
    merger.merge_to_master("sB")  # merge

    return merger


# ─── can_unmerge テスト ───────────────────────────


class TestCanUnmerge:
    def test_can_unmerge_valid(self, tmp_path):
        merger = _setup_two_sessions(tmp_path / "test.db")
        result = merger.can_unmerge("sB")
        merger.close()
        assert result["ok"] is True

    def test_can_unmerge_no_session_graph(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = _setup_db(db_path)
        conn.close()
        merger = CrossSessionMerger(db_path)
        result = merger.can_unmerge("nonexistent")
        merger.close()
        assert result["ok"] is False
        assert "存在しません" in result["reason"]

    def test_can_unmerge_not_merged(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = _setup_db(db_path)
        conn.execute(
            "INSERT INTO lc_session_graphs (session_id, node_count)"
            " VALUES ('s1', 5)"
        )
        conn.commit()
        conn.close()
        merger = CrossSessionMerger(db_path)
        result = merger.can_unmerge("s1")
        merger.close()
        assert result["ok"] is False
        assert "未マージ" in result["reason"]

    def test_can_unmerge_no_mappings(self, tmp_path):
        db_path = tmp_path / "test.db"
        conn = _setup_db(db_path)
        conn.execute(
            "INSERT INTO lc_session_graphs (session_id, node_count, built_at)"
            " VALUES ('s1', 5, '2026-01-01')"
        )
        conn.commit()
        conn.close()
        merger = CrossSessionMerger(db_path)
        result = merger.can_unmerge("s1")
        merger.close()
        assert result["ok"] is False
        assert "node_mappings" in result["reason"]


# ─── unmerge_session テスト ───────────────────────


class TestUnmergeSession:
    def test_unmerge_removes_session_nodes(self, tmp_path):
        """B を unmerge → B 固有ノード (battle) が消え、共通ノードは残る。"""
        merger = _setup_two_sessions(tmp_path / "test.db")

        # battle は B 固有
        before = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes WHERE master_fp = 'battle'"
        ).fetchone()[0]
        assert before == 1

        result = merger.unmerge_session("sB")
        assert result["ok"] is True

        # battle は消えている
        after = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes WHERE master_fp = 'battle'"
        ).fetchone()[0]
        assert after == 0

        # 共通ノード (home, menu) と A 固有 (shop) は残る
        remaining = {r[0] for r in merger._conn.execute(
            "SELECT master_fp FROM lc_master_nodes"
        ).fetchall()}
        assert remaining == {"home", "menu", "shop"}

        # node_mappings に sB がない
        b_mappings = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_node_mappings WHERE session_id = 'sB'"
        ).fetchone()[0]
        assert b_mappings == 0

        # session_graphs の sB は built_at が NULL
        sg = merger._conn.execute(
            "SELECT built_at FROM lc_session_graphs WHERE session_id = 'sB'"
        ).fetchone()
        assert sg["built_at"] is None

        merger.close()

    def test_unmerge_restores_manual_changes(self, tmp_path):
        """手動変更 (user_excluded, title) が復元される。"""
        merger = _setup_two_sessions(tmp_path / "test.db")

        # 手動変更を適用
        merger._conn.execute(
            "UPDATE lc_master_nodes SET user_excluded = 1 WHERE master_fp = 'shop'"
        )
        merger._conn.execute(
            "UPDATE lc_master_nodes SET title = '手動タイトル' WHERE master_fp = 'home'"
        )
        merger._conn.execute(
            "UPDATE lc_master_nodes SET manual_group_id = 1,"
            " is_group_representative = 1 WHERE master_fp = 'menu'"
        )
        merger._conn.commit()

        result = merger.unmerge_session("sB")
        assert result["ok"] is True
        assert result["restored_manual"] > 0

        # user_excluded が復元されている
        shop = merger._conn.execute(
            "SELECT user_excluded FROM lc_master_nodes WHERE master_fp = 'shop'"
        ).fetchone()
        assert shop["user_excluded"] == 1

        # title が復元されている
        home = merger._conn.execute(
            "SELECT title FROM lc_master_nodes WHERE master_fp = 'home'"
        ).fetchone()
        assert home["title"] == "手動タイトル"

        # manual_group_id が復元されている
        menu = merger._conn.execute(
            "SELECT manual_group_id, is_group_representative"
            " FROM lc_master_nodes WHERE master_fp = 'menu'"
        ).fetchone()
        assert menu["manual_group_id"] == 1
        assert menu["is_group_representative"] == 1

        merger.close()

    def test_unmerge_clears_rebuilding_flag(self, tmp_path):
        """正常完了時に is_rebuilding フラグが 0 に戻る。"""
        merger = _setup_two_sessions(tmp_path / "test.db")

        merger.unmerge_session("sB")

        flag = merger._conn.execute(
            "SELECT value FROM auto_pilot_state WHERE key = 'is_rebuilding'"
        ).fetchone()
        assert flag is not None
        assert flag["value"] == "0"

        merger.close()

    def test_unmerge_impossible_returns_error(self, tmp_path):
        """can_unmerge が False の場合、エラーを返す。"""
        db_path = tmp_path / "test.db"
        conn = _setup_db(db_path)
        conn.close()
        merger = CrossSessionMerger(db_path)
        result = merger.unmerge_session("nonexistent")
        assert result["ok"] is False
        assert result["error"] is not None
        merger.close()

    def test_unmerge_edges_recalculated(self, tmp_path):
        """B を unmerge → B 固有エッジ (menu→battle) が消える。"""
        merger = _setup_two_sessions(tmp_path / "test.db")

        # menu→battle エッジ存在確認
        before = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_master_edges"
            " WHERE from_master_fp = 'menu' AND to_master_fp = 'battle'"
        ).fetchone()[0]
        assert before >= 1

        merger.unmerge_session("sB")

        # menu→battle は消えている
        after = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_master_edges"
            " WHERE from_master_fp = 'menu' AND to_master_fp = 'battle'"
        ).fetchone()[0]
        assert after == 0

        # menu→shop は残っている
        shop_edge = merger._conn.execute(
            "SELECT COUNT(*) FROM lc_master_edges"
            " WHERE from_master_fp = 'menu' AND to_master_fp = 'shop'"
        ).fetchone()[0]
        assert shop_edge >= 1

        merger.close()
