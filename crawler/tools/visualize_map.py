"""
visualize_map.py — クロール結果の遷移マップ可視化ツール

【機能】
  - crawl_summary.json から画面遷移グラフを構築
  - Mermaid graph TD / ASCII ツリー / ギャップ分析を出力
  - MySQL の screens/ui_elements テーブルからも読み込み可能 (--db)

【使用例】
  # 最新 evidence から全フォーマット表示
  python tools/visualize_map.py --format all

  # 特定セッション
  python tools/visualize_map.py --session evidence/20260303_160759 --format mermaid

  # DB から読み込み
  python tools/visualize_map.py --db --host localhost --format tree
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional


# ============================================================
# データ読み込み
# ============================================================

def load_summary(session_dir: Path) -> list[dict]:
    """
    crawl_summary.json を読み込んで画面リストを返す。

    Args:
        session_dir: crawl_summary.json が置かれているセッションディレクトリ

    Returns:
        画面情報 dict のリスト (fingerprint/title/depth/parent_fp/tappable_items/phash 等)

    Raises:
        FileNotFoundError: crawl_summary.json が存在しない場合
    """
    summary_file = session_dir / "crawl_summary.json"
    if not summary_file.exists():
        raise FileNotFoundError(
            f"crawl_summary.json が見つかりません: {summary_file}\n"
            "クローラーを実行後にこのツールを使用してください。"
        )
    data = json.loads(summary_file.read_text(encoding="utf-8"))
    return data.get("screens", [])


def load_from_db(
    host: str,
    port: int = 3306,
    db_name: str = "ludus_cartographer",
    user: str = "root",
    password: str = "",
    game_id: int = 1,
) -> list[dict]:
    """
    MySQL の screens / ui_elements テーブルから画面情報を読み込む。

    Returns:
        画面情報 dict のリスト (load_summary と同形式)
    """
    try:
        import pymysql
    except ImportError:
        raise ImportError("pymysql が必要です: pip install pymysql")

    conn = pymysql.connect(
        host=host, port=port, db=db_name,
        user=user, password=password,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    screens = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT screen_hash, name, ocr_text, screenshot_path "
                "FROM screens WHERE game_id=%s ORDER BY id",
                (game_id,),
            )
            for row in cur.fetchall():
                # DB は parent_fp/depth 情報を持たないため 0 / None で補完
                screens.append({
                    "fingerprint":    row["screen_hash"],
                    "title":          row["name"] or "unknown",
                    "depth":          0,
                    "parent_fp":      None,
                    "tappable_items": [],
                    "phash":          None,
                    "screenshot_path": row["screenshot_path"] or "",
                    "discovered_at":  "",
                })
    finally:
        conn.close()
    return screens


# ============================================================
# グラフ構築
# ============================================================

def build_graph(screens: list[dict]) -> dict:
    """
    画面リストから遷移グラフを構築する。

    Returns:
        {
            "nodes": {fingerprint: screen_dict, ...},
            "edges": [(parent_fp, child_fp), ...]
        }
    """
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str]] = []

    for s in screens:
        fp = s["fingerprint"]
        nodes[fp] = s
        if s.get("parent_fp"):
            edges.append((s["parent_fp"], fp))

    return {"nodes": nodes, "edges": edges}


# ============================================================
# Mermaid レンダリング
# ============================================================

def render_mermaid(graph: dict) -> str:
    """
    Mermaid graph TD テキストを生成する。

    出力例:
        graph TD
            n0["一般 (d=0) | 9items"]
            n1["情報 (d=1) | 11items"]
            n0 --> n1
            style n2 fill:#ffcccc
    """
    nodes = graph["nodes"]
    edges = graph["edges"]

    # fingerprint → 短い ID (n0, n1, ...) のマッピング
    fp_to_id = {fp: f"n{i}" for i, fp in enumerate(nodes)}

    lines = ["graph TD"]

    # ノード定義
    for fp, screen in nodes.items():
        nid = fp_to_id[fp]
        title = screen["title"]
        if title == "unknown":
            title = "❓unknown"
        items = len(screen.get("tappable_items", []))
        item_str = f"{items} item{'s' if items != 1 else ''}"
        label = f"{title} (d={screen['depth']}) | {item_str}"
        lines.append(f'    {nid}["{label}"]')

    # エッジ定義
    for parent_fp, child_fp in edges:
        if parent_fp in fp_to_id and child_fp in fp_to_id:
            lines.append(f"    {fp_to_id[parent_fp]} --> {fp_to_id[child_fp]}")

    # unknown ノードのスタイル
    for fp, screen in nodes.items():
        if screen["title"] == "unknown":
            lines.append(f"    style {fp_to_id[fp]} fill:#ffcccc")

    return "\n".join(lines)


# ============================================================
# ASCII ツリーレンダリング
# ============================================================

def render_tree(graph: dict, root_fp: Optional[str] = None) -> str:
    """
    ASCII ツリーを生成する。

    出力例:
        📱 一般 [depth=0] (9 items)
        ├── 情報 [depth=1] (11 items)
        └── ❓ unknown [depth=1] (0 items)  ← 要調査
    """
    nodes = graph["nodes"]
    edges = graph["edges"]

    # 子ノードマップを構築
    children: dict[str, list[str]] = {fp: [] for fp in nodes}
    has_parent: set[str] = set()
    for parent_fp, child_fp in edges:
        if parent_fp in children:
            children[parent_fp].append(child_fp)
        has_parent.add(child_fp)

    # ルートノードを決定
    if root_fp is not None:
        roots = [root_fp] if root_fp in nodes else []
    else:
        roots = [fp for fp in nodes if fp not in has_parent]
    if not roots:
        roots = list(nodes.keys())[:1]

    lines: list[str] = []

    def _node_line(fp: str, prefix: str, connector: str) -> None:
        screen = nodes[fp]
        title = screen["title"]
        depth = screen["depth"]
        items = len(screen.get("tappable_items", []))
        item_str = f"{items} item{'s' if items != 1 else ''}"

        warnings: list[str] = []
        display_title = title
        if title == "unknown":
            display_title = "❓ unknown"
            warnings.append("← 要調査")
        elif items <= 1 and depth > 0:
            warnings.append("⚠ タップ候補少")

        warn_str = f"  {warnings[0]}" if warnings else ""
        lines.append(f"{prefix}{connector}{display_title} [depth={depth}] ({item_str}){warn_str}")

        child_fps = children.get(fp, [])
        extension = "    " if connector == "└── " else "│   "
        for i, child_fp in enumerate(child_fps):
            is_last = (i == len(child_fps) - 1)
            _node_line(child_fp, prefix + extension, "└── " if is_last else "├── ")

    for root in roots:
        screen = nodes[root]
        title = screen["title"]
        depth = screen["depth"]
        items = len(screen.get("tappable_items", []))
        item_str = f"{items} item{'s' if items != 1 else ''}"
        lines.append(f"📱 {title} [depth={depth}] ({item_str})")

        child_fps = children.get(root, [])
        for i, child_fp in enumerate(child_fps):
            is_last = (i == len(child_fps) - 1)
            _node_line(child_fp, "", "└── " if is_last else "├── ")

    return "\n".join(lines)


# ============================================================
# ギャップ分析
# ============================================================

def analyze_gaps(screens: list[dict]) -> dict:
    """
    クロール結果のギャップ（問題箇所）を分析する。

    Returns:
        {
            "unknown_screens":   [screen, ...],  # title == "unknown"
            "suspicious_titles": [screen, ...],  # 1文字 or 記号のみ
            "untapped_screens":  [screen, ...],  # tappable_items が空
        }
    """
    _SUSPICIOUS = frozenset({'>', '›', '<', '‹', '7', 'L', 'Q', '…', '-', '_'})

    unknown_screens:   list[dict] = []
    suspicious_titles: list[dict] = []
    untapped_screens:  list[dict] = []

    for s in screens:
        title = s.get("title", "")
        items = s.get("tappable_items", [])

        if title == "unknown":
            unknown_screens.append(s)
        elif len(title) <= 1 or title.strip() in _SUSPICIOUS:
            suspicious_titles.append(s)

        if len(items) == 0:
            untapped_screens.append(s)

    return {
        "unknown_screens":   unknown_screens,
        "suspicious_titles": suspicious_titles,
        "untapped_screens":  untapped_screens,
    }


def format_gaps(gaps: dict) -> str:
    """analyze_gaps の結果を人間が読める形式にフォーマットする。"""
    lines = ["=== ギャップ分析レポート ==="]

    def _section(label: str, screens: list[dict]) -> None:
        lines.append(f"\n[{label}] {len(screens)} 件")
        for s in screens:
            lines.append(
                f"  - {s.get('title', '?')!r}"
                f"  depth={s.get('depth', '?')}"
                f"  fp={str(s.get('fingerprint', ''))[:8]}…"
            )

    _section("❓ unknown タイトル", gaps["unknown_screens"])
    _section("⚠ 疑惑タイトル", gaps["suspicious_titles"])
    _section("🔇 タップ候補なし", gaps["untapped_screens"])
    return "\n".join(lines)


# ============================================================
# CLI エントリポイント
# ============================================================

def _find_latest_session(base_dir: Path) -> Optional[Path]:
    """evidence/ 内で最新の crawl_summary.json を持つセッションを返す。"""
    summaries = sorted(base_dir.glob("*/crawl_summary.json"), reverse=True)
    return summaries[0].parent if summaries else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="クロール結果の遷移マップ可視化ツール",
    )
    parser.add_argument(
        "--session", "-s",
        help="セッションディレクトリ (デフォルト: evidence/ 最新)",
    )
    parser.add_argument(
        "--db", action="store_true",
        help="MySQL から読み込む",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("DB_HOST", "127.0.0.1"),
        help="DB ホスト (デフォルト: DB_HOST 環境変数 or 127.0.0.1)",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["mermaid", "tree", "gaps", "all"],
        default="all",
        help="出力フォーマット (デフォルト: all)",
    )

    args = parser.parse_args()

    # --- データ読み込み ---
    if args.db:
        screens = load_from_db(host=args.host)
    else:
        # session_dir の決定
        if args.session:
            session_dir = Path(args.session)
        else:
            # スクリプトの場所から evidence/ を探す
            base = Path(__file__).parent.parent / "evidence"
            session_dir = _find_latest_session(base)
            if session_dir is None:
                print("ERROR: evidence/ に crawl_summary.json が見つかりません。", file=sys.stderr)
                print("クローラーを先に実行してください。", file=sys.stderr)
                sys.exit(1)
            print(f"[INFO] セッション: {session_dir}")

        screens = load_summary(session_dir)

    if not screens:
        print("ERROR: 画面情報が 0 件です。", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] {len(screens)} 画面を読み込みました\n")

    graph = build_graph(screens)
    fmt   = args.format

    # Mermaid
    if fmt in ("mermaid", "all"):
        print("--- Mermaid ---")
        print(render_mermaid(graph))
        print()

    # ASCII ツリー
    if fmt in ("tree", "all"):
        root_fps = [s["fingerprint"] for s in screens if s.get("parent_fp") is None]
        root_fp  = root_fps[0] if root_fps else None
        print("--- ASCII Tree ---")
        print(render_tree(graph, root_fp))
        print()

    # ギャップ分析
    if fmt in ("gaps", "all"):
        gaps = analyze_gaps(screens)
        print(format_gaps(gaps))


if __name__ == "__main__":
    main()
