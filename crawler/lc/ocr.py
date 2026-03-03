"""
ocr.py — PaddleOCR ユーティリティモジュール

画像ファイルから文字列・信頼スコア・バウンディングボックス・中心座標を抽出する。
CLAUDE.md §8 OCRフォールバックで使用する「座標指定タップ」の基盤。

PaddleOCR 3.x API:
    ocr.predict(image_path) → list[dict]
        dict keys: rec_texts, rec_scores, rec_polys
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# PaddleOCR のインポート（未インストール時も他機能は使えるよう遅延 import）
try:
    from paddleocr import PaddleOCR as _PaddleOCR
    _HAS_PADDLE = True
except ImportError:
    _HAS_PADDLE = False

# シングルトン: lang ごとにインスタンスをキャッシュ（モデル再読み込みを防ぐ）
_ocr_instances: dict[str, "_PaddleOCR"] = {}


def _get_ocr(lang: str) -> "_PaddleOCR":
    if not _HAS_PADDLE:
        raise ImportError(
            "PaddleOCR が見つかりません。\n"
            "  pip install paddleocr で導入してください。"
        )
    if lang not in _ocr_instances:
        _ocr_instances[lang] = _PaddleOCR(
            use_textline_orientation=True,
            lang=lang,
            device="cpu",
        )
    return _ocr_instances[lang]


# ============================================================
# 公開 API
# ============================================================

def run_ocr(
    image_path: Union[str, Path],
    lang: str = "japan",
    min_confidence: float = 0.0,
) -> list[dict]:
    """
    画像に対して PaddleOCR を実行し、構造化された結果リストを返す。

    Args:
        image_path     : 解析する画像ファイルのパス
        lang           : OCR 言語 ("japan" / "en" / "ch")
        min_confidence : この値以上の信頼スコアの結果のみ返す (0.0 = 全て)

    Returns:
        [
            {
                "text":       "認識テキスト",
                "confidence": 0.98,
                "box":        [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],
                "center":     [cx, cy],   # ボックスの中心座標
            },
            ...
        ]
    """
    ocr = _get_ocr(lang)
    predict_results = ocr.predict(str(image_path))

    results: list[dict] = []
    if not predict_results:
        return results

    r = predict_results[0]
    texts  = r.get("rec_texts",  []) or []
    scores = r.get("rec_scores", []) or []
    polys  = r.get("rec_polys",  []) or []

    for text, confidence, poly in zip(texts, scores, polys):
        if not text:
            continue
        conf = float(confidence)
        if conf < min_confidence:
            continue
        box = [list(map(int, point)) for point in poly]
        results.append({
            "text":       text,
            "confidence": conf,
            "box":        box,
            "center":     center_of_box(box),
        })

    logger.debug(f"[OCR] {Path(image_path).name}: {len(results)} 件検出 (lang={lang})")
    return results


def center_of_box(box: list[list[int]]) -> list[int]:
    """
    4点のバウンディングボックスから中心座標を計算する。

    Args:
        box: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]

    Returns:
        [cx, cy]
    """
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return [int(sum(xs) / len(xs)), int(sum(ys) / len(ys))]


def find_text(
    results: list[dict],
    keyword: str,
    min_confidence: float = 0.5,
) -> list[dict]:
    """
    OCR 結果から特定キーワードを含むエントリを返す。

    Args:
        results        : run_ocr() の戻り値
        keyword        : 検索キーワード（部分一致）
        min_confidence : 最低信頼スコア

    Returns:
        マッチしたエントリのリスト（見つからなければ空リスト）
    """
    return [
        r for r in results
        if keyword in r["text"] and r["confidence"] >= min_confidence
    ]


def find_best(
    results: list[dict],
    keyword: str,
    min_confidence: float = 0.5,
) -> dict | None:
    """
    OCR 結果から最も信頼スコアの高い一致エントリを返す。
    見つからなければ None。
    """
    matches = find_text(results, keyword, min_confidence)
    if not matches:
        return None
    return max(matches, key=lambda r: r["confidence"])


def format_results(results: list[dict]) -> str:
    """
    OCR 結果を人間が読みやすい文字列にフォーマットする。
    ログ出力・デバッグ用。
    """
    lines = [
        "=" * 62,
        "  PaddleOCR 認識結果一覧",
        "=" * 62,
    ]
    if not results:
        lines.append("  (認識結果なし)")
    for i, r in enumerate(results, 1):
        cx, cy = r["center"]
        lines.append(
            f"  [{i:02d}] conf={r['confidence']:.3f}  center=({cx:4d},{cy:4d})"
            f"  {r['text']!r}"
        )
    lines += [
        "=" * 62,
        f"  合計: {len(results)} 件",
        "=" * 62,
    ]
    return "\n".join(lines)
