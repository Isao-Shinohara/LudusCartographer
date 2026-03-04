"""
organize_screenshots.py — 探索済みスクリーンショットを階層構造で整理するユーティリティ

【使い方】
  cd crawler
  venv/bin/python tools/organize_screenshots.py evidence/20260304_120000/
  # → evidence/20260304_120000/organized/ に階層コピーを生成

  # 出力先指定
  venv/bin/python tools/organize_screenshots.py evidence/20260304_120000/ --output /tmp/shots

  # dry-run (コピーせず一覧表示)
  venv/bin/python tools/organize_screenshots.py evidence/20260304_120000/ --dry-run
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Optional


def _slugify(title: str, max_len: int = 30) -> str:
    """タイトルをディレクトリ名に使える slug に変換する。"""
    slug = re.sub(r'[\\/:*?"<>|\s]', '_', title)
    return slug[:max_len].strip("_") or "unknown"


def _build_title_chain(fp: str, fp_map: dict, visited: set) -> list[str]:
    """fingerprint からルートまでのタイトルリスト (root first) を返す。"""
    chain: list[str] = []
    cur = fp
    while cur and cur not in visited:
        visited.add(cur)
        rec = fp_map.get(cur)
        if rec is None:
            break
        chain.insert(0, rec["title"])
        cur = rec.get("parent_fp")
    return chain


def organize_screenshots(
    session_dir: Path,
    output_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """
    crawl_summary.json を元にスクリーンショットを階層ディレクトリに整理する。

    Args:
        session_dir: evidence セッションディレクトリ (crawl_summary.json が存在する)
        output_dir:  出力先ディレクトリ (省略時は session_dir/organized/)
        dry_run:     True のときはファイルコピーを行わず一覧のみ表示

    Returns:
        {"copied": int, "skipped": int, "index_path": str}
    """
    summary_path = session_dir / "crawl_summary.json"
    if not summary_path.exists():
        raise FileNotFoundError(f"crawl_summary.json が見つかりません: {summary_path}")

    with summary_path.open(encoding="utf-8") as f:
        summary = json.load(f)

    screens = summary.get("screens", [])
    if not screens:
        print("[organize] 画面データが空です")
        return {"copied": 0, "skipped": 0, "index_path": ""}

    # fp → {title, parent_fp, screenshot_path, depth} マッピング構築
    fp_map: dict[str, dict] = {}
    for s in screens:
        fp = s.get("fingerprint", "")
        if fp:
            fp_map[fp] = {
                "title":           s.get("title", "unknown"),
                "parent_fp":       s.get("parent_fp"),
                "screenshot_path": s.get("screenshot_path", ""),
                "depth":           s.get("depth", 0),
            }

    if output_dir is None:
        output_dir = session_dir / "organized"

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    skipped = 0
    index_rows: list[str] = []
    index_rows.append("# Organized Screenshots\n")
    index_rows.append("| # | タイトル | 深さ | パス |")
    index_rows.append("|---|---------|------|------|")

    for i, (fp, rec) in enumerate(fp_map.items(), 1):
        chain = _build_title_chain(fp, fp_map, set())
        if not chain:
            chain = [rec["title"]]

        # 階層ディレクトリパスを構築
        slug_parts = [_slugify(t) for t in chain]
        dest_dir = output_dir
        for part in slug_parts[:-1]:  # 最後のタイトルはファイル名に使う
            dest_dir = dest_dir / part

        title_slug = _slugify(rec["title"])
        dest_file = dest_dir / f"{fp[:8]}_{title_slug}.png"

        src_path_str = rec.get("screenshot_path", "")
        src_path = Path(src_path_str) if src_path_str else None

        rel_path = str(dest_file.relative_to(output_dir))
        index_rows.append(f"| {i} | {rec['title']} | {rec['depth']} | `{rel_path}` |")

        if dry_run:
            print(f"[dry-run] {rec['title']!r}  →  {dest_file}")
            continue

        if src_path is None or not src_path.exists():
            print(f"[organize] スキップ (元ファイルなし): {src_path_str!r}")
            skipped += 1
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_file)
        copied += 1

    # organized_index.md を書き出す
    index_path = output_dir / "organized_index.md"
    if not dry_run:
        index_path.write_text("\n".join(index_rows) + "\n", encoding="utf-8")
        print(f"[organize] 完了: {copied} コピー / {skipped} スキップ")
        print(f"[organize] インデックス: {index_path}")
    else:
        print(f"[dry-run] {len(fp_map)} 画面を整理予定")

    return {
        "copied":     copied,
        "skipped":    skipped,
        "index_path": str(index_path) if not dry_run else "",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="explore された画面のスクリーンショットを階層ディレクトリに整理する",
    )
    parser.add_argument(
        "session_dir",
        type=Path,
        help="evidence セッションディレクトリ (crawl_summary.json が存在するディレクトリ)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="DIR",
        help="出力先ディレクトリ (省略時は <session_dir>/organized/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="コピーを行わず整理予定一覧のみ表示する",
    )
    args = parser.parse_args()

    try:
        organize_screenshots(
            session_dir=args.session_dir.resolve(),
            output_dir=args.output.resolve() if args.output else None,
            dry_run=args.dry_run,
        )
    except FileNotFoundError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
