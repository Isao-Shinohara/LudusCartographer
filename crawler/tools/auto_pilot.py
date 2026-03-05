#!/usr/bin/env python3
"""
auto_pilot.py — まどドラ自律操縦スクリプト (ハイブリッド版)

1秒 phash ポーリング → 5秒変化なしで強制 OCR → 指差しアイコン最優先タップ。
WAIT_FOR_CHANGE 後の閾値引き上げを廃止し、デッドロックを根絶。

使い方:
    cd crawler
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\
    venv/bin/python -u tools/auto_pilot.py
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルート
_CRAWLER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_CRAWLER_ROOT))

from lc.ocr import run_ocr, find_text, find_best, format_results
from lc.utils import get_android_serial, compute_phash, phash_distance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_pilot")

# ─── 設定 ────────────────────────────────────────────
try:
    DEVICE_SERIAL = get_android_serial()
except RuntimeError as e:
    logger.error(str(e))
    sys.exit(1)

SCREENSHOT_PATH = "/tmp/lc_autopilot.png"
REMOTE_PATH = "/sdcard/lc_autopilot.png"
EVIDENCE_DIR = _CRAWLER_ROOT / "evidence" / f"autopilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# ─── タイミング ───
MAX_ITERATIONS = 2000
POLL_INTERVAL = 1.0         # phash ポーリング間隔 (秒)
PHASH_THRESHOLD = 5         # phash 距離 >= 5 → 画面変化あり
FORCE_ANALYZE_AFTER = 5     # phash 変化なし連続 N 回 → 強制 OCR (≒5秒)
STALL_TIMEOUT = 25.0        # 強制OCRでもタップできず続く秒数 → スタック介入
BATTLE_WAIT = 5.0
DOWNLOAD_WAIT = 10.0
BLACKOUT_BRIGHTNESS = 20

# ─── 解析基準解像度 ───
ANALYSIS_W = 1520
ANALYSIS_H = 720

OCR_LANG = "japan"
OCR_MIN_CONF = 0.3


@dataclass
class PilotState:
    """操縦状態"""
    iteration: int = 0
    last_phash: str = ""
    stall_start: float = 0.0
    stall_corner_tried: bool = False
    last_action: str = ""
    last_ocr_texts: list = field(default_factory=list)
    battle_wait_count: int = 0
    auto_activated: bool = False
    home_reached: bool = False
    total_taps: int = 0
    total_ocr_calls: int = 0
    total_ocr_skipped: int = 0
    total_blackout_skipped: int = 0
    screenshots_saved: int = 0
    device_w: int = 0
    device_h: int = 0
    # 強制解析カウンタ (phash 変化なしの連続回数)
    same_phash_count: int = 0
    # 最後に強制解析を実行した時刻
    last_forced_ocr_at: float = 0.0
    # 同一位置の指差しブロブ連続検出カウンタ (誤検出抑制)
    last_blob_xy: tuple = (0, 0)
    blob_same_count: int = 0
    # フリーバトル: 左キャラ選択済みフラグ (True なら次は右スキルを優先)
    char_just_selected: bool = False


# ─── ADB ユーティリティ ─────────────────────────────
def adb(cmd: str) -> str:
    full = f"adb -s {DEVICE_SERIAL} {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("adb timeout: %s", cmd)
        return ""


def take_screenshot() -> tuple[Path, int, int]:
    adb(f"shell screencap -p {REMOTE_PATH}")
    subprocess.run(
        f"adb -s {DEVICE_SERIAL} pull {REMOTE_PATH} {SCREENSHOT_PATH}",
        shell=True, capture_output=True, timeout=10,
    )
    path = Path(SCREENSHOT_PATH)
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
    except Exception:
        w, h = ANALYSIS_W, ANALYSIS_H
    return path, w, h


def tap_device(x: int, y: int, state: PilotState, desc: str = "") -> None:
    if state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        real_x = int(x * sx)
        real_y = int(y * sy)
    else:
        real_x, real_y = x, y
    time.sleep(0.3)
    adb(f"shell input tap {real_x} {real_y}")
    state.total_taps += 1
    if (real_x, real_y) != (x, y):
        logger.info("  TAP (%d,%d)→device(%d,%d) %s", x, y, real_x, real_y, desc)
    else:
        logger.info("  TAP (%d,%d) %s", x, y, desc)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", x1, y1, x2, y2, duration_ms)


def save_evidence(img_path: Path, ocr_results: list, action: str, state: PilotState) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    dest = EVIDENCE_DIR / f"{ts}_iter{state.iteration:03d}_{action}.png"
    try:
        import shutil
        shutil.copy2(str(img_path), str(dest))
        state.screenshots_saved += 1
    except Exception as e:
        logger.warning("Evidence save failed: %s", e)


def is_dark_screen(img_path: Path) -> bool:
    try:
        from PIL import Image
        import numpy as np
        with Image.open(img_path) as img:
            gray = img.convert("L")
            return float(np.mean(np.array(gray))) <= BLACKOUT_BRIGHTNESS
    except Exception:
        return False


def prepare_analysis_image(img_path: Path, actual_w: int, actual_h: int) -> Path:
    from PIL import Image
    needs_transform = (actual_w < actual_h) or \
        ((actual_w, actual_h) != (ANALYSIS_W, ANALYSIS_H) and
         (actual_h, actual_w) != (ANALYSIS_W, ANALYSIS_H))
    if not needs_transform:
        return img_path
    analysis_path = Path("/tmp/lc_autopilot_analysis.png")
    img = Image.open(img_path)
    if img.width < img.height:
        img = img.rotate(90, expand=True)
    if img.size != (ANALYSIS_W, ANALYSIS_H):
        img = img.resize((ANALYSIS_W, ANALYSIS_H), Image.LANCZOS)
    img.save(analysis_path)
    return analysis_path


# ─── 指差しアイコン (肌色ブロブ) 検出 ──────────────
def find_finger_blobs(img_path: Path, min_area: int = 400) -> list[tuple[int, int, float]]:
    """
    指差しアイコン（肌色）の大きいブロブを検出。
    battle_loop.py と同じ HSV マスク手法。
    返値: [(cx, cy, area), ...] 面積降順
    """
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(img_path))
        if img is None:
            return []
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower = np.array([5, 40, 150])
        upper = np.array([25, 180, 255])
        mask = cv2.inRange(hsv, lower, upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = []
        for c in contours:
            area = cv2.contourArea(c)
            if area >= min_area:
                M = cv2.moments(c)
                if M["m00"] > 0:
                    cx = int(M["m10"] / M["m00"])
                    cy = int(M["m01"] / M["m00"])
                    blobs.append((cx, cy, area))
        return sorted(blobs, key=lambda b: b[2], reverse=True)
    except ImportError:
        return []
    except Exception as e:
        logger.debug("find_finger_blobs error: %s", e)
        return []


# ─── OCR テキスト検索ヘルパー ──────────────────────
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


# ─── 画面判定・アクション ──────────────────────────
def detect_and_act(ocr: list, state: PilotState,
                   analysis_path: Optional[Path] = None) -> tuple[str, float]:
    """
    OCR + 指差しブロブを分析し、アクションを決定する。
    analysis_path が渡された場合は finger blob 検出も実行。

    Returns: (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    W, H = ANALYSIS_W, ANALYSIS_H

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        is_battle_screen = any(kw in " ".join(texts) for kw in
                               ["AUTO", "通常攻撃", "单体攻撃", "単体攻撃", "必殺技", "BREAK"])
        # min_area は常に400。空間フィルタ(下記)で誤検出を排除するため過大閾値は不要
        blobs = find_finger_blobs(analysis_path, min_area=400)
        if blobs:
            # バトル中は中央エリア(バトルフィールド)の肌色は誤検出なので無視
            # 優先順位: 左キャラカード(x<600,y>550) > 右パネル(x>1050) > 下部UI(y>H*0.8)
            if is_battle_screen:
                left_char = [(x, y, a) for x, y, a in blobs if x < 600 and y > H * 0.76]
                right_panel = [(x, y, a) for x, y, a in blobs if x > 1050]
                bottom_ui = [(x, y, a) for x, y, a in blobs if y > H * 0.8 and x >= 600]
                if state.char_just_selected:
                    # 左キャラ選択済み → 右スキルを選択 (左キャラ再タップしない)
                    if right_panel:
                        blobs = right_panel
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 右スキルもや %d個", len(blobs))
                    elif bottom_ui:
                        blobs = bottom_ui
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 下部UIもや %d個", len(blobs))
                    else:
                        # 右スキルがまだ表示されていない → 少し待つ
                        logger.info("  バトル: キャラ選択後 → 右スキル待ち")
                        blobs = []
                elif left_char:
                    # フリーバトル: 左キャラ選択が最優先
                    blobs = left_char
                    logger.info("  バトル: 左キャラもや %d個 (最優先)", len(blobs))
                elif right_panel:
                    blobs = right_panel
                    logger.info("  バトル: 右パネルもや %d個", len(blobs))
                elif bottom_ui:
                    blobs = bottom_ui
                    logger.info("  バトル: 下部UIもや %d個", len(blobs))
                else:
                    logger.info("  バトル中: 有効もやなし(中央は誤検出) → OCR判定へ")
                    blobs = []

        if blobs:
            # 右側行動アイコン (x > 1050) が最優先
            right_blobs = [(x, y, a) for x, y, a in blobs if x > 1050]
            chosen = right_blobs[0] if right_blobs else blobs[0]
            fx, fy, fa = chosen
            if right_blobs and len(blobs) > 1:
                logger.info("  (右パネル優先: %d個中1個を選択)", len(blobs))
            # 100px グリッドで同一座標判定 (50pxだとy=399/400で境界越えリセットが発生)
            blob_pos = (fx // 100, fy // 100)
            if blob_pos == state.last_blob_xy:
                state.blob_same_count += 1
            else:
                state.blob_same_count = 0
                state.last_blob_xy = blob_pos
            if state.blob_same_count >= 5:
                logger.info(">>> もや同一座標 %d回タップ済み → OCRフォールバック (%d,%d)",
                            state.blob_same_count, fx, fy)
                # カウンタはリセットせず、OCR ベース処理に落ちる
            else:
                logger.info(">>> 【もやアイコン検出】 (%d,%d) area=%.0f count=%d — 直接タップ",
                            fx, fy, fa, state.blob_same_count)
                tap_device(fx, fy, state, f"MOYA_TAP ({fx},{fy})")
                # 左キャラ選択後は char_just_selected フラグをセット
                if fx < 600 and fy > H * 0.76:
                    state.char_just_selected = True
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 2.0

    # ─── 【最優先 #2】ハイライト指示テキスト ───
    tutorial_kws = ["ここをタップ", "タップしてください", "タップして下さい", "タップして"]
    for kw in tutorial_kws:
        match = has_text(ocr, kw, min_conf=0.3)
        if match:
            cx, cy = match["center"]
            logger.info(">>> 【ハイライト指示】 '%s' (%d,%d)", kw, cx, cy)
            tap_device(cx, cy, state, f"HIGHLIGHT '{kw}'")
            return "HIGHLIGHT_TAP", 2.0

    # ─── ホーム画面検出 ───
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成"]
    home_count = sum(1 for h in home_indicators if any(h in t for t in texts))
    if home_count >= 3:
        logger.info(">>> ホーム画面検出! (%d個)", home_count)
        state.home_reached = True
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 ───
    dl = has_any(ocr, ["ダウンロード", "追加データ", "Loading", "ロード中",
                       "通信中", "Now Loading"])
    if dl:
        logger.info(">>> ロード中: '%s' — 待機", dl["text"])
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ─── クエストマップ/ステージ選択 ───
    stage_num = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                               "3-1", "3-2", "4-1", "4-2", "Main"])
    sentu_btn = has_text(ocr, "戦闘") or has_text(ocr, "出撃")
    if not sentu_btn:
        expl = has_text(ocr, "探索")
        if expl and expl["center"][1] > H * 0.6:
            sentu_btn = expl
    if stage_num and sentu_btn:
        cx, cy = sentu_btn["center"]
        logger.info(">>> クエストマップ — 「%s」(%d,%d)", sentu_btn["text"], cx, cy)
        tap_device(cx, cy, state, f"QUEST_START {sentu_btn['text']}")
        state.battle_wait_count = 0
        return "QUEST_START", 5.0
    elif stage_num:
        fx, fy = int(W * 0.74), int(H * 0.91)
        logger.info(">>> クエストマップ(固定) (%d,%d)", fx, fy)
        tap_device(fx, fy, state, "QUEST_START_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 5.0

    # ─── バトル画面 ───
    battle_keywords = ["AUTO", "通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "BREAK", "HP", "Turn", "WAVE", "戦闘"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1

        # バトルチュートリアル: バフ効果
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx, by = int(W * 0.888), int(H * 0.667)
            logger.info(">>> バフチュートリアル (%d,%d)", bx, by)
            tap_device(bx, by, state, "BUFF_TUTORIAL")
            return "BATTLE_TUTORIAL", 2.0

        # バトルチュートリアル: スキル使用
        skill_tut = has_any(ocr, ["スキルを使ってみましょう", "スキを使ってみ",
                                   "戦闘スキルを使", "戦闘スキを使",
                                   "スキルを使用してみ", "使ってみましょう"])
        if skill_tut:
            sx, sy = int(W * 0.947), int(H * 0.722)
            logger.info(">>> スキルチュートリアル (%d,%d)", sx, sy)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL")
            time.sleep(0.8)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 3.0

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            hx, hy = int(W * 0.862), int(H * 0.778)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL")
            time.sleep(0.8)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 3.0

        # バトルチュートリアル: 攻撃対象変更
        if has_any(ocr, ["攻撃対象を変更", "対象を変更"]):
            ex, ey = int(W * 0.651), int(H * 0.361)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 2.0

        # バトルチュートリアル: 一般ポップアップ
        tutorial_popup = has_any(ocr, [
            "タイムライン", "表示されている", "行動してい",
            "ここをタップ", "タップしてください",
            "ことができます", "することができ",
            "スキルを使用", "スキルを選択", "カードを選択",
            "ましょう", "みましょう", "てみよう",
            "一番上に", "順番に行動", "DEFENDER",
        ])
        if tutorial_popup:
            cx, cy = tutorial_popup["center"]
            logger.info(">>> バトルチュートリアル popup '%s' (%d,%d)",
                        tutorial_popup["text"][:10], cx, cy)
            tap_device(cx, cy, state, "BATTLE_TUTORIAL_POPUP")
            return "BATTLE_TUTORIAL", 2.0

        # AUTO ボタン
        if not state.auto_activated:
            ax, ay = int(W * 0.845), int(H * 0.090)
            logger.info(">>> AUTO タップ (%d,%d)", ax, ay)
            tap_device(ax, ay, state, "AUTO_ON")
            state.auto_activated = True
            return "BATTLE_AUTO", BATTLE_WAIT

        # バトル停滞時: ハイライト候補を順番にタップ試行
        if state.battle_wait_count > 8:
            stall_phase = (state.battle_wait_count - 8) % 8
            if stall_phase == 0:
                sx, sy = int(W * 0.947), int(H * 0.722)
                logger.info(">>> バトル停滞 — スキルタップ (%d,%d)", sx, sy)
                tap_device(sx, sy, state, "STALL_SKILL")
                time.sleep(0.8)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 2.0
            elif stall_phase == 4:
                hx, hy = int(W * 0.862), int(H * 0.778)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU")
                time.sleep(0.8)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 2.0

        logger.info(">>> バトル中 — 待機 (count=%d, auto=%s)",
                    state.battle_wait_count, state.auto_activated)
        return "BATTLE_WAIT", BATTLE_WAIT

    # バトル終了検出
    if state.battle_wait_count > 0:
        logger.info(">>> バトル終了検出 (wait_count was %d)", state.battle_wait_count)
        state.battle_wait_count = 0
        state.auto_activated = False

    # ─── バトル結果/リザルト ───
    result_match = has_any(ocr, ["リザルト", "Result", "RESULT", "勝利", "Victory",
                                  "クリア", "CLEAR", "EXP", "経験値", "ランクアップ"])
    if result_match:
        cx, cy = result_match["center"]
        logger.info(">>> バトル結果 '%s' (%d,%d)", result_match["text"], cx, cy)
        tap_device(cx, cy, state, "RESULT_TAP")
        return "RESULT_TAP", 3.0

    # ─── スキップ ───
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if skip_match:
        cx, cy = skip_match["center"]
        logger.info(">>> スキップ '%s' (%d,%d)", skip_match["text"], cx, cy)
        tap_device(cx, cy, state, f"SKIP '{skip_match['text']}'")
        return "SKIP", 3.0

    # ─── 閉じるボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        logger.info(">>> 閉じる '%s' (%d,%d)", close_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CLOSE '{close_match['text']}'")
        return "CLOSE", 2.0

    # ─── 規約同意 ───
    agree_match = has_any(ocr, ["同意", "規約", "利用規約"])
    if agree_match:
        logger.info(">>> 規約画面 — スクロール→同意")
        for _ in range(3):
            swipe(700, 500, 700, 200, 500)
            time.sleep(0.8)
        agree_btn = has_any(ocr, ["同意"])
        if agree_btn:
            cx, cy = agree_btn["center"]
            tap_device(cx, cy, state, "AGREE")
        return "AGREE", 3.0

    # ─── 確認ダイアログ ───
    confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                   "受け取る", "受取", "了解", "わかった",
                                   "進む", "START", "開始",
                                   "TAP TO START", "TOUCH", "始める",
                                   "戦闘", "出撃", "クエスト開始", "バトル開始"])
    if confirm_match:
        cx, cy = confirm_match["center"]
        logger.info(">>> 確認 '%s' (%d,%d)", confirm_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CONFIRM '{confirm_match['text']}'")
        return "CONFIRM", 3.0

    # ─── ストーリー/会話 (下部テキストボックス) ───
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6]
    if lower_texts and len(ocr) <= 15:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 2.0

    # ─── ログインボーナス等 ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        cx, cy = bonus_match["center"]
        logger.info(">>> ポップアップ '%s' (%d,%d)", bonus_match["text"], cx, cy)
        tap_device(cx, cy, state, "POPUP_TAP")
        return "POPUP_TAP", 2.0

    # ─── フォールバック: 何も見つからない ───
    logger.info(">>> 不明な画面 — WAIT_FOR_CHANGE (OCR %d件)", len(ocr))
    return "WAIT_FOR_CHANGE", 0


# ─── コマンドライン引数 ───────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="まどドラ自律操縦")
    parser.add_argument("--verbose", action="store_true", help="デバッグログ出力")
    return parser.parse_args()


# ─── メインループ ─────────────────────────────────
def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (ハイブリッド版)")
    logger.info("  デバイス: %s", DEVICE_SERIAL)
    logger.info("  ポーリング: %.1fs  強制解析: %d回変化なし  スタックTimeout: %.0fs",
                POLL_INTERVAL, FORCE_ANALYZE_AFTER, STALL_TIMEOUT)
    logger.info("=" * 62)

    state = PilotState()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(MAX_ITERATIONS):
        state.iteration = i

        # ── 1) スクリーンショット取得 ──
        img_path, actual_w, actual_h = take_screenshot()
        if not img_path.exists():
            logger.warning("Screenshot failed, retrying...")
            time.sleep(2)
            continue

        if not state.device_w:
            state.device_w = actual_w
            state.device_h = actual_h
            logger.info("実機解像度: %dx%d (解析基準: %dx%d)",
                        actual_w, actual_h, ANALYSIS_W, ANALYSIS_H)

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            state.total_blackout_skipped += 1
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転 — 3s 待機", i)
            state.last_phash = ""
            state.same_phash_count = 0
            time.sleep(3.0)
            continue

        # ── 3) phash 粗解析 ──
        try:
            cur_phash = compute_phash(img_path)
        except Exception:
            cur_phash = ""

        if state.last_phash and cur_phash:
            dist = phash_distance(state.last_phash, cur_phash)
        else:
            dist = 999

        screen_changed = dist >= PHASH_THRESHOLD

        if screen_changed:
            # 画面変化あり → カウンタリセット
            state.same_phash_count = 0
            state.stall_start = 0.0
            state.stall_corner_tried = False
        else:
            # 画面変化なし
            state.same_phash_count += 1
            state.total_ocr_skipped += 1

            # 5秒以上変化なし → 強制 OCR (デッドロック防止の核心)
            if state.same_phash_count >= FORCE_ANALYZE_AFTER:
                logger.info("[iter %d] %d秒変化なし(phash=%d) → 強制 OCR",
                            i, state.same_phash_count, dist)
                screen_changed = True  # OCR ブロックへ進む

            else:
                # まだ待機フェーズ
                if i % 5 == 0:
                    logger.info("[iter %d] phash=%d same=%d — polling...",
                                i, dist, state.same_phash_count)
                state.last_phash = cur_phash
                time.sleep(POLL_INTERVAL)
                continue

            # ── スタック介入 (強制OCRでもタップできず続いた場合) ──
            if state.stall_start == 0.0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                logger.warning(">>> %.0f秒スタック — 右上×ボタン試行", stall_elapsed)
                save_evidence(img_path, [], "STALL_CORNER", state)
                time.sleep(0.3)
                adb(f"shell input tap {actual_w - 40} 40")
                state.total_taps += 1
                state.stall_corner_tried = True
                state.last_phash = ""
                state.same_phash_count = 0
                time.sleep(2)
                continue

            if stall_elapsed >= STALL_TIMEOUT * 2 and state.stall_corner_tried:
                logger.error(">>> %.0f秒スタック解消不能 — 停止", stall_elapsed)
                save_evidence(img_path, [], "STALL_FATAL", state)
                logger.info("  総タップ: %d  OCR実行: %d  スキップ: %d  暗転: %d",
                            state.total_taps, state.total_ocr_calls,
                            state.total_ocr_skipped, state.total_blackout_skipped)
                return

        # ── 4) 解析用画像の準備 ──
        state.last_phash = cur_phash
        analysis_path = prepare_analysis_image(img_path, actual_w, actual_h)

        # ── 5) OCR 精査 ──
        state.total_ocr_calls += 1
        try:
            ocr_results = run_ocr(str(analysis_path), lang=OCR_LANG,
                                  min_confidence=OCR_MIN_CONF)
        except Exception as e:
            logger.error("OCR failed: %s", e)
            state.last_phash = ""  # 次回も確実に解析
            time.sleep(2)
            continue

        texts = all_texts(ocr_results)
        logger.info("[iter %d] phash=%d same=%d OCR(%d): %s",
                    i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── 6) 判定 & アクション (finger blob も渡す) ──
        action, wait_sec = detect_and_act(ocr_results, state, analysis_path)
        state.last_action = action

        # タップ成功時: スタックカウンタリセット
        if action not in ("WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT"):
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.same_phash_count = 0

        # エビデンス保存
        if i % 20 == 0 or action in ("HOME_REACHED", "SKIP", "AGREE", "RESULT_TAP"):
            save_evidence(img_path, ocr_results, action, state)

        # ── 7) ホーム到達チェック ──
        if state.home_reached:
            logger.info("=" * 62)
            logger.info("  ホーム画面に到達しました!")
            logger.info("  総タップ: %d  イテレーション: %d", state.total_taps, i + 1)
            logger.info("  OCR実行: %d  スキップ: %d  暗転: %d",
                        state.total_ocr_calls, state.total_ocr_skipped,
                        state.total_blackout_skipped)
            logger.info("=" * 62)
            save_evidence(img_path, ocr_results, "FINAL_HOME", state)
            return

        # ── 8) 待機 ──
        if wait_sec > 0:
            logger.info("  [%s] wait %.1fs", action, wait_sec)
            time.sleep(wait_sec)

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)


if __name__ == "__main__":
    main()
