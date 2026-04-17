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


def _text_similarity(a: str, b: str) -> float:
    """2つのテキストの類似度 (文字レベル)。0.0〜1.0。

    difflib.SequenceMatcher を使い、日本語テキストでも正しく動作する。
    短文 (5文字以下) は完全一致のみ 1.0 を返す (OCR 誤読の誤マージ防止)。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 短文ガード: 5文字以下は完全一致のみ
    if min(len(a), len(b)) <= 5:
        return 0.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def _normalize_text(text: str) -> str:
    """OCR テキストの揺れを正規化して比較用文字列を生成。"""
    import re
    import unicodedata
    # Unicode 正規化 (全角英数→半角、半角カナ→全角 等)
    t = unicodedata.normalize("NFKC", text)
    # 三点リーダ / 中黒の揺れを統一
    t = t.replace("…", "...").replace("・・・", "...").replace("・・", "..")
    # 連続ドット・スペースを正規化
    t = re.sub(r'\.{2,}', '...', t)
    t = re.sub(r'\s+', ' ', t).strip()
    # 記号揺れ: 全角記号を半角に
    t = t.replace("＋", "+").replace("＆", "&").replace("！", "!").replace("？", "?")
    # 句読点・記号を除去 (OCR 誤読が多い文末記号の揺れを吸収)
    t = re.sub(r'[、。,.!?…・\-―~～\s]+$', '', t)
    return t


class BackgroundWorker:
    """auto_pilot と並行動作するバックグラウンド処理ワーカー。"""

    def __init__(
        self,
        db_path: Path,
        session_id: Optional[str] = None,
        interval_dedup: float = 15.0,
        interval_ocr: float = 0.5,
        interval_group: float = 30.0,
        interval_graph: float = 120.0,
    ):
        self._db_path = db_path
        self._session_id = session_id
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
        self._thread = threading.Thread(target=self._run_loop_safe, daemon=True, name="bg_worker")
        self._thread.start()
        logger.info("[BG_WORKER] バックグラウンドワーカー起動")

    def _run_loop_safe(self) -> None:
        """クラッシュ時に自動再起動するラッパー。"""
        while not self._stop_event.is_set():
            try:
                self._run_loop()
            except Exception as e:
                logger.error("[BG_WORKER] ワーカースレッドクラッシュ: %s — 5秒後に再起動", e)
                self._stop_event.wait(timeout=5.0)

    def wait_until_idle(self, timeout: float = 600.0) -> None:
        """未処理がなくなるまで待機 (最大 timeout 秒)。"""
        _start = time.time()
        while time.time() - _start < timeout:
            conn = self._get_conn()
            try:
                pending_ocr = conn.execute(
                    "SELECT COUNT(*) FROM lc_screens"
                    " WHERE ocr_text_hq IS NULL"
                    " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                ).fetchone()[0]
                pending_cluster = conn.execute(
                    "SELECT COUNT(*) FROM lc_screens"
                    " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
                    " AND ocr_text_hq IS NOT NULL"
                ).fetchone()[0]
            finally:
                conn.close()
            if pending_ocr == 0 and pending_cluster == 0:
                logger.info("[BG_WORKER] 全処理完了 (%.0f秒待機)", time.time() - _start)
                return
            logger.info("[BG_WORKER] 待機中... OCR残=%d, cluster残=%d", pending_ocr, pending_cluster)
            time.sleep(5.0)
        logger.warning("[BG_WORKER] タイムアウト (%.0f秒)", timeout)

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
        last_gemini = 0.0
        last_merge = 0.0

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

            # HQ OCR (PaddleOCR フル解像度): GEMINI_API_KEY 未設定時のみ実行
            # Gemini が画像から直接 OCR するため、API キーがあれば不要
            if not os.environ.get("GEMINI_API_KEY"):
                if now - last_ocr >= self._interval_ocr:
                    try:
                        self._run_incremental_ocr()
                    except Exception as e:
                        logger.warning("[BG_WORKER] ocr 例外: %s", e)
                    last_ocr = time.time()

            # 間引き処理 (15秒間隔) — OCR 完了後に実行
            if now - last_dedup >= self._interval_dedup:
                try:
                    self._run_incremental_dedup()
                except Exception as e:
                    logger.warning("[BG_WORKER] dedup 例外: %s", e)
                last_dedup = time.time()

            # Gemini バッチ修正 (60秒間隔、OCR+間引き完了後)
            if now - last_gemini >= 30.0:
                try:
                    self._run_gemini_batch_correction()
                except Exception as e:
                    logger.warning("[BG_WORKER] gemini 例外: %s", e)
                last_gemini = time.time()

            # 遷移グラフ構築 (120秒間隔)
            if now - last_graph >= self._interval_graph:
                try:
                    self._run_graph_build()
                except Exception as e:
                    logger.warning("[BG_WORKER] graph 例外: %s", e)
                last_graph = time.time()

            # クロスセッションマージ (300秒間隔)
            if now - last_merge >= 300.0:
                try:
                    self._run_cross_session_merge()
                except Exception as e:
                    logger.warning("[BG_WORKER] merge 例外: %s", e)
                last_merge = time.time()

            self._stop_event.wait(timeout=0.5)

    # ─── グルーピング ─────────────────────────────────────

    def _run_incremental_group(self) -> None:
        """group_id が NULL の未グループ化スクリーンをグルーピング。"""
        conn = self._get_conn()
        try:
            _grp_where = " WHERE group_id IS NULL"
            _grp_params = ()
            if self._session_id:
                _grp_where += " AND session_id = ?"
                _grp_params = (self._session_id,)
            rows = conn.execute(
                "SELECT id, session_id, scene, discovered_at FROM lc_screens"
                + _grp_where + " ORDER BY session_id, discovered_at",
                _grp_params,
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

    # ─── 顔面積ヘルパー ────────────────────────────────────

    @staticmethod
    def _max_face_area(conn: sqlite3.Connection, screen_id: int) -> int:
        """スクリーンの screenshot_path から最大顔面積を返す。検出なし=0。"""
        row = conn.execute(
            "SELECT screenshot_path FROM lc_screens WHERE id = ?", (screen_id,)
        ).fetchone()
        if not row or not row["screenshot_path"]:
            return 0
        path = row["screenshot_path"]
        if not Path(path).exists():
            return 0
        try:
            import cv2
            _cascade_path = Path(__file__).parent.parent.parent / "assets" / "lbpcascade_animeface.xml"
            if not _cascade_path.exists():
                return 0
            cascade = cv2.CascadeClassifier(str(_cascade_path))
            img = cv2.imread(path)
            if img is None:
                return 0
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30))
            if len(faces) == 0:
                return 0
            return max(w * h for (_, _, w, h) in faces)
        except Exception:
            return 0

    @staticmethod
    def _get_rep_id(conn: sqlite3.Connection, cluster_id: int) -> Optional[int]:
        """クラスタの代表画像 ID を返す。"""
        row = conn.execute(
            "SELECT id FROM lc_screens WHERE cluster_id = ? AND is_representative = 1 LIMIT 1",
            (cluster_id,),
        ).fetchone()
        return row["id"] if row else None

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
            # HQ OCR 未完了の画像はスキップ（OCR 完了後に間引く）
            # セッション分離: 自セッション内のみクラスタリング (誤マージ防止)
            _sid_filter = ""
            _sid_params: tuple = ()
            if self._session_id:
                _sid_filter = " AND session_id = ?"
                _sid_params = (self._session_id,)

            # GEMINI_API_KEY 設定時: HQ OCR を待たず初期 OCR で間引き
            # 未設定時: HQ OCR 完了済みのみ間引き対象
            _hq_filter = "" if os.environ.get("GEMINI_API_KEY") else " AND ocr_text_hq IS NOT NULL"
            rows = conn.execute(
                "SELECT id, phash, title, COALESCE(ocr_text_hq, ocr_text) AS ocr FROM lc_screens"
                " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
                + _hq_filter
                + _sid_filter +
                " ORDER BY discovered_at",
                _sid_params,
            ).fetchall()

            if not rows:
                return

            existing_reps = conn.execute(
                "SELECT cluster_id, phash, title, COALESCE(ocr_text_hq, ocr_text) AS ocr FROM lc_screens"
                " WHERE is_representative = 1 AND phash IS NOT NULL"
                + _sid_filter +
                " ORDER BY cluster_id",
                _sid_params,
            ).fetchall()
            # rep_map: cluster_id → (phash, title, normalized_ocr_text)
            rep_map: dict[int, tuple[str, str, str]] = {
                r["cluster_id"]: (r["phash"], r["title"] or "", _normalize_text(r["ocr"] or ""))
                for r in existing_reps
            }

            # cluster_id はグローバルに一意 (セッション横断で重複しない)
            max_cid = conn.execute(
                "SELECT COALESCE(MAX(cluster_id), -1) FROM lc_screens"
            ).fetchone()[0]
            next_cid = max_cid + 1

            # 直前クラスタ ID のみ追跡 (phash/text は常に rep_map から取得 → ドリフト防止)
            _prev_cid: Optional[int] = None
            last_screen = conn.execute(
                "SELECT cluster_id FROM lc_screens WHERE cluster_id IS NOT NULL"
                + _sid_filter +
                " ORDER BY discovered_at DESC LIMIT 1",
                _sid_params,
            ).fetchone()
            if last_screen:
                _prev_cid = last_screen["cluster_id"]

            processed = 0
            for row in rows:
                sid = row["id"]
                ph = row["phash"]
                title = row["title"] or ""
                ocr_text = row["ocr"] or ""
                norm_text = _normalize_text(ocr_text)

                _is_meaningful = len(norm_text) > 0

                # 1) テキスト一致チェック (グローバル): 同じテキスト or 前方一致なら同一画面
                #    セリフ途中（文字送り中）のスクショは前方一致で同クラスタに統合
                text_match_cid = None
                if _is_meaningful:
                    for cid, (rep_ph, rep_title, rep_norm) in rep_map.items():
                        if not rep_norm:
                            continue
                        if rep_norm == norm_text:
                            text_match_cid = cid
                            break
                        # 前方一致: 短い方が長い方の先頭と一致 (最低5文字)
                        shorter, longer = (norm_text, rep_norm) if len(norm_text) <= len(rep_norm) else (rep_norm, norm_text)
                        if len(shorter) >= 5 and longer.startswith(shorter):
                            text_match_cid = cid
                            break

                if text_match_cid is not None:
                    # テキスト一致: OCR テキストが長い方を代表に採用
                    old_rep_id = self._get_rep_id(conn, text_match_cid)
                    _old_text_len = 0
                    if old_rep_id:
                        _old_row = conn.execute(
                            "SELECT LENGTH(COALESCE(ocr_text_hq, ocr_text, '')) AS tlen FROM lc_screens WHERE id = ?",
                            (old_rep_id,),
                        ).fetchone()
                        _old_text_len = _old_row["tlen"] if _old_row else 0
                    _new_text_len = len(ocr_text)
                    if _new_text_len > _old_text_len and old_rep_id:
                        # 新しい方がテキスト長い → 代表交代
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (text_match_cid, sid),
                        )
                        conn.execute(
                            "UPDATE lc_screens SET is_representative = 0 WHERE id = ?",
                            (old_rep_id,),
                        )
                        rep_map[text_match_cid] = (ph, title, norm_text)
                    else:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                            (text_match_cid, sid),
                        )
                    _prev_cid = text_match_cid
                elif _is_meaningful:
                    # 2a) 直前テキスト空 + phash 近い → 統合 (テキストあり側が代表に)
                    #     例: 暗転→セリフ表示。統合後 rep にテキストが入るため次の画面で
                    #     再度 2a に入ることはなく、連鎖マージは発生しない。
                    # 2b) 直前テキストあり + phash 近い + テキスト類似 → OCR 揺れ
                    _merge_to_prev = False
                    if _prev_cid is not None and _prev_cid in rep_map:
                        _rep_ph, _rep_title, _rep_norm = rep_map[_prev_cid]
                        d = phash_distance(_rep_ph, ph) if _rep_ph else 999
                        _has_face = self._max_face_area(conn, sid) > 0
                        _ph_lim = 5 if _has_face else 20
                        if not _rep_norm and d < _ph_lim:
                            # 直前テキスト空 + phash 近い → 統合 (テキストあり側が代表)
                            _merge_to_prev = True
                            old_rep_id = self._get_rep_id(conn, _prev_cid)
                            conn.execute(
                                "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                (_prev_cid, sid),
                            )
                            if old_rep_id:
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0 WHERE id = ?",
                                    (old_rep_id,),
                                )
                            rep_map[_prev_cid] = (ph, title, norm_text)
                        elif _rep_norm and d < 5 and _text_similarity(norm_text, _rep_norm) >= 0.5:
                            # テキスト類似 + phash 近い → OCR 揺れ (テキスト長い方を代表に)
                            # phash が非常に近い (< 10) 場合はテキスト類似度を緩和 (OCR 誤読救済)
                            _merge_to_prev = True
                            old_rep_id = self._get_rep_id(conn, _prev_cid)
                            _old_tlen = 0
                            if old_rep_id:
                                _old_row = conn.execute(
                                    "SELECT LENGTH(COALESCE(ocr_text_hq, ocr_text, '')) AS tlen FROM lc_screens WHERE id = ?",
                                    (old_rep_id,),
                                ).fetchone()
                                _old_tlen = _old_row["tlen"] if _old_row else 0
                            if len(ocr_text) > _old_tlen and old_rep_id:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0 WHERE id = ?",
                                    (old_rep_id,),
                                )
                                rep_map[_prev_cid] = (ph, title, norm_text)
                            else:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                    if not _merge_to_prev:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (next_cid, sid),
                        )
                        rep_map[next_cid] = (ph, title, norm_text)
                        _prev_cid = next_cid
                        next_cid += 1
                else:
                    # 3) テキスト空 → 直前クラスタ代表の phash と比較 (ドリフト防止)
                    _matched = False
                    if _prev_cid is not None and _prev_cid in rep_map:
                        _rep_ph, _rep_title, _rep_norm = rep_map[_prev_cid]
                        d = phash_distance(_rep_ph, ph) if _rep_ph else 999
                        if d < 20:
                            # 代表交代判定: テキストあり > テキスト空 > 顔面積
                            old_rep_id = self._get_rep_id(conn, _prev_cid)
                            _should_promote = False
                            if _is_meaningful and not _rep_norm:
                                _should_promote = True
                            elif not _is_meaningful and not _rep_norm:
                                # 暗い画像群: brightness が高い方を代表に (ロゴのフェードイン/アウト対策)
                                new_br = self._get_brightness(conn, sid)
                                old_br = self._get_brightness(conn, old_rep_id) if old_rep_id else 0
                                if new_br < 80 and old_br < 80:
                                    # 両方暗い → brightness が高い方を採用
                                    if new_br > old_br:
                                        _should_promote = True
                                else:
                                    # 明るい画像群 → 顔面積で判定
                                    new_face = self._max_face_area(conn, sid)
                                    old_face = self._max_face_area(conn, old_rep_id) if old_rep_id else 0
                                    if new_face > old_face:
                                        _should_promote = True
                            if _should_promote and old_rep_id:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0 WHERE id = ?",
                                    (old_rep_id,),
                                )
                                rep_map[_prev_cid] = (ph, title, norm_text)
                            else:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                            _matched = True
                    if not _matched:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (next_cid, sid),
                        )
                        rep_map[next_cid] = (ph, title, norm_text)
                        _prev_cid = next_cid
                        next_cid += 1

                processed += 1

            if processed > 0:
                conn.commit()
                self.dedup_count += processed
                logger.info("[BG_WORKER] dedup: %d 枚処理 (合計 %d)", processed, self.dedup_count)

        finally:
            conn.close()

    @staticmethod
    def _get_brightness(conn: sqlite3.Connection, screen_id: int) -> float:
        """スクリーンショットの平均輝度を返す。"""
        try:
            row = conn.execute(
                "SELECT screenshot_path FROM lc_screens WHERE id = ?", (screen_id,)
            ).fetchone()
            if row and row["screenshot_path"]:
                import cv2
                import numpy as np
                img = cv2.imread(row["screenshot_path"])
                if img is not None:
                    return float(np.mean(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)))
        except Exception:
            pass
        return 0.0

    @staticmethod
    def _is_dark_phash(phash: str) -> bool:
        """暗転フレーム判定: phash の立ちビット数が少ない (≤8)。"""
        try:
            return bin(int(phash, 16)).count('1') <= 8
        except (ValueError, TypeError):
            return False

    # ─── PaddleOCR 再処理 ─────────────────────────────────

    def _run_incremental_ocr(self) -> None:
        """ocr_text_hq IS NULL のスクリーンを 1 枚処理（間引き前なので全画像対象）。"""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, screenshot_path FROM lc_screens"
                " WHERE ocr_text_hq IS NULL"
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
                # HQ OCR: 2x拡大 + PNG変換で精度向上 (バッチ処理なので負荷許容)
                _img = cv2.imread(path)
                if _img is None:
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_hq = '' WHERE id = ?", (sid,)
                    )
                    conn.commit()
                    return
                _img_2x = cv2.resize(_img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                _tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                cv2.imwrite(_tmp.name, _img_2x)
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
                # 段階1+2: OCR テキスト修正 (正規表現 + 辞書マッチ)
                from tools.ap.ocr_correction import correct_ocr_text
                hq_text = correct_ocr_text(hq_text)

                # タイトルも HQ OCR 結果で更新 (信頼度上位3つ、修正済み)
                import re
                _HAS_TEXT = re.compile(r'[\u3040-\u9fff\u30a0-\u30ffA-Za-z]')
                _PURE_NUM = re.compile(r'^[\d\s.:/%×+\-~]+$')
                _candidates = sorted(
                    [(it.get("confidence", 0), correct_ocr_text(it.get("text", "").strip()))
                     for it in ocr_results
                     if it.get("confidence", 0) >= 0.3
                     and it.get("text", "").strip()
                     and _HAS_TEXT.search(it.get("text", ""))
                     and not _PURE_NUM.match(it.get("text", "").strip())],
                    key=lambda x: -x[0],
                )
                hq_title = " / ".join(t for _, t in _candidates[:3]) if _candidates else None
                if hq_title:
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_hq = ?, title = ? WHERE id = ?",
                        (hq_text, hq_title, sid),
                    )
                else:
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
                        " WHERE ocr_text_hq IS NULL"
                        " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                    ).fetchone()[0]
                    logger.info("[BG_WORKER] reocr: %d 枚完了 (残り %d)",
                                self.ocr_count, remaining)

            finally:
                os.environ.pop("OCR_ENGINE", None)

        finally:
            conn.close()

    # ─── Gemini バッチ修正 ──────────────────────────────────

    def _run_gemini_batch_correction(self) -> None:
        """Gemini Flash で代表画像の OCR を画像付きで補正 (API キー未設定ならスキップ)。

        ocr_text_gemini カラムに保存。ocr_text_hq は PaddleOCR 結果として保持。
        """
        import os
        import time as _time
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return

        conn = self._get_conn()
        try:
            # ocr_text_gemini が NULL の代表画像のみ対象
            # Gemini が画像から直接 OCR するため HQ OCR 不要
            from tools.ap.ocr_correction import (
                _init_gemini_client, _GEMINI_BATCH_SIZE, _GEMINI_RATE_LIMIT,
                gemini_correct_multi,
            )
            # 1バッチ = _GEMINI_BATCH_SIZE 枚, 1回の起動で 6 バッチまで処理
            # 自動処理は self._session_id があればそのセッション、なければ status='running' のセッション
            # を優先 (古い完了済みセッションは「バックグラウンド未完了」セクションで手動処理)
            fetch_limit = _GEMINI_BATCH_SIZE * 6
            target_sid = self._session_id
            if not target_sid:
                row = conn.execute(
                    "SELECT session_id FROM lc_sessions WHERE status = 'running'"
                    " ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    target_sid = row["session_id"]
            if not target_sid:
                return  # アクティブセッションがない → 自動処理しない
            rows = conn.execute(
                "SELECT id, screenshot_path,"
                " COALESCE(ocr_text_hq, ocr_text, '') AS ocr"
                " FROM lc_screens"
                " WHERE is_representative = 1"
                " AND ocr_text_gemini IS NULL"
                " AND screenshot_path != ''"
                " AND session_id = ?"
                " ORDER BY discovered_at"
                " LIMIT ?",
                (target_sid, fetch_limit),
            ).fetchall()

            if not rows:
                return

            client = _init_gemini_client()
            if client is None:
                return

            updated = 0
            total = 0
            # _GEMINI_BATCH_SIZE 枚ごとに分割してバッチ送信
            for batch_start in range(0, len(rows), _GEMINI_BATCH_SIZE):
                batch = rows[batch_start:batch_start + _GEMINI_BATCH_SIZE]
                items = []
                for row in batch:
                    sid = row["id"]
                    path = row["screenshot_path"]
                    if not path or not Path(path).exists():
                        # 画像なし → 空文字でスキップマーク
                        conn.execute(
                            "UPDATE lc_screens SET ocr_text_gemini = '' WHERE id = ?",
                            (sid,),
                        )
                        continue
                    items.append({
                        "id": sid,
                        "screenshot_path": path,
                        "ocr_text": row["ocr"],
                    })

                if not items:
                    continue

                results = gemini_correct_multi(items, client=client)
                if results is None:
                    # API エラー → 次回再試行のため更新しない
                    break

                # 結果を id でマップ
                result_map = {r["id"]: r for r in results}
                import re as _re
                from tools.ap.ocr_correction import _clean_gemini_output
                _has_text = _re.compile(r'[\u3040-\u9fff\u30a0-\u30ffA-Za-z]')
                _pure_num = _re.compile(r'^[\d\s.:/%×+\-~]+$')
                artifact_count = 0
                for item in items:
                    sid = item["id"]
                    r = result_map.get(sid)
                    raw_corrected = (r.get("corrected_text", "") if r else "").strip()
                    corrected = _clean_gemini_output(raw_corrected)
                    # is_artifact 検知 → 対応するマスターノードを user_excluded=1 に
                    is_artifact = bool(r.get("is_artifact", False)) if r else False
                    screen_type = (r.get("screen_type", "") if r else "")
                    if is_artifact:
                        logger.info("[BG_WORKER] artifact 検出: id=%d type=%s text=%s",
                                    sid, screen_type, corrected[:30] if corrected else "(empty)")
                        # screen の fingerprint からマスターノードを特定
                        screen_fp = conn.execute(
                            "SELECT fingerprint FROM lc_screens WHERE id = ?",
                            (sid,),
                        ).fetchone()
                        if screen_fp:
                            updated_nodes = conn.execute(
                                "UPDATE lc_master_nodes SET user_excluded = 1"
                                " WHERE master_fp = ?",
                                (screen_fp["fingerprint"],),
                            ).rowcount
                            if updated_nodes > 0:
                                artifact_count += 1
                            # マスター未登録の場合は screen の is_representative を 0 にして
                            # 今後マージ対象外に (元の代表は別の画面が再選出される)
                            else:
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0"
                                    " WHERE id = ?", (sid,),
                                )
                    # Gemini がテキストなしと判断 → タイトルもクリア
                    if not corrected and item["ocr_text"]:
                        conn.execute(
                            "UPDATE lc_screens SET ocr_text_gemini = '', title = '(UNKNOWN)'"
                            " WHERE id = ?",
                            (sid,),
                        )
                    elif corrected:
                        # プレースホルダ title (STARTUP, UNKNOWN等) なら、補正テキストから生成
                        words = [w.strip() for w in corrected.split()
                                 if w.strip() and _has_text.search(w)
                                 and not _pure_num.match(w.strip())]
                        new_title = " / ".join(words[:3]) if words else None
                        if new_title:
                            conn.execute(
                                "UPDATE lc_screens SET ocr_text_gemini = ?, title = ?"
                                " WHERE id = ? AND (title LIKE '(%' OR title IS NULL)",
                                (corrected, new_title, sid),
                            )
                            # プレースホルダでない場合は ocr_text_gemini のみ更新
                            conn.execute(
                                "UPDATE lc_screens SET ocr_text_gemini = ?"
                                " WHERE id = ? AND title NOT LIKE '(%' AND title IS NOT NULL",
                                (corrected, sid),
                            )
                        else:
                            conn.execute(
                                "UPDATE lc_screens SET ocr_text_gemini = ? WHERE id = ?",
                                (corrected, sid),
                            )
                    else:
                        conn.execute(
                            "UPDATE lc_screens SET ocr_text_gemini = '' WHERE id = ?",
                            (sid,),
                        )
                    if corrected and corrected != item["ocr_text"]:
                        updated += 1
                    total += 1

                conn.commit()
                if artifact_count > 0:
                    logger.info("[BG_WORKER] gemini batch: %d 枚処理 (バッチサイズ=%d, アーティファクト除外=%d)",
                                len(items), _GEMINI_BATCH_SIZE, artifact_count)
                else:
                    logger.info("[BG_WORKER] gemini batch: %d 枚処理 (バッチサイズ=%d)",
                                len(items), _GEMINI_BATCH_SIZE)
                # レート制限対策
                _time.sleep(_GEMINI_RATE_LIMIT)

            if total > 0:
                logger.info("[BG_WORKER] gemini: %d/%d 件修正 (合計)", updated, total)
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
                " WHERE ocr_text_hq IS NULL"
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
                sccs = bp.build_graph(session_id=self._session_id)
                if sccs > 0:
                    self.graph_sccs = sccs
                    logger.info("[BG_WORKER] graph: %d SCC 構築完了", sccs)
            finally:
                bp.close()
        except Exception as e:
            logger.warning("[BG_WORKER] graph 例外: %s", e)

    # ─── クロスセッションマージ ───────────────────────────

    def _run_cross_session_merge(self) -> None:
        """セッショングラフ構築完了後にマスターグラフにマージ。"""
        conn = self._get_conn()
        try:
            # session_graphs に存在 + セッション完了済み + 未マージ
            pending = conn.execute(
                "SELECT sg.session_id FROM lc_session_graphs sg"
                " JOIN lc_sessions s ON s.session_id = sg.session_id"
                " WHERE s.status = 'completed'"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM lc_node_mappings nm"
                "   WHERE nm.session_id = sg.session_id"
                " )"
            ).fetchall()
            if not pending:
                return
        finally:
            conn.close()

        from tools.cross_session_merger import CrossSessionMerger
        merger = CrossSessionMerger(db_path=self._db_path)
        try:
            for row in pending:
                sid = row["session_id"]
                # プレビューでマッチ内訳をログ出力
                preview = merger.preview_merge(sid)
                sm = preview["summary"]
                logger.info(
                    "[BG_WORKER] merge preview: session=%s, screens=%d, "
                    "anchor=%d, k_hop=%d, transition=%d, new=%d",
                    sid, preview["session_screens"],
                    sm["anchor"], sm["k_hop"], sm["transition"], sm["new"],
                )
                # マージ実行
                new_nodes = merger.merge_to_master(sid)
                logger.info("[BG_WORKER] merge done: session=%s, +%d new nodes", sid, new_nodes)
        finally:
            merger.close()
