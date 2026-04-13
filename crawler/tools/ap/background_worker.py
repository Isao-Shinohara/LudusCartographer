"""
バックグラウンドワーカー — auto_pilot と並行して間引き・OCR再処理をリアルタイム実行。

デーモンスレッドで動作し、auto_pilot のメインループに影響を与えない。
SQLite WAL モードで並行アクセスする。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PHASH_CLUSTER_THRESHOLD = 8  # batch_processor.py と同じ


class BackgroundWorker:
    """auto_pilot と並行動作するバックグラウンド処理ワーカー。"""

    def __init__(
        self,
        db_path: Path,
        interval_dedup: float = 15.0,
        interval_ocr: float = 5.0,
    ):
        self._db_path = db_path
        self._interval_dedup = interval_dedup
        self._interval_ocr = interval_ocr
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 統計
        self.dedup_count = 0
        self.ocr_count = 0

    def start(self) -> None:
        """デーモンスレッドを起動。"""
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="bg_worker")
        self._thread.start()
        logger.info("[BG_WORKER] バックグラウンドワーカー起動")

    def stop(self) -> None:
        """停止シグナルを送り、スレッド終了を待つ。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("[BG_WORKER] 停止 (dedup=%d, ocr=%d)", self.dedup_count, self.ocr_count)

    def _get_conn(self) -> sqlite3.Connection:
        """スレッド専用の DB 接続を生成。"""
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _run_loop(self) -> None:
        """メインループ: 間引き → OCR再処理を交互に実行。"""
        # OpenCV スレッド数を制限 (CPU 競合防止)
        try:
            import cv2
            cv2.setNumThreads(1)
        except Exception:
            pass

        last_dedup = 0.0
        last_ocr = 0.0

        # 起動直後は 5 秒待機 (auto_pilot の初期化を邪魔しない)
        self._stop_event.wait(timeout=5.0)

        while not self._stop_event.is_set():
            now = time.time()

            # 間引き処理 (軽量: phash 比較のみ)
            if now - last_dedup >= self._interval_dedup:
                try:
                    self._run_incremental_dedup()
                except Exception as e:
                    logger.warning("[BG_WORKER] dedup 例外: %s", e)
                last_dedup = time.time()

            # OCR 再処理 (重量: 1枚ずつ)
            if now - last_ocr >= self._interval_ocr:
                try:
                    self._run_incremental_ocr()
                except Exception as e:
                    logger.warning("[BG_WORKER] ocr 例外: %s", e)
                last_ocr = time.time()

            self._stop_event.wait(timeout=3.0)

    def _run_incremental_dedup(self) -> None:
        """未処理スクリーンに対して phash クラスタリングを実行。"""
        from lc.utils import phash_distance

        conn = self._get_conn()
        try:
            # cluster_id が NULL の未処理レコードを取得
            rows = conn.execute(
                "SELECT id, phash FROM lc_screens"
                " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
                " ORDER BY discovered_at"
            ).fetchall()

            if not rows:
                return

            # 既存クラスタの代表 phash を取得 (cluster_id → phash マッピング)
            existing_reps = conn.execute(
                "SELECT cluster_id, phash FROM lc_screens"
                " WHERE is_representative = 1 AND phash IS NOT NULL"
                " ORDER BY cluster_id"
            ).fetchall()
            rep_map: dict[int, str] = {r["cluster_id"]: r["phash"] for r in existing_reps}

            # 次のクラスタ ID
            max_cid = conn.execute(
                "SELECT COALESCE(MAX(cluster_id), -1) FROM lc_screens"
            ).fetchone()[0]
            next_cid = max_cid + 1

            processed = 0
            for row in rows:
                sid = row["id"]
                ph = row["phash"]

                # 既存クラスタとの距離を比較
                best_cid = None
                best_dist = 999
                for cid, rep_ph in rep_map.items():
                    if rep_ph:
                        d = phash_distance(rep_ph, ph)
                        if d < best_dist:
                            best_dist = d
                            best_cid = cid

                if best_dist < _PHASH_CLUSTER_THRESHOLD and best_cid is not None:
                    # 既存クラスタに割当て
                    conn.execute(
                        "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                        (best_cid, sid),
                    )
                else:
                    # 新規クラスタ作成 (この画像が代表)
                    conn.execute(
                        "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                        (next_cid, sid),
                    )
                    rep_map[next_cid] = ph
                    next_cid += 1

                processed += 1

            if processed > 0:
                conn.commit()
                self.dedup_count += processed
                logger.info("[BG_WORKER] dedup: %d 枚処理 (合計 %d)", processed, self.dedup_count)
        finally:
            conn.close()

    def _run_incremental_ocr(self) -> None:
        """is_representative=1 かつ ocr_text_hq IS NULL のスクリーンを 1 枚処理。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, screenshot_path FROM lc_screens"
                " WHERE is_representative = 1 AND ocr_text_hq IS NULL"
                " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                " ORDER BY discovered_at"
                " LIMIT 1"
            ).fetchone()

            if not row:
                return

            sid = row["id"]
            path = row["screenshot_path"]
            if not Path(path).exists():
                # ファイルがない場合はスキップマーク
                conn.execute(
                    "UPDATE lc_screens SET ocr_text_hq = '' WHERE id = ?", (sid,)
                )
                conn.commit()
                return

            # PaddleOCR で再処理
            os.environ["OCR_ENGINE"] = "paddle"
            try:
                import cv2
                from lc.ocr import run_ocr

                # WebP → 一時 PNG 変換 (PaddleOCR は WebP 非対応)
                _ocr_path = path
                _tmp_path = None
                if path.endswith(".webp"):
                    _img = cv2.imread(path)
                    if _img is None:
                        conn.execute(
                            "UPDATE lc_screens SET ocr_text_hq = '' WHERE id = ?", (sid,)
                        )
                        conn.commit()
                        return
                    _tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    cv2.imwrite(_tmp.name, _img)
                    _ocr_path = _tmp.name
                    _tmp_path = _tmp.name

                ocr_results = run_ocr(_ocr_path, lang="japan")

                # 一時ファイル削除
                if _tmp_path:
                    try:
                        os.unlink(_tmp_path)
                    except Exception:
                        pass

                hq_text = " ".join(
                    item.get("text", "") for item in ocr_results
                    if item.get("confidence", 0) >= 0.3
                )
                conn.execute(
                    "UPDATE lc_screens SET ocr_text_hq = ? WHERE id = ?",
                    (hq_text, sid),
                )

                # tappable_items も更新
                conn.execute(
                    "DELETE FROM lc_tappable_items WHERE screen_id = ?", (sid,)
                )
                tap_rows = [
                    (sid, item.get("text", "").strip(), item.get("confidence", 0))
                    for item in ocr_results
                    if item.get("confidence", 0) >= 0.3 and item.get("text", "").strip()
                ]
                if tap_rows:
                    conn.executemany(
                        "INSERT INTO lc_tappable_items (screen_id, text, confidence)"
                        " VALUES (?, ?, ?)",
                        tap_rows,
                    )

                conn.commit()
                self.ocr_count += 1
                logger.debug("[BG_WORKER] reocr: id=%d done (合計 %d)", sid, self.ocr_count)

            finally:
                # OCR エンジンを戻す
                os.environ.pop("OCR_ENGINE", None)

        finally:
            conn.close()
