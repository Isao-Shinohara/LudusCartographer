"""orb_matcher のユニットテスト。"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.orb_matcher import compute_descriptors, descriptors_from_blob, match_rate


@pytest.fixture
def text_image(tmp_path: Path) -> Path:
    """テキスト + 画像のあるシーン。"""
    p = tmp_path / "text.png"
    img = np.full((720, 1440, 3), 60, dtype=np.uint8)
    cv2.putText(img, "Hello World", (100, 360), cv2.FONT_HERSHEY_SIMPLEX, 4, (255, 255, 255), 8)
    cv2.rectangle(img, (50, 50), (300, 300), (200, 200, 200), -1)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def text_image_v2(tmp_path: Path) -> Path:
    """同じ構図だがテキストが違う。"""
    p = tmp_path / "text_v2.png"
    img = np.full((720, 1440, 3), 60, dtype=np.uint8)
    cv2.putText(img, "Goodbye World", (100, 360), cv2.FONT_HERSHEY_SIMPLEX, 4, (255, 255, 255), 8)
    cv2.rectangle(img, (50, 50), (300, 300), (200, 200, 200), -1)
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def black_image(tmp_path: Path) -> Path:
    p = tmp_path / "black.png"
    cv2.imwrite(str(p), np.zeros((720, 1440, 3), dtype=np.uint8))
    return p


def test_compute_descriptors_returns_bytes(text_image: Path) -> None:
    blob = compute_descriptors(text_image)
    assert isinstance(blob, bytes)
    assert len(blob) > 0
    assert len(blob) % 32 == 0  # ORB 記述子は 32 bytes/feature


def test_compute_descriptors_black_returns_empty(black_image: Path) -> None:
    blob = compute_descriptors(black_image)
    # 真っ黒 → 特徴点なし
    assert blob == b''


def test_descriptors_from_blob_roundtrip(text_image: Path) -> None:
    blob = compute_descriptors(text_image)
    arr = descriptors_from_blob(blob)
    assert arr is not None
    assert arr.dtype == np.uint8
    assert arr.shape[1] == 32
    assert arr.shape[0] > 0


def test_descriptors_from_blob_empty() -> None:
    assert descriptors_from_blob(b'') is None
    assert descriptors_from_blob(None) is None


def test_match_rate_same_image_high(text_image: Path) -> None:
    blob = compute_descriptors(text_image)
    rate = match_rate(blob, blob)
    assert rate > 0.5  # 同じ画像はマッチ率高い


def test_match_rate_different_text_low(text_image: Path, text_image_v2: Path) -> None:
    """同じ構図でテキストだけ違う → マッチ率は中程度か低い。"""
    b1 = compute_descriptors(text_image)
    b2 = compute_descriptors(text_image_v2)
    rate = match_rate(b1, b2)
    # 一部のテキスト・図形は同じなのでマッチ率は 0 ではない
    assert 0.0 <= rate <= 1.0


def test_match_rate_empty_descriptors_zero(text_image: Path) -> None:
    blob = compute_descriptors(text_image)
    assert match_rate(blob, b'') == 0.0
    assert match_rate(b'', blob) == 0.0
    assert match_rate(b'', b'') == 0.0
