"""test_merge_sort.py — SafeInsertStrategy のユニットテスト"""
import sqlite3
import pytest
from tools.merge_sort_strategy import SafeInsertStrategy, renumber_sort_orders


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE lc_master_nodes (
            master_fp TEXT PRIMARY KEY,
            representative_screen_id INTEGER,
            title TEXT,
            sort_order INTEGER DEFAULT 0,
            first_seen_at TEXT,
            last_seen_at TEXT,
            scene TEXT, phash TEXT, ocr_text TEXT,
            bfs_depth INTEGER, scc_id INTEGER, scc_label TEXT,
            visit_count INTEGER DEFAULT 1,
            user_excluded INTEGER DEFAULT 0,
            manual_group_id INTEGER, is_group_representative INTEGER DEFAULT 1
        );
        CREATE TABLE lc_node_mappings (
            session_id TEXT, session_fp TEXT, master_fp TEXT,
            match_method TEXT, match_score REAL
        );
        CREATE TABLE lc_screens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, fingerprint TEXT, title TEXT,
            is_representative INTEGER DEFAULT 1,
            discovered_at TEXT, scene TEXT, phash TEXT,
            ocr_text TEXT, cluster_id INTEGER,
            screenshot_path TEXT, thumbnail_path TEXT,
            depth INTEGER DEFAULT 0
        );
    """)
    return conn


def _add_master(conn, fp, sort_order):
    conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, sort_order, first_seen_at) VALUES (?, ?, ?)",
        (fp, sort_order, f"2026-01-01T00:00:{sort_order:02d}"),
    )


def _add_screen(conn, sid, fp, time_suffix):
    conn.execute(
        "INSERT INTO lc_screens (session_id, fingerprint, title, is_representative, discovered_at)"
        " VALUES (?, ?, ?, 1, ?)",
        (sid, fp, fp, f"2026-01-01T01:00:{time_suffix}"),
    )


def _add_mapping(conn, sid, s_fp, m_fp, method):
    conn.execute(
        "INSERT INTO lc_node_mappings (session_id, session_fp, master_fp, match_method, match_score)"
        " VALUES (?, ?, ?, ?, 1.0)",
        (sid, s_fp, m_fp, method),
    )


class TestAdjacentInsert:
    """隣接アンカー間の挿入"""

    def test_single_insert_between_adjacent(self, db):
        # Master: A(0) B(1) C(2)
        _add_master(db, "A", 0)
        _add_master(db, "B", 1)
        _add_master(db, "C", 2)
        # S2: A' F B' (F is new, A-B are adjacent)
        _add_screen(db, "s2", "a2", "01")
        _add_screen(db, "s2", "f", "02")
        _add_screen(db, "s2", "b2", "03")
        _add_mapping(db, "s2", "a2", "A", "anchor")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "b2", "B", "anchor")
        db.commit()

        s = SafeInsertStrategy()
        result = s.compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 1
        assert result.inserts[0][0] == "F"
        assert 0 < result.inserts[0][1] < 1  # Between A(0) and B(1)
        assert len(result.skipped) == 0

    def test_multiple_insert_between_adjacent(self, db):
        _add_master(db, "A", 0)
        _add_master(db, "B", 1)
        # S2: A' F G B'
        _add_screen(db, "s2", "a2", "01")
        _add_screen(db, "s2", "f", "02")
        _add_screen(db, "s2", "g", "03")
        _add_screen(db, "s2", "b2", "04")
        _add_mapping(db, "s2", "a2", "A", "anchor")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "g", "G", "new")
        _add_mapping(db, "s2", "b2", "B", "anchor")
        db.commit()

        result = SafeInsertStrategy().compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 2
        assert result.inserts[0][0] == "F"
        assert result.inserts[1][0] == "G"
        # F before G, both between 0 and 1
        assert 0 < result.inserts[0][1] < result.inserts[1][1] < 1


class TestHeadTailInsert:
    """先頭・末尾への挿入"""

    def test_tail_insert(self, db):
        _add_master(db, "A", 0)
        _add_master(db, "B", 1)
        # S2: B' F G (B is tail anchor)
        _add_screen(db, "s2", "b2", "01")
        _add_screen(db, "s2", "f", "02")
        _add_screen(db, "s2", "g", "03")
        _add_mapping(db, "s2", "b2", "B", "anchor")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "g", "G", "new")
        db.commit()

        result = SafeInsertStrategy().compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 2
        assert result.inserts[0][1] > 1  # After B(1)
        assert result.inserts[0][1] < result.inserts[1][1]  # F before G

    def test_head_insert(self, db):
        _add_master(db, "A", 0)
        _add_master(db, "B", 1)
        # S2: F A' (A is head anchor)
        _add_screen(db, "s2", "f", "01")
        _add_screen(db, "s2", "a2", "02")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "a2", "A", "anchor")
        db.commit()

        result = SafeInsertStrategy().compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 1
        assert result.inserts[0][1] < 0  # Before A(0)


class TestSkip:
    """非隣接アンカー → スキップ"""

    def test_non_adjacent_skip(self, db):
        _add_master(db, "A", 0)
        _add_master(db, "B", 1)
        _add_master(db, "C", 2)
        # S2: A' F C' (A-C are NOT adjacent, B is between)
        _add_screen(db, "s2", "a2", "01")
        _add_screen(db, "s2", "f", "02")
        _add_screen(db, "s2", "c2", "03")
        _add_mapping(db, "s2", "a2", "A", "anchor")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "c2", "C", "anchor")
        db.commit()

        result = SafeInsertStrategy().compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 0
        assert "F" in result.skipped

    def test_no_anchors_all_skip(self, db):
        _add_master(db, "A", 0)
        # S2: F G (no anchors)
        _add_screen(db, "s2", "f", "01")
        _add_screen(db, "s2", "g", "02")
        _add_mapping(db, "s2", "f", "F", "new")
        _add_mapping(db, "s2", "g", "G", "new")
        db.commit()

        result = SafeInsertStrategy().compute_sort_order(db, "s2", {})
        assert len(result.inserts) == 0
        assert len(result.skipped) == 2


class TestRenumber:
    """再番号付け"""

    def test_renumber(self, db):
        _add_master(db, "A", 0)
        _add_master(db, "X", 0)  # duplicate
        _add_master(db, "B", 5)
        db.execute("UPDATE lc_master_nodes SET sort_order = -1 WHERE master_fp = 'X'")
        db.commit()
        renumber_sort_orders(db)
        rows = db.execute("SELECT master_fp, sort_order FROM lc_master_nodes ORDER BY sort_order").fetchall()
        assert [(r["master_fp"], r["sort_order"]) for r in rows] == [("X", 0), ("A", 1), ("B", 2)]
