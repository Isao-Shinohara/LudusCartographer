"""
ap/helpers.py — テキスト判定・シーン分類・証拠保存ユーティリティ
"""
from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from lc.ocr import find_best
from tools.ap.constants import (
    SCENE_INTERVAL, EVIDENCE_DIR,
    APP_PACKAGE, APP_ACTIVITY,
    WATCHDOG_MAX_TOTAL_RECOVERIES,
)

logger = logging.getLogger("auto_pilot")


def classify_scene(texts: list[str], last_action: str,
                    adv_detected: bool = False,
                    current_scene: str = "") -> tuple[str, float]:
    """
    OCR テキストからシーンを分類し (scene_label, poll_interval) を返す。
    - BATTLE  : バトル画面 — detect_scene_early のテンプレートマッチで判定済み
    - ADV     : アドベンチャー — スキップボタンあり or 直前に STORY_TAP
    - STORY   : ストーリー送り — スキップなし・会話テキストのみ
    - LOADING : ロード/ダウンロード中
    - MENU    : ホーム/メニュー画面
    - UNKNOWN : 判定不能

    adv_detected: True なら ADV シーンを確定で返す (detect_adv_scene 由来)。
    current_scene: state.current_scene — テンプレートマッチで確定済みのシーン。
    """
    joined = " ".join(texts)
    if any(kw in joined for kw in ["ダウンロード", "Loading", "Now Loading", "ロード中", "通信中"]):
        return "LOADING", SCENE_INTERVAL["LOADING"]
    if current_scene == "BATTLE":
        return "BATTLE", SCENE_INTERVAL["BATTLE"]
    if any(kw in joined for kw in ["クエスト", "ショップ", "ガシャ", "ガチャ",
                                    "ホーム", "メニュー", "お知らせ", "編成", "光の間"]):
        return "MENU", SCENE_INTERVAL["MENU"]
    # ADV = ツールバー検出 or スキップボタンあり
    if adv_detected or any(kw in joined for kw in ["スキップ", "SKIP"]):
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


# ─── マイルストーン到達時間ログ ────────────────────────
def log_milestone(state, milestone: str) -> None:
    """目標到達時の経過時間をログ出力する。同一マイルストーンは初回のみ記録。"""
    if milestone in state.milestone_logged:
        return
    _now = time.time()
    _elapsed = _now - state.launch_time
    _m, _s = divmod(int(_elapsed), 60)
    _h, _m = divmod(_m, 60)
    if state.is_fresh_start:
        logger.info("  [TIMER] %s — 起動から %d時間%02d分%02d秒 (新規スタート)",
                    milestone, _h, _m, _s)
    else:
        logger.info("  [TIMER] %s — 起動から %d時間%02d分%02d秒 (途中再開のため総所要時間は計測不可)",
                    milestone, _h, _m, _s)
    state.milestone_logged[milestone] = _elapsed


# ─── Watchdog: デッドロック自動復旧 ─────────────────────
def watchdog_recover(state) -> bool:
    """Unityメインスレッドのデッドロックを検出した際の自動復旧。

    戦略 (pm clear は一切使用しない — BAN リスク排除):
      1〜3回目: am force-stop → am start (ソフト再起動のみ)
      4回目以降: 諦めて False を返す (人間に委譲)

    Returns: True=復旧試行を実施, False=諦め(mainが終了する)
    """
    from tools.ap.device import adb  # 遅延 import (循環防止)

    state.watchdog_recovery_count += 1
    count = state.watchdog_recovery_count
    elapsed = time.time() - state.last_screen_change_time

    if count > WATCHDOG_MAX_TOTAL_RECOVERIES:
        logger.error(
            "[WATCHDOG] 復旧試行%d回失敗 (last_action=%s, %.0f秒経過) — 人間の介入が必要です。停止します。",
            count - 1, state.last_action, elapsed
        )
        return False

    logger.warning(
        "[WATCHDOG] デッドロック判定: 画面変化なし %.0f秒 / last_action=%s / 復旧試行 #%d",
        elapsed, state.last_action, count
    )
    logger.warning("[WATCHDOG] → am force-stop → am start (ソフト再起動のみ。pm clearは使用しない)")
    adb(f"shell am force-stop {APP_PACKAGE}")
    time.sleep(3)

    adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
    logger.info("[WATCHDOG] am start 実行 — 15秒待機 (初期化 + ご注意画面の出現を待つ)")
    time.sleep(15)

    state.last_phash = ""
    state.same_phash_count = 0
    state.stall_start = 0.0
    state.stall_corner_tried = False
    state.home_reached = False
    state.auto_activated = False
    state.character_selected = False
    state.char_just_selected = False
    state.battle_wait_count = 0
    state.last_action = "WATCHDOG_RECOVERY"
    state.last_screen_change_time = time.time()
    return True
