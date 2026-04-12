"""
バッチプロセッサ — スクショの間引き・グルーピング・OCR再処理。

リアルタイム撮影 (screen_recorder.py) で寛容に撮りためたスクショを
バッチで間引き・分類・OCR再処理する。全てローカル完結。

設計書: docs/screen_recorder.md

使い方:
    venv/bin/python tools/batch_processor.py                    # 全処理
    venv/bin/python tools/batch_processor.py --group            # Phase 1
    venv/bin/python tools/batch_processor.py --deduplicate      # Phase 2
    venv/bin/python tools/batch_processor.py --reocr            # Phase 3
    venv/bin/python tools/batch_processor.py --session ap_xxx   # セッション指定
    venv/bin/python tools/batch_processor.py --dry-run          # ドライラン
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# crawler/ をモジュール検索パスに追加
_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))

logger = logging.getLogger(__name__)

# ─── 定数 ────────────────────────────────────────────
_DEFAULT_DB = Path(__file__).parent.parent / "storage" / "ludus.db"
_SCREENSHOTS_ROOT = Path(__file__).parent.parent / "storage" / "screenshots"
_FINAL_DIR = _SCREENSHOTS_ROOT / "final"
_GROUP_GAP_SECONDS = 60  # 同一 scene でもこの秒数以上の空白で別グループ
_PHASH_CLUSTER_THRESHOLD = 8  # phash 距離がこれ未満なら同一クラスタ

_SCENE_LABELS = {
    "BATTLE": "バトル",
    "ADV": "ストーリー",
    "MENU": "メニュー",
    "GACHA": "ガチャ",
    "UNKNOWN": "シーン",
}

# ─── スキーマ ─────────────────────────────────────────
_GROUPS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lc_screen_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    scene        TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    screen_count INTEGER DEFAULT 0
);
"""


class BatchProcessor:
    """スクショのバッチ処理器。"""

    def __init__(self, db_path: Path = _DEFAULT_DB, dry_run: bool = False) -> None:
        self._db_path = Path(db_path)
        self._dry_run = dry_run
        self._conn = sqlite3.connect(str(self._db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        """テーブル・カラムの自動マイグレーション。"""
        self._conn.executescript(_GROUPS_SCHEMA)

        # lc_transitions テーブル (遷移グラフ Phase 1)
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS lc_transitions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT    NOT NULL,
                from_screen_id  INTEGER NOT NULL,
                to_screen_id    INTEGER,
                from_fp         TEXT    NOT NULL,
                to_fp           TEXT,
                tap_x           INTEGER,
                tap_y           INTEGER,
                tap_label       TEXT,
                action_name     TEXT,
                discovered_at   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_trans_from ON lc_transitions(from_fp);
            CREATE INDEX IF NOT EXISTS idx_trans_to ON lc_transitions(to_fp);
            CREATE INDEX IF NOT EXISTS idx_trans_session ON lc_transitions(session_id);
        """)

        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_screens)")}
        for col, typ in [
            ("group_id", "INTEGER"),
            ("is_representative", "BOOLEAN DEFAULT 0"),
            ("cluster_id", "INTEGER"),
            ("ocr_text_hq", "TEXT"),
        ]:
            if col not in cols:
                self._conn.execute(f"ALTER TABLE lc_screens ADD COLUMN {col} {typ}")
                logger.info("[BatchProcessor] migrate: %s カラム追加", col)
        self._conn.commit()

    # ─── Phase 1: グルーピング + ラベル付け ────────────

    def group(self, session_id: Optional[str] = None) -> int:
        """時系列 + scene でグルーピングし、ラベルを付ける。

        Returns: 作成されたグループ数
        """
        where = "WHERE session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        rows = self._conn.execute(
            f"SELECT id, session_id, scene, discovered_at FROM lc_screens"
            f" {where} ORDER BY session_id, discovered_at",
            params,
        ).fetchall()

        if not rows:
            logger.info("[group] 対象スクリーンなし")
            return 0

        # 既存グループをクリア (再実行可能にする)
        if not self._dry_run:
            if session_id:
                self._conn.execute(
                    "DELETE FROM lc_screen_groups WHERE session_id = ?", (session_id,)
                )
                self._conn.execute(
                    "UPDATE lc_screens SET group_id = NULL WHERE session_id = ?",
                    (session_id,),
                )
            else:
                self._conn.execute("DELETE FROM lc_screen_groups")
                self._conn.execute("UPDATE lc_screens SET group_id = NULL")

        groups_created = 0
        scene_counters: dict[str, int] = {}  # scene → 連番カウンタ

        current_session = None
        current_scene = None
        current_group_screens: list[dict] = []
        last_time: Optional[datetime] = None

        def _flush_group():
            nonlocal groups_created
            if not current_group_screens:
                return

            scene = current_group_screens[0]["scene"] or "UNKNOWN"
            sid = current_group_screens[0]["session_id"]

            # 連番カウンタ
            key = f"{sid}_{scene}"
            scene_counters[key] = scene_counters.get(key, 0) + 1
            seq = scene_counters[key]

            label_prefix = _SCENE_LABELS.get(scene, "シーン")
            label = f"{label_prefix}#{seq}"

            started = current_group_screens[0]["discovered_at"]
            ended = current_group_screens[-1]["discovered_at"]
            count = len(current_group_screens)

            if self._dry_run:
                logger.info(
                    "[group][dry-run] %s: %d枚 (%s ~ %s)",
                    label, count, started, ended,
                )
                groups_created += 1
                return

            cur = self._conn.execute(
                "INSERT INTO lc_screen_groups"
                " (session_id, label, scene, seq, started_at, ended_at, screen_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, label, scene, seq, started, ended, count),
            )
            group_id = cur.lastrowid

            screen_ids = [s["id"] for s in current_group_screens]
            self._conn.execute(
                f"UPDATE lc_screens SET group_id = ? WHERE id IN ({','.join('?' * len(screen_ids))})",
                [group_id] + screen_ids,
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

            # セッション変更 → flush
            if sid != current_session:
                _flush_group()
                current_group_screens = []
                current_session = sid
                current_scene = None
                last_time = None
                scene_counters = {k: v for k, v in scene_counters.items() if not k.startswith(f"{sid}_")}

            # scene 変更 or 時間ギャップ → flush + 新グループ
            time_gap = False
            if ts and last_time:
                gap = (ts - last_time).total_seconds()
                if gap > _GROUP_GAP_SECONDS:
                    time_gap = True

            if scene != current_scene or time_gap:
                _flush_group()
                current_group_screens = []
                current_scene = scene

            current_group_screens.append(row_dict)
            last_time = ts

        # 最後のグループ
        _flush_group()

        if not self._dry_run:
            self._conn.commit()

        logger.info("[group] %d グループ作成 (%d スクリーン)", groups_created, len(rows))
        return groups_created

    # ─── Phase 2: phash クラスタリング間引き ───────────

    def deduplicate(self, session_id: Optional[str] = None) -> int:
        """グループ内で phash クラスタリングし、代表1枚を選出。

        Returns: 代表画像の数
        """
        from lc.utils import phash_distance

        where = "WHERE g.session_id = ?" if session_id else ""
        params = (session_id,) if session_id else ()

        groups = self._conn.execute(
            f"SELECT g.id, g.label FROM lc_screen_groups g {where} ORDER BY g.id",
            params,
        ).fetchall()

        if not groups:
            logger.info("[deduplicate] グループなし（先に --group を実行）")
            return 0

        total_reps = 0

        for group in groups:
            gid = group["id"]
            screens = self._conn.execute(
                "SELECT id, phash FROM lc_screens"
                " WHERE group_id = ? ORDER BY discovered_at",
                (gid,),
            ).fetchall()

            if not screens:
                continue

            # クラスタリング
            clusters: list[list[int]] = []  # [[screen_id, ...], ...]
            current_cluster: list[int] = [screens[0]["id"]]
            last_phash = screens[0]["phash"] or ""

            for screen in screens[1:]:
                ph = screen["phash"] or ""
                if last_phash and ph:
                    dist = phash_distance(last_phash, ph)
                else:
                    dist = 999

                if dist < _PHASH_CLUSTER_THRESHOLD:
                    current_cluster.append(screen["id"])
                else:
                    clusters.append(current_cluster)
                    current_cluster = [screen["id"]]
                last_phash = ph

            clusters.append(current_cluster)

            # 代表選出 (各クラスタの中央)
            if not self._dry_run:
                # リセット
                all_ids = [s["id"] for s in screens]
                self._conn.execute(
                    f"UPDATE lc_screens SET is_representative = 0, cluster_id = NULL"
                    f" WHERE id IN ({','.join('?' * len(all_ids))})",
                    all_ids,
                )

            for ci, cluster in enumerate(clusters):
                rep_idx = len(cluster) // 2
                rep_id = cluster[rep_idx]

                if not self._dry_run:
                    # クラスタ番号を設定
                    self._conn.execute(
                        f"UPDATE lc_screens SET cluster_id = ?"
                        f" WHERE id IN ({','.join('?' * len(cluster))})",
                        [ci] + cluster,
                    )
                    # 代表フラグ
                    self._conn.execute(
                        "UPDATE lc_screens SET is_representative = 1 WHERE id = ?",
                        (rep_id,),
                    )

                total_reps += 1

            if self._dry_run:
                logger.info(
                    "[deduplicate][dry-run] %s: %d枚 → %dクラスタ",
                    group["label"], len(screens), len(clusters),
                )

        if not self._dry_run:
            self._conn.commit()

        logger.info("[deduplicate] 代表画像 %d 枚選出", total_reps)
        return total_reps

    # ─── 間引きファイル移動 ─────────────────────────────

    def move_thinned(self, session_id: Optional[str] = None) -> int:
        """間引かれた（非代表）画像をセッションディレクトリ内の thinned/ に移動。

        Returns: 移動したファイル数
        """
        import shutil

        where = "WHERE is_representative = 0 AND screenshot_path != ''"
        params: list = []
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)

        screens = self._conn.execute(
            f"SELECT id, session_id, screenshot_path, thumbnail_path"
            f" FROM lc_screens {where}",
            params,
        ).fetchall()

        moved = 0
        for screen in screens:
            ss_path = Path(screen["screenshot_path"]) if screen["screenshot_path"] else None
            thumb_path = Path(screen["thumbnail_path"]) if screen["thumbnail_path"] else None

            if ss_path and ss_path.exists():
                thinned_dir = ss_path.parent / "thinned"
                thinned_dir.mkdir(exist_ok=True)
                dst = thinned_dir / ss_path.name
                shutil.move(str(ss_path), str(dst))
                # DB のパスも更新
                if not self._dry_run:
                    self._conn.execute(
                        "UPDATE lc_screens SET screenshot_path = ? WHERE id = ?",
                        (str(dst), screen["id"]),
                    )
                moved += 1

            if thumb_path and thumb_path.exists():
                thinned_dir = thumb_path.parent / "thinned"
                thinned_dir.mkdir(exist_ok=True)
                dst_t = thinned_dir / thumb_path.name
                shutil.move(str(thumb_path), str(dst_t))
                if not self._dry_run:
                    self._conn.execute(
                        "UPDATE lc_screens SET thumbnail_path = ? WHERE id = ?",
                        (str(dst_t), screen["id"]),
                    )

        if not self._dry_run:
            self._conn.commit()

        logger.info("[move_thinned] %d ファイル移動 → thinned/", moved)
        return moved

    # ─── 統合: 代表画像を final/ にコピー ────────────────

    def integrate(self, session_id: Optional[str] = None) -> int:
        """代表画像を final/ ディレクトリに統合コピー。

        fingerprint ベースで重複排除し、既に final/ にある画像はスキップ。

        Returns: 新規コピーした画像数
        """
        import shutil

        _FINAL_DIR.mkdir(parents=True, exist_ok=True)

        where = "WHERE is_representative = 1 AND screenshot_path != ''"
        params: list = []
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)

        screens = self._conn.execute(
            f"SELECT id, fingerprint, screenshot_path, thumbnail_path"
            f" FROM lc_screens {where}",
            params,
        ).fetchall()

        copied = 0
        skipped = 0
        for screen in screens:
            fp = screen["fingerprint"]
            ss_path = Path(screen["screenshot_path"]) if screen["screenshot_path"] else None

            if not ss_path or not ss_path.exists():
                continue

            # fingerprint ベースの決定版ファイル名
            final_path = _FINAL_DIR / f"{fp}.webp"
            if final_path.exists():
                skipped += 1
                continue

            if not self._dry_run:
                shutil.copy2(str(ss_path), str(final_path))

                # サムネイルもコピー
                thumb_path = Path(screen["thumbnail_path"]) if screen["thumbnail_path"] else None
                if thumb_path and thumb_path.exists():
                    final_thumb = _FINAL_DIR / f"{fp}_thumb.webp"
                    shutil.copy2(str(thumb_path), str(final_thumb))

            copied += 1

        logger.info(
            "[integrate] final/ に %d 枚コピー (%d 枚は既存スキップ)",
            copied, skipped,
        )
        return copied

    # ─── Phase 3: PaddleOCR 再処理 ────────────────────

    def reocr(self, session_id: Optional[str] = None) -> int:
        """代表画像に対して PaddleOCR フル解像度で OCR 再処理。

        Returns: 再処理した画像数
        """
        import os
        os.environ["OCR_ENGINE"] = "paddle"

        where = "WHERE is_representative = 1"
        params: list = []
        if session_id:
            where += " AND session_id = ?"
            params.append(session_id)

        screens = self._conn.execute(
            f"SELECT id, screenshot_path FROM lc_screens {where}",
            params,
        ).fetchall()

        if not screens:
            logger.info("[reocr] 代表画像なし（先に --deduplicate を実行）")
            return 0

        from lc.ocr import run_ocr

        processed = 0
        for screen in screens:
            sid = screen["id"]
            path = screen["screenshot_path"]
            if not path or not Path(path).exists():
                logger.warning("[reocr] 画像なし: id=%d path=%s", sid, path)
                continue

            if self._dry_run:
                logger.info("[reocr][dry-run] id=%d path=%s", sid, path)
                processed += 1
                continue

            try:
                # PaddleOCR は WebP 非対応 → 一時 PNG に変換
                import cv2
                import tempfile
                _ocr_path = path
                if path.endswith(".webp"):
                    _img = cv2.imread(path)
                    if _img is None:
                        logger.warning("[reocr] 画像読込失敗: %s", path)
                        continue
                    _tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    cv2.imwrite(_tmp.name, _img)
                    _ocr_path = _tmp.name

                ocr_results = run_ocr(_ocr_path, lang="japan")

                # 一時ファイル削除
                if _ocr_path != path:
                    os.unlink(_ocr_path)
                hq_text = " ".join(
                    item.get("text", "") for item in ocr_results
                    if item.get("confidence", 0) >= 0.3
                )
                self._conn.execute(
                    "UPDATE lc_screens SET ocr_text_hq = ? WHERE id = ?",
                    (hq_text, sid),
                )

                # tappable_items も更新
                self._conn.execute(
                    "DELETE FROM lc_tappable_items WHERE screen_id = ?", (sid,)
                )
                rows = [
                    (sid, item.get("text", "").strip(), item.get("confidence", 0))
                    for item in ocr_results
                    if item.get("confidence", 0) >= 0.3 and item.get("text", "").strip()
                ]
                if rows:
                    self._conn.executemany(
                        "INSERT INTO lc_tappable_items (screen_id, text, confidence)"
                        " VALUES (?, ?, ?)",
                        rows,
                    )

                processed += 1
                if processed % 10 == 0:
                    self._conn.commit()
                    logger.info("[reocr] %d/%d 処理済み", processed, len(screens))
            except Exception as e:
                logger.warning("[reocr] OCR 失敗 id=%d: %s", sid, e)

        if not self._dry_run:
            self._conn.commit()

        logger.info("[reocr] %d 枚の OCR 再処理完了", processed)
        return processed

    # ─── 全処理実行 ───────────────────────────────────

    def run_all(self, session_id: Optional[str] = None) -> None:
        """Phase 1-5 を順番に実行。"""
        logger.info("=" * 50)
        logger.info("  バッチプロセッサ開始")
        logger.info("=" * 50)

        g = self.group(session_id)
        d = self.deduplicate(session_id)
        m = self.move_thinned(session_id)
        r = self.reocr(session_id)
        i = self.integrate(session_id)

        logger.info("=" * 50)
        logger.info("  完了: %dグループ, %d代表, %d間引き移動, %d OCR再処理, %d統合",
                     g, d, m, r, i)
        logger.info("=" * 50)

    def close(self) -> None:
        self._conn.close()


# ─── CLI ──────────────────────────────────────────────
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="スクショ バッチプロセッサ")
    parser.add_argument("--db", type=str, default=str(_DEFAULT_DB),
                        help="SQLite DB パス")
    parser.add_argument("--session", type=str, default=None,
                        help="対象セッション ID (未指定時は最新セッション)")
    parser.add_argument("--all-sessions", action="store_true",
                        help="全セッション対象")
    parser.add_argument("--group", action="store_true",
                        help="Phase 1: グルーピング + ラベル付け")
    parser.add_argument("--deduplicate", action="store_true",
                        help="Phase 2: phash クラスタリング間引き")
    parser.add_argument("--reocr", action="store_true",
                        help="Phase 3: PaddleOCR 再処理")
    parser.add_argument("--move-thinned", action="store_true",
                        help="間引きファイルを thinned/ に移動")
    parser.add_argument("--integrate", action="store_true",
                        help="代表画像を final/ に統合コピー")
    parser.add_argument("--dry-run", action="store_true",
                        help="DB 変更なし、結果をログに出力")
    args = parser.parse_args()

    bp = BatchProcessor(db_path=Path(args.db), dry_run=args.dry_run)

    # セッション決定: --session > --all-sessions > 最新セッション
    session_id = args.session
    if not session_id and not args.all_sessions:
        row = bp._conn.execute(
            "SELECT session_id FROM lc_sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if row:
            session_id = row[0]
            logger.info("[BatchProcessor] 最新セッション: %s", session_id)
        else:
            logger.warning("[BatchProcessor] セッションが見つかりません")
    elif args.all_sessions:
        session_id = None

    try:
        if args.group:
            bp.group(session_id)
        elif args.deduplicate:
            bp.deduplicate(session_id)
        elif args.move_thinned:
            bp.move_thinned(session_id)
        elif args.reocr:
            bp.reocr(session_id)
        elif args.integrate:
            bp.integrate(session_id)
        else:
            bp.run_all(session_id)
    finally:
        bp.close()


if __name__ == "__main__":
    main()
