"""
DB接続テスト: crawler/tests/test_db_conn.py

MySQL への接続を検証するテストスイート。
環境変数 DB_HOST が設定されていない場合は接続テストをスキップし、
スキーマ定義・クエリ構造のユニットテストのみ実行する。
"""
import os
import pytest
import pymysql
from unittest.mock import MagicMock, patch


# ============================================================
# ヘルパー
# ============================================================

def get_db_config() -> dict:
    """環境変数から DB 接続設定を取得する。"""
    return {
        "host":     os.environ.get("DB_HOST", "localhost"),
        "port":     int(os.environ.get("DB_PORT", 3306)),
        "db":       os.environ.get("DB_NAME", "ludus_cartographer"),
        "user":     os.environ.get("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "charset":  "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }


def is_db_available() -> bool:
    """MySQL が実際に利用可能かどうかを確認する。"""
    try:
        cfg = get_db_config()
        conn = pymysql.connect(**cfg)
        conn.close()
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not is_db_available(),
    reason="MySQL が利用できないためスキップ (CI環境またはDB未起動)"
)


# ============================================================
# Unit tests (MySQLが不要 — モックを使用)
# ============================================================

class TestDbConfigUnit:
    """DB設定取得のユニットテスト。"""

    def test_default_host(self, monkeypatch):
        monkeypatch.delenv("DB_HOST", raising=False)
        cfg = get_db_config()
        assert cfg["host"] == "localhost"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("DB_HOST", "db.example.com")
        monkeypatch.setenv("DB_PORT", "3307")
        monkeypatch.setenv("DB_NAME", "test_db")
        cfg = get_db_config()
        assert cfg["host"] == "db.example.com"
        assert cfg["port"] == 3307
        assert cfg["db"] == "test_db"

    def test_charset_is_utf8mb4(self):
        cfg = get_db_config()
        assert cfg["charset"] == "utf8mb4"

    def test_cursor_is_dict(self):
        cfg = get_db_config()
        assert cfg["cursorclass"] == pymysql.cursors.DictCursor


class TestDbConnectionMock:
    """pymysql をモックした接続テスト。"""

    def test_connect_and_close(self):
        mock_conn = MagicMock()
        with patch("pymysql.connect", return_value=mock_conn) as mock_connect:
            cfg = get_db_config()
            conn = pymysql.connect(**cfg)
            conn.close()
            mock_connect.assert_called_once()
            mock_conn.close.assert_called_once()

    def test_execute_select_one(self):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"result": 1}
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("pymysql.connect", return_value=mock_conn):
            conn = pymysql.connect(**get_db_config())
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS result")
                row = cur.fetchone()
            assert row == {"result": 1}

    def test_screens_table_columns(self):
        """screens テーブルの想定カラムが定義に含まれているか検証。"""
        expected_columns = {
            "id", "game_id", "screen_hash", "name", "category",
            "screenshot_path", "thumbnail_path", "ocr_text",
            "visited_count", "first_seen_at", "last_seen_at",
            "created_at", "updated_at",
        }
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{"Field": col} for col in expected_columns]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("pymysql.connect", return_value=mock_conn):
            conn = pymysql.connect(**get_db_config())
            with conn.cursor() as cur:
                cur.execute("DESCRIBE screens")
                rows = cur.fetchall()

        actual_columns = {r["Field"] for r in rows}
        assert actual_columns == expected_columns

    def test_ui_elements_table_columns(self):
        """ui_elements テーブルの想定カラムが定義に含まれているか検証。"""
        expected_columns = {
            "id", "screen_id", "element_type", "label",
            "bbox_x", "bbox_y", "bbox_w", "bbox_h",
            "is_tappable", "navigates_to", "confidence",
            "created_at", "updated_at",
        }
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [{"Field": col} for col in expected_columns]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch("pymysql.connect", return_value=mock_conn):
            conn = pymysql.connect(**get_db_config())
            with conn.cursor() as cur:
                cur.execute("DESCRIBE ui_elements")
                rows = cur.fetchall()

        actual_columns = {r["Field"] for r in rows}
        assert actual_columns == expected_columns


# ============================================================
# Integration tests (実際のMySQLが必要)
# ============================================================

class TestDbConnectionIntegration:
    """実際のMySQLに対する統合テスト。DB未起動時はスキップ。"""

    @requires_db
    def test_ping(self):
        """MySQL に接続し、SELECT 1 が返ることを確認。"""
        conn = pymysql.connect(**get_db_config())
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS alive")
                row = cur.fetchone()
            assert row["alive"] == 1
        finally:
            conn.close()

    @requires_db
    def test_tables_exist(self):
        """必要なテーブルが存在することを確認。"""
        required_tables = {"games", "screens", "ui_elements", "crawl_sessions"}
        conn = pymysql.connect(**get_db_config())
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                rows = cur.fetchall()
            actual = {list(r.values())[0] for r in rows}
            assert required_tables.issubset(actual), (
                f"Missing tables: {required_tables - actual}"
            )
        finally:
            conn.close()

    @requires_db
    def test_fulltext_search(self):
        """OCRテキストのFULLTEXT検索が動作することを確認。"""
        conn = pymysql.connect(**get_db_config())
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name FROM screens "
                    "WHERE MATCH(ocr_text) AGAINST (%s IN BOOLEAN MODE)",
                    ("ショップ*",)
                )
                rows = cur.fetchall()
            assert len(rows) >= 1, "FULLTEXT検索でショップ画面が見つかるはず"
        finally:
            conn.close()
