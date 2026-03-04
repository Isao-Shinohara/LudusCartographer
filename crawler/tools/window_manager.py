"""
window_manager.py — macOS ミラーリングウィンドウ検索・キャプチャ・前面表示

UxPlay (iOS) または scrcpy (Android) のウィンドウを検索し、
OpenCV 形式 (BGR numpy.ndarray) でキャプチャして返す。

対応プラットフォーム: macOS (Quartz CGWindowListCopyWindowInfo を使用)

依存パッケージ:
  pip install pyobjc-framework-Quartz mss
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# ウィンドウ検索
# ============================================================

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
    result = find_mirroring_window_ex(title_candidates)
    return result[0] if result is not None else None


def find_mirroring_window_ex(
    title_candidates: list[str],
) -> Optional[tuple[tuple[int, int, int, int], str]]:
    """
    find_mirroring_window() の拡張版。

    ウィンドウ領域とオーナーアプリ名を同時に返す。
    ソース判別（UxPlay / QuickTime Player 等）が必要なトリミング処理で使用する。

    Args:
        title_candidates: 検索するタイトルリスト (例: ["UxPlay", "QuickTime Player"])
                          リスト順に検索し、最初に見つかったウィンドウを返す。

    Returns:
        ((x, y, width, height), owner_name) のタプル、または見つからない場合は None。
        owner_name は "UxPlay" / "QuickTime Player" 等の macOS アプリ名。

    Raises:
        NotImplementedError: macOS 以外で呼び出された場合
        ImportError: pyobjc-framework-Quartz がインストールされていない場合
    """
    _require_darwin()
    Quartz = _import_quartz()

    window_list = _get_window_list(Quartz)

    def _extract_rect(win) -> Optional[tuple[int, int, int, int]]:
        bounds = win.get("kCGWindowBounds") or {}
        x      = int(bounds.get("X",      0))
        y      = int(bounds.get("Y",      0))
        width  = int(bounds.get("Width",  0))
        height = int(bounds.get("Height", 0))
        return (x, y, width, height) if width > 0 and height > 0 else None

    # Pass 1: owner 名が候補と完全一致するウィンドウを優先検索
    #   → "uxplay" を実行している Terminal など title に候補名が混入するウィンドウを除外
    for candidate in title_candidates:
        for win in window_list:
            owner = (win.get("kCGWindowOwnerName") or "")
            if owner.lower() == candidate.lower():
                rect = _extract_rect(win)
                if rect:
                    title = (win.get("kCGWindowName") or "")
                    logger.debug(
                        "[WM] ウィンドウ発見(owner一致): owner=%r  title=%r  rect=(%d,%d,%d,%d)",
                        owner, title, *rect,
                    )
                    return (rect, owner)

    # Pass 2: owner + title の結合文字列に候補が含まれるウィンドウ（後方互換フォールバック）
    for win in window_list:
        owner = (win.get("kCGWindowOwnerName") or "")
        title = (win.get("kCGWindowName") or "")
        combined = f"{owner} {title}".lower()
        for candidate in title_candidates:
            if candidate.lower() in combined:
                rect = _extract_rect(win)
                if rect:
                    logger.debug(
                        "[WM] ウィンドウ発見(combined一致): owner=%r  title=%r  rect=(%d,%d,%d,%d)",
                        owner, title, *rect,
                    )
                    return (rect, owner)

    return None


def bring_window_to_front(title_candidates: list[str]) -> bool:
    """
    タイトル候補に一致するウィンドウを前面に持ってくる。

    Quartz でオーナーアプリ名を取得し、AppleScript (osascript) で
    そのアプリをアクティベートする。

    Args:
        title_candidates: 検索するタイトルリスト (例: ["UxPlay", "iPhone"])

    Returns:
        前面表示に成功した場合 True、ウィンドウが見つからない場合 False。

    Raises:
        NotImplementedError: macOS 以外で呼び出された場合
        ImportError: pyobjc-framework-Quartz がインストールされていない場合
    """
    _require_darwin()
    Quartz = _import_quartz()

    window_list = _get_window_list(Quartz)
    owner = _find_window_owner(title_candidates, window_list)
    if not owner:
        logger.debug(f"[WM] 前面表示対象ウィンドウ未発見: {title_candidates}")
        return False

    try:
        script = f'tell application "{owner}" to activate'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            timeout=3.0,
        )
        if result.returncode == 0:
            logger.debug(f"[WM] 前面表示成功: {owner}")
            return True
        else:
            logger.warning(
                f"[WM] 前面表示失敗 (osascript exit={result.returncode}): "
                f"{result.stderr.decode(errors='replace').strip()}"
            )
            return False
    except subprocess.TimeoutExpired:
        logger.warning("[WM] 前面表示タイムアウト (osascript が応答しない)")
        return False
    except Exception as e:
        logger.warning(f"[WM] 前面表示エラー: {e}")
        return False


# ============================================================
# キャプチャ
# ============================================================

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


# ============================================================
# デバッグ用
# ============================================================

def list_all_windows() -> list[dict]:
    """
    現在表示中のすべてのウィンドウ情報を返す (デバッグ用)。

    Returns:
        各ウィンドウの辞書リスト:
          owner  : ウィンドウオーナーアプリ名
          title  : ウィンドウタイトル
          bounds : {"X": int, "Y": int, "Width": int, "Height": int}
    """
    _require_darwin()
    Quartz = _import_quartz()

    window_list = _get_window_list(Quartz)
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


# ============================================================
# 内部ヘルパー
# ============================================================

def _require_darwin() -> None:
    if sys.platform != "darwin":
        raise NotImplementedError(
            "window_manager は macOS 専用です。"
            f" 現在のプラットフォーム: {sys.platform}"
        )


def _import_quartz():
    """Quartz をインポートして返す。未インストール時は ImportError。"""
    try:
        import Quartz
        return Quartz
    except ImportError:
        raise ImportError(
            "pyobjc-framework-Quartz が必要です。\n"
            "  pip install pyobjc-framework-Quartz"
        )


def _get_window_list(Quartz) -> list:
    """オンスクリーンのウィンドウ一覧を取得する。"""
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    return Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)


def _find_window_owner(
    title_candidates: list[str],
    window_list: Optional[list] = None,
) -> Optional[str]:
    """
    タイトル候補に一致するウィンドウのオーナーアプリ名を返す。

    Args:
        title_candidates: 検索するタイトルリスト
        window_list: 既取得のウィンドウリスト (None の場合は再取得)

    Returns:
        オーナーアプリ名、見つからない場合は None
    """
    if window_list is None:
        Quartz = _import_quartz()
        window_list = _get_window_list(Quartz)

    for win in window_list:
        owner = (win.get("kCGWindowOwnerName") or "")
        title = (win.get("kCGWindowName") or "")
        combined = f"{owner} {title}".lower()
        for candidate in title_candidates:
            if candidate.lower() in combined:
                return owner
    return None
