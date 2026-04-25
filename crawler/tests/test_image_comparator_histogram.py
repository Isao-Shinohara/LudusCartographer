"""image_comparator のヒストグラム類似度ヘルパのテスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.image_comparator import HIST_SIMILARITY_THRESHOLD, is_similar_by_histogram


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


def test_default_threshold_is_reasonable() -> None:
    assert 0.0 < HIST_SIMILARITY_THRESHOLD < 1.0


def test_similar_images_return_true(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    assert is_similar_by_histogram(gradient_image, gradient_image_v2) is True


def test_different_images_return_false(
    black_image: Path, bright_image: Path
) -> None:
    assert is_similar_by_histogram(black_image, bright_image) is False


def test_same_image_returns_true(gradient_image: Path) -> None:
    assert is_similar_by_histogram(gradient_image, gradient_image) is True


def test_custom_threshold(gradient_image: Path, gradient_image_v2: Path) -> None:
    assert is_similar_by_histogram(
        gradient_image, gradient_image_v2, similarity_threshold=0.99
    ) is False
