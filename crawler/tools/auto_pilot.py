#!/usr/bin/env python3
"""
auto_pilot.py — まどドラ自律操縦スクリプト (低燃費版)

1秒 phash ポーリング → 画面変化時のみ OCR → 確実なターゲットのみタップ。
ブラインド中央タップは一切行わない。

使い方:
    cd crawler
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \
    venv/bin/python tools/auto_pilot.py
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

# ─── タイミング ───
MAX_ITERATIONS = 2000      # 安全上限 (1秒サイクルなので長めに)
POLL_INTERVAL = 1.0        # phash ポーリング間隔 (秒)
PHASH_THRESHOLD = 5        # phash 距離 >= 5 → 画面変化あり (ハイライト演出対応)
PHASH_MINOR_UPPER = 5      # 微小変化スキップを無効化 (ハイライト演出を見落とさない)
PHASH_ELEVATED = 15        # WAIT_FOR_CHANGE 後の一時的な閾値引き上げ
BLACKOUT_BRIGHTNESS = 20   # 平均輝度がこれ以下 → 暗転とみなす
STALL_TIMEOUT = 30.0       # 同一画面が続く秒数 → スタック介入
BATTLE_WAIT = 5.0          # バトル AUTO 後の待機 (秒)
DOWNLOAD_WAIT = 10.0       # ダウンロード中の待機 (秒)

# ─── 解析基準解像度 (すべての比率計算のベース) ───
ANALYSIS_W = 1520
ANALYSIS_H = 720

OCR_LANG = "japan"
OCR_MIN_CONF = 0.3


@dataclass
class PilotState:
    """操縦状態"""
    iteration: int = 0
    last_phash: str = ""
    stall_start: float = 0.0       # スタック開始時刻 (time.time())
    stall_corner_tried: bool = False  # スタック時の角タップ試行済みフラグ
    last_action: str = ""
    last_ocr_texts: list = field(default_factory=list)
    battle_wait_count: int = 0
    auto_activated: bool = False
    home_reached: bool = False
    total_taps: int = 0
    total_ocr_calls: int = 0
    total_ocr_skipped: int = 0
    total_minor_skipped: int = 0   # 微小変化スキップ回数
    total_blackout_skipped: int = 0  # 暗転スキップ回数
    screenshots_saved: int = 0
    # 実機解像度 (初回スクリーンショットで確定)
    device_w: int = 0
    device_h: int = 0
    # 動的閾値制御
    current_threshold: int = PHASH_THRESHOLD  # AI 結果に応じて動的に変化
    elevated_until: float = 0.0               # 閾値引き上げ期限 (time.time())


# ─── ADB ユーティリティ ─────────────────────────────
def adb(cmd: str) -> str:
    """adb -s <serial> <cmd> を実行"""
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
    """スクリーンショットを取得。(path, width, height) を返す"""
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
    """
    実機座標でタップ。解析座標 (ANALYSIS_W x ANALYSIS_H) から
    実機座標への自動スケーリングを行う。
    """
    if state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        real_x = int(x * sx)
        real_y = int(y * sy)
    else:
        real_x, real_y = x, y
    time.sleep(0.5)
    adb(f"shell input tap {real_x} {real_y}")
    state.total_taps += 1
    if (real_x, real_y) != (x, y):
        logger.info("  TAP (%d,%d) → device (%d,%d) %s", x, y, real_x, real_y, desc)
    else:
        logger.info("  TAP (%d,%d) %s", x, y, desc)


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


def is_dark_screen(img_path: Path) -> bool:
    """画面が暗転（平均輝度 <= BLACKOUT_BRIGHTNESS）かどうかを判定"""
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            gray = img.convert("L")
            import numpy as np
            mean_brightness = np.mean(np.array(gray))
            return mean_brightness <= BLACKOUT_BRIGHTNESS
    except Exception:
        return False


def prepare_analysis_image(img_path: Path, actual_w: int, actual_h: int) -> Path:
    """
    解析用画像を準備する。ポートレートならランドスケープに回転し、
    ANALYSIS_W x ANALYSIS_H にリサイズする。
    元画像と同じならそのまま返す。
    """
    from PIL import Image
    needs_transform = False

    # ポートレート検出 (w < h だがランドスケープが期待される場合)
    if actual_w < actual_h:
        needs_transform = True

    # 解像度が基準と異なる場合
    if (actual_w, actual_h) != (ANALYSIS_W, ANALYSIS_H) and \
       (actual_h, actual_w) != (ANALYSIS_W, ANALYSIS_H):
        needs_transform = True

    if not needs_transform:
        return img_path

    analysis_path = Path("/tmp/lc_autopilot_analysis.png")
    img = Image.open(img_path)

    # ポートレート → ランドスケープに回転
    if img.width < img.height:
        img = img.rotate(90, expand=True)
        logger.debug("Portrait → Landscape rotation applied")

    # リサイズ
    if img.size != (ANALYSIS_W, ANALYSIS_H):
        img = img.resize((ANALYSIS_W, ANALYSIS_H), Image.LANCZOS)
        logger.debug("Resized to %dx%d for analysis", ANALYSIS_W, ANALYSIS_H)

    img.save(analysis_path)
    return analysis_path


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
def detect_and_act(ocr: list, state: PilotState) -> tuple[str, float]:
    """
    OCR結果を分析し、確実なターゲットが見つかった場合のみタップする。
    座標はすべて解析基準 (ANALYSIS_W x ANALYSIS_H) で計算し、
    tap_device() が実機座標へスケーリングする。

    Returns:
        (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    W, H = ANALYSIS_W, ANALYSIS_H

    # ─── 【最優先】ハイライト・チュートリアル指示 ───
    # 「ここをタップ」「タップしてください」などの指示テキストを最優先で検出
    tutorial_keywords = ["ここをタップ", "タップしてください", "タップして下さい",
                         "タップして", "指差し", "ハイライト"]
    for kw in tutorial_keywords:
        match = has_text(ocr, kw, min_conf=0.3)
        if match:
            cx, cy = match["center"]
            logger.info(">>> 【最優先】ハイライト指示 '%s' (%d,%d)", kw, cx, cy)
            tap_device(cx, cy, state, f"HIGHLIGHT '{kw}'")
            return "HIGHLIGHT_TAP", 2.0

    # ─── ホーム画面検出 ───
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成"]
    home_count = sum(1 for h in home_indicators if any(h in t for t in texts))
    if home_count >= 3:
        logger.info(">>> ホーム画面検出! (%d個のインジケータ一致)", home_count)
        state.home_reached = True
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 ───
    dl = has_any(ocr, ["ダウンロード", "追加データ", "Loading", "ロード中",
                       "通信中", "Now Loading"])
    if dl:
        logger.info(">>> ダウンロード/ロード中: '%s' — 待機", dl["text"])
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
        logger.info(">>> クエストマップ — 「%s」をタップ (%d,%d)", sentu_btn["text"], cx, cy)
        tap_device(cx, cy, state, f"QUEST_START {sentu_btn['text']}")
        state.battle_wait_count = 0
        return "QUEST_START", 5.0
    elif stage_num and not sentu_btn:
        fx, fy = int(W * 0.74), int(H * 0.91)
        logger.info(">>> クエストマップ(ボタン未検出) — 固定座標 (%d,%d)", fx, fy)
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
            logger.info(">>> バフチュートリアル — CRTカード (%d,%d)", bx, by)
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
        attack_target_tut = has_any(ocr, ["攻撃対象を変更", "対象を変更"])
        if attack_target_tut:
            ex, ey = int(W * 0.651), int(H * 0.361)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 2.0

        # バトルチュートリアル: 一般ポップアップ (説明テキスト)
        tutorial_popup = has_any(ocr, ["タイムライン", "表示されている", "行動してい",
                                        "ここをタップ", "タップしてください",
                                        "ことができます", "することができ",
                                        "スキルを使用", "スキルを選択", "カードを選択",
                                        "ましょう", "みましょう", "てみよう",
                                        "一番上に", "順番に行動"])
        if tutorial_popup:
            # ポップアップ内テキストの座標をタップ (中央ではなくテキスト自体)
            cx, cy = tutorial_popup["center"]
            logger.info(">>> バトルチュートリアルポップアップ — テキスト座標タップ (%d,%d)", cx, cy)
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
                logger.info(">>> バトル停滞 — スキルタップ試行 (%d,%d)", sx, sy)
                tap_device(sx, sy, state, "STALL_SKILL")
                time.sleep(0.8)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 2.0
            elif stall_phase == 4:
                hx, hy = int(W * 0.862), int(H * 0.778)
                logger.info(">>> バトル停滞 — 必殺技タップ試行 (%d,%d)", hx, hy)
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
        logger.info(">>> バトル結果: '%s' — 座標タップ (%d,%d)", result_match["text"], cx, cy)
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

    # ─── 確認ダイアログ (OK/はい/次へ 等) ───
    confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                   "受け取る", "受取", "了解", "わかった",
                                   "進む", "START", "開始", "タップ",
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
        # 送りアイコン (▼) または最下部のテキストをタップ
        target = lower_texts[-1]  # 最下部のテキスト
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り — '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 2.0

    # ─── チュートリアル指示テキスト ───
    tutorial_match = has_any(ocr, ["チュートリアル", "Tutorial", "ここをタップ",
                                    "タップしてください", "タップして"])
    if tutorial_match:
        cx, cy = tutorial_match["center"]
        logger.info(">>> チュートリアル '%s' (%d,%d)", tutorial_match["text"], cx, cy)
        tap_device(cx, cy, state, f"TUTORIAL '{tutorial_match['text']}'")
        return "TUTORIAL", 3.0

    # ─── ログインボーナス等 ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        cx, cy = bonus_match["center"]
        logger.info(">>> ポップアップ '%s' (%d,%d)", bonus_match["text"], cx, cy)
        tap_device(cx, cy, state, "POPUP_TAP")
        return "POPUP_TAP", 2.0

    # ─── フォールバック: タップ禁止 ───
    # 確実なターゲットが見つからない → タップせず待機。
    # スタック検知はメインループ側の phash + 30秒タイマーが処理する。
    logger.info(">>> 不明な画面 — Waiting for changes... (タップなし)")
    return "WAIT_FOR_CHANGE", 0


# ─── メインループ ─────────────────────────────────
def main():
    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (低燃費版)")
    logger.info("  デバイス: %s", DEVICE_SERIAL)
    logger.info("  ポーリング: %.1fs  スタックタイムアウト: %.0fs",
                POLL_INTERVAL, STALL_TIMEOUT)
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

        # 初回: 実機解像度を記録
        if not state.device_w:
            state.device_w = actual_w
            state.device_h = actual_h
            logger.info("実機解像度: %dx%d (解析基準: %dx%d)",
                        actual_w, actual_h, ANALYSIS_W, ANALYSIS_H)

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            state.total_blackout_skipped += 1
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転検出 (brightness<=%.0f) — OCR スキップ, 3s 待機",
                            i, BLACKOUT_BRIGHTNESS)
            state.last_phash = ""  # 暗転中は phash リセット (復帰後に必ず変化検出)
            time.sleep(3.0)
            continue

        # ── 3) phash 粗解析 (1秒ポーリング) ──
        try:
            cur_phash = compute_phash(img_path)
        except Exception:
            cur_phash = ""

        if state.last_phash and cur_phash:
            dist = phash_distance(state.last_phash, cur_phash)
        else:
            dist = 999  # 初回 → 変化あり扱い

        # 動的閾値: WAIT_FOR_CHANGE 後は一時的に引き上げ
        active_threshold = state.current_threshold
        if state.elevated_until and time.time() < state.elevated_until:
            active_threshold = PHASH_ELEVATED
        elif state.elevated_until and time.time() >= state.elevated_until:
            # 引き上げ期限切れ → 通常閾値に復帰
            state.current_threshold = PHASH_THRESHOLD
            state.elevated_until = 0.0

        # 微小変化スキップを無効化（ハイライト演出を見落とさない）
        # ≒ PHASH_THRESHOLD = PHASH_MINOR_UPPER に設定済みなので、この判定は常に False
        if False:  # 無効化済み
            state.total_minor_skipped += 1
            state.last_phash = cur_phash
            if state.total_minor_skipped % 5 == 1:
                logger.info("[iter %d] 微小変化スキップ (無効化済み)")
            time.sleep(2.0)
            continue

        screen_changed = dist >= active_threshold

        if not screen_changed:
            # ── 画面変化なし: OCR スキップ、タップ禁止 ──
            state.total_ocr_skipped += 1
            if state.stall_start == 0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            # 30秒スタック → 右上×ボタンをタップ試行 (1回だけ)
            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                logger.warning(
                    ">>> %.0f秒スタック — 右上×ボタン試行 (%d,%d)",
                    stall_elapsed, actual_w - 40, 40,
                )
                save_evidence(img_path, [], "STALL_CORNER", state)
                time.sleep(0.5)
                adb(f"shell input tap {actual_w - 40} 40")
                state.total_taps += 1
                state.stall_corner_tried = True
                state.last_phash = ""  # 次ループで強制的に変化検出
                time.sleep(2)
                continue

            # 角タップ後もさらに30秒スタック → 停止して報告
            if stall_elapsed >= STALL_TIMEOUT * 2 and state.stall_corner_tried:
                logger.error(
                    ">>> %.0f秒スタック解消不能 — 停止して手動確認を要求",
                    stall_elapsed,
                )
                save_evidence(img_path, [], "STALL_FATAL", state)
                logger.info("  総タップ数: %d  OCR実行: %d  スキップ: %d  微小: %d  暗転: %d",
                            state.total_taps, state.total_ocr_calls,
                            state.total_ocr_skipped, state.total_minor_skipped,
                            state.total_blackout_skipped)
                return

            if i % 10 == 0:
                logger.info("[iter %d] No change (phash=%d, stall=%.0fs) — polling...",
                            i, dist, stall_elapsed)
            state.last_phash = cur_phash
            time.sleep(POLL_INTERVAL)
            continue

        # ── 画面が変化した! → スタックタイマーリセット ──
        state.stall_start = 0.0
        state.stall_corner_tried = False
        state.last_phash = cur_phash

        # ── 4) 解析用画像の準備 (リサイズ/回転) ──
        analysis_path = prepare_analysis_image(img_path, actual_w, actual_h)

        # ── 5) OCR 精査 (画面変化時のみ) ──
        state.total_ocr_calls += 1
        try:
            ocr_results = run_ocr(str(analysis_path), lang=OCR_LANG,
                                  min_confidence=OCR_MIN_CONF)
        except Exception as e:
            logger.error("OCR failed: %s", e)
            time.sleep(3)
            continue

        texts = all_texts(ocr_results)
        logger.info("[iter %d] phash_dist=%d OCR(%d): %s",
                    i, dist, len(ocr_results), texts[:10])
        state.last_ocr_texts = texts

        # ── 6) 判定 & アクション ──
        action, wait_sec = detect_and_act(ocr_results, state)
        state.last_action = action

        # 動的閾値制御: WAIT_FOR_CHANGE → 閾値を一時的に 15 に引き上げ
        if action == "WAIT_FOR_CHANGE":
            state.current_threshold = PHASH_ELEVATED
            state.elevated_until = time.time() + 15.0  # 15秒間は閾値引き上げ
            logger.info("  閾値引き上げ: %d → %d (15秒間)",
                        PHASH_THRESHOLD, PHASH_ELEVATED)
        elif action not in ("BATTLE_WAIT", "DOWNLOAD_WAIT"):
            # 確認済みアクション実行 → 閾値を通常に復帰
            if state.current_threshold != PHASH_THRESHOLD:
                logger.info("  閾値復帰: %d → %d", state.current_threshold, PHASH_THRESHOLD)
            state.current_threshold = PHASH_THRESHOLD
            state.elevated_until = 0.0

        # エビデンス保存
        if i % 20 == 0 or action in ("HOME_REACHED", "SKIP", "AGREE",
                                       "RESULT_TAP", "STALL_CORNER"):
            save_evidence(img_path, ocr_results, action, state)

        # ── 7) ホーム画面到達チェック ──
        if state.home_reached:
            logger.info("=" * 62)
            logger.info("  ホーム画面に到達しました!")
            logger.info("  総タップ数: %d", state.total_taps)
            logger.info("  総イテレーション: %d", i + 1)
            logger.info("  OCR 実行: %d  スキップ: %d  微小変化: %d  暗転: %d",
                        state.total_ocr_calls, state.total_ocr_skipped,
                        state.total_minor_skipped, state.total_blackout_skipped)
            logger.info("  スクリーンショット保存: %d", state.screenshots_saved)
            logger.info("=" * 62)
            save_evidence(img_path, ocr_results, "FINAL_HOME", state)
            return

        # ── 8) 待機 ──
        if wait_sec > 0:
            logger.info("  [%s] wait %.1fs", action, wait_sec)
            time.sleep(wait_sec)
        # wait_sec == 0 → 即座に次のポーリングへ (WAIT_FOR_CHANGE)

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)


if __name__ == "__main__":
    main()
