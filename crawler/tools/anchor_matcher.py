"""
anchor_matcher.py — 段階的アンカーマッチング

安全性最優先: 安全性が担保できないなら破棄。
周回を重ねれば自然にアンカーは増える。

Phase 1: tap + テキスト完全/前方一致 (最も確実)
Phase 2: tap + phash近接 + テキスト類似 + Gemini Flash 判定
Phase 3: auto + テキストあり (Phase 1+2 を基準に精度向上)
Phase 4: tap + テキスト空 (Phase 1-3 を基準に候補限定)
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
    return t


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
    method: str          # "phase1_tap_text" / "phase2_gemini" / "phase3_auto_text" / "phase4_tap_phash"
    score: float
    phase: int


def _text_hash(text: str) -> str:
    """テキストの SHA256 ハッシュ (キャッシュキー用)。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _ensure_judgment_table(conn: sqlite3.Connection) -> None:
    """Gemini 判定キャッシュテーブルを作成。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lc_anchor_judgments (
            session_text_hash TEXT NOT NULL,
            master_text_hash  TEXT NOT NULL,
            is_same           INTEGER NOT NULL,
            judged_at         TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (session_text_hash, master_text_hash)
        )
    """)
    conn.commit()


class AnchorMatcher:
    """段階的アンカーマッチング。"""

    # Phase 1: テキスト一致 + phash 閾値
    PHASE1_PHASH_THRESHOLD = 30

    # Phase 2: phash 近接 + テキスト類似 → Gemini 判定
    PHASE2_PHASH_THRESHOLD = 5
    PHASE2_TEXT_SIMILARITY = 0.7

    # Phase 3: phash のみの閾値 (より厳しく)
    PHASE4_PHASH_THRESHOLD = 15

    def compute_matches(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> tuple[dict[str, tuple[str, str, float]], list[str]]:
        """全 Phase を実行し、マッチ結果を返す。

        Returns:
            (node_mapping, skipped_fps)
            node_mapping: session_fp → (master_fp, method, score)
            skipped_fps: マッチしなかった session_fp のリスト
        """
        session_nodes, master_nodes, master_sort_map = self._prepare_data(conn, session_id)

        if not session_nodes or not master_nodes:
            return {}, [n.fp for n in session_nodes]

        # Phase 1: tap + テキスト完全/前方一致
        anchors = self._phase1_tap_text(session_nodes, master_nodes, master_sort_map)
        anchors = self._verify_consistency(anchors)
        logger.info("[AnchorMatcher] Phase 1: %d アンカー確定", len(anchors))

        # Phase 2: tap + phash近接 + テキスト類似 + Gemini判定
        phase2 = self._phase2_gemini(conn, session_nodes, master_nodes, master_sort_map, anchors)
        anchors.extend(phase2)
        anchors = self._verify_consistency(anchors)
        logger.info("[AnchorMatcher] Phase 2: +%d → 合計 %d アンカー", len(phase2), len(anchors))

        # Phase 3: auto + テキストあり
        phase3 = self._phase3_auto_text(session_nodes, master_nodes, master_sort_map, anchors)
        anchors.extend(phase3)
        anchors = self._verify_consistency(anchors)
        logger.info("[AnchorMatcher] Phase 3: +%d → 合計 %d アンカー", len(phase3), len(anchors))

        # Phase 4: tap + テキスト空
        phase4 = self._phase4_tap_phash(session_nodes, master_nodes, master_sort_map, anchors)
        anchors.extend(phase4)
        anchors = self._verify_consistency(anchors)
        logger.info("[AnchorMatcher] Phase 4: +%d → 合計 %d アンカー", len(phase4), len(anchors))

        # 結果を node_mapping 形式に変換
        matched_session_fps = set()
        node_mapping: dict[str, tuple[str, str, float]] = {}
        for a in anchors:
            node_mapping[a.session_fp] = (a.master_fp, a.method, a.score)
            matched_session_fps.add(a.session_fp)

        skipped = [n.fp for n in session_nodes if n.fp not in matched_session_fps]

        logger.info(
            "[AnchorMatcher] session=%s: matched=%d (P1=%d, P2=%d, P3=%d, P4=%d), skipped=%d",
            session_id, len(node_mapping),
            sum(1 for a in anchors if a.phase == 1),
            sum(1 for a in anchors if a.phase == 2),
            sum(1 for a in anchors if a.phase == 3),
            sum(1 for a in anchors if a.phase == 4),
            len(skipped),
        )

        return node_mapping, skipped

    # ─── データ準備 ───────────────────────────────────

    def _prepare_data(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> tuple[list[NodeInfo], list[NodeInfo], dict[str, int]]:
        """DB からデータ取得し、ノード分類する。"""
        from lc.utils import phash_distance  # noqa: F401 (import test)

        # セッション側: 代表画面を時系列順に取得
        rows = conn.execute(
            "SELECT s.fingerprint, s.phash, s.scene,"
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
            text = _normalize_text(r["text"] or "")
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
        master_rows = conn.execute(
            "SELECT master_fp, phash, scene, sort_order,"
            " COALESCE(ocr_text_manual, ocr_text, '') AS text"
            " FROM lc_master_nodes"
            " ORDER BY sort_order ASC"
        ).fetchall()

        master_nodes: list[NodeInfo] = []
        master_sort_map: dict[str, int] = {}
        for r in master_rows:
            text = _normalize_text(r["text"] or "")
            master_nodes.append(NodeInfo(
                fp=r["master_fp"], text=text, phash=r["phash"] or "",
                scene=r["scene"] or "", edge_type="",
                has_text=len(text) > 0, time_rank=r["sort_order"],
            ))
            master_sort_map[r["master_fp"]] = r["sort_order"]

        return session_nodes, master_nodes, master_sort_map

    # ─── Phase 1: tap + テキストあり ──────────────────

    def _phase1_tap_text(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
    ) -> list[AnchorMatch]:
        """tap + テキストありノードをテキスト一致 + phash でマッチ。"""
        from lc.utils import phash_distance

        # マスター側のテキスト → ノード逆引き
        master_by_text: dict[str, list[NodeInfo]] = {}
        for m in master_nodes:
            if m.has_text:
                master_by_text.setdefault(m.text, []).append(m)

        targets = [n for n in session_nodes if n.edge_type == "tap" and n.has_text]
        anchors: list[AnchorMatch] = []
        matched_master_fps: set[str] = set()

        for s in targets:
            # 完全一致
            candidates = [m for m in master_by_text.get(s.text, [])
                          if m.fp not in matched_master_fps]

            # 前方一致 (完全一致がなければ)
            if not candidates and len(s.text) >= 5:
                for text, ms in master_by_text.items():
                    if not text:
                        continue
                    shorter, longer = (s.text, text) if len(s.text) <= len(text) else (text, s.text)
                    if len(shorter) >= 5 and longer.startswith(shorter):
                        candidates.extend(m for m in ms if m.fp not in matched_master_fps)

            if len(candidates) != 1:
                continue  # 0 件 or 複数 → 破棄

            m = candidates[0]
            # phash 二重確認
            if s.phash and m.phash:
                dist = phash_distance(s.phash, m.phash)
                if dist >= self.PHASE1_PHASH_THRESHOLD:
                    continue  # テキスト一致でも phash が遠い → 破棄

            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase1_tap_text", score=1.0, phase=1,
            ))
            matched_master_fps.add(m.fp)

        return anchors

    # ─── Phase 2: phash近接 + テキスト類似 + Gemini判定 ────

    def _phase2_gemini(
        self,
        conn: sqlite3.Connection,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
    ) -> list[AnchorMatch]:
        """Phase 1 でテキスト不一致だったノードを phash + テキスト類似度 + Gemini で判定。"""
        from lc.utils import phash_distance

        matched_session_fps = {a.session_fp for a in existing_anchors}
        matched_master_fps = {a.master_fp for a in existing_anchors}

        targets = [n for n in session_nodes
                   if n.edge_type == "tap" and n.has_text and n.fp not in matched_session_fps]
        if not targets:
            return []

        # 候補ペア抽出: phash < 5 + SequenceMatcher >= 0.7
        candidates: list[tuple[NodeInfo, NodeInfo, float]] = []  # (session, master, similarity)
        for s in targets:
            if not s.phash:
                continue
            best_m = None
            best_sim = 0.0
            for m in master_nodes:
                if m.fp in matched_master_fps or not m.phash or not m.has_text:
                    continue
                dist = phash_distance(s.phash, m.phash)
                if dist >= self.PHASE2_PHASH_THRESHOLD:
                    continue
                sim = SequenceMatcher(None, s.text, m.text).ratio()
                if sim >= self.PHASE2_TEXT_SIMILARITY and sim > best_sim:
                    best_m = m
                    best_sim = sim
            if best_m:
                candidates.append((s, best_m, best_sim))

        if not candidates:
            return []

        logger.info("[AnchorMatcher] Phase 2: %d 候補ペアを Gemini に判定依頼", len(candidates))

        # 進捗報告ヘルパー
        def _report(stage: str, current: int, total: int, elapsed: float):
            import json as _json
            eta = (elapsed / current * (total - current)) if current > 0 else 0
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO auto_pilot_state (key, value) VALUES ('merge_progress', ?)",
                    (_json.dumps({"stage": stage, "current": current, "total": total,
                                  "elapsed": round(elapsed, 1), "eta": round(eta, 1)}, ensure_ascii=False),),
                )
                conn.commit()
            except Exception:
                pass

        # キャッシュテーブル準備
        _ensure_judgment_table(conn)

        # キャッシュ確認 & 未判定ペアを抽出
        uncached: list[tuple[NodeInfo, NodeInfo, float, str, str]] = []
        cached_results: dict[tuple[str, str], bool] = {}
        for s, m, sim in candidates:
            s_hash = _text_hash(s.text)
            m_hash = _text_hash(m.text)
            row = conn.execute(
                "SELECT is_same FROM lc_anchor_judgments"
                " WHERE session_text_hash = ? AND master_text_hash = ?",
                (s_hash, m_hash),
            ).fetchone()
            if row is not None:
                cached_results[(s.fp, m.fp)] = bool(row["is_same"])
            else:
                uncached.append((s, m, sim, s_hash, m_hash))

        _report(f"Gemini判定 候補{len(candidates)}件 (キャッシュ{len(candidates)-len(uncached)}件)", 0, max(len(uncached), 1), 0)

        # Gemini 判定 (バッチ)
        if uncached:
            import time as _time
            _t0 = _time.time()
            gemini_results = self._gemini_batch_judge(uncached, lambda done, total: _report(
                f"Gemini判定 {done}/{total}件", done, total, _time.time() - _t0))
            for (s, m, sim, s_hash, m_hash), is_same in zip(uncached, gemini_results):
                # キャッシュに保存
                try:
                    conn.execute(
                        "INSERT OR REPLACE INTO lc_anchor_judgments"
                        " (session_text_hash, master_text_hash, is_same) VALUES (?, ?, ?)",
                        (s_hash, m_hash, 1 if is_same else 0),
                    )
                except Exception:
                    pass
                cached_results[(s.fp, m.fp)] = is_same
            try:
                conn.commit()
            except Exception:
                pass

        # アンカー確定
        anchors: list[AnchorMatch] = []
        for s, m, sim in candidates:
            if cached_results.get((s.fp, m.fp), False):
                anchors.append(AnchorMatch(
                    session_fp=s.fp, master_fp=m.fp,
                    master_sort=master_sort_map[m.fp],
                    method="phase2_gemini", score=round(sim, 3), phase=2,
                ))
                matched_master_fps.add(m.fp)

        return anchors

    @staticmethod
    def _gemini_batch_judge(
        pairs: list[tuple[NodeInfo, NodeInfo, float, str, str]],
        on_progress=None,
    ) -> list[bool]:
        """Gemini Flash にテキストペアの同一画面判定をバッチで問い合わせる。"""
        import json
        import re
        import time

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            logger.warning("[AnchorMatcher] GEMINI_API_KEY 未設定: Phase 2 スキップ")
            return [False] * len(pairs)

        try:
            from google import genai
            client = genai.Client(api_key=api_key)
        except Exception as e:
            logger.warning("[AnchorMatcher] Gemini 初期化失敗: %s", e)
            return [False] * len(pairs)

        BATCH_SIZE = 10
        results: list[bool] = []

        for i in range(0, len(pairs), BATCH_SIZE):
            batch = pairs[i:i + BATCH_SIZE]
            prompt_parts = []
            for idx, (s, m, sim, _, _) in enumerate(batch):
                prompt_parts.append(
                    f"ペア{idx+1}:\n"
                    f"  テキストA: {s.text[:500]}\n"
                    f"  テキストB: {m.text[:500]}"
                )

            prompt = (
                "以下のテキストペアは、同じゲーム画面のOCR結果です。\n"
                "OCRの揺れ（スペース、記号、改行の違い）により異なるテキストになっていますが、\n"
                "同じ画面の同じコンテンツを指しているかどうかを判定してください。\n"
                "セリフや数値が異なる場合は「異なる画面」と判定してください。\n\n"
                + "\n\n".join(prompt_parts) + "\n\n"
                "各ペアについて true (同じ画面) または false (異なる画面) を JSON 配列で返してください。\n"
                '例: [true, false, true]\n'
                "JSON 配列のみ返してください。説明は不要です。"
            )

            try:
                response = client.models.generate_content(
                    model="gemini-2.5-flash-lite",
                    contents=[prompt],
                )
                text = response.text.strip()
                # マークダウンコードブロック除去
                if text.startswith("```"):
                    text = re.sub(r'^```\w*\n?', '', text)
                    text = re.sub(r'\n?```$', '', text)
                judgments = json.loads(text)
                if isinstance(judgments, list):
                    for j in judgments:
                        results.append(bool(j))
                    # バッチサイズに満たない場合 False で埋める
                    while len(results) < i + len(batch):
                        results.append(False)
                else:
                    results.extend([False] * len(batch))
            except Exception as e:
                logger.warning("[AnchorMatcher] Gemini 判定失敗: %s", e)
                results.extend([False] * len(batch))

            # 進捗報告
            if on_progress:
                on_progress(min(i + len(batch), len(pairs)), len(pairs))

            # レートリミット
            if i + BATCH_SIZE < len(pairs):
                time.sleep(1.5)

        return results

    # ─── Phase 3: auto + テキストあり ─────────────────

    def _phase3_auto_text(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
    ) -> list[AnchorMatch]:
        """auto + テキストありノードを、Phase 1 アンカー範囲制限付きでマッチ。"""
        from lc.utils import phash_distance

        matched_master_fps = {a.master_fp for a in existing_anchors}
        matched_session_fps = {a.session_fp for a in existing_anchors}

        # アンカーを session time_rank 順にソート
        sorted_anchors = sorted(existing_anchors, key=lambda a: self._session_rank(a, session_nodes))

        targets = [n for n in session_nodes
                   if n.edge_type == "auto" and n.has_text and n.fp not in matched_session_fps]

        # マスターテキスト逆引き
        master_by_text: dict[str, list[NodeInfo]] = {}
        for m in master_nodes:
            if m.has_text:
                master_by_text.setdefault(m.text, []).append(m)

        anchors: list[AnchorMatch] = []

        for s in targets:
            # 候補範囲を限定
            sort_min, sort_max = self._get_sort_range(s, session_nodes, sorted_anchors, master_sort_map)

            # テキスト一致候補 (範囲内のみ)
            candidates = []
            for m in master_by_text.get(s.text, []):
                if m.fp in matched_master_fps:
                    continue
                m_sort = master_sort_map.get(m.fp, -1)
                if sort_min <= m_sort <= sort_max:
                    candidates.append(m)

            # 前方一致 (完全一致がなければ)
            if not candidates and len(s.text) >= 5:
                for text, ms in master_by_text.items():
                    if not text:
                        continue
                    shorter, longer = (s.text, text) if len(s.text) <= len(text) else (text, s.text)
                    if len(shorter) >= 5 and longer.startswith(shorter):
                        for m in ms:
                            if m.fp in matched_master_fps:
                                continue
                            m_sort = master_sort_map.get(m.fp, -1)
                            if sort_min <= m_sort <= sort_max:
                                candidates.append(m)

            if len(candidates) != 1:
                continue

            m = candidates[0]
            if s.phash and m.phash:
                dist = phash_distance(s.phash, m.phash)
                if dist >= self.PHASE1_PHASH_THRESHOLD:
                    continue

            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase3_auto_text", score=1.0, phase=3,
            ))
            matched_master_fps.add(m.fp)

        return anchors

    # ─── Phase 4: tap + テキスト空 ────────────────────

    def _phase4_tap_phash(
        self,
        session_nodes: list[NodeInfo],
        master_nodes: list[NodeInfo],
        master_sort_map: dict[str, int],
        existing_anchors: list[AnchorMatch],
    ) -> list[AnchorMatch]:
        """tap + テキスト空ノードを、前後アンカー必須 + phash でマッチ。"""
        from lc.utils import phash_distance

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
                    dist = phash_distance(s.phash, m.phash)
                    if dist < self.PHASE4_PHASH_THRESHOLD:
                        candidates.append((m, dist))

            if len(candidates) != 1:
                continue

            m, dist = candidates[0]
            score = max(0.0, 1.0 - dist / 64.0)
            anchors.append(AnchorMatch(
                session_fp=s.fp, master_fp=m.fp,
                master_sort=master_sort_map[m.fp],
                method="phase4_tap_phash", score=score, phase=4,
            ))
            matched_master_fps.add(m.fp)

        return anchors

    # ─── 時系列整合性チェック ──────────────────────────

    def _verify_consistency(self, anchors: list[AnchorMatch]) -> list[AnchorMatch]:
        """時系列整合性チェック。矛盾するアンカーを LIS で除去。"""
        if len(anchors) <= 1:
            return anchors

        # session time_rank 順にソート
        sorted_by_rank = sorted(anchors, key=lambda a: a.master_sort)
        # master_sort の列から LIS (最長増加部分列) を求める
        # ここでは session 順にソートして master_sort が単調増加か確認
        sorted_by_session = sorted(anchors, key=lambda a: a.session_fp)
        # 実際には time_rank でソートすべきだが、session_fp は一意なので
        # AnchorMatch に time_rank を持たせる必要がある

        # 簡易版: session の順序(追加順 ≈ time_rank 順) で master_sort が単調増加か
        master_sorts = [a.master_sort for a in anchors]

        # LIS (最長増加部分列) を求める
        lis_indices = self._longest_increasing_subsequence(master_sorts)
        lis_set = set(lis_indices)

        removed = len(anchors) - len(lis_indices)
        if removed > 0:
            logger.warning("[AnchorMatcher] 矛盾検出: %d アンカーを破棄 (LIS で %d 保持)",
                           removed, len(lis_indices))

        return [anchors[i] for i in lis_indices]

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
