"""cluster_decision — テキスト空フレームペアのクラスタ判定 (Step A: phash 単独)。

§16 ルール 3「テキスト空 + phash判定」を以下で判定する:

  Step A 第1層: phash 即決
    phash < near_threshold       → 同 (phash_near)
    phash >= far_threshold       → 別 (phash_far)
    near <= phash < far (中間域) → 同 (phash_mid) ← まず統合し、Step B で分割

  Step B (background_worker._validate_clusters): クラスタ内を dHash 距離で分割
    代表との dHash 距離 >= 閾値 → クラスタから分離

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
    phash_distance: int
    dhash_distance: Optional[int]        # UI 表示・後段検証用 (Step A 自体は使わない)
    prev_brightness: Optional[float]
    curr_brightness: Optional[float]


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
    """旧 API: 後方互換用。"""
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
    """Step A: phash 単独で同/別を決める。中間域はとりあえず「同」。"""
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

    # 中間域 (8-39): Step A ではとりあえず「同」とし、Step B (validate) で dHash 分割を行う
    if prev_path is None:
        same = phash_distance < fallback_threshold
        return ClassifyResult(same, "phash_fallback", phash_distance, dhash_distance, prev_br, curr_br)
    return ClassifyResult(True, "phash_mid", phash_distance, dhash_distance, prev_br, curr_br)
