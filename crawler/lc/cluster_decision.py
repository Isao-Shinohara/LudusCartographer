"""cluster_decision — テキスト空フレームペアの 4段階クラスタ判定。

§16 ルール 3「テキスト空 + phash判定」を以下の4段階に拡張する:

  第0層: 暗転 / ハードカット境界 → 強制別クラスタ
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
    """4段階分類 (旧 API: 後方互換用、内部で classify_empty_text_pair_with_metrics を呼ぶ)。"""
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
    """4段階分類を行い、判定結果 + 計算済み数値を返す。

    閾値調整 UI で使うため、各層で計算した数値 (ヒスト距離・輝度) も保持する。
    """
    import cv2

    # 平均輝度 (暗転判定用)
    def _brightness(path: Path | str) -> Optional[float]:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else None

    prev_br = _brightness(prev_path) if prev_path is not None else None
    curr_br = _brightness(curr_path)

    # 第0層: 境界判定 (prev_path がある場合のみ)
    if prev_path is not None:
        from lc.scene_boundary_detector import (
            compute_grayscale_histogram,
            histogram_distance,
        )

        BLACKOUT_TH = 20.0
        HIST_HARDCUT_TH = 0.7

        if (prev_br is not None and prev_br < BLACKOUT_TH) or (
            curr_br is not None and curr_br < BLACKOUT_TH
        ):
            return ClassifyResult(False, "blackout", hash_distance, None, prev_br, curr_br)

        h1 = compute_grayscale_histogram(prev_path)
        h2 = compute_grayscale_histogram(curr_path)
        hist_dist = histogram_distance(h1, h2)
        if hist_dist > HIST_HARDCUT_TH:
            return ClassifyResult(False, "hard_cut", hash_distance, hist_dist, prev_br, curr_br)
    else:
        hist_dist = None

    # 第1層: dHash 即決
    if hash_distance < near_threshold:
        return ClassifyResult(True, "dhash_near", hash_distance, hist_dist, prev_br, curr_br)
    if hash_distance >= far_threshold:
        return ClassifyResult(False, "dhash_far", hash_distance, hist_dist, prev_br, curr_br)

    # 第2層: ヒストグラム (中間域)
    if prev_path is None:
        same = hash_distance < fallback_threshold
        return ClassifyResult(same, "dhash_fallback", hash_distance, None, prev_br, curr_br)

    from lc.image_comparator import HIST_SIMILARITY_THRESHOLD

    # hist_dist は第0層で既に計算済み
    similarity = 1.0 - (hist_dist if hist_dist is not None else 0.0)
    if similarity >= HIST_SIMILARITY_THRESHOLD:
        return ClassifyResult(True, "hist_match", hash_distance, hist_dist, prev_br, curr_br)
    return ClassifyResult(False, "hist_mismatch", hash_distance, hist_dist, prev_br, curr_br)
