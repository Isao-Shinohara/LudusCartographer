"""Phase 4: 詳細 (sub_scene) タグの Gemini 判定テスト。

Phase 3 で実装した tag_judgment.py のロジックが sub_scene にも
適切に動作することを確認する。

設計書: docs/design/master_node_tags.md §4.3 §8.2
詳細計画: docs/design/master_node_tags_phase1.md §11 (P4 スコープ)
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

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


def _seed_master(db_path, master_fp, screen_id=100):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO lc_screens (id, fingerprint, ocr_text, scene)"
        " VALUES (?, ?, 'dummy', 'BATTLE')",
        (screen_id, master_fp),
    )
    conn.execute(
        "INSERT INTO lc_master_nodes"
        " (master_fp, representative_screen_id, scene, version_id)"
        " VALUES (?, ?, 'BATTLE', 1)",
        (master_fp, screen_id),
    )
    conn.commit()
    conn.close()


# ─── モデル選択 ───────────────────────────────────────


def test_subscene_uses_flash_model():
    from tools.tag_judgment import MODEL_BY_TYPE
    assert MODEL_BY_TYPE["sub_scene"] == "gemini-2.5-flash"
    assert MODEL_BY_TYPE["scene"] == "gemini-2.5-flash-lite"


def test_subscene_purpose_distinct():
    from tools.tag_judgment import PURPOSE_BY_TYPE
    assert PURPOSE_BY_TYPE["sub_scene"] == "tag_subscene_judgment"
    assert PURPOSE_BY_TYPE["scene"] == "tag_scene_judgment"


# ─── デフォルトプロンプト ──────────────────────────────


def test_subscene_default_prompt_contains_multi_tag_guidance():
    from tools.tag_judgment import DEFAULT_PROMPTS
    p = DEFAULT_PROMPTS["sub_scene"]
    assert "0 個以上" in p or "全て" in p
    assert "{tag_candidates}" in p
    assert "{ocr_text}" in p


def test_subscene_render_prompt_substitutes(tmp_path):
    from tools.tag_judgment import render_prompt, DEFAULT_PROMPTS
    template = DEFAULT_PROMPTS["sub_scene"]
    out = render_prompt(template,
                        [{"id": 12, "name": "ダイアログ", "description": "OK/キャンセル"}],
                        "BATTLE", "Wave 1")
    assert "id=12" in out
    assert "ダイアログ" in out


# ─── 候補タグ取得 ─────────────────────────────────────


def test_fetch_subscene_candidates(tmp_path):
    from tools.tag_judgment import fetch_candidate_tags
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    tags = fetch_candidate_tags(conn, "sub_scene")
    assert len(tags) == 9  # 初期データ
    names = {t["name"] for t in tags}
    assert "ダイアログ" in names
    assert "ログインボーナス" in names
    conn.close()


# ─── apply_judgment (詳細タグの 0+ 配列) ──────────────


def test_subscene_applies_multiple_tags(tmp_path):
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [12, 13, 14]},
                   {12, 13, 14, 15, 16}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    assert [r["tag_id"] for r in rows] == [12, 13, 14]
    conn.close()


def test_subscene_empty_array_is_valid(tmp_path):
    """0 個でも valid (= 該当なし)。"""
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": []},
                   {12, 13, 14}, reset_manual=False)
    conn.commit()
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchone()[0]
    assert cnt == 0
    conn.close()


def test_subscene_replaces_existing_gemini_tags(tmp_path):
    """既存の gemini sub_scene タグを全削除して新規付与。"""
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 既存 gemini sub_scene
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 12, 'gemini'), ('fp1', 1, 13, 'gemini')"
    )
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [14, 15]},
                   {12, 13, 14, 15, 16}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    # 12,13 は削除されて 14,15 のみ残る
    assert [r["tag_id"] for r in rows] == [14, 15]
    conn.close()


def test_subscene_does_not_touch_scene_tags(tmp_path):
    """sub_scene 判定で scene タグを破壊しない。"""
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 既存 scene タグ (id=1=ホーム)
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'gemini')"
    )
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [12]},
                   {12, 13, 14}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT t.name, t.tag_type FROM lc_master_node_tags mnt"
        " JOIN lc_tags t ON t.id = mnt.tag_id"
        " WHERE mnt.master_fp = 'fp1' ORDER BY t.tag_type"
    ).fetchall()
    types = {r["tag_type"] for r in rows}
    assert "scene" in types  # scene 残ってる
    assert "sub_scene" in types  # sub_scene も付与された
    conn.close()


def test_subscene_preserves_manual_tags(tmp_path):
    """reset_manual=False のとき manual sub_scene タグは保持される。"""
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 12, 'manual'), ('fp1', 1, 13, 'gemini')"
    )
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [14]},
                   {12, 13, 14, 15}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags"
        " WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    # 12 (manual) は保持、13 (gemini) は削除、14 (gemini 新規) が付与
    assert [(r["tag_id"], r["assigned_by"]) for r in rows] == [
        (12, "manual"), (14, "gemini"),
    ]
    conn.close()


def test_subscene_filters_invalid_tag_ids(tmp_path):
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 12 は valid、999 / 1000 は invalid
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [12, 999, 1000]},
                   {12, 13}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchall()
    assert [r["tag_id"] for r in rows] == [12]  # invalid は無視
    conn.close()


# ─── run_judgment エンドツーエンド (sub_scene) ────────


def test_subscene_run_uses_flash_model(tmp_path):
    from tools.tag_judgment import run_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")

    captured_models = []

    def _mock(model, prompt, api_key):
        captured_models.append(model)
        return ({"tag_ids": [12, 13], "confidence": 0.8}, 200, 50, None)

    with patch("tools.tag_judgment.call_gemini", side_effect=_mock):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fake"}, clear=False):
            result = run_judgment(db_path, "sub_scene", "unassigned", False, 1)

    assert result["ok"]
    assert result["summary"]["api_calls"] == 1
    assert captured_models == ["gemini-2.5-flash"]

    # api_usage に sub_scene の purpose が記録されている
    conn = sqlite3.connect(str(db_path))
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_api_usage WHERE purpose='tag_subscene_judgment'"
    ).fetchone()[0]
    assert cnt == 1
    conn.close()


def test_subscene_run_writes_multiple_tags(tmp_path):
    from tools.tag_judgment import run_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")

    def _mock(model, prompt, api_key):
        return ({"tag_ids": [12, 13, 14], "confidence": 0.9}, 100, 30, None)

    with patch("tools.tag_judgment.call_gemini", side_effect=_mock):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fake"}, clear=False):
            result = run_judgment(db_path, "sub_scene", "unassigned", False, 1)

    assert result["ok"]
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    assert [r[0] for r in rows] == [12, 13, 14]
    conn.close()


def test_subscene_estimate(tmp_path):
    from tools.tag_judgment import estimate_targets
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    _seed_master(db_path, "fp2", screen_id=102)
    est = estimate_targets(db_path, "sub_scene", "unassigned", False, 1)
    assert est["target_count"] == 2
    assert est["model"] == "gemini-2.5-flash"
