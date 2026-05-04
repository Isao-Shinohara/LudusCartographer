"""Phase 1: 代表変更ハンドラのテスト。

CrossSessionMerger の orphan 修復で representative_screen_id が変わったとき:
1. 現在の付与タグを lc_master_node_tag_history に記録
2. assigned_by='gemini' のタグを物理削除
3. auto_pilot / manual は保持

設計書: docs/design/master_node_tags.md §21 ルール 5
詳細計画: docs/design/master_node_tags_phase1.md §5
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))


def _setup_db(tmp_path):
    """前提テーブル + タグ migration + lc_versions + マスターノード + タグ付与を準備。"""
    from tools.batch_processor import BatchProcessor

    db_path = tmp_path / "test.db"
    pre = sqlite3.connect(str(db_path))
    pre.executescript("""
        CREATE TABLE IF NOT EXISTS lc_sessions (
            id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
            screens_found INTEGER DEFAULT 0, started_at TEXT,
            status TEXT DEFAULT 'completed', game_title TEXT DEFAULT 'Test'
        );
        CREATE TABLE IF NOT EXISTS lc_screens (
            id INTEGER PRIMARY KEY, session_id TEXT, fingerprint TEXT,
            title TEXT, depth INTEGER DEFAULT 0, parent_fp TEXT,
            phash TEXT, screenshot_path TEXT, thumbnail_path TEXT,
            ocr_text TEXT, scene TEXT, discovered_at TEXT,
            is_representative INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS lc_tappable_items (
            id INTEGER PRIMARY KEY, screen_id INTEGER, text TEXT, confidence REAL
        );
    """)
    pre.commit()
    pre.close()

    bp = BatchProcessor(db_path=db_path)
    # lc_versions に v1 (active) を確保
    if not bp._conn.execute("SELECT 1 FROM lc_versions LIMIT 1").fetchone():
        bp._conn.execute(
            "INSERT INTO lc_versions (name, is_active) VALUES ('v1.0.0', 1)"
        )
    # 既存タグを使う (シーン: ホーム=1)
    bp._conn.execute(
        "INSERT OR IGNORE INTO lc_master_nodes"
        " (master_fp, representative_screen_id, sort_order, version_id)"
        " VALUES ('fp1', 100, 0, 1)"
    )
    # 3 種別の付与を作る (auto_pilot / manual / gemini)
    rows = bp._conn.execute(
        "SELECT id FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0 LIMIT 3"
    ).fetchall()
    tag_ids = [r[0] for r in rows]
    bp._conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'auto_pilot')",
        (tag_ids[0],),
    )
    bp._conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'manual')",
        (tag_ids[1],),
    )
    bp._conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'gemini')",
        (tag_ids[2],),
    )
    bp._conn.commit()
    bp._conn.close()
    return db_path, tag_ids


def test_record_rep_change_history_inserts_row(tmp_path):
    """履歴行が representative_changed として記録される。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, tag_ids = _setup_db(tmp_path)
    merger = CrossSessionMerger(db_path)
    merger._record_rep_change_history("fp1", old_screen_id=100, new_screen_id=200)
    merger._conn.commit()

    rows = merger._conn.execute(
        "SELECT event_type, old_screen_id, new_screen_id, old_tag_ids"
        " FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
    ).fetchall()
    merger.close()

    assert len(rows) == 1
    assert rows[0]["event_type"] == "representative_changed"
    assert rows[0]["old_screen_id"] == 100
    assert rows[0]["new_screen_id"] == 200
    saved_tag_ids = sorted(json.loads(rows[0]["old_tag_ids"]))
    assert saved_tag_ids == sorted(tag_ids)


def test_record_rep_change_history_no_tags(tmp_path):
    """付与タグがないノードでは履歴行が作られない (= 記録不要)。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, _ = _setup_db(tmp_path)
    # タグを全削除
    conn = sqlite3.connect(str(db_path))
    conn.execute("DELETE FROM lc_master_node_tags WHERE master_fp = 'fp1'")
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    merger._record_rep_change_history("fp1", old_screen_id=100, new_screen_id=200)
    merger._conn.commit()

    cnt = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
    ).fetchone()[0]
    merger.close()
    assert cnt == 0


def test_record_rep_change_history_per_version(tmp_path):
    """version をまたいだ付与があれば version ごとに 1 行記録される。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, tag_ids = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    # version=2 に別タグ付与
    conn.execute(
        "INSERT INTO lc_versions (name, is_active) VALUES ('v2.0.0', 0)"
    )
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 2, ?, 'manual')",
        (tag_ids[0],),
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    merger._record_rep_change_history("fp1", old_screen_id=100, new_screen_id=200)
    merger._conn.commit()

    rows = merger._conn.execute(
        "SELECT version_id, old_tag_ids"
        " FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
        " ORDER BY version_id"
    ).fetchall()
    merger.close()
    assert len(rows) == 2
    assert rows[0]["version_id"] == 1
    assert rows[1]["version_id"] == 2


def test_cleanup_gemini_tags_only(tmp_path):
    """assigned_by='gemini' だけ物理削除、auto_pilot / manual は保持。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, tag_ids = _setup_db(tmp_path)
    merger = CrossSessionMerger(db_path)
    merger._cleanup_gemini_tags_on_rep_change("fp1")
    merger._conn.commit()

    rows = merger._conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags"
        " WHERE master_fp = 'fp1' ORDER BY id"
    ).fetchall()
    merger.close()

    assert len(rows) == 2
    assert {r["assigned_by"] for r in rows} == {"auto_pilot", "manual"}
    # gemini で付与されていた tag_ids[2] は削除されている
    remaining_tag_ids = {r["tag_id"] for r in rows}
    assert tag_ids[2] not in remaining_tag_ids


def test_cleanup_gemini_tags_no_op_when_none(tmp_path):
    """gemini 付与がない場合は何も削除しない。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, _ = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "DELETE FROM lc_master_node_tags WHERE master_fp = 'fp1' AND assigned_by = 'gemini'"
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    before = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'fp1'"
    ).fetchone()[0]
    merger._cleanup_gemini_tags_on_rep_change("fp1")
    merger._conn.commit()
    after = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'fp1'"
    ).fetchone()[0]
    merger.close()
    assert before == after  # 削除なし


def test_cleanup_gemini_tags_does_not_affect_other_master_fp(tmp_path):
    """別 master_fp の gemini タグは削除されない。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path, tag_ids = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp_other', 1, ?, 'gemini')",
        (tag_ids[0],),
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    merger._cleanup_gemini_tags_on_rep_change("fp1")
    merger._conn.commit()

    cnt = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags"
        " WHERE master_fp = 'fp_other' AND assigned_by = 'gemini'"
    ).fetchone()[0]
    merger.close()
    assert cnt == 1
