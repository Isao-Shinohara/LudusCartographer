"""cluster_decision のユニットテスト — Step A: phash 単独判定 (中間域は「同」)。

Step B (dHash 分割) は background_worker._validate_clusters の責務でここではテストしない。
"""
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


# ─── 中間域: phash_mid (Step A は「同」) ──────────────────────


def test_phash_mid_returns_same_regardless_of_dhash(gray_image: Path) -> None:
    """中間域は dHash 値に関係なく「同」(Step A は統合のみ、分割は Step B)。"""
    for dhash in (0, 10, 25, 50):
        is_same, method = classify_empty_text_pair(
            prev_path=gray_image,
            curr_path=gray_image,
            phash_distance=20,
            dhash_distance=dhash,
            near_threshold=8,
            far_threshold=40,
            fallback_threshold=30,
        )
        assert is_same is True
        assert method == "phash_mid"


# ─── prev_path None フォールバック ────────────────────────────


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
    assert r.method == "phash_mid"
    assert r.phash_distance == 20
    assert r.dhash_distance == 15  # UI 表示用に保持


def test_metrics_brightness_recorded(black_image: Path, gray_image: Path) -> None:
    r = classify_empty_text_pair_with_metrics(
        prev_path=black_image, curr_path=gray_image,
        phash_distance=20, dhash_distance=10,
        near_threshold=8, far_threshold=40, fallback_threshold=30,
    )
    assert r.prev_brightness is not None and r.prev_brightness < 5
    assert r.curr_brightness is not None and 100 < r.curr_brightness < 150
