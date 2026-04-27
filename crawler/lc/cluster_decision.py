"""cluster_decision — テキスト空フレームペアのクラスタ判定。

§16 ルール 3「テキスト空 + phash判定」を以下の2段階で判定する:

  第1層: dHash 即決 (< near_threshold で同, >= far_threshold で別)
  第2層: dHash 中間域 + ヒストグラム類似度で判定

呼び出し側は判定理由 (decision_method) を `lc_screens.cluster_decision_method`
に保存し、UI でクラスタの統合根拠を可視化する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass
class ClassifyResult:
    """テキスト空ペア分類の結果と計算済み数値 (閾値調整用)。"""

    is_same: bool
    method: str
    hash_distance: int
    hist_distance: Optional[float]      # バタチャリヤ距離 (0=同一, 1=完全異なる)
    prev_brightness: Optional[float]    # 直前画像の平均輝度
    curr_brightness: Optional[float]    # 現画像の平均輝度


def classify_empty_text_pair(
    *,
    prev_path: Optional[Path | str],
    curr_path: Path | str,
    hash_distance: int,
    near_threshold: int,
    far_threshold: int,
    fallback_threshold: int,
) -> Tuple[bool, str]:
    """旧 API: 後方互換用、内部で classify_empty_text_pair_with_metrics を呼ぶ。"""
    r = classify_empty_text_pair_with_metrics(
        prev_path=prev_path,
        curr_path=curr_path,
        hash_distance=hash_distance,
        near_threshold=near_threshold,
        far_threshold=far_threshold,
        fallback_threshold=fallback_threshold,
    )
    return r.is_same, r.method


def classify_empty_text_pair_with_metrics(
    *,
    prev_path: Optional[Path | str],
    curr_path: Path | str,
    hash_distance: int,
    near_threshold: int,
    far_threshold: int,
    fallback_threshold: int,
) -> ClassifyResult:
    """2段階分類を行い、判定結果 + 計算済み数値を返す。

    閾値調整 UI で使うため、各層で計算した数値 (ヒスト距離・輝度) も保持する。
    """
    import cv2

    def _brightness(path: Path | str) -> Optional[float]:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else None

    prev_br = _brightness(prev_path) if prev_path is not None else None
    curr_br = _brightness(curr_path)

    # 第1層: dHash 即決
    if hash_distance < near_threshold:
        return ClassifyResult(True, "dhash_near", hash_distance, None, prev_br, curr_br)
    if hash_distance >= far_threshold:
        return ClassifyResult(False, "dhash_far", hash_distance, None, prev_br, curr_br)

    # 第2層: ヒストグラム類似度 (中間域)
    if prev_path is None:
        same = hash_distance < fallback_threshold
        return ClassifyResult(same, "dhash_fallback", hash_distance, None, prev_br, curr_br)

    from lc.scene_boundary_detector import (
        compute_color_histogram,
        histogram_distance,
    )
    from lc.image_comparator import HIST_SIMILARITY_THRESHOLD

    h1 = compute_color_histogram(prev_path)
    h2 = compute_color_histogram(curr_path)
    hist_dist = histogram_distance(h1, h2)
    similarity = 1.0 - hist_dist
    if similarity >= HIST_SIMILARITY_THRESHOLD:
        return ClassifyResult(True, "hist_match", hash_distance, hist_dist, prev_br, curr_br)
    return ClassifyResult(False, "hist_mismatch", hash_distance, hist_dist, prev_br, curr_br)
