"""cluster_stable_loops によるクラスタ安定判定テスト。

目的: クラスタリング loop で cluster_id が変動しなかった連続回数を追跡し、
閾値以上に達した代表のみ Gemini OCR の対象にすることで、降格代表への
無駄な API 呼び出しを防ぐ仕組みを検証する。

対象:
  - BackgroundWorker._take_cluster_snapshot
  - BackgroundWorker._mark_cluster_stability
  - _run_gemini_batch_correction の SQL クエリ (running セッションは安定待ち、
    completed セッションは無条件で対象)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tools.ap.background_worker import (
    BackgroundWorker,
    _GEMINI_CLUSTER_STABLE_THRESHOLD,
)


# ─── テスト用 DB スキーマ ──────────────────────────────

def _create_test_schema(conn: sqlite3.Connection) -> None:
    """最小限の lc_screens / lc_sessions スキーマを作成。"""
    conn.executescript("""
        CREATE TABLE lc_screens (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id          TEXT,
            screenshot_path     TEXT,
            scene               TEXT,
            phash               TEXT,
            dhash               TEXT,
            cluster_id          INTEGER,
            cluster_stable_loops INTEGER DEFAULT 0,
            is_representative   INTEGER DEFAULT 0,
            is_artifact         INTEGER DEFAULT 0,
            ocr_text            TEXT,
            ocr_text_hq         TEXT,
            ocr_text_gemini     TEXT,
            discovered_at       TEXT
        );
        CREATE TABLE lc_sessions (
            session_id TEXT PRIMARY KEY,
            status     TEXT,
            version_id INTEGER,
            started_at TEXT
        );
        CREATE TABLE lc_versions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE,
            is_active  INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0
        );
        INSERT INTO lc_versions (id, name, is_active) VALUES (1, 'v1.0.0', 1);
    """)
    conn.commit()


def _insert_screen(
    conn: sqlite3.Connection,
    *,
    sid: str,
    cluster_id=None,
    is_rep=1,
    ocr_text_gemini=None,
    cluster_stable_loops=0,
    screenshot_path="/tmp/a.png",
    discovered_at="2026-05-15 00:00:00",
) -> int:
    cur = conn.execute(
        "INSERT INTO lc_screens"
        " (session_id, cluster_id, is_representative, ocr_text_gemini,"
        "  cluster_stable_loops, screenshot_path, discovered_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, cluster_id, is_rep, ocr_text_gemini,
         cluster_stable_loops, screenshot_path, discovered_at),
    )
    return cur.lastrowid


@pytest.fixture
def tmp_db(tmp_path: Path):
    """一時 DB + 最小スキーマを作成して返す。"""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _create_test_schema(conn)
    yield db_path, conn
    conn.close()


@pytest.fixture
def worker(tmp_db):
    """BackgroundWorker インスタンス (session_id=test-sid)。"""
    db_path, _ = tmp_db
    return BackgroundWorker(db_path=db_path, session_id="test-sid")


# ─── _take_cluster_snapshot ───────────────────────────────

class TestTakeClusterSnapshot:
    def test_includes_only_assigned_clusters(self, tmp_db, worker):
        """cluster_id IS NOT NULL の screen のみスナップショットに含む。"""
        _, conn = tmp_db
        sid1 = _insert_screen(conn, sid="test-sid", cluster_id=10)
        sid2 = _insert_screen(conn, sid="test-sid", cluster_id=20)
        sid3 = _insert_screen(conn, sid="test-sid", cluster_id=None)  # NULL
        conn.commit()

        snap = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        assert snap == {sid1: 10, sid2: 20}
        assert sid3 not in snap

    def test_respects_session_filter(self, tmp_db, worker):
        """sid_filter で他セッションを除外する。"""
        _, conn = tmp_db
        sid_mine = _insert_screen(conn, sid="test-sid", cluster_id=10)
        _insert_screen(conn, sid="other-sid", cluster_id=99)
        conn.commit()

        snap = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        assert snap == {sid_mine: 10}

    def test_empty_db_returns_empty_dict(self, tmp_db, worker):
        _, conn = tmp_db
        snap = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        assert snap == {}


# ─── _mark_cluster_stability ──────────────────────────────

class TestMarkClusterStability:
    def test_increments_when_cluster_id_unchanged(self, tmp_db, worker):
        _, conn = tmp_db
        sid = _insert_screen(conn, sid="test-sid", cluster_id=10, cluster_stable_loops=0)
        conn.commit()

        worker._mark_cluster_stability(conn, pre_snapshot={sid: 10})
        row = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()
        assert row["cluster_stable_loops"] == 1

    def test_resets_when_cluster_id_changed(self, tmp_db, worker):
        _, conn = tmp_db
        sid = _insert_screen(conn, sid="test-sid", cluster_id=20, cluster_stable_loops=5)
        conn.commit()

        # snapshot で 10 だったが、現在は 20 → 変動あり
        worker._mark_cluster_stability(conn, pre_snapshot={sid: 10})
        row = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()
        assert row["cluster_stable_loops"] == 0

    def test_ignores_screens_not_in_snapshot(self, tmp_db, worker):
        """スナップショット外 (新規) の screen は触らない。"""
        _, conn = tmp_db
        sid_old = _insert_screen(conn, sid="test-sid", cluster_id=10, cluster_stable_loops=0)
        sid_new = _insert_screen(conn, sid="test-sid", cluster_id=11, cluster_stable_loops=0)
        conn.commit()

        # スナップショットには sid_old のみ
        worker._mark_cluster_stability(conn, pre_snapshot={sid_old: 10})

        row_old = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid_old,)
        ).fetchone()
        row_new = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid_new,)
        ).fetchone()
        assert row_old["cluster_stable_loops"] == 1
        assert row_new["cluster_stable_loops"] == 0  # 新規は不変

    def test_handles_deleted_screen_gracefully(self, tmp_db, worker):
        """スナップショットに含まれるが現在は削除された screen でクラッシュしない。"""
        _, conn = tmp_db
        # 99 は存在しない id
        worker._mark_cluster_stability(conn, pre_snapshot={99: 10})
        # クラッシュしなければ OK

    def test_empty_snapshot_is_noop(self, tmp_db, worker):
        _, conn = tmp_db
        sid = _insert_screen(conn, sid="test-sid", cluster_id=10, cluster_stable_loops=3)
        conn.commit()

        worker._mark_cluster_stability(conn, pre_snapshot={})
        row = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()
        # 触らないので 3 のまま
        assert row["cluster_stable_loops"] == 3


# ─── 複数 loop シミュレーション ──────────────────────────

class TestMultiLoopProgression:
    def test_new_screen_reaches_threshold_in_3_loops(self, tmp_db, worker):
        """新規 screen は 3 loop 経過で _GEMINI_CLUSTER_STABLE_THRESHOLD (=2) に到達。

        - loop1: 新規 screen 投入。snapshot は空 → 更新なし。loop 終了時に
                 cluster_id 確定。値 = 0
        - loop2: snapshot に入る。cluster_id 変動なし → +1。値 = 1
        - loop3: snapshot に入る。変動なし → +1。値 = 2 (閾値到達)
        """
        _, conn = tmp_db

        # loop1 開始: DB は空
        snap1 = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        assert snap1 == {}

        # loop1 中に新規 screen 追加 (cluster_id 確定)
        sid = _insert_screen(conn, sid="test-sid", cluster_id=10, cluster_stable_loops=0)
        conn.commit()
        worker._mark_cluster_stability(conn, snap1)
        v = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()["cluster_stable_loops"]
        assert v == 0

        # loop2 開始: snapshot に入る
        snap2 = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        assert snap2 == {sid: 10}
        # loop2 中に cluster_id 変動なし
        worker._mark_cluster_stability(conn, snap2)
        v = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()["cluster_stable_loops"]
        assert v == 1

        # loop3
        snap3 = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        worker._mark_cluster_stability(conn, snap3)
        v = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()["cluster_stable_loops"]
        assert v == 2
        assert v >= _GEMINI_CLUSTER_STABLE_THRESHOLD

    def test_demotion_resets_counter_mid_progression(self, tmp_db, worker):
        """安定途中で cluster_id が変動するとカウンタは 0 にリセット。"""
        _, conn = tmp_db
        sid = _insert_screen(conn, sid="test-sid", cluster_id=10, cluster_stable_loops=1)
        conn.commit()

        # loop 開始時 cluster_id=10
        snap = worker._take_cluster_snapshot(conn, " AND session_id = ?", ("test-sid",))
        # loop 中に cluster_id 変動 (_remerge による移動を模擬)
        conn.execute("UPDATE lc_screens SET cluster_id = 20 WHERE id = ?", (sid,))
        conn.commit()
        worker._mark_cluster_stability(conn, snap)

        v = conn.execute(
            "SELECT cluster_stable_loops FROM lc_screens WHERE id = ?", (sid,)
        ).fetchone()["cluster_stable_loops"]
        assert v == 0


# ─── Gemini 対象クエリ (SQL レベル検証) ──────────────────

class TestGeminiQueryStabilityFilter:
    """_run_gemini_batch_correction の WHERE 句が cluster_stable_loops を
    正しく考慮するかを SQL レベルで検証する。"""

    def _gemini_query_for_session(
        self, conn: sqlite3.Connection, session_id: str
    ) -> list:
        """_run_gemini_batch_correction と同じ条件分岐を再現。"""
        sess_row = conn.execute(
            "SELECT status FROM lc_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        is_running = bool(sess_row and sess_row["status"] == "running")
        stable_clause = (
            " AND COALESCE(cluster_stable_loops, 0) >= ?"
            if is_running else ""
        )
        params: tuple = (session_id,)
        if is_running:
            params = (session_id, _GEMINI_CLUSTER_STABLE_THRESHOLD)
        params = params + (24,)
        return list(conn.execute(
            "SELECT id FROM lc_screens"
            " WHERE is_representative = 1"
            " AND ocr_text_gemini IS NULL"
            " AND screenshot_path != ''"
            " AND session_id = ?"
            + stable_clause +
            " ORDER BY discovered_at"
            " LIMIT ?",
            params,
        ).fetchall())

    def test_running_session_excludes_unstable_screens(self, tmp_db):
        _, conn = tmp_db
        conn.execute(
            "INSERT INTO lc_sessions (session_id, status, version_id) VALUES (?, 'running', 1)",
            ("sid-r",),
        )
        # 安定済み: stable_loops=2
        stable_sid = _insert_screen(
            conn, sid="sid-r", cluster_id=1, is_rep=1, cluster_stable_loops=2)
        # 未安定: stable_loops=1
        _insert_screen(
            conn, sid="sid-r", cluster_id=2, is_rep=1, cluster_stable_loops=1)
        # 未安定: stable_loops=0 (新規)
        _insert_screen(
            conn, sid="sid-r", cluster_id=3, is_rep=1, cluster_stable_loops=0)
        conn.commit()

        rows = self._gemini_query_for_session(conn, "sid-r")
        ids = [r["id"] for r in rows]
        assert ids == [stable_sid]

    def test_completed_session_includes_all_screens(self, tmp_db):
        _, conn = tmp_db
        conn.execute(
            "INSERT INTO lc_sessions (session_id, status, version_id) VALUES (?, 'completed', 1)",
            ("sid-c",),
        )
        sid_a = _insert_screen(
            conn, sid="sid-c", cluster_id=1, is_rep=1, cluster_stable_loops=0)
        sid_b = _insert_screen(
            conn, sid="sid-c", cluster_id=2, is_rep=1, cluster_stable_loops=2)
        conn.commit()

        rows = self._gemini_query_for_session(conn, "sid-c")
        ids = sorted(r["id"] for r in rows)
        # completed では cluster_stable_loops 制約なし → 両方対象
        assert ids == sorted([sid_a, sid_b])

    def test_running_skips_already_geminied_screens(self, tmp_db):
        """ocr_text_gemini != NULL は安定でも対象外 (既存ロジック維持)。"""
        _, conn = tmp_db
        conn.execute(
            "INSERT INTO lc_sessions (session_id, status, version_id) VALUES (?, 'running', 1)",
            ("sid-r",),
        )
        _insert_screen(
            conn, sid="sid-r", cluster_id=1, is_rep=1,
            cluster_stable_loops=2, ocr_text_gemini="既に処理済み")
        conn.commit()

        rows = self._gemini_query_for_session(conn, "sid-r")
        assert rows == []


# ─── マイグレーション ───────────────────────────────────

class TestMigration:
    def test_screen_recorder_migrate_adds_column(self, tmp_path):
        """screen_recorder._migrate で cluster_stable_loops カラムが追加される。"""
        # NOTE: ScreenRecorder のフル初期化は Appium 等が絡むため、
        # _migrate メソッドだけ単独で呼ぶ最小セットアップを組む。
        import sqlite3
        db_path = tmp_path / "migrate_test.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # マイグレーション対象の最小 lc_screens (カラムなし状態)
        conn.executescript("""
            CREATE TABLE lc_screens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                phash TEXT
            );
            CREATE TABLE lc_sessions (session_id TEXT PRIMARY KEY);
            CREATE TABLE lc_transitions (id INTEGER PRIMARY KEY);
        """)
        conn.commit()

        # _migrate を呼ぶための擬似インスタンス
        from tools.ap.screen_recorder import ScreenRecorder
        sr = ScreenRecorder.__new__(ScreenRecorder)
        sr._conn = conn
        sr._migrate()

        cols = {r["name"] for r in conn.execute("PRAGMA table_info(lc_screens)")}
        assert "cluster_stable_loops" in cols
        conn.close()
