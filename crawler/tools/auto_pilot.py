#!/usr/bin/env python3
"""
auto_pilot.py — まどドラ自律操縦スクリプト

チュートリアルからホーム画面到達まで、画面のOCR結果に基づいて
自動的にタップ/待機を繰り返す。

使い方:
    cd crawler
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    venv/bin/python tools/auto_pilot.py

ルール:
    - バトル中: AUTOボタンを確認し、勝利まで待機
    - ストーリー: Skip/画面タップで飛ばす
    - ダウンロード: 完了まで待機
    - チュートリアル: 案内に従ってタップ
    - ホーム画面到達で終了
"""
from __future__ import annotations

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
MAX_ITERATIONS = 500       # 安全上限
LOOP_INTERVAL = 5.0        # 各ループの基本間隔 (秒)
BATTLE_WAIT = 5.0          # バトル中の待機間隔 (秒)
DOWNLOAD_WAIT = 10.0       # ダウンロード中の待機間隔 (秒)
LONG_WAIT = 10.0           # ロード画面の待機 (秒)
PHASH_SAME_THRESHOLD = 8   # phash 距離 < 8 → 同一画面とみなす
STALL_LIMIT = 3            # 同一画面が続いたら停止して待機する回数
OCR_LANG = "japan"
OCR_MIN_CONF = 0.3
SCREEN_W = 1520            # Android landscape
SCREEN_H = 720


@dataclass
class PilotState:
    """操縦状態"""
    iteration: int = 0
    consecutive_same: int = 0      # 同一画面検出回数 (phash ベース)
    last_action: str = ""
    last_ocr_texts: list = field(default_factory=list)
    last_phash: str = ""           # 前回のスクリーンショット phash
    battle_wait_count: int = 0
    auto_activated: bool = False    # 今のバトルでAUTOをオンにしたか
    home_reached: bool = False
    total_taps: int = 0
    total_ocr_skipped: int = 0     # phash 一致で OCR をスキップした回数
    screenshots_saved: int = 0


# ─── ADB ユーティリティ ─────────────────────────────
def adb(cmd: str) -> str:
    """adb -s <serial> shell <cmd> を実行"""
    full = f"adb -s {DEVICE_SERIAL} {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("adb command timed out: %s", cmd)
        return ""


def take_screenshot() -> tuple[Path, int, int]:
    """スクリーンショットを取得してローカルに保存。(path, width, height) を返す"""
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
        w, h = SCREEN_W, SCREEN_H
    return path, w, h


def tap(x: int, y: int, desc: str = "") -> None:
    """指定座標をタップ (UI安定待機0.5秒後)"""
    time.sleep(0.5)
    adb(f"shell input tap {x} {y}")
    logger.info("  TAP (%d, %d) %s", x, y, desc)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
    """スワイプ"""
    adb(f"shell input swipe {x1} {y1} {x2} {y2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", x1, y1, x2, y2, duration_ms)


def save_evidence(img_path: Path, ocr_results: list, action: str, state: PilotState) -> None:
    """エビデンスを保存"""
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    dest = EVIDENCE_DIR / f"{ts}_iter{state.iteration:03d}_{action}.png"
    try:
        import shutil
        shutil.copy2(str(img_path), str(dest))
        state.screenshots_saved += 1
    except Exception as e:
        logger.warning("Evidence save failed: %s", e)


# ─── OCR テキスト検索ヘルパー ──────────────────────
def has_any(ocr: list, keywords: list[str], min_conf: float = 0.3) -> Optional[dict]:
    """OCR結果にいずれかのキーワードが含まれるか"""
    for kw in keywords:
        match = find_best(ocr, kw, min_confidence=min_conf)
        if match:
            return match
    return None


def has_text(ocr: list, keyword: str, min_conf: float = 0.3) -> Optional[dict]:
    """OCR結果に特定キーワードが含まれるか"""
    return find_best(ocr, keyword, min_confidence=min_conf)


def all_texts(ocr: list) -> list[str]:
    """OCR結果からテキスト一覧を取得"""
    return [r["text"] for r in ocr]


# ─── 画面判定・アクション ──────────────────────────
def detect_and_act(ocr: list, state: PilotState,
                   screen_w: int = SCREEN_W, screen_h: int = SCREEN_H) -> tuple[str, float]:
    """
    OCR結果を分析し、適切なアクションを実行する。
    screen_w/screen_h は今回のスクリーンショット実測値を使用する。

    Returns:
        (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    texts_joined = " ".join(texts)

    # ─── ホーム画面検出 ───
    # まどドラのホーム画面要素: フッターに光の間、ショップ、ガシャ、パーティ等
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成"]
    home_count = sum(1 for h in home_indicators if any(h in t for t in texts))
    if home_count >= 3:
        logger.info(">>> ホーム画面検出! (%d個のインジケータ一致)", home_count)
        state.home_reached = True
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 ───
    dl = has_any(ocr, ["ダウンロード", "追加データ", "Loading", "ロード中", "通信中", "Now Loading"])
    if dl:
        logger.info(">>> ダウンロード/ロード中: '%s' — 待機", dl["text"])
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ─── クエストマップ/ステージ選択画面 ───
    # ステージ番号 (1-1, 1-2 等) + Main が同時に見えたらクエストマップ
    stage_num = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                               "3-1", "3-2", "4-1", "4-2", "Main"])
    sentu_btn = has_text(ocr, "戦闘") or has_text(ocr, "出撃")
    # 「探索」ボタン=戦闘ボタン付近の別検出 / 右下エリア(y>500)のみ対象
    if not sentu_btn:
        expl = has_text(ocr, "探索")
        if expl and expl["center"][1] > 500:
            sentu_btn = expl
    if stage_num and sentu_btn:
        cx, cy = sentu_btn["center"]
        logger.info(">>> クエストマップ — 「%s」ボタンをタップ (%d,%d)",
                    sentu_btn["text"], cx, cy)
        tap(cx, cy, f"QUEST_START {sentu_btn['text']}")
        state.total_taps += 1
        state.battle_wait_count = 0
        return "QUEST_START", 5.0
    elif stage_num and not sentu_btn:
        # 戦闘ボタンがOCRで見つからない場合は右下固定座標をタップ
        fx = int(screen_w * 0.74)  # 1520 * 0.74 ≈ 1125
        fy = int(screen_h * 0.91)  # 720 * 0.91 ≈ 655
        logger.info(">>> クエストマップ(戦闘ボタン未検出) — 固定座標 (%d,%d) タップ", fx, fy)
        tap(fx, fy, "QUEST_START_FIXED")
        state.total_taps += 1
        state.battle_wait_count = 0
        return "QUEST_START", 5.0

    # ─── バトル画面 ───
    battle_keywords = ["AUTO", "通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "BREAK", "HP", "Turn", "WAVE", "戦闘"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1
        # チュートリアルポップアップ(行動説明テキスト)がある場合はタップして閉じる
        # 「攻撃対象を変更」= 敵をタップする操作チュートリアル → 指差し先の敵をタップ
        # 「スキルを使ってみましょう」= 戦闘スキルカードをタップするチュートリアル
        # 「バフ効果」説明 → ハイライトされたスキルカード（右寄り）をタップ
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx = int(screen_w * 0.888)  # ≈ 1350
            by = int(screen_h * 0.667)  # ≈ 480
            logger.info(">>> バフ効果チュートリアル — CRTカードをタップ (%d,%d)", bx, by)
            tap(bx, by, "BUFF_TUTORIAL_TAP")
            state.total_taps += 1
            return "BATTLE_TUTORIAL", 2.0
        skill_tut = has_any(ocr, ["スキルを使ってみましょう", "スキを使ってみ",
                                   "戦闘スキルを使", "戦闘スキを使",
                                   "スキルを使用してみ", "使ってみましょう"])
        if skill_tut:
            # 戦闘スキルカードは右端ボタン: 約 (1440, 520) / 1520x720
            # ※ カードは1回目タップで拡大表示 → 2回目で実行
            sx = int(screen_w * 0.947)  # ≈ 1440
            sy = int(screen_h * 0.722)  # ≈ 520
            logger.info(">>> スキルチュートリアル — 戦闘スキルカードをタップ (%d,%d)", sx, sy)
            tap(sx, sy, "SKILL_CARD_TUTORIAL")
            time.sleep(0.8)
            tap(sx, sy, "SKILL_CARD_TUTORIAL confirm")
            state.total_taps += 2
            return "BATTLE_TUTORIAL", 3.0
        # 必殺技チュートリアル: CTDアップバフカード + 必殺技 が表示される
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            # 必殺技カードの固定座標: 約 (1310, 560) / 1520x720
            hx = int(screen_w * 0.862)  # ≈ 1310
            hy = int(screen_h * 0.778)  # ≈ 560
            logger.info(">>> 必殺技チュートリアル — 必殺技カードをタップ (%d,%d)", hx, hy)
            tap(hx, hy, "HISSATSU_TUTORIAL")
            time.sleep(0.8)
            tap(hx, hy, "HISSATSU_TUTORIAL confirm")
            state.total_taps += 2
            return "BATTLE_TUTORIAL", 3.0
        attack_target_tut = has_any(ocr, ["攻撃対象を変更", "対象を変更"])
        if attack_target_tut:
            # 指差しアイコンがある敵の位置 (ゴールドハイライトボックス内の敵)
            # スクリーンショット実測値: 敵 ≈ (990, 260), 指差し ≈ (1000, 410) / 1520x720
            ex = int(screen_w * 0.651)  # ≈ 990
            ey = int(screen_h * 0.361)  # ≈ 260
            logger.info(">>> 攻撃対象チュートリアル — 指差し先の敵をタップ (%d,%d)", ex, ey)
            tap(ex, ey, "ATTACK_TARGET_TUTORIAL")
            state.total_taps += 1
            return "BATTLE_TUTORIAL", 2.0
        tutorial_popup = has_any(ocr, ["タイムライン", "表示されている", "行動してい",
                                        "ここをタップ", "タップしてください",
                                        "ことができます", "することができ",
                                        "スキルを使用", "スキルを選択", "カードを選択",
                                        "ましょう", "みましょう", "てみよう",
                                        "一番上に", "順番に行動"])
        if tutorial_popup:
            logger.info(">>> バトルチュートリアルポップアップ — 中央タップ")
            tap(screen_w // 2, screen_h // 2, "battle tutorial dismiss")
            state.total_taps += 1
            return "BATTLE_TUTORIAL", 2.0
        # AUTOボタンをタップ（まだオンにしていない場合のみ）
        # 座標はスクリーンショット実測値: 上部バー右側 (1285, 65) / 1520x720
        AUTO_BTN_X = int(screen_w * 0.845)  # ≈ 1285
        AUTO_BTN_Y = int(screen_h * 0.090)  # ≈ 65
        if not state.auto_activated:
            tap(AUTO_BTN_X, AUTO_BTN_Y, "AUTO ON (fixed coord)")
            state.total_taps += 1
            state.auto_activated = True
            logger.info(">>> バトル中 — AUTO タップ (固定座標 %d,%d)", AUTO_BTN_X, AUTO_BTN_Y)
            return "BATTLE_AUTO", BATTLE_WAIT

        # 長時間待機でバトルが進まない → ハイライト候補を順番にタップ試行
        # フェーズ0: 戦闘スキル, フェーズ4: 必殺技, フェーズ8: 中央タップ (チュートリアルテキスト解除)
        if state.battle_wait_count > 8:
            stall_phase = (state.battle_wait_count - 8) % 12
            if stall_phase == 0:
                sx = int(screen_w * 0.947)  # ≈ 1440
                sy = int(screen_h * 0.722)  # ≈ 520
                logger.info(">>> バトル停滞[phase0] — 戦闘スキルタップ (%d,%d)", sx, sy)
                tap(sx, sy, "STALL_SKILL")
                time.sleep(0.8)
                tap(sx, sy, "STALL_SKILL confirm")
                state.total_taps += 2
                return "BATTLE_STALL", 2.0
            elif stall_phase == 4:
                hx = int(screen_w * 0.862)  # ≈ 1310
                hy = int(screen_h * 0.778)  # ≈ 560
                logger.info(">>> バトル停滞[phase4] — 必殺技タップ (%d,%d)", hx, hy)
                tap(hx, hy, "STALL_HISSATSU")
                time.sleep(0.8)
                tap(hx, hy, "STALL_HISSATSU confirm")
                state.total_taps += 2
                return "BATTLE_STALL", 2.0
            elif stall_phase == 8:
                logger.info(">>> バトル停滞[phase8] — 中央タップ (チュートリアルテキスト解除)")
                tap(screen_w // 2, screen_h // 2, "STALL_CENTER")
                state.total_taps += 1
                return "BATTLE_STALL", 2.0
        logger.info(">>> バトル中 — 待機 (count=%d, auto=%s)", state.battle_wait_count, state.auto_activated)
        return "BATTLE_WAIT", BATTLE_WAIT

    # バトル待機カウントをリセット (バトル画面でなくなった)
    if state.battle_wait_count > 0:
        logger.info(">>> バトル終了検出 (wait_count was %d)", state.battle_wait_count)
        state.battle_wait_count = 0
        state.auto_activated = False  # 次のバトル用にリセット

    # ─── バトル結果/リザルト ─── (「初回報酬」=クエストマップ表示なので除外)
    result_match = has_any(ocr, ["リザルト", "Result", "RESULT", "勝利", "Victory",
                                  "クリア", "CLEAR", "EXP", "経験値", "ランクアップ"])
    if result_match:
        logger.info(">>> バトル結果: '%s' — タップして進む", result_match["text"])
        tap(screen_w // 2, screen_h // 2, "result screen tap")
        state.total_taps += 1
        return "RESULT_TAP", 3.0

    # ─── スキップ可能なシーン ───
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if skip_match:
        cx, cy = skip_match["center"]
        tap(cx, cy, f"SKIP '{skip_match['text']}'")
        state.total_taps += 1
        logger.info(">>> スキップボタンをタップ")
        return "SKIP", 3.0

    # ─── 「閉じる」ボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        tap(cx, cy, f"CLOSE '{close_match['text']}'")
        state.total_taps += 1
        return "CLOSE", 2.0

    # ─── 規約同意画面 ───
    agree_match = has_any(ocr, ["同意", "規約", "利用規約"])
    if agree_match:
        # 規約は3回スワイプしてから同意タップ
        logger.info(">>> 規約画面 — スクロールしてから同意")
        for _ in range(3):
            swipe(700, 500, 700, 200, 500)
            time.sleep(0.8)
        agree_btn = has_any(ocr, ["同意"])
        if agree_btn:
            cx, cy = agree_btn["center"]
            tap(cx, cy, "AGREE")
            state.total_taps += 1
        return "AGREE", 3.0

    # ─── 確認ダイアログ (OK/はい/次へ/確認/完了/決定) ───
    confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                   "受け取る", "受取", "了解", "わかった",
                                   "進む", "START", "開始", "タップ",
                                   "TAP TO START", "TOUCH", "始める",
                                   "戦闘", "出撃", "クエスト開始", "バトル開始"])
    if confirm_match:
        cx, cy = confirm_match["center"]
        tap(cx, cy, f"CONFIRM '{confirm_match['text']}'")
        state.total_taps += 1
        return "CONFIRM", 3.0

    # ─── ストーリー/会話画面（テキストボックスが下部にある） ───
    # 画面下半分にテキストがある場合はストーリーと判断してタップ
    lower_texts = [r for r in ocr if r["center"][1] > screen_h * 0.6]
    if lower_texts and len(ocr) <= 15:
        # 少量のテキストが下部にある = 会話画面の可能性
        logger.info(">>> ストーリー/会話画面の可能性 — 画面タップ")
        tap(screen_w // 2, screen_h // 2, "story tap")
        state.total_taps += 1
        return "STORY_TAP", 2.0

    # ─── チュートリアル矢印/ハイライト ───
    tutorial_match = has_any(ocr, ["チュートリアル", "Tutorial", "ここをタップ",
                                    "タップしてください", "タップして"])
    if tutorial_match:
        cx, cy = tutorial_match["center"]
        tap(cx, cy, f"TUTORIAL '{tutorial_match['text']}'")
        state.total_taps += 1
        return "TUTORIAL", 3.0

    # ─── ログインボーナス等のポップアップ ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        # 画面中央タップで閉じる
        tap(screen_w // 2, screen_h // 2, "popup dismiss")
        state.total_taps += 1
        return "POPUP_DISMISS", 2.0

    # ─── フォールバック: タップ禁止・待機のみ ───
    # 何も確実なターゲットが見つからない場合はタップせず待機する。
    # 画面変化は phash 比較でメインループ側が検知する。
    logger.info(">>> 不明な画面 — タップせず待機 (consecutive_same=%d)",
                state.consecutive_same)
    return "WAIT_FOR_CHANGE", LONG_WAIT


# ─── メインループ ─────────────────────────────────
def verify_scale() -> None:
    """スクリーンショット解像度とデバイス解像度を照合してスケールを確認"""
    try:
        from PIL import Image
        tmp = Path("/tmp/lc_scale_check.png")
        adb(f"shell screencap -p /sdcard/lc_scale_check.png")
        subprocess.run(f"adb -s {DEVICE_SERIAL} pull /sdcard/lc_scale_check.png {tmp}",
                       shell=True, capture_output=True, timeout=10)
        img = Image.open(tmp)
        w, h = img.size
        logger.info("解像度照合: screenshot=%dx%d, SCREEN_W=%d, SCREEN_H=%d",
                    w, h, SCREEN_W, SCREEN_H)
        if (w, h) != (SCREEN_W, SCREEN_H):
            logger.warning("⚠ 解像度ミスマッチ! OCR座標がズレる可能性あり")
        else:
            logger.info("✓ 解像度一致 — スケール係数 1.0 (補正不要)")
    except Exception as e:
        logger.warning("スケール検証スキップ: %s", e)


def main():
    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot 開始")
    logger.info("  デバイス: %s", DEVICE_SERIAL)
    logger.info("=" * 62)

    verify_scale()
    state = PilotState()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    for i in range(MAX_ITERATIONS):
        state.iteration = i

        # 1) スクリーンショット取得 (実測解像度も取得)
        img_path, cur_w, cur_h = take_screenshot()
        if not img_path.exists():
            logger.warning("Screenshot failed, retrying...")
            time.sleep(2)
            continue
        if (cur_w, cur_h) != (SCREEN_W, SCREEN_H):
            logger.warning("⚠ 解像度変化: %dx%d → 座標系を自動調整", cur_w, cur_h)

        # 2) phash 粗解析 — 画面変化の有無を判定
        try:
            cur_phash = compute_phash(img_path)
        except Exception:
            cur_phash = ""

        if state.last_phash and cur_phash:
            dist = phash_distance(state.last_phash, cur_phash)
        else:
            dist = 999  # 初回は「変化あり」扱い

        screen_changed = dist >= PHASH_SAME_THRESHOLD

        if not screen_changed:
            # 画面変化なし → OCR スキップ、タップ禁止、5 秒待機
            state.consecutive_same += 1
            state.total_ocr_skipped += 1

            if state.consecutive_same >= STALL_LIMIT:
                logger.info(
                    "[iter %d] Waiting for visual change... "
                    "(phash_dist=%d, stalled=%d, ocr_skipped=%d)",
                    i, dist, state.consecutive_same, state.total_ocr_skipped,
                )
                # スタック状態: エビデンスを残して待機 (タップしない)
                if state.consecutive_same == STALL_LIMIT:
                    save_evidence(img_path, [], "STALLED", state)
            else:
                logger.info(
                    "[iter %d] No change (phash_dist=%d) — skip OCR, wait %.1fs",
                    i, dist, LOOP_INTERVAL,
                )
            state.last_phash = cur_phash
            time.sleep(LOOP_INTERVAL)
            continue

        # 画面が変化した → phash 連続カウントをリセット
        state.consecutive_same = 0
        state.last_phash = cur_phash

        # 3) OCR 精査 (画面変化があった時のみ実行)
        try:
            ocr_results = run_ocr(str(img_path), lang=OCR_LANG, min_confidence=OCR_MIN_CONF)
        except Exception as e:
            logger.error("OCR failed: %s", e)
            time.sleep(3)
            continue

        texts = all_texts(ocr_results)
        logger.info("[iter %d] %dx%d phash_dist=%d OCR: %s",
                    i, cur_w, cur_h, dist, texts[:10])
        state.last_ocr_texts = texts

        # 4) 判定 & アクション実行 (実測解像度を渡す)
        action, wait_sec = detect_and_act(ocr_results, state, cur_w, cur_h)
        state.last_action = action

        # エビデンス保存 (10回に1回 + 重要アクション)
        if i % 10 == 0 or action in ("HOME_REACHED", "SKIP", "AGREE", "RESULT_TAP"):
            save_evidence(img_path, ocr_results, action, state)

        # 5) ホーム画面到達チェック
        if state.home_reached:
            logger.info("=" * 62)
            logger.info("  ホーム画面に到達しました!")
            logger.info("  総タップ数: %d", state.total_taps)
            logger.info("  総イテレーション: %d", i + 1)
            logger.info("  OCR スキップ数: %d", state.total_ocr_skipped)
            logger.info("  スクリーンショット保存: %d", state.screenshots_saved)
            logger.info("=" * 62)
            save_evidence(img_path, ocr_results, "FINAL_HOME", state)
            return

        # 6) 待機
        logger.info("  [%s] wait %.1fs", action, wait_sec)
        time.sleep(wait_sec)

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)


if __name__ == "__main__":
    main()
