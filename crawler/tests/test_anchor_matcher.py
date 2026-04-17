"""test_anchor_matcher.py — AnchorMatcher ユニットテスト"""
import sqlite3
import pytest
from tools.anchor_matcher import AnchorMatcher, AnchorMatch, _normalize_text


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE lc_master_nodes (
            master_fp TEXT PRIMARY KEY,
            sort_order INTEGER DEFAULT 0,
            title TEXT, scene TEXT, phash TEXT,
            ocr_text TEXT, ocr_text_manual TEXT,
            first_seen_at TEXT, last_seen_at TEXT,
            representative_screen_id INTEGER,
            bfs_depth INTEGER, scc_id INTEGER, scc_label TEXT,
            visit_count INTEGER DEFAULT 1,
            user_excluded INTEGER DEFAULT 0,
            manual_group_id INTEGER, is_group_representative INTEGER DEFAULT 1
        );
        CREATE TABLE lc_screens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, fingerprint TEXT, title TEXT,
            is_representative INTEGER DEFAULT 1,
            is_artifact INTEGER DEFAULT 0,
            discovered_at TEXT, scene TEXT, phash TEXT,
            ocr_text TEXT, ocr_text_hq TEXT, ocr_text_gemini TEXT,
            cluster_id INTEGER, screenshot_path TEXT, thumbnail_path TEXT,
            depth INTEGER DEFAULT 0, edge_type TEXT
        );
        CREATE TABLE lc_transitions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, from_screen_id INTEGER,
            to_screen_id INTEGER, from_fp TEXT, to_fp TEXT,
            tap_x INTEGER, tap_y INTEGER, tap_label TEXT,
            action_name TEXT, edge_type TEXT DEFAULT 'tap',
            discovered_at TEXT
        );
        CREATE TABLE lc_node_mappings (
            session_id TEXT, session_fp TEXT, master_fp TEXT,
            match_method TEXT, match_score REAL
        );
    """)
    return conn


def _add_master(conn, fp, sort_order, text="", phash="aa00aa00aa00aa00", scene="ADV"):
    conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, sort_order, ocr_text, phash, scene) VALUES (?, ?, ?, ?, ?)",
        (fp, sort_order, text, phash, scene),
    )

def _add_session_screen(conn, sid, fp, time_idx, text="", phash="aa00aa00aa00aa00", scene="ADV"):
    conn.execute(
        "INSERT INTO lc_screens (session_id, fingerprint, title, is_representative, discovered_at, scene, phash, ocr_text)"
        " VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
        (sid, fp, fp, f"2026-01-01T00:00:{time_idx:02d}", scene, phash, text),
    )

def _add_tap_edge(conn, sid, from_fp, to_fp):
    conn.execute(
        "INSERT INTO lc_transitions (session_id, from_screen_id, to_screen_id, from_fp, to_fp, edge_type)"
        " VALUES (?, 0, 0, ?, ?, 'tap')",
        (sid, from_fp, to_fp),
    )

def _add_auto_edge(conn, sid, from_fp, to_fp):
    conn.execute(
        "INSERT INTO lc_transitions (session_id, from_screen_id, to_screen_id, from_fp, to_fp, edge_type)"
        " VALUES (?, 0, 0, ?, ?, 'auto')",
        (sid, from_fp, to_fp),
    )


class TestPrepareData:
    def test_node_classification(self, db):
        _add_session_screen(db, "s2", "a", 1, text="hello", phash="aa00aa00aa00aa00")
        _add_session_screen(db, "s2", "b", 2, text="", phash="bb00bb00bb00bb00")
        _add_session_screen(db, "s2", "c", 3, text="world", phash="cc00")
        _add_tap_edge(db, "s2", "a", "b")
        _add_auto_edge(db, "s2", "b", "c")
        db.commit()

        m = AnchorMatcher()
        session_nodes, _, _ = m._prepare_data(db, "s2")
        assert len(session_nodes) == 3
        assert session_nodes[0].edge_type == "tap"  # a: tap edge
        assert session_nodes[0].has_text == True
        assert session_nodes[1].edge_type == "tap"  # b: tap の to_fp
        assert session_nodes[1].has_text == False
        assert session_nodes[2].edge_type == "auto"  # c: auto edge
        assert session_nodes[2].has_text == True

    def test_time_rank(self, db):
        _add_session_screen(db, "s2", "x", 3, text="third")
        _add_session_screen(db, "s2", "y", 1, text="first")
        _add_session_screen(db, "s2", "z", 2, text="second")
        db.commit()

        m = AnchorMatcher()
        nodes, _, _ = m._prepare_data(db, "s2")
        # discovered_at 順: y(01), z(02), x(03)
        assert nodes[0].fp == "y"
        assert nodes[0].time_rank == 0
        assert nodes[1].fp == "z"
        assert nodes[1].time_rank == 1
        assert nodes[2].fp == "x"
        assert nodes[2].time_rank == 2


class TestPhase1TapText:
    def test_exact_match(self, db):
        _add_master(db, "M_A", 0, text="hello world", phash="aa00aa00aa00aa00")
        _add_master(db, "M_B", 1, text="goodbye", phash="bb00bb00bb00bb00")
        _add_session_screen(db, "s2", "a", 1, text="hello world", phash="aa00aa00aa00aa01")
        _add_tap_edge(db, "s2", "a", "x")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        anchors = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        assert len(anchors) == 1
        assert anchors[0].master_fp == "M_A"

    def test_multiple_candidates_rejected(self, db):
        _add_master(db, "M_A", 0, text="hello", phash="aa00aa00aa00aa00")
        _add_master(db, "M_B", 1, text="hello", phash="bb00bb00bb00bb00")  # same text
        _add_session_screen(db, "s2", "a", 1, text="hello", phash="aa00aa00aa00aa01")
        _add_tap_edge(db, "s2", "a", "x")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        anchors = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        assert len(anchors) == 0  # ambiguous → rejected

    def test_phash_too_far_rejected(self, db):
        _add_master(db, "M_A", 0, text="hello", phash="0000000000000000")
        _add_session_screen(db, "s2", "a", 1, text="hello", phash="ffffffffffffffff")
        _add_tap_edge(db, "s2", "a", "x")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        anchors = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        assert len(anchors) == 0  # phash too far

    def test_auto_edge_excluded(self, db):
        _add_master(db, "M_A", 0, text="hello", phash="aa00aa00aa00aa00")
        _add_session_screen(db, "s2", "a", 1, text="hello", phash="aa00aa00aa00aa01")
        _add_auto_edge(db, "s2", "a", "x")  # auto, not tap
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        anchors = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        assert len(anchors) == 0  # auto → not Phase 1 target


class TestVerifyConsistency:
    def test_all_consistent(self, db):
        m = AnchorMatcher()
        anchors = [
            AnchorMatch("a", "M_A", 0, "p1", 1.0, 1),
            AnchorMatch("b", "M_B", 5, "p1", 1.0, 1),
            AnchorMatch("c", "M_C", 10, "p1", 1.0, 1),
        ]
        result = m._verify_consistency(anchors)
        assert len(result) == 3

    def test_one_contradiction(self, db):
        m = AnchorMatcher()
        anchors = [
            AnchorMatch("a", "M_A", 0, "p1", 1.0, 1),
            AnchorMatch("b", "M_B", 10, "p1", 1.0, 1),
            AnchorMatch("c", "M_C", 5, "p1", 1.0, 1),  # 10 > 5 → contradiction
        ]
        result = m._verify_consistency(anchors)
        assert len(result) == 2  # LIS keeps 2

    def test_empty(self, db):
        m = AnchorMatcher()
        result = m._verify_consistency([])
        assert len(result) == 0


class TestPhase2AutoText:
    def test_range_limited_match(self, db):
        _add_master(db, "M_A", 0, text="start", phash="aa00aa00aa00aa00")
        _add_master(db, "M_X", 1, text="middle", phash="cc00cc00cc00cc00")
        _add_master(db, "M_B", 2, text="end", phash="bb00bb00bb00bb00")
        _add_master(db, "M_Y", 5, text="middle", phash="dd00dd00dd00dd00")  # same text but out of range

        _add_session_screen(db, "s2", "a", 1, text="start", phash="aa00aa00aa00aa01")
        _add_session_screen(db, "s2", "x", 2, text="middle", phash="cc00cc00cc00cc01")
        _add_session_screen(db, "s2", "b", 3, text="end", phash="bb00bb00bb00bb01")
        _add_tap_edge(db, "s2", "a", "b")
        _add_auto_edge(db, "s2", "a", "x")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        p1 = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        assert len(p1) == 2  # A and B matched

        p2 = m._phase2_auto_text(s_nodes, m_nodes, sort_map, p1)
        assert len(p2) == 1
        assert p2[0].master_fp == "M_X"  # M_Y is out of range


class TestPhase3TapPhash:
    def test_both_anchors_required(self, db):
        _add_master(db, "M_A", 0, text="start", phash="aa00aa00aa00aa00")
        _add_master(db, "M_X", 1, text="", phash="cc00cc00cc00cc00")
        _add_master(db, "M_B", 2, text="end", phash="bb00bb00bb00bb00")

        _add_session_screen(db, "s2", "a", 1, text="start", phash="aa00aa00aa00aa01")
        _add_session_screen(db, "s2", "x", 2, text="", phash="cc00cc00cc00cc01")
        _add_session_screen(db, "s2", "b", 3, text="end", phash="bb00bb00bb00bb01")
        _add_tap_edge(db, "s2", "a", "x")
        _add_tap_edge(db, "s2", "x", "b")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        p1 = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        p3 = m._phase3_tap_phash(s_nodes, m_nodes, sort_map, p1)
        assert len(p3) == 1
        assert p3[0].master_fp == "M_X"

    def test_no_prev_anchor_skipped(self, db):
        _add_master(db, "M_X", 0, text="", phash="cc00cc00cc00cc00")
        _add_master(db, "M_B", 1, text="end", phash="bb00bb00bb00bb00")

        _add_session_screen(db, "s2", "x", 1, text="", phash="cc00cc00cc00cc01")
        _add_session_screen(db, "s2", "b", 2, text="end", phash="bb00bb00bb00bb01")
        _add_tap_edge(db, "s2", "x", "b")
        db.commit()

        m = AnchorMatcher()
        s_nodes, m_nodes, sort_map = m._prepare_data(db, "s2")
        p1 = m._phase1_tap_text(s_nodes, m_nodes, sort_map)
        p3 = m._phase3_tap_phash(s_nodes, m_nodes, sort_map, p1)
        assert len(p3) == 0  # no prev anchor → skip


class TestCrossSessionMergerIntegration:
    """CrossSessionMerger 経由で AnchorMatcher が呼ばれることを確認。"""

    @pytest.fixture
    def full_db(self):
        """CrossSessionMerger が必要とする全テーブルを持つ DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE lc_master_nodes (
                master_fp TEXT PRIMARY KEY,
                representative_screen_id INTEGER,
                title TEXT, scene TEXT, phash TEXT,
                ocr_text TEXT, ocr_text_manual TEXT, title_manual TEXT,
                manual_edited_at TEXT,
                bfs_depth INTEGER, scc_id INTEGER, scc_label TEXT,
                visit_count INTEGER DEFAULT 1,
                first_seen_at TEXT, last_seen_at TEXT,
                sort_order INTEGER DEFAULT 0,
                user_excluded INTEGER DEFAULT 0,
                manual_group_id INTEGER DEFAULT NULL,
                is_group_representative INTEGER DEFAULT 1
            );
            CREATE TABLE lc_master_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_master_fp TEXT NOT NULL, to_master_fp TEXT NOT NULL,
                tap_label TEXT, action_name TEXT,
                edge_type TEXT DEFAULT 'tap',
                count INTEGER DEFAULT 1,
                avg_duration REAL, min_duration REAL,
                first_seen_at TEXT, last_seen_at TEXT,
                UNIQUE(from_master_fp, to_master_fp, tap_label)
            );
            CREATE TABLE lc_screens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, fingerprint TEXT, title TEXT,
                is_representative INTEGER DEFAULT 1,
                is_artifact INTEGER DEFAULT 0,
                discovered_at TEXT, scene TEXT, phash TEXT,
                ocr_text TEXT, ocr_text_hq TEXT, ocr_text_gemini TEXT,
                cluster_id INTEGER, screenshot_path TEXT, thumbnail_path TEXT,
                depth INTEGER DEFAULT 0, edge_type TEXT
            );
            CREATE TABLE lc_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, from_screen_id INTEGER,
                to_screen_id INTEGER, from_fp TEXT, to_fp TEXT,
                tap_x INTEGER, tap_y INTEGER, tap_label TEXT,
                action_name TEXT, edge_type TEXT DEFAULT 'tap',
                discovered_at TEXT
            );
            CREATE TABLE lc_node_mappings (
                session_id TEXT, session_fp TEXT, master_fp TEXT,
                match_method TEXT, match_score REAL
            );
            CREATE TABLE lc_session_graphs (
                session_id TEXT PRIMARY KEY,
                node_count INTEGER DEFAULT 0, edge_count INTEGER DEFAULT 0,
                scc_count INTEGER DEFAULT 0, home_fp TEXT, built_at TEXT
            );
            CREATE TABLE auto_pilot_state (
                key TEXT PRIMARY KEY, value TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        return conn

    def test_compute_matches_uses_anchor_matcher(self, full_db):
        """_compute_matches が AnchorMatcher に委譲されること。"""
        from unittest.mock import patch, MagicMock
        from tools.cross_session_merger import CrossSessionMerger

        # マスターに1ノードあれば seed=False パスに入る
        full_db.execute(
            "INSERT INTO lc_master_nodes (master_fp, sort_order, ocr_text, phash, scene)"
            " VALUES ('M_A', 0, 'hello', 'aa00aa00aa00aa00', 'ADV')"
        )
        _add_session_screen(full_db, "s2", "a", 1, text="hello", phash="aa00aa00aa00aa01")
        _add_tap_edge(full_db, "s2", "a", "x")
        full_db.commit()

        # CrossSessionMerger の conn を差し替え
        with patch.object(CrossSessionMerger, '__init__', lambda self, **kw: None):
            merger = CrossSessionMerger.__new__(CrossSessionMerger)
            merger._conn = full_db
            from tools.merge_sort_strategy import SafeInsertStrategy
            from tools.anchor_matcher import AnchorMatcher
            merger._sort_strategy = SafeInsertStrategy()
            merger._anchor_matcher = AnchorMatcher()

            node_mapping, session_reps, is_seed = merger._compute_matches("s2")

        assert is_seed is False
        # AnchorMatcher で Phase 1 マッチが見つかるはず
        assert "a" in node_mapping
        assert node_mapping["a"][0] == "M_A"
        assert node_mapping["a"][1] == "phase1_tap_text"

    def test_seed_path_unchanged(self, full_db):
        """マスター空の場合は seed パスに入り AnchorMatcher は不使用。"""
        from unittest.mock import patch
        from tools.cross_session_merger import CrossSessionMerger

        _add_session_screen(full_db, "s1", "a", 1, text="hello")
        full_db.commit()

        with patch.object(CrossSessionMerger, '__init__', lambda self, **kw: None):
            merger = CrossSessionMerger.__new__(CrossSessionMerger)
            merger._conn = full_db
            from tools.merge_sort_strategy import SafeInsertStrategy
            merger._sort_strategy = SafeInsertStrategy()
            merger._anchor_matcher = AnchorMatcher()

            node_mapping, session_reps, is_seed = merger._compute_matches("s1")

        assert is_seed is True
        assert len(node_mapping) == 0
        assert len(session_reps) == 1
