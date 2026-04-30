"""
cross_session_merger.py — クロスセッションマージ

各セッションの遷移グラフをマスターグラフに統合する。
設計書: docs/cross_session_merge.md

アルゴリズム:
1. マスターが空 → 最初のセッションをそのままコピー (seed)
2. AnchorMatcher で段階的マッチング (Phase 1/2/3/4)
3. SafeInsertStrategy で安全な位置にのみ挿入
4. BFS depth + SCC 再計算
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import networkx as nx

logger = logging.getLogger(__name__)


# ─── メインクラス ─────────────────────────────────────

class CrossSessionMerger:
    """セッション別グラフをマスターグラフに統合する。"""

    def __init__(self, db_path: Path, sort_strategy=None, anchor_matcher=None,
                 version_id: int | None = None):
        from tools.merge_sort_strategy import SafeInsertStrategy
        from tools.anchor_matcher import AnchorMatcher
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._sort_strategy = sort_strategy or SafeInsertStrategy()
        self._anchor_matcher = anchor_matcher or AnchorMatcher()

        # version_id: 明示指定がなければ lc_versions のアクティブバージョンを使用
        if version_id is not None:
            self._version_id = version_id
        else:
            row = self._conn.execute(
                "SELECT id FROM lc_versions WHERE is_active = 1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self._version_id = row["id"] if row else 1

    def close(self) -> None:
        self._conn.close()

    # ─── メインエントリ ──────────────────────────────

    # 進捗報告は削除済み (merge_result.json の出現で完了判定)

    def _compute_matches(
        self, session_id: str,
    ) -> tuple[dict[str, tuple[str, str, float]], list[sqlite3.Row], bool,
               dict[str, tuple[str, str, float, str]],
               dict[str, tuple[str, str, float]]]:
        """マッチ計算のみ (副作用なし)。AnchorMatcher に委譲。

        Returns:
            (node_mapping, session_reps, is_seed, discarded_mapping, excluded_mapping)
            node_mapping: session_fp → (master_fp, method, score)
            session_reps: セッションの代表画面一覧
            is_seed: 初回セッション (マスター空) の場合 True
            discarded_mapping: session_fp → (master_fp, method, score, reason)
            excluded_mapping: 不採用ノード一致 session_fp → (master_fp, method, score)
        """
        import time as _time
        t0 = _time.time()

        master_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
        ).fetchone()[0]

        # is_artifact > 0 (Live でユーザー不採用 = 2 / Gemini 不採用 = 1) は除外。
        # _seed_master と AnchorMatcher も同条件で除外しているのでプレビューと一致させる。
        session_reps = self._conn.execute(
            "SELECT fingerprint FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1"
            " AND COALESCE(is_artifact, 0) = 0",
            (session_id,),
        ).fetchall()

        if master_count == 0:
            return {}, session_reps, True, {}, {}

        # AnchorMatcher に委譲
        node_mapping, skipped, discarded_mapping, excluded_mapping = \
            self._anchor_matcher.compute_matches(self._conn, session_id)

        elapsed = _time.time() - t0
        logger.info("[Merger] AnchorMatcher 完了: anchor=%d, excluded_match=%d / %d ノード (%.1f秒)",
                    len(node_mapping), len(excluded_mapping), len(session_reps), elapsed)

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

        return node_mapping, session_reps, False, discarded_mapping, excluded_mapping

    def preview_merge(self, session_id: str,
                      exclude_fps: set[str] | None = None,
                      include_fps: set[str] | None = None) -> dict:
        """マージのプレビュー (DB 書き込みなし)。

        Returns: {
            'session_id', 'is_seed', 'session_screens', 'master_nodes_before',
            'matches': [{'session_fp', 'master_fp', 'method', 'score',
                         'session_title', 'session_thumb', 'master_title', 'master_thumb',
                         'session_neighbors', 'master_neighbors'}],
            'new_nodes': [{'fp', 'title', 'thumb', 'neighbors'}],
            'summary': {<method>: count, ..., 'new': count}
        }
        """
        master_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
        ).fetchone()[0]

        node_mapping, session_reps, is_seed, discarded_mapping, excluded_mapping = \
            self._compute_matches(session_id)

        # サマリー集計
        summary: dict[str, int] = {}
        for _, (_, method, _) in node_mapping.items():
            summary[method] = summary.get(method, 0) + 1
        new_fps = [r["fingerprint"] for r in session_reps
                   if r["fingerprint"] not in node_mapping
                   and r["fingerprint"] not in excluded_mapping]
        summary["new"] = len(new_fps)
        summary["excluded_match"] = len(excluded_mapping)

        # セッション・マスターの neighbors を一括取得
        all_session_fps = (list(node_mapping.keys()) + new_fps
                           + list(discarded_mapping.keys())
                           + list(excluded_mapping.keys()))
        session_neighbors_map = self._get_neighbors_batch(all_session_fps, session_id)
        master_fps_list = [m_fp for (m_fp, _, _) in node_mapping.values()]
        master_fps_list += [m_fp for (m_fp, _, _, _) in discarded_mapping.values()]
        master_fps_list += [m_fp for (m_fp, _, _) in excluded_mapping.values()]
        master_neighbors_map = self._get_master_neighbors_batch(master_fps_list)

        # マッチ詳細
        from lc.image_comparator import phash_distance
        from difflib import SequenceMatcher as _SM
        # phash/テキスト情報を一括取得
        _screen_phash: dict[str, str] = {}
        _screen_text: dict[str, str] = {}
        for r in self._conn.execute(
            "SELECT fingerprint, phash, COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS text"
            " FROM lc_screens WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall():
            _screen_phash[r["fingerprint"]] = r["phash"] or ""
            _screen_text[r["fingerprint"]] = r["text"] or ""
        _master_phash: dict[str, str] = {}
        _master_text: dict[str, str] = {}
        for r in self._conn.execute(
            "SELECT master_fp, phash, COALESCE(ocr_text_manual, ocr_text, '') AS text"
            " FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
        ).fetchall():
            _master_phash[r["master_fp"]] = r["phash"] or ""
            _master_text[r["master_fp"]] = r["text"] or ""

        # 採用 + 棄却 + 不採用ノード一致を統合して構築
        # status: "" = 採用, "discarded" = 棄却, "excluded_match" = 不採用ノード一致
        all_anchor_entries: list[tuple[str, str, str, float, str, str]] = []
        for s_fp, (m_fp, method, score) in node_mapping.items():
            all_anchor_entries.append((s_fp, m_fp, method, score, "", ""))
        for s_fp, (m_fp, method, score, reason) in discarded_mapping.items():
            all_anchor_entries.append((s_fp, m_fp, method, score, "discarded", reason))
        for s_fp, (m_fp, method, score) in excluded_mapping.items():
            all_anchor_entries.append((s_fp, m_fp, method, score, "excluded_match", ""))

        matches = []
        for s_fp, m_fp, method, score, status, reason in all_anchor_entries:
            s_info = self._get_screen_info(s_fp, session_id)
            m_info = self._get_master_screen_info(m_fp)
            s_ph = _screen_phash.get(s_fp, "")
            m_ph = _master_phash.get(m_fp, "")
            ph_dist = phash_distance(s_ph, m_ph) if s_ph and m_ph else -1
            s_txt = _screen_text.get(s_fp, "")
            m_txt = _master_text.get(m_fp, "")
            text_sim = round(_SM(None, s_txt, m_txt).ratio(), 3) if s_txt and m_txt else -1
            entry = {
                "session_fp": s_fp,
                "master_fp": m_fp,
                "method": method,
                "score": round(score, 3),
                "phash_dist": ph_dist,
                "text_sim": text_sim,
                "session_title": s_info.get("title", "") if s_info else "",
                "session_thumb": s_info.get("thumbnail_path", "") if s_info else "",
                "session_scene": s_info.get("scene", "") if s_info else "",
                "session_discovered_at": s_info.get("discovered_at", "") if s_info else "",
                "session_ocr_text": s_info.get("ocr_text", "") if s_info else "",
                "session_screenshot": s_info.get("screenshot_path", "") if s_info else "",
                "master_title": m_info.get("title", "") if m_info else "",
                "master_thumb": m_info.get("thumbnail_path", "") if m_info else "",
                "master_screenshot": m_info.get("screenshot_path", "") if m_info else "",
                "master_ocr_text": m_txt,
                "session_neighbors": session_neighbors_map.get(s_fp, []),
                "master_neighbors": master_neighbors_map.get(m_fp, []),
            }
            if status == "discarded":
                entry["discarded"] = True
                entry["discard_reason"] = reason
            elif status == "excluded_match":
                entry["excluded_match"] = True
            matches.append(entry)
        # 時系列順にソート
        matches.sort(key=lambda x: x.get("session_discovered_at", ""))
        summary["discarded"] = len(discarded_mapping)

        # 手動の採用/不採用をSafeInsert用のマッピングに反映
        adjusted_mapping = dict(node_mapping)
        if include_fps and discarded_mapping:
            for fp in include_fps:
                if fp in discarded_mapping:
                    m_fp, method, score, _reason = discarded_mapping[fp]
                    adjusted_mapping[fp] = (m_fp, method, score)
        if exclude_fps:
            for fp in exclude_fps:
                adjusted_mapping.pop(fp, None)

        # adjusted_mapping に基づく新規ノード再計算
        adjusted_new_fps = [r["fingerprint"] for r in session_reps
                            if r["fingerprint"] not in adjusted_mapping
                            and r["fingerprint"] not in discarded_mapping
                            and r["fingerprint"] not in excluded_mapping]

        # SafeInsert 判定: 実際に挿入されるノードを特定
        # seed (master 空) は _seed_master が全代表を無条件に INSERT するため、
        # SafeInsert を呼ばず全 new_fps を insertable として返す。
        if is_seed:
            insertable_fps = list(adjusted_new_fps if (exclude_fps or include_fps) else new_fps)
            skipped_fps: list[str] = []
        else:
            insertable_fps, skipped_fps = self._sort_strategy.preview_insertable(
                self._conn, session_id, adjusted_mapping
            )
        insertable_set = set(insertable_fps)
        summary["insertable"] = len(insertable_fps)
        summary["skipped"] = len(skipped_fps)

        # 新規ノード詳細 (adjusted_mapping 反映後)
        new_nodes = []
        use_new_fps = adjusted_new_fps if (exclude_fps or include_fps) else new_fps
        for fp in use_new_fps:
            s_info = self._get_screen_info(fp, session_id)
            new_nodes.append({
                "fp": fp,
                "screen_id": s_info.get("id") if s_info else None,
                "title": s_info.get("title", "") if s_info else "",
                "thumb": s_info.get("thumbnail_path", "") if s_info else "",
                "scene": s_info.get("scene", "") if s_info else "",
                "discovered_at": s_info.get("discovered_at", "") if s_info else "",
                "ocr_text": s_info.get("ocr_text", "") if s_info else "",
                "screenshot": s_info.get("screenshot_path", "") if s_info else "",
                "neighbors": session_neighbors_map.get(fp, []),
                "insertable": fp in insertable_set,
            })

        from tools.anchor_matcher import PHASE_DEFS
        return {
            "session_id": session_id,
            "is_seed": is_seed,
            "session_screens": len(session_reps),
            "master_nodes_before": master_count,
            "matches": matches,
            "new_nodes": new_nodes,
            "summary": summary,
            "phase_defs": PHASE_DEFS,
        }

    def _check_ocr_complete(self, session_id: str) -> None:
        """Gemini OCR 補正が完了しているか確認。未完了なら例外。"""
        pending = self._conn.execute(
            "SELECT COUNT(*) FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1"
            " AND ocr_text_gemini IS NULL",
            (session_id,),
        ).fetchone()[0]
        if pending > 0:
            raise RuntimeError(
                f"Gemini OCR 未完了: {session_id} ({pending} 件未処理)。"
                " ダッシュボードの「再開」ボタンで OCR 補正を完了してからマージしてください。"
            )

    def merge_to_master(self, session_id: str, exclude_fps: set[str] | None = None,
                        include_fps: set[str] | None = None) -> int:
        """セッションのグラフをマスターグラフにマージする。

        Args:
            exclude_fps: マッチから除外する session_fp のセット (手動除外用)
            include_fps: 棄却されたアンカーを強制採用する session_fp のセット (手動復帰用)
        Returns: 新規追加ノード数
        Raises: RuntimeError — Gemini OCR 補正が未完了の場合
        """
        self._check_ocr_complete(session_id)
        node_mapping, session_reps, is_seed, discarded, excluded_mapping = \
            self._compute_matches(session_id)

        # ユーザーが復帰指定した棄却ノードを node_mapping に追加
        if include_fps and discarded:
            for fp in include_fps:
                if fp in discarded:
                    m_fp, method, score, _reason = discarded[fp]
                    node_mapping[fp] = (m_fp, method, score)

        # ユーザーが除外指定した session_fp をマッピングから除去
        if exclude_fps:
            for fp in exclude_fps:
                node_mapping.pop(fp, None)

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
            " AND version_id = ?",
            (self._version_id,),
        ).fetchall()
        for gr in group_rows:
            rep = self._conn.execute(
                "SELECT master_fp FROM lc_master_nodes"
                " WHERE manual_group_id = ? AND is_group_representative = 1"
                " AND version_id = ?",
                (gr["manual_group_id"], self._version_id),
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

        # 不採用ノード一致: node_mappings に記録 (重複防止) だが新規ノード作成しない
        for s_fp, (m_fp, method, score) in excluded_mapping.items():
            self._conn.execute(
                "UPDATE lc_master_nodes SET visit_count = visit_count + 1,"
                " last_seen_at = ? WHERE master_fp = ? AND version_id = ?",
                (now, m_fp, self._version_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO lc_node_mappings"
                " (session_id, session_fp, master_fp, match_method, match_score, version_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, s_fp, m_fp, "excluded_match", score, self._version_id),
            )

        for row in session_reps:
            s_fp = row["fingerprint"]
            if s_fp in node_mapping:
                m_fp, method, score = node_mapping[s_fp]
                self._conn.execute(
                    "UPDATE lc_master_nodes SET visit_count = visit_count + 1,"
                    " last_seen_at = ? WHERE master_fp = ? AND version_id = ?",
                    (now, m_fp, self._version_id),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO lc_node_mappings"
                    " (session_id, session_fp, master_fp, match_method, match_score, version_id)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (session_id, s_fp, m_fp, method, score, self._version_id),
                )
            elif s_fp in excluded_mapping:
                continue  # 不採用ノード一致は既に処理済み
            else:
                s_info = self._get_node_info(s_fp, session_id)
                if not s_info:
                    continue
                screen = self._conn.execute(
                    "SELECT id, discovered_at FROM lc_screens"
                    " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
                    (s_fp, session_id),
                ).fetchone()
                screen_time = screen["discovered_at"] if screen else now
                self._conn.execute(
                    "INSERT OR IGNORE INTO lc_master_nodes"
                    " (master_fp, representative_screen_id, title, scene, phash,"
                    "  ocr_text, visit_count, first_seen_at, last_seen_at, version_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (s_fp, screen["id"] if screen else None,
                     s_info.get("title", ""), s_info["scene"], s_info["phash"],
                     s_info["ocr_text"], screen_time, now, self._version_id),
                )
                self._conn.execute(
                    "INSERT OR REPLACE INTO lc_node_mappings"
                    " (session_id, session_fp, master_fp, match_method, match_score, version_id)"
                    " VALUES (?, ?, ?, 'new', 1.0, ?)",
                    (session_id, s_fp, s_fp, self._version_id),
                )
                new_count += 1

        self._merge_edges(session_id, node_mapping)

        # SafeInsert: 挿入可能なノードのみ配置、それ以外は削除
        from tools.merge_sort_strategy import renumber_sort_orders
        result = self._sort_strategy.compute_sort_order(
            self._conn, session_id, node_mapping
        )

        # 挿入するノードの sort_order を設定
        for fp, sort_pos in result.inserts:
            self._conn.execute(
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ? AND version_id = ?",
                (sort_pos, fp, self._version_id),
            )

        # skipped ノードをマスターから削除
        for fp in result.skipped:
            self._conn.execute(
                "DELETE FROM lc_master_nodes WHERE master_fp = ? AND version_id = ?",
                (fp, self._version_id),
            )
            self._conn.execute(
                "DELETE FROM lc_node_mappings WHERE master_fp = ? AND session_id = ?"
                " AND version_id = ?",
                (fp, session_id, self._version_id),
            )
            self._conn.execute(
                "DELETE FROM lc_master_edges WHERE (from_master_fp = ? OR to_master_fp = ?)"
                " AND version_id = ?",
                (fp, fp, self._version_id),
            )
            new_count -= 1

        # sort_order を連番に振り直し
        renumber_sort_orders(self._conn)

        self._conn.commit()
        logger.info("[Merger] マージ完了: session=%s, matched=%d, new=%d, skipped=%d",
                    session_id, len(node_mapping), new_count, len(result.skipped))
        return new_count

    def rebuild_master(self) -> None:
        """全セッションからマスターグラフを再構築。"""
        logger.info("[Merger] マスターグラフ再構築開始")

        # クリア
        self._conn.execute("DELETE FROM lc_master_nodes WHERE version_id = ?",
                           (self._version_id,))
        self._conn.execute("DELETE FROM lc_master_edges WHERE version_id = ?",
                           (self._version_id,))
        self._conn.execute("DELETE FROM lc_node_mappings WHERE version_id = ?",
                           (self._version_id,))
        self._conn.commit()

        # セッション一覧
        sessions = self._conn.execute(
            "SELECT session_id FROM lc_session_graphs"
            " WHERE version_id = ? ORDER BY built_at",
            (self._version_id,),
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
            "SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
        ).fetchone()[0]
        edge_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_master_edges WHERE version_id = ?",
            (self._version_id,),
        ).fetchone()[0]
        logger.info("[Merger] 再構築完了: %d ノード, %d エッジ", master_count, edge_count)

    # ─── 未マージ (Unmerge) ──────────────────────────

    def can_unmerge(self, session_id: str) -> dict:
        """セッションの未マージが可能か判定する。

        Returns: {"ok": bool, "reason": str | None}
        """
        # session_graphs に存在するか
        sg = self._conn.execute(
            "SELECT session_id, built_at FROM lc_session_graphs"
            " WHERE session_id = ? AND version_id = ?",
            (session_id, self._version_id),
        ).fetchone()
        if not sg:
            return {"ok": False, "reason": "セッショングラフが存在しません"}
        if not sg["built_at"]:
            return {"ok": False, "reason": "未マージのセッションです"}

        # node_mappings にマッピングがあるか
        mapping_count = self._conn.execute(
            "SELECT COUNT(*) FROM lc_node_mappings WHERE session_id = ?"
            " AND version_id = ?",
            (session_id, self._version_id),
        ).fetchone()[0]
        if mapping_count == 0:
            return {"ok": False, "reason": "マージ記録（node_mappings）がありません"}

        # 残りのマージ済みセッションの transitions/screens が存在するか
        other_sessions = self._conn.execute(
            "SELECT sg.session_id FROM lc_session_graphs sg"
            " WHERE sg.session_id != ? AND sg.built_at IS NOT NULL"
            " AND sg.version_id = ?",
            (session_id, self._version_id),
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
                " FROM lc_master_nodes WHERE version_id = ?",
                (self._version_id,),
            )
            backup_count = self._conn.execute(
                "SELECT COUNT(*) FROM _unmerge_backup"
            ).fetchone()[0]
            logger.info("[Unmerge] バックアップ: %d ノード", backup_count)

            # 3. マスターグラフ全削除 (このバージョンのみ)
            self._conn.execute("DELETE FROM lc_master_nodes WHERE version_id = ?",
                               (self._version_id,))
            self._conn.execute("DELETE FROM lc_master_edges WHERE version_id = ?",
                               (self._version_id,))
            self._conn.execute("DELETE FROM lc_node_mappings WHERE version_id = ?",
                               (self._version_id,))
            self._conn.commit()

            # 4. 対象セッションを除外して再構築
            sessions = self._conn.execute(
                "SELECT session_id FROM lc_session_graphs"
                " WHERE session_id != ? AND built_at IS NOT NULL"
                " AND version_id = ?"
                " ORDER BY built_at",
                (session_id, self._version_id),
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
                " AND lc_master_nodes.version_id = ?",
                (self._version_id,),
            ).rowcount
            logger.info("[Unmerge] 手動変更復元: %d ノード", restored)

            # 6. orphan チェック: representative_screen_id の整合性
            orphans = self._conn.execute(
                "SELECT m.master_fp, m.representative_screen_id"
                " FROM lc_master_nodes m"
                " WHERE m.version_id = ?"
                "   AND m.representative_screen_id IS NOT NULL"
                "   AND NOT EXISTS ("
                "     SELECT 1 FROM lc_screens s"
                "     WHERE s.id = m.representative_screen_id)",
                (self._version_id,),
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
                    " WHERE master_fp = ? AND version_id = ?",
                    (new_id, orph["master_fp"], self._version_id),
                )
            if orphans:
                logger.info("[Unmerge] orphan 修復: %d ノード", len(orphans))

            # 7. 対象セッションの built_at をクリア（未マージ状態に戻す）
            self._conn.execute(
                "UPDATE lc_session_graphs SET built_at = NULL"
                " WHERE session_id = ? AND version_id = ?",
                (session_id, self._version_id),
            )

            self._conn.execute("DROP TABLE IF EXISTS _unmerge_backup")
            self._conn.commit()

            master_nodes = self._conn.execute(
                "SELECT COUNT(*) FROM lc_master_nodes WHERE version_id = ?",
                (self._version_id,),
            ).fetchone()[0]
            master_edges = self._conn.execute(
                "SELECT COUNT(*) FROM lc_master_edges WHERE version_id = ?",
                (self._version_id,),
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
        """最初のセッションをマスターにコピー。

        Raises: RuntimeError — Gemini OCR 補正が未完了の場合
        """
        self._check_ocr_complete(session_id)
        now = datetime.now().isoformat()

        # 代表画像をマスターノードにコピー (Gemini OCR 優先, is_artifact 除外)
        reps = self._conn.execute(
            "SELECT id, fingerprint, title, scene, phash,"
            "  COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr, discovered_at"
            " FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1"
            " AND COALESCE(is_artifact, 0) = 0",
            (session_id,),
        ).fetchall()

        for r in reps:
            self._conn.execute(
                "INSERT OR IGNORE INTO lc_master_nodes"
                " (master_fp, representative_screen_id, title, scene, phash,"
                "  ocr_text, visit_count, first_seen_at, last_seen_at, version_id)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (r["fingerprint"], r["id"], r["title"], r["scene"],
                 r["phash"], r["ocr"],
                 r["discovered_at"], now, self._version_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO lc_node_mappings"
                " (session_id, session_fp, master_fp, match_method, match_score, version_id)"
                " VALUES (?, ?, ?, 'seed', 1.0, ?)",
                (session_id, r["fingerprint"], r["fingerprint"], self._version_id),
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
                    "  edge_type, count, first_seen_at, last_seen_at, version_id)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (from_fp, to_fp, e["tap_label"],
                     e["action_name"], (e["edge_type"] if "edge_type" in e.keys() else "tap") or "tap",
                     e["discovered_at"], now, self._version_id),
                )

        # Seed: sort_order は first_seen_at 順 (位相ソートは使わない)
        # 初回セッションは一本道なので時系列 = ゲーム進行順
        sorted_nodes = self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes"
            " WHERE version_id = ? ORDER BY first_seen_at ASC",
            (self._version_id,),
        ).fetchall()
        for i, row in enumerate(sorted_nodes):
            self._conn.execute(
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ?"
                " AND version_id = ?",
                (i, row["master_fp"], self._version_id),
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
            "  COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr, discovered_at"
            " FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall()

        for r in reps:
            self._conn.execute(
                "INSERT OR IGNORE INTO lc_master_nodes"
                " (master_fp, representative_screen_id, title, scene, phash,"
                "  ocr_text, visit_count, first_seen_at, last_seen_at, version_id)"
                " VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (r["fingerprint"], r["id"], r["title"], r["scene"],
                 r["phash"], r["ocr"],
                 r["discovered_at"], now, self._version_id),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO lc_node_mappings"
                " (session_id, session_fp, master_fp, match_method, match_score, version_id)"
                " VALUES (?, ?, ?, 'new', 1.0, ?)",
                (session_id, r["fingerprint"], r["fingerprint"], self._version_id),
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
        """セッションのエッジをマスターにマージ (tap + auto 両方)。"""
        now = datetime.now().isoformat()
        edges = self._conn.execute(
            "SELECT from_fp, to_fp, tap_label, action_name, edge_type, discovered_at"
            " FROM lc_transitions"
            " WHERE session_id = ? AND to_fp IS NOT NULL",
            (session_id,),
        ).fetchall()

        # マッピング: session_fp → master_fp
        fp_map: dict[str, str] = {}
        for s_fp, (m_fp, _, _) in node_mapping.items():
            fp_map[s_fp] = m_fp
        # 新規ノード (session_fp == master_fp)
        new_nodes = self._conn.execute(
            "SELECT session_fp, master_fp FROM lc_node_mappings"
            " WHERE session_id = ? AND match_method IN ('new', 'seed')"
            " AND version_id = ?",
            (session_id, self._version_id),
        ).fetchall()
        for r in new_nodes:
            fp_map[r["session_fp"]] = r["master_fp"]

        master_fps = set(r["master_fp"] for r in self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
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
                " WHERE from_master_fp = ? AND to_master_fp = ? AND tap_label IS ?"
                " AND version_id = ?",
                (m_from, m_to, e["tap_label"], self._version_id),
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
                    "  edge_type, count, first_seen_at, last_seen_at, version_id)"
                    " VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (m_from, m_to, e["tap_label"], e["action_name"],
                     (e["edge_type"] if "edge_type" in e.keys() else "tap") or "tap",
                     e["discovered_at"], now, self._version_id),
                )

    def _recalculate_master_graph(self) -> None:
        """マスターグラフの BFS depth + SCC + sort_order を再計算。"""
        nodes = self._conn.execute(
            "SELECT master_fp FROM lc_master_nodes WHERE version_id = ?",
            (self._version_id,),
        ).fetchall()
        edges = self._conn.execute(
            "SELECT from_master_fp, to_master_fp, COALESCE(edge_type, 'tap') AS edge_type"
            " FROM lc_master_edges WHERE version_id = ?",
            (self._version_id,),
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
            " WHERE m.version_id = ?"
            " AND EXISTS (SELECT 1 FROM lc_master_edges e"
            "   WHERE e.version_id = ?"
            "   AND (e.from_master_fp = m.master_fp OR e.to_master_fp = m.master_fp))"
            " ORDER BY m.first_seen_at ASC LIMIT 1",
            (self._version_id, self._version_id),
        ).fetchone()
        home_fp = root["master_fp"] if root else None

        if not home_fp or home_fp not in G:
            # フォールバック: 出次数最大
            if G.number_of_nodes() > 0:
                home_fp = max(G.nodes(), key=lambda n: G.out_degree(n))

        # BFS depth (前回の値をリセット)
        self._conn.execute(
            "UPDATE lc_master_nodes SET bfs_depth = NULL, scc_id = NULL, scc_label = NULL"
            " WHERE version_id = ?",
            (self._version_id,),
        )
        depth_map: dict[str, int] = {}
        if home_fp and home_fp in G:
            undirected = G.to_undirected()
            depth_map[home_fp] = 0
            for parent, child in nx.bfs_edges(undirected, home_fp):
                depth_map[child] = depth_map[parent] + 1

        for fp, depth in depth_map.items():
            self._conn.execute(
                "UPDATE lc_master_nodes SET bfs_depth = ? WHERE master_fp = ?"
                " AND version_id = ?",
                (depth, fp, self._version_id),
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
                    "UPDATE lc_master_nodes SET scc_id = ?, scc_label = ? WHERE master_fp = ?"
                    " AND version_id = ?",
                    (idx, f"SCC#{idx}", fp, self._version_id),
                )

        # sort_order: 位相ソート (チュートリアル進行順)
        # SCC を condensation し、位相ソートで順序決定。
        # SCC 内は「入口ノードからの DFS 順」(マージ時期に依存しない)。
        # 接続なしノードは first_seen_at で末尾に。
        first_seen_map: dict[str, str] = {
            r["master_fp"]: r["first_seen_at"] or ""
            for r in self._conn.execute(
                "SELECT master_fp, first_seen_at FROM lc_master_nodes"
                " WHERE version_id = ?",
                (self._version_id,),
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
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ?"
                " AND version_id = ?",
                (i, fp, self._version_id),
            )

    def _get_node_info(self, fp: str, session_id: str) -> Optional[dict]:
        """セッション内のノード情報を取得 (Gemini OCR 優先)。"""
        row = self._conn.execute(
            "SELECT title, scene, phash,"
            " COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr"
            " FROM lc_screens"
            " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
            (fp, session_id),
        ).fetchone()
        if not row:
            # 代表でなくても取得
            row = self._conn.execute(
                "SELECT title, scene, phash,"
                " COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr"
                " FROM lc_screens"
                " WHERE fingerprint = ? AND session_id = ?",
                (fp, session_id),
            ).fetchone()
        if not row:
            return None
        return {
            "title": row["title"] or "",
            "ocr_text": row["ocr"] or "",
            "phash": row["phash"] or "",
            "scene": row["scene"] or "",
        }

    def _get_master_node_info(self, fp: str) -> Optional[dict]:
        """マスターノードの情報を取得。"""
        row = self._conn.execute(
            "SELECT title, scene, phash, ocr_text FROM lc_master_nodes"
            " WHERE master_fp = ? AND version_id = ?",
            (fp, self._version_id),
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
            "SELECT id, title, thumbnail_path, screenshot_path, scene,"
            " discovered_at, COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr_text"
            " FROM lc_screens"
            " WHERE fingerprint = ? AND session_id = ? AND is_representative = 1",
            (fp, session_id),
        ).fetchone()
        if not row:
            row = self._conn.execute(
                "SELECT id, title, thumbnail_path, screenshot_path, scene,"
                " discovered_at, COALESCE(ocr_text_gemini, ocr_text_hq, ocr_text, '') AS ocr_text"
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
            " WHERE m.master_fp = ? AND m.version_id = ?",
            (master_fp, self._version_id),
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
                f"   AND m.version_id = ?"
                f" LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
                f" WHERE e.{col_from} = ? AND e.version_id = ?",
                (self._version_id, master_fp, self._version_id),
            ).fetchall()
            for r in rows:
                neighbors.append({
                    "direction": direction,
                    "fp": r["fp"],
                    "title": r["title"] or "",
                    "thumb": r["thumbnail_path"] or "",
                })
        return neighbors

    def _get_neighbors_batch(self, fps: list[str], session_id: str) -> dict[str, list[dict]]:
        """セッション内の遷移先/遷移元を一括取得。"""
        result: dict[str, list[dict]] = {fp: [] for fp in fps}
        if not fps:
            return result

        # 1) セッションの代表画面の title/thumbnail を一括ルックアップ
        screen_info: dict[str, tuple[str, str]] = {}
        for r in self._conn.execute(
            "SELECT fingerprint, title, thumbnail_path FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1",
            (session_id,),
        ).fetchall():
            screen_info[r["fingerprint"]] = (r["title"] or "", r["thumbnail_path"] or "")

        # 2) 該当セッションの全遷移を一括取得 (JOIN なし)
        rows = self._conn.execute(
            "SELECT DISTINCT from_fp, to_fp FROM lc_transitions"
            " WHERE session_id = ?",
            (session_id,),
        ).fetchall()

        fp_set = set(fps)
        for r in rows:
            from_fp = r["from_fp"]
            to_fp = r["to_fp"]
            if from_fp in fp_set and to_fp:
                info = screen_info.get(to_fp, ("", ""))
                result[from_fp].append({
                    "direction": "to", "fp": to_fp,
                    "title": info[0], "thumb": info[1],
                })
            if to_fp in fp_set and from_fp:
                info = screen_info.get(from_fp, ("", ""))
                result[to_fp].append({
                    "direction": "from", "fp": from_fp,
                    "title": info[0], "thumb": info[1],
                })

        # 重複除去
        for fp in result:
            seen = set()
            deduped = []
            for n in result[fp]:
                key = (n["direction"], n["fp"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(n)
            result[fp] = deduped

        return result

    def _get_master_neighbors_batch(self, master_fps: list[str]) -> dict[str, list[dict]]:
        """マスターグラフの遷移先/遷移元を一括取得。"""
        result: dict[str, list[dict]] = {fp: [] for fp in master_fps}
        if not master_fps:
            return result

        # 1) マスターノードの title/thumbnail を一括ルックアップ
        node_info: dict[str, tuple[str, str]] = {}
        for r in self._conn.execute(
            "SELECT m.master_fp, m.title, s.thumbnail_path"
            " FROM lc_master_nodes m"
            " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
            " WHERE m.version_id = ?",
            (self._version_id,),
        ).fetchall():
            node_info[r["master_fp"]] = (r["title"] or "", r["thumbnail_path"] or "")

        # 2) 全マスターエッジを一括取得 (JOIN なし)
        rows = self._conn.execute(
            "SELECT DISTINCT from_master_fp, to_master_fp FROM lc_master_edges"
            " WHERE version_id = ?",
            (self._version_id,),
        ).fetchall()

        fp_set = set(master_fps)
        for r in rows:
            from_fp = r["from_master_fp"]
            to_fp = r["to_master_fp"]
            if from_fp in fp_set and to_fp:
                info = node_info.get(to_fp, ("", ""))
                result[from_fp].append({
                    "direction": "to", "fp": to_fp,
                    "title": info[0], "thumb": info[1],
                })
            if to_fp in fp_set and from_fp:
                info = node_info.get(from_fp, ("", ""))
                result[to_fp].append({
                    "direction": "from", "fp": from_fp,
                    "title": info[0], "thumb": info[1],
                })

        # 重複除去
        for fp in result:
            seen = set()
            deduped = []
            for n in result[fp]:
                key = (n["direction"], n["fp"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(n)
            result[fp] = deduped

        return result

