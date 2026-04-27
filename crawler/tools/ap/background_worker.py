"""
バックグラウンドワーカー — auto_pilot と並行して全バッチ処理をリアルタイム実行。

デーモンスレッドで動作し、auto_pilot のメインループに影響を与えない。
SQLite WAL モードで並行アクセスする。

処理内容:
  1. グルーピング (scene + 時間ギャップでグループ化)
  2. phash クラスタリング (代表選出)
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


def _is_degenerate_phash(ph: Optional[str]) -> bool:
    """縮退 phash 判定: set bit が極端に少ない/多い (ほぼ単色画像)。

    Hamming 距離は「異なるビット数」なので、bit がほぼ全 0 または全 1 の phash は
    距離計算で見かけ上「他と近い」と誤判定されやすい。アンカーとして信頼できない。
    """
    if not ph:
        return True
    try:
        n = bin(int(ph, 16)).count('1')
    except (ValueError, TypeError):
        return True
    return n < 8 or n > 56  # 64 bit 中、8 未満 or 56 超 → 縮退


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
        interval_clustering: float = 15.0,
        interval_ocr: float = 0.5,
        interval_group: float = 30.0,
        interval_graph: float = 120.0,
    ):
        self._db_path = db_path
        self._session_id = session_id
        self._interval_clustering = interval_clustering
        self._interval_ocr = interval_ocr
        self._interval_group = interval_group
        self._interval_graph = interval_graph
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 統計
        self.clustering_count = 0
        self.ocr_count = 0
        self.group_count = 0
        self.graph_sccs = 0

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        logger.info("[BG_WORKER] セッション切替: %s → %s", self._session_id, value)
        self._session_id = value

    def start(self) -> None:
        """デーモンスレッドを起動。"""
        self._thread = threading.Thread(target=self._run_loop_safe, daemon=True, name="bg_worker")
        self._thread.start()
        text_sep = os.environ.get("LC_TEXT_SEPARATION", "on").lower() != "off"
        logger.info(
            "[BG_WORKER] バックグラウンドワーカー起動 (テキスト分離: %s)",
            "ON" if text_sep else "OFF (dHash/ヒストのみで判定)",
        )

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
        import os as _os
        _has_gemini = bool(_os.environ.get("GEMINI_API_KEY"))
        _start = time.time()
        while time.time() - _start < timeout:
            conn = self._get_conn()
            try:
                if _has_gemini:
                    # Gemini 有効時: 代表画像の Gemini OCR 完了を待つ
                    pending_ocr = conn.execute(
                        "SELECT COUNT(*) FROM lc_screens"
                        " WHERE is_representative = 1"
                        " AND ocr_text_gemini IS NULL"
                        " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                    ).fetchone()[0]
                else:
                    # PaddleOCR のみ: HQ OCR 完了を待つ
                    pending_ocr = conn.execute(
                        "SELECT COUNT(*) FROM lc_screens"
                        " WHERE ocr_text_hq IS NULL"
                        " AND screenshot_path IS NOT NULL AND screenshot_path != ''"
                    ).fetchone()[0]
                pending_cluster = conn.execute(
                    "SELECT COUNT(*) FROM lc_screens"
                    " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
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
            "[BG_WORKER] 停止 (group=%d, clustering=%d, ocr=%d, scc=%d)",
            self.group_count, self.clustering_count, self.ocr_count, self.graph_sccs,
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

        last_clustering = 0.0
        last_ocr = 0.0
        last_group = 0.0
        last_graph = 0.0
        last_gemini = 0.0
        # last_merge 削除: マージは手動実行に変更

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

            # クラスタリング処理 (15秒間隔) — OCR 完了後に実行
            if now - last_clustering >= self._interval_clustering:
                try:
                    self._run_incremental_clustering()
                except Exception as e:
                    logger.warning("[BG_WORKER] clustering 例外: %s", e)
                last_clustering = time.time()

            # Gemini バッチ修正 (60秒間隔、OCR+クラスタリング完了後)
            if now - last_gemini >= 30.0:
                try:
                    self._run_gemini_batch_correction()
                except Exception as e:
                    logger.warning("[BG_WORKER] gemini 例外: %s", e)
                last_gemini = time.time()

            # 合成エッジ + 遷移グラフ構築 (120秒間隔)
            if now - last_graph >= self._interval_graph:
                try:
                    self._synthesize_auto_edges()
                    self._run_graph_build()
                except Exception as e:
                    logger.warning("[BG_WORKER] graph 例外: %s", e)
                last_graph = time.time()

            # クロスセッションマージは手動実行 (ダッシュボードから)
            # auto_pilot ではマージ待ち状態まで進める

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

    @staticmethod
    def _get_rep_screenshot_path(
        conn: sqlite3.Connection, cluster_id: int
    ) -> Optional[str]:
        """クラスタ代表の screenshot_path を返す (NULL/空なら None)。"""
        row = conn.execute(
            "SELECT screenshot_path FROM lc_screens"
            " WHERE cluster_id = ? AND is_representative = 1 LIMIT 1",
            (cluster_id,),
        ).fetchone()
        if not row:
            return None
        path = row["screenshot_path"]
        return path if path else None

    @staticmethod
    def _set_decision(
        conn: sqlite3.Connection,
        screen_id: int,
        cluster_id: int,
        method: str,
        *,
        phash_dist: Optional[int] = None,
        dhash_dist: Optional[int] = None,
        avg_brightness: Optional[float] = None,
    ) -> None:
        """採用版クラスタID と判定理由 + 計算済み数値を保存する。

        phash_dist / dhash_dist は直前クラスタ代表との比較値 (閾値調整 UI 用)。
        avg_brightness は現スクリーンの平均輝度 (UI 表示用)。
        """
        cols = ["cluster_id_hybrid = ?", "cluster_decision_method = ?"]
        params: list = [cluster_id, method]
        if phash_dist is not None:
            cols.append("phash_dist_to_prev_rep = ?")
            params.append(phash_dist)
        if dhash_dist is not None:
            cols.append("dhash_dist_to_prev_rep = ?")
            params.append(dhash_dist)
        if avg_brightness is not None:
            cols.append("avg_brightness = ?")
            params.append(avg_brightness)
        params.append(screen_id)
        conn.execute(
            f"UPDATE lc_screens SET {', '.join(cols)} WHERE id = ?",
            params,
        )

    # ─── phash クラスタリング ──────────────────────────────

    @staticmethod
    def _phash_distance(h1: str, h2: str) -> int:
        """phash 距離 (Hamming)。"""
        from lc.image_comparator import phash_distance
        return phash_distance(h1, h2)

    @staticmethod
    def _dhash_distance(h1: str, h2: str) -> int:
        """dHash 距離 (Hamming)。"""
        from lc.image_comparator import dhash_distance
        return dhash_distance(h1, h2)

    def _run_incremental_clustering(self) -> None:
        """未処理スクリーンに対してテキスト優先 + ハッシュ距離フォールバックでクラスタリング。

        1. OCR テキスト (title) が既存代表と一致 → 同一クラスタ（不採用）
        2. テキストが異なる or 空 → 2段階ハッシュ判定 (phash 即決 → 中間域は dHash)
        3. どちらも一致しない → 新規クラスタ（採用）

        環境変数 LC_TEXT_SEPARATION=off でテキスト判定を完全スキップし
        全 screen をハッシュ判定で分類する (デバッグ/視認用)。
        """
        _phash_distance = self._phash_distance
        _dhash_distance = self._dhash_distance
        _text_sep_enabled = os.environ.get("LC_TEXT_SEPARATION", "on").lower() != "off"

        conn = self._get_conn()
        try:
            # HQ OCR 未完了の画像はスキップ（OCR 完了後に間引く）
            # セッション分離: 自セッション内のみクラスタリング (誤マージ防止)
            _sid_filter = ""
            _sid_params: tuple = ()
            if self._session_id:
                _sid_filter = " AND session_id = ?"
                _sid_params = (self._session_id,)

            # クラスタリングは常に初期 OCR (Vision) で判定し、HQ OCR を待たない。
            # HQ OCR (PaddleOCR/Gemini) はクラスタリング後の代表のみで実行され、表示用テキストの精度向上が目的。
            _hq_filter = ""
            rows = conn.execute(
                "SELECT id, phash, dhash, title, screenshot_path,"
                " COALESCE(ocr_text_hq, ocr_text) AS ocr FROM lc_screens"
                " WHERE cluster_id IS NULL AND phash IS NOT NULL AND phash != ''"
                + _hq_filter
                + _sid_filter +
                " ORDER BY discovered_at",
                _sid_params,
            ).fetchall()

            if not rows:
                return

            existing_reps = conn.execute(
                "SELECT cluster_id, phash, dhash, title, COALESCE(ocr_text_hq, ocr_text) AS ocr FROM lc_screens"
                " WHERE is_representative = 1 AND phash IS NOT NULL"
                + _sid_filter +
                " ORDER BY cluster_id",
                _sid_params,
            ).fetchall()
            # rep_map: cluster_id → (phash, dhash, title, normalized_ocr_text)
            rep_map: dict[int, tuple[str, Optional[str], str, str]] = {
                r["cluster_id"]: (r["phash"], r["dhash"], r["title"] or "", _normalize_text(r["ocr"] or ""))
                for r in existing_reps
            }

            # cluster_phashes: cluster_id → 全メンバーの phash リスト (Step A 直径制限用)
            # 代表交代によるドリフトを防ぐため、新 screen 追加時に全メンバーとの最大距離をチェックする
            cluster_phashes: dict[int, list[str]] = {}
            for r in conn.execute(
                "SELECT cluster_id, phash FROM lc_screens"
                " WHERE cluster_id IS NOT NULL AND phash IS NOT NULL AND phash != ''"
                + _sid_filter,
                _sid_params,
            ).fetchall():
                cluster_phashes.setdefault(r["cluster_id"], []).append(r["phash"])

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
                dh = row["dhash"]
                title = row["title"] or ""
                ocr_text = row["ocr"] or ""
                norm_text = _normalize_text(ocr_text)

                # LC_TEXT_SEPARATION=off の場合、テキスト判定を完全スキップ
                # ケース3 (2段階判定: phash → dHash) に直行させる
                _is_meaningful = (len(norm_text) > 0) and _text_sep_enabled

                # 1) テキスト一致チェック (直前クラスタのみ): 同じテキスト or 前方一致なら同一画面
                #    セリフ途中（文字送り中）のスクショは前方一致で同クラスタに統合
                #    §16: クラスタリングは直前クラスタとのみ比較（厳格）
                text_match_cid = None
                _text_match_method = ""
                if _is_meaningful and _prev_cid is not None and _prev_cid in rep_map:
                    rep_ph, rep_dh, rep_title, rep_norm = rep_map[_prev_cid]
                    if rep_norm:
                        if rep_norm == norm_text:
                            text_match_cid = _prev_cid
                            _text_match_method = "text_match"
                        else:
                            # 前方一致: 短い方が長い方の先頭と一致 (最低5文字)
                            shorter, longer = (norm_text, rep_norm) if len(norm_text) <= len(rep_norm) else (rep_norm, norm_text)
                            if len(shorter) >= 5 and longer.startswith(shorter):
                                text_match_cid = _prev_cid
                                _text_match_method = "text_prefix"

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
                        # クラスタ内の全代表をリセットしてから新代表を設定
                        conn.execute(
                            "UPDATE lc_screens SET is_representative = 0 WHERE cluster_id = ? AND is_representative = 1",
                            (text_match_cid,),
                        )
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (text_match_cid, sid),
                        )
                        logger.debug("[REP_TRACE] id=%d rep=0 (text_match代表交代, 新代表=%d, cid=%d)", old_rep_id, sid, text_match_cid)
                        rep_map[text_match_cid] = (ph, dh, title, norm_text)
                    else:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                            (text_match_cid, sid),
                        )
                        logger.debug("[REP_TRACE] id=%d rep=0 (text_match非代表統合, cid=%d)", sid, text_match_cid)
                    self._set_decision(conn, sid, text_match_cid, _text_match_method)
                    cluster_phashes.setdefault(text_match_cid, []).append(ph)
                    _prev_cid = text_match_cid
                elif _is_meaningful:
                    # 2a) 直前テキスト空 + phash 近い → 統合 (テキストあり側が代表に)
                    #     例: 暗転→セリフ表示。統合後 rep にテキストが入るため次の画面で
                    #     再度 2a に入ることはなく、連鎖マージは発生しない。
                    # 2b) 直前テキストあり + phash 近い + テキスト類似 → OCR 揺れ
                    _merge_to_prev = False
                    _merge_method = ""
                    if _prev_cid is not None and _prev_cid in rep_map:
                        _rep_ph, _rep_dh, _rep_title, _rep_norm = rep_map[_prev_cid]
                        d = _phash_distance(_rep_ph, ph) if _rep_ph else 999
                        _has_face = self._max_face_area(conn, sid) > 0
                        _ph_lim = 5 if _has_face else 20
                        if not _rep_norm and d < _ph_lim:
                            # 直前テキスト空 + phash 近い → 統合 (テキストあり側が代表)
                            _merge_to_prev = True
                            _merge_method = "merge_to_prev_empty"
                            old_rep_id = self._get_rep_id(conn, _prev_cid)
                            # クラスタ内の全代表をリセットしてから新代表を設定
                            conn.execute(
                                "UPDATE lc_screens SET is_representative = 0 WHERE cluster_id = ? AND is_representative = 1",
                                (_prev_cid,),
                            )
                            conn.execute(
                                "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                (_prev_cid, sid),
                            )
                            if old_rep_id:
                                logger.debug("[REP_TRACE] id=%d rep=0 (prev_merge空テキスト代表交代, 新代表=%d, cid=%d)", old_rep_id, sid, _prev_cid)
                            rep_map[_prev_cid] = (ph, dh, title, norm_text)
                        elif _rep_norm and d < 5 and _text_similarity(norm_text, _rep_norm) >= 0.5:
                            # テキスト類似 + phash 近い → OCR 揺れ (テキスト長い方を代表に)
                            # phash が非常に近い (< 10) 場合はテキスト類似度を緩和 (OCR 誤読救済)
                            _merge_to_prev = True
                            _merge_method = "merge_to_prev_similar"
                            old_rep_id = self._get_rep_id(conn, _prev_cid)
                            _old_tlen = 0
                            if old_rep_id:
                                _old_row = conn.execute(
                                    "SELECT LENGTH(COALESCE(ocr_text_hq, ocr_text, '')) AS tlen FROM lc_screens WHERE id = ?",
                                    (old_rep_id,),
                                ).fetchone()
                                _old_tlen = _old_row["tlen"] if _old_row else 0
                            if len(ocr_text) > _old_tlen and old_rep_id:
                                # クラスタ内の全代表をリセットしてから新代表を設定
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0 WHERE cluster_id = ? AND is_representative = 1",
                                    (_prev_cid,),
                                )
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                logger.debug("[REP_TRACE] id=%d rep=0 (prev_mergeテキスト類似代表交代, 新代表=%d, cid=%d)", old_rep_id, sid, _prev_cid)
                                rep_map[_prev_cid] = (ph, dh, title, norm_text)
                            else:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                logger.debug("[REP_TRACE] id=%d rep=0 (prev_mergeテキスト類似非代表, cid=%d)", sid, _prev_cid)
                    if _merge_to_prev:
                        self._set_decision(conn, sid, _prev_cid, _merge_method)
                        cluster_phashes.setdefault(_prev_cid, []).append(ph)
                    else:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (next_cid, sid),
                        )
                        rep_map[next_cid] = (ph, dh, title, norm_text)
                        self._set_decision(conn, sid, next_cid, "new_cluster")
                        cluster_phashes[next_cid] = [ph] if ph else []
                        _prev_cid = next_cid
                        next_cid += 1
                else:
                    # 3) テキスト空 → 2段階判定 (phash 即決 + dHash 中間域判定)
                    from lc.cluster_decision import classify_empty_text_pair_with_metrics

                    _NEAR_TH = 8
                    _FAR_TH = 35
                    _FALLBACK_TH = 35
                    _matched = False
                    _decision_method = "new_cluster"
                    _metric_phash = None       # 直前代表との phash 距離
                    _metric_dhash = None       # 直前代表との dHash 距離
                    _metric_brightness = None  # 現スクリーン平均輝度

                    if _prev_cid is not None and _prev_cid in rep_map:
                        _rep_ph, _rep_dh, _rep_title, _rep_norm = rep_map[_prev_cid]
                        p_d = _phash_distance(_rep_ph, ph) if _rep_ph else 999
                        d_d = _dhash_distance(_rep_dh, dh) if _rep_dh and dh else None
                        prev_path = self._get_rep_screenshot_path(conn, _prev_cid)
                        curr_path = row["screenshot_path"]

                        _result = classify_empty_text_pair_with_metrics(
                            prev_path=prev_path,
                            curr_path=curr_path,
                            phash_distance=p_d,
                            dhash_distance=d_d,
                            near_threshold=_NEAR_TH,
                            far_threshold=_FAR_TH,
                            fallback_threshold=_FALLBACK_TH,
                        )
                        is_same = _result.is_same
                        _decision_method = _result.method
                        _metric_phash = _result.phash_distance
                        _metric_dhash = _result.dhash_distance
                        _metric_brightness = _result.curr_brightness

                        # Step A 直径制限: cluster 内全メンバーとの最大 phash 距離 >= 25 で別 cluster
                        # 代表交代によるドリフト防止 (代表に依存しない判定)
                        if is_same:
                            MAX_PHASH_DIAMETER = 25
                            members = cluster_phashes.get(_prev_cid, [])
                            if members and ph:
                                max_d = max(
                                    (self._phash_distance(m, ph) for m in members if m),
                                    default=0,
                                )
                                if max_d >= MAX_PHASH_DIAMETER:
                                    is_same = False
                                    _decision_method = "phash_diameter"

                        if is_same:
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
                                # クラスタ内の全代表をリセットしてから新代表を設定
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0 WHERE cluster_id = ? AND is_representative = 1",
                                    (_prev_cid,),
                                )
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                logger.debug("[REP_TRACE] id=%d rep=0 (空テキスト代表交代, 新代表=%d, cid=%d, method=%s)", old_rep_id, sid, _prev_cid, _decision_method)
                                rep_map[_prev_cid] = (ph, dh, title, norm_text)
                            else:
                                conn.execute(
                                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                                    (_prev_cid, sid),
                                )
                                logger.debug("[REP_TRACE] id=%d rep=0 (空テキスト非代表, cid=%d, method=%s)", sid, _prev_cid, _decision_method)
                            self._set_decision(
                                conn, sid, _prev_cid, _decision_method,
                                phash_dist=_metric_phash, dhash_dist=_metric_dhash,
                                avg_brightness=_metric_brightness,
                            )
                            cluster_phashes.setdefault(_prev_cid, []).append(ph)
                            _matched = True
                    if not _matched:
                        conn.execute(
                            "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                            (next_cid, sid),
                        )
                        rep_map[next_cid] = (ph, dh, title, norm_text)
                        # 新規クラスタの分離理由を保存 (例: "new:phash_far", "new:phash_diameter")
                        self._set_decision(
                            conn, sid, next_cid,
                            f"new:{_decision_method}" if _decision_method != "new_cluster" else "new_cluster",
                            phash_dist=_metric_phash, dhash_dist=_metric_dhash,
                            avg_brightness=_metric_brightness,
                        )
                        cluster_phashes[next_cid] = [ph] if ph else []
                        _prev_cid = next_cid
                        next_cid += 1

                processed += 1

            if processed > 0:
                conn.commit()
                self.clustering_count += processed
                logger.info("[BG_WORKER] clustering: %d 枚処理 (合計 %d)", processed, self.clustering_count)

            # Step A 直後の cluster_id を cluster_id_phash_only にスナップショット保存
            # (Step B 適用前の実態。比較ビュー左ペイン用)
            # 同時に「Step A で影響を受けた cluster_id」を集める (Step B の対象を限定)
            target_cluster_ids: set[int] = set()
            if processed > 0:
                # スナップショット直前 = cluster_id_phash_only IS NULL の screen が「新規処理」
                target_cluster_ids = {
                    r["cluster_id"] for r in conn.execute(
                        "SELECT DISTINCT cluster_id FROM lc_screens"
                        " WHERE cluster_id IS NOT NULL AND cluster_id_phash_only IS NULL"
                        + _sid_filter,
                        _sid_params,
                    ).fetchall()
                }
                conn.execute(
                    "UPDATE lc_screens SET cluster_id_phash_only = cluster_id"
                    " WHERE cluster_id IS NOT NULL AND cluster_id_phash_only IS NULL"
                    + _sid_filter,
                    _sid_params,
                )
                conn.commit()

            # クラスタ内バリデーション: Step B (dHash 分割) を新規 screen を含む cluster のみで実行
            # cluster_id を更新するが cluster_id_phash_only は変えない
            if target_cluster_ids:
                self._validate_clusters(conn, next_cid, _sid_filter, _sid_params, target_cluster_ids)

            # HQ OCR 後のクラスタ再統合: Vision OCR が空で phash 任せだった screen が
            # 後から HQ/Gemini OCR でテキストを獲得した場合、同セリフのクラスタを統合する。
            # _remerge_text_clusters は ocr_text_gemini OR ocr_text_hq IS NOT NULL の代表が対象。
            self._remerge_text_clusters(conn)

        finally:
            conn.close()

    def _validate_clusters(
        self,
        conn: sqlite3.Connection,
        next_cid: int,
        sid_filter: str,
        sid_params: tuple,
        target_cluster_ids: set,
    ) -> None:
        """Step B: クラスタ内を dHash 距離で分割する (新規 screen を含む cluster のみ対象)。

        Step A (phash 中間域は「同」) で統合されたクラスタの中から、
        構図が大きく異なるメンバー (代表との dHash 距離 >= 閾値) を分離する。
        分離後、時系列順に隣り合うものを dHash 距離で再グループ化する。

        target_cluster_ids: Step A で影響を受けた (= 新規 screen を含む) cluster_id のセット。
        このセットに含まれる cluster のみが Step B の対象。それ以外の既存 cluster は触らない。
        """
        if not target_cluster_ids:
            return
        _dhash_distance = self._dhash_distance
        DHASH_VALIDATE_THRESHOLD = 22  # cluster の直径 (max pairwise dHash) がこれ以上で分離

        # LC_TEXT_SEPARATION=on の場合、§16 ルール 1「テキスト一致 → 同クラスタ」を
        # Step B でも尊重する: 代表とテキスト一致 (完全/前方一致) するメンバーは
        # dHash 距離が大きくても分離しない。
        text_sep_enabled = os.environ.get("LC_TEXT_SEPARATION", "on").lower() != "off"

        target_placeholders = ",".join("?" * len(target_cluster_ids))
        target_params = list(target_cluster_ids)

        # 2メンバー以上のクラスタを取得 (target_cluster_ids 内のみ)
        clusters = conn.execute(
            f"SELECT cluster_id, COUNT(*) as cnt FROM lc_screens"
            f" WHERE cluster_id IN ({target_placeholders})"
            + sid_filter +
            " GROUP BY cluster_id HAVING cnt > 1",
            target_params + list(sid_params),
        ).fetchall()

        if not clusters:
            return

        split_items: list[tuple[int, str]] = []  # (id, dhash)

        for cluster in clusters:
            cid = cluster["cluster_id"]

            # cluster 内の全メンバー (代表含む) を取得
            all_members = [
                dict(r) for r in conn.execute(
                    "SELECT id, dhash, avg_brightness AS br, is_representative AS rep,"
                    "       ocr_text_hq, ocr_text"
                    " FROM lc_screens"
                    " WHERE cluster_id = ?"
                    "   AND dhash IS NOT NULL AND dhash != ''",
                    (cid,),
                ).fetchall()
            ]
            if len(all_members) < 2:
                continue

            # LC_TEXT_SEPARATION=on のとき、代表のテキストを正規化して保持
            rep_norm_text = ""
            if text_sep_enabled:
                for m in all_members:
                    if m.get("rep"):
                        raw = m.get("ocr_text_hq") or m.get("ocr_text") or ""
                        rep_norm_text = _normalize_text(raw)
                        break

            def _matches_rep_text(member: dict) -> bool:
                """LC_TEXT_SEPARATION=on かつ代表とテキスト一致 (完全/前方一致) なら True。"""
                if not text_sep_enabled or not rep_norm_text:
                    return False
                raw = member.get("ocr_text_hq") or member.get("ocr_text") or ""
                m_norm = _normalize_text(raw)
                if not m_norm:
                    return False
                return (
                    m_norm == rep_norm_text
                    or m_norm.startswith(rep_norm_text)
                    or rep_norm_text.startswith(m_norm)
                )

            # 反復分離: 「他メンバーとの最大 dHash 距離」が一番大きいメンバーから分離
            # 直径 < 閾値 になるまで繰り返し (代表 + 代表とテキスト一致は分離対象外)
            while len(all_members) > 1:
                # 非代表メンバーの最大距離を計算
                max_dists = []
                for m in all_members:
                    if m.get("rep"):
                        continue  # 代表は分離対象外
                    if _matches_rep_text(m):
                        continue  # 代表とテキスト一致 → §16 ルール 1 で分離対象外
                    max_d = max(
                        (_dhash_distance(m["dhash"], o["dhash"])
                         for o in all_members if o["id"] != m["id"]),
                        default=0,
                    )
                    max_dists.append((m, max_d))
                if not max_dists:
                    break  # 代表のみ残る
                # 最遠メンバー (タイ時は br 低い方を分離)
                most_isolated, max_d = max(
                    max_dists,
                    key=lambda x: (x[1], -(x[0].get("br") or 0.0)),
                )
                if max_d < DHASH_VALIDATE_THRESHOLD:
                    break  # 直径 OK
                # 分離
                conn.execute(
                    "UPDATE lc_screens SET cluster_id = NULL, is_representative = 0 WHERE id = ?",
                    (most_isolated["id"],),
                )
                split_items.append((most_isolated["id"], most_isolated["dhash"]))
                all_members = [m for m in all_members if m["id"] != most_isolated["id"]]
                logger.debug(
                    "[BG_WORKER] cluster_validate: id=%d をクラスタ %d から分離 (最大dHash=%d)",
                    most_isolated["id"], cid, max_d,
                )

        if not split_items:
            return

        # ─── 段階 1 完了: 反復分離 → commit (DB 安定化) ───
        conn.commit()

        # ─── 段階 2: 分離 screen の最近傍 cluster 再統合 ───
        # 分離された screen を時系列順 (id 順) に処理し、
        # 直前 screen の cluster の代表との dHash 距離を比較:
        #   距離 < 閾値 + id 差 <= ID_GAP_THRESHOLD → その cluster に再統合 (revalidate_merge)
        #   それ以外 → 新規独立 cluster (revalidate_split)
        # ID_GAP_THRESHOLD: 直前 screen との id 差がこれ以下なら時系列で隣接と判定。
        #   (極端に大きいと「過去の遠い cluster」に飛んで時系列が崩れる)
        ID_GAP_THRESHOLD = 30
        split_items.sort(key=lambda x: x[0])
        merged_count = 0
        new_count = 0
        for sid, dhash in split_items:
            # 直前 screen (cluster_id IS NOT NULL) の id と cluster_id を取得
            prev_screen = conn.execute(
                "SELECT id, cluster_id FROM lc_screens"
                " WHERE id < ? AND cluster_id IS NOT NULL"
                " ORDER BY id DESC LIMIT 1",
                (sid,),
            ).fetchone()

            target_cid: Optional[int] = None
            if prev_screen and prev_screen["cluster_id"] is not None:
                # 時系列で隣接していることを id 差で確認
                if sid - prev_screen["id"] <= ID_GAP_THRESHOLD:
                    prev_cid = prev_screen["cluster_id"]
                    rep = conn.execute(
                        "SELECT dhash FROM lc_screens"
                        " WHERE cluster_id = ? AND is_representative = 1 LIMIT 1",
                        (prev_cid,),
                    ).fetchone()
                    if rep and rep["dhash"]:
                        d = _dhash_distance(rep["dhash"], dhash)
                        if d < DHASH_VALIDATE_THRESHOLD:
                            target_cid = prev_cid

            if target_cid is not None:
                conn.execute(
                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                    (target_cid, sid),
                )
                self._set_decision(conn, sid, target_cid, "revalidate_merge")
                merged_count += 1
            else:
                conn.execute(
                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                    (next_cid, sid),
                )
                self._set_decision(conn, sid, next_cid, "revalidate_split")
                next_cid += 1
                new_count += 1

        # ─── 段階 2 完了: 再統合 → commit (DB 安定化) ───
        conn.commit()

        # ─── 段階 3: 時系列連続性チェック ───

        # 時系列連続性チェック: cluster 内のメンバーで「直前 screen の cluster_id」が
        # 当該 cluster と違う場合、§16「直前クラスタとのみ比較」ルール違反になるので
        # メンバーを切り離して再分配する。
        # 例: A(X) → B(X) → C(Y, 分離) → D(X) で、D の直前 C が X じゃない →
        #     D は X から切り離して別 cluster に。
        gap_merged = 0
        gap_new = 0
        # 必要なデータを 3 つの SELECT で一括取得 (パフォーマンス最適化)
        # 1) 走査対象は target_cluster_ids 内の screen のみ (新規 screen 含む cluster)
        all_data = conn.execute(
            f"SELECT id, cluster_id, dhash, is_representative AS rep FROM lc_screens"
            f" WHERE cluster_id IN ({target_placeholders})"
            f"   AND dhash IS NOT NULL AND dhash != ''"
            + sid_filter +
            " ORDER BY id",
            target_params + list(sid_params),
        ).fetchall()
        # 2) 各 cluster の代表 dhash
        rep_dhash_map: dict[int, str] = {
            r["cluster_id"]: r["dhash"] for r in conn.execute(
                "SELECT cluster_id, dhash FROM lc_screens"
                " WHERE is_representative = 1"
                "   AND dhash IS NOT NULL AND dhash != ''"
                + sid_filter,
                sid_params,
            ).fetchall()
        }
        # 3) 1メンバー cluster の cluster_id (代表が孤立している cluster)
        single_member_cids = {
            r["cluster_id"] for r in conn.execute(
                "SELECT cluster_id FROM lc_screens"
                " WHERE cluster_id IS NOT NULL" + sid_filter +
                " GROUP BY cluster_id HAVING COUNT(*) = 1",
                sid_params,
            ).fetchall()
        }
        # 各 screen の「直前 screen の id と cluster_id」をメモリで計算
        # 注: target 内 screen の直前は target 外でもありうるので、全 screen から計算
        prev_cid_map: dict[int, int] = {}
        prev_id_map: dict[int, int] = {}
        _prev_cid_so_far: Optional[int] = None
        _prev_id_so_far: Optional[int] = None
        for s in conn.execute(
            "SELECT id, cluster_id FROM lc_screens"
            " WHERE cluster_id IS NOT NULL" + sid_filter +
            " ORDER BY id",
            sid_params,
        ).fetchall():
            if _prev_cid_so_far is not None and _prev_id_so_far is not None:
                prev_cid_map[s["id"]] = _prev_cid_so_far
                prev_id_map[s["id"]] = _prev_id_so_far
            _prev_cid_so_far = s["cluster_id"]
            _prev_id_so_far = s["id"]
        # 走査して必要な変更のみ DB に適用
        for m in all_data:
            if m["rep"] and m["cluster_id"] not in single_member_cids:
                continue  # 複数メンバー cluster の代表 → 対象外
            mid = m["id"]
            mcid = m["cluster_id"]
            mdh = m["dhash"]
            prev_for_m = prev_cid_map.get(mid)
            prev_id_for_m = prev_id_map.get(mid)
            if prev_for_m is None or prev_for_m == mcid:
                continue  # 直前も同じ cluster → 連続性 OK
            target_cid: Optional[int] = None
            # id 差が大きい場合は統合先候補から除外 (時系列で離れた古い cluster は除外)
            if prev_id_for_m is not None and mid - prev_id_for_m <= ID_GAP_THRESHOLD:
                rep_dh = rep_dhash_map.get(prev_for_m)
                if rep_dh:
                    d = _dhash_distance(rep_dh, mdh)
                    if d < DHASH_VALIDATE_THRESHOLD:
                        target_cid = prev_for_m
            if target_cid is not None and target_cid != mcid:
                conn.execute(
                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 0 WHERE id = ?",
                    (target_cid, mid),
                )
                self._set_decision(conn, mid, target_cid, "revalidate_merge")
                gap_merged += 1
            elif target_cid is None:
                conn.execute(
                    "UPDATE lc_screens SET cluster_id = ?, is_representative = 1 WHERE id = ?",
                    (next_cid, mid),
                )
                self._set_decision(conn, mid, next_cid, "revalidate_split")
                # 新規 cluster の代表 dhash を map に追加 (連鎖判定用)
                rep_dhash_map[next_cid] = mdh
                next_cid += 1
                gap_new += 1
        conn.commit()

        logger.debug(
            "[BG_WORKER] cluster_validate: %d 件分離 (再統合 %d / 新クラスタ %d) "
            "+ 時系列ギャップ %d 件 (再統合 %d / 新クラスタ %d)",
            len(split_items), merged_count, new_count, gap_merged + gap_new, gap_merged, gap_new,
        )

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
        """ocr_text_hq IS NULL の代表スクリーンを 1 枚処理（クラスタリング後の代表のみ対象）。

        クラスタリングは Vision OCR (ocr_text) で判定済み。HQ OCR は表示用テキストの
        精度向上が目的で、代表画像のみで十分。
        """
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT id, screenshot_path FROM lc_screens"
                " WHERE ocr_text_hq IS NULL"
                " AND is_representative = 1"
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

        1枚1リクエストで8並列送信（コンテキスト汚染防止で精度最優先）。
        ocr_text_gemini カラムに保存。ocr_text_hq は PaddleOCR 結果として保持。
        """
        import os
        from concurrent.futures import ThreadPoolExecutor, as_completed
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return

        conn = self._get_conn()
        try:
            from tools.ap.ocr_correction import (
                _init_gemini_client, _GEMINI_PARALLEL_WORKERS,
                gemini_correct_single,
            )
            # 1回の起動で最大24枚処理
            fetch_limit = 24
            target_sid = self._session_id
            if not target_sid:
                row = conn.execute(
                    "SELECT session_id FROM lc_sessions WHERE status = 'running'"
                    " AND version_id = (SELECT id FROM lc_versions WHERE is_active = 1)"
                    " ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    target_sid = row["session_id"]
            if not target_sid:
                row = conn.execute(
                    "SELECT s.session_id FROM lc_sessions s"
                    " WHERE s.status = 'completed'"
                    " AND s.version_id = (SELECT id FROM lc_versions WHERE is_active = 1)"
                    " AND EXISTS ("
                    "   SELECT 1 FROM lc_screens sc"
                    "   WHERE sc.session_id = s.session_id"
                    "     AND sc.is_representative = 1"
                    "     AND sc.ocr_text_gemini IS NULL"
                    "     AND sc.screenshot_path IS NOT NULL AND sc.screenshot_path != ''"
                    " )"
                    " ORDER BY s.started_at ASC LIMIT 1"
                ).fetchone()
                if row:
                    target_sid = row["session_id"]
            if not target_sid:
                return
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

            # 前処理: 見切れ検出 + 有効アイテム収集
            items = []
            _BLACK_PIXEL_THRESHOLD = 0.50
            for row in rows:
                sid = row["id"]
                path = row["screenshot_path"]
                if not path or not Path(path).exists():
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_gemini = '' WHERE id = ?",
                        (sid,),
                    )
                    continue
                try:
                    import cv2
                    _img = cv2.imread(str(path))
                    if _img is not None:
                        _gray = cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY)
                        _black_ratio = (_gray < 15).sum() / _gray.size
                        if _black_ratio >= _BLACK_PIXEL_THRESHOLD:
                            conn.execute(
                                "UPDATE lc_screens SET ocr_text_gemini = '', is_artifact = 1"
                                " WHERE id = ?", (sid,),
                            )
                            logger.info("[BG_WORKER] 見切れ検出: id=%d black=%.0f%% → artifact",
                                        sid, _black_ratio * 100)
                            continue
                except Exception:
                    pass
                items.append({
                    "id": sid,
                    "screenshot_path": path,
                    "ocr_text": row["ocr"],
                })

            if not items:
                return

            # 1枚1リクエストで並列送信（スレッドごとにClient作成でコネクションプール競合を回避）
            results_list: list[Optional[dict]] = []
            with ThreadPoolExecutor(max_workers=_GEMINI_PARALLEL_WORKERS) as executor:
                futures = {
                    executor.submit(
                        gemini_correct_single,
                        item["screenshot_path"],
                        item["ocr_text"],
                        None,  # スレッドごとに新規Client作成（共有Client→SSLタイムアウト対策）
                        item["id"],
                    ): item
                    for item in items
                }
                for future in as_completed(futures):
                    item = futures[future]
                    try:
                        result = future.result()
                        results_list.append(result)
                    except Exception as e:
                        logger.warning("[GEMINI] 並列処理エラー id=%d: %s", item["id"], e)
                        results_list.append(None)

            # 結果を DB に反映
            import re as _re
            from tools.ap.ocr_correction import _clean_gemini_output
            _has_text = _re.compile(r'[\u3040-\u9fff\u30a0-\u30ffA-Za-z]')
            _pure_num = _re.compile(r'^[\d\s.:/%×+\-~]+$')
            updated = 0
            total = 0
            artifact_count = 0

            # id → item マップ
            item_map = {item["id"]: item for item in items}

            for r in results_list:
                if r is None:
                    continue
                sid = r.get("id")
                if sid is None or sid not in item_map:
                    continue
                item = item_map[sid]

                raw_corrected = (r.get("corrected_text", "") or "").strip()
                corrected = _clean_gemini_output(raw_corrected)
                is_artifact = bool(r.get("is_artifact", False))
                screen_type = r.get("screen_type", "")

                if is_artifact:
                    logger.info("[BG_WORKER] artifact 検出: id=%d type=%s text=%s",
                                sid, screen_type, corrected[:30] if corrected else "(empty)")
                    conn.execute(
                        "UPDATE lc_screens SET is_artifact = 1 WHERE id = ?",
                        (sid,),
                    )
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
                        else:
                            _art_screen = conn.execute(
                                "SELECT cluster_id, session_id FROM lc_screens WHERE id = ?",
                                (sid,),
                            ).fetchone()
                            conn.execute(
                                "UPDATE lc_screens SET is_representative = 0"
                                " WHERE id = ?", (sid,),
                            )
                            logger.debug("[REP_TRACE] id=%d rep=0 (artifact判定, マスター未登録)", sid)
                            if _art_screen and _art_screen["cluster_id"] is not None:
                                conn.execute(
                                    "UPDATE lc_screens SET is_representative = 0"
                                    " WHERE cluster_id = ? AND is_representative = 1",
                                    (_art_screen["cluster_id"],),
                                )
                                _new_rep = conn.execute(
                                    "SELECT id FROM lc_screens"
                                    " WHERE cluster_id = ? AND session_id = ?"
                                    "   AND id != ? AND is_artifact = 0"
                                    " ORDER BY discovered_at LIMIT 1",
                                    (_art_screen["cluster_id"], _art_screen["session_id"], sid),
                                ).fetchone()
                                if _new_rep:
                                    conn.execute(
                                        "UPDATE lc_screens SET is_representative = 1 WHERE id = ?",
                                        (_new_rep["id"],),
                                    )
                                    logger.debug("[REP_TRACE] id=%d rep=1 (artifact代替代表, cid=%d)",
                                                _new_rep["id"], _art_screen["cluster_id"])

                if not corrected and item["ocr_text"]:
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_gemini = '', title = '(UNKNOWN)'"
                        " WHERE id = ?",
                        (sid,),
                    )
                elif corrected:
                    words = [w.strip() for w in corrected.split()
                             if w.strip() and _has_text.search(w)
                             and not _pure_num.match(w.strip())]
                    new_title = " / ".join(words[:3]) if words else corrected[:30]
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_gemini = ?, title = ?"
                        " WHERE id = ?",
                        (corrected, new_title, sid),
                    )
                else:
                    conn.execute(
                        "UPDATE lc_screens SET ocr_text_gemini = '' WHERE id = ?",
                        (sid,),
                    )
                if corrected and corrected != item["ocr_text"]:
                    updated += 1
                total += 1

                # ノイズ語辞書に登録
                for nw in r.get("noise_words", []):
                    nw = (nw or "").strip()
                    if nw:
                        conn.execute(
                            "INSERT OR IGNORE INTO lc_ocr_noise_words (word) VALUES (?)",
                            (nw,),
                        )
                        conn.execute(
                            "UPDATE lc_ocr_noise_words SET count = count + 1,"
                            " last_seen_at = datetime('now') WHERE word = ?",
                            (nw,),
                        )

            # API エラーで結果が返らなかったアイテムは NULL のまま（次回再送信）
            processed_ids = {r.get("id") for r in results_list if r is not None}
            skipped = [item["id"] for item in items if item["id"] not in processed_ids]
            if skipped:
                logger.warning("[GEMINI] %d 件 APIエラー → NULL維持（次回再送信）: %s",
                               len(skipped), skipped[:5])

            # Gemini 伝播は廃止 — remerge との相互作用で誤統合が発生するため
            # 非代表の ocr_text_gemini は NULL のまま。代表昇格時は Gemini に再送信。

            conn.commit()
            if artifact_count > 0:
                logger.info("[BG_WORKER] gemini: %d/%d 件修正, %d artifact (並列%dワーカー)",
                            updated, total, artifact_count, _GEMINI_PARALLEL_WORKERS)
            elif total > 0:
                logger.info("[BG_WORKER] gemini: %d/%d 件修正 (並列%dワーカー)",
                            updated, total, _GEMINI_PARALLEL_WORKERS)
            if total > 0:
                self._remerge_text_clusters(conn)
        finally:
            conn.close()

    def _remerge_text_clusters(self, conn) -> None:
        """OCR (Gemini または PaddleOCR HQ) で得られた良質テキストで直前クラスタと再照合する。

        対象: ocr_text_gemini または ocr_text_hq が設定済みの代表画面。
        Step A クラスタリング時点では Vision OCR (ocr_text) が空で phash 任せだった
        画面が、後から HQ/Gemini OCR で text を獲得した場合に同セリフで統合する。

        discovered_at 順に走査し、直前クラスタとのみ比較する（§16 厳格ルール）。
        - テキスト一致/前方一致/類似 → 直前クラスタに統合 (長い方が代表)
        - テキスト空同士 → phash < 30 で直前クラスタに統合
        """
        try:
            _phash_distance = self._phash_distance
            _EMPTY_HASH_THRESHOLD = 30

            sid_filter = ""
            sid_params: tuple = ()
            if self._session_id:
                sid_filter = " AND session_id = ?"
                sid_params = (self._session_id,)

            # anchor_matcher のノイズ除去 + 類似度計算を流用
            from tools.anchor_matcher import (
                _normalize_for_comparison, _text_similarity,
            )

            # 代表画面の補正テキストを discovered_at 順に取得
            # Gemini → HQ → Vision の優先順で fallback
            reps = conn.execute(
                "SELECT id, cluster_id, phash,"
                " COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS gemini_text"
                " FROM lc_screens"
                " WHERE is_representative = 1"
                " AND (ocr_text_gemini IS NOT NULL OR ocr_text_hq IS NOT NULL)"
                " AND phash IS NOT NULL AND phash != ''"
                + sid_filter +
                " ORDER BY discovered_at",
                sid_params,
            ).fetchall()

            if len(reps) < 2:
                return

            merged = 0
            merged_clusters: set[int] = set()

            # 直前クラスタの情報を追跡
            prev_cid: Optional[int] = None
            prev_ph: Optional[str] = None
            prev_norm: Optional[str] = None
            prev_orig: Optional[str] = None

            for r in reps:
                cid = r["cluster_id"]
                if cid in merged_clusters:
                    continue
                ph = r["phash"]
                orig = _normalize_text(r["gemini_text"])
                norm = _normalize_for_comparison(orig, conn)

                should_merge = False

                if prev_cid is not None and prev_cid != cid:
                    if norm and prev_norm:
                        # テキストあり同士: 類似度判定
                        sim = _text_similarity(norm, prev_norm)
                        if sim >= 0.85:
                            should_merge = True
                        else:
                            # 前方一致 (5文字以上) フォールバック
                            shorter, longer = (norm, prev_norm) if len(norm) <= len(prev_norm) else (prev_norm, norm)
                            if len(shorter) >= 5 and longer.startswith(shorter):
                                should_merge = True
                    elif not norm and not prev_norm:
                        # テキスト空同士: ノイズ除去前も空の場合のみハッシュで判定
                        if not orig and not prev_orig:
                            # 縮退 phash (ほぼ単色画像) はアンカーとして信頼できない
                            # → 連鎖マージで遠いクラスタまで吸い込む問題を防ぐため除外
                            if _is_degenerate_phash(ph) or _is_degenerate_phash(prev_ph):
                                pass  # 統合しない
                            else:
                                d = _phash_distance(ph, prev_ph) if ph and prev_ph else 999
                                if d < _EMPTY_HASH_THRESHOLD:
                                    should_merge = True

                if should_merge:
                    # cid を prev_cid に統合
                    _affected_ids = [row["id"] for row in conn.execute(
                        "SELECT id FROM lc_screens WHERE cluster_id = ?", (cid,)).fetchall()]
                    conn.execute(
                        "UPDATE lc_screens SET cluster_id = ?, is_representative = 0,"
                        " cluster_id_hybrid = ?, cluster_decision_method = 'remerge'"
                        " WHERE cluster_id = ?",
                        (prev_cid, prev_cid, cid),
                    )
                    for _aid in _affected_ids:
                        logger.debug("[REP_TRACE] id=%d rep=0 (remerge統合, cid=%d→prev_cid=%d)", _aid, cid, prev_cid)
                    # 統合先クラスタの代表をリセットしてから1枚だけ選出
                    conn.execute(
                        "UPDATE lc_screens SET is_representative = 0 WHERE cluster_id = ?",
                        (prev_cid,),
                    )
                    rep = conn.execute(
                        "SELECT id FROM lc_screens WHERE cluster_id = ?"
                        " ORDER BY LENGTH(COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '')) DESC,"
                        " discovered_at ASC LIMIT 1",
                        (prev_cid,),
                    ).fetchone()
                    if rep:
                        conn.execute(
                            "UPDATE lc_screens SET is_representative = 1 WHERE id = ?",
                            (rep["id"],),
                        )
                    conn.commit()
                    merged_clusters.add(cid)
                    merged += 1
                    # prev_cid はそのまま（統合先を維持）
                    # prev の norm/orig/ph は代表が変わった可能性があるので更新
                    if rep:
                        _new_rep = conn.execute(
                            "SELECT phash, COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS gemini_text"
                            " FROM lc_screens WHERE id = ?",
                            (rep["id"],),
                        ).fetchone()
                        if _new_rep:
                            prev_ph = _new_rep["phash"]
                            prev_orig = _normalize_text(_new_rep["gemini_text"])
                            prev_norm = _normalize_for_comparison(prev_orig, conn)
                else:
                    # 統合しない → 直前クラスタを更新
                    prev_cid = cid
                    prev_ph = ph
                    prev_norm = norm
                    prev_orig = orig

            if merged > 0:
                logger.info("[BG_WORKER] text remerge: %d クラスタ統合", merged)
        except Exception as e:
            logger.warning("[BG_WORKER] text remerge 例外: %s", e)

    # ─── 合成エッジ ─────────────────────────────────────

    _AUTO_EDGE_TIMEOUT = 60  # 秒: これ以上離れていたら文脈切れ

    def _synthesize_auto_edges_for(self, session_id: str) -> None:
        """指定セッションの合成エッジを生成。"""
        orig = self._session_id
        self._session_id = session_id
        try:
            self._synthesize_auto_edges()
        finally:
            self._session_id = orig

    def _synthesize_auto_edges(self) -> None:
        """artifact でない有効画面間に合成エッジ (auto) を注入する。

        is_representative=1 かつ is_artifact=0 の画面を discovered_at 順に走査し、
        既に tap エッジがない隣接画面間に edge_type='auto' の遷移を追加する。
        冪等: 既存の auto エッジがあれば INSERT OR IGNORE でスキップ。
        """
        conn = self._get_conn()
        try:
            sid_filter = ""
            sid_params: tuple = ()
            if self._session_id:
                sid_filter = " AND session_id = ?"
                sid_params = (self._session_id,)

            # 有効な代表画面を時系列順に取得
            rows = conn.execute(
                "SELECT id, fingerprint, session_id, discovered_at"
                " FROM lc_screens"
                " WHERE is_representative = 1"
                " AND COALESCE(is_artifact, 0) = 0"
                + sid_filter +
                " ORDER BY discovered_at ASC",
                sid_params,
            ).fetchall()

            if len(rows) < 2:
                return

            # 既存の tap エッジを高速検索用にセット化
            existing = set()
            for r in conn.execute(
                "SELECT from_fp, to_fp FROM lc_transitions"
                " WHERE edge_type = 'tap' OR edge_type IS NULL"
                + sid_filter,
                sid_params,
            ).fetchall():
                existing.add((r["from_fp"], r["to_fp"]))

            inserted = 0
            prev = rows[0]
            for cur in rows[1:]:
                # セッションが異なる場合はスキップ
                if cur["session_id"] != prev["session_id"]:
                    prev = cur
                    continue
                # タイムアウト: 60秒以上空いていたら文脈切れ
                from datetime import datetime
                try:
                    t_prev = datetime.fromisoformat(prev["discovered_at"])
                    t_cur = datetime.fromisoformat(cur["discovered_at"])
                    gap = (t_cur - t_prev).total_seconds()
                except (ValueError, TypeError):
                    gap = 0
                if gap > self._AUTO_EDGE_TIMEOUT:
                    prev = cur
                    continue
                # 既に tap エッジがあればスキップ
                if (prev["fingerprint"], cur["fingerprint"]) in existing:
                    prev = cur
                    continue
                # 合成エッジを挿入
                conn.execute(
                    "INSERT OR IGNORE INTO lc_transitions"
                    " (session_id, from_screen_id, to_screen_id, from_fp, to_fp,"
                    "  action_name, edge_type, discovered_at)"
                    " VALUES (?, ?, ?, ?, ?, 'AUTO_TRANSITION', 'auto', ?)",
                    (cur["session_id"], prev["id"], cur["id"],
                     prev["fingerprint"], cur["fingerprint"],
                     cur["discovered_at"]),
                )
                inserted += 1
                prev = cur

            if inserted > 0:
                conn.commit()
                logger.info("[BG_WORKER] auto edges: %d 件の合成エッジを追加", inserted)
        except Exception as e:
            logger.warning("[BG_WORKER] auto edges 例外: %s", e)
        finally:
            conn.close()

    # ─── 遷移グラフ構築 ───────────────────────────────────

    def _run_graph_build(self) -> None:
        """OCR + クラスタリング完了済みセッションの遷移グラフを構築。"""
        import os as _os
        _has_gemini = bool(_os.environ.get("GEMINI_API_KEY"))
        conn = self._get_conn()
        try:
            # グラフ未構築の completed セッションを探す
            # OCR + クラスタリングが完了しているセッションのみ対象
            targets = conn.execute(
                "SELECT s.session_id FROM lc_sessions s"
                " WHERE s.status = 'completed'"
                " AND s.version_id = (SELECT id FROM lc_versions WHERE is_active = 1)"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM lc_session_graphs sg WHERE sg.session_id = s.session_id"
                " )"
                " AND NOT EXISTS ("
                "   SELECT 1 FROM lc_screens sc WHERE sc.session_id = s.session_id"
                "     AND sc.cluster_id IS NULL AND sc.phash IS NOT NULL AND sc.phash != ''"
                " )"
                + (" AND NOT EXISTS ("
                   "   SELECT 1 FROM lc_screens sc WHERE sc.session_id = s.session_id"
                   "     AND sc.is_representative = 1 AND sc.ocr_text_gemini IS NULL"
                   "     AND sc.screenshot_path IS NOT NULL AND sc.screenshot_path != ''"
                   " )" if _has_gemini else
                   " AND NOT EXISTS ("
                   "   SELECT 1 FROM lc_screens sc WHERE sc.session_id = s.session_id"
                   "     AND sc.ocr_text_hq IS NULL"
                   "     AND sc.screenshot_path IS NOT NULL AND sc.screenshot_path != ''"
                   " )")
                + " ORDER BY s.started_at ASC"
            ).fetchall()
            if not targets:
                return
        finally:
            conn.close()

        try:
            from tools.batch_processor import BatchProcessor
            bp = BatchProcessor(db_path=self._db_path)
            try:
                for row in targets:
                    sid = row["session_id"]
                    # 合成エッジを先に生成
                    self._synthesize_auto_edges_for(sid)
                    sccs = bp.build_graph(session_id=sid)
                    if sccs > 0:
                        self.graph_sccs = sccs
                        logger.info("[BG_WORKER] graph: session=%s, %d SCC 構築完了", sid, sccs)
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
                " AND s.version_id = (SELECT id FROM lc_versions WHERE is_active = 1)"
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
                _sm_parts = ", ".join(f"{k}={v}" for k, v in sm.items())
                logger.info(
                    "[BG_WORKER] merge preview: session=%s, screens=%d, %s",
                    sid, preview["session_screens"], _sm_parts,
                )
                # マージ実行
                new_nodes = merger.merge_to_master(sid)
                logger.info("[BG_WORKER] merge done: session=%s, +%d new nodes", sid, new_nodes)
        finally:
            merger.close()
