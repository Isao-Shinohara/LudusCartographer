"""cluster_decision — テキスト空フレームペアのクラスタ判定。

§16 ルール 3「テキスト空 + phash判定」を以下の2段階で判定する:

  第1層: phash 即決 (< near_threshold で同, >= far_threshold で別)
  第2層: dHash 距離で判定 (中間域)

呼び出し側は判定理由 (decision_method) を `lc_screens.cluster_decision_method`
に保存し、UI でクラスタの統合根拠を可視化する。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


# 第2層 dHash 閾値 (中間域内で「同」と判定する上限)
DHASH_MID_THRESHOLD = 25


@dataclass
class ClassifyResult:
    """テキスト空ペア分類の結果と計算済み数値 (閾値調整用)。"""

    is_same: bool
    method: str
    phash_distance: int                  # 第1層で使った phash 距離
    dhash_distance: Optional[int]        # 第2層で使った dHash 距離
    prev_brightness: Optional[float]     # 直前画像の平均輝度 (UI 表示用)
    curr_brightness: Optional[float]     # 現画像の平均輝度 (UI 表示用)


def classify_empty_text_pair(
    *,
    prev_path: Optional[Path | str],
    curr_path: Path | str,
    phash_distance: int,
    dhash_distance: Optional[int],
    near_threshold: int,
    far_threshold: int,
    fallback_threshold: int,
) -> Tuple[bool, str]:
    """旧 API: 後方互換用、内部で classify_empty_text_pair_with_metrics を呼ぶ。"""
    r = classify_empty_text_pair_with_metrics(
        prev_path=prev_path,
        curr_path=curr_path,
        phash_distance=phash_distance,
        dhash_distance=dhash_distance,
        near_threshold=near_threshold,
        far_threshold=far_threshold,
        fallback_threshold=fallback_threshold,
    )
    return r.is_same, r.method


def classify_empty_text_pair_with_metrics(
    *,
    prev_path: Optional[Path | str],
    curr_path: Path | str,
    phash_distance: int,
    dhash_distance: Optional[int],
    near_threshold: int,
    far_threshold: int,
    fallback_threshold: int,
) -> ClassifyResult:
    """2段階分類を行い、判定結果 + 計算済み数値を返す。"""
    import cv2

    def _brightness(path: Path | str) -> Optional[float]:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else None

    prev_br = _brightness(prev_path) if prev_path is not None else None
    curr_br = _brightness(curr_path)

    # 第1層: phash 即決
    if phash_distance < near_threshold:
        return ClassifyResult(True, "phash_near", phash_distance, dhash_distance, prev_br, curr_br)
    if phash_distance >= far_threshold:
        return ClassifyResult(False, "phash_far", phash_distance, dhash_distance, prev_br, curr_br)

    # 第2層: dHash 距離 (中間域)
    if dhash_distance is None:
        # dHash が計算できない場合は phash 距離だけで判定 (フォールバック)
        same = phash_distance < fallback_threshold
        return ClassifyResult(same, "phash_fallback", phash_distance, None, prev_br, curr_br)

    if dhash_distance < DHASH_MID_THRESHOLD:
        return ClassifyResult(True, "dhash_match", phash_distance, dhash_distance, prev_br, curr_br)
    return ClassifyResult(False, "dhash_mismatch", phash_distance, dhash_distance, prev_br, curr_br)
