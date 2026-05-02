"""
BatchProcessor ユニットテスト

Phase 1: グルーピング + ラベル付け
Phase 2: phash クラスタリング間引き
Phase 3: PaddleOCR 再処理 (モック)
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest


def _create_test_db(tmp_path: Path) -> Path:
    """テスト用 DB を作成し、スクリーンデータを投入。"""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE lc_sessions (
            id INTEGER PRIMARY KEY, session_id TEXT UNIQUE,
            screens_found INTEGER DEFAULT 0, started_at TEXT,
            status TEXT DEFAULT 'completed', game_title TEXT DEFAULT 'Test'
        );
        CREATE TABLE lc_screens (
            id INTEGER PRIMARY KEY, session_id TEXT, fingerprint TEXT,
            title TEXT, depth INTEGER DEFAULT 0, parent_fp TEXT,
            phash TEXT, screenshot_path TEXT, thumbnail_path TEXT,
            ocr_text TEXT, scene TEXT, discovered_at TEXT
        );
        CREATE TABLE lc_tappable_items (
            id INTEGER PRIMARY KEY, screen_id INTEGER, text TEXT, confidence REAL
        );
    """)
    conn.execute(
        "INSERT INTO lc_sessions (session_id, started_at, game_title)"
        " VALUES ('test_session', ?, 'TestGame')",
        (datetime.now().isoformat(),),
    )

    # テストデータ: ADV x3 → BATTLE x4 → ADV x2 → (60s gap) → ADV x2
    base = datetime(2026, 4, 10, 12, 0, 0)
    screens = [
        # ADV グループ#1 (phash 類似 = 同一クラスタ)
        ("fp01", "セリフA", "aaa0000000000001", "ADV", base + timedelta(seconds=0)),
        ("fp02", "セリフB", "aaa0000000000002", "ADV", base + timedelta(seconds=3)),
        ("fp03", "セリフC", "aaa0000000000003", "ADV", base + timedelta(seconds=6)),
        # BATTLE グループ#1 (phash 変化あり → 複数クラスタ)
        ("fp04", "ATTACKER", "bbb0000000000001", "BATTLE", base + timedelta(seconds=10)),
        ("fp05", "ATTACKER AUTO", "bbb0000000000002", "BATTLE", base + timedelta(seconds=13)),
        ("fp06", "スキル発動", "bbb00000000000ff", "BATTLE", base + timedelta(seconds=16)),
        ("fp07", "TOTAL DAMAGE", "ccc0000000000001", "BATTLE", base + timedelta(seconds=20)),
        # ADV グループ#2
        ("fp08", "結果画面", "ddd0000000000001", "ADV", base + timedelta(seconds=25)),
        ("fp09", "次のセリフ", "ddd0000000000002", "ADV", base + timedelta(seconds=28)),
        # 60秒ギャップ → ADV グループ#3 (別グループ)
        ("fp10", "新しいシーン", "eee0000000000001", "ADV", base + timedelta(seconds=100)),
        ("fp11", "続き", "eee0000000000002", "ADV", base + timedelta(seconds=103)),
    ]
    for fp, title, phash, scene, ts in screens:
        conn.execute(
            "INSERT INTO lc_screens (session_id, fingerprint, title, phash, scene, discovered_at, ocr_text)"
            " VALUES ('test_session', ?, ?, ?, ?, ?, ?)",
            (fp, title, phash, scene, ts.isoformat(), title),
        )
    conn.commit()
    conn.close()
    return db_path


# ─── Phase 1: グルーピング ────────────────────────────

class TestGroup:

    def test_creates_groups(self, tmp_path):
        """グループが正しく作成される。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            count = bp.group(session_id="test_session")
            assert count == 4  # ADV#1, BATTLE#1, ADV#2, ADV#3
        finally:
            bp.close()

    def test_group_labels(self, tmp_path):
        """ラベルが正しく生成される。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            conn = sqlite3.connect(str(db_path))
            groups = conn.execute(
                "SELECT label, scene, screen_count FROM lc_screen_groups ORDER BY id"
            ).fetchall()
            conn.close()
            assert groups[0] == ("ストーリー#1", "ADV", 3)
            assert groups[1] == ("バトル#1", "BATTLE", 4)
            assert groups[2] == ("ストーリー#2", "ADV", 2)
            assert groups[3] == ("ストーリー#3", "ADV", 2)  # 60秒ギャップで別
        finally:
            bp.close()

    def test_group_id_assigned(self, tmp_path):
        """lc_screens.group_id が正しく設定される。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT group_id FROM lc_screens WHERE session_id = 'test_session' ORDER BY id"
            ).fetchall()
            conn.close()
            # 全スクリーンに group_id が設定されている
            assert all(r[0] is not None for r in rows)
            # ADV#1 の3枚は同じ group_id
            assert rows[0][0] == rows[1][0] == rows[2][0]
            # BATTLE は別 group_id
            assert rows[3][0] != rows[0][0]
        finally:
            bp.close()

    def test_time_gap_splits_group(self, tmp_path):
        """60秒以上のギャップで同じ scene でも別グループ。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            conn = sqlite3.connect(str(db_path))
            # fp09 (ADV#2) と fp10 (ADV#3) は別 group_id
            rows = conn.execute(
                "SELECT fingerprint, group_id FROM lc_screens"
                " WHERE fingerprint IN ('fp09', 'fp10') ORDER BY fingerprint"
            ).fetchall()
            conn.close()
            assert rows[0][1] != rows[1][1]
        finally:
            bp.close()

    def test_rerun_clears_old_groups(self, tmp_path):
        """再実行で古いグループがクリアされる。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            bp.group(session_id="test_session")  # 2回目
            conn = sqlite3.connect(str(db_path))
            count = conn.execute("SELECT count(*) FROM lc_screen_groups").fetchone()[0]
            conn.close()
            assert count == 4  # 重複しない
        finally:
            bp.close()

    def test_dry_run(self, tmp_path):
        """dry-run では DB に変更がない。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path, dry_run=True)
        try:
            count = bp.group(session_id="test_session")
            assert count == 4
            conn = sqlite3.connect(str(db_path))
            groups = conn.execute("SELECT count(*) FROM lc_screen_groups").fetchone()[0]
            conn.close()
            assert groups == 0  # DB 未変更
        finally:
            bp.close()


# ─── Phase 2: 間引き ─────────────────────────────────

class TestDeduplicate:

    def test_creates_clusters(self, tmp_path):
        """クラスタが作成され代表が選出される。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            reps = bp.deduplicate(session_id="test_session")
            assert reps > 0

            conn = sqlite3.connect(str(db_path))
            rep_count = conn.execute(
                "SELECT count(*) FROM lc_screens WHERE is_representative = 1"
            ).fetchone()[0]
            conn.close()
            assert rep_count == reps
        finally:
            bp.close()

    def test_representative_is_middle(self, tmp_path):
        """代表はクラスタの中央。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            bp.deduplicate(session_id="test_session")

            conn = sqlite3.connect(str(db_path))
            # ADV#1 (3枚) の代表は中央 = fp02
            reps = conn.execute(
                "SELECT fingerprint FROM lc_screens"
                " WHERE is_representative = 1 AND scene = 'ADV'"
                " ORDER BY discovered_at"
            ).fetchall()
            conn.close()
            # 最初の ADV グループの代表が含まれる
            rep_fps = [r[0] for r in reps]
            assert "fp02" in rep_fps  # 3枚の中央
        finally:
            bp.close()

    def test_cluster_id_assigned(self, tmp_path):
        """cluster_id が設定される。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            bp.group(session_id="test_session")
            bp.deduplicate(session_id="test_session")

            conn = sqlite3.connect(str(db_path))
            rows = conn.execute(
                "SELECT cluster_id FROM lc_screens"
                " WHERE group_id IS NOT NULL ORDER BY id"
            ).fetchall()
            conn.close()
            assert all(r[0] is not None for r in rows)
        finally:
            bp.close()


# ─── Phase 3: OCR 再処理 (モック) ─────────────────────

class TestReocr:

    def test_skips_when_no_representatives(self, tmp_path):
        """代表画像がなければスキップ。"""
        from tools.batch_processor import BatchProcessor
        db_path = _create_test_db(tmp_path)
        bp = BatchProcessor(db_path=db_path)
        try:
            result = bp.reocr(session_id="test_session")
            assert result == 0
        finally:
            bp.close()
