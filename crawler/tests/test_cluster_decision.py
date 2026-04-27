"""cluster_decision のユニットテスト — テキスト空フレームペアの2段階判定。"""
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
def bright_image(tmp_path: Path) -> Path:
    p = tmp_path / "bright.png"
    cv2.imwrite(str(p), np.full((720, 1440, 3), 200, dtype=np.uint8))
    return p


@pytest.fixture
def gray_image(tmp_path: Path) -> Path:
    p = tmp_path / "gray.png"
    cv2.imwrite(str(p), np.full((720, 1440, 3), 128, dtype=np.uint8))
    return p


@pytest.fixture
def gradient_image(tmp_path: Path) -> Path:
    p = tmp_path / "gradient.png"
    grad = np.tile(np.linspace(0, 255, 1440, dtype=np.uint8), (720, 1))
    cv2.imwrite(str(p), cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR))
    return p


@pytest.fixture
def gradient_image_v2(tmp_path: Path) -> Path:
    p = tmp_path / "gradient_v2.png"
    grad = np.tile(np.linspace(5, 250, 1440, dtype=np.uint8), (720, 1))
    cv2.imwrite(str(p), cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR))
    return p


# ─── 第1層: dHash 即決 ────────────────────────────────────────


def test_dhash_near_returns_same(gray_image: Path) -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=gray_image,
        curr_path=gray_image,
        hash_distance=3,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "dhash_near"


def test_dhash_far_returns_not_same(gradient_image: Path, gradient_image_v2: Path) -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=gradient_image,
        curr_path=gradient_image_v2,
        hash_distance=50,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "dhash_far"


# ─── 第2層: ヒストグラム ──────────────────────────────────────


def test_dhash_mid_with_similar_histogram_returns_same(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=gradient_image,
        curr_path=gradient_image_v2,
        hash_distance=20,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "hist_match"


def test_dhash_mid_with_different_histogram_returns_not_same(
    black_image: Path, bright_image: Path
) -> None:
    """中間域 + ヒスト類似低 → hist_mismatch で別。"""
    is_same, method = classify_empty_text_pair(
        prev_path=black_image,
        curr_path=bright_image,
        hash_distance=20,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "hist_mismatch"


# ─── prev_path None フォールバック ────────────────────────────


def test_no_prev_path_within_fallback_returns_same() -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=None,
        curr_path=Path("dummy"),
        hash_distance=20,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is True
    assert method == "dhash_fallback"


def test_no_prev_path_beyond_fallback_returns_not_same() -> None:
    is_same, method = classify_empty_text_pair(
        prev_path=None,
        curr_path=Path("dummy"),
        hash_distance=35,
        near_threshold=8,
        far_threshold=40,
        fallback_threshold=30,
    )
    assert is_same is False
    assert method == "dhash_fallback"


# ─── metrics API ─────────────────────────────────────────────


def test_metrics_includes_hist_distance(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    r = classify_empty_text_pair_with_metrics(
        prev_path=gradient_image, curr_path=gradient_image_v2,
        hash_distance=20, near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.method == "hist_match"
    assert r.hist_distance is not None
    assert 0.0 <= r.hist_distance <= 1.0


def test_metrics_dhash_near_skips_hist(gray_image: Path) -> None:
    """dHash 即決 (近) では中間域に到達しないので hist_distance は計算されない。"""
    r = classify_empty_text_pair_with_metrics(
        prev_path=gray_image, curr_path=gray_image,
        hash_distance=3, near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.method == "dhash_near"
    assert r.hash_distance == 3
    assert r.hist_distance is None


def test_metrics_no_prev_path() -> None:
    r = classify_empty_text_pair_with_metrics(
        prev_path=None, curr_path=Path("dummy"),
        hash_distance=10, near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.method == "dhash_fallback"
    assert r.prev_brightness is None
    assert r.hist_distance is None


def test_metrics_brightness_recorded(black_image: Path, gray_image: Path) -> None:
    """輝度は判定とは独立に UI 表示用として常に記録される。"""
    r = classify_empty_text_pair_with_metrics(
        prev_path=black_image, curr_path=gray_image,
        hash_distance=20, near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.prev_brightness is not None and r.prev_brightness < 5
    assert r.curr_brightness is not None and 100 < r.curr_brightness < 150
