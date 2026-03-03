"""
window_manager.py — macOS ミラーリングウィンドウ検索・キャプチャ

UxPlay (iOS) または scrcpy (Android) のウィンドウを検索し、
OpenCV 形式 (BGR numpy.ndarray) でキャプチャして返す。

対応プラットフォーム: macOS (Quartz CGWindowListCopyWindowInfo を使用)

依存パッケージ:
  pip install pyobjc-framework-Quartz mss
"""

from __future__ import annotations

import sys
from typing import Optional

import numpy as np


def find_mirroring_window(
    title_candidates: list[str],
) -> Optional[tuple[int, int, int, int]]:
    """
    タイトル候補のいずれかに部分一致するウィンドウを検索し、
    その領域 (x, y, width, height) をスクリーン論理座標で返す。

    ウィンドウオーナー名 + ウィンドウタイトルを結合した文字列に対して
    大文字小文字を区別しない部分一致検索を行う。

    Args:
        title_candidates: 検索するタイトルリスト (例: ["UxPlay", "iPhone"])
                          リスト順に検索し、最初に見つかったウィンドウを返す。

    Returns:
        (x, y, width, height) のタプル、または見つからない場合は None。
        座標は macOS スクリーン論理座標 (左上原点)。

    Raises:
        NotImplementedError: macOS 以外で呼び出された場合
        ImportError: pyobjc-framework-Quartz がインストールされていない場合
    """
    if sys.platform != "darwin":
        raise NotImplementedError(
            "window_manager は macOS 専用です。"
            f" 現在のプラットフォーム: {sys.platform}"
        )

    try:
        import Quartz
    except ImportError:
        raise ImportError(
            "pyobjc-framework-Quartz が必要です。\n"
            "  pip install pyobjc-framework-Quartz"
        )

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    window_list = Quartz.CGWindowListCopyWindowInfo(
        options, Quartz.kCGNullWindowID
    )

    for win in window_list:
        owner = (win.get("kCGWindowOwnerName") or "")
        title = (win.get("kCGWindowName") or "")
        combined = f"{owner} {title}".lower()

        for candidate in title_candidates:
            if candidate.lower() in combined:
                bounds = win.get("kCGWindowBounds") or {}
                x      = int(bounds.get("X",      0))
                y      = int(bounds.get("Y",      0))
                width  = int(bounds.get("Width",  0))
                height = int(bounds.get("Height", 0))
                if width > 0 and height > 0:
                    return (x, y, width, height)

    return None


def capture_region(rect: tuple[int, int, int, int]) -> np.ndarray:
    """
    指定領域 (x, y, width, height) をスクリーンショットとしてキャプチャし、
    OpenCV 形式 (BGR, numpy.ndarray) で返す。

    Args:
        rect: (x, y, width, height) — スクリーン論理座標 (左上原点)

    Returns:
        BGR numpy.ndarray、shape = (height, width, 3)

    Raises:
        ImportError: mss がインストールされていない場合
    """
    try:
        import mss
    except ImportError:
        raise ImportError(
            "mss が必要です。\n"
            "  pip install mss"
        )

    x, y, width, height = rect

    with mss.mss() as sct:
        monitor = {"top": y, "left": x, "width": width, "height": height}
        sct_img = sct.grab(monitor)
        # mss は BGRA を返す。OpenCV (BGR) に変換するため alpha チャネルを除去
        img_bgra = np.array(sct_img, dtype=np.uint8)
        img_bgr  = img_bgra[:, :, :3]

    return img_bgr


def list_all_windows() -> list[dict]:
    """
    現在表示中のすべてのウィンドウ情報を返す (デバッグ用)。

    Returns:
        各ウィンドウの辞書リスト:
          owner  : ウィンドウオーナーアプリ名
          title  : ウィンドウタイトル
          bounds : {"X": int, "Y": int, "Width": int, "Height": int}
    """
    if sys.platform != "darwin":
        raise NotImplementedError("window_manager は macOS 専用です。")

    try:
        import Quartz
    except ImportError:
        raise ImportError("pip install pyobjc-framework-Quartz")

    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    window_list = Quartz.CGWindowListCopyWindowInfo(
        options, Quartz.kCGNullWindowID
    )

    results = []
    for win in window_list:
        bounds = win.get("kCGWindowBounds") or {}
        w = int(bounds.get("Width",  0))
        h = int(bounds.get("Height", 0))
        if w > 0 and h > 0:
            results.append({
                "owner":  win.get("kCGWindowOwnerName", ""),
                "title":  win.get("kCGWindowName",      ""),
                "bounds": {"X": int(bounds.get("X", 0)),
                           "Y": int(bounds.get("Y", 0)),
                           "Width": w, "Height": h},
            })
    return results
