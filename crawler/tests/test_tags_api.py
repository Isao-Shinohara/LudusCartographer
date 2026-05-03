"""Phase 1: tags.php API のロジックを SQL レベルで検証する。

PHP コード本体は Playwright (Step 5-8) で end-to-end 検証する。
ここでは API が DB に対して行う SQL の挙動を pytest で固める。

設計書: docs/design/master_node_tags.md §5
詳細計画: docs/design/master_node_tags_phase1.md §4
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))


@pytest.fixture
def db_with_tags(tmp_path):
    """migration 済 DB に operation タグを 1 件追加した状態を返す。"""
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
    # operation tag を 1 件追加 (P2 想定の準備)
    bp._conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    bp._conn.commit()
    yield bp._conn
    bp._conn.close()


# ─── 一覧取得 (GET /api/tags.php) ─────────────────────


def test_list_tags_filter_by_type(db_with_tags):
    """type フィルタが効く: scene=11, sub_scene=9, operation=1。"""
    rows_scene = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchall()
    rows_sub = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE tag_type = 'sub_scene' AND is_deleted = 0"
    ).fetchall()
    rows_op = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE tag_type = 'operation' AND is_deleted = 0"
    ).fetchall()
    assert len(rows_scene) == 11
    assert len(rows_sub) == 9
    assert len(rows_op) == 1


def test_list_tags_excludes_deleted_by_default(db_with_tags):
    """include_deleted=0 (デフォルト) で論理削除済みを除外。"""
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1 WHERE name = 'ホーム'"
    )
    rows = db_with_tags.execute(
        "SELECT name FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "ホーム" not in names


def test_list_tags_includes_deleted_when_flag(db_with_tags):
    """include_deleted=1 で論理削除済みも返す。"""
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1 WHERE name = 'ホーム'"
    )
    rows = db_with_tags.execute(
        "SELECT name FROM lc_tags WHERE tag_type = 'scene'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "ホーム" in names


def test_list_tags_assigned_count(db_with_tags):
    """assigned_count: 全 version 横断で COUNT する。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual'),"
        "        ('fp2', 1, 1, 'gemini'),"
        "        ('fp3', 2, 1, 'manual'),"  # version 2 でも同タグ
        "        ('fp4', 1, 2, 'manual')"
    )
    rows = db_with_tags.execute("""
        SELECT t.id, COALESCE(c.cnt, 0) AS assigned_count
        FROM lc_tags t
        LEFT JOIN (
            SELECT tag_id, COUNT(*) AS cnt FROM lc_master_node_tags GROUP BY tag_id
        ) c ON c.tag_id = t.id
        WHERE t.id IN (1, 2)
    """).fetchall()
    counts = {r[0]: r[1] for r in rows}
    assert counts[1] == 3  # version 横断
    assert counts[2] == 1


def test_list_tags_order_by_sort_order(db_with_tags):
    """sort_order 順で返る。"""
    rows = db_with_tags.execute(
        "SELECT name, sort_order FROM lc_tags"
        " WHERE tag_type = 'scene' AND is_deleted = 0"
        " ORDER BY sort_order, id"
    ).fetchall()
    sort_orders = [r[1] for r in rows]
    assert sort_orders == sorted(sort_orders)
    assert rows[0][0] == "ホーム"  # sort_order=0


# ─── タグ作成 (POST /api/tags.php) ─────────────────────


def test_create_tag_scene(db_with_tags):
    cur = db_with_tags.execute(
        "INSERT INTO lc_tags (name, tag_type, description, color, sort_order)"
        " VALUES ('テスト', 'scene', '説明', '#FF0000', 5)"
    )
    assert cur.lastrowid is not None


def test_create_tag_duplicate_active_name_check(db_with_tags):
    """同種別 active で同名チェックする SQL パターン。"""
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_tags"
        " WHERE name = 'ホーム' AND tag_type = 'scene' AND is_deleted = 0"
    ).fetchone()[0]
    assert cnt == 1
    # API はこの値が 0 の時のみ INSERT を許可する責務


def test_create_tag_allows_after_logical_delete(db_with_tags):
    """論理削除済みのタグと同名でも新規作成可能。"""
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1 WHERE name = 'ホーム'"
    )
    cur = db_with_tags.execute(
        "INSERT INTO lc_tags (name, tag_type) VALUES ('ホーム', 'scene')"
    )
    assert cur.lastrowid is not None


# ─── タグ編集 (PUT /api/tags.php?id=...) ───────────────


def test_update_tag_changes_updated_at(db_with_tags):
    db_with_tags.execute(
        "UPDATE lc_tags SET name = '改名', description = '新説明',"
        " color = '#000000', sort_order = 99,"
        " updated_at = datetime('now')"
        " WHERE id = 1 AND is_system = 0 AND is_deleted = 0"
    )
    row = db_with_tags.execute(
        "SELECT name, updated_at FROM lc_tags WHERE id = 1"
    ).fetchone()
    assert row[0] == "改名"
    assert row[1] is not None


def test_update_tag_blocked_for_system_tag(db_with_tags):
    """is_system=1 ガードで操縦カテゴリは UPDATE されない。"""
    op_id = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE code_key = 'tutorial'"
    ).fetchone()[0]
    db_with_tags.execute(
        "UPDATE lc_tags SET name = '別名'"
        " WHERE id = ? AND is_system = 0 AND is_deleted = 0",
        (op_id,),
    )
    name = db_with_tags.execute(
        "SELECT name FROM lc_tags WHERE id = ?", (op_id,)
    ).fetchone()[0]
    assert name == "チュートリアル"  # 変更されていない


# ─── タグ削除 (DELETE /api/tags.php?id=...) ────────────


def test_delete_tag_logical(db_with_tags):
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1, updated_at = datetime('now')"
        " WHERE id = 1 AND is_system = 0 AND is_deleted = 0"
    )
    row = db_with_tags.execute(
        "SELECT is_deleted FROM lc_tags WHERE id = 1"
    ).fetchone()
    assert row[0] == 1


def test_delete_tag_keeps_assignment_records(db_with_tags):
    """論理削除しても付与レコード (lc_master_node_tags) は保持される。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = 1")
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE tag_id = 1"
    ).fetchone()[0]
    assert cnt == 1


def test_delete_tag_blocked_for_system(db_with_tags):
    """is_system=1 ガードで操縦カテゴリは削除されない。"""
    op_id = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE code_key = 'tutorial'"
    ).fetchone()[0]
    db_with_tags.execute(
        "UPDATE lc_tags SET is_deleted = 1"
        " WHERE id = ? AND is_system = 0 AND is_deleted = 0",
        (op_id,),
    )
    is_deleted = db_with_tags.execute(
        "SELECT is_deleted FROM lc_tags WHERE id = ?", (op_id,)
    ).fetchone()[0]
    assert is_deleted == 0


def test_delete_tag_returns_affected_count(db_with_tags):
    """API が「削除前の付与件数」を返すための SQL。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual'),"
        "        ('fp2', 1, 1, 'gemini')"
    )
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE tag_id = 1"
    ).fetchone()[0]
    assert cnt == 2


# ─── ノードタグ操作 (Step 4) ───────────────────────


def _scene_replace_logic(conn, master_fp, version_id, new_tag_id):
    """tags.php の replace_scene_tag と同じ SQL 手続きをエミュレート。"""
    existing = conn.execute(
        "SELECT mnt.id, mnt.tag_id FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = ? AND mnt.version_id = ?"
        "   AND t.tag_type = 'scene'",
        (master_fp, version_id),
    ).fetchall()
    if not existing:
        return
    import json as _json
    old_ids = [r[1] for r in existing]
    if new_tag_id not in old_ids:
        conn.execute(
            "INSERT INTO lc_master_node_tag_history"
            " (master_fp, version_id, event_type, old_tag_ids, new_tag_ids)"
            " VALUES (?, ?, 'manual_scene_replaced', ?, ?)",
            (master_fp, version_id, _json.dumps(old_ids), _json.dumps([new_tag_id])),
        )
    conn.execute(
        "DELETE FROM lc_master_node_tags"
        " WHERE id IN ("
        "   SELECT mnt.id FROM lc_master_node_tags mnt"
        "   JOIN lc_tags t ON t.id = mnt.tag_id"
        "   WHERE mnt.master_fp = ? AND mnt.version_id = ?"
        "     AND t.tag_type = 'scene'"
        " )",
        (master_fp, version_id),
    )


def test_list_node_tags(db_with_tags):
    """ノードに付与されたタグを種別順に取得する。"""
    # シーン (id=1=ホーム) + 詳細 (id=12=ダイアログ想定) + operation (チュートリアル)
    op_id = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE code_key = 'tutorial'"
    ).fetchone()[0]
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual'),"
        "        ('fp1', 1, 12, 'gemini'),"
        "        ('fp1', 1, ?, 'auto_pilot')",
        (op_id,),
    )
    rows = db_with_tags.execute(
        "SELECT t.tag_type, t.name FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND mnt.version_id = 1"
        "   AND t.is_deleted = 0"
        " ORDER BY"
        "   CASE t.tag_type WHEN 'scene' THEN 1 WHEN 'sub_scene' THEN 2 ELSE 3 END,"
        "   t.sort_order, t.id"
    ).fetchall()
    types = [r[0] for r in rows]
    assert types == ['scene', 'sub_scene', 'operation']


def test_list_node_tags_excludes_deleted_tag_definitions(db_with_tags):
    """論理削除されたタグ定義は付与レコードがあっても表示しない。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = 1")
    rows = db_with_tags.execute(
        "SELECT t.id FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND mnt.version_id = 1"
        "   AND t.is_deleted = 0"
    ).fetchall()
    assert len(rows) == 0


def test_assign_tag_scene_replaces_existing(db_with_tags):
    """シーンタグ付与: 既存シーン (id=1=ホーム) → バトル (id=3) への置換。"""
    # 事前: ホーム付与
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    # 置換ロジック実行
    _scene_replace_logic(db_with_tags, 'fp1', 1, 3)
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by, confidence)"
        " VALUES ('fp1', 1, 3, 'manual', 1.0)"
    )

    rows = db_with_tags.execute(
        "SELECT t.id, t.name FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND t.tag_type = 'scene'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 3


def test_assign_scene_tag_records_history(db_with_tags):
    """シーン置換時に lc_master_node_tag_history に manual_scene_replaced 記録。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    _scene_replace_logic(db_with_tags, 'fp1', 1, 3)

    rows = db_with_tags.execute(
        "SELECT event_type, old_tag_ids, new_tag_ids"
        " FROM lc_master_node_tag_history"
        " WHERE master_fp = 'fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'manual_scene_replaced'
    import json
    assert json.loads(rows[0][1]) == [1]
    assert json.loads(rows[0][2]) == [3]


def test_assign_scene_same_tag_no_history(db_with_tags):
    """同じシーンタグの再付与では履歴記録しない (no-op)。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    _scene_replace_logic(db_with_tags, 'fp1', 1, 1)  # 同じ ID
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tag_history WHERE master_fp = 'fp1'"
    ).fetchone()[0]
    assert cnt == 0


def test_assign_sub_scene_does_not_replace(db_with_tags):
    """詳細タグは複数付与可能 (置換しない)。"""
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 12, 'manual')"  # ダイアログ想定
    )
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by) VALUES ('fp1', 1, 13, 'manual')"
    )
    rows = db_with_tags.execute(
        "SELECT t.id FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND t.tag_type = 'sub_scene'"
    ).fetchall()
    assert len(rows) == 2


def test_assign_duplicate_tag_no_op(db_with_tags):
    """同タグ二重付与は UNIQUE 制約で no-op。"""
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by) VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute(
        "INSERT OR IGNORE INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by) VALUES ('fp1', 1, 1, 'manual')"
    )
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'fp1' AND tag_id = 1"
    ).fetchone()[0]
    assert cnt == 1


def test_unassign_records_history(db_with_tags):
    """手動解除時に lc_master_node_tag_history に manual_unassigned 記録。"""
    import json
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tag_history"
        " (master_fp, version_id, event_type, old_tag_ids)"
        " VALUES ('fp1', 1, 'manual_unassigned', ?)",
        (json.dumps([1]),),
    )
    db_with_tags.execute(
        "DELETE FROM lc_master_node_tags"
        " WHERE master_fp = 'fp1' AND version_id = 1 AND tag_id = 1"
    )
    rows = db_with_tags.execute(
        "SELECT event_type, old_tag_ids FROM lc_master_node_tag_history"
        " WHERE master_fp = 'fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 'manual_unassigned'
    assert json.loads(rows[0][1]) == [1]
    cnt = db_with_tags.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp = 'fp1' AND tag_id = 1"
    ).fetchone()[0]
    assert cnt == 0


def test_cannot_unassign_system_tag(db_with_tags):
    """is_system=1 タグ (操縦カテゴリ) は API 側でガード。SQL チェックパターン。"""
    op_id = db_with_tags.execute(
        "SELECT id FROM lc_tags WHERE code_key = 'tutorial'"
    ).fetchone()[0]
    db_with_tags.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, ?, 'auto_pilot')",
        (op_id,),
    )
    is_system = db_with_tags.execute(
        "SELECT t.is_system FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' AND mnt.tag_id = ?",
        (op_id,),
    ).fetchone()[0]
    assert is_system == 1
    # API はこれを 403 で返す責務
