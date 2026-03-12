"""
ocr.py — OCR ユーティリティモジュール (デュアルエンジン)

高速パターン: macOS Vision framework (ocrmac) — ~300ms
汎用パターン: PaddleOCR 3.x (CPU) — ~7-8s

macOS 環境では自動的に Vision framework を使用し、
非 macOS 環境や Vision 未インストール時は PaddleOCR にフォールバックする。
"""
from __future__ import annotations

import logging
import platform
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# ─── Vision framework (macOS) ───────────────────────────────
_HAS_VISION = False
if platform.system() == "Darwin":
    try:
        from ocrmac.ocrmac import OCR as _VisionOCR
        _HAS_VISION = True
    except ImportError:
        pass

# ─── PaddleOCR (汎用) ──────────────────────────────────────
try:
    from paddleocr import PaddleOCR as _PaddleOCR
    _HAS_PADDLE = True
except ImportError:
    _HAS_PADDLE = False

# シングルトン: lang ごとにインスタンスをキャッシュ（モデル再読み込みを防ぐ）
_ocr_instances: dict[str, "_PaddleOCR"] = {}

# エンジン選択ログ（初回のみ）
_engine_logged = False

# 環境変数でエンジン強制指定:
#   OCR_ENGINE=vision  → Vision framework (macOS のみ)
#   OCR_ENGINE=paddle  → PaddleOCR
#   OCR_ENGINE=auto    → 自動選択 (デフォルト)
import os
_OCR_ENGINE_OVERRIDE = os.environ.get("OCR_ENGINE", "auto").lower()


def _get_paddle(lang: str) -> "_PaddleOCR":
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


def _run_ocr_vision(
    image_path: str,
    min_confidence: float = 0.0,
) -> list[dict]:
    """macOS Vision framework で OCR を実行。"""
    from PIL import Image as _PILImage
    with _PILImage.open(image_path) as _img:
        img_w, img_h = _img.size

    results_raw = _VisionOCR(
        image_path,
        language_preference=["ja-JP", "en-US"],
    ).recognize()

    results: list[dict] = []
    for text, confidence, bbox in results_raw:
        if not text:
            continue
        conf = float(confidence)
        if conf < min_confidence:
            continue

        # bbox = [x, y, w, h] 正規化座標 (0-1), 原点=左下
        bx, by, bw, bh = bbox
        # ピクセル座標に変換 (原点=左上)
        px = int(bx * img_w)
        py = int((1.0 - by - bh) * img_h)
        pw = int(bw * img_w)
        ph = int(bh * img_h)

        # 4点ボックス (PaddleOCR 互換形式)
        box = [
            [px, py],
            [px + pw, py],
            [px + pw, py + ph],
            [px, py + ph],
        ]
        center = [px + pw // 2, py + ph // 2]

        results.append({
            "text": text,
            "confidence": conf,
            "box": box,
            "center": center,
        })

    return results


def _run_ocr_paddle(
    image_path: str,
    lang: str = "japan",
    min_confidence: float = 0.0,
) -> list[dict]:
    """PaddleOCR で OCR を実行。"""
    ocr = _get_paddle(lang)
    predict_results = ocr.predict(image_path)

    results: list[dict] = []
    if not predict_results:
        return results

    r = predict_results[0]
    texts = r.get("rec_texts", []) or []
    scores = r.get("rec_scores", []) or []
    polys = r.get("rec_polys", []) or []

    # 画像の実寸取得（OOB座標修正用）
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as _img:
            img_w, img_h = _img.size
    except Exception:
        img_w, img_h = None, None

    for text, confidence, poly in zip(texts, scores, polys):
        if not text:
            continue
        conf = float(confidence)
        if conf < min_confidence:
            continue
        box = [list(map(int, point)) for point in poly]
        center = center_of_box(box)

        # OOB座標修正: PaddleOCR が画像を転置処理した場合、X/Y が入れ替わる
        if img_w and img_h:
            cx, cy = center
            if (cy > img_h or cx > img_w) and cy <= img_w and cx <= img_h:
                center = [cy, cx]
                box = [[p[1], p[0]] for p in box]
                logger.debug("[OCR] OOB補正 '%s': (%d,%d) → (%d,%d)",
                             text, cx, cy, cy, cx)

        results.append({
            "text": text,
            "confidence": conf,
            "box": box,
            "center": center,
        })

    return results


# ============================================================
# 公開 API
# ============================================================

def run_ocr(
    image_path: Union[str, Path],
    lang: str = "japan",
    min_confidence: float = 0.0,
) -> list[dict]:
    """
    画像に対して OCR を実行し、構造化された結果リストを返す。

    エンジン選択:
        macOS + ocrmac → Vision framework (~300ms)
        それ以外       → PaddleOCR (~7-8s)

    Returns:
        [
            {
                "text":       "認識テキスト",
                "confidence": 0.98,
                "box":        [[x1,y1],[x2,y2],[x3,y3],[x4,y4]],
                "center":     [cx, cy],
            },
            ...
        ]
    """
    global _engine_logged
    image_path = str(image_path)

    t0 = time.time()

    # エンジン選択: 環境変数 OCR_ENGINE で強制指定可能
    use_vision = (
        (_OCR_ENGINE_OVERRIDE == "vision" and _HAS_VISION) or
        (_OCR_ENGINE_OVERRIDE == "auto" and _HAS_VISION)
    )
    use_paddle = (
        _OCR_ENGINE_OVERRIDE == "paddle" or
        (not use_vision and _HAS_PADDLE)
    )

    if use_vision:
        if not _engine_logged:
            logger.info("[OCR] エンジン: macOS Vision framework (高速モード)"
                        " | OCR_ENGINE=%s で切替可能", _OCR_ENGINE_OVERRIDE)
            _engine_logged = True
        results = _run_ocr_vision(image_path, min_confidence)
    elif use_paddle:
        if not _engine_logged:
            logger.info("[OCR] エンジン: PaddleOCR (汎用モード)"
                        " | OCR_ENGINE=%s で切替可能", _OCR_ENGINE_OVERRIDE)
            _engine_logged = True
        results = _run_ocr_paddle(image_path, lang, min_confidence)
    else:
        raise ImportError(
            "OCR エンジンが利用不可。\n"
            "  macOS: pip install ocrmac\n"
            "  汎用:  pip install paddleocr"
        )

    elapsed_ms = (time.time() - t0) * 1000
    logger.debug("[OCR] %s: %d件 %.0fms (%s)",
                 Path(image_path).name, len(results), elapsed_ms,
                 "Vision" if _HAS_VISION else "Paddle")
    return results


def center_of_box(box: list[list[int]]) -> list[int]:
    """4点のバウンディングボックスから中心座標を計算する。"""
    xs = [p[0] for p in box]
    ys = [p[1] for p in box]
    return [int(sum(xs) / len(xs)), int(sum(ys) / len(ys))]


def find_text(
    results: list[dict],
    keyword: str,
    min_confidence: float = 0.5,
) -> list[dict]:
    """OCR 結果から特定キーワードを含むエントリを返す。"""
    return [
        r for r in results
        if keyword in r["text"] and r["confidence"] >= min_confidence
    ]


def find_best(
    results: list[dict],
    keyword: str,
    min_confidence: float = 0.5,
) -> dict | None:
    """OCR 結果から最も信頼スコアの高い一致エントリを返す。"""
    matches = find_text(results, keyword, min_confidence)
    if not matches:
        return None
    return max(matches, key=lambda r: r["confidence"])


def format_results(results: list[dict]) -> str:
    """OCR 結果を人間が読みやすい文字列にフォーマットする。"""
    engine = "Vision" if _HAS_VISION else "PaddleOCR"
    lines = [
        "=" * 62,
        f"  {engine} 認識結果一覧",
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
