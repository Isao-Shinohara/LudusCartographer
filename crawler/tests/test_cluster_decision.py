"""cluster_decision のユニットテスト — テキスト空フレームペアの2段階判定 (phash → dHash)。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.cluster_decision import classify_empty_text_pair, classify_empty_text_pair_with_metrics


@pytest.fixture
def black_image(tmp_path: Path) -> Path:
    p = tmp_path / "black.png"
    cv2.imwrite(str(p), np.zeros((720, 1440, 3), dtype=np.uint8))
    return p


@pytest.fixture
def gray_image(tmp_path: Path) -> Path:
    p = tmp_path / "gray.png"
    cv2.imwrite(str(p), np.full((720, 1440, 3), 128, dtype=np.uint8))
    return p


# ─── 第1層: phash 即決 ────────────────────────────────────────


def test_phash_near_returns_same(gray_image: Path) -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        phash_distance=3,
        dhash_distance=10,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "phash_near"


def test_phash_far_returns_not_same(gray_image: Path) -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        phash_distance=50,
        dhash_distance=10,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "phash_far"


# ─── 第2層: dHash 中間域 ──────────────────────────────────────


def test_dhash_match_returns_same(gray_image: Path) -> None:
    """phash 中間域 + dHash<25 → 同 (dhash_match)。"""
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        phash_distance=20,
        dhash_distance=10,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "dhash_match"


def test_dhash_mismatch_returns_not_same(gray_image: Path) -> None:
    """phash 中間域 + dHash>=25 → 別 (dhash_mismatch)。"""
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        phash_distance=20,
        dhash_distance=30,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "dhash_mismatch"


# ─── prev_path None / dhash None フォールバック ──────────────


def test_no_prev_path_within_fallback_returns_same() -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=None,
        curr_path=Path("dummy"),
        phash_distance=20,
        dhash_distance=None,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "phash_fallback"


def test_no_dhash_within_fallback_returns_same(gray_image: Path) -> None:
    """dhash が計算できない場合 (None) はフォールバック (phash 距離 < 30 で同)。"""
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        phash_distance=20,
        dhash_distance=None,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "phash_fallback"


def test_no_prev_path_beyond_fallback_returns_not_same() -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=None,
        curr_path=Path("dummy"),
        phash_distance=35,
        dhash_distance=None,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "phash_fallback"


# ─── metrics API ─────────────────────────────────────────────


def test_metrics_records_phash_and_dhash(gray_image: Path) -> None:
    r = classify_empty_text_pair_with_metrics(
        prev_path=gray_image, curr_path=gray_image,
        phash_distance=20, dhash_distance=15,
        near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.method == "dhash_match"
    assert r.phash_distance == 20
    assert r.dhash_distance == 15


def test_metrics_phash_near_records_distances(gray_image: Path) -> None:
    """phash 即決時は dhash_distance は計算されないため None で渡された値がそのまま入る (情報量保持目的)。"""
    r = classify_empty_text_pair_with_metrics(
        prev_path=gray_image, curr_path=gray_image,
        phash_distance=3, dhash_distance=None,
        near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.method == "phash_near"
    assert r.phash_distance == 3
    assert r.dhash_distance is None


def test_metrics_brightness_recorded(black_image: Path, gray_image: Path) -> None:
    r = classify_empty_text_pair_with_metrics(
        prev_path=black_image, curr_path=gray_image,
        phash_distance=20, dhash_distance=10,
        near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.prev_brightness is not None and r.prev_brightness < 5
    assert r.curr_brightness is not None and 100 < r.curr_brightness < 150
