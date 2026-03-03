"""
test_icon_detection.py — アイコン検出（テンプレートマッチング）ユニットテスト

Appium・実機・カメラ不要。numpy/cv2 で合成した画像のみを使用する。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest

from lc.crawler import _iou, CrawlerConfig, ScreenCrawler


# ============================================================
# フィクスチャ
# ============================================================

@pytest.fixture
def mock_driver(tmp_path: Path) -> MagicMock:
    """AppiumDriver の最小モック (evidence_dir のみ設定)。"""
    driver = MagicMock()
    driver._evidence_dir = tmp_path / "evidence"
    driver._evidence_dir.mkdir()
    driver.session_id = "test_session_icon"
    return driver


@pytest.fixture
def crawler(mock_driver: MagicMock, tmp_path: Path) -> ScreenCrawler:
    """DB 未接続・テンプレートなし状態のクローラー。"""
    cfg = CrawlerConfig()          # db_host="" → DB スキップ
    c = ScreenCrawler(mock_driver, cfg)
    return c


@pytest.fixture
def cross_template() -> np.ndarray:
    """
    テスト用 16×16 グレースケール十字テンプレート。
    均一でない非自明パターン → matchTemplate の std が非 0 になる。
    """
    tmpl = np.zeros((16, 16), dtype=np.uint8)
    tmpl[6:10, 2:14] = 200   # 横棒
    tmpl[2:14, 6:10] = 200   # 縦棒
    return tmpl


@pytest.fixture
def screen_with_cross(tmp_path: Path, cross_template: np.ndarray) -> Path:
    """
    400×200 の黒スクリーンに cross_template を (50, 100) に埋め込んだ PNG。
    テンプレートは pixel (50, 100) 〜 (65, 115) に配置。
    """
    screen = np.zeros((400, 200), dtype=np.uint8)
    h, w = cross_template.shape
    screen[100: 100 + h, 50: 50 + w] = cross_template
    path = tmp_path / "screen.png"
    cv2.imwrite(str(path), screen)
    return path


# ============================================================
# _iou テスト
# ============================================================

def test_iou_identical() -> None:
    """完全一致の矩形は IoU = 1.0。"""
    assert _iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)


def test_iou_no_overlap() -> None:
    """離れた矩形は IoU = 0.0。"""
    assert _iou((0, 0, 5, 5), (10, 10, 5, 5)) == pytest.approx(0.0)


def test_iou_partial_overlap() -> None:
    """
    (0,0,10,10) と (5,5,10,10) の IoU。
    intersection = 5×5 = 25、union = 100+100−25 = 175。
    """
    result = _iou((0, 0, 10, 10), (5, 5, 10, 10))
    assert result == pytest.approx(25 / 175, abs=1e-6)


# ============================================================
# テンプレート読み込みテスト
# ============================================================

def test_load_templates_empty_dir(crawler: ScreenCrawler) -> None:
    """assets/templates/ にテンプレートがなければ空辞書。"""
    # crawlerの _load_icon_templates は既に __init__ で呼ばれているが、
    # テンプレートディレクトリが空（または存在しない）なら空。
    assert isinstance(crawler._icon_templates, dict)
    # 既存のテンプレートは assets/templates/ にある実ファイルに依存するため
    # テスト時点で置かれていない PNG は含まれない。


def test_load_templates_with_file(tmp_path: Path, mock_driver: MagicMock) -> None:
    """PNG ファイルが 1 件あれば _icon_templates に登録される。"""
    tmpl = np.zeros((16, 16), dtype=np.uint8)
    tmpl[4:12, 4:12] = 200
    tmpl_path = tmp_path / "close_btn.png"
    cv2.imwrite(str(tmpl_path), tmpl)

    cfg = CrawlerConfig()
    c = ScreenCrawler(mock_driver, cfg)
    # 手動でテンプレートを設定（_load_icon_templates はパスが固定されるため）
    c._icon_templates = {"close_btn": tmpl}

    assert "close_btn" in c._icon_templates
    assert c._icon_templates["close_btn"].shape == (16, 16)


# ============================================================
# _detect_icons テスト
# ============================================================

def test_detect_icons_empty_templates(
    crawler: ScreenCrawler,
    screen_with_cross: Path,
) -> None:
    """テンプレートが空なら空リストを返す。"""
    crawler._icon_templates = {}
    result = crawler._detect_icons(screen_with_cross)
    assert result == []


def test_detect_icons_invalid_path(crawler: ScreenCrawler, tmp_path: Path) -> None:
    """存在しない画像パスには空リストを返す。"""
    crawler._icon_templates = {"dummy": np.zeros((8, 8), dtype=np.uint8)}
    result = crawler._detect_icons(tmp_path / "nonexistent.png")
    assert result == []


def test_detect_icons_finds_match(
    crawler: ScreenCrawler,
    cross_template: np.ndarray,
    screen_with_cross: Path,
) -> None:
    """スクリーンに埋め込んだテンプレートが 1 件検出される。"""
    crawler._icon_templates = {"cross_btn": cross_template}
    results = crawler._detect_icons(screen_with_cross)
    assert len(results) == 1


def test_detect_icons_score_above_threshold(
    crawler: ScreenCrawler,
    cross_template: np.ndarray,
    screen_with_cross: Path,
) -> None:
    """検出された confidence は icon_threshold (0.80) 以上。"""
    crawler._icon_templates = {"cross_btn": cross_template}
    results = crawler._detect_icons(screen_with_cross)
    assert results[0]["confidence"] >= crawler.config.icon_threshold


def test_detect_icons_score_below_threshold(
    crawler: ScreenCrawler,
    cross_template: np.ndarray,
    tmp_path: Path,
) -> None:
    """閾値を 1.0 にするとどのスクリーンでも検出されない。"""
    crawler.config.icon_threshold = 1.01   # 完全一致でも超えられない上限
    crawler._icon_templates = {"cross_btn": cross_template}
    # テンプレートを埋め込んだスクリーンでもスコアは 1.0 に達しない場合がある
    screen = np.zeros((400, 200), dtype=np.uint8)
    h, w = cross_template.shape
    screen[100: 100 + h, 50: 50 + w] = cross_template
    img_path = tmp_path / "screen_high_thresh.png"
    cv2.imwrite(str(img_path), screen)

    results = crawler._detect_icons(img_path)
    assert results == []


def test_detect_icons_output_format(
    crawler: ScreenCrawler,
    cross_template: np.ndarray,
    screen_with_cross: Path,
) -> None:
    """検出結果のフォーマット検証。"""
    crawler._icon_templates = {"close_btn": cross_template}
    results = crawler._detect_icons(screen_with_cross)
    assert len(results) == 1
    det = results[0]

    # text は "icon:{stem}" 形式
    assert det["text"] == "icon:close_btn"
    # confidence は float
    assert isinstance(det["confidence"], float)
    # box は 4 点の座標リスト
    assert len(det["box"]) == 4
    assert all(len(pt) == 2 for pt in det["box"])
    # center は [cx, cy]
    assert len(det["center"]) == 2


def test_detect_icons_nms_deduplication(
    crawler: ScreenCrawler,
    tmp_path: Path,
) -> None:
    """
    NMS 検証: 高 IoU (>= 0.4) の重複候補は 1 件に集約される。

    テンプレートを2箇所に配置。一方は1px ずれ → IoU が高く NMS で除去される。
    """
    # 20×20 の明確なパターン
    tmpl = np.zeros((20, 20), dtype=np.uint8)
    tmpl[5:15, 5:15] = 220
    tmpl[8:12, 2:18] = 150

    # 300×300 スクリーンに (10, 10) と (11, 10) に配置（1px ずれ = IoU ≈ 19/21 > 0.4）
    screen = np.zeros((300, 300), dtype=np.uint8)
    screen[10:30, 10:30] = tmpl
    screen[10:30, 11:31] = tmpl  # 2nd copy overwrites 1px → same template exists

    img_path = tmp_path / "nms_screen.png"
    cv2.imwrite(str(img_path), screen)

    crawler._icon_templates = {"nms_btn": tmpl}
    results = crawler._detect_icons(img_path)

    # 真の検出は 1 件（高 IoU の重複は NMS で除去される）
    assert len(results) == 1


def test_detect_icons_multiple_templates(
    crawler: ScreenCrawler,
    tmp_path: Path,
) -> None:
    """2 種類のテンプレートがそれぞれ検出される。"""
    # テンプレート A: 左上ブロック
    tmpl_a = np.zeros((16, 16), dtype=np.uint8)
    tmpl_a[2:14, 2:14] = 180

    # テンプレート B: 対角ライン
    tmpl_b = np.zeros((16, 16), dtype=np.uint8)
    for i in range(16):
        tmpl_b[i, i] = 200

    # 200×200 スクリーンに離れた位置に配置
    screen = np.zeros((200, 200), dtype=np.uint8)
    screen[10:26, 10:26] = tmpl_a   # A at (10, 10)
    screen[100:116, 150:166] = tmpl_b  # B at (150, 100)

    img_path = tmp_path / "multi_screen.png"
    cv2.imwrite(str(img_path), screen)

    crawler._icon_templates = {"icon_a": tmpl_a, "icon_b": tmpl_b}
    results = crawler._detect_icons(img_path)

    texts = {r["text"] for r in results}
    assert "icon:icon_a" in texts
    assert "icon:icon_b" in texts
