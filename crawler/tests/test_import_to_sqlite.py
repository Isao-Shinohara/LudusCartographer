"""
test_import_to_sqlite.py — import_to_sqlite.py のユニットテスト

Appium 不要。すべてのテストはインメモリ SQLite で動作する。

テスト対象:
  tools/import_to_sqlite.py — SCHEMA / migrate() / import_session() / seed_test_games()
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.import_to_sqlite import SCHEMA, import_session, migrate, seed_test_games


# ============================================================
# フィクスチャ
# ============================================================


@pytest.fixture
def conn():
    """インメモリ SQLite 接続を返す。テスト終了時に自動クローズ。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    yield c
    c.close()


def _make_summary(tmp_path: Path, session_id: str, device_mode: str = "SIMULATOR") -> Path:
    """テスト用 crawl_summary.json を作成して Path を返す。"""
    data = {
        "session_id": session_id,
        "game_title": "TestGame",
        "device_mode": device_mode,
        "screens": [
            {
                "fingerprint": "fp001",
                "title": "ホーム",
                "depth": 0,
                "parent_fp": None,
                "tappable_items": [{"text": "設定", "confidence": 0.95}],
                "phash": "abcd1234abcd1234",
                "screenshot_path": "/tmp/shot.png",
                "discovered_at": "2026-03-04T12:00:00",
            }
        ],
        "stats": {"screens_found": 1, "screens_skipped": 0, "taps_total": 1, "elapsed_sec": 10.0},
    }
    path = tmp_path / f"{session_id}" / "crawl_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ============================================================
# SCHEMA — device_mode カラムの存在確認
# ============================================================


class TestSchema:
    def test_lc_sessions_has_device_mode_column(self, conn):
        """lc_sessions に device_mode カラムが存在すること。"""
        cols = [r[1] for r in conn.execute("PRAGMA table_info(lc_sessions)")]
        assert "device_mode" in cols

    def test_device_mode_default_is_simulator(self, conn):
        """device_mode のデフォルト値が 'SIMULATOR' であること。"""
        conn.execute(
            "INSERT INTO lc_sessions (session_id) VALUES (?)", ("sess_default",)
        )
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?", ("sess_default",)
        ).fetchone()
        assert row["device_mode"] == "SIMULATOR"


# ============================================================
# migrate() — 既存 DB への device_mode 追加
# ============================================================


class TestMigrate:
    def _make_old_db(self) -> sqlite3.Connection:
        """device_mode カラムがない古い DB を作成する。"""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.executescript("""
            CREATE TABLE lc_sessions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT UNIQUE NOT NULL,
                screens_found INTEGER DEFAULT 0,
                started_at    TEXT,
                status        TEXT DEFAULT 'completed',
                game_title    TEXT DEFAULT 'Unknown Game'
            );
            INSERT INTO lc_sessions (session_id, game_title)
            VALUES ('old_sess_001', 'OldGame');
        """)
        return c

    def test_migrate_adds_device_mode_to_old_db(self):
        """古い DB に device_mode カラムを追加できること。"""
        c = self._make_old_db()
        migrate(c)
        cols = [r[1] for r in c.execute("PRAGMA table_info(lc_sessions)")]
        assert "device_mode" in cols
        c.close()

    def test_migrate_existing_rows_get_simulator(self):
        """既存行の device_mode が 'SIMULATOR' になること。"""
        c = self._make_old_db()
        migrate(c)
        row = c.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?", ("old_sess_001",)
        ).fetchone()
        assert row["device_mode"] == "SIMULATOR"
        c.close()

    def test_migrate_idempotent(self, conn):
        """device_mode カラムが既に存在するとき migrate() は冪等であること。"""
        # エラーなく 2 回実行できること
        migrate(conn)
        migrate(conn)


# ============================================================
# import_session() — device_mode の保存
# ============================================================


class TestImportSession:
    def test_simulator_device_mode_saved(self, conn, tmp_path):
        """SIMULATOR の device_mode が lc_sessions に保存される。"""
        path = _make_summary(tmp_path, "sess_sim", device_mode="SIMULATOR")
        import_session(conn, path)
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?", ("sess_sim",)
        ).fetchone()
        assert row["device_mode"] == "SIMULATOR"

    def test_mirror_device_mode_saved(self, conn, tmp_path):
        """MIRROR の device_mode が lc_sessions に保存される。"""
        path = _make_summary(tmp_path, "sess_mirror", device_mode="MIRROR")
        import_session(conn, path)
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?", ("sess_mirror",)
        ).fetchone()
        assert row["device_mode"] == "MIRROR"

    def test_missing_device_mode_defaults_to_simulator(self, conn, tmp_path):
        """JSON に device_mode がない場合 'SIMULATOR' になること。"""
        data = {
            "session_id": "sess_no_mode",
            "game_title": "OldGame",
            # device_mode キーなし
            "screens": [],
            "stats": {"screens_found": 0, "screens_skipped": 0, "taps_total": 0, "elapsed_sec": 0},
        }
        path = tmp_path / "sess_no_mode" / "crawl_summary.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

        import_session(conn, path)
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?", ("sess_no_mode",)
        ).fetchone()
        assert row["device_mode"] == "SIMULATOR"

    def test_game_title_also_saved(self, conn, tmp_path):
        """game_title も正しく保存されること（既存動作の回帰）。"""
        path = _make_summary(tmp_path, "sess_gt", device_mode="MIRROR")
        import_session(conn, path)
        row = conn.execute(
            "SELECT game_title FROM lc_sessions WHERE session_id=?", ("sess_gt",)
        ).fetchone()
        assert row["game_title"] == "TestGame"

    def test_returns_imported_screen_count(self, conn, tmp_path):
        """import_session() は追加した画面数を返すこと。"""
        path = _make_summary(tmp_path, "sess_cnt", device_mode="SIMULATOR")
        count = import_session(conn, path)
        assert count == 1


# ============================================================
# seed_test_games() — テストデータの device_mode
# ============================================================


class TestSeedTestGames:
    def test_calendar_is_simulator(self, conn):
        """カレンダーのシード値が SIMULATOR であること。"""
        seed_test_games(conn)
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?",
            ("test_calendar_001",),
        ).fetchone()
        assert row["device_mode"] == "SIMULATOR"

    def test_maps_is_mirror(self, conn):
        """マップのシード値が MIRROR であること。"""
        seed_test_games(conn)
        row = conn.execute(
            "SELECT device_mode FROM lc_sessions WHERE session_id=?",
            ("test_maps_001",),
        ).fetchone()
        assert row["device_mode"] == "MIRROR"

    def test_seed_idempotent(self, conn):
        """seed_test_games() は 2 回呼んでも重複しないこと。"""
        seed_test_games(conn)
        seed_test_games(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM lc_sessions WHERE session_id LIKE 'test_%'"
        ).fetchone()[0]
        assert count == 2  # calendar + maps


# ============================================================
# main.py — --mirror フラグ
# ============================================================


class TestMainMirrorFlag:
    """main.py の --mirror フラグが環境変数を正しく設定するか検証する。"""

    def _run_main_with_mirror(self, env_extras: dict) -> dict:
        """
        --mirror フラグを指定して main() を呼び、実行後の環境変数スナップショットを返す。

        patch.dict で環境変数の変更を呼び出し後に元に戻す。
        """
        import argparse
        import importlib
        from unittest.mock import patch

        import main as m
        importlib.reload(m)

        captured: dict = {}

        base_env = {
            "IOS_BUNDLE_ID": "com.example.test",
            **env_extras,
        }

        with patch.dict(os.environ, base_env, clear=False):
            with patch.object(m, "_parse_args", return_value=argparse.Namespace(
                mirror=True, bundle=None, title=None, duration=None, depth=None
            )):
                with patch("driver_factory.create_driver_session") as mock_ctx:
                    mock_ctx.return_value.__enter__ = lambda *a: __import__(
                        "unittest.mock", fromlist=["MagicMock"]
                    ).MagicMock()
                    mock_ctx.return_value.__exit__ = lambda *a: False
                    try:
                        m.main()
                    except (SystemExit, Exception):
                        pass
                # patch.dict スコープ内でキャプチャ
                captured["DEVICE_MODE"]       = os.environ.get("DEVICE_MODE")
                captured["IOS_USE_SIMULATOR"] = os.environ.get("IOS_USE_SIMULATOR")

        return captured

    def test_mirror_flag_sets_device_mode(self):
        """--mirror 指定で DEVICE_MODE=MIRROR が設定されること。"""
        captured = self._run_main_with_mirror({})
        assert captured["DEVICE_MODE"] == "MIRROR"

    def test_mirror_flag_disables_simulator(self):
        """--mirror 指定で IOS_USE_SIMULATOR=0 が設定されること。"""
        captured = self._run_main_with_mirror({"IOS_USE_SIMULATOR": "1"})
        assert captured["IOS_USE_SIMULATOR"] == "0"
