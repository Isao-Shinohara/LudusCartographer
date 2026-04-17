"""
cross_session_merger.py — クロスセッションマージ

各セッションの遷移グラフをマスターグラフに統合する。
設計書: docs/cross_session_merge.md

アルゴリズム:
1. マスターが空 → 最初のセッションをそのままコピー
2. アンカーポイント検出 (ホーム画面, 収束点, 静的UI)
3. アンカー同士マッチング (phash + テキスト + scene)
4. k-hop 拡張マッチング
5. 残りは transition_similarity で追加マッチング
6. マッチしないノード・エッジは新規追加
7. BFS depth + SCC 再計算
"""
from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)


# ─── ヘルパー ────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """OCR テキストの揺れを正規化。"""
    text = re.sub(r'[\s\u3000]+', ' ', text).strip().lower()
    text = re.sub(r'[^\u3040-\u9fff\u30a0-\u30ffA-Za-z0-9 ]', '', text)
    return text


def _text_similarity(a: str, b: str) -> float:
    """Jaccard 類似度 (トークンベース)。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a and not tokens_b:
        return 1.0
    inter = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(inter) / len(union) if union else 0.0


@dataclass
class AnchorPoint:
    fp: str
    session_id: str
    anchor_type: str  # 'home', 'convergence', 'static_ui'
    ocr_text: str
    phash: str
    scene: str


# ─── メインクラス ─────────────────────────────────────

class CrossSessionMerger:
    """セッション別グラフをマスターグラフに統合する。"""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self._conn.close()

    # ─── アンカー検出 ─────────────────────────────────

    def find_anchors(self, session_id: str) -> list[AnchorPoint]:
        """セッション内のアンカーポイント（確実に同定可能な画面）を検出。"""
        anchors: list[AnchorPoint] = []

        # 1. ホーム画面
        home = self._conn.execute(
            "SELECT to_fp FROM lc_transitions"
            " WHERE action_name = 'GOAL_HOME_REACHED' AND session_id = ?"
            " LIMIT 1",
            (session_id,),
        ).fetchone()
        if home and home["to_fp"]:
            info = self._get_node_info(home["to_fp"], session_id)
            if info:
                anchors.append(AnchorPoint(
                    fp=home["to_fp"], session_id=session_id,
                    anchor_type="home", **info,
                ))

        # 2. 収束点 (入次数上位)
        convergence = self._conn.execute(
            "SELECT to_fp, COUNT(*) as in_deg FROM lc_transitions"
            " WHERE to_fp IS NOT NULL AND session_id = ?"
            " GROUP BY to_fp ORDER BY in_deg DESC LIMIT 5",
            (session_id,),
        ).fetchall()
        seen_fps = {a.fp for a in anchors}
        for row in convergence:
            fp = row["to_fp"]
            if fp in seen_fps:
                continue
            if row["in_deg"] < 3:
                continue
            info = self._get_node_info(fp, session_id)
            if info:
                anchors.append(AnchorPoint(
                    fp=fp, session_id=session_id,
                    anchor_type="convergence", **info,
                ))
                seen_fps.add(fp)

        # 3. 静的UI (テキスト量が多い MENU シーン)
        static_ui = self._conn.execute(
            "SELECT fingerprint, COALESCE(ocr_text_hq, ocr_text, '') AS ocr,"
            "  phash, scene"
            " FROM lc_screens"
            " WHERE session_id = ? AND scene = 'MENU' AND is_representative = 1"
            "   AND LENGTH(COALESCE(ocr_text_hq, ocr_text, '')) > 30"
            " ORDER BY LENGTH(COALESCE(ocr_text_hq, ocr_text, '')) DESC"
            " LIMIT 5",
            (session_id,),
        ).fetchall()
        for row in static_ui:
            fp = row["fingerprint"]
            if fp in seen_fps:
                continue
            anchors.append(AnchorPoint(
                fp=fp, session_id=session_id, anchor_type="static_ui",
                ocr_text=_normalize_text(row["ocr"]),
                phash=row["phash"] or "", scene=row["scene"] or "",
            ))
            seen_fps.add(fp)

        logger.info("[Merger] アンカー検出: session=%s, %d件 (home=%d, conv=%d, static=%d)",
                    session_id, len(anchors),
                    sum(1 for a in anchors if a.anchor_type == "home"),
                    sum(1 for a in anchors if a.anchor_type == "convergence"),
                    sum(1 for a in anchors if a.anchor_type == "static_ui"))
        return anchors

    def find_master_anchors(self) -> list[AnchorPoint]:
        """マスターグラフのアンカーポイントを検出。"""
        anchors: list[AnchorPoint] = []
        seen_fps: set[str] = set()

        # 1. ホーム (bfs_depth=0)
        home = self._conn.execute(
            "SELECT master_fp, title, scene, phash, ocr_text FROM lc_master_nodes"
            " WHERE bfs_depth = 0 LIMIT 1"
        ).fetchone()
        if home:
            anchors.append(AnchorPoint(
                fp=home["master_fp"], session_id="master", anchor_type="home",
                ocr_text=_normalize_text(home["ocr_text"] or ""),
                phash=home["phash"] or "", scene=home["scene"] or "",
            ))
            seen_fps.add(home["master_fp"])

        # 2. 収束点
        convergence = self._conn.execute(
            "SELECT to_master_fp, SUM(count) as in_deg FROM lc_master_edges"
            " GROUP BY to_master_fp ORDER BY in_deg DESC LIMIT 5"
        ).fetchall()
        for row in convergence:
            fp = row["to_master_fp"]
            if fp in seen_fps:
                continue
            node = self._conn.execute(
                "SELECT title, scene, phash, ocr_text FROM lc_master_nodes WHERE master_fp = ?",
                (fp,),
            ).fetchone()
            if node:
                anchors.append(AnchorPoint(
                    fp=fp, session_id="master", anchor_type="convergence",
                    ocr_text=_normalize_text(node["ocr_text"] or ""),
                    phash=node["phash"] or "", scene=node["scene"] or "",
                ))
                seen_fps.add(fp)

        # 3. 静的UI
        static = self._conn.execute(
            "SELECT master_fp, title, scene, phash, ocr_text FROM lc_master_nodes"
            " WHERE scene = 'MENU' AND LENGTH(COALESCE(ocr_text, '')) > 30"
            " ORDER BY LENGTH(ocr_text) DESC LIMIT 5"
        ).fetchall()
        for row in static:
            fp = row["master_fp"]
            if fp in seen_fps:
                continue
            anchors.append(AnchorPoint(
                fp=fp, session_id="master", anchor_type="static_ui",
                ocr_text=_normalize_text(row["ocr_text"] or ""),
                phash=row["phash"] or "", scene=row["scene"] or "",
            ))
            seen_fps.add(fp)

        return anchors

    # ─── マッチング ──────────────────────────────────

    @staticmethod
    def match_score(a: AnchorPoint, b: AnchorPoint) -> float:
        """2つのアンカーのマッチスコア (0.0〜1.0)。"""
        from lc.utils import phash_distance

        # phash 距離 → 類似度 (0〜1)
        ph_sim = 0.0
        if a.phash and b.phash:
            dist = phash_distance(a.phash, b.phash)
            ph_sim = max(0.0, 1.0 - dist / 64.0)

        # テキスト類似度
        text_sim = _text_similarity(a.ocr_text, b.ocr_text)

        # scene 一致
        scene_bonus = 1.0 if a.scene == b.scene else 0.0

        return ph_sim * 0.3 + text_sim * 0.5 + scene_bonus * 0.2

    def node_match_score(self, fp_a: str, sid_a: str, fp_b: str, sid_b: str) -> float:
        """任意の2ノードのマッチスコア。"""
        from lc.utils import phash_distance

        if sid_a == "master":
            info_a = self._get_master_node_info(fp_a)
        else:
            info_a = self._get_node_info(fp_a, sid_a)
        if sid_b == "master":
            info_b = self._get_master_node_info(fp_b)
        else:
            info_b = self._get_node_info(fp_b, sid_b)

        if not info_a or not info_b:
            return 0.0

        ph_sim = 0.0
        if info_a["phash"] and info_b["phash"]:
            dist = phash_distance(info_a["phash"], info_b["phash"])
            ph_sim = max(0.0, 1.0 - dist / 64.0)

        text_sim = _text_similarity(info_a["ocr_text"], info_b["ocr_text"])
        scene_bonus = 1.0 if info_a["scene"] == info_b["scene"] else 0.0

        return ph_sim * 0.3 + text_sim * 0.5 + scene_bonus * 0.2

    def k_hop_match(
        self,
        anchor_s: str, anchor_m: str,
        session_id: str, k: int = 2,
    ) -> dict[str, str]:
        """アンカーから k-hop 以内のノードをマッチング。

        Returns: {session_fp: master_fp}
        """
        s_graph = self._load_session_transitions(session_id)
        m_graph = self._load_master_transitions()

        if anchor_s not in s_graph or anchor_m not in m_graph:
            return {}

        # BFS で k-hop 以内のノードを層別に取得
        s_layers = self._bfs_layers(s_graph, anchor_s, k)
        m_layers = self._bfs_layers(m_graph, anchor_m, k)

        mapping: dict[str, str] = {}

        for depth in range(1, k + 1):
            s_nodes = s_layers.get(depth, [])
            m_nodes = m_layers.get(depth, [])
            if not s_nodes or not m_nodes:
                continue

            # 各 s_node に対してベストマッチを探す
            for s_fp in s_nodes:
                if s_fp in mapping:
                    continue
                best_score = 0.0
                best_m_fp = None
                for m_fp in m_nodes:
                    if m_fp in mapping.values():
                        continue
                    score = self.node_match_score(s_fp, session_id, m_fp, "master")
                    if score > best_score and score >= 0.5:
                        best_score = score
                        best_m_fp = m_fp
                if best_m_fp:
                    mapping[s_fp] = best_m_fp

        return mapping

    def transition_similarity(
        self, fp_s: str, session_id: str, fp_m: str,
    ) -> float:
        """ノードの遷移パターン（前後の接続先テキスト）の類似度。"""
        s_graph = self._load_session_transitions(session_id)
        m_graph = self._load_master_transitions()

        # 遷移先テキスト集合
        s_succs = set()
        if fp_s in s_graph:
            for succ in s_graph.successors(fp_s):
                info = self._get_node_info(succ, session_id)
                if info and info["ocr_text"]:
                    s_succs.add(info["ocr_text"])

        m_succs = set()
        if fp_m in m_graph:
            for succ in m_graph.successors(fp_m):
                info = self._get_master_node_info(succ)
                if info and info["ocr_text"]:
                    m_succs.add(info["ocr_text"])

        # 遷移元テキスト集合
        s_preds = set()
        if fp_s in s_graph:
            for pred in s_graph.predecessors(fp_s):
                info = self._get_node_info(pred, session_id)
                if info and info["ocr_text"]:
                    s_preds.add(info["ocr_text"])

        m_preds = set()
        if fp_m in m_graph:
            for pred in m_graph.predecessors(fp_m):
                info = self._get_master_node_info(pred)
                if info and info["ocr_text"]:
                    m_preds.add(info["ocr_text"])

        succ_sim = _text_set_jaccard(s_succs, m_succs)
        pred_sim = _text_set_jaccard(s_preds, m_preds)

        return (succ_sim + pred_sim) / 2

    # ─── メインエントリ ──────────────────────────────

    def _report_progress(self, stage: str, current: int, total: int,
                         elapsed: float) -> None:
        """マージ進捗を auto_pilot_state に書き込む。"""
        import json as _json
        eta = (elapsed / current * (total - current)) if current > 0 else 0
        progress = _json.dumps({
            "stage": stage, "current": current, "total": total,
            "elapsed": round(elapsed, 1), "eta": round(eta, 1),
        }, ensure_ascii=False)
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO auto_pilot_state (key, value)"
                " VALUES ('merge_progress', ?)",
                (progress,),
            )
            self._conn.commit()
        except Exception:
            pass

    def _clear_progress(self) -> None:
        """マージ進捗をクリア。"""
        try:
            self._conn.execute(
                "DELETE FROM auto_pilot_state WHERE key = 'merge_progress'"
            )
            self._conn.commit()
        except Exception:
            pass

    def _compute_matches(
        self, session_id: str,
    ) -> tuple[dict[str, tuple[str, str, float]], list[sqlite3.Row], bool]:
        """マッチ計算のみ (副作用なし)。

        Returns:
            (node_mapping, session_reps, is_seed)
            node_mapping: session_fp → (master_fp, method, score)
            session_reps: セッションの代表画面一覧
            is_seed: 初回セッション (マスター空) の場合 True
        """
        import time as _time
        t0 = _time.time()

        master_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes"
        ).fetchone()[0]

        if master_count == 0:
            session_reps = self._conn.execute(
                "SELECT fingerprint FROM lc_screens"
                " WHERE session_id = ? AND is_representative = 1",
                (session_id,),
            ).fetchall()
            self._clear_progress()
            return {}, session_reps, True

        self._report_progress("アンカー検出", 0, 1, 0)

        # アンカー検出
        s_anchors = self.find_anchors(session_id)
        m_anchors = self.find_master_anchors()

        session_reps = self._conn.execute(
            "SELECT fingerprint FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall()

        if not s_anchors or not m_anchors:
            logger.warning("[Merger] アンカーが見つからない")
            self._clear_progress()
            return {}, session_reps, False

        # アンカーマッチング
        anchor_pairs: list[tuple[AnchorPoint, AnchorPoint, float]] = []
        for sa in s_anchors:
            for ma in m_anchors:
                score = self.match_score(sa, ma)
                if score >= 0.6:
                    anchor_pairs.append((sa, ma, score))
        anchor_pairs.sort(key=lambda x: -x[2])

        logger.info("[Merger] アンカーマッチ: %d ペア", len(anchor_pairs))

        # ノードマッピング構築
        node_mapping: dict[str, tuple[str, str, float]] = {}

        # Step 1: アンカーマッチ + Step 2: k-hop 拡張
        self._report_progress("k-hop 拡張", 0, len(anchor_pairs),
                              _time.time() - t0)
        for i, (sa, ma, score) in enumerate(anchor_pairs):
            if sa.fp not in node_mapping:
                node_mapping[sa.fp] = (ma.fp, "anchor", score)
                k_hop_map = self.k_hop_match(sa.fp, ma.fp, session_id, k=2)
                for s_fp, m_fp in k_hop_map.items():
                    if s_fp not in node_mapping:
                        k_score = self.node_match_score(s_fp, session_id, m_fp, "master")
                        node_mapping[s_fp] = (m_fp, "k_hop", k_score)
            self._report_progress("k-hop 拡張", i + 1, len(anchor_pairs),
                                  _time.time() - t0)

        logger.info("[Merger] アンカー+k-hop マッチ: %d ノード", len(node_mapping))

        # Step 3: transition_similarity で追加マッチング
        master_fps = [r["master_fp"] for r in self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes"
        ).fetchall()]
        matched_master_fps = {v[0] for v in node_mapping.values()}

        unmatched = [r for r in session_reps
                     if r["fingerprint"] not in node_mapping]
        for idx, row in enumerate(unmatched):
            s_fp = row["fingerprint"]
            best_score = 0.0
            best_m_fp = None
            s_info = self._get_node_info(s_fp, session_id)
            if not s_info:
                self._report_progress("transition マッチ", idx + 1,
                                      len(unmatched), _time.time() - t0)
                continue
            for m_fp in master_fps:
                if m_fp in matched_master_fps:
                    continue
                n_score = self.node_match_score(s_fp, session_id, m_fp, "master")
                if n_score < 0.5:
                    continue
                t_score = self.transition_similarity(s_fp, session_id, m_fp)
                combined = n_score * 0.6 + t_score * 0.4
                if combined > best_score and combined >= 0.5:
                    best_score = combined
                    best_m_fp = m_fp
            if best_m_fp:
                node_mapping[s_fp] = (best_m_fp, "transition", best_score)
                matched_master_fps.add(best_m_fp)
            self._report_progress("transition マッチ", idx + 1,
                                  len(unmatched), _time.time() - t0)

        elapsed = _time.time() - t0
        logger.info("[Merger] 全マッチ完了: %d/%d ノード (%.1f秒)",
                    len(node_mapping), len(session_reps), elapsed)

        # 所要時間を記録
        try:
            self._conn.execute(
                "INSERT OR REPLACE INTO auto_pilot_state (key, value)"
                " VALUES ('last_merge_duration', ?)",
                (str(round(elapsed, 1)),),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO auto_pilot_state (key, value)"
                " VALUES ('last_merge_nodes', ?)",
                (str(len(session_reps) * master_count),),
            )
            self._conn.commit()
        except Exception:
            pass

        self._clear_progress()
        return node_mapping, session_reps, False

    def preview_merge(self, session_id: str) -> dict:
        """マージのプレビュー (DB 書き込みなし)。

        Returns: {
            'session_id', 'is_seed', 'session_screens', 'master_nodes_before',
            'matches': [{'session_fp', 'master_fp', 'method', 'score',
                         'session_title', 'session_thumb', 'master_title', 'master_thumb',
                         'session_neighbors', 'master_neighbors'}],
            'new_nodes': [{'fp', 'title', 'thumb', 'neighbors'}],
            'summary': {'anchor', 'k_hop', 'transition', 'new'}
        }
        """
        master_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes"
        ).fetchone()[0]

        node_mapping, session_reps, is_seed = self._compute_matches(session_id)

        # サマリー集計
        summary = {"anchor": 0, "k_hop": 0, "transition": 0, "new": 0}
        for _, (_, method, _) in node_mapping.items():
            summary[method] = summary.get(method, 0) + 1
        new_fps = [r["fingerprint"] for r in session_reps
                   if r["fingerprint"] not in node_mapping]
        summary["new"] = len(new_fps)

        # マッチ詳細
        matches = []
        for s_fp, (m_fp, method, score) in node_mapping.items():
            s_info = self._get_screen_info(s_fp, session_id)
            m_info = self._get_master_screen_info(m_fp)
            matches.append({
                "session_fp": s_fp,
                "master_fp": m_fp,
                "method": method,
                "score": round(score, 3),
                "session_title": s_info.get("title", "") if s_info else "",
                "session_thumb": s_info.get("thumbnail_path", "") if s_info else "",
                "master_title": m_info.get("title", "") if m_info else "",
                "master_thumb": m_info.get("thumbnail_path", "") if m_info else "",
                "session_neighbors": self._get_neighbors(s_fp, session_id),
                "master_neighbors": self._get_master_neighbors(m_fp),
            })

        # 新規ノード詳細
        new_nodes = []
        for fp in new_fps:
            s_info = self._get_screen_info(fp, session_id)
            new_nodes.append({
                "fp": fp,
                "title": s_info.get("title", "") if s_info else "",
                "thumb": s_info.get("thumbnail_path", "") if s_info else "",
                "neighbors": self._get_neighbors(fp, session_id),
            })

        return {
            "session_id": session_id,
            "is_seed": is_seed,
            "session_screens": len(session_reps),
            "master_nodes_before": master_count,
            "matches": matches,
            "new_nodes": new_nodes,
            "summary": summary,
        }

    def merge_to_master(self, session_id: str) -> int:
        """セッションのグラフをマスターグラフにマージする。

        Returns: 新規追加ノード数
        """
        node_mapping, session_reps, is_seed = self._compute_matches(session_id)

        if is_seed:
            return self._seed_master(session_id)

        if not node_mapping and session_reps:
            # アンカーなし → 全ノード新規追加
            return self._add_all_as_new(session_id)

        # manual_group: マッチ先がグループ非代表なら代表にリダイレクト
        group_redirect = {}
        group_rows = self._conn.execute(
            "SELECT master_fp, manual_group_id FROM lc_master_nodes"
            " WHERE manual_group_id IS NOT NULL AND is_group_representative = 0"
        ).fetchall()
        for gr in group_rows:
            rep = self._conn.execute(
                "SELECT master_fp FROM lc_master_nodes"
                " WHERE manual_group_id = ? AND is_group_representative = 1",
                (gr["manual_group_id"],),
            ).fetchone()
            if rep:
                group_redirect[gr["master_fp"]] = rep["master_fp"]

        if group_redirect:
            for s_fp, (m_fp, method, score) in list(node_mapping.items()):
                if m_fp in group_redirect:
                    node_mapping[s_fp] = (group_redirect[m_fp], method, score)

        # DB 更新
        now = datetime.now().isoformat()
        new_count = 0

        for row in session_reps:
            s_fp = row["fingerprint"]
            if s_fp in node_mapping:
                m_fp, method, score = node_mapping[s_fp]
                self._conn.execute(
                    "UPDATE lc_master_nodes SET visit_count = visit_count + 1,"
                    " last_seen_at = ? WHERE master_fp = ?",
                    (now, m_fp),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO lc_node_mappings"
                    " (session_id, session_fp, master_fp, match_method, match_score)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, s_fp, m_fp, method, score),
                )
            else:
                s_info = self._get_node_info(s_fp, session_id)
                if not s_info:
                    continue
                screen = self._conn.execute(
                    "SELECT id, discovered_at FROM lc_screens"
                    " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
                    (s_fp, session_id),
                ).fetchone()
                self._conn.execute(
                    "INSERT OR IGNORE INTO lc_master_nodes"
                    " (master_fp, representative_screen_id, title, scene, phash,"
                    "  ocr_text, visit_count, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (s_fp, screen["id"] if screen else None,
                     s_info.get("title", ""), s_info["scene"], s_info["phash"],
                     s_info["ocr_text"], now, now),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO lc_node_mappings"
                    " (session_id, session_fp, master_fp, match_method, match_score)"
                    " VALUES (?, ?, ?, 'new', 1.0)",
                    (session_id, s_fp, s_fp),
                )
                new_count += 1

        self._merge_edges(session_id, node_mapping)
        self._recalculate_master_graph()

        self._conn.commit()
        logger.info("[Merger] マージ完了: session=%s, matched=%d, new=%d",
                    session_id, len(node_mapping), new_count)
        return new_count

    def rebuild_master(self) -> None:
        """全セッションからマスターグラフを再構築。"""
        logger.info("[Merger] マスターグラフ再構築開始")

        # クリア
        self._conn.execute("DELETE FROM lc_master_nodes")
        self._conn.execute("DELETE FROM lc_master_edges")
        self._conn.execute("DELETE FROM lc_node_mappings")
        self._conn.commit()

        # セッション一覧
        sessions = self._conn.execute(
            "SELECT session_id FROM lc_session_graphs ORDER BY built_at"
        ).fetchall()

        if not sessions:
            logger.info("[Merger] セッショングラフなし → スキップ")
            return

        total_new = 0
        for i, row in enumerate(sessions):
            sid = row["session_id"]
            new_count = self.merge_to_master(sid)
            total_new += new_count
            logger.info("[Merger] rebuild %d/%d: session=%s, +%d nodes",
                        i + 1, len(sessions), sid, new_count)

        master_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes"
        ).fetchone()[0]
        edge_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_edges"
        ).fetchone()[0]
        logger.info("[Merger] 再構築完了: %d ノード, %d エッジ", master_count, edge_count)

    # ─── 未マージ (Unmerge) ──────────────────────────

    def can_unmerge(self, session_id: str) -> dict:
        """セッションの未マージが可能か判定する。

        Returns: {"ok": bool, "reason": str | None}
        """
        # session_graphs に存在するか
        sg = self._conn.execute(
            "SELECT session_id, built_at FROM lc_session_graphs WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not sg:
            return {"ok": False, "reason": "セッショングラフが存在しません"}
        if not sg["built_at"]:
            return {"ok": False, "reason": "未マージのセッションです"}

        # node_mappings にマッピングがあるか
        mapping_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_node_mappings WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if mapping_count == 0:
            return {"ok": False, "reason": "マージ記録（node_mappings）がありません"}

        # 残りのマージ済みセッションの transitions/screens が存在するか
        other_sessions = self._conn.execute(
            "SELECT sg.session_id FROM lc_session_graphs sg"
            " WHERE sg.session_id != ? AND sg.built_at IS NOT NULL",
            (session_id,),
        ).fetchall()

        for other in other_sessions:
            sid = other["session_id"]
            trans = self._conn.execute(
                "SELECT COUNT(*) FROM lc_transitions WHERE session_id = ?",
                (sid,),
            ).fetchone()[0]
            screens = self._conn.execute(
                "SELECT COUNT(*) FROM lc_screens"
                " WHERE session_id = ? AND is_representative = 1",
                (sid,),
            ).fetchone()[0]
            if trans == 0 or screens == 0:
                return {
                    "ok": False,
                    "reason": f"セッション {sid} の遷移データまたは"
                              f"代表画像が欠損しており再構築できません",
                }

        return {"ok": True, "reason": None}

    def unmerge_session(self, session_id: str) -> dict:
        """セッションを未マージに戻し、マスターグラフを再構築する。

        Returns: {"ok": bool, "master_nodes": int, "master_edges": int,
                  "restored_manual": int, "error": str | None}
        """
        check = self.can_unmerge(session_id)
        if not check["ok"]:
            return {"ok": False, "master_nodes": 0, "master_edges": 0,
                    "restored_manual": 0, "error": check["reason"]}

        logger.info("[Unmerge] セッション %s の未マージ開始", session_id)

        # 1. 排他フラグ ON
        self._conn.execute(
            "INSERT OR REPLACE INTO auto_pilot_state (key, value)"
            " VALUES ('is_rebuilding', '1')"
        )
        self._conn.commit()

        try:
            # 2. 手動変更バックアップ（全ノード）
            self._conn.execute("DROP TABLE IF EXISTS _unmerge_backup")
            self._conn.execute(
                "CREATE TEMP TABLE _unmerge_backup AS"
                " SELECT master_fp, user_excluded, manual_group_id,"
                "   is_group_representative, title,"
                "   ocr_text_manual, title_manual, manual_edited_at"
                " FROM lc_master_nodes"
            )
            backup_count = self._conn.execute(
                "SELECT COUNT(*) FROM _unmerge_backup"
            ).fetchone()[0]
            logger.info("[Unmerge] バックアップ: %d ノード", backup_count)

            # 3. マスターグラフ全削除
            self._conn.execute("DELETE FROM lc_master_nodes")
            self._conn.execute("DELETE FROM lc_master_edges")
            self._conn.execute("DELETE FROM lc_node_mappings")
            self._conn.commit()

            # 4. 対象セッションを除外して再構築
            sessions = self._conn.execute(
                "SELECT session_id FROM lc_session_graphs"
                " WHERE session_id != ? AND built_at IS NOT NULL"
                " ORDER BY built_at",
                (session_id,),
            ).fetchall()

            for i, row in enumerate(sessions):
                sid = row["session_id"]
                new_count = self.merge_to_master(sid)
                logger.info("[Unmerge] rebuild %d/%d: session=%s, +%d nodes",
                            i + 1, len(sessions), sid, new_count)

            # 5. 手動変更の復元
            restored = self._conn.execute(
                "UPDATE lc_master_nodes SET"
                "  user_excluded = b.user_excluded,"
                "  manual_group_id = b.manual_group_id,"
                "  is_group_representative = b.is_group_representative,"
                "  title = b.title,"
                "  ocr_text_manual = b.ocr_text_manual,"
                "  title_manual = b.title_manual,"
                "  manual_edited_at = b.manual_edited_at"
                " FROM _unmerge_backup b"
                " WHERE lc_master_nodes.master_fp = b.master_fp"
            ).rowcount
            logger.info("[Unmerge] 手動変更復元: %d ノード", restored)

            # 6. orphan チェック: representative_screen_id の整合性
            orphans = self._conn.execute(
                "SELECT m.master_fp, m.representative_screen_id"
                " FROM lc_master_nodes m"
                " WHERE m.representative_screen_id IS NOT NULL"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM lc_screens s"
                "     WHERE s.id = m.representative_screen_id)"
            ).fetchall()
            for orph in orphans:
                alt = self._conn.execute(
                    "SELECT id FROM lc_screens"
                    " WHERE fingerprint = ? AND is_representative = 1"
                    " ORDER BY discovered_at DESC LIMIT 1",
                    (orph["master_fp"],),
                ).fetchone()
                new_id = alt["id"] if alt else None
                self._conn.execute(
                    "UPDATE lc_master_nodes SET representative_screen_id = ?"
                    " WHERE master_fp = ?",
                    (new_id, orph["master_fp"]),
                )
            if orphans:
                logger.info("[Unmerge] orphan 修復: %d ノード", len(orphans))

            # 7. 対象セッションの built_at をクリア（未マージ状態に戻す）
            self._conn.execute(
                "UPDATE lc_session_graphs SET built_at = NULL WHERE session_id = ?",
                (session_id,),
            )

            self._conn.execute("DROP TABLE IF EXISTS _unmerge_backup")
            self._conn.commit()

            master_nodes = self._conn.execute(
                "SELECT COUNT(*) FROM lc_master_nodes"
            ).fetchone()[0]
            master_edges = self._conn.execute(
                "SELECT COUNT(*) FROM lc_master_edges"
            ).fetchone()[0]

            logger.info("[Unmerge] 完了: %d ノード, %d エッジ, 復元 %d 件",
                        master_nodes, master_edges, restored)
            return {
                "ok": True,
                "master_nodes": master_nodes,
                "master_edges": master_edges,
                "restored_manual": restored,
                "error": None,
            }

        finally:
            # 排他フラグ OFF
            self._conn.execute(
                "UPDATE auto_pilot_state SET value = '0' WHERE key = 'is_rebuilding'"
            )
            self._conn.commit()

    # ─── 内部ヘルパー ────────────────────────────────

    def _seed_master(self, session_id: str) -> int:
        """最初のセッションをマスターにコピー。"""
        now = datetime.now().isoformat()

        # 代表画像をマスターノードにコピー
        reps = self._conn.execute(
            "SELECT id, fingerprint, title, scene, phash,"
            "  COALESCE(ocr_text_hq, ocr_text, '') AS ocr, discovered_at"
            " FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall()

        for r in reps:
            self._conn.execute(
                "INSERT OR IGNORE INTO lc_master_nodes"
                " (master_fp, representative_screen_id, title, scene, phash,"
                "  ocr_text, visit_count, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (r["fingerprint"], r["id"], r["title"], r["scene"],
                 r["phash"], _normalize_text(r["ocr"]),
                 r["discovered_at"], now),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO lc_node_mappings"
                " (session_id, session_fp, master_fp, match_method, match_score)"
                " VALUES (?, ?, ?, 'seed', 1.0)",
                (session_id, r["fingerprint"], r["fingerprint"]),
            )

        # エッジをコピー (tap + auto 両方。位相ソートに使うため)
        edges = self._conn.execute(
            "SELECT from_fp, to_fp, tap_label, action_name, edge_type, discovered_at"
            " FROM lc_transitions"
            " WHERE session_id = ? AND to_fp IS NOT NULL",
            (session_id,),
        ).fetchall()

        master_fps = {r["fingerprint"] for r in reps}
        # 不採用画面 → 同クラスタ代表画面の fingerprint に解決するマップ
        non_rep_to_rep: dict[str, str] = {}
        non_reps = self._conn.execute(
            "SELECT s.fingerprint, rep.fingerprint AS rep_fp"
            " FROM lc_screens s"
            " JOIN lc_screens rep ON rep.cluster_id = s.cluster_id"
            "   AND rep.session_id = s.session_id AND rep.is_representative = 1"
            " WHERE s.session_id = ? AND s.is_representative = 0"
            "   AND s.cluster_id IS NOT NULL",
            (session_id,),
        ).fetchall()
        for nr in non_reps:
            non_rep_to_rep[nr["fingerprint"]] = nr["rep_fp"]

        for e in edges:
            from_fp = e["from_fp"]
            to_fp = e["to_fp"]
            # 不採用画面はクラスタ代表に解決
            if from_fp not in master_fps:
                from_fp = non_rep_to_rep.get(from_fp, from_fp)
            if to_fp not in master_fps:
                to_fp = non_rep_to_rep.get(to_fp, to_fp)
            if from_fp not in master_fps or to_fp not in master_fps:
                continue
            if from_fp == to_fp:
                continue
            self._conn.execute(
                    "INSERT OR IGNORE INTO lc_master_edges"
                    " (from_master_fp, to_master_fp, tap_label, action_name,"
                    "  edge_type, count, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (from_fp, to_fp, e["tap_label"],
                     e["action_name"], (e["edge_type"] if "edge_type" in e.keys() else "tap") or "tap",
                     e["discovered_at"], now),
                )

        # Seed: sort_order は first_seen_at 順 (位相ソートは使わない)
        # 初回セッションは一本道なので時系列 = ゲーム進行順
        sorted_nodes = self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes ORDER BY first_seen_at ASC"
        ).fetchall()
        for i, row in enumerate(sorted_nodes):
            self._conn.execute(
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ?",
                (i, row["master_fp"]),
            )

        self._conn.commit()

        logger.info("[Merger] Seed: session=%s → %d ノード, first_seen_at 順で sort_order 確定",
                    session_id, len(reps))
        return len(reps)

    def _add_all_as_new(self, session_id: str) -> int:
        """全ノードを新規としてマスターに追加。"""
        now = datetime.now().isoformat()
        reps = self._conn.execute(
            "SELECT id, fingerprint, title, scene, phash,"
            "  COALESCE(ocr_text_hq, ocr_text, '') AS ocr, discovered_at"
            " FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall()

        for r in reps:
            self._conn.execute(
                "INSERT OR IGNORE INTO lc_master_nodes"
                " (master_fp, representative_screen_id, title, scene, phash,"
                "  ocr_text, visit_count, first_seen_at, last_seen_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (r["fingerprint"], r["id"], r["title"], r["scene"],
                 r["phash"], _normalize_text(r["ocr"]),
                 r["discovered_at"], now),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO lc_node_mappings"
                " (session_id, session_fp, master_fp, match_method, match_score)"
                " VALUES (?, ?, ?, 'new', 1.0)",
                (session_id, r["fingerprint"], r["fingerprint"]),
            )

        self._merge_edges(session_id, {})
        self._recalculate_master_graph()
        self._conn.commit()
        return len(reps)

    def _resolve_fp_to_master(self, fp: str, session_id: str,
                              fp_map: dict[str, str]) -> Optional[str]:
        """session_fp をマスター fp に変換。不採用画面はクラスタ代表経由で変換。"""
        if fp in fp_map:
            return fp_map[fp]
        # 不採用画面 → 同クラスタの代表画面の fp を取得 → fp_map で変換
        row = self._conn.execute(
            "SELECT cluster_id FROM lc_screens"
            " WHERE fingerprint = ? AND session_id = ?",
            (fp, session_id),
        ).fetchone()
        if not row or row["cluster_id"] is None:
            return None
        rep = self._conn.execute(
            "SELECT fingerprint FROM lc_screens"
            " WHERE cluster_id = ? AND session_id = ? AND is_representative = 1",
            (row["cluster_id"], session_id),
        ).fetchone()
        if not rep:
            return None
        return fp_map.get(rep["fingerprint"])

    def _merge_edges(
        self, session_id: str,
        node_mapping: dict[str, tuple[str, str, float]],
    ) -> None:
        """セッションのエッジをマスターにマージ。auto エッジは除外。"""
        now = datetime.now().isoformat()
        edges = self._conn.execute(
            "SELECT from_fp, to_fp, tap_label, action_name, discovered_at"
            " FROM lc_transitions"
            " WHERE session_id = ? AND to_fp IS NOT NULL"
            " AND COALESCE(edge_type, 'tap') != 'auto'",
            (session_id,),
        ).fetchall()

        # マッピング: session_fp → master_fp
        fp_map: dict[str, str] = {}
        for s_fp, (m_fp, _, _) in node_mapping.items():
            fp_map[s_fp] = m_fp
        # 新規ノード (session_fp == master_fp)
        new_nodes = self._conn.execute(
            "SELECT session_fp, master_fp FROM lc_node_mappings"
            " WHERE session_id = ? AND match_method IN ('new', 'seed')",
            (session_id,),
        ).fetchall()
        for r in new_nodes:
            fp_map[r["session_fp"]] = r["master_fp"]

        master_fps = set(r["master_fp"] for r in self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes"
        ).fetchall())

        for e in edges:
            m_from = self._resolve_fp_to_master(e["from_fp"], session_id, fp_map)
            m_to = self._resolve_fp_to_master(e["to_fp"], session_id, fp_map)
            if not m_from or not m_to:
                continue
            if m_from not in master_fps or m_to not in master_fps:
                continue
            if m_from == m_to:
                continue

            existing = self._conn.execute(
                "SELECT id, count FROM lc_master_edges"
                " WHERE from_master_fp = ? AND to_master_fp = ? AND tap_label IS ?",
                (m_from, m_to, e["tap_label"]),
            ).fetchone()

            if existing:
                self._conn.execute(
                    "UPDATE lc_master_edges SET count = count + 1, last_seen_at = ?"
                    " WHERE id = ?",
                    (now, existing["id"]),
                )
            else:
                self._conn.execute(
                    "INSERT OR IGNORE INTO lc_master_edges"
                    " (from_master_fp, to_master_fp, tap_label, action_name,"
                    "  count, first_seen_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (m_from, m_to, e["tap_label"], e["action_name"],
                     e["discovered_at"], now),
                )

    def _recalculate_master_graph(self) -> None:
        """マスターグラフの BFS depth + SCC + sort_order を再計算。"""
        nodes = self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes"
        ).fetchall()
        edges = self._conn.execute(
            "SELECT from_master_fp, to_master_fp, COALESCE(edge_type, 'tap') AS edge_type"
            " FROM lc_master_edges"
        ).fetchall()

        if not nodes or not edges:
            return

        G = nx.DiGraph()
        # エッジに edge_type を属性として持たせる (tap 優先 DFS 用)
        edge_types: dict[tuple[str, str], str] = {}
        for r in edges:
            G.add_edge(r["from_master_fp"], r["to_master_fp"])
            key = (r["from_master_fp"], r["to_master_fp"])
            # tap が1つでもあれば tap 扱い
            if key not in edge_types or r["edge_type"] == "tap":
                edge_types[key] = r["edge_type"]
        # ノードのみ（エッジなし）も追加
        for r in nodes:
            if r["master_fp"] not in G:
                G.add_node(r["master_fp"])

        # ROOT 検出: エッジを持つノードのうち first_seen_at が最古
        root = self._conn.execute(
            "SELECT m.master_fp FROM lc_master_nodes m"
            " WHERE EXISTS (SELECT 1 FROM lc_master_edges e"
            "   WHERE e.from_master_fp = m.master_fp OR e.to_master_fp = m.master_fp)"
            " ORDER BY m.first_seen_at ASC LIMIT 1"
        ).fetchone()
        home_fp = root["master_fp"] if root else None

        if not home_fp or home_fp not in G:
            # フォールバック: 出次数最大
            if G.number_of_nodes() > 0:
                home_fp = max(G.nodes(), key=lambda n: G.out_degree(n))

        # BFS depth (前回の値をリセット)
        self._conn.execute("UPDATE lc_master_nodes SET bfs_depth = NULL, scc_id = NULL, scc_label = NULL")
        depth_map: dict[str, int] = {}
        if home_fp and home_fp in G:
            undirected = G.to_undirected()
            depth_map[home_fp] = 0
            for parent, child in nx.bfs_edges(undirected, home_fp):
                depth_map[child] = depth_map[parent] + 1

        for fp, depth in depth_map.items():
            self._conn.execute(
                "UPDATE lc_master_nodes SET bfs_depth = ? WHERE master_fp = ?",
                (depth, fp),
            )

        # SCC
        forward_G = nx.DiGraph()
        for src, tgt in G.edges():
            d_src = depth_map.get(src, 999)
            d_tgt = depth_map.get(tgt, 999)
            if d_tgt >= d_src:
                forward_G.add_edge(src, tgt)

        sccs = [s for s in nx.strongly_connected_components(forward_G) if len(s) > 1]
        sccs.sort(key=lambda s: min(depth_map.get(fp, 999) for fp in s))

        for idx, scc_fps in enumerate(sccs, 1):
            for fp in scc_fps:
                self._conn.execute(
                    "UPDATE lc_master_nodes SET scc_id = ?, scc_label = ? WHERE master_fp = ?",
                    (idx, f"SCC#{idx}", fp),
                )

        # sort_order: 位相ソート (チュートリアル進行順)
        # SCC を condensation し、位相ソートで順序決定。
        # SCC 内は「入口ノードからの DFS 順」(マージ時期に依存しない)。
        # 接続なしノードは first_seen_at で末尾に。
        first_seen_map: dict[str, str] = {
            r["master_fp"]: r["first_seen_at"] or ""
            for r in self._conn.execute(
                "SELECT master_fp, first_seen_at FROM lc_master_nodes"
            ).fetchall()
        }

        ordered_fps: list[str] = []
        if G.number_of_edges() > 0:
            # SCC 検出 (有向グラフ全体で)
            sccs_all = list(nx.strongly_connected_components(G))
            # 各 SCC を condensation
            condensation = nx.condensation(G, scc=sccs_all)
            # 位相ソート (DAG 保証)
            try:
                topo_scc_ids = list(nx.topological_sort(condensation))
            except nx.NetworkXUnfeasible:
                topo_scc_ids = list(condensation.nodes())

            # SCC ID → メンバー集合
            scc_members: dict[int, set[str]] = {
                scc_id: set(condensation.nodes[scc_id]["members"])
                for scc_id in condensation.nodes()
            }
            # ノード fp → 所属 SCC ID
            fp_to_scc: dict[str, int] = {
                fp: scc_id for scc_id, members in scc_members.items() for fp in members
            }

            for scc_id in topo_scc_ids:
                members = scc_members[scc_id]
                if len(members) == 1:
                    ordered_fps.extend(members)
                    continue

                # 入口ノード = SCC 外から流入があるノード
                entry_nodes = []
                for m in members:
                    for pred in G.predecessors(m):
                        if fp_to_scc.get(pred) != scc_id:
                            entry_nodes.append(m)
                            break
                # 入口がなければ first_seen_at 最古を入口とする
                if not entry_nodes:
                    entry_nodes = [min(members, key=lambda fp: first_seen_map.get(fp, ""))]
                # 入口を first_seen_at 順で並べる (複数入口対応)
                entry_nodes = sorted(set(entry_nodes),
                                     key=lambda fp: first_seen_map.get(fp, ""))

                # SCC 内サブグラフで DFS (tap エッジ優先)
                sub_G = G.subgraph(members)
                visited: set[str] = set()
                scc_order: list[str] = []
                for entry in entry_nodes:
                    if entry in visited:
                        continue
                    stack = [entry]
                    while stack:
                        node = stack.pop()
                        if node in visited:
                            continue
                        visited.add(node)
                        scc_order.append(node)
                        # tap エッジ優先、同優先度なら first_seen_at 順
                        # (降順で積む → pop で昇順)
                        succs = sorted(
                            (n for n in sub_G.successors(node) if n not in visited),
                            key=lambda fp: (
                                0 if edge_types.get((node, fp)) == "tap" else 1,
                                first_seen_map.get(fp, ""),
                            ),
                            reverse=True,
                        )
                        stack.extend(succs)
                # 訪問漏れ (双方向到達不能等) を first_seen_at 順で末尾追加
                missed = sorted(
                    (fp for fp in members if fp not in visited),
                    key=lambda fp: first_seen_map.get(fp, ""),
                )
                scc_order.extend(missed)
                ordered_fps.extend(scc_order)

        # グラフに含まれないノード (孤立) を first_seen_at 順で末尾に追加
        in_graph = set(ordered_fps)
        orphans = sorted(
            (fp for fp in first_seen_map if fp not in in_graph),
            key=lambda fp: first_seen_map.get(fp, ""),
        )
        ordered_fps.extend(orphans)

        for i, fp in enumerate(ordered_fps):
            self._conn.execute(
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ?",
                (i, fp),
            )

    def _get_node_info(self, fp: str, session_id: str) -> Optional[dict]:
        """セッション内のノード情報を取得。"""
        row = self._conn.execute(
            "SELECT title, scene, phash, COALESCE(ocr_text_hq, ocr_text, '') AS ocr"
            " FROM lc_screens"
            " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
            (fp, session_id),
        ).fetchone()
        if not row:
            # 代表でなくても取得
            row = self._conn.execute(
                "SELECT title, scene, phash, COALESCE(ocr_text_hq, ocr_text, '') AS ocr"
                " FROM lc_screens"
                " WHERE fingerprint = ? AND session_id = ?",
                (fp, session_id),
            ).fetchone()
        if not row:
            return None
        return {
            "title": row["title"] or "",
            "ocr_text": _normalize_text(row["ocr"]),
            "phash": row["phash"] or "",
            "scene": row["scene"] or "",
        }

    def _get_master_node_info(self, fp: str) -> Optional[dict]:
        """マスターノードの情報を取得。"""
        row = self._conn.execute(
            "SELECT title, scene, phash, ocr_text FROM lc_master_nodes WHERE master_fp = ?",
            (fp,),
        ).fetchone()
        if not row:
            return None
        return {
            "title": row["title"] or "",
            "ocr_text": row["ocr_text"] or "",
            "phash": row["phash"] or "",
            "scene": row["scene"] or "",
        }

    # ─── プレビュー用ヘルパー ──────────────────────────

    def _get_screen_info(self, fp: str, session_id: str) -> Optional[dict]:
        """セッション画面の表示用情報を取得。"""
        row = self._conn.execute(
            "SELECT title, thumbnail_path, screenshot_path, scene"
            " FROM lc_screens"
            " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
            (fp, session_id),
        ).fetchone()
        if not row:
            row = self._conn.execute(
                "SELECT title, thumbnail_path, screenshot_path, scene"
                " FROM lc_screens WHERE fingerprint = ? AND session_id = ?",
                (fp, session_id),
            ).fetchone()
        if not row:
            return None
        return dict(row)

    def _get_master_screen_info(self, master_fp: str) -> Optional[dict]:
        """マスターノードの表示用情報を取得。"""
        row = self._conn.execute(
            "SELECT m.title, s.thumbnail_path, s.screenshot_path, m.scene"
            " FROM lc_master_nodes m"
            " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
            " WHERE m.master_fp = ?",
            (master_fp,),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def _get_neighbors(self, fp: str, session_id: str) -> list[dict]:
        """セッション内の遷移先/遷移元をサムネ付きで返す。"""
        neighbors = []
        for direction, col_from, col_to in [("to", "from_fp", "to_fp"), ("from", "to_fp", "from_fp")]:
            rows = self._conn.execute(
                f"SELECT DISTINCT t.{col_to} AS fp, s.title, s.thumbnail_path"
                f" FROM lc_transitions t"
                f" LEFT JOIN lc_screens s ON s.fingerprint = t.{col_to}"
                f"   AND s.session_id = t.session_id AND s.is_representative = 1"
                f" WHERE t.{col_from} = ? AND t.session_id = ? AND t.{col_to} IS NOT NULL",
                (fp, session_id),
            ).fetchall()
            for r in rows:
                neighbors.append({
                    "direction": direction,
                    "fp": r["fp"],
                    "title": r["title"] or "",
                    "thumb": r["thumbnail_path"] or "",
                })
        return neighbors

    def _get_master_neighbors(self, master_fp: str) -> list[dict]:
        """マスターグラフの遷移先/遷移元をサムネ付きで返す。"""
        neighbors = []
        for direction, col_from, col_to in [("to", "from_master_fp", "to_master_fp"), ("from", "to_master_fp", "from_master_fp")]:
            rows = self._conn.execute(
                f"SELECT DISTINCT e.{col_to} AS fp, m.title,"
                f" s.thumbnail_path"
                f" FROM lc_master_edges e"
                f" LEFT JOIN lc_master_nodes m ON m.master_fp = e.{col_to}"
                f" LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
                f" WHERE e.{col_from} = ?",
                (master_fp,),
            ).fetchall()
            for r in rows:
                neighbors.append({
                    "direction": direction,
                    "fp": r["fp"],
                    "title": r["title"] or "",
                    "thumb": r["thumbnail_path"] or "",
                })
        return neighbors

    def _load_session_transitions(self, session_id: str) -> nx.DiGraph:
        """セッションの遷移グラフをロード。"""
        G = nx.DiGraph()
        edges = self._conn.execute(
            "SELECT from_fp, to_fp FROM lc_transitions"
            " WHERE session_id = ? AND to_fp IS NOT NULL",
            (session_id,),
        ).fetchall()
        for e in edges:
            G.add_edge(e["from_fp"], e["to_fp"])
        return G

    def _load_master_transitions(self) -> nx.DiGraph:
        """マスターグラフの遷移をロード。"""
        G = nx.DiGraph()
        edges = self._conn.execute(
            "SELECT from_master_fp, to_master_fp FROM lc_master_edges"
        ).fetchall()
        for e in edges:
            G.add_edge(e["from_master_fp"], e["to_master_fp"])
        return G

    @staticmethod
    def _bfs_layers(G: nx.DiGraph, source: str, max_depth: int) -> dict[int, list[str]]:
        """BFS で層別にノードを取得。"""
        layers: dict[int, list[str]] = {}
        visited = {source}
        current = [source]
        for depth in range(1, max_depth + 1):
            next_layer = []
            for node in current:
                for neighbor in set(G.successors(node)) | set(G.predecessors(node)):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_layer.append(neighbor)
            if next_layer:
                layers[depth] = next_layer
            current = next_layer
        return layers


def _text_set_jaccard(a: set[str], b: set[str]) -> float:
    """テキスト集合の Jaccard 類似度。"""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0
