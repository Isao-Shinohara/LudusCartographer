"""
API 使用量トラッキング。

Gemini API 呼び出し後に record_api_usage() を呼ぶだけで
lc_api_usage テーブルに使用量が記録される。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).parent.parent.parent / "storage"
_DB_PATH = _STORAGE_DIR / "ludus.db"


def _ensure_table(conn: sqlite3.Connection) -> None:
    """lc_api_usage テーブルが存在しなければ作成。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            purpose TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


_table_ensured = False
_ensure_lock_imported = False


def _ensure_table_via_worker() -> None:
    """WriteWorker 経由で 1 度だけテーブル作成を投げる (idempotent)。"""
    global _table_ensured
    if _table_ensured:
        return
    from tools.ap.write_worker import get_write_worker
    worker = get_write_worker(_DB_PATH)
    worker.submit_sync("""
        CREATE TABLE IF NOT EXISTS lc_api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            purpose TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """, ())
    _table_ensured = True


def record_api_usage(
    model: str,
    purpose: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    conn: sqlite3.Connection | None = None,
) -> None:
    """API 使用量を記録する。

    Args:
        model: モデル名 (e.g. "gemini-2.5-flash-lite")
        purpose: 用途 (e.g. "hq_ocr", "anchor_judgment")
        input_tokens: 入力トークン数
        output_tokens: 出力トークン数
        conn: 既存の DB 接続。None なら WriteWorker 経由で非同期書き込み。

    並列ワーカー (Gemini 8 並列等) から呼ばれても "database is locked"
    にならないよう、conn=None のケースは WriteWorker queue に投入する
    (fire-and-forget)。conn が渡される場合 (tag_judgment 等) は呼び出し
    側のトランザクション境界に従う従来動作。
    """
    if conn is not None:
        # 呼び出し側がトランザクション制御している (tag_judgment 等)
        try:
            _ensure_table(conn)
            conn.execute(
                "INSERT INTO lc_api_usage (model, purpose, input_tokens, output_tokens)"
                " VALUES (?, ?, ?, ?)",
                (model, purpose, input_tokens, output_tokens),
            )
            conn.commit()
            logger.debug(
                "[API_USAGE] %s/%s: in=%d out=%d (caller conn)",
                model, purpose, input_tokens, output_tokens,
            )
        except Exception as e:
            logger.warning("[API_USAGE] 記録失敗 (caller conn): %s", e)
        return

    # conn=None → WriteWorker 経由 (lock 競合なし)
    try:
        _ensure_table_via_worker()
        from tools.ap.write_worker import get_write_worker
        worker = get_write_worker(_DB_PATH)
        worker.submit(
            "INSERT INTO lc_api_usage (model, purpose, input_tokens, output_tokens)"
            " VALUES (?, ?, ?, ?)",
            (model, purpose, input_tokens, output_tokens),
        )
        logger.debug(
            "[API_USAGE] %s/%s: in=%d out=%d (queued)",
            model, purpose, input_tokens, output_tokens,
        )
    except Exception as e:
        logger.warning("[API_USAGE] 記録失敗: %s", e)


def extract_usage_from_response(response) -> tuple[int, int]:
    """Gemini レスポンスからトークン数を抽出する。

    Returns:
        (input_tokens, output_tokens)
    """
    try:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return 0, 0
        input_tokens = getattr(meta, "prompt_token_count", 0) or 0
        output_tokens = getattr(meta, "candidates_token_count", 0) or 0
        return input_tokens, output_tokens
    except Exception:
        return 0, 0
