"""Phase 1: タグ機能のスキーマ migration & 制約テスト。

設計書: docs/design/master_node_tags.md
詳細計画: docs/design/master_node_tags_phase1.md
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# crawler/ をモジュール検索パスに追加 (他テストと同じパターン)
_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))


@pytest.fixture
def fresh_db(tmp_path):
    """前提テーブル作成 + migration を走らせて conn を返す。

    BatchProcessor._migrate() は lc_screens 等が既に存在することを前提とする
    (screen_recorder が先に作るため)。テストでは事前に骨格を用意する。
    """
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
    yield bp._conn, db_path
    bp._conn.close()


# ─── Migration: テーブル作成 ─────────────────────────────


def test_migration_creates_lc_tags(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lc_tags'"
    ).fetchall()
    assert len(rows) == 1


def test_migration_creates_lc_master_node_tags(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='lc_master_node_tags'"
    ).fetchall()
    assert len(rows) == 1


def test_migration_creates_lc_tag_judgments(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='lc_tag_judgments'"
    ).fetchall()
    assert len(rows) == 1


def test_migration_creates_lc_master_node_tag_history(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='lc_master_node_tag_history'"
    ).fetchall()
    assert len(rows) == 1


def test_migration_creates_lc_tag_prompts(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name='lc_tag_prompts'"
    ).fetchall()
    assert len(rows) == 1


# ─── Migration: index 作成 ───────────────────────────────


def test_migration_creates_indexes(fresh_db):
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
        " AND name LIKE 'idx_%'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_tags_type" in names
    assert "idx_tags_code_key_active" in names
    assert "idx_mnt_master" in names
    assert "idx_mnt_tag" in names
    assert "idx_mnt_assigned_by" in names
    assert "idx_tj_master" in names
    assert "idx_mnth_master" in names


# ─── Migration: 冪等性 ─────────────────────────────────


def test_migration_idempotent(fresh_db):
    """2 回 migration を回しても初期データ件数が変わらない。"""
    from tools.batch_processor import BatchProcessor

    conn, db_path = fresh_db
    before = conn.execute(
        "SELECT COUNT(*) FROM lc_tags"
    ).fetchone()[0]

    # 同じ DB で再度 BatchProcessor を作成 (= _migrate() 再実行)
    bp2 = BatchProcessor(db_path=db_path)
    after = bp2._conn.execute(
        "SELECT COUNT(*) FROM lc_tags"
    ).fetchone()[0]
    bp2._conn.close()

    assert before == after


# ─── 初期データ ──────────────────────────────────────


def test_initial_scene_tags_count(fresh_db):
    """シーンタグ初期 11 件。"""
    conn, _ = fresh_db
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchone()[0]
    assert cnt == 11


def test_initial_sub_scene_tags_count(fresh_db):
    """詳細タグ初期 9 件。"""
    conn, _ = fresh_db
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE tag_type = 'sub_scene' AND is_deleted = 0"
    ).fetchone()[0]
    assert cnt == 9


def test_no_initial_operation_tags(fresh_db):
    """操縦カテゴリは P1 では初期データなし (P2 で auto_pilot 起動時に追加)。"""
    conn, _ = fresh_db
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tags WHERE tag_type = 'operation'"
    ).fetchone()[0]
    assert cnt == 0


def test_initial_tags_have_color_and_sort_order(fresh_db):
    """シーンタグ全件に color / sort_order が設定される。"""
    conn, _ = fresh_db
    rows = conn.execute(
        "SELECT name, color, sort_order FROM lc_tags"
        " WHERE tag_type = 'scene' AND is_deleted = 0"
    ).fetchall()
    for name, color, sort_order in rows:
        assert color is not None and color.startswith("#"), f"{name} の color 異常: {color}"
        assert sort_order is not None


def test_initial_data_does_not_overwrite_user_edit(fresh_db):
    """既存タグの description をユーザー編集後の再 migration で上書きしない。"""
    from tools.batch_processor import BatchProcessor

    conn, db_path = fresh_db
    conn.execute(
        "UPDATE lc_tags SET description = 'ユーザー編集' WHERE name = 'ホーム'"
    )
    conn.commit()

    bp2 = BatchProcessor(db_path=db_path)
    desc = bp2._conn.execute(
        "SELECT description FROM lc_tags WHERE name = 'ホーム' AND tag_type = 'scene'"
    ).fetchone()[0]
    bp2._conn.close()

    assert desc == "ユーザー編集"


# ─── 制約: tag_type CHECK ─────────────────────────────


def test_tag_type_check_rejects_invalid(fresh_db):
    conn, _ = fresh_db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_tags (name, tag_type) VALUES ('x', 'invalid_type')"
        )


def test_tag_type_check_accepts_operation(fresh_db):
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('test_op', 'テスト操縦', 'operation', 1)"
    )


# ─── 制約: code_key 部分 UNIQUE ───────────────────────


def test_code_key_unique_among_active(fresh_db):
    """同 code_key の active 重複は禁止。"""
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
            " VALUES ('tutorial', '別名', 'operation', 1)"
        )


def test_code_key_reusable_after_logical_delete(fresh_db):
    """論理削除済みの code_key は再利用可能 (部分 UNIQUE のため)。"""
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system, is_deleted)"
        " VALUES ('old_op', 'old', 'operation', 1, 1)"
    )
    # 同 code_key で active 挿入できる
    conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('old_op', 'new', 'operation', 1)"
    )


# ─── 制約: lc_master_node_tags UNIQUE ────────────────


def test_master_node_tags_unique(fresh_db):
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_master_node_tags"
            " (master_fp, version_id, tag_id, assigned_by)"
            " VALUES ('fp1', 1, 1, 'manual')"
        )


def test_master_node_tags_unique_allows_different_versions(fresh_db):
    """version_id が違えば同 (master_fp, tag_id) の付与可能。"""
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    conn.execute(
        "INSERT INTO lc_master_node_tags"
        " (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 2, 1, 'manual')"
    )


def test_assigned_by_check_constraint(fresh_db):
    conn, _ = fresh_db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_master_node_tags"
            " (master_fp, version_id, tag_id, assigned_by)"
            " VALUES ('fp1', 1, 1, 'invalid_source')"
        )


# ─── 制約: lc_tag_judgments UNIQUE ───────────────────


def test_tag_judgments_unique(fresh_db):
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tag_judgments"
        " (master_fp, tag_type, prompt_hash, result_json, model)"
        " VALUES ('fp1', 'scene', 'hash1', '{}', 'gemini-2.5-flash-lite')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_tag_judgments"
            " (master_fp, tag_type, prompt_hash, result_json, model)"
            " VALUES ('fp1', 'scene', 'hash1', '{}', 'gemini-2.5-flash-lite')"
        )


def test_tag_judgments_distinct_by_model(fresh_db):
    """同 (master_fp, tag_type, prompt_hash) でも model が違えば別エントリ。"""
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tag_judgments"
        " (master_fp, tag_type, prompt_hash, result_json, model)"
        " VALUES ('fp1', 'scene', 'hash1', '{}', 'gemini-2.5-flash-lite')"
    )
    conn.execute(
        "INSERT INTO lc_tag_judgments"
        " (master_fp, tag_type, prompt_hash, result_json, model)"
        " VALUES ('fp1', 'scene', 'hash1', '{}', 'gemini-2.5-flash')"
    )


def test_tag_judgments_type_check(fresh_db):
    conn, _ = fresh_db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_tag_judgments"
            " (master_fp, tag_type, prompt_hash, result_json, model)"
            " VALUES ('fp1', 'operation', 'hash1', '{}', 'm1')"
        )


# ─── 制約: lc_tag_prompts UNIQUE ─────────────────────


def test_tag_prompts_unique_tag_type(fresh_db):
    conn, _ = fresh_db
    conn.execute(
        "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
        " VALUES ('scene', 'テストプロンプト', 1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
            " VALUES ('scene', '別のプロンプト', 1)"
        )
