"""WriteWorker のテスト。"""
import sqlite3
import threading
from pathlib import Path

import pytest

from tools.ap.write_worker import (
    WriteWorker, get_write_worker, shutdown_write_worker,
    reset_write_worker_for_test,
)


def _setup_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS test_writes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _count(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT COUNT(*) FROM test_writes").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_singleton():
    """各テスト前後で singleton をリセット。"""
    reset_write_worker_for_test()
    yield
    reset_write_worker_for_test()


class TestWriteWorker:
    def test_submit_async_writes(self, tmp_path):
        """submit() で投入したデータが反映される。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        worker.submit("INSERT INTO test_writes (value) VALUES (?)", ("a",))
        worker.submit("INSERT INTO test_writes (value) VALUES (?)", ("b",))
        worker.close()  # drain して終了

        assert _count(db) == 2

    def test_submit_sync_waits(self, tmp_path):
        """submit_sync() は完了まで待つ。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        worker.submit_sync(
            "INSERT INTO test_writes (value) VALUES (?)", ("sync",)
        )
        # close 前に既に書き込み完了している
        assert _count(db) == 1
        worker.close()

    def test_submit_sync_raises_on_error(self, tmp_path):
        """submit_sync は SQL エラーを呼び出し側に伝える。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        with pytest.raises(sqlite3.OperationalError):
            worker.submit_sync(
                "INSERT INTO nonexistent_table (x) VALUES (?)", (1,)
            )
        worker.close(drain=False)

    def test_concurrent_submits_are_serialized(self, tmp_path):
        """50 スレッド × 10 件 を並列 submit しても全件記録される。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        def producer(thread_id: int):
            for i in range(10):
                worker.submit(
                    "INSERT INTO test_writes (value) VALUES (?)",
                    (f"t{thread_id}-{i}",),
                )

        threads = [threading.Thread(target=producer, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        worker.close()  # drain 待ち
        assert _count(db) == 500

    def test_executemany_inserts_all(self, tmp_path):
        """executemany で複数行投入される。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        rows = [(f"v{i}",) for i in range(100)]
        worker.executemany("INSERT INTO test_writes (value) VALUES (?)", rows)
        worker.close()
        assert _count(db) == 100

    def test_close_with_drain_processes_queued(self, tmp_path):
        """close(drain=True) はキュー残を処理してから終了する。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        for i in range(20):
            worker.submit("INSERT INTO test_writes (value) VALUES (?)", (str(i),))
        worker.close(drain=True)
        assert _count(db) == 20

    def test_close_without_drain_may_skip(self, tmp_path):
        """close(drain=False) は queue 残を保証しない (= 性能優先)。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        # 大量に投入してすぐ close — 一部は処理されるが全件保証はない
        for i in range(50):
            worker.submit("INSERT INTO test_writes (value) VALUES (?)", (str(i),))
        worker.close(drain=False, timeout=1.0)
        # drain しないので何件残っているかは非決定的。0 ≤ count ≤ 50
        cnt = _count(db)
        assert 0 <= cnt <= 50

    def test_singleton_returns_same_instance(self, tmp_path):
        """get_write_worker は singleton を返す。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        w1 = get_write_worker(db)
        w2 = get_write_worker(db)
        assert w1 is w2
        shutdown_write_worker()

    def test_singleton_recreated_after_shutdown(self, tmp_path):
        """shutdown 後に再度 get すると新しい instance が作られる。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        w1 = get_write_worker(db)
        shutdown_write_worker()
        w2 = get_write_worker(db)
        assert w1 is not w2
        shutdown_write_worker()

    def test_invalid_sql_does_not_crash_worker(self, tmp_path):
        """1 つの SQL が失敗してもワーカーは継続する。"""
        db = tmp_path / "test.db"
        _setup_db(db)
        worker = WriteWorker(db)
        worker.start()

        worker.submit("INVALID SQL", ())
        worker.submit("INSERT INTO test_writes (value) VALUES (?)", ("ok",))
        worker.close()
        assert _count(db) == 1  # 無効な SQL はスキップ、有効な分だけ記録
