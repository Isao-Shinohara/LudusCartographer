"""Phase 2: 操縦カテゴリ自動付与のテスト。

設計書: docs/design/master_node_tags.md §7
詳細計画: docs/design/master_node_tags_phase1.md §11 (P2 スコープ)
CLAUDE.md §21 ルール 1 (操縦カテゴリの追加)
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))


def _setup_db(tmp_path):
    """前提テーブル + タグ migration 済 DB を返す (path)。"""
    from tools.batch_processor import BatchProcessor

    db_path = tmp_path / "test.db"
    pre = sqlite3.connect(str(db_path))
    pre.executescript("""
        CREATE TABLE IF NOT EXISTS lc_sessions (
            id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
            screens_found INTEGER DEFAULT 0, started_at TEXT,
            status TEXT DEFAULT 'completed', game_title TEXT DEFAULT 'Test',
            version_id INTEGER DEFAULT 1
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
    if not bp._conn.execute("SELECT 1 FROM lc_versions LIMIT 1").fetchone():
        bp._conn.execute(
            "INSERT INTO lc_versions (name, is_active) VALUES ('v1.0.0', 1)"
        )
        bp._conn.commit()
    bp._conn.close()
    return db_path


# ─── enum + 逆引き ─────────────────────────────────────


def test_operation_enum_has_tutorial():
    from tools.ap.operation_tags import OperationTag
    assert hasattr(OperationTag, "TUTORIAL")
    assert OperationTag.TUTORIAL == 1


def test_operation_enum_code_keys():
    from tools.ap.operation_tags import (
        OperationTag, OPERATION_TAG_CODE_KEYS, OPERATION_TAG_NAMES,
    )
    assert OPERATION_TAG_CODE_KEYS[OperationTag.TUTORIAL] == "tutorial"
    assert OPERATION_TAG_NAMES[OperationTag.TUTORIAL] == "チュートリアル"


def test_resolve_known_code_key():
    from tools.ap.operation_tags import resolve_operation_code_key, OperationTag
    op = resolve_operation_code_key("tutorial")
    assert op == OperationTag.TUTORIAL


def test_resolve_unknown_raises_systemexit():
    from tools.ap.operation_tags import resolve_operation_code_key
    with pytest.raises(SystemExit) as exc_info:
        resolve_operation_code_key("invalid_op")
    assert "invalid_op" in str(exc_info.value)


# ─── upsert into lc_tags ───────────────────────────────


def test_upsert_inserts_new_tag(tmp_path):
    from tools.ap.operation_tags import OperationTag, upsert_operation_tag

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    tag_id = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    conn.commit()

    row = conn.execute(
        "SELECT name, code_key, tag_type, is_system FROM lc_tags WHERE id = ?",
        (tag_id,),
    ).fetchone()
    assert row["name"] == "チュートリアル"
    assert row["code_key"] == "tutorial"
    assert row["tag_type"] == "operation"
    assert row["is_system"] == 1
    conn.close()


def test_upsert_idempotent(tmp_path):
    """2 回 upsert を呼んでも 1 件のまま。"""
    from tools.ap.operation_tags import OperationTag, upsert_operation_tag

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))

    tag_id1 = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    tag_id2 = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    conn.commit()

    assert tag_id1 == tag_id2
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE code_key = 'tutorial' AND is_deleted = 0"
    ).fetchone()[0]
    assert cnt == 1
    conn.close()


def test_upsert_syncs_name_change(tmp_path):
    """コード側で name を変更したら DB に同期される。"""
    from tools.ap.operation_tags import (
        OperationTag, OPERATION_TAG_NAMES, upsert_operation_tag,
    )

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))

    # 初回 upsert
    tag_id = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    conn.commit()

    # コード側で name を一時的に書き換え (元に戻す)
    original = OPERATION_TAG_NAMES[OperationTag.TUTORIAL]
    OPERATION_TAG_NAMES[OperationTag.TUTORIAL] = "チュートリアル新名"
    try:
        upsert_operation_tag(conn, OperationTag.TUTORIAL)
        conn.commit()
        new_name = conn.execute(
            "SELECT name FROM lc_tags WHERE id = ?", (tag_id,),
        ).fetchone()[0]
        assert new_name == "チュートリアル新名"
    finally:
        OPERATION_TAG_NAMES[OperationTag.TUTORIAL] = original
        upsert_operation_tag(conn, OperationTag.TUTORIAL)
        conn.commit()
    conn.close()


# ─── lc_sessions に operation_code_key カラムが追加される ────


def test_screen_recorder_adds_operation_columns(tmp_path):
    """ScreenRecorder 起動時に lc_sessions に operation_code_key/operation_tag_id が追加される。"""
    from tools.ap.screen_recorder import ScreenRecorder

    db_path = _setup_db(tmp_path)
    storage = tmp_path / "storage"

    rec = ScreenRecorder(
        db_path=db_path,
        storage_dir=storage,
        session_id="ap_test_001",
        operation_code_key="tutorial",
        operation_tag_id=1,
    )
    cols = {r[1] for r in rec._conn.execute("PRAGMA table_info(lc_sessions)")}
    assert "operation_code_key" in cols
    assert "operation_tag_id" in cols
    rec._conn.close()


def test_screen_recorder_writes_operation_to_session(tmp_path):
    """ScreenRecorder がセッション行に operation_code_key/operation_tag_id を書く。"""
    from tools.ap.operation_tags import OperationTag, upsert_operation_tag
    from tools.ap.screen_recorder import ScreenRecorder

    db_path = _setup_db(tmp_path)
    storage = tmp_path / "storage"

    # tag_id を取得
    conn = sqlite3.connect(str(db_path))
    op_tag_id = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    conn.commit()
    conn.close()

    rec = ScreenRecorder(
        db_path=db_path,
        storage_dir=storage,
        session_id="ap_test_002",
        operation_code_key="tutorial",
        operation_tag_id=op_tag_id,
    )
    row = rec._conn.execute(
        "SELECT operation_code_key, operation_tag_id FROM lc_sessions"
        " WHERE session_id = 'ap_test_002'"
    ).fetchone()
    assert row[0] == "tutorial"
    assert row[1] == op_tag_id
    rec._conn.close()


# ─── マージ時に master_fp に operation tag が付与される ────


def test_merger_assigns_operation_tag_to_master_fps(tmp_path):
    """cross_session_merger が session の operation_tag_id を master_fp に付与する。"""
    from tools.ap.operation_tags import OperationTag, upsert_operation_tag
    from tools.cross_session_merger import CrossSessionMerger

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    op_tag_id = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    # セッション + master_fp + lc_node_mappings を準備
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at, version_id,"
        " operation_code_key, operation_tag_id)"
        " VALUES ('ap_test', datetime('now'), 1, 'tutorial', ?)",
        (op_tag_id,),
    )
    conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, version_id) VALUES ('mfp1', 1), ('mfp2', 1)"
    )
    conn.execute(
        "INSERT INTO lc_node_mappings (session_id, session_fp, master_fp, match_method, version_id)"
        " VALUES ('ap_test', 'sfp1', 'mfp1', 'exact', 1),"
        "        ('ap_test', 'sfp2', 'mfp2', 'exact', 1)"
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    n = merger._assign_operation_tags_for_session('ap_test')
    merger._conn.commit()

    rows = merger._conn.execute(
        "SELECT master_fp, tag_id, assigned_by FROM lc_master_node_tags"
        " WHERE tag_id = ? ORDER BY master_fp", (op_tag_id,),
    ).fetchall()
    merger.close()

    assert n == 2
    assert len(rows) == 2
    assert rows[0]["master_fp"] == "mfp1"
    assert rows[1]["master_fp"] == "mfp2"
    assert rows[0]["assigned_by"] == "auto_pilot"


def test_merger_assignment_is_idempotent(tmp_path):
    """同じ session を 2 回処理しても master_node_tags が重複しない。"""
    from tools.ap.operation_tags import OperationTag, upsert_operation_tag
    from tools.cross_session_merger import CrossSessionMerger

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    op_tag_id = upsert_operation_tag(conn, OperationTag.TUTORIAL)
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at, version_id,"
        " operation_code_key, operation_tag_id)"
        " VALUES ('ap_test', datetime('now'), 1, 'tutorial', ?)",
        (op_tag_id,),
    )
    conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, version_id) VALUES ('mfp1', 1)"
    )
    conn.execute(
        "INSERT INTO lc_node_mappings (session_id, session_fp, master_fp, match_method, version_id)"
        " VALUES ('ap_test', 'sfp1', 'mfp1', 'exact', 1)"
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    merger._assign_operation_tags_for_session('ap_test')
    merger._assign_operation_tags_for_session('ap_test')
    merger._conn.commit()

    cnt = merger._conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'mfp1'"
    ).fetchone()[0]
    merger.close()
    assert cnt == 1


def test_merger_skips_session_with_no_operation(tmp_path):
    """operation_tag_id NULL のセッションは何も付与しない。"""
    from tools.cross_session_merger import CrossSessionMerger

    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at, version_id)"
        " VALUES ('ap_no_op', datetime('now'), 1)"
    )
    conn.execute(
        "INSERT INTO lc_master_nodes (master_fp, version_id) VALUES ('mfp1', 1)"
    )
    conn.execute(
        "INSERT INTO lc_node_mappings (session_id, session_fp, master_fp, match_method, version_id)"
        " VALUES ('ap_no_op', 'sfp1', 'mfp1', 'exact', 1)"
    )
    conn.commit()
    conn.close()

    merger = CrossSessionMerger(db_path)
    n = merger._assign_operation_tags_for_session('ap_no_op')
    merger._conn.commit()
    merger.close()
    assert n == 0
