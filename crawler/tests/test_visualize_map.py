"""
test_visualize_map.py — 可視化ツールのユニットテスト (Appium 不要)

テスト方針:
  - モックデータを使用。Appium / MySQL 不要。
  - phash テストは numpy + cv2 でダミー画像を生成。
  - integration テストは evidence/ 最新セッションがあれば実行。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

# crawler/ ディレクトリを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.visualize_map import (
    analyze_gaps,
    build_graph,
    load_summary,
    render_mermaid,
    render_tree,
)
from lc.utils import compute_phash, phash_distance


# ============================================================
# テストフィクスチャ
# ============================================================

MOCK_SCREENS: list[dict] = [
    {
        "fingerprint":    "aaa1aaa1",
        "title":          "一般",
        "depth":          0,
        "parent_fp":      None,
        "tappable_items": [
            {"text": "情報",       "confidence": 0.95},
            {"text": "キーボード",  "confidence": 0.92},
            {"text": "unknown",    "confidence": 0.88},
        ],
        "phash":           "0000000000000001",
        "screenshot_path": "/tmp/a.png",
        "discovered_at":   "2026-03-03T00:00:00",
    },
    {
        "fingerprint":    "bbb2bbb2",
        "title":          "情報",
        "depth":          1,
        "parent_fp":      "aaa1aaa1",
        "tappable_items": [
            {"text": "名前",         "confidence": 0.97},
            {"text": "iOSバージョン", "confidence": 0.91},
        ],
        "phash":           "0000000000000002",
        "screenshot_path": "/tmp/b.png",
        "discovered_at":   "2026-03-03T00:00:01",
    },
    {
        "fingerprint":    "ccc3ccc3",
        "title":          "unknown",
        "depth":          1,
        "parent_fp":      "aaa1aaa1",
        "tappable_items": [],
        "phash":           "0000000000000003",
        "screenshot_path": "/tmp/c.png",
        "discovered_at":   "2026-03-03T00:00:02",
    },
]


# ============================================================
# A. グラフ構築テスト
# ============================================================

def test_build_graph():
    """3 画面のモックデータから graph edges が正しく生成されるか。"""
    graph = build_graph(MOCK_SCREENS)

    assert "nodes" in graph
    assert "edges" in graph

    nodes = graph["nodes"]
    edges = graph["edges"]

    # ノード数
    assert len(nodes) == 3
    assert "aaa1aaa1" in nodes
    assert "bbb2bbb2" in nodes
    assert "ccc3ccc3" in nodes

    # エッジ: 一般 → 情報, 一般 → unknown
    assert ("aaa1aaa1", "bbb2bbb2") in edges
    assert ("aaa1aaa1", "ccc3ccc3") in edges

    # 根ノード (parent_fp=None) にエッジの親はない
    parent_fps = {e[0] for e in edges}
    assert "bbb2bbb2" not in parent_fps  # 情報は親になっていない
    assert "ccc3ccc3" not in parent_fps  # unknown は親になっていない


# ============================================================
# B. Mermaid レンダリングテスト
# ============================================================

def test_render_mermaid():
    """Mermaid 文字列に 'graph TD' と正しいノード・エッジが含まれるか。"""
    graph   = build_graph(MOCK_SCREENS)
    mermaid = render_mermaid(graph)

    # ヘッダー
    assert "graph TD" in mermaid

    # タイトル文字列
    assert "一般" in mermaid
    assert "情報" in mermaid
    assert "❓unknown" in mermaid  # unknown は ❓ プレフィックス付き

    # エッジ (-->)
    assert "-->" in mermaid

    # unknown ノードのスタイル指定
    assert "fill:#ffcccc" in mermaid

    # 深さ情報
    assert "d=0" in mermaid
    assert "d=1" in mermaid


# ============================================================
# C. ASCII ツリーレンダリングテスト
# ============================================================

def test_render_tree():
    """ASCII ツリーの行数・インデントが正しいか。"""
    graph  = build_graph(MOCK_SCREENS)
    tree   = render_tree(graph, root_fp="aaa1aaa1")
    lines  = tree.splitlines()

    # ルート行は 📱 から始まる
    assert lines[0].startswith("📱 一般")

    # 子ノードが 2 件 (情報, unknown)
    child_lines = [l for l in lines if "├──" in l or "└──" in l]
    assert len(child_lines) == 2

    # 「❓ unknown」行に「← 要調査」が含まれる
    unknown_line = next((l for l in lines if "❓" in l), None)
    assert unknown_line is not None
    assert "要調査" in unknown_line

    # インデントが一貫している (子ノードはすべてスペースで始まる)
    for line in child_lines:
        # ├── または └── が行の先頭（インデントなし）にある
        stripped = line.lstrip()
        assert stripped.startswith("├──") or stripped.startswith("└──")


# ============================================================
# D. ギャップ分析テスト
# ============================================================

def test_analyze_gaps():
    """unknown / 疑惑タイトル / タップ候補なし が正しく抽出されるか。"""
    gaps = analyze_gaps(MOCK_SCREENS)

    # unknown タイトル
    assert len(gaps["unknown_screens"]) == 1
    assert gaps["unknown_screens"][0]["fingerprint"] == "ccc3ccc3"

    # 疑惑タイトル (「一般」「情報」は正常なので 0 件)
    assert len(gaps["suspicious_titles"]) == 0

    # タップ候補なし (unknown 画面だけ tappable_items=[] )
    assert len(gaps["untapped_screens"]) == 1
    assert gaps["untapped_screens"][0]["fingerprint"] == "ccc3ccc3"


# ============================================================
# E. phash テスト
# ============================================================

def _make_image(pattern: str, size: int = 64) -> "numpy.ndarray":
    """テスト用ダミー画像 (グレースケール) を生成する。"""
    import numpy as np
    img = np.zeros((size, size), dtype=np.uint8)
    if pattern == "white":
        img[:] = 255
    elif pattern == "black":
        img[:] = 0
    elif pattern == "gradient":
        for i in range(size):
            img[i, :] = min(255, i * 4)
    elif pattern == "checker":
        for i in range(size):
            for j in range(size):
                img[i, j] = 255 if (i // 8 + j // 8) % 2 == 0 else 0
    return img


def _save_image(img: "numpy.ndarray", path: Path) -> None:
    import cv2
    cv2.imwrite(str(path), img)


def test_compute_phash_basic(tmp_path):
    """phash が 16 文字 hex 文字列になるか (実画像不要: numpy で生成)。"""
    img  = _make_image("white")
    path = tmp_path / "white.png"
    _save_image(img, path)

    h = compute_phash(path)
    assert isinstance(h, str)
    assert len(h) == 16
    # 全て hex 文字
    int(h, 16)  # ValueError が出なければ OK


def test_phash_distance_same(tmp_path):
    """同一画像の phash 距離が 0 になるか。"""
    img  = _make_image("gradient")
    path = tmp_path / "grad.png"
    _save_image(img, path)

    h = compute_phash(path)
    assert phash_distance(h, h) == 0


def test_phash_distance_different(tmp_path):
    """異なる画像の phash 距離が > 0 になるか。"""
    white   = tmp_path / "white.png"
    checker = tmp_path / "checker.png"
    _save_image(_make_image("white"),   white)
    _save_image(_make_image("checker"), checker)

    h_white   = compute_phash(white)
    h_checker = compute_phash(checker)
    assert phash_distance(h_white, h_checker) > 0


# ============================================================
# F. JSON 読み込みテスト
# ============================================================

def test_load_summary(tmp_path):
    """JSON 読み込みと画面リストへの変換が正しいか。"""
    summary_data = {
        "session_id": "test_20260303",
        "screens": [
            {
                "fingerprint":    "aaa1aaa1",
                "title":          "一般",
                "depth":          0,
                "parent_fp":      None,
                "tappable_items": [{"text": "情報", "confidence": 0.95}],
                "phash":          "0000000000000001",
                "screenshot_path": "/tmp/a.png",
                "discovered_at":  "2026-03-03T00:00:00",
            }
        ],
        "stats": {
            "screens_found": 1, "screens_skipped": 0,
            "taps_total": 0,    "elapsed_sec": 5.0,
        },
    }
    summary_file = tmp_path / "crawl_summary.json"
    summary_file.write_text(json.dumps(summary_data), encoding="utf-8")

    screens = load_summary(tmp_path)
    assert len(screens) == 1
    assert screens[0]["fingerprint"] == "aaa1aaa1"
    assert screens[0]["title"] == "一般"
    assert screens[0]["depth"] == 0
    assert screens[0]["parent_fp"] is None


def test_load_summary_missing(tmp_path):
    """crawl_summary.json が存在しない場合に FileNotFoundError が上がるか。"""
    with pytest.raises(FileNotFoundError):
        load_summary(tmp_path)


# ============================================================
# G. 統合テスト (evidence/ 最新セッション)
# ============================================================

def test_visualize_integration():
    """
    evidence/ 最新セッションに crawl_summary.json があれば実際に可視化する。
    ファイルが存在しなければ pytest.skip する。
    """
    evidence_base = Path(__file__).parent.parent / "evidence"
    if not evidence_base.exists():
        pytest.skip("evidence/ ディレクトリが見つかりません")

    summary_files = sorted(evidence_base.glob("*/crawl_summary.json"), reverse=True)
    if not summary_files:
        pytest.skip(
            "crawl_summary.json が見つかりません "
            "(クローラーを先に実行してください)"
        )

    latest = summary_files[0]
    screens = load_summary(latest.parent)
    assert len(screens) > 0, "画面数が 0 件"

    graph   = build_graph(screens)
    mermaid = render_mermaid(graph)
    assert "graph TD" in mermaid

    root_fps = [s["fingerprint"] for s in screens if s.get("parent_fp") is None]
    if root_fps:
        tree = render_tree(graph, root_fps[0])
        assert "📱" in tree

    gaps = analyze_gaps(screens)
    assert isinstance(gaps["unknown_screens"], list)
    assert isinstance(gaps["untapped_screens"], list)
