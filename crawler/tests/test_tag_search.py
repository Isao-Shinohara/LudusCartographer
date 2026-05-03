"""Phase 5: タグによる master_fp 検索。

設計書: docs/design/master_node_tags.md §1 (将来) / §11 (検索機能との統合)

タグ検索 API のロジックを SQL レベルで検証する。
PHP 側の tags.php に ?action=search_master_fps として実装される。

入力: version_id, tag_ids (array), match (AND/OR), within_type (per-type OR for AND)
出力: master_fp 一覧
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
    # operation tag (tutorial) を仕込む
    bp._conn.execute(
        "INSERT INTO lc_tags (code_key, name, tag_type, is_system)"
        " VALUES ('tutorial', 'チュートリアル', 'operation', 1)"
    )
    bp._conn.commit()
    bp._conn.close()
    return db_path


def _get_tag_id(conn, name, tag_type):
    return conn.execute(
        "SELECT id FROM lc_tags WHERE name = ? AND tag_type = ?",
        (name, tag_type),
    ).fetchone()[0]


def _seed_master_with_tags(conn, master_fp, tags, version_id=1):
    """master_fp と (tag_id, assigned_by) のリストで付与。"""
    conn.execute(
        "INSERT OR IGNORE INTO lc_master_nodes (master_fp, version_id)"
        " VALUES (?, ?)", (master_fp, version_id),
    )
    for tag_id, assigned_by in tags:
        conn.execute(
            "INSERT OR IGNORE INTO lc_master_node_tags"
            " (master_fp, version_id, tag_id, assigned_by, confidence)"
            " VALUES (?, ?, ?, ?, 1.0)",
            (master_fp, version_id, tag_id, assigned_by),
        )


# ─── 検索ロジックのリファレンス実装 ──────────────────


def search_master_fps(
    conn: sqlite3.Connection, version_id: int,
    tag_ids: list[int], match: str = "and",
) -> list[str]:
    """指定タグ群が付与されている master_fp を返す。

    match='and': 全タグが付与されている master_fp のみ
    match='or':  いずれか 1 つでも付与されている master_fp

    is_deleted=1 のタグは除外。lc_master_nodes 自体に存在する master_fp に
    限定する (= 検索対象は実在ノードのみ)。
    """
    if not tag_ids:
        rows = conn.execute(
            "SELECT master_fp FROM lc_master_nodes WHERE version_id = ?"
            " ORDER BY sort_order, master_fp",
            (version_id,),
        ).fetchall()
        return [r[0] for r in rows]

    placeholders = ",".join(["?"] * len(tag_ids))
    if match == "and":
        sql = (
            f"SELECT mnt.master_fp FROM lc_master_node_tags mnt"
            f" JOIN lc_tags t ON t.id = mnt.tag_id"
            f" JOIN lc_master_nodes m ON m.master_fp = mnt.master_fp"
            f"   AND m.version_id = mnt.version_id"
            f" WHERE mnt.version_id = ?"
            f"   AND mnt.tag_id IN ({placeholders})"
            f"   AND t.is_deleted = 0"
            f" GROUP BY mnt.master_fp"
            f" HAVING COUNT(DISTINCT mnt.tag_id) = ?"
            f" ORDER BY MIN(m.sort_order), mnt.master_fp"
        )
        params = (version_id, *tag_ids, len(tag_ids))
    else:
        sql = (
            f"SELECT DISTINCT mnt.master_fp FROM lc_master_node_tags mnt"
            f" JOIN lc_tags t ON t.id = mnt.tag_id"
            f" JOIN lc_master_nodes m ON m.master_fp = mnt.master_fp"
            f"   AND m.version_id = mnt.version_id"
            f" WHERE mnt.version_id = ?"
            f"   AND mnt.tag_id IN ({placeholders})"
            f"   AND t.is_deleted = 0"
            f" ORDER BY m.sort_order, mnt.master_fp"
        )
        params = (version_id, *tag_ids)
    rows = conn.execute(sql, params).fetchall()
    return [r[0] for r in rows]


def search_master_fps_grouped(
    conn: sqlite3.Connection, version_id: int,
    tag_ids_by_type: dict[str, list[int]],
) -> list[str]:
    """種別ごとに OR、種別間で AND の絞り込み。

    tag_ids_by_type = {"operation": [1], "scene": [3, 4], "sub_scene": [12]}
    → (op IN (1)) AND (scene IN (3, 4)) AND (sub_scene IN (12))

    各種別について空配列はその種別をフィルタしない (制約なし)。
    全種別空 → 全件返す。
    """
    active = {k: v for k, v in tag_ids_by_type.items() if v}
    if not active:
        rows = conn.execute(
            "SELECT master_fp FROM lc_master_nodes WHERE version_id = ?"
            " ORDER BY sort_order, master_fp",
            (version_id,),
        ).fetchall()
        return [r[0] for r in rows]

    # 各種別ごとに master_fp の集合を計算 → 共通集合を取る
    sets = []
    for tag_type, ids in active.items():
        if not ids:
            continue
        placeholders = ",".join(["?"] * len(ids))
        rows = conn.execute(
            f"SELECT DISTINCT mnt.master_fp FROM lc_master_node_tags mnt"
            f" JOIN lc_tags t ON t.id = mnt.tag_id"
            f" WHERE mnt.version_id = ?"
            f"   AND mnt.tag_id IN ({placeholders})"
            f"   AND t.tag_type = ?"
            f"   AND t.is_deleted = 0",
            (version_id, *ids, tag_type),
        ).fetchall()
        sets.append({r[0] for r in rows})

    if not sets:
        return []
    matched = sets[0]
    for s in sets[1:]:
        matched = matched & s
    if not matched:
        return []

    placeholders = ",".join(["?"] * len(matched))
    rows = conn.execute(
        f"SELECT master_fp FROM lc_master_nodes"
        f" WHERE master_fp IN ({placeholders}) AND version_id = ?"
        f" ORDER BY sort_order, master_fp",
        (*matched, version_id),
    ).fetchall()
    return [r[0] for r in rows]


# ─── AND ロジック ──────────────────────────────────


def test_search_and_returns_master_with_all_tags(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    dialog_id = _get_tag_id(conn, "ダイアログ", "sub_scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual"), (dialog_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [(home_id, "manual")])  # ダイアログなし
    conn.commit()

    result = search_master_fps(conn, 1, [home_id, dialog_id], match="and")
    assert result == ["fp1"]
    conn.close()


def test_search_and_excludes_partial_match(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    dialog_id = _get_tag_id(conn, "ダイアログ", "sub_scene")
    notice_id = _get_tag_id(conn, "お知らせ", "sub_scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual"), (dialog_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [(home_id, "manual"), (notice_id, "manual")])
    conn.commit()

    result = search_master_fps(conn, 1, [home_id, dialog_id], match="and")
    assert result == ["fp1"]
    conn.close()


def test_search_and_empty_tags_returns_all(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [])
    conn.commit()

    result = search_master_fps(conn, 1, [], match="and")
    assert sorted(result) == ["fp1", "fp2"]
    conn.close()


# ─── OR ロジック ───────────────────────────────────


def test_search_or_returns_any_match(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    battle_id = _get_tag_id(conn, "バトル", "scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [(battle_id, "manual")])
    _seed_master_with_tags(conn, "fp3", [])  # タグなし
    conn.commit()

    result = search_master_fps(conn, 1, [home_id, battle_id], match="or")
    assert sorted(result) == ["fp1", "fp2"]
    conn.close()


def test_search_or_distinct_master_fps(tmp_path):
    """同 master_fp に複数タグ付与されていても重複しない。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    dialog_id = _get_tag_id(conn, "ダイアログ", "sub_scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual"), (dialog_id, "manual")])
    conn.commit()

    result = search_master_fps(conn, 1, [home_id, dialog_id], match="or")
    assert result == ["fp1"]
    conn.close()


# ─── タグ削除 (is_deleted) ────────────────────────────


def test_search_excludes_deleted_tags(tmp_path):
    """論理削除されたタグは検索条件から除外される。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])
    conn.execute("UPDATE lc_tags SET is_deleted = 1 WHERE id = ?", (home_id,))
    conn.commit()

    result = search_master_fps(conn, 1, [home_id], match="and")
    assert result == []
    conn.close()


# ─── version 分離 ─────────────────────────────────────


def test_search_filters_by_version(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO lc_versions (name, is_active) VALUES ('v2', 0)")
    home_id = _get_tag_id(conn, "ホーム", "scene")
    _seed_master_with_tags(conn, "fp_v1", [(home_id, "manual")], version_id=1)
    _seed_master_with_tags(conn, "fp_v2", [(home_id, "manual")], version_id=2)
    conn.commit()

    r1 = search_master_fps(conn, 1, [home_id], match="and")
    r2 = search_master_fps(conn, 2, [home_id], match="and")
    assert r1 == ["fp_v1"]
    assert r2 == ["fp_v2"]
    conn.close()


# ─── 種別ごと OR + 種別間 AND (グループ検索) ──────────


def test_search_grouped_per_type_or_cross_type_and(tmp_path):
    """(scene IN (...)) AND (sub_scene IN (...)) のロジック。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    battle_id = _get_tag_id(conn, "バトル", "scene")
    dialog_id = _get_tag_id(conn, "ダイアログ", "sub_scene")
    notice_id = _get_tag_id(conn, "お知らせ", "sub_scene")

    # fp1: ホーム + ダイアログ → MATCH (scene OR + sub_scene OR)
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual"), (dialog_id, "manual")])
    # fp2: バトル + お知らせ → MATCH
    _seed_master_with_tags(conn, "fp2", [(battle_id, "manual"), (notice_id, "manual")])
    # fp3: ホームのみ → 不一致 (sub_scene 未満たし)
    _seed_master_with_tags(conn, "fp3", [(home_id, "manual")])
    # fp4: ダイアログのみ → 不一致 (scene 未満たし)
    _seed_master_with_tags(conn, "fp4", [(dialog_id, "manual")])
    conn.commit()

    result = search_master_fps_grouped(conn, 1, {
        "scene": [home_id, battle_id],
        "sub_scene": [dialog_id, notice_id],
    })
    assert sorted(result) == ["fp1", "fp2"]
    conn.close()


def test_search_grouped_empty_type_means_no_constraint(tmp_path):
    """空配列の種別はフィルタしない (= その種別の制約なし)。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [])
    conn.commit()

    # operation/sub_scene は空、scene のみ指定
    result = search_master_fps_grouped(conn, 1, {
        "operation": [],
        "scene": [home_id],
        "sub_scene": [],
    })
    assert result == ["fp1"]
    conn.close()


def test_search_grouped_all_empty_returns_all(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [])
    conn.commit()

    result = search_master_fps_grouped(conn, 1, {})
    assert sorted(result) == ["fp1", "fp2"]
    conn.close()


def test_search_grouped_with_operation_tag(tmp_path):
    """操縦カテゴリ + シーンタグの組み合わせ。"""
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    op_id = _get_tag_id(conn, "チュートリアル", "operation")
    home_id = _get_tag_id(conn, "ホーム", "scene")
    battle_id = _get_tag_id(conn, "バトル", "scene")

    _seed_master_with_tags(conn, "fp1", [(op_id, "auto_pilot"), (home_id, "manual")])
    _seed_master_with_tags(conn, "fp2", [(op_id, "auto_pilot"), (battle_id, "manual")])
    _seed_master_with_tags(conn, "fp3", [(home_id, "manual")])  # operation なし
    conn.commit()

    result = search_master_fps_grouped(conn, 1, {
        "operation": [op_id],
        "scene": [home_id],
    })
    assert result == ["fp1"]
    conn.close()


def test_search_grouped_no_match_returns_empty(tmp_path):
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    home_id = _get_tag_id(conn, "ホーム", "scene")
    dialog_id = _get_tag_id(conn, "ダイアログ", "sub_scene")
    _seed_master_with_tags(conn, "fp1", [(home_id, "manual")])  # ダイアログなし
    conn.commit()

    result = search_master_fps_grouped(conn, 1, {
        "scene": [home_id],
        "sub_scene": [dialog_id],
    })
    assert result == []
    conn.close()
