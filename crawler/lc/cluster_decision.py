"""cluster_decision — テキスト空フレームペアのクラスタ判定 (Step A: phash 単独)。

§16 ルール 3「テキスト空 + phash判定」を以下で判定する:

  Step A 第1層: phash 即決
    phash < near_threshold       → 同 (phash_near)
    phash >= far_threshold       → 別 (phash_far)
    near <= phash < far (中間域) → 中間判定:
      Jaccard 類似度 < min_jaccard → 別 (phash_low_jaccard)
      それ以外                       → 同 (phash_mid)

  Step B (background_worker._validate_clusters): クラスタ内を dHash 距離で分割
    代表との dHash 距離 >= 閾値 → クラスタから分離

中間域で Jaccard を見る理由: Hamming 距離は「両方 0」を「同じ」と数えるため、
両方が疎な phash (明るい単色寄り画像) は実質的に違うのに距離が小さく出る。
Jaccard は set bit の重なり度を測るので、疎ペアの実質類似度を補正できる。

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
    jaccard_similarity: Optional[float] = None,
    min_jaccard: float = 0.3,
    prev_phash: Optional[str] = None,
    curr_phash: Optional[str] = None,
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
        jaccard_similarity=jaccard_similarity,
        min_jaccard=min_jaccard,
        prev_phash=prev_phash,
        curr_phash=curr_phash,
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
    jaccard_similarity: Optional[float] = None,
    min_jaccard: float = 0.3,
    prev_phash: Optional[str] = None,
    curr_phash: Optional[str] = None,
) -> ClassifyResult:
    """Step A: phash 単独で同/別を決める。

    中間域 (near <= phash < far) では Jaccard 類似度も考慮:
    Jaccard < min_jaccard なら別判定 (phash_low_jaccard)、それ以外は phash_mid。

    縮退 phash (set bit < 8 or > 56) 同士は内容と無関係に距離 0 になるため、
    phash_near (= 同) 判定の対象外とする。これらは別物として扱い、新規クラスタ
    として独立させる (誤統合防止)。
    """
    import cv2

    def _brightness(path: Path | str) -> Optional[float]:
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return float(img.mean()) if img is not None else None

    prev_br = _brightness(prev_path) if prev_path is not None else None
    curr_br = _brightness(curr_path)

    # 縮退 phash 同士は phash 距離を信用しない (同 phash でも別画像の可能性大)
    is_degen_pair = False
    if prev_phash is not None and curr_phash is not None:
        from lc.utils import is_degenerate_phash
        is_degen_pair = is_degenerate_phash(prev_phash) or is_degenerate_phash(curr_phash)

    # 第1層: phash 即決
    if phash_distance < near_threshold and not is_degen_pair:
        return ClassifyResult(True, "phash_near", phash_distance, dhash_distance, prev_br, curr_br)
    if phash_distance >= far_threshold:
        return ClassifyResult(False, "phash_far", phash_distance, dhash_distance, prev_br, curr_br)

    # 縮退 phash 同士は中間域・near 判定もスキップして「別」確定 (内容判定不能)
    if is_degen_pair:
        return ClassifyResult(False, "phash_degenerate", phash_distance, dhash_distance, prev_br, curr_br)

    # 中間域 (near 〜 far): Jaccard 類似度で疎ペア (両方が単色寄り) を弾く
    # Jaccard < min_jaccard → 別 (phash_low_jaccard)
    if jaccard_similarity is not None and jaccard_similarity < min_jaccard:
        return ClassifyResult(False, "phash_low_jaccard", phash_distance, dhash_distance, prev_br, curr_br)

    if prev_path is None:
        same = phash_distance < fallback_threshold
        return ClassifyResult(same, "phash_fallback", phash_distance, dhash_distance, prev_br, curr_br)
    return ClassifyResult(True, "phash_mid", phash_distance, dhash_distance, prev_br, curr_br)
