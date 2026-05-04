"""Phase 6: タグ一括付与 + deprecated 表示の SQL レベル検証。

API: POST /api/tags.php?action=bulk_assign&tag_id=N&version_id=N
     body: {master_fps: [...]}

PHP 側のロジックを SQL レベルでエミュレートしてテストする。
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
        bp._conn.execute("INSERT INTO lc_versions (name, is_active) VALUES ('v1', 1)")
        bp._conn.commit()
    bp._conn.close()
    return db_path


def _seed_masters(db_path, fps):
    conn = sqlite3.connect(str(db_path))
    for fp in fps:
        conn.execute(
            "INSERT OR IGNORE INTO lc_master_nodes (master_fp, version_id)"
            " VALUES (?, 1)", (fp,),
        )
    conn.commit()
    conn.close()


def _bulk_assign(conn, tag_id, version_id, master_fps):
    """tags.php の handle_bulk_assign を Python でエミュレート。"""
    tag = conn.execute(
        "SELECT tag_type, is_system, is_deleted FROM lc_tags WHERE id = ?",
        (tag_id,),
    ).fetchone()
    if not tag or tag[2] == 1:
        return {"error": "not_found"}
    if tag[1] == 1:
        return {"error": "system_tag_modification_forbidden"}
    tag_type = tag[0]
    assigned = 0
    skipped = 0
    for fp in master_fps:
        # master_fp が実在するか
        exists = conn.execute(
            "SELECT 1 FROM lc_master_nodes WHERE master_fp = ? AND version_id = ?",
            (fp, version_id),
        ).fetchone()
        if not exists:
            skipped += 1
            continue
        if tag_type == "scene":
            # シーンタグ置換 (既存削除)
            existing = conn.execute(
                "SELECT mnt.id FROM lc_master_node_tags mnt"
                " JOIN lc_tags t ON t.id = mnt.tag_id"
                " WHERE mnt.master_fp = ? AND mnt.version_id = ?"
                "   AND t.tag_type = 'scene' AND mnt.tag_id != ?",
                (fp, version_id, tag_id),
            ).fetchall()
            if existing:
                conn.execute(
                    "DELETE FROM lc_master_node_tags"
                    " WHERE id IN (" + ",".join(["?"] * len(existing)) + ")",
                    [r[0] for r in existing],
                )
        cur = conn.execute(
            "INSERT OR IGNORE INTO lc_master_node_tags"
            " (master_fp, version_id, tag_id, assigned_by, confidence, assigned_at)"
            " VALUES (?, ?, ?, 'manual', 1.0, datetime('now'))",
            (fp, version_id, tag_id),
        )
        if cur.rowcount > 0:
            assigned += 1
        else:
            skipped += 1
    return {"assigned": assigned, "skipped": skipped, "tag_type": tag_type}


# ─── 一括付与: 基本 ─────────────────────────────────


def test_bulk_assign_inserts_for_all_master_fps(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1", "fp2", "fp3"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    res = _bulk_assign(conn, home_id, 1, ["fp1", "fp2", "fp3"])
    conn.commit()
    assert res["assigned"] == 3
    assert res["skipped"] == 0
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE tag_id = ?", (home_id,)
    ).fetchone()[0]
    assert cnt == 3
    conn.close()


def test_bulk_assign_skips_nonexistent_master_fp(tmp_path):
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    res = _bulk_assign(conn, home_id, 1, ["fp1", "fp_unknown"])
    conn.commit()
    assert res["assigned"] == 1
    assert res["skipped"] == 1
    conn.close()


def test_bulk_assign_idempotent(tmp_path):
    """2 回実行しても 1 件しか付かない (UNIQUE 制約)。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    _bulk_assign(conn, home_id, 1, ["fp1"])
    res = _bulk_assign(conn, home_id, 1, ["fp1"])
    conn.commit()
    # 同 (master_fp, version_id, tag_id) は INSERT OR IGNORE で no-op
    assert res["assigned"] == 0
    assert res["skipped"] == 1
    conn.close()


def test_bulk_assign_scene_replaces_existing(tmp_path):
    """シーンタグ一括付与で既存シーンタグが置換される。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    battle_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='バトル' AND tag_type='scene'"
    ).fetchone()[0]
    # 既存: ホーム
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'manual')", (home_id,),
    )
    # 一括付与: バトル
    _bulk_assign(conn, battle_id, 1, ["fp1"])
    conn.commit()
    rows = conn.execute(
        "SELECT t.id, t.tag_type FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND t.tag_type = 'scene'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == battle_id
    conn.close()


def test_bulk_assign_sub_scene_does_not_replace(tmp_path):
    """詳細タグは置換しない (複数付与可能)。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    dialog_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ダイアログ' AND tag_type='sub_scene'"
    ).fetchone()[0]
    notice_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='お知らせ' AND tag_type='sub_scene'"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'manual')", (dialog_id,),
    )
    _bulk_assign(conn, notice_id, 1, ["fp1"])
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    assert sorted([r[0] for r in rows]) == sorted([dialog_id, notice_id])
    conn.close()


def test_bulk_assign_rejects_system_tag(tmp_path):
    """is_system=1 タグは一括付与不可。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    op_id = cur.lastrowid
    res = _bulk_assign(conn, op_id, 1, ["fp1"])
    conn.commit()
    assert res.get("error") == "system_tag_modification_forbidden"
    conn.close()


def test_bulk_assign_rejects_deleted_tag(tmp_path):
    """is_deleted=1 タグは付与不可。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    conn.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = ?", (home_id,))
    conn.commit()
    res = _bulk_assign(conn, home_id, 1, ["fp1"])
    assert res.get("error") == "not_found"
    conn.close()


def test_bulk_assign_assigned_by_manual(tmp_path):
    """assigned_by='manual' で記録される (Gemini 再判定で保護)。"""
    db_path = _setup_db(tmp_path)
    _seed_masters(db_path, ["fp1"])
    conn = sqlite3.connect(str(db_path))
    home_id = conn.execute(
        "SELECT id FROM lc_tags WHERE name='ホーム' AND tag_type='scene'"
    ).fetchone()[0]
    _bulk_assign(conn, home_id, 1, ["fp1"])
    conn.commit()
    row = conn.execute(
        "SELECT assigned_by, confidence FROM lc_master_node_tags"
        " WHERE master_fp='fp1' AND tag_id = ?", (home_id,),
    ).fetchone()
    assert row[0] == "manual"
    assert row[1] == 1.0
    conn.close()


# ─── deprecated 表示用 SQL ──────────────────────────────


def test_list_with_include_deleted_returns_logical_deleted(tmp_path):
    """include_deleted=1 で論理削除されたタグも返る。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    cur = conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system, is_deleted)"
        " VALUES ('old_op', '旧操縦', 'operation', 1, 1)"
    )
    deleted_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    active_id = cur.lastrowid
    conn.commit()

    # include_deleted=1 で取得
    rows = conn.execute(
        "SELECT id, name, is_deleted FROM lc_tags"
        " WHERE tag_type = 'operation'"
    ).fetchall()
    ids = {r[0] for r in rows}
    assert deleted_id in ids
    assert active_id in ids
    deleted_row = next(r for r in rows if r[0] == deleted_id)
    assert deleted_row[2] == 1
    conn.close()
