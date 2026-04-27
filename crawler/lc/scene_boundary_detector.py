"""scene_boundary_detector — カラーヒストグラム計算ユーティリティ。

クラスタ判定 (cluster_decision.py 第2層) や ImageComparator のヒスト比較で使う。
BGR 各 8 ビン (合計 8×8×8 = 512 ビン) の 3D ヒストグラムで色の組み合わせを捉える。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def compute_color_histogram(image_path: Path | str, bins: int = 8) -> np.ndarray:
    """BGR 3D カラーヒストグラム (各チャンネル bins ビン) を正規化して返す。"""
    img = cv2.imread(str(image_path))
    if img is None:
        raise ValueError(f"画像を読み込めません: {image_path}")
    hist = cv2.calcHist(
        [img], [0, 1, 2], None,
        [bins, bins, bins],
        [0, 256, 0, 256, 0, 256],
    )
    cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
    return hist


def histogram_distance(h1: np.ndarray, h2: np.ndarray) -> float:
    """バタチャリヤ距離を返す (0=同一, 1=全く異なる)。"""
    return float(cv2.compareHist(h1, h2, cv2.HISTCMP_BHATTACHARYYA))


def histogram_similarity(h1: np.ndarray, h2: np.ndarray) -> float:
    """類似度 (1.0=同一 〜 0.0=全く異なる) を返す。バタチャリヤ距離の逆。"""
    return 1.0 - histogram_distance(h1, h2)
