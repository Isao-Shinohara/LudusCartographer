"""
image_comparator.py — 画像類似度比較（プラグイン可能なアルゴリズム）

環境変数 LC_HASH_ALGO で切替可能:
  - "phash"  : DCT ベースの知覚ハッシュ (デフォルト、従来互換)
  - "dhash"  : 勾配ベースのハッシュ (構図の違いに敏感)

使い方:
    from lc.image_comparator import get_comparator
    cmp = get_comparator()           # 環境変数に従う
    h = cmp.compute_hash(img_path)   # ハッシュ計算
    d = cmp.distance(h1, h2)         # ハミング距離
    t = cmp.translate_threshold(30)  # phash 閾値を現アルゴリズムに変換
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


# ─── アルゴリズム Protocol ─────────────────────────────────

class HashAlgorithm(Protocol):
    """ハッシュアルゴリズムのインターフェース。"""

    @property
    def name(self) -> str: ...

    def compute(self, image_path: Path, hash_size: int) -> str:
        """画像からハッシュ文字列を計算する。"""
        ...

    def distance(self, h1: str, h2: str) -> int:
        """2つのハッシュ間のハミング距離を返す (0=同一)。"""
        ...


# ─── PHash (DCT ベース) ──────────────────────────────────

class PHashAlgorithm:
    """DCT ベースの知覚ハッシュ。従来の実装と同一。"""

    @property
    def name(self) -> str:
        return "phash"

    def compute(self, image_path: Path, hash_size: int = 8) -> str:
        import cv2
        import numpy as np

        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"画像を読み込めません: {image_path}")
        img = cv2.resize(img, (hash_size * 4, hash_size * 4))
        dct = cv2.dct(np.float32(img))
        top = dct[:hash_size, :hash_size]
        avg = top.mean()
        bits = top.flatten() > avg
        return format(int("".join("1" if b else "0" for b in bits), 2), "016x")

    def distance(self, h1: str, h2: str) -> int:
        a, b = int(h1, 16), int(h2, 16)
        return bin(a ^ b).count("1")


# ─── DHash (勾配ベース) ──────────────────────────────────

class DHashAlgorithm:
    """勾配ベースの差分ハッシュ。構図・エッジ方向に敏感。

    隣接ピクセルの水平勾配を比較するため、
    色調が似ていても構図が異なれば距離が大きくなる。
    """

    @property
    def name(self) -> str:
        return "dhash"

    def compute(self, image_path: Path, hash_size: int = 8) -> str:
        import cv2

        img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f"画像を読み込めません: {image_path}")
        # hash_size+1 列に縮小し、隣接ピクセルの差分を取る
        resized = cv2.resize(img, (hash_size + 1, hash_size))
        diff = resized[:, 1:] > resized[:, :-1]
        return format(int("".join("1" if b else "0" for b in diff.flatten()), 2), "016x")

    def distance(self, h1: str, h2: str) -> int:
        a, b = int(h1, 16), int(h2, 16)
        return bin(a ^ b).count("1")


# ─── 閾値変換テーブル ─────────────────────────────────────

# phash の閾値を各アルゴリズムの等価閾値に変換するマップ。
# キー: phash 閾値 → 値: 各アルゴリズムでの等価閾値。
# ※ 初期値は推定。実データで要チューニング。
_THRESHOLD_MAP: dict[str, dict[int, int]] = {
    "phash": {},  # identity — そのまま返す
    "dhash": {
        3: 3,
        5: 4,
        8: 6,
        12: 10,
        15: 12,
        20: 16,
        30: 24,
    },
}


# ─── ImageComparator ──────────────────────────────────────

class ImageComparator:
    """プラグイン可能な画像類似度比較器。

    Usage:
        cmp = ImageComparator("dhash")
        h = cmp.compute_hash(Path("screenshot.png"))
        d = cmp.distance(h1, h2)
        th = cmp.translate_threshold(30)  # phash 30 → dhash 24
    """

    _ALGORITHMS: dict[str, type] = {
        "phash": PHashAlgorithm,
        "dhash": DHashAlgorithm,
    }

    def __init__(self, algorithm: str = "phash", hash_size: int = 8):
        algo_cls = self._ALGORITHMS.get(algorithm)
        if algo_cls is None:
            raise ValueError(
                f"Unknown algorithm: {algorithm}. "
                f"Available: {list(self._ALGORITHMS.keys())}"
            )
        self._algo: HashAlgorithm = algo_cls()
        self._hash_size = hash_size
        self._threshold_map = _THRESHOLD_MAP.get(algorithm, {})

    @property
    def name(self) -> str:
        """現在のアルゴリズム名。"""
        return self._algo.name

    def compute_hash(self, image_path: Path) -> str:
        """画像からハッシュを計算する。"""
        return self._algo.compute(image_path, self._hash_size)

    def distance(self, h1: str, h2: str) -> int:
        """2つのハッシュ間のハミング距離を返す。"""
        return self._algo.distance(h1, h2)

    def translate_threshold(self, phash_threshold: int) -> int:
        """phash 閾値を現アルゴリズムの等価閾値に変換する。

        マップにないキーは最も近いキーで線形補間する。
        """
        if not self._threshold_map:
            # identity (phash)
            return phash_threshold

        # 完全一致
        if phash_threshold in self._threshold_map:
            return self._threshold_map[phash_threshold]

        # 線形補間
        keys = sorted(self._threshold_map.keys())
        if phash_threshold <= keys[0]:
            ratio = self._threshold_map[keys[0]] / keys[0] if keys[0] > 0 else 1.0
            return max(1, int(phash_threshold * ratio))
        if phash_threshold >= keys[-1]:
            ratio = self._threshold_map[keys[-1]] / keys[-1]
            return int(phash_threshold * ratio)

        # 2点間の補間
        for i in range(len(keys) - 1):
            if keys[i] <= phash_threshold <= keys[i + 1]:
                lo_k, hi_k = keys[i], keys[i + 1]
                lo_v, hi_v = self._threshold_map[lo_k], self._threshold_map[hi_k]
                t = (phash_threshold - lo_k) / (hi_k - lo_k)
                return int(lo_v + t * (hi_v - lo_v))

        return phash_threshold  # fallback


# ─── シングルトン取得 ─────────────────────────────────────

_instance: ImageComparator | None = None


def get_comparator() -> ImageComparator:
    """環境変数 LC_HASH_ALGO に従ったシングルトンを返す。"""
    global _instance
    if _instance is None:
        algo = os.environ.get("LC_HASH_ALGO", "phash").lower()
        _instance = ImageComparator(algorithm=algo)
        logger.info("[ImageComparator] アルゴリズム: %s", _instance.name)
    return _instance


def reset_comparator() -> None:
    """テスト用: シングルトンをリセットする。"""
    global _instance
    _instance = None


# ─── ヒストグラム類似度（dHash 補助） ─────────────────────────

# dHash 中間域でヒストグラム類似度がこの値以上なら同シーンとみなす。
HIST_SIMILARITY_THRESHOLD: float = 0.5


def is_similar_by_histogram(
    image_path1: Path,
    image_path2: Path,
    similarity_threshold: float = HIST_SIMILARITY_THRESHOLD,
) -> bool:
    """2画像のグレースケールヒストグラム類似度が threshold 以上なら True。"""
    from lc.scene_boundary_detector import (
        compute_grayscale_histogram,
        histogram_similarity,
    )

    h1 = compute_grayscale_histogram(image_path1)
    h2 = compute_grayscale_histogram(image_path2)
    return histogram_similarity(h1, h2) >= similarity_threshold
