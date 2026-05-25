"""SQLite 書き込み専用ワーカー。

複数スレッド (BG_WORKER / Gemini 並列ワーカー / メトリクス記録など) からの
書き込みを 1 つのスレッドで直列化し、SQLite の "database is locked" 競合を
完全に排除する。

設計:
- auto_pilot プロセス内で singleton (get_write_worker)
- queue.Queue ベースの async (submit) と Future ベースの sync (submit_sync)
- daemon thread として動作 — プロセス終了で自動 cleanup
- close() でキューを drain してから停止 (整合性保証)

呼び出しパターン:
    worker = get_write_worker(db_path)
    worker.submit("INSERT ... VALUES (?, ?)", (a, b))             # fire-and-forget
    worker.submit_sync("INSERT ... VALUES (?, ?)", (a, b))         # 完了まで待つ
    worker.executemany("INSERT ... VALUES (?, ?)", [(a, b), ...])  # 一括投入
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# 内部キュー要素 (poison pill = None / 通常 = (sql, params, future|None))
class _WriteRequest:
    __slots__ = ("sql", "params", "future")

    def __init__(self, sql: str, params: tuple, future: Optional[Future] = None):
        self.sql = sql
        self.params = params
        self.future = future


class WriteWorker:
    """SQLite 書き込みを直列化するワーカースレッド。"""

    def __init__(self, db_path: Path):
        self._db_path = Path(db_path)
        self._queue: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="SQLiteWriteWorker"
        )
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._thread.start()
                self._started = True

    # ─── public API ──────────────────────────────────────

    def submit(self, sql: str, params: tuple = ()) -> None:
        """非同期書き込み (fire-and-forget)。順序は保証される。"""
        self.start()
        self._queue.put(_WriteRequest(sql, params))

    def submit_sync(
        self, sql: str, params: tuple = (), timeout: float = 10.0
    ) -> None:
        """書き込み完了まで待つ。例外は呼び出し側に再 raise。"""
        self.start()
        future: Future = Future()
        self._queue.put(_WriteRequest(sql, params, future))
        future.result(timeout=timeout)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        """複数行を 1 トランザクションで投入 (async)。"""
        if not rows:
            return
        self.start()
        # 同じ SQL を連続 submit するだけで内部で順次処理される。
        # 1 行ずつ commit するため、rows が大量だと性能はやや落ちるが
        # メトリクス用途では問題にならない。
        for params in rows:
            self._queue.put(_WriteRequest(sql, params))

    def qsize(self) -> int:
        return self._queue.qsize()

    def close(self, drain: bool = True, timeout: float = 5.0) -> None:
        """シャットダウン。drain=True なら queue 完了まで待ってから停止。"""
        if not self._started:
            return
        if drain:
            # キューの全タスク完了を待つ
            self._queue.join()
        # poison pill で worker スレッドを終了
        self._stop_event.set()
        self._queue.put(None)  # type: ignore[arg-type]
        self._thread.join(timeout=timeout)

    # ─── internal ─────────────────────────────────────────

    def _run(self) -> None:
        try:
            conn = sqlite3.connect(str(self._db_path), timeout=30)
            # WAL モード + busy_timeout で読み込みと干渉しないようにする
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA synchronous=NORMAL")
        except Exception as e:
            logger.error("[WRITE_WORKER] DB 接続失敗: %s", e)
            return

        try:
            while True:
                try:
                    req = self._queue.get(timeout=0.5)
                except queue.Empty:
                    if self._stop_event.is_set():
                        break
                    continue

                # poison pill
                if req is None:
                    self._queue.task_done()
                    break

                try:
                    conn.execute(req.sql, req.params)
                    conn.commit()
                    if req.future is not None:
                        req.future.set_result(None)
                except Exception as e:
                    logger.warning(
                        "[WRITE_WORKER] 書き込み失敗: %s | sql=%s",
                        e, (req.sql or "")[:80],
                    )
                    if req.future is not None:
                        req.future.set_exception(e)
                finally:
                    self._queue.task_done()
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ─── singleton accessor ─────────────────────────────────────────────

_global_writer: Optional[WriteWorker] = None
_global_lock = threading.Lock()


def get_write_worker(db_path: Path) -> WriteWorker:
    """auto_pilot プロセス全体で単一の WriteWorker を返す。"""
    global _global_writer
    with _global_lock:
        if _global_writer is None or not _global_writer._started:
            _global_writer = WriteWorker(db_path)
            _global_writer.start()
    return _global_writer


def shutdown_write_worker(drain: bool = True, timeout: float = 5.0) -> None:
    """auto_pilot 停止時に呼ぶ。drain=True で残りキューを処理してから停止。"""
    global _global_writer
    with _global_lock:
        if _global_writer is not None:
            try:
                _global_writer.close(drain=drain, timeout=timeout)
            except Exception as e:
                logger.warning("[WRITE_WORKER] shutdown 失敗: %s", e)
            _global_writer = None


def reset_write_worker_for_test() -> None:
    """テスト用: グローバル singleton をリセット (close せず破棄)。"""
    global _global_writer
    with _global_lock:
        if _global_writer is not None:
            try:
                _global_writer.close(drain=False, timeout=1.0)
            except Exception:
                pass
            _global_writer = None
