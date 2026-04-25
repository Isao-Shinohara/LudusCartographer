"""scene_boundary_detector のユニットテスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.scene_boundary_detector import (
    compute_grayscale_histogram,
    detect_blackout,
    histogram_distance,
    is_scene_boundary,
)


@pytest.fixture
def black_image(tmp_path: Path) -> Path:
    p = tmp_path / "black.png"
    img = np.zeros((720, 1440, 3), dtype=np.uint8)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def near_black_image(tmp_path: Path) -> Path:
    p = tmp_path / "near_black.png"
    img = np.full((720, 1440, 3), 15, dtype=np.uint8)
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


# ─── detect_blackout ─────────────────────────────────────────


def test_detect_blackout_pure_black(black_image: Path) -> None:
    assert detect_blackout(black_image) is True


def test_detect_blackout_near_black(near_black_image: Path) -> None:
    assert detect_blackout(near_black_image) is True


def test_detect_blackout_gray_is_not_blackout(gray_image: Path) -> None:
    assert detect_blackout(gray_image) is False


def test_detect_blackout_bright_is_not_blackout(bright_image: Path) -> None:
    assert detect_blackout(bright_image) is False


def test_detect_blackout_custom_threshold(gray_image: Path) -> None:
    assert detect_blackout(gray_image, threshold=130) is True
    assert detect_blackout(gray_image, threshold=120) is False


# ─── histogram ──────────────────────────────────────────────


def test_histogram_shape(gray_image: Path) -> None:
    h = compute_grayscale_histogram(gray_image)
    assert h.shape == (256, 1)
    assert h.dtype == np.float32


def test_histogram_distance_same_image(gray_image: Path) -> None:
    h1 = compute_grayscale_histogram(gray_image)
    h2 = compute_grayscale_histogram(gray_image)
    d = histogram_distance(h1, h2)
    assert d < 0.01


def test_histogram_distance_similar_images(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    h1 = compute_grayscale_histogram(gradient_image)
    h2 = compute_grayscale_histogram(gradient_image_v2)
    d = histogram_distance(h1, h2)
    assert d < 0.4


def test_histogram_distance_different_images(
    black_image: Path, bright_image: Path
) -> None:
    h1 = compute_grayscale_histogram(black_image)
    h2 = compute_grayscale_histogram(bright_image)
    d = histogram_distance(h1, h2)
    assert d > 0.7


# ─── is_scene_boundary ──────────────────────────────────────


def test_is_scene_boundary_blackout_first(
    black_image: Path, gray_image: Path
) -> None:
    is_boundary, reason = is_scene_boundary(black_image, gray_image)
    assert is_boundary is True
    assert reason == "blackout"


def test_is_scene_boundary_blackout_second(
    gray_image: Path, black_image: Path
) -> None:
    is_boundary, reason = is_scene_boundary(gray_image, black_image)
    assert is_boundary is True
    assert reason == "blackout"


def test_is_scene_boundary_hard_cut(
    black_image: Path, bright_image: Path
) -> None:
    is_boundary, reason = is_scene_boundary(bright_image, gray_image := bright_image)
    assert is_boundary is False
    is_boundary2, reason2 = is_scene_boundary(black_image, bright_image)
    assert is_boundary2 is True
    assert reason2 in ("blackout", "hard_cut")


def test_is_scene_boundary_similar_no_boundary(
    gradient_image: Path, gradient_image_v2: Path
) -> None:
    is_boundary, reason = is_scene_boundary(gradient_image, gradient_image_v2)
    assert is_boundary is False
    assert reason == ""


def test_is_scene_boundary_same_image(gray_image: Path) -> None:
    is_boundary, reason = is_scene_boundary(gray_image, gray_image)
    assert is_boundary is False
    assert reason == ""
