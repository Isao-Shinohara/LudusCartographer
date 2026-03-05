#!/usr/bin/env python3
"""
battle_loop.py — まどドラ チュートリアルバトル自律進行スクリプト

指差しアイコン（肌色ブロブ）とゴールドハイライトを検出し、
チュートリアルの指示に従って自動的にタップする。

ルール:
1. 右パネル(x>1050)に大きい指差し → 必殺技/戦闘スキルカードをタップ
2. 左カード(x<600)に大きい指差し → ハイライトされたキャラカードをタップ
3. 指差しなし + バトルUI表示中 → 通常攻撃をタップ
4. テキストメッセージ表示中 → 画面中央タップ
5. ポップアップ → 右下閉じるボタン
6. Result/勝利 → タップして進む
7. ホーム画面要素3つ以上 → 終了
"""
import cv2
import numpy as np
import subprocess
import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("battle_loop")

from lc.utils import get_android_serial

try:
    SERIAL = get_android_serial()
except RuntimeError as e:
    logger.error(str(e))
    sys.exit(1)
SS_LOCAL = "/tmp/lc_bl.png"
SS_REMOTE = "/sdcard/lc_bl.png"
SCREEN_W, SCREEN_H = 1520, 720

def adb(cmd):
    try:
        r = subprocess.run(f"adb -s {SERIAL} {cmd}", shell=True, capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except:
        return ""

def screenshot():
    adb(f"shell screencap -p {SS_REMOTE}")
    subprocess.run(f"adb -s {SERIAL} pull {SS_REMOTE} {SS_LOCAL}", shell=True, capture_output=True, timeout=10)
    return SS_LOCAL

def tap(x, y, desc=""):
    adb(f"shell input tap {x} {y}")
    logger.info("TAP (%d,%d) %s", x, y, desc)

def find_finger_blobs(img, min_area=500):
    """指差しアイコン（肌色）の大きいブロブを検出"""
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
            if M['m00'] > 0:
                cx = int(M['m10']/M['m00'])
                cy = int(M['m01']/M['m00'])
                blobs.append((cx, cy, area))
    return sorted(blobs, key=lambda b: b[2], reverse=True)

def ocr_quick(path):
    """OCR実行（重いので必要な時のみ）"""
    try:
        from lc.ocr import run_ocr
        return run_ocr(path, lang='japan', min_confidence=0.3)
    except:
        return []

def main():
    logger.info("=== まどドラ バトル自律ループ開始 ===")

    ocr_initialized = False
    consecutive_no_change = 0
    last_action = ""

    for iteration in range(300):
        # スクリーンショット
        ss = screenshot()
        img = cv2.imread(ss)
        if img is None:
            time.sleep(2)
            continue

        h, w = img.shape[:2]

        # 指差しアイコン検出
        fingers = find_finger_blobs(img, min_area=500)

        # 右パネル (x > 1050) の指差し
        right_fingers = [(x,y,a) for x,y,a in fingers if x > 1050]
        # 左カード (x < 600) の指差し
        left_fingers = [(x,y,a) for x,y,a in fingers if x < 600]
        # 中央の指差し (600 < x < 1050)
        center_fingers = [(x,y,a) for x,y,a in fingers if 600 <= x <= 1050]

        logger.info("[iter %d] fingers: right=%d left=%d center=%d",
                     iteration, len(right_fingers), len(left_fingers), len(center_fingers))

        if right_fingers:
            # 右パネルに指差し → スキルカードをタップ
            fx, fy, fa = right_fingers[0]
            logger.info("  >>> 右パネル指差し at (%d,%d) area=%d → スキルカードタップ", fx, fy, fa)
            tap(1400, 580, "skill card")
            time.sleep(5)
            last_action = "SKILL"
            consecutive_no_change = 0
            continue

        if left_fingers:
            # 左カードに指差し → ハイライトキャラをタップ
            fx, fy, fa = left_fingers[0]
            # 指差しの位置に最も近いカード位置を推定
            # カード中心: 左=130, 中=310, 右=490 (y≈650)
            card_centers = [130, 310, 490]
            closest = min(card_centers, key=lambda cx: abs(cx - fx))
            logger.info("  >>> 左カード指差し at (%d,%d) → カード(%d,650)タップ", fx, fy, closest)
            tap(closest, 650, f"char card x={closest}")
            time.sleep(4)
            last_action = "CHAR_CARD"
            consecutive_no_change = 0
            continue

        if center_fingers:
            # 中央に指差し → 敵/対象をタップ
            fx, fy, fa = center_fingers[0]
            logger.info("  >>> 中央指差し at (%d,%d) → タップ", fx, fy)
            tap(fx, fy, "center finger target")
            time.sleep(3)
            last_action = "CENTER_TARGET"
            consecutive_no_change = 0
            continue

        # 指差しなし → OCRで状況判定
        if not ocr_initialized:
            logger.info("  OCR初期化中...")

        results = ocr_quick(ss)
        texts = [r['text'] for r in results]
        texts_str = " ".join(texts)

        # ホーム画面チェック
        home_kw = ["クエスト", "ショップ", "ガチャ", "ミッション", "メニュー", "ホーム", "お知らせ", "編成"]
        home_count = sum(1 for kw in home_kw if any(kw in t for t in texts))
        if home_count >= 3:
            logger.info(">>> ホーム画面到達！ (%d indicators)", home_count)
            return

        # ダウンロード/ロード中
        if any(kw in texts_str for kw in ["ダウンロード", "Loading", "Now Loading", "ロード中"]):
            logger.info("  >>> ロード中 — 待機")
            time.sleep(8)
            continue

        # Result画面
        if any(kw in texts_str for kw in ["Result", "RESULT", "リザルト"]):
            logger.info("  >>> Result画面 → タップ")
            tap(760, 650, "result")
            time.sleep(3)
            continue

        # バトルUI（AUTO, 通常攻撃, 戦闘スキル等）
        battle_kw = ["AUTO", "通常攻撃", "戦闘スキル", "単体攻撃", "单体攻撃", "BREAK", "Turn"]
        is_battle = any(kw in texts_str for kw in battle_kw)

        if is_battle:
            # バトル中 → 通常攻撃タップ
            logger.info("  >>> バトル中（指差しなし）→ 通常攻撃")
            tap(1330, 650, "normal attack")
            time.sleep(3)
            last_action = "NORMAL_ATTACK"
            continue

        # スキップボタン
        if any(kw in texts_str for kw in ["スキップ", "SKIP", "Skip"]):
            for r in results:
                if any(kw in r['text'] for kw in ["スキップ", "SKIP", "Skip"]):
                    cx, cy = r['center']
                    tap(cx, cy, f"SKIP '{r['text']}'")
                    time.sleep(3)
                    break
            continue

        # 閉じる/OK/次へ
        for kw in ["閉じる", "OK", "次へ", "確認", "完了", "了解", "決定", "受け取る"]:
            match = None
            for r in results:
                if kw in r['text']:
                    match = r
                    break
            if match:
                cx, cy = match['center']
                tap(cx, cy, f"'{match['text']}'")
                time.sleep(2)
                break
        else:
            # 何も検出できない → 画面中央タップ（ストーリー進行/ローディング待ち）
            consecutive_no_change += 1
            if consecutive_no_change > 5:
                # 長時間同じ → 右下閉じるボタン試行
                tap(1480, 690, "close button fallback")
                consecutive_no_change = 0
            else:
                tap(760, 400, "center fallback")
            time.sleep(2)
            continue

        consecutive_no_change = 0
        time.sleep(1)

    logger.info("最大イテレーション到達")

if __name__ == "__main__":
    main()
