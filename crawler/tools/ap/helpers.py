"""
ap/helpers.py — テキスト判定・シーン分類・証拠保存ユーティリティ
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from lc.ocr import find_best
from tools.ap.constants import (
    SCENE_INTERVAL, _BATTLE_CORE_KWS, EVIDENCE_DIR,
)

logger = logging.getLogger("auto_pilot")


def classify_scene(texts: list[str], last_action: str) -> tuple[str, float]:
    """
    OCR テキストからシーンを分類し (scene_label, poll_interval) を返す。
    - BATTLE  : バトル画面 — 戦闘固有キーワードあり
    - ADV     : アドベンチャー — スキップボタンあり or 直前に STORY_TAP
    - STORY   : ストーリー送り — スキップなし・会話テキストのみ
    - LOADING : ロード/ダウンロード中
    - MENU    : ホーム/メニュー画面
    - UNKNOWN : 判定不能
    """
    joined = " ".join(texts)
    if any(kw in joined for kw in ["ダウンロード", "Loading", "Now Loading", "ロード中", "通信中"]):
        return "LOADING", SCENE_INTERVAL["LOADING"]
    if any(kw in joined for kw in _BATTLE_CORE_KWS) or "ENEMY TURN" in joined:
        return "BATTLE", SCENE_INTERVAL["BATTLE"]
    if any(kw in joined for kw in ["クエスト", "ショップ", "ガシャ", "ガチャ",
                                    "ホーム", "メニュー", "お知らせ", "編成", "光の間"]):
        return "MENU", SCENE_INTERVAL["MENU"]
    # ADV = スキップボタンあり（能動的に会話が進む）
    if any(kw in joined for kw in ["スキップ", "SKIP"]):
        return "ADV", SCENE_INTERVAL["ADV"]
    # STORY = 直前アクションが会話送り、またはスキップなし会話テキスト
    if last_action in ("STORY_TAP", "ADV_RAPID_TAP", "STORY_TAP_HINT"):
        return "STORY", SCENE_INTERVAL["STORY"]
    # STORY ヒューリスティック: 長い日本語文章 (8文字超 + ひらがな含む) が2件以上
    story_lines = [t for t in texts if len(t) >= 8 and
                   any(0x3041 <= ord(c) <= 0x30FF for c in t)]
    if len(story_lines) >= 2:
        return "STORY", SCENE_INTERVAL["STORY"]
    return "UNKNOWN", SCENE_INTERVAL["UNKNOWN"]


def text_core_center(
    button_region: tuple[int, int, int, int],
    ocr_items: list[dict],
    label: str = "",
) -> tuple[int, int]:
    """Text-Core Priority Algorithm: テキスト中心優先のタップ座標決定。

    STEP 1: button_region (B) 内に OCR テキスト中心が存在するか判定
    STEP 2: テキストあり → テキスト領域の中心座標を返す
    STEP 3: テキストなし → B の中心（下部15%除外）を返す

    Args:
        button_region : ボタン検出領域 (x, y, width, height)
        ocr_items     : OCR 結果リスト [{"text", "center", "confidence", "box"}, ...]
        label         : ログ用ラベル
    Returns: (tap_x, tap_y)
    """
    bx, by, bw, bh = button_region

    # STEP 1: B 内のテキストを検索
    texts_in_button = []
    for item in ocr_items:
        tcx, tcy = item["center"]
        if bx <= tcx <= bx + bw and by <= tcy <= by + bh:
            texts_in_button.append(item)

    if texts_in_button:
        # STEP 2: 最も信頼度の高いテキストの中心を使用
        best = max(texts_in_button, key=lambda r: r["confidence"])
        tx, ty = best["center"]
        logger.debug("[SmartTap] text='%s' btn(%d,%d,%d,%d)→(%d,%d)%s",
                     best["text"], bx, by, bw, bh, tx, ty,
                     f" {label}" if label else "")
        return tx, ty

    # STEP 3: テキストなし → B の中心（下部15%除外）
    effective_h = int(bh * 0.85)
    cx = bx + bw // 2
    cy = by + effective_h // 2
    logger.debug("[SmartTap] no-text btn(%d,%d,%d,%d)→(%d,%d)%s",
                 bx, by, bw, bh, cx, cy,
                 f" {label}" if label else "")
    return cx, cy


def save_evidence(img_path: Path, ocr_results: list, action: str, state) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    dest = EVIDENCE_DIR / f"{ts}_iter{state.iteration:03d}_{action}.png"
    try:
        shutil.copy2(str(img_path), str(dest))
        state.screenshots_saved += 1
    except Exception as e:
        logger.warning("Evidence save failed: %s", e)


def has_any(ocr: list, keywords: list[str], min_conf: float = 0.3) -> Optional[dict]:
    for kw in keywords:
        match = find_best(ocr, kw, min_confidence=min_conf)
        if match:
            return match
    return None


def has_text(ocr: list, keyword: str, min_conf: float = 0.3) -> Optional[dict]:
    return find_best(ocr, keyword, min_confidence=min_conf)


def all_texts(ocr: list) -> list[str]:
    return [r["text"] for r in ocr]
