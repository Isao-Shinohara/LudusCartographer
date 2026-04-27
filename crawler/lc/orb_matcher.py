"""orb_matcher — ORB 特徴記述子による画像マッチング

ORB は局所特徴点を見るので、構図全体は似ているが内容が違う画像
(例: 同じ UI レイアウトで違うキャラ・テキスト) を区別できる。dHash の弱点を補う。

使い方:
    from lc.orb_matcher import compute_descriptors, match_rate
    blob = compute_descriptors("/path/img.png")
    rate = match_rate(blob1, blob2)  # 0.0〜1.0
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import cv2
import numpy as np

ORB_FEATURES = 500
ORB_DESCRIPTOR_SIZE = 32  # bytes per feature (default ORB)
LOWE_RATIO = 0.75


def compute_descriptors(image_path: Path | str) -> bytes:
    """ORB 特徴記述子を計算し DB 保存用 bytes で返す。

    特徴点 0 個 (黒画面・ぼかし等) の場合は空 bytes。
    """
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return b''
    orb = cv2.ORB_create(nfeatures=ORB_FEATURES)
    _, des = orb.detectAndCompute(img, None)
    if des is None or len(des) == 0:
        return b''
    return des.tobytes()


def descriptors_from_blob(blob: Optional[bytes]) -> Optional[np.ndarray]:
    """BLOB から ndarray (uint8, shape=(N,32)) に復元。"""
    if not blob:
        return None
    arr = np.frombuffer(blob, dtype=np.uint8)
    if len(arr) == 0 or len(arr) % ORB_DESCRIPTOR_SIZE != 0:
        return None
    return arr.reshape(-1, ORB_DESCRIPTOR_SIZE)


def match_rate(blob1: Optional[bytes], blob2: Optional[bytes]) -> float:
    """2 画像の ORB マッチ率を計算 (0.0〜1.0)。

    Lowe's ratio test で good matches を抽出し、
    good / max(len(d1), len(d2)) を返す。
    特徴点が少ない (< 2) 場合は 0.0。
    """
    d1 = descriptors_from_blob(blob1)
    d2 = descriptors_from_blob(blob2)
    if d1 is None or d2 is None or len(d1) < 2 or len(d2) < 2:
        return 0.0
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    try:
        matches = bf.knnMatch(d1, d2, k=2)
    except cv2.error:
        return 0.0
    good = 0
    for pair in matches:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance < LOWE_RATIO * n.distance:
            good += 1
    return good / max(len(d1), len(d2))
