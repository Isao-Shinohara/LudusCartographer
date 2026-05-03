"""Phase 3: tag_judgment.py の単体テスト。

Gemini API は urllib をモックして実行する。
DB 書き込み・キャッシュ・apply_judgment ロジックを担保する。
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
    """前提テーブル + tag migration 済 DB を返す (path)。"""
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


def _seed_master(db_path, master_fp, ocr="dummy", scene="MENU", screen_id=100,
                 version_id=1):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO lc_screens (id, fingerprint, ocr_text, scene)"
        " VALUES (?, ?, ?, ?)",
        (screen_id, master_fp, ocr, scene),
    )
    conn.execute(
        "INSERT INTO lc_master_nodes"
        " (master_fp, representative_screen_id, scene, version_id)"
        " VALUES (?, ?, ?, ?)",
        (master_fp, screen_id, scene, version_id),
    )
    conn.commit()
    conn.close()


# ─── prompt_hash 計算 ─────────────────────────────────


def test_prompt_hash_deterministic():
    from tools.tag_judgment import compute_prompt_hash
    h1 = compute_prompt_hash("hello", [{"id": 1, "name": "A", "description": "x"}])
    h2 = compute_prompt_hash("hello", [{"id": 1, "name": "A", "description": "x"}])
    assert h1 == h2 and len(h1) == 16


def test_prompt_hash_changes_with_description():
    from tools.tag_judgment import compute_prompt_hash
    h1 = compute_prompt_hash("p", [{"id": 1, "name": "A", "description": "x"}])
    h2 = compute_prompt_hash("p", [{"id": 1, "name": "A", "description": "y"}])
    assert h1 != h2


def test_prompt_hash_changes_with_prompt():
    from tools.tag_judgment import compute_prompt_hash
    h1 = compute_prompt_hash("p1", [{"id": 1, "name": "A", "description": "x"}])
    h2 = compute_prompt_hash("p2", [{"id": 1, "name": "A", "description": "x"}])
    assert h1 != h2


def test_prompt_hash_unaffected_by_tag_order():
    from tools.tag_judgment import compute_prompt_hash
    h1 = compute_prompt_hash("p", [
        {"id": 1, "name": "A", "description": "x"},
        {"id": 2, "name": "B", "description": "y"},
    ])
    h2 = compute_prompt_hash("p", [
        {"id": 2, "name": "B", "description": "y"},
        {"id": 1, "name": "A", "description": "x"},
    ])
    assert h1 == h2


# ─── プロンプト展開 ─────────────────────────────────


def test_render_prompt_substitutes_placeholders():
    from tools.tag_judgment import render_prompt, DEFAULT_PROMPTS
    template = DEFAULT_PROMPTS["scene"]
    out = render_prompt(template, [{"id": 1, "name": "ホーム", "description": ""}],
                        "MENU", "OCR テキスト")
    assert "id=1" in out
    assert "ホーム" in out
    assert "MENU" in out
    assert "OCR テキスト" in out


# ─── fetch_active_prompt ─────────────────────────────


def test_fetch_active_prompt_inserts_default(tmp_path):
    from tools.tag_judgment import fetch_active_prompt, DEFAULT_PROMPTS
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    text = fetch_active_prompt(conn, "scene")
    assert text == DEFAULT_PROMPTS["scene"]
    cnt = conn.execute("SELECT COUNT(*) FROM lc_tag_prompts WHERE tag_type='scene'").fetchone()[0]
    assert cnt == 1
    conn.close()


def test_fetch_active_prompt_returns_user_edit(tmp_path):
    from tools.tag_judgment import fetch_active_prompt
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
        " VALUES ('scene', 'カスタム', 0)"
    )
    conn.commit()
    text = fetch_active_prompt(conn, "scene")
    assert text == "カスタム"
    conn.close()


# ─── fetch_target_master_fps ─────────────────────────


def test_fetch_targets_unassigned_excludes_manual(tmp_path):
    from tools.tag_judgment import fetch_target_master_fps
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1", screen_id=101)
    _seed_master(db_path, "fp2", screen_id=102)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"  # 1 = ホーム (scene)
    )
    conn.commit()
    targets = fetch_target_master_fps(conn, "scene", "unassigned", False, 1)
    fps = {t["master_fp"] for t in targets}
    assert "fp1" not in fps  # manual で付与済み → 除外
    assert "fp2" in fps
    conn.close()


def test_fetch_targets_unassigned_excludes_auto_pilot(tmp_path):
    from tools.tag_judgment import fetch_target_master_fps
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1", screen_id=101)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # ただし scene tag だけが除外条件、auto_pilot 付与でも scene なら除外
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'auto_pilot')"
    )
    conn.commit()
    targets = fetch_target_master_fps(conn, "scene", "unassigned", False, 1)
    fps = {t["master_fp"] for t in targets}
    assert "fp1" not in fps
    conn.close()


def test_fetch_targets_unassigned_includes_gemini_only(tmp_path):
    """gemini 付与のみのノードは「未付与」扱い (prompt_hash 変化で再判定可能)。"""
    from tools.tag_judgment import fetch_target_master_fps
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1", screen_id=101)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'gemini')"
    )
    conn.commit()
    targets = fetch_target_master_fps(conn, "scene", "unassigned", False, 1)
    fps = {t["master_fp"] for t in targets}
    assert "fp1" in fps
    conn.close()


def test_fetch_targets_all_protects_manual_default(tmp_path):
    from tools.tag_judgment import fetch_target_master_fps
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1", screen_id=101)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    conn.commit()
    # all + reset_manual=False → manual 保護
    t1 = fetch_target_master_fps(conn, "scene", "all", False, 1)
    assert {t["master_fp"] for t in t1} == set()
    # all + reset_manual=True → manual も対象
    t2 = fetch_target_master_fps(conn, "scene", "all", True, 1)
    assert {t["master_fp"] for t in t2} == {"fp1"}
    conn.close()


def test_fetch_targets_all_always_protects_auto_pilot(tmp_path):
    from tools.tag_judgment import fetch_target_master_fps
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1", screen_id=101)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'auto_pilot')"
    )
    conn.commit()
    t = fetch_target_master_fps(conn, "scene", "all", True, 1)
    assert {t["master_fp"] for t in t} == set()  # reset_manual でも auto_pilot は保護
    conn.close()


# ─── キャッシュ ───────────────────────────────────────


def test_save_and_get_cache(tmp_path):
    from tools.tag_judgment import save_cache, get_cached_judgment
    db_path = _setup_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    save_cache(conn, "fp1", "scene", "h1", "m1", {"tag_id": 1, "confidence": 0.9})
    conn.commit()
    cached = get_cached_judgment(conn, "fp1", "scene", "h1", "m1")
    assert cached == {"tag_id": 1, "confidence": 0.9}
    # 別 model なら hit しない
    assert get_cached_judgment(conn, "fp1", "scene", "h1", "m2") is None
    # 別 prompt_hash なら hit しない
    assert get_cached_judgment(conn, "fp1", "scene", "h2", "m1") is None
    conn.close()


# ─── apply_judgment ───────────────────────────────────


def test_apply_judgment_scene_replaces_existing_gemini(tmp_path):
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 既存 Gemini scene tag (id=1=ホーム)
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'gemini')"
    )
    apply_judgment(conn, "fp1", 1, "scene",
                   {"tag_id": 3, "confidence": 0.8},  # 3 = バトル
                   {1, 2, 3, 4}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tag_id"] == 3
    assert rows[0]["assigned_by"] == "gemini"
    conn.close()


def test_apply_judgment_scene_protects_manual(tmp_path):
    """reset_manual=False のとき manual シーンタグは消さない (が gemini シーンは置換可能)。

    apply_judgment 自体は同 tag_type の gemini を消す。manual は呼び出し元で
    既にフィルタされていることを前提とするが、防御的にここでも担保される。
    """
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"  # ホーム manual
    )
    apply_judgment(conn, "fp1", 1, "scene",
                   {"tag_id": 3}, {1, 2, 3, 4}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags WHERE master_fp='fp1'"
        " ORDER BY id"
    ).fetchall()
    # manual ホーム残る + gemini バトル付く
    assert len(rows) == 2
    assert rows[0]["tag_id"] == 1
    assert rows[0]["assigned_by"] == "manual"
    assert rows[1]["tag_id"] == 3
    assert rows[1]["assigned_by"] == "gemini"
    conn.close()


def test_apply_judgment_scene_reset_manual_overwrites(tmp_path):
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "INSERT INTO lc_master_node_tags (master_fp, version_id, tag_id, assigned_by)"
        " VALUES ('fp1', 1, 1, 'manual')"
    )
    apply_judgment(conn, "fp1", 1, "scene",
                   {"tag_id": 3}, {1, 2, 3, 4}, reset_manual=True)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tag_id"] == 3
    assert rows[0]["assigned_by"] == "gemini"
    conn.close()


def test_apply_judgment_sub_scene_multiple_tags(tmp_path):
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_judgment(conn, "fp1", 1, "sub_scene",
                   {"tag_ids": [12, 13, 14], "confidence": 0.7},
                   {12, 13, 14, 15}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT tag_id FROM lc_master_node_tags WHERE master_fp='fp1' ORDER BY tag_id"
    ).fetchall()
    assert [r["tag_id"] for r in rows] == [12, 13, 14]
    conn.close()


def test_apply_judgment_filters_invalid_tag_ids(tmp_path):
    """Gemini が candidate にない tag_id を返しても無視される。"""
    from tools.tag_judgment import apply_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    apply_judgment(conn, "fp1", 1, "scene",
                   {"tag_id": 999},  # 候補にない
                   {1, 2, 3}, reset_manual=False)
    conn.commit()
    rows = conn.execute(
        "SELECT COUNT(*) FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchone()
    assert rows[0] == 0
    conn.close()


# ─── run_judgment エンドツーエンド (Gemini モック) ─────


def _mock_gemini_response(tag_id):
    return ({"tag_id": tag_id, "confidence": 0.9, "reasoning": "test"},
            100, 20, None)


def test_run_judgment_dry_run(tmp_path):
    from tools.tag_judgment import run_judgment
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    _seed_master(db_path, "fp2", screen_id=102)
    result = run_judgment(db_path, "scene", "unassigned", False, 1, dry_run=True)
    assert result["ok"]
    assert result["dry_run"]
    assert result["total"] == 2


def test_run_judgment_uses_cache(tmp_path):
    """事前にキャッシュを入れておくと API を呼ばずに同じ結果を返す。"""
    from tools.tag_judgment import run_judgment, compute_prompt_hash, fetch_candidate_tags, fetch_active_prompt, MODEL_BY_TYPE

    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")

    # キャッシュ事前投入
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    candidate_tags = fetch_candidate_tags(conn, "scene")
    template = fetch_active_prompt(conn, "scene")
    ph = compute_prompt_hash(template, candidate_tags)
    conn.execute(
        "INSERT INTO lc_tag_judgments"
        " (master_fp, tag_type, prompt_hash, result_json, model)"
        " VALUES ('fp1', 'scene', ?, ?, ?)",
        (ph, json.dumps({"tag_id": 1, "confidence": 0.9}), MODEL_BY_TYPE["scene"]),
    )
    conn.commit()
    conn.close()

    # API は呼ばれないはず → API キーなしでも成功
    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
        result = run_judgment(db_path, "scene", "unassigned", False, 1)
    assert result["ok"]
    assert result["summary"]["cache_hits"] == 1
    assert result["summary"]["api_calls"] == 0

    # タグが付与されている
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT tag_id, assigned_by FROM lc_master_node_tags WHERE master_fp='fp1'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == "gemini"
    conn.close()


def test_run_judgment_calls_gemini_when_not_cached(tmp_path):
    """キャッシュなし + Gemini モック → API 呼び出しが記録される。"""
    from tools.tag_judgment import run_judgment

    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")

    with patch("tools.tag_judgment.call_gemini") as mock:
        mock.return_value = _mock_gemini_response(1)
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fake"}, clear=False):
            result = run_judgment(db_path, "scene", "unassigned", False, 1)

    assert result["ok"]
    assert result["summary"]["api_calls"] == 1
    assert result["summary"]["cache_hits"] == 0

    # キャッシュが保存されている
    conn = sqlite3.connect(str(db_path))
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tag_judgments WHERE master_fp='fp1'"
    ).fetchone()[0]
    assert cnt == 1
    # api_usage が記録されている
    cnt2 = conn.execute(
        "SELECT COUNT(*) FROM lc_api_usage WHERE purpose='tag_scene_judgment'"
    ).fetchone()[0]
    assert cnt2 == 1
    conn.close()


def test_run_judgment_error_not_cached(tmp_path):
    """API エラー時はキャッシュに保存しない (再実行で復旧可能)。"""
    from tools.tag_judgment import run_judgment

    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")

    with patch("tools.tag_judgment.call_gemini") as mock:
        mock.return_value = (None, 0, 0, "simulated_error")
        with patch.dict("os.environ", {"GEMINI_API_KEY": "fake"}, clear=False):
            result = run_judgment(db_path, "scene", "unassigned", False, 1)

    assert result["ok"]
    assert result["summary"]["errors"] == 1

    conn = sqlite3.connect(str(db_path))
    cnt = conn.execute(
        "SELECT COUNT(*) FROM lc_tag_judgments WHERE master_fp='fp1'"
    ).fetchone()[0]
    assert cnt == 0  # エラーはキャッシュされない
    conn.close()


# ─── estimate_targets ─────────────────────────────────


def test_estimate_targets(tmp_path):
    from tools.tag_judgment import estimate_targets
    db_path = _setup_db(tmp_path)
    _seed_master(db_path, "fp1")
    _seed_master(db_path, "fp2", screen_id=102)
    est = estimate_targets(db_path, "scene", "unassigned", False, 1)
    assert est["target_count"] == 2
    assert est["api_call_estimate"] == 2  # キャッシュなし
    assert est["model"] == "gemini-2.5-flash-lite"
    assert "prompt_hash" in est
