"""
バックグラウンドワーカー — auto_pilot と並行して全バッチ処理をリアルタイム実行。

デーモンスレッドで動作し、auto_pilot のメインループに影響を与えない。
SQLite WAL モードで並行アクセスする。

処理内容:
  1. グルーピング (scene + 時間ギャップでグループ化)
  2. phash クラスタリング (間引き + 代表選出)
  3. PaddleOCR 再処理 (代表画像のみ)
  4. 遷移グラフ構築 (BFS + SCC)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_PHASH_CLUSTER_THRESHOLD = 8
_GROUP_GAP_SECONDS = 60

_SCENE_LABELS = {
    "BATTLE": "バトル",
    "ADV": "ストーリー",
    "MENU": "メニュー",
    "MOVIE": "ムービー",
    "GACHA": "ガチャ",
    "UNKNOWN": "シーン",
}


class BackgroundWorker:
    """auto_pilot と並行動作するバックグラウンド処理ワーカー。"""

    def __init__(
        self,
        db_path: Path,
        interval_dedup: float = 15.0,
        interval_ocr: float = 5.0,
        interval_group: float = 30.0,
        interval_graph: float = 120.0,
    ):
        self._db_path = db_path
        self._interval_dedup = interval_dedup
        self._interval_ocr = interval_ocr
        self._interval_group = interval_group
        self._interval_graph = interval_graph
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 統計
        self.dedup_count = 0
        self.ocr_count = 0
        self.group_count = 0
        self.graph_sccs = 0

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
        logger.info(
            "[BG_WORKER] 停止 (group=%d, dedup=%d, ocr=%d, scc=%d)",
            self.group_count, self.dedup_count, self.ocr_count, self.graph_sccs,
        )

    def _get_conn(self) -> sqlite3.Connection:
        """スレッド専用の DB 接続を生成。"""
        conn = sqlite3.connect(str(self._db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _run_loop(self) -> None:
        """メインループ。"""
        try:
            import cv2
            cv2.setNumThreads(1)
        except Exception:
            pass

        last_dedup = 0.0
        last_ocr = 0.0
        last_group = 0.0
        last_graph = 0.0

        # 起動直後は 5 秒待機
        self._stop_event.wait(timeout=5.0)

        while not self._stop_event.is_set():
            now = time.time()

            # グルーピング (30秒間隔)
            if now - last_group >= self._interval_group:
                try:
                    self._run_incremental_group()
                except Exception as e:
                    logger.warning("[BG_WORKER] group 例外: %s", e)
                last_group = time.time()

            # 間引き処理 (15秒間隔)
            if now - last_dedup >= self._interval_dedup:
                try:
                    self._run_incremental_dedup()
                except Exception as e:
                    logger.warning("[BG_WORKER] dedup 例外: %s", e)
                last_dedup = time.time()

            # OCR 再処理 (5秒間隔/1枚)
            if now - last_ocr >= self._interval_ocr:
                try:
                    self._run_incremental_ocr()
                except Exception as e:
                    logger.warning("[BG_WORKER] ocr 例外: %s", e)
                last_ocr = time.time()

            # 遷移グラフ構築 (120秒間隔)
            if now - last_graph >= self._interval_graph:
                try:
                    self._run_graph_build()
                except Exception as e:
                    logger.warning("[BG_WORKER] graph 例外: %s", e)
                last_graph = time.time()

            self._stop_event.wait(timeout=3.0)

    # ─── グルーピング ─────────────────────────────────────

    def _run_incremental_group(self) -> None:
        """group_id が NULL の未グループ化スクリーンをグルーピング。"""
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, session_id, scene, discovered_at FROM lc_screens"
                " WHERE group_id IS NULL"
                " ORDER BY session_id, discovered_at"
            ).fetchall()

            if not rows:
                return

            # lc_screen_groups テーブルを保証
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS lc_screen_groups (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id   TEXT,
                    label        TEXT,
                    scene        TEXT,
                    seq          INTEGER,
                    started_at   TEXT,
                    ended_at     TEXT,
                    screen_count INTEGER DEFAULT 0
                );
            """)

            # 既存グループの scene 連番を取得
            existing_seqs = {}
            for r in conn.execute(
                "SELECT session_id, scene, MAX(seq) as max_seq FROM lc_screen_groups"
                " GROUP BY session_id, scene"
            ).fetchall():
                existing_seqs[f"{r['session_id']}_{r['scene']}"] = r["max_seq"]

            groups_created = 0
            current_session = None
            current_scene = None
            current_group: list[dict] = []
            last_time: Optional[datetime] = None

            def _flush():
                nonlocal groups_created
                if not current_group:
                    return

                scene = current_group[0]["scene"] or "UNKNOWN"
                sid = current_group[0]["session_id"]
                key = f"{sid}_{scene}"
                seq = existing_seqs.get(key, 0) + 1
                existing_seqs[key] = seq

                label_prefix = _SCENE_LABELS.get(scene, "シーン")
                label = f"{label_prefix}#{seq}"

                cur = conn.execute(
                    "INSERT INTO lc_screen_groups"
                    " (session_id, label, scene, seq, started_at, ended_at, screen_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, label, scene, seq,
                     current_group[0]["discovered_at"],
                     current_group[-1]["discovered_at"],
                     len(current_group)),
                )
                gid = cur.lastrowid
                screen_ids = [s["id"] for s in current_group]
                conn.execute(
                    f"UPDATE lc_screens SET group_id = ?"
                    f" WHERE id IN ({','.join('?' * len(screen_ids))})",
                    [gid] + screen_ids,
                )
                groups_created += 1

            for row in rows:
                row_dict = dict(row)
                scene = row_dict.get("scene") or "UNKNOWN"
                sid = row_dict["session_id"]
                ts_str = row_dict.get("discovered_at") or ""
                try:
                    ts = datetime.fromisoformat(ts_str) if ts_str else None
                except ValueError:
                    ts = None

                if sid != current_session:
                    _flush()
                    current_group = []
                    current_session = sid
                    current_scene = None
                    last_time = None

                time_gap = False
                if ts and last_time:
                    gap = (ts - last_time).total_seconds()
                    if gap > _GROUP_GAP_SECONDS:
                        time_gap = True

                if scene != current_scene or time_gap:
                    _flush()
                    current_group = []
                    current_scene = scene

                current_group.append(row_dict)
                last_time = ts

            _flush()

            if groups_created > 0:
                conn.commit()
                self.group_count += groups_created
                logger.info("[BG_WORKER] group: %d グループ作成 (合計 %d)",
                            groups_created, self.group_count)
        finally:
            conn.close()

    # ─── phash クラスタリング ──────────────────────────────

    def _run_incremental_dedup(self) -> None:
        """未処理スクリーンに対してテキスト優先 + phash フォールバックでクラスタリング。

        1. OCR テキスト (title) が既存代表と一致 → 同一クラスタ（不採用）
        2. テキストが異なる or 空 → phash 距離で判定（フォールバック）
        3. どちらも一致しない → 新規クラスタ（採用）
        """
        from lc.utils import phash_distance

        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, phash, title FROM lc_screens"
                " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
                " ORDER BY discovered_at"
            ).fetchall()

            if not rows:
                return

            existing_reps = conn.execute(
                "SELECT cluster_id, phash, title FROM lc_screens"
                " WHERE is_representative = 1 AND phash IS NOT NULL"
                " ORDER BY cluster_id"
            ).fetchall()
            # rep_map: cluster_id → (phash, title)
            rep_map: dict[int, tuple[str, str]] = {
                r["cluster_id"]: (r["phash"], r["title"] or "")
                for r in existing_reps
            }

            max_cid = conn.execute(
                "SELECT COALESCE(MAX(cluster_id), -1) FROM lc_screens"
            ).fetchone()[0]
            next_cid = max_cid + 1

            processed = 0
            for row in rows:
                sid = row["id"]
                ph = row["phash"]
                title = row["title"] or ""

                # 1) テキスト一致チェック（空タイトルやシーン名のみは除外）
                _is_meaningful = len(title) > 5 and not title.startswith("(")
                text_match_cid = None
                if _is_meaningful:
                    for cid, (rep_ph, rep_title) in rep_map.items():
                        if rep_title == title:
                            text_match_cid = cid
                            break

                if text_match_cid is not None:
                    # テキスト完全一致 → 不採用
                    conn.execute(
                        "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                        (text_match_cid, sid),
                    )
                else:
                    # 2) phash フォールバック
                    best_cid = None
                    best_dist = 999
                    for cid, (rep_ph, _) in rep_map.items():
                        if rep_ph:
                            d = phash_distance(rep_ph, ph)
                            if d < best_dist:
                                best_dist = d
                                best_cid = cid

                    if best_dist < _PHASH_CLUSTER_THRESHOLD and best_cid is not None:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                            (best_cid, sid),
                        )
                    else:
                        # 新規クラスタ（採用）
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (next_cid, sid),
                        )
                        rep_map[next_cid] = (ph, title)
                        next_cid += 1

                processed += 1

            if processed > 0:
                conn.commit()
                self.dedup_count += processed
                logger.info("[BG_WORKER] dedup: %d 枚処理 (合計 %d)", processed, self.dedup_count)
        finally:
            conn.close()

    # ─── PaddleOCR 再処理 ─────────────────────────────────

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
                conn.execute(
                    "UPDATE lc_screens SET ocr_text_hq = '' WHERE id = ?", (sid,)
                )
                conn.commit()
                return

            os.environ["OCR_ENGINE"] = "paddle"
            try:
                import cv2
                from lc.ocr import run_ocr

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
                if self.ocr_count % 50 == 0:
                    remaining = conn.execute(
                        "SELECT COUNT(*) FROM lc_screens"
                        " WHERE is_representative = 1 AND ocr_text_hq IS NULL"
                        " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                    ).fetchone()[0]
                    logger.info("[BG_WORKER] reocr: %d 枚完了 (残り %d)",
                                self.ocr_count, remaining)

            finally:
                os.environ.pop("OCR_ENGINE", None)

        finally:
            conn.close()

    # ─── 遷移グラフ構築 ───────────────────────────────────

    def _run_graph_build(self) -> None:
        """全セッションの dedup + OCR が完了してから遷移グラフを構築。"""
        conn = self._get_conn()
        try:
            # 未処理が残っていたらスキップ
            pending_cluster = conn.execute(
                "SELECT COUNT(*) FROM lc_screens"
                " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
            ).fetchone()[0]
            pending_ocr = conn.execute(
                "SELECT COUNT(*) FROM lc_screens"
                " WHERE is_representative = 1 AND ocr_text_hq IS NULL"
                " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
            ).fetchone()[0]
            if pending_cluster > 0 or pending_ocr > 0:
                logger.debug(
                    "[BG_WORKER] graph: 待機中 (cluster残=%d, ocr残=%d)",
                    pending_cluster, pending_ocr,
                )
                return
        finally:
            conn.close()

        try:
            from tools.batch_processor import BatchProcessor
            bp = BatchProcessor(db_path=self._db_path)
            try:
                sccs = bp.build_graph()
                if sccs > 0:
                    self.graph_sccs = sccs
                    logger.info("[BG_WORKER] graph: %d SCC 構築完了", sccs)
            finally:
                bp.close()
        except Exception as e:
            logger.warning("[BG_WORKER] graph 例外: %s", e)
