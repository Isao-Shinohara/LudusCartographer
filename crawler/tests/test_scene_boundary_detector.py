"""scene_boundary_detector のユニットテスト (ヒストグラム計算ユーティリティ)。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.scene_boundary_detector import (
    compute_color_histogram,
    histogram_distance,
    histogram_similarity,
)


@pytest.fixture
def black_image(tmp_path: Path) -> Path:
    p = tmp_path / "black.png"
    img = np.zeros((720, 1440, 3), dtype=np.uint8)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def gray_image(tmp_path: Path) -> Path:
    p = tmp_path / "gray.png"
    img = np.full((720, 1440, 3), 128, dtype=np.uint8)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def bright_image(tmp_path: Path) -> Path:
    p = tmp_path / "bright.png"
    img = np.full((720, 1440, 3), 200, dtype=np.uint8)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def gradient_image(tmp_path: Path) -> Path:
    p = tmp_path / "gradient.png"
    grad = np.tile(np.linspace(0, 255, 1440, dtype=np.uint8), (720, 1))
    img = cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def gradient_image_v2(tmp_path: Path) -> Path:
    p = tmp_path / "gradient_v2.png"
    grad = np.tile(np.linspace(5, 250, 1440, dtype=np.uint8), (720, 1))
    img = cv2.cvtColor(grad, cv2.COLOR_GRAY2BGR)
    cv2.imwrite(str(p), img)
    return p


def test_histogram_shape(gray_image: Path) -> None:
    h = compute_color_histogram(gray_image)
    assert h.shape == (8, 8, 8)
    assert h.dtype == np.float32


def test_histogram_distance_same_image(gray_image: Path) -> None:
    h1 = compute_color_histogram(gray_image)
    h2 = compute_color_histogram(gray_image)
    d = histogram_distance(h1, h2)
    assert d < 0.01


def test_histogram_distance_similar_images(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    h1 = compute_color_histogram(gradient_image)
    h2 = compute_color_histogram(gradient_image_v2)
    d = histogram_distance(h1, h2)
    assert d < 0.4


def test_histogram_distance_different_images(
    black_image: Path, bright_image: Path
) -> None:
    h1 = compute_color_histogram(black_image)
    h2 = compute_color_histogram(bright_image)
    d = histogram_distance(h1, h2)
    assert d > 0.7


def test_histogram_similarity_inverse_of_distance(gray_image: Path) -> None:
    h1 = compute_color_histogram(gray_image)
    h2 = compute_color_histogram(gray_image)
    sim = histogram_similarity(h1, h2)
    assert sim > 0.99
