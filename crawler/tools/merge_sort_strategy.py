"""
merge_sort_strategy.py — マージ時の sort_order 決定戦略

差し替え可能な設計:
  MergeSortStrategy ABC を継承して新しい戦略を作成し、
  CrossSessionMerger に渡すだけで切り替え可能。
"""
from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MergeSortResult:
    """sort_order 計算結果。"""
    inserts: list[tuple[str, float]]  # [(master_fp, sort_position), ...]
    skipped: list[str]                # 挿入しなかった master_fp のリスト


class MergeSortStrategy(ABC):
    """マージ時の sort_order 決定戦略の抽象基底クラス。"""

    @abstractmethod
    def compute_sort_order(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        node_mapping: dict[str, tuple[str, str, float]],
    ) -> MergeSortResult:
        """新規ノードの sort_order を計算する。

        Args:
            conn: DB 接続
            session_id: マージ対象のセッション ID
            node_mapping: session_fp → (master_fp, method, score)

        Returns:
            MergeSortResult
        """
        ...


class SafeInsertStrategy(MergeSortStrategy):
    """安全挿入方式: 隣接アンカー条件を満たす場合のみ挿入。

    原則:
    1. 挿入されたノードの順序は 100% 正しい
    2. 不確実な位置には挿入しない
    3. 一度配置されたノードの sort_order は変更しない
    4. 周回を重ねてアンカーが密になれば挿入可能な位置が増える
    """

    def compute_sort_order(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        node_mapping: dict[str, tuple[str, str, float]],
    ) -> MergeSortResult:
        # 現在の sort_order マップ
        sort_orders: dict[str, int] = {
            r["master_fp"]: r["sort_order"]
            for r in conn.execute(
                "SELECT master_fp, sort_order FROM lc_master_nodes"
            ).fetchall()
        }
        if not sort_orders:
            return MergeSortResult(inserts=[], skipped=[])

        max_sort = max(sort_orders.values())
        min_sort = min(sort_orders.values())

        # sort_order のセット (隣接判定用)
        occupied = set(sort_orders.values())

        # 新規ノードの master_fp を特定
        new_fps = {
            r["master_fp"]
            for r in conn.execute(
                "SELECT master_fp FROM lc_node_mappings"
                " WHERE session_id = ? AND match_method = 'new'",
                (session_id,),
            ).fetchall()
        }
        if not new_fps:
            return MergeSortResult(inserts=[], skipped=[])

        # session_fp → master_fp
        all_mappings = {
            r["session_fp"]: r["master_fp"]
            for r in conn.execute(
                "SELECT session_fp, master_fp FROM lc_node_mappings"
                " WHERE session_id = ?", (session_id,),
            ).fetchall()
        }

        # セッション画面を時系列順に取得
        session_screens = conn.execute(
            "SELECT fingerprint FROM lc_screens"
            " WHERE session_id = ? AND is_representative = 1"
            " ORDER BY discovered_at ASC",
            (session_id,),
        ).fetchall()

        # アンカー列を構築 (セッション時系列順でマッチしたノード)
        anchor_sequence = self._build_anchor_sequence(
            session_screens, all_mappings, new_fps, sort_orders
        )

        # 新規ノードの挿入位置を計算
        inserts: list[tuple[str, float]] = []
        skipped: list[str] = []

        # セッション時系列を走査
        pending_new: list[str] = []  # 現在のアンカー区間の新規ノード
        prev_anchor_sort: Optional[int] = None
        prev_anchor_is_first = True  # 最初のアンカーより前か

        seen_new = set()

        for screen in session_screens:
            m_fp = all_mappings.get(screen["fingerprint"])
            if not m_fp:
                continue

            if m_fp in new_fps and m_fp not in seen_new:
                # 新規ノード → pending に追加
                pending_new.append(m_fp)
                seen_new.add(m_fp)

            elif m_fp in sort_orders and m_fp not in new_fps:
                # アンカーに到達 → pending の新規ノードを処理
                cur_anchor_sort = sort_orders[m_fp]

                if pending_new:
                    if prev_anchor_sort is None:
                        # 最初のアンカー前の新規ノード → 先頭挿入
                        if cur_anchor_sort == min_sort:
                            for i, fp in enumerate(pending_new):
                                inserts.append((fp, min_sort - len(pending_new) + i))
                        else:
                            skipped.extend(pending_new)
                    elif cur_anchor_sort == prev_anchor_sort + 1:
                        # 隣接アンカー → 間に挿入
                        gap = 1.0 / (len(pending_new) + 1)
                        for i, fp in enumerate(pending_new):
                            inserts.append((fp, prev_anchor_sort + gap * (i + 1)))
                    elif prev_anchor_sort == max_sort:
                        # 前のアンカーが末尾 → 末尾追加
                        for i, fp in enumerate(pending_new):
                            inserts.append((fp, max_sort + i + 1))
                        max_sort = max_sort + len(pending_new)
                    else:
                        # アンカーが離れている → スキップ
                        skipped.extend(pending_new)

                    pending_new = []

                prev_anchor_sort = cur_anchor_sort
                prev_anchor_is_first = False

        # 末尾に残った新規ノード
        if pending_new:
            if prev_anchor_sort is not None and prev_anchor_sort == max_sort:
                # 前のアンカーが末尾 → 末尾追加
                for i, fp in enumerate(pending_new):
                    inserts.append((fp, max_sort + i + 1))
            elif prev_anchor_sort is not None:
                skipped.extend(pending_new)
            else:
                skipped.extend(pending_new)

        # seen されなかった new_fps → スキップ
        for fp in new_fps:
            if fp not in seen_new:
                skipped.append(fp)

        logger.info(
            "[SafeInsert] session=%s: %d 挿入, %d スキップ (アンカー %d 個)",
            session_id, len(inserts), len(skipped), len(anchor_sequence),
        )

        return MergeSortResult(inserts=inserts, skipped=skipped)

    @staticmethod
    def _build_anchor_sequence(
        session_screens: list,
        all_mappings: dict[str, str],
        new_fps: set[str],
        sort_orders: dict[str, int],
    ) -> list[tuple[str, int]]:
        """セッション時系列順のアンカー列を構築。"""
        anchors: list[tuple[str, int]] = []
        seen = set()
        for screen in session_screens:
            m_fp = all_mappings.get(screen["fingerprint"])
            if not m_fp or m_fp in new_fps or m_fp in seen:
                continue
            if m_fp in sort_orders:
                anchors.append((m_fp, sort_orders[m_fp]))
                seen.add(m_fp)
        return anchors


def renumber_sort_orders(conn: sqlite3.Connection) -> None:
    """全マスターノードの sort_order を 0 から連番で振り直す。

    既存ノードの相対順序は不変。
    """
    rows = conn.execute(
        "SELECT master_fp, sort_order FROM lc_master_nodes ORDER BY sort_order ASC"
    ).fetchall()
    for i, r in enumerate(rows):
        if r["sort_order"] != i:
            conn.execute(
                "UPDATE lc_master_nodes SET sort_order = ? WHERE master_fp = ?",
                (i, r["master_fp"]),
            )
