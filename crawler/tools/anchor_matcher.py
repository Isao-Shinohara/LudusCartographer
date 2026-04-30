"""
anchor_matcher.py — 段階的アンカーマッチング

安全性最優先: 安全性が担保できないなら破棄。
周回を重ねれば自然にアンカーは増える。

実行順: P1 → P2 → P3 → P4 → P5 → P6

Phase 1: tap + テキスト完全/前方一致 (最も確実)
Phase 2: auto + テキストあり (無料・高速)
Phase 3: tap + テキスト空 + phash (無料・高速)
Phase 4: テキスト Gemini 判定 (テキストのみ送信、画像なし、安価)
Phase 5: 画像 Gemini Flash-Lite (phash<8, 高確信ペア)
Phase 6: 画像 Gemini Flash (phash 8-20 + P5 棄却再審査)
auto + テキスト空 → マッチ対象外
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

logger = logging.getLogger(__name__)


def _normalize_text(text: str) -> str:
    """OCR テキストの揺れを正規化。background_worker と同一ロジック。"""
    import re
    import unicodedata
    t = unicodedata.normalize("NFKC", text)
    t = t.replace("\u2026", "...").replace("\u30fb\u30fb\u30fb", "...").replace("\u30fb\u30fb", "..")
    t = re.sub(r'\.{2,}', '...', t)
    t = re.sub(r'\s+', ' ', t).strip()
    t = t.replace("\uff0b", "+").replace("\uff06", "&").replace("\uff01", "!").replace("\uff1f", "?")
    t = re.sub(r'[\u3001\u3002,.!?\u2026\u30fb\-\u2015~\uff5e\s]+$', '', t)
    # 英字と日本語の境界スペースを除去 (OCR でスペースが入ったり入らなかったりする)
    t = re.sub(r'(?<=[\u3040-\u9fff\u30a0-\u30ff]) (?=[A-Za-z0-9])', '', t)
    t = re.sub(r'(?<=[A-Za-z0-9]) (?=[\u3040-\u9fff\u30a0-\u30ff])', '', t)
    return t


# ノイズ語辞書キャッシュ (DB から読み込み)
_noise_words_cache: set[str] | None = None


def _load_noise_words(conn: sqlite3.Connection) -> set[str]:
    """lc_ocr_noise_words テーブルからノイズ語を読み込む。"""
    global _noise_words_cache
    if _noise_words_cache is not None:
        return _noise_words_cache
    try:
        rows = conn.execute("SELECT word FROM lc_ocr_noise_words WHERE count >= 2").fetchall()
        _noise_words_cache = {r["word"] if isinstance(r, sqlite3.Row) else r[0] for r in rows}
    except Exception:
        _noise_words_cache = set()
    # デフォルトノイズ語 (Gemini 辞書が育つまでのフォールバック)
    _noise_words_cache |= {"AUTO", "SKIP", "NEW", "WAVE", "Turn", "MAX"}
    return _noise_words_cache


def _normalize_for_comparison(text: str, conn: sqlite3.Connection | None = None) -> str:
    """比較用のノイズ除去付き正規化。元テキストは変えない。"""
    import re
    import unicodedata
    # スペースが残っている段階で数値トークンを先に除去
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r'\s+', ' ', t).strip()
    _NUM_TOKEN = re.compile(r'^(?:[\d,.:/%×+\-~]+|[A-Za-z]{0,3}\.?\d[\d,.]*%?)$')
    t = ' '.join(w for w in t.split() if not _NUM_TOKEN.match(w))
    # その後に通常の正規化
    t = _normalize_text(t)
    # ノイズ語を除去 (日本語境界対応: \b の代わりにスペース区切り + 先頭末尾)
    noise = _load_noise_words(conn) if conn is not None else set()
    # デフォルトノイズ + DB ノイズ
    all_noise = noise | {"AUTO", "SKIP", "MANUAL", "NEW", "WAVE", "Turn", "MAX"}
    # Episode + 数字パターン
    t = re.sub(r'Episode\d*', '', t, flags=re.IGNORECASE)
    # 1文字のノイズ (i, !, ※, +, ★ 等) — 単独で出現するもの
    t = re.sub(r'(?<=\s)[i!※★☆+×]\s', ' ', t)
    t = re.sub(r'^[i!※★☆+×]\s', '', t)
    # ノイズ語を除去 (スペース区切りトークン + 日本語にくっついたケース)
    tokens = t.split()
    tokens = [w for w in tokens if w not in all_noise]
    t = ' '.join(tokens)
    # 英字ノイズ語が日本語にくっついているケース (例: AUTO暁美, キオクSKIP)
    for nw in sorted(all_noise, key=len, reverse=True):
        if re.search(r'[A-Z]', nw):  # 英字ノイズ語のみ
            t = re.sub(re.escape(nw), '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _bow_similarity(a: str, b: str) -> float:
    """Bag-of-words 類似度。単語順を無視して共通単語の割合を返す。"""
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)  # Jaccard 係数


def _text_similarity(a: str, b: str) -> float:
    """SequenceMatcher と bag-of-words の高い方を返す。"""
    seq_sim = SequenceMatcher(None, a, b).ratio()
    bow_sim = _bow_similarity(a, b)
    return max(seq_sim, bow_sim)


@dataclass
class NodeInfo:
    """マッチング用のノード情報。"""
    fp: str
    text: str           # normalize 済みテキスト
    phash: str
    scene: str
    edge_type: str      # "tap" / "auto" / "none"
    has_text: bool
    time_rank: int       # セッション内の時系列順位 (0始まり)


@dataclass
class AnchorMatch:
    """確定したアンカーマッチ。"""
    session_fp: str
    master_fp: str
    master_sort: int
    method: str          # PHASE_DEFS の key
    score: float
    phase: int


# ─── フェーズ定義 (表示順・表示名・色を一元管理) ─────────
# key: 内部メソッド名 (DB・API で使用、変更しない)
# order: 実行順 (表示順)
# label: UI 表示名
# color_bg / color_text: Tailwind CSS クラス
PHASE_DEFS: dict[str, dict] = {
    "direct_fp_match": {
        "order": 0,
        "label": "FP",
        "description": "直接 fp 一致 (同 fingerprint)",
        "color_bg": "bg-green-900/50",
        "color_text": "text-green-300",
    },
    "phase1_tap_text": {
        "order": 1,
        "label": "P1",
        "description": "tap + テキスト一致",
        "color_bg": "bg-blue-900/50",
        "color_text": "text-blue-300",
    },
    "phase2_auto_text": {
        "order": 2,
        "label": "P2",
        "description": "auto + テキスト一致",
        "color_bg": "bg-purple-900/50",
        "color_text": "text-purple-300",
    },
    "phase3_tap_phash": {
        "order": 3,
        "label": "P3",
        "description": "tap + phash (テキスト空)",
        "color_bg": "bg-yellow-900/50",
        "color_text": "text-yellow-300",
    },
    "phase4_gemini_text": {
        "order": 4,
        "label": "P4",
        "description": "Gemini テキスト判定",
        "color_bg": "bg-teal-900/50",
        "color_text": "text-teal-300",
    },
    "phase5_gemini_image": {
        "order": 5,
        "label": "P5",
        "description": "Gemini Flash-Lite 画像判定",
        "color_bg": "bg-amber-900/50",
        "color_text": "text-amber-300",
    },
    "phase6_gemini_flash": {
        "order": 6,
        "label": "P6",
        "description": "Gemini Flash 画像判定",
        "color_bg": "bg-red-900/50",
        "color_text": "text-red-300",
    },
}


def _text_hash(text: str) -> str:
    """テキストの SHA256 ハッシュ (キャッシュキー用)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ensure_judgment_table(conn: sqlite3.Connection) -> None:
    """Gemini 判定キャッシュテーブルを作成 (v4: model カラム追加)。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_anchor_judgments (
            session_fp TEXT NOT NULL,
            master_fp  TEXT NOT NULL,
            is_same    INTEGER NOT NULL,
            prefer     TEXT DEFAULT '',
            model      TEXT DEFAULT '',
            judged_at  TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (session_fp, master_fp)
        )
    """)
    # マイグレーション
    for col, default in [("prefer", "''"), ("model", "''")]:
        try:
            conn.execute(f"ALTER TABLE lc_anchor_judgments ADD COLUMN {col} TEXT DEFAULT {default}")
        except Exception:
            pass
    conn.commit()


class AnchorMatcher:
    """段階的アンカーマッチング。"""

    # Phase 1: テキスト一致 + ハッシュ閾値 (phash ベース値、translate_threshold で変換)
    PHASE1_PHASH_THRESHOLD = 30       # 完全一致/前方一致用
    PHASE1_FUZZY_PHASH_THRESHOLD = 20  # あいまい一致用

    # Phase 2: auto + テキスト (P1 と同じあいまい一致を適用)
    PHASE2_FUZZY_PHASH_THRESHOLD = 20

    # Phase 3: tap + テキスト空 + phash のみ
    PHASE3_PHASH_THRESHOLD = 15

    @staticmethod
    def _hash_col() -> str:
        """phash 固定 (常に phash カラムを使う)。"""
        return "phash"

    @staticmethod
    def _hash_distance(h1: str, h2: str) -> int:
        from lc.image_comparator import phash_distance
        return phash_distance(h1, h2)

    @staticmethod
    def _th(phash_threshold: int) -> int:
        """phash 固定なので閾値変換は不要 (identity)。"""
        return phash_threshold

    @staticmethod
    def _fuzzy_sim_threshold(text_len: int) -> float:
        """テキスト長に応じたあいまい一致閾値。P4 (Gemini) が最終検証するため攻めの閾値。

        短いテキストはOCR 1文字の揺れで類似度が大きく落ちるため閾値を下げる。
        長いテキストは小さな揺れが多数積み重なるため同様に閾値を下げる。
        """
        if text_len < 20:
            return 0.5   # 短い: 1文字違いで大きく変動。P4 で画像検証
        elif text_len < 50:
            return 0.65  # やや短い
        elif text_len < 200:
            return 0.8   # 中間: OCR 揺れが数箇所
        else:
            return 0.7   # 長い: 揺れが積み重なる

    # Phase 4: テキスト Gemini (テキストのみ、画像なし)
    PHASE4_TEXT_SIMILARITY = 0.4   # テキスト類似度の下限
    PHASE4_PHASH_THRESHOLD = 20    # phash 距離の上限

    # Phase 5: 画像 Gemini Flash-Lite (phash<8, 高確信ペア)
    PHASE5_PHASH_THRESHOLD = 8
    PHASE5_TEXT_SIMILARITY = 0.4

    # Phase 6: 画像 Gemini Flash (phash 8-20 + P5 棄却再審査)
    PHASE6_PHASH_MIN = 8     # P5 の上限から
    PHASE6_PHASH_MAX = 20    # この未満まで
    PHASE6_TEXT_SIMILARITY = 0.3

    @staticmethod
    def _write_progress(conn: sqlite3.Connection, phase: str,
                        total_anchors: int, total_nodes: int) -> None:
        """Phase 進捗を auto_pilot_state に書き込む (ポーリング用)。"""
        import json as _json
        try:
            conn.execute(
                "INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_phase', ?)",
                (_json.dumps({"phase": phase, "anchors": total_anchors,
                              "total": total_nodes}, ensure_ascii=False),),
            )
            conn.commit()
        except Exception:
            pass

    def compute_matches(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        version_id: int | None = None,
    ) -> tuple[dict[str, tuple[str, str, float]], list[str]]:
        """全 Phase を実行し、マッチ結果を返す。

        Returns:
            (node_mapping, skipped_fps, discarded_mapping, excluded_mapping)
            node_mapping: session_fp → (master_fp, method, score)
            skipped_fps: マッチしなかった session_fp のリスト
            discarded_mapping: Gemini 棄却
            excluded_mapping: 不採用ノード一致 session_fp → (master_fp, method, score)
        """
        session_nodes, master_nodes, master_sort_map, excluded_master_fps = self._prepare_data(conn, session_id, version_id)

        if not session_nodes or not master_nodes:
            return {}, [n.fp for n in session_nodes]

        total = len(session_nodes)

        # Phase 1: tap + テキスト完全/前方一致 (最も確実)
        self._write_progress(conn, "P1 実行中...", 0, total)
        anchors = self._phase1_tap_text(session_nodes, master_nodes, master_sort_map)
        logger.info("[AnchorMatcher] Phase 1: %d アンカー確定", len(anchors))
        self._write_progress(conn, "P1 完了", len(anchors), total)

        # Phase 2: auto + テキストあり (無料・高速)
        phase2 = self._phase2_auto_text(session_nodes, master_nodes, master_sort_map, anchors)
        anchors.extend(phase2)
        logger.info("[AnchorMatcher] Phase 2: +%d → 合計 %d アンカー", len(phase2), len(anchors))
        self._write_progress(conn, "P2 完了", len(anchors), total)

        # Phase 3: tap + テキスト空 + phash (無料・高速)
        phase3 = self._phase3_tap_phash(session_nodes, master_nodes, master_sort_map, anchors)
        anchors.extend(phase3)
        logger.info("[AnchorMatcher] Phase 3: +%d → 合計 %d アンカー", len(phase3), len(anchors))
        self._write_progress(conn, "P3 完了", len(anchors), total)

        # Phase 4: テキスト Gemini (テキストのみ送信、画像なし、安価)
        self._write_progress(conn, "P4 Gemini テキスト判定中...", len(anchors), total)
        phase4_new, phase4_rejected = self._phase4_gemini_text(
            conn, session_nodes, master_nodes, master_sort_map, anchors, version_id)
        anchors.extend(phase4_new)
        logger.info("[AnchorMatcher] Phase 4: +%d (棄却%d → P5へ) → 合計 %d アンカー",
                    len(phase4_new), len(phase4_rejected), len(anchors))
        self._write_progress(conn, "P4 完了", len(anchors), total)

        # Phase 5: 画像 Gemini Flash-Lite (P3検証 + P4棄却再検証 + 新規候補)
        self._write_progress(conn, "P5 Gemini 画像判定中...", len(anchors), total)
        phase5_new, phase5_rejected = self._phase5_gemini_image(
            conn, session_nodes, master_nodes, master_sort_map, anchors, version_id,
            p4_rejected=phase4_rejected)
        p5_rejected_fps = {a.session_fp for a in phase5_rejected}
        if p5_rejected_fps:
            anchors = [a for a in anchors if a.session_fp not in p5_rejected_fps]
            logger.info("[AnchorMatcher] Phase 5 検証: %d 件棄却 → Phase 6 へ", len(p5_rejected_fps))
        anchors.extend(phase5_new)
        logger.info("[AnchorMatcher] Phase 5: +%d → 合計 %d アンカー", len(phase5_new), len(anchors))
        self._write_progress(conn, "P5 完了", len(anchors), total)

        # Phase 6: 画像 Gemini Flash (phash 8-20 + P5 棄却再審査)
        self._write_progress(conn, "P6 Gemini Flash 判定中...", len(anchors), total)
        phase6_new, phase6_final_rejected = self._phase6_gemini_flash(
            conn, session_nodes, master_nodes, master_sort_map, anchors,
            phase5_rejected, version_id)
        anchors.extend(phase6_new)
        logger.info("[AnchorMatcher] Phase 6: +%d (復活%d + 新規%d) → 合計 %d アンカー",
                    len(phase6_new),
                    sum(1 for a in phase6_new if a.session_fp in p5_rejected_fps),
                    sum(1 for a in phase6_new if a.session_fp not in p5_rejected_fps),
                    len(anchors))
        self._write_progress(conn, "完了", len(anchors), total)

        # 結果を node_mapping 形式に変換 (不採用ノード一致を分離)
        matched_session_fps = set()
        node_mapping: dict[str, tuple[str, str, float]] = {}
        excluded_mapping: dict[str, tuple[str, str, float]] = {}
        for a in anchors:
            if a.master_fp in excluded_master_fps:
                excluded_mapping[a.session_fp] = (a.master_fp, a.method, a.score)
            else:
                node_mapping[a.session_fp] = (a.master_fp, a.method, a.score)
            matched_session_fps.add(a.session_fp)

        skipped = [n.fp for n in session_nodes if n.fp not in matched_session_fps]

        excluded_count = len(excluded_mapping)
        active_count = len(node_mapping)
        logger.info(
            "[AnchorMatcher] session=%s: anchor=%d, excluded_match=%d"
            " (P1=%d, P2=%d, P3=%d, P4=%d, P5=%d, P6=%d), skipped=%d",
            session_id, active_count, excluded_count,
            sum(1 for a in anchors if a.phase == 1 and a.master_fp not in excluded_master_fps),
            sum(1 for a in anchors if a.phase == 2 and a.master_fp not in excluded_master_fps),
            sum(1 for a in anchors if a.phase == 3 and a.master_fp not in excluded_master_fps),
            sum(1 for a in anchors if a.phase == 4 and a.master_fp not in excluded_master_fps),
            sum(1 for a in anchors if a.phase == 5 and a.master_fp not in excluded_master_fps),
            sum(1 for a in anchors if a.phase == 6 and a.master_fp not in excluded_master_fps),
            len(skipped),
        )

        # Gemini 検証で最終棄却されたアンカー (P6 でも棄却)
        discarded_mapping: dict[str, tuple[str, str, float, str]] = {}
        for a in phase6_final_rejected:
            discarded_mapping[a.session_fp] = (
                a.master_fp, a.method, a.score, "Gemini Flash 再審査でも別画面と判定"
            )

        return node_mapping, skipped, discarded_mapping, excluded_mapping

    # ─── データ準備 ───────────────────────────────────

    def _prepare_data(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        version_id: int | None = None,
    ) -> tuple[list[NodeInfo], list[NodeInfo], dict[str, int], set[str]]:
        """DB からデータ取得し、ノード分類する。

        Returns:
            (session_nodes, master_nodes, master_sort_map, excluded_master_fps)
            excluded_master_fps: user_excluded=1 のマスターノード fp セット
        """
        _hcol = self._hash_col()

        # セッション側: 代表画面を時系列順に取得
        rows = conn.execute(
            f"SELECT s.fingerprint, s.{_hcol} AS phash, s.scene,"
            " COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') AS text"
            " FROM lc_screens s"
            " WHERE s.session_id = ? AND s.is_representative = 1"
            " AND COALESCE(s.is_artifact, 0) = 0"
            " ORDER BY s.discovered_at ASC",
            (session_id,),
        ).fetchall()

        # 各ノードの edge_type を判定
        tap_fps = set()
        auto_fps = set()
        for r in conn.execute(
            "SELECT DISTINCT from_fp, to_fp, COALESCE(edge_type, 'tap') AS et"
            " FROM lc_transitions WHERE session_id = ?",
            (session_id,),
        ).fetchall():
            et = r["et"]
            if et == "tap":
                if r["from_fp"]:
                    tap_fps.add(r["from_fp"])
                if r["to_fp"]:
                    tap_fps.add(r["to_fp"])
            elif et == "auto":
                if r["from_fp"]:
                    auto_fps.add(r["from_fp"])
                if r["to_fp"]:
                    auto_fps.add(r["to_fp"])

        session_nodes: list[NodeInfo] = []
        for rank, r in enumerate(rows):
            fp = r["fingerprint"]
            text = _normalize_for_comparison(r["text"] or "", conn)
            if fp in tap_fps:
                et = "tap"
            elif fp in auto_fps:
                et = "auto"
            else:
                et = "none"
            session_nodes.append(NodeInfo(
                fp=fp, text=text, phash=r["phash"] or "",
                scene=r["scene"] or "", edge_type=et,
                has_text=len(text) > 0, time_rank=rank,
            ))

        # マスター側
        _v_filter = " WHERE version_id = ?" if version_id else ""
        _v_params = (version_id,) if version_id else ()
        master_rows = conn.execute(
            f"SELECT master_fp, {_hcol} AS phash, scene, sort_order,"
            " COALESCE(ocr_text_manual, ocr_text, '') AS text,"
            " COALESCE(user_excluded, 0) AS user_excluded"
            " FROM lc_master_nodes" + _v_filter
            + " ORDER BY sort_order ASC",
            _v_params,
        ).fetchall()

        master_nodes: list[NodeInfo] = []
        master_sort_map: dict[str, int] = {}
        excluded_master_fps: set[str] = set()
        for r in master_rows:
            text = _normalize_for_comparison(r["text"] or "", conn)
            sort_order = r["sort_order"] if r["sort_order"] is not None else -1
            master_nodes.append(NodeInfo(
                fp=r["master_fp"], text=text, phash=r["phash"] or "",
                scene=r["scene"] or "", edge_type="",
                has_text=len(text) > 0, time_rank=sort_order,
            ))
            master_sort_map[r["master_fp"]] = sort_order
            if r["user_excluded"]:
                excluded_master_fps.add(r["master_fp"])

        return session_nodes, master_nodes, master_sort_map, excluded_master_fps

    # ─── Phase 1: tap + テキストあり ──────────────────

    def _phase1_tap_text(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
    ) -> list[AnchorMatch]:
        """tap + テキストありノードをテキスト一致 + phash でマッチ。
        完全一致/前方一致 → あいまい一致 (sim>=0.9, phash<20, 候補1件) の順。
        """
        _hash_distance = self._hash_distance

        # マスター側のテキスト → ノード逆引き
        master_by_text: dict[str, list[NodeInfo]] = {}
        for m in master_nodes:
            if m.has_text:
                master_by_text.setdefault(m.text, []).append(m)

        targets = [n for n in session_nodes if n.edge_type == "tap" and n.has_text]
        anchors: list[AnchorMatch] = []
        matched_session_fps: set[str] = set()
        matched_master_fps: set[str] = set()

        # --- Pass 1: 完全一致 / 前方一致 ---
        for s in targets:
            candidates = [m for m in master_by_text.get(s.text, [])
                          if m.fp not in matched_master_fps]

            if not candidates and len(s.text) >= 5:
                for text, ms in master_by_text.items():
                    if not text:
                        continue
                    shorter, longer = (s.text, text) if len(s.text) <= len(text) else (text, s.text)
                    if len(shorter) >= 5 and longer.startswith(shorter):
                        candidates.extend(m for m in ms if m.fp not in matched_master_fps)

            if len(candidates) != 1:
                continue

            m = candidates[0]
            if s.phash and m.phash:
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE1_PHASH_THRESHOLD):
                    continue

            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase1_tap_text", score=1.0, phase=1,
            ))
            matched_session_fps.add(s.fp)
            matched_master_fps.add(m.fp)

        # --- Pass 2: あいまい一致 (sim>=0.9, phash<20, 候補1件) ---
        fuzzy_targets = [n for n in targets if n.fp not in matched_session_fps]
        for s in fuzzy_targets:
            if not s.phash:
                continue
            best_m = None
            best_sim = 0.0
            candidate_count = 0
            for m in master_nodes:
                if m.fp in matched_master_fps or not m.phash or not m.has_text:
                    continue
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE1_FUZZY_PHASH_THRESHOLD):
                    continue
                sim = _text_similarity(s.text, m.text)
                if sim >= self._fuzzy_sim_threshold(len(s.text)):
                    candidate_count += 1
                    if sim > best_sim:
                        best_m = m
                        best_sim = sim
            if candidate_count == 1 and best_m:
                anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=best_m.fp,
                    master_sort=master_sort_map[best_m.fp],
                    method="phase1_tap_text", score=round(best_sim, 3), phase=1,
                ))
                matched_session_fps.add(s.fp)
                matched_master_fps.add(best_m.fp)

        return anchors

    # ─── Phase 4: テキスト Gemini 判定 (画像なし、安価) ────

    def _phase4_gemini_text(
        self,
        conn: sqlite3.Connection,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
        version_id: int | None = None,
    ) -> tuple[list[AnchorMatch], list[tuple]]:
        """Phase 4: テキストのみで Gemini に同一画面判定を問い合わせる。

        画像を送信しないため安価。P1-P3 で未マッチのテキストあり候補を対象に、
        OCR テキストペアだけで同一画面か判定する。

        Returns:
            (new_anchors, rejected_candidates)
            new_anchors: 新たにマッチしたアンカー
            rejected_candidates: 棄却された候補 [(session_node, master_node, sim)] — P5 で画像再検証用
        """
        _hash_distance = self._hash_distance

        matched_session_fps = {a.session_fp for a in existing_anchors}
        matched_master_fps = {a.master_fp for a in existing_anchors}

        _ensure_judgment_table(conn)

        # 候補探索: 未マッチ + テキストあり + phash < PHASE4_PHASH_THRESHOLD + sim >= PHASE4_TEXT_SIMILARITY
        candidates: list[tuple[NodeInfo, NodeInfo, float]] = []
        targets = [n for n in session_nodes
                   if n.edge_type == "tap" and n.has_text and n.fp not in matched_session_fps]
        for s in targets:
            if not s.phash:
                continue
            best_m = None
            best_sim = 0.0
            for m in master_nodes:
                if m.fp in matched_master_fps or not m.phash or not m.has_text:
                    continue
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE4_PHASH_THRESHOLD):
                    continue
                sim = _text_similarity(s.text, m.text)
                if sim >= self.PHASE4_TEXT_SIMILARITY and sim > best_sim:
                    best_m = m
                    best_sim = sim
            if best_m:
                candidates.append((s, best_m, best_sim))

        if not candidates:
            logger.info("[AnchorMatcher] Phase 4: 候補なし")
            return [], []

        # キャッシュ確認 (gemini-text 優先、他 model で is_same=1 ならスキップ)
        model_name = "gemini-text"
        uncached: list[tuple[NodeInfo, NodeInfo, float]] = []
        cached_results: dict[tuple[str, str], bool] = {}
        for s, m, sim in candidates:
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_fp = ? AND master_fp = ? AND model = ?",
                (s.fp, m.fp, model_name),
            ).fetchone()
            if row is not None:
                cached_results[(s.fp, m.fp)] = bool(row["is_same"])
            else:
                # P5/P6 の画像判定で既に確定済みならテキスト再送信は不要
                row_any = conn.execute(
                    "SELECT is_same FROM lc_anchor_judgments"
                    " WHERE session_fp = ? AND master_fp = ? AND is_same = 1"
                    " LIMIT 1",
                    (s.fp, m.fp),
                ).fetchone()
                if row_any is not None:
                    cached_results[(s.fp, m.fp)] = True
                else:
                    uncached.append((s, m, sim))

        logger.info(
            "[AnchorMatcher] Phase 4: %d候補 (キャッシュ%d件, Geminiテキスト送信%d件)",
            len(candidates), len(cached_results), len(uncached),
        )

        # Gemini テキスト判定 — エラー結果はキャッシュしない (§17 厳格ルール)
        if uncached:
            results = self._gemini_text_judge(uncached)
            for (s, m, sim), result in zip(uncached, results):
                if result.get("error"):
                    continue  # エラーはキャッシュせずスキップ
                is_same = result["is_same"]
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO lc_anchor_judgments"
                        " (session_fp, master_fp, is_same, prefer, model) VALUES (?, ?, ?, ?, ?)",
                        (s.fp, m.fp, 1 if is_same else 0, "", model_name),
                    )
                except Exception:
                    pass
                cached_results[(s.fp, m.fp)] = is_same
            try:
                conn.commit()
            except Exception:
                pass

        # 結果集計
        new_anchors: list[AnchorMatch] = []
        rejected_candidates: list[tuple] = []
        for s, m, sim in candidates:
            result = cached_results.get((s.fp, m.fp))
            if result is True:
                new_anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=m.fp,
                    master_sort=master_sort_map[m.fp],
                    method="phase4_gemini_text", score=round(sim, 3), phase=4,
                ))
            elif result is False:
                rejected_candidates.append((s, m, sim))
                # result is None (エラーでキャッシュされなかった) はスキップ

        return new_anchors, rejected_candidates

    @staticmethod
    def _gemini_text_judge(
        candidates: list[tuple[NodeInfo, NodeInfo, float]],
    ) -> list[dict]:
        """Gemini にテキストペアの同一画面判定を問い合わせる (画像なし、安価)。

        candidates: [(session_node, master_node, similarity)]
        Returns: [{"is_same": bool, "error": bool}, ...]
            error=True の結果はキャッシュしてはならない。
        """
        import json
        import re

        _error = {"is_same": False, "error": True}
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("[AnchorMatcher] GEMINI_API_KEY 未設定: Phase 4 スキップ")
            return [_error.copy() for _ in candidates]

        try:
            from google import genai
            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.warning("[AnchorMatcher] Gemini 初期化失敗: %s", e)
            return [_error.copy() for _ in candidates]

        model_name = "gemini-2.5-flash-lite"
        CONCURRENCY = 5
        results: list[dict] = [None] * len(candidates)  # type: ignore

        prompt_template = (
            "以下の2つのテキストは、同じモバイルゲームの異なるプレイセッションで、"
            "画面の OCR (文字認識) から抽出されたものです。\n"
            "これらが「同じ画面」から取得されたテキストかどうかを判定してください。\n\n"
            "## 判定基準:\n"
            "- OCR の読み取り誤差（1-2文字の違い、記号の有無）は同じ画面\n"
            "- 同じキャラクターの同じセリフなら同じ画面\n"
            "- 同じ UI 画面で数値（HP、ダメージ、Lv等）だけ異なるのは同じ画面\n"
            "- 異なるキャラクターのセリフは別画面\n"
            "- 同じ UI テンプレートでも内容（クエスト名、アイテム名等）が異なれば別画面\n"
            "- バトル画面で敵や味方の構成が異なれば別画面\n\n"
            "重要: 迷ったら true (同じ画面) と判定してください。\n\n"
            "JSON のみ返してください。\n"
            '{{"is_same": true}}\n\n'
            "テキストA (セッション):\n{text_a}\n\n"
            "テキストB (マスター):\n{text_b}"
        )

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _judge_one(idx: int, s: NodeInfo, m: NodeInfo) -> None:
            try:
                prompt = prompt_template.format(text_a=s.text[:500], text_b=m.text[:500])
                response = client.models.generate_content(
                    model=model_name,
                    contents=[prompt],
                )

                try:
                    from tools.ap.api_usage import record_api_usage, extract_usage_from_response
                    in_tok, out_tok = extract_usage_from_response(response)
                    record_api_usage(model_name, "anchor_text_judgment", in_tok, out_tok)
                except Exception:
                    pass

                raw = (response.text or "").strip()
                if raw.startswith("```"):
                    raw = re.sub(r'^```\w*\n?', '', raw)
                    raw = re.sub(r'\n?```$', '', raw)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        parsed = parsed[0] if parsed else {}
                    is_same = bool(parsed.get("is_same", False))
                except (json.JSONDecodeError, AttributeError):
                    is_same = '"is_same": true' in raw.lower()

                results[idx] = {"is_same": is_same, "error": False}
                logger.info(
                    "[AnchorMatcher] Gemini テキスト判定: s=%s m=%s → same=%s",
                    s.fp[:12], m.fp[:12], is_same,
                )
            except Exception as e:
                logger.warning("[AnchorMatcher] Gemini テキスト判定失敗: s=%s: %s", s.fp[:12], e)
                results[idx] = _error.copy()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [
                pool.submit(_judge_one, idx, s, m)
                for idx, (s, m, _) in enumerate(candidates)
            ]
            for f in as_completed(futures):
                f.result()

        ok = sum(1 for r in results if r and not r.get("error"))
        err = sum(1 for r in results if r and r.get("error"))
        logger.info("[AnchorMatcher] Gemini テキスト判定完了: %d/%d 件 (エラー%d件)",
                    sum(1 for r in results if r and r.get("is_same")), ok, err)

        return [r or _error.copy() for r in results]

    # ─── Phase 5: phash近接 + 画像 Gemini Flash-Lite 判定 ────

    def _phase5_gemini_image(
        self,
        conn: sqlite3.Connection,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
        version_id: int | None = None,
        p4_rejected: list[tuple] | None = None,
    ) -> tuple[list[AnchorMatch], list[AnchorMatch]]:
        """Phase 5: Gemini Flash-Lite 画像判定。

        - P3 確定アンカーの検証（phash のみで確定、テキストなし）
        - P4 棄却の画像再検証（テキストでは判断不可だったペア）
        - 新規候補の発見（phash < PHASE5_PHASH_THRESHOLD）

        Returns:
            (new_anchors, rejected_anchors)
            new_anchors: 新たにマッチしたアンカー (P4棄却復活 + 新規)
            rejected_anchors: 既存アンカーのうち Gemini が棄却したもの
        """
        _hash_distance = self._hash_distance

        matched_session_fps = {a.session_fp for a in existing_anchors}
        matched_master_fps = {a.master_fp for a in existing_anchors}

        # キャッシュテーブル準備
        _ensure_judgment_table(conn)

        # 画像パス取得 (セッション側)
        session_node_map = {n.fp: n for n in session_nodes}
        master_node_map = {n.fp: n for n in master_nodes}

        # session_id を取得
        first_fp = session_nodes[0].fp if session_nodes else ""
        session_id_row = conn.execute(
            "SELECT session_id FROM lc_screens WHERE fingerprint = ? AND is_representative = 1 LIMIT 1",
            (first_fp,),
        ).fetchone()
        session_id_str = session_id_row["session_id"] if session_id_row else ""

        session_img_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT fingerprint, screenshot_path FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id_str,),
        ).fetchall():
            if row["screenshot_path"]:
                session_img_map[row["fingerprint"]] = row["screenshot_path"]

        master_img_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT m.master_fp, s.screenshot_path"
            " FROM lc_master_nodes m"
            " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
            " WHERE m.version_id = ?",
            (version_id or 1,),
        ).fetchall():
            if row["screenshot_path"]:
                master_img_map[row["master_fp"]] = row["screenshot_path"]

        # --- Step 1: 既存アンカーの検証 (P3 のみ対象) ---
        # P1/P2 はテキストベースのマッチなので信頼度が高く、画像検証は不要
        # P4 はテキスト Gemini で確定済みなので画像再検証は不要
        # P3 (テキスト空 + phash のみ) のアンカーだけ画像で検証する
        model_name = "gemini-2.5-flash-lite"
        verify_pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]] = []
        verify_cached: dict[tuple[str, str], bool] = {}
        anchors_to_verify = [a for a in existing_anchors if a.phase == 3]
        for a in existing_anchors:
            if a.phase != 3:
                # P1/P2: テキストベースのマッチ → 検証不要
                verify_cached[(a.session_fp, a.master_fp)] = True
                continue
            s_node = session_node_map.get(a.session_fp)
            m_node = master_node_map.get(a.master_fp)
            if not s_node or not m_node:
                continue
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_fp = ? AND master_fp = ? AND model = ?",
                (a.session_fp, a.master_fp, model_name),
            ).fetchone()
            if row is not None:
                verify_cached[(a.session_fp, a.master_fp)] = bool(row["is_same"])
            else:
                s_img = session_img_map.get(a.session_fp, "")
                m_img = master_img_map.get(a.master_fp, "")
                if s_img and m_img:
                    verify_pairs.append((s_node, m_node, a.score, s_img, m_img))
                else:
                    # 画像なし → 検証スキップ（既存判定を信頼）
                    verify_cached[(a.session_fp, a.master_fp)] = True

        # --- Step 2: 未マッチノードの新規発見候補 (phash < PHASE5_PHASH_THRESHOLD) ---
        new_candidates: list[tuple[NodeInfo, NodeInfo, float]] = []
        targets = [n for n in session_nodes
                   if n.edge_type == "tap" and n.has_text and n.fp not in matched_session_fps]
        for s in targets:
            if not s.phash:
                continue
            best_m = None
            best_sim = 0.0
            for m in master_nodes:
                if m.fp in matched_master_fps or not m.phash or not m.has_text:
                    continue
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE5_PHASH_THRESHOLD):
                    continue
                sim = _text_similarity(s.text, m.text)
                if sim >= self.PHASE5_TEXT_SIMILARITY and sim > best_sim:
                    best_m = m
                    best_sim = sim
            if best_m:
                new_candidates.append((s, best_m, best_sim))

        # --- Step 2.5: P4 棄却の画像再検証 ---
        p4_retry_count = 0
        new_candidate_sfps = {s.fp for s, _, _ in new_candidates}
        if p4_rejected:
            for s, m, sim in p4_rejected:
                if (s.fp not in matched_session_fps and m.fp not in matched_master_fps
                        and s.fp not in new_candidate_sfps):
                    new_candidates.append((s, m, sim))
                    new_candidate_sfps.add(s.fp)
                    p4_retry_count += 1

        discover_pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]] = []
        discover_cached: dict[tuple[str, str], bool] = {}
        for s, m, sim in new_candidates:
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_fp = ? AND master_fp = ? AND model = ?",
                (s.fp, m.fp, model_name),
            ).fetchone()
            if row is not None:
                discover_cached[(s.fp, m.fp)] = bool(row["is_same"])
            else:
                s_img = session_img_map.get(s.fp, "")
                m_img = master_img_map.get(m.fp, "")
                discover_pairs.append((s, m, sim, s_img, m_img))

        # --- Gemini に一括送信 ---
        all_uncached = verify_pairs + discover_pairs
        total_new = len(new_candidates) - p4_retry_count
        logger.info(
            "[AnchorMatcher] Phase 5: 検証%d件(P3) + P4棄却再検証%d件 + 新規%d件 (キャッシュ%d件, Gemini送信%d件)",
            len(anchors_to_verify), p4_retry_count, total_new,
            len(new_candidates) + len(anchors_to_verify) - len(all_uncached), len(all_uncached),
        )

        if not all_uncached and not verify_cached and not discover_cached:
            return [], []

        if all_uncached:
            gemini_results = self._gemini_batch_judge(all_uncached, model=model_name)
            for (s, m, sim, _, _), result in zip(all_uncached, gemini_results):
                if result.get("error"):
                    continue  # エラーはキャッシュせずスキップ (§17 厳格ルール)
                is_same = result["is_same"]
                prefer = result.get("prefer", "")
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO lc_anchor_judgments"
                        " (session_fp, master_fp, is_same, prefer, model) VALUES (?, ?, ?, ?, ?)",
                        (s.fp, m.fp, 1 if is_same else 0, prefer, model_name),
                    )
                except Exception:
                    pass
                # verify_pairs か discover_pairs かで振り分け
                if (s.fp, m.fp) not in verify_cached:
                    verify_cached[(s.fp, m.fp)] = is_same
                if (s.fp, m.fp) not in discover_cached:
                    discover_cached[(s.fp, m.fp)] = is_same
            try:
                conn.commit()
            except Exception:
                pass

        # --- 結果集計 ---
        # 既存アンカーの検証結果
        rejected: list[AnchorMatch] = []
        verified_fps: set[str] = set()
        for a in existing_anchors:
            is_same = verify_cached.get((a.session_fp, a.master_fp))
            if is_same is False:
                rejected.append(a)
                logger.info("[AnchorMatcher] Phase 5 検証棄却: %s → %s (%s)",
                            a.session_fp[:12], a.master_fp[:12], a.method)
            elif is_same is True and a.phase == 3:
                # P3 アンカーが P5 で検証通過 → method を P5 に更新
                a.method = "phase5_gemini_image"
                a.phase = 5
                verified_fps.add(a.session_fp)
        if verified_fps:
            logger.info("[AnchorMatcher] Phase 5 検証通過 (P3→P5): %d件", len(verified_fps))

        # 新規アンカー
        new_anchors: list[AnchorMatch] = []
        for s, m, sim in new_candidates:
            if discover_cached.get((s.fp, m.fp), False):
                new_anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=m.fp,
                    master_sort=master_sort_map[m.fp],
                    method="phase5_gemini_image", score=round(sim, 3), phase=5,
                ))

        return new_anchors, rejected

    @staticmethod
    def _gemini_batch_judge(
        pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]],
        model: str = "gemini-2.5-flash-lite",
    ) -> list[dict]:
        """Gemini に画像ペアの同一画面判定を問い合わせる (1ペアずつ並列)。

        pairs: [(session_node, master_node, similarity, session_img_path, master_img_path)]
        model: 使用する Gemini モデル名
        Returns: [{"is_same": bool, "prefer": str, "error": bool}, ...]
            error=True の結果はキャッシュしてはならない。
        """
        import json
        import re
        import time
        from pathlib import Path

        _error = {"is_same": False, "prefer": "", "error": True}
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("[AnchorMatcher] GEMINI_API_KEY 未設定: スキップ")
            return [_error.copy() for _ in pairs]

        try:
            from google import genai
            from google.genai import types
            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.warning("[AnchorMatcher] Gemini 初期化失敗: %s", e)
            return [_error.copy() for _ in pairs]

        CONCURRENCY = 5  # 同時並列リクエスト数
        results: list[dict] = []

        prompt_text = (
            "以下の2枚のスクリーンショットは同じモバイルゲームの異なるプレイセッションから取得したものです。\n"
            "これらが「同じ種類の画面」であるかどうかを判定してください。\n\n"
            "## 「同じ画面」と判定すべきケース:\n"
            "- 同じUI画面（メニュー、設定、規約画面等）の異なるタイミングでのキャプチャ\n"
            "- 同じ会話シーンで同じキャラクターが話している\n"
            "- ボタンの選択状態、スクロール位置、数値（HP、ダメージ等）が異なっても画面の種類が同じ\n"
            "- テキストが多少異なっていても、画面のレイアウトと目的が同じ\n"
            "- 画質や明るさが若干異なる同じ画面\n"
            "- OCR テキストに揺れがあるだけの同じ画面（例: 「きっぱり」→「っぱり」）\n\n"
            "## 「異なる画面」と判定すべきケース:\n"
            "- 完全に異なるシーン（バトル vs メニュー等）\n"
            "- 異なるメニュー画面（ショップ vs ガチャ等）\n"
            "- 異なるキャラクターが話している会話シーン\n"
            "- バトル画面でキャラクター編成（下部のアイコン列）が異なる場合\n"
            "- 画面が見切れている・不完全なキャプチャ（片方が正常で片方が見切れている場合）\n\n"
            "重要: 迷ったら true (同じ画面) と判定してください。\n"
            "false の誤りは取り返しがつきませんが、true の誤りは人間が後から修正できます。\n\n"
            "JSON のみ返してください。説明は不要です。\n"
            '{"is_same": true, "prefer": "A"}\n\n'
            "- is_same: 同じ画面なら true、異なる画面なら false\n"
            "- prefer: 同じ画面の場合、テキストがより正確な方。A=セッション、B=マスター。同等なら A。"
        )

        # 画像なしペアを先に処理
        valid_pairs: list[tuple[int, NodeInfo, NodeInfo, float, str, str]] = []
        for idx, (s, m, sim, s_img, m_img) in enumerate(pairs):
            if not s_img or not m_img or not Path(s_img).exists() or not Path(m_img).exists():
                logger.warning("[AnchorMatcher] 画像なし: s=%s m=%s", s.fp[:12], m.fp[:12])
                results.append(_error.copy())
            else:
                valid_pairs.append((idx, s, m, sim, s_img, m_img))
                results.append(None)  # placeholder

        if not valid_pairs:
            return [r or _error.copy() for r in results]

        # スレッド並列リクエスト (1ペア=1リクエスト、同時N件)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _judge_one(orig_idx: int, s: NodeInfo, m: NodeInfo,
                       s_img: str, m_img: str) -> None:
            try:
                s_mime = "image/webp" if s_img.endswith(".webp") else "image/png"
                m_mime = "image/webp" if m_img.endswith(".webp") else "image/png"
                with open(s_img, "rb") as f:
                    s_data = f.read()
                with open(m_img, "rb") as f:
                    m_data = f.read()

                response = client.models.generate_content(
                    model=model,
                    contents=[
                        "画像A (セッション):",
                        genai.types.Part.from_bytes(data=s_data, mime_type=s_mime),
                        "画像B (マスター):",
                        genai.types.Part.from_bytes(data=m_data, mime_type=m_mime),
                        prompt_text,
                    ],
                )

                # API 使用量記録
                try:
                    from tools.ap.api_usage import record_api_usage, extract_usage_from_response
                    in_tok, out_tok = extract_usage_from_response(response)
                    record_api_usage(model, "anchor_judgment", in_tok, out_tok)
                except Exception:
                    pass

                raw = (response.text or "").strip()
                if raw.startswith("```"):
                    raw = re.sub(r'^```\w*\n?', '', raw)
                    raw = re.sub(r'\n?```$', '', raw)
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        parsed = parsed[0] if parsed else {}
                    is_same = bool(parsed.get("is_same", False))
                    prefer = str(parsed.get("prefer", "")) or ""
                except (json.JSONDecodeError, AttributeError):
                    is_same = raw.lower().startswith("true") or '"is_same": true' in raw.lower()
                    prefer = ""

                results[orig_idx] = {"is_same": is_same, "prefer": prefer, "error": False}
                logger.info(
                    "[AnchorMatcher] Gemini [%s]: s=%s m=%s → same=%s prefer=%s",
                    model, s.fp[:12], m.fp[:12], is_same, prefer,
                )
            except Exception as e:
                logger.warning("[AnchorMatcher] Gemini [%s] 判定失敗: s=%s: %s", model, s.fp[:12], e)
                results[orig_idx] = _error.copy()

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = [
                pool.submit(_judge_one, orig_idx, s, m, s_img, m_img)
                for orig_idx, s, m, _, s_img, m_img in valid_pairs
            ]
            for f in as_completed(futures):
                f.result()  # 例外があれば再 raise

        logger.info("[AnchorMatcher] Gemini [%s] 判定完了: %d/%d 件",
                    model, sum(1 for r in results if r and r.get("is_same")), len(pairs))

        return [r or _default.copy() for r in results]

    # ─── Phase 6: Gemini Flash (phash 8-20 + P5 棄却再審査) ────

    def _phase6_gemini_flash(
        self,
        conn: sqlite3.Connection,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
        p5_rejected: list[AnchorMatch],
        version_id: int | None = None,
    ) -> tuple[list[AnchorMatch], list[AnchorMatch]]:
        """Phase 6: Gemini Flash で P5 より広い範囲の候補を判定 + P5 棄却の再審査。

        Returns:
            (new_anchors, final_rejected)
            new_anchors: 新たにマッチしたアンカー (P6 新規 + P5 棄却復活)
            final_rejected: P5 でも棄却されたもの (最終棄却)
        """
        _hash_distance = self._hash_distance

        matched_session_fps = {a.session_fp for a in existing_anchors}
        matched_master_fps = {a.master_fp for a in existing_anchors}

        _ensure_judgment_table(conn)

        session_node_map = {n.fp: n for n in session_nodes}
        master_node_map = {n.fp: n for n in master_nodes}

        # session_id を取得
        first_fp = session_nodes[0].fp if session_nodes else ""
        session_id_row = conn.execute(
            "SELECT session_id FROM lc_screens WHERE fingerprint = ? AND is_representative = 1 LIMIT 1",
            (first_fp,),
        ).fetchone()
        session_id_str = session_id_row["session_id"] if session_id_row else ""

        session_img_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT fingerprint, screenshot_path FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id_str,),
        ).fetchall():
            if row["screenshot_path"]:
                session_img_map[row["fingerprint"]] = row["screenshot_path"]

        master_img_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT m.master_fp, s.screenshot_path"
            " FROM lc_master_nodes m"
            " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
            " WHERE m.version_id = ?",
            (version_id or 1,),
        ).fetchall():
            if row["screenshot_path"]:
                master_img_map[row["master_fp"]] = row["screenshot_path"]

        model_name = "gemini-2.5-flash"

        # --- Step 1: P4 棄却の再審査 ---
        retry_pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]] = []
        retry_cached: dict[tuple[str, str], bool] = {}
        for a in p5_rejected:
            s_node = session_node_map.get(a.session_fp)
            m_node = master_node_map.get(a.master_fp)
            if not s_node or not m_node:
                continue
            # flash モデルでのキャッシュを確認 (model='gemini-2.5-flash' のみ)
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_fp = ? AND master_fp = ? AND model = ?",
                (a.session_fp, a.master_fp, model_name),
            ).fetchone()
            if row is not None:
                retry_cached[(a.session_fp, a.master_fp)] = bool(row["is_same"])
            else:
                s_img = session_img_map.get(a.session_fp, "")
                m_img = master_img_map.get(a.master_fp, "")
                if s_img and m_img:
                    retry_pairs.append((s_node, m_node, a.score, s_img, m_img))
                else:
                    retry_cached[(a.session_fp, a.master_fp)] = False

        # --- Step 2: 未マッチノードの新規候補 (phash 8-20) ---
        new_candidates: list[tuple[NodeInfo, NodeInfo, float]] = []
        targets = [n for n in session_nodes
                   if n.edge_type == "tap" and n.has_text and n.fp not in matched_session_fps]
        for s in targets:
            if not s.phash:
                continue
            best_m = None
            best_sim = 0.0
            for m in master_nodes:
                if m.fp in matched_master_fps or not m.phash or not m.has_text:
                    continue
                dist = _hash_distance(s.phash, m.phash)
                if dist < self._th(self.PHASE6_PHASH_MIN) or dist >= self._th(self.PHASE6_PHASH_MAX):
                    continue
                sim = _text_similarity(s.text, m.text)
                if sim >= self.PHASE6_TEXT_SIMILARITY and sim > best_sim:
                    best_m = m
                    best_sim = sim
            if best_m:
                new_candidates.append((s, best_m, best_sim))

        discover_pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]] = []
        discover_cached: dict[tuple[str, str], bool] = {}
        for s, m, sim in new_candidates:
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_fp = ? AND master_fp = ? AND model = ?",
                (s.fp, m.fp, model_name),
            ).fetchone()
            if row is not None:
                discover_cached[(s.fp, m.fp)] = bool(row["is_same"])
            else:
                s_img = session_img_map.get(s.fp, "")
                m_img = master_img_map.get(m.fp, "")
                discover_pairs.append((s, m, sim, s_img, m_img))

        # --- Gemini Flash に送信 ---
        all_uncached = retry_pairs + discover_pairs
        total_pairs = len(p5_rejected) + len(new_candidates)
        cached_count = total_pairs - len(all_uncached)
        logger.info(
            "[AnchorMatcher] Phase 6: 再審査%d件 + 新規%d件 = %d件 (キャッシュ%d件, Gemini送信%d件)",
            len(p5_rejected), len(new_candidates), total_pairs, cached_count, len(all_uncached),
        )

        if not all_uncached and not retry_cached and not discover_cached:
            return [], list(p5_rejected)

        if all_uncached:
            gemini_results = self._gemini_batch_judge(all_uncached, model=model_name)
            for (s, m, sim, _, _), result in zip(all_uncached, gemini_results):
                if result.get("error"):
                    continue  # エラーはキャッシュせずスキップ (§17 厳格ルール)
                is_same = result["is_same"]
                prefer = result.get("prefer", "")
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO lc_anchor_judgments"
                        " (session_fp, master_fp, is_same, prefer, model) VALUES (?, ?, ?, ?, ?)",
                        (s.fp, m.fp, 1 if is_same else 0, prefer, model_name),
                    )
                except Exception:
                    pass
                if (s.fp, m.fp) not in retry_cached:
                    retry_cached[(s.fp, m.fp)] = is_same
                if (s.fp, m.fp) not in discover_cached:
                    discover_cached[(s.fp, m.fp)] = is_same
            try:
                conn.commit()
            except Exception:
                pass

        # --- 結果集計 ---
        new_anchors: list[AnchorMatch] = []
        final_rejected: list[AnchorMatch] = []

        # P4 棄却の再審査結果
        for a in p5_rejected:
            is_same = retry_cached.get((a.session_fp, a.master_fp))
            if is_same:
                # flash が同一画面と判定 → 復活 (元の phase を維持、method は P5 に)
                new_anchors.append(AnchorMatch(
                    session_fp=a.session_fp, master_fp=a.master_fp,
                    master_sort=a.master_sort,
                    method="phase6_gemini_flash", score=a.score, phase=6,
                ))
                logger.info("[AnchorMatcher] Phase 6 復活: %s → %s (元 %s)",
                            a.session_fp[:12], a.master_fp[:12], a.method)
            else:
                final_rejected.append(a)
                logger.info("[AnchorMatcher] Phase 6 最終棄却: %s → %s",
                            a.session_fp[:12], a.master_fp[:12])

        # 新規候補
        for s, m, sim in new_candidates:
            if discover_cached.get((s.fp, m.fp), False):
                new_anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=m.fp,
                    master_sort=master_sort_map[m.fp],
                    method="phase6_gemini_flash", score=round(sim, 3), phase=6,
                ))

        return new_anchors, final_rejected

    # ─── Phase 2: auto + テキストあり ─────────────────

    def _phase2_auto_text(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
    ) -> list[AnchorMatch]:
        """Phase 2: auto + テキストありノードを、アンカー範囲制限付きでマッチ。
        完全一致/前方一致 → あいまい一致 (sim>=0.9, phash<20, 候補1件) の順。
        """
        _hash_distance = self._hash_distance

        matched_master_fps = {a.master_fp for a in existing_anchors}
        matched_session_fps = {a.session_fp for a in existing_anchors}

        sorted_anchors = sorted(existing_anchors, key=lambda a: self._session_rank(a, session_nodes))

        targets = [n for n in session_nodes
                   if n.edge_type == "auto" and n.has_text and n.fp not in matched_session_fps]

        master_by_text: dict[str, list[NodeInfo]] = {}
        for m in master_nodes:
            if m.has_text:
                master_by_text.setdefault(m.text, []).append(m)

        anchors: list[AnchorMatch] = []
        p3_matched_session: set[str] = set()
        p3_matched_master: set[str] = set()

        # --- Pass 1: 完全一致 / 前方一致 ---
        for s in targets:
            sort_min, sort_max = self._get_sort_range(s, session_nodes, sorted_anchors, master_sort_map)

            candidates = []
            for m in master_by_text.get(s.text, []):
                if m.fp in matched_master_fps or m.fp in p3_matched_master:
                    continue
                m_sort = master_sort_map.get(m.fp, -1)
                if sort_min <= m_sort <= sort_max:
                    candidates.append(m)

            if not candidates and len(s.text) >= 5:
                for text, ms in master_by_text.items():
                    if not text:
                        continue
                    shorter, longer = (s.text, text) if len(s.text) <= len(text) else (text, s.text)
                    if len(shorter) >= 5 and longer.startswith(shorter):
                        for m in ms:
                            if m.fp in matched_master_fps or m.fp in p3_matched_master:
                                continue
                            m_sort = master_sort_map.get(m.fp, -1)
                            if sort_min <= m_sort <= sort_max:
                                candidates.append(m)

            if len(candidates) != 1:
                continue

            m = candidates[0]
            if s.phash and m.phash:
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE1_PHASH_THRESHOLD):
                    continue

            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase2_auto_text", score=1.0, phase=2,
            ))
            p3_matched_session.add(s.fp)
            p3_matched_master.add(m.fp)

        # --- Pass 2: あいまい一致 (sim>=0.9, phash<20, 候補1件) ---
        all_matched_master = matched_master_fps | p3_matched_master
        all_matched_session = matched_session_fps | p3_matched_session
        # あいまい一致用に sorted_anchors を更新
        updated_anchors = existing_anchors + anchors
        sorted_anchors_updated = sorted(updated_anchors, key=lambda a: self._session_rank(a, session_nodes))

        fuzzy_targets = [n for n in targets if n.fp not in all_matched_session]
        for s in fuzzy_targets:
            if not s.phash:
                continue
            sort_min, sort_max = self._get_sort_range(s, session_nodes, sorted_anchors_updated, master_sort_map)

            best_m = None
            best_sim = 0.0
            candidate_count = 0
            for m in master_nodes:
                if m.fp in all_matched_master or not m.phash or not m.has_text:
                    continue
                m_sort = master_sort_map.get(m.fp, -1)
                if not (sort_min <= m_sort <= sort_max):
                    continue
                dist = _hash_distance(s.phash, m.phash)
                if dist >= self._th(self.PHASE2_FUZZY_PHASH_THRESHOLD):
                    continue
                sim = _text_similarity(s.text, m.text)
                if sim >= self._fuzzy_sim_threshold(len(s.text)):
                    candidate_count += 1
                    if sim > best_sim:
                        best_m = m
                        best_sim = sim
            if candidate_count == 1 and best_m:
                anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=best_m.fp,
                    master_sort=master_sort_map[best_m.fp],
                    method="phase2_auto_text", score=round(best_sim, 3), phase=2,
                ))
                all_matched_master.add(best_m.fp)

        return anchors

    # ─── Phase 3: tap + テキスト空 ────────────────────

    def _phase3_tap_phash(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
    ) -> list[AnchorMatch]:
        """tap + テキスト空ノードを、前後アンカー必須 + phash でマッチ。"""
        _hash_distance = self._hash_distance

        matched_master_fps = {a.master_fp for a in existing_anchors}
        matched_session_fps = {a.session_fp for a in existing_anchors}

        sorted_anchors = sorted(existing_anchors, key=lambda a: self._session_rank(a, session_nodes))

        targets = [n for n in session_nodes
                   if n.edge_type == "tap" and not n.has_text and n.fp not in matched_session_fps]

        anchors: list[AnchorMatch] = []

        for s in targets:
            # 前後のアンカーが両方必要
            prev_anchor, next_anchor = self._get_surrounding_anchors(s, session_nodes, sorted_anchors)
            if prev_anchor is None or next_anchor is None:
                continue  # 片側のみ → スキップ

            sort_min = prev_anchor.master_sort
            sort_max = next_anchor.master_sort

            # 範囲内の phash 近接ノード
            candidates = []
            for m in master_nodes:
                if m.fp in matched_master_fps:
                    continue
                m_sort = master_sort_map.get(m.fp, -1)
                if m_sort < sort_min or m_sort > sort_max:
                    continue
                if s.phash and m.phash:
                    dist = _hash_distance(s.phash, m.phash)
                    if dist < self._th(self.PHASE3_PHASH_THRESHOLD):
                        candidates.append((m, dist))

            if len(candidates) != 1:
                continue

            m, dist = candidates[0]
            score = max(0.0, 1.0 - dist / 64.0)
            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase3_tap_phash", score=score, phase=3,
            ))
            matched_master_fps.add(m.fp)

        return anchors

    # ─── 時系列整合性チェック ──────────────────────────

    def _verify_consistency(
        self, anchors: list[AnchorMatch], phase_label: str = "",
    ) -> tuple[list[AnchorMatch], list[tuple[AnchorMatch, str]]]:
        """時系列整合性チェック。矛盾するアンカーを LIS で除去。

        Returns:
            (kept, discarded) — discarded は (anchor, reason) のリスト
        """
        if len(anchors) <= 1:
            return anchors, []

        master_sorts = [a.master_sort for a in anchors]
        lis_indices = self._longest_increasing_subsequence(master_sorts)
        lis_set = set(lis_indices)

        removed = len(anchors) - len(lis_indices)
        if removed > 0:
            logger.warning("[AnchorMatcher] 矛盾検出: %d アンカーを破棄 (LIS で %d 保持)",
                           removed, len(lis_indices))

        kept = [anchors[i] for i in lis_indices]
        reason = "他のアンカーと順序が競合（マッチ自体は正しい可能性あり）"
        discarded = [(anchors[i], reason) for i in range(len(anchors)) if i not in lis_set]
        return kept, discarded

    @staticmethod
    def _longest_increasing_subsequence(seq: list[int]) -> list[int]:
        """最長増加部分列のインデックスを返す。"""
        if not seq:
            return []
        n = len(seq)
        # dp[i] = seq[i] を末尾とする LIS の長さ
        dp = [1] * n
        parent = [-1] * n
        for i in range(1, n):
            for j in range(i):
                if seq[j] < seq[i] and dp[j] + 1 > dp[i]:
                    dp[i] = dp[j] + 1
                    parent[i] = j

        # 最長の末尾を見つけてバックトラック
        max_len = max(dp)
        idx = dp.index(max_len)
        result = []
        while idx != -1:
            result.append(idx)
            idx = parent[idx]
        return list(reversed(result))

    # ─── ヘルパー ─────────────────────────────────────

    @staticmethod
    def _session_rank(anchor: AnchorMatch, session_nodes: list[NodeInfo]) -> int:
        """アンカーのセッション内 time_rank を取得。"""
        for n in session_nodes:
            if n.fp == anchor.session_fp:
                return n.time_rank
        return 0

    def _get_sort_range(
        self,
        node: NodeInfo,
        session_nodes: list[NodeInfo],
        sorted_anchors: list[AnchorMatch],
        master_sort_map: dict[str, int],
    ) -> tuple[int, int]:
        """ノードの前後アンカーから、マスター側の sort_order 範囲を取得。"""
        max_sort = max(master_sort_map.values()) if master_sort_map else 0

        prev_anchor, next_anchor = self._get_surrounding_anchors(node, session_nodes, sorted_anchors)

        sort_min = prev_anchor.master_sort if prev_anchor else 0
        sort_max = next_anchor.master_sort if next_anchor else max_sort

        return sort_min, sort_max

    def _get_surrounding_anchors(
        self,
        node: NodeInfo,
        session_nodes: list[NodeInfo],
        sorted_anchors: list[AnchorMatch],
    ) -> tuple[Optional[AnchorMatch], Optional[AnchorMatch]]:
        """ノードの前後のアンカーを取得。"""
        anchor_ranks = {self._session_rank(a, session_nodes): a for a in sorted_anchors}

        prev_anchor = None
        next_anchor = None
        for rank in sorted(anchor_ranks.keys()):
            if rank < node.time_rank:
                prev_anchor = anchor_ranks[rank]
            elif rank > node.time_rank:
                next_anchor = anchor_ranks[rank]
                break

        return prev_anchor, next_anchor
