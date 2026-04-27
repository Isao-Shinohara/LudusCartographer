"""
image_comparator.py — 画像類似度比較ユーティリティ

phash と dHash の両アルゴリズムを並走させる:
  - phash: DCT ベースの知覚ハッシュ (大局構造)
  - dHash: 隣接ピクセル差分ハッシュ (構図エッジ)

メインクラスタリングは phash 即決 + dHash 中間域判定の二段構え (cluster_decision.py)。
両ハッシュは画像登録時に同時計算され DB に保存される (screen_recorder.py)。

使い方:
    from lc.image_comparator import compute_phash, compute_dhash, phash_distance, dhash_distance

    p = compute_phash(img_path)
    d = compute_dhash(img_path)
    d1 = phash_distance(p1, p2)
    d2 = dhash_distance(d1, d2)
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


# ─── PHash (DCT ベース) ──────────────────────────────────


def compute_phash(image_path: Path | str, hash_size: int = 8) -> str:
    """DCT ベース知覚ハッシュ (16桁hex)。"""
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


def phash_distance(h1: str, h2: str) -> int:
    """phash 同士の Hamming 距離。"""
    a, b = int(h1, 16), int(h2, 16)
    return bin(a ^ b).count("1")


# ─── DHash (勾配ベース) ──────────────────────────────────


def compute_dhash(image_path: Path | str, hash_size: int = 8) -> str:
    """隣接ピクセル差分ハッシュ (16桁hex)。構図・エッジに敏感。"""
    import cv2

    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"画像を読み込めません: {image_path}")
    resized = cv2.resize(img, (hash_size + 1, hash_size))
    diff = resized[:, 1:] > resized[:, :-1]
    return format(int("".join("1" if b else "0" for b in diff.flatten()), 2), "016x")


def dhash_distance(h1: str, h2: str) -> int:
    """dHash 同士の Hamming 距離。"""
    a, b = int(h1, 16), int(h2, 16)
    return bin(a ^ b).count("1")
