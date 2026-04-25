"""cluster_decision — テキスト空フレームペアの 4段階クラスタ判定。

§16 ルール 3「テキスト空 + phash判定」を以下の4段階に拡張する:

  第0層: 暗転 / ハードカット境界 → 強制別クラスタ
  第1層: dHash 即決 (< near_threshold で同, >= far_threshold で別)
  第2層: dHash 中間域 + ヒストグラム類似度で判定

呼び出し側は判定理由 (decision_method) を `lc_screens.cluster_decision_method`
に保存し、UI でクラスタの統合根拠を可視化する。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple


def classify_empty_text_pair(
    *,
    prev_path: Optional[Path | str],
    curr_path: Path | str,
    hash_distance: int,
    near_threshold: int,
    far_threshold: int,
    fallback_threshold: int,
) -> Tuple[bool, str]:
    """テキスト空フレームペアを4段階で分類する。

    Args:
        prev_path: 直前フレームの画像パス。None なら境界判定スキップ。
        curr_path: 現フレームの画像パス。
        hash_distance: 既算済みの dHash/phash 距離。
        near_threshold: 即決同クラスタ (距離 < this)。translate_threshold(8) 推奨。
        far_threshold: 即決別クラスタ (距離 >= this)。translate_threshold(40) 推奨。
        fallback_threshold: prev_path が None の時のフォールバック (距離 < this で同)。
                            translate_threshold(30) 推奨。

    Returns:
        (is_same_cluster, decision_method): decision_method は以下のいずれか:
            "blackout"        — 暗転検出 (強制別)
            "hard_cut"        — ハードカット (強制別)
            "dhash_near"      — dHash 即決同
            "dhash_far"       — dHash 即決別
            "hist_match"      — dHash 中間 + ヒスト類似 (同)
            "hist_mismatch"   — dHash 中間 + ヒスト非類似 (別)
            "dhash_fallback"  — prev_path 取得失敗時の dHash 単純判定
    """
    # 第0層: 境界判定 (prev_path がある場合のみ)
    if prev_path is not None:
        from lc.scene_boundary_detector import is_scene_boundary

        is_boundary, reason = is_scene_boundary(prev_path, curr_path)
        if is_boundary:
            return False, reason  # "blackout" or "hard_cut"

    # 第1層: dHash 即決
    if hash_distance < near_threshold:
        return True, "dhash_near"
    if hash_distance >= far_threshold:
        return False, "dhash_far"

    # 第2層: ヒストグラム (中間域)
    if prev_path is None:
        return hash_distance < fallback_threshold, "dhash_fallback"

    from lc.image_comparator import is_similar_by_histogram

    if is_similar_by_histogram(prev_path, curr_path):
        return True, "hist_match"
    return False, "hist_mismatch"
