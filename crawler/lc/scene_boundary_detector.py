"""scene_boundary_detector — 動画切り替わり境界の検出。

暗転（平均輝度<threshold）と、グレースケールヒストグラムのバタチャリヤ距離による
ハードカット検出を提供する。クラスタリング時の「直前との比較で強制境界」判定に使う。
"""
from __future__ import annotations

from pathlib import Path
from typing import Tuple

import cv2
import numpy as np


# ─── 暗転検出 ────────────────────────────────────────────────


def detect_blackout(image_path: Path | str, threshold: int = 20) -> bool:
    """画像の平均輝度が threshold 未満なら暗転とみなす。"""
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"画像を読み込めません: {image_path}")
    return float(img.mean()) < float(threshold)


# ─── ヒストグラム ─────────────────────────────────────────────


def compute_grayscale_histogram(image_path: Path | str) -> np.ndarray:
    """グレースケール 256 ビンヒストグラムを正規化して返す (shape=(256,1), float32)。"""
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"画像を読み込めません: {image_path}")
    hist = cv2.calcHist([img], [0], None, [256], [0, 256])
    cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
    return hist


def histogram_distance(h1: np.ndarray, h2: np.ndarray) -> float:
    """バタチャリヤ距離を返す (0=同一, 1=全く異なる)。"""
    return float(cv2.compareHist(h1, h2, cv2.HISTCMP_BHATTACHARYYA))


def histogram_similarity(h1: np.ndarray, h2: np.ndarray) -> float:
    """類似度 (1.0=同一 〜 0.0=全く異なる) を返す。バタチャリヤ距離の逆。"""
    return 1.0 - histogram_distance(h1, h2)


# ─── 境界判定 ────────────────────────────────────────────────


def is_scene_boundary(
    image_path1: Path | str,
    image_path2: Path | str,
    blackout_threshold: int = 20,
    hist_threshold: float = 0.7,
) -> Tuple[bool, str]:
    """2画像の間にシーン境界があるかを判定する。

    判定順:
      1. どちらかが暗転 → ('blackout')
      2. ヒストグラム距離 > hist_threshold → ('hard_cut')
      3. それ以外 → 境界なし

    Returns:
        (is_boundary, reason): reason は 'blackout' / 'hard_cut' / '' のいずれか。
    """
    if detect_blackout(image_path1, blackout_threshold) or detect_blackout(
        image_path2, blackout_threshold
    ):
        return True, "blackout"
    h1 = compute_grayscale_histogram(image_path1)
    h2 = compute_grayscale_histogram(image_path2)
    if histogram_distance(h1, h2) > hist_threshold:
        return True, "hard_cut"
    return False, ""
