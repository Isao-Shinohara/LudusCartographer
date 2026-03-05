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
POLL_INTERVAL = 0.3         # phash ポーリング間隔 (秒) — 高速化
PHASH_THRESHOLD = 5         # phash 距離 >= 5 → 画面変化あり
FORCE_ANALYZE_AFTER = 3     # phash 変化なし連続 N 回 → 強制 OCR — 高速化
STALL_TIMEOUT = 20.0        # 強制OCRでもタップできず続く秒数 → スタック介入
BATTLE_WAIT = 1.5           # バトル待機 — 高速化
DOWNLOAD_WAIT = 10.0
ADV_RAPID_PHASH_MAX = 25    # ADV高速モード: phash がこれ以下なら OCR スキップ連打
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
    # チュートリアルポップアップ連続タップ回数 (高くなると異なる座標を試す)
    pre_popup_tap_count: int = 0
    # 現在のシーン分類 (BATTLE / ADV / LOADING / MENU / UNKNOWN)
    current_scene: str = "UNKNOWN"


# ─── シーン分類 ──────────────────────────────────────
# シーン別ポーリング間隔 (ユーザー指定)
SCENE_INTERVAL = {
    "BATTLE":  1.0,   # バトル画面: 最速反応
    "ADV":     1.0,   # アドベンチャー/会話: 最速反応
    "STORY":   2.0,   # ストーリー(スキップなし): スキップボタン出現を即検知
    "LOADING": 5.0,   # ロード中: 負荷軽減
    "MENU":    1.0,   # ホーム/メニュー
    "UNKNOWN": 1.0,   # 不明
}

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
    if any(kw in joined for kw in ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                                    "必殺技", "BREAK", "WAVE", "ENEMY TURN", "Turn"]):
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
    time.sleep(0.05)
    adb(f"shell input tap {real_x} {real_y}")
    state.total_taps += 1
    if (real_x, real_y) != (x, y):
        logger.info("  ACTION_TAKEN TAP (%d,%d)→device(%d,%d) %s", x, y, real_x, real_y, desc)
    else:
        logger.info("  ACTION_TAKEN TAP (%d,%d) %s", x, y, desc)


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


# ─── 探索マップ 3D矢印 検出 ──────────────────────────
def find_3d_arrow(img_path: Path) -> Optional[tuple[int, int]]:
    """
    探索マップ上のキャラ頭上に浮かぶ3D矢印（白い曲線矢印）を検出。
    明るい白色コンターが最大のものを矢印とみなす。
    Returns: (cx, cy) or None
    """
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        # キャラ頭上エリア (y=120-280, x=500-1050)
        roi_y1, roi_y2 = 120, 280
        roi_x1, roi_x2 = 500, 1050
        roi = img[roi_y1:roi_y2, roi_x1:roi_x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        _, bright = cv2.threshold(gray, 150, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None
        # サイズフィルタ: 30〜800px² の中からY座標が最も上（小）のものを矢印とみなす
        # (面積最大だとキャラの衣装/武器を誤検出するため)
        candidates = [(cv2.contourArea(c), c) for c in contours
                      if 30 <= cv2.contourArea(c) <= 800]
        if not candidates:
            return None
        # Y座標が最も小さい（画面上部に近い）ものを選択
        def top_y(pair):
            c = pair[1]
            M = cv2.moments(c)
            return (M["m01"] / M["m00"]) if M["m00"] > 0 else 9999
        area, best = min(candidates, key=top_y)
        if area < 30:
            return None
        M = cv2.moments(best)
        if M["m00"] == 0:
            return None
        cx = int(M["m10"] / M["m00"]) + roi_x1
        cy = int(M["m01"] / M["m00"]) + roi_y1
        logger.debug("[3D_ARROW] area=%.0f center=(%d,%d)", area, cx, cy)
        return (cx, cy)
    except Exception as e:
        logger.debug("find_3d_arrow error: %s", e)
        return None


# ─── UI資産ライブラリ (AssetManager) ──────────────
class AssetManager:
    """
    assets/templates/ 内のテンプレート画像を使った高速 UI マッチング。

    ファイル構成:
      assets/templates/{name}.png   — グレースケールテンプレート画像
      assets/templates/{name}.json  — メタデータ (threshold, action, offset)

    処理時間: ~0.1s (OCR比: 20-50倍高速)
    """

    TEMPLATES_DIR = _CRAWLER_ROOT / "assets" / "templates"
    DEFAULT_THRESHOLD = 0.80

    def __init__(self):
        self._templates: dict[str, dict] = {}
        self._load_templates()

    def _load_templates(self) -> None:
        import cv2, json
        count = 0
        for png in sorted(self.TEMPLATES_DIR.glob("*.png")):
            name = png.stem
            img = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            meta: dict = {}
            meta_path = png.with_suffix(".json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass
            self._templates[name] = {
                "img": img,
                "threshold": float(meta.get("threshold", self.DEFAULT_THRESHOLD)),
                "action": meta.get("action", f"ASSET_{name.upper()}"),
                "offset": meta.get("offset", [0, 0]),
            }
            count += 1
        if count:
            logger.info("[AssetManager] %d テンプレート読込: %s",
                        count, list(self._templates.keys()))

    def match(self, screenshot_path: Path) -> Optional[tuple[int, int, str]]:
        """
        スクリーンショットと全テンプレートを比較。
        Returns: (tap_x, tap_y, action_name) or None
        """
        import cv2
        if not self._templates:
            return None
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        best_score = 0.0
        best_result: Optional[tuple[int, int, str]] = None
        for name, data in self._templates.items():
            tmpl = data["img"]
            if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            try:
                res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= data["threshold"] and max_val > best_score:
                    best_score = max_val
                    h, w = tmpl.shape
                    cx = max_loc[0] + w // 2 + int(data["offset"][0])
                    cy = max_loc[1] + h // 2 + int(data["offset"][1])
                    best_result = (cx, cy, data["action"])
                    logger.debug("[Asset] '%s' score=%.3f at (%d,%d)", name, max_val, cx, cy)
            except Exception as e:
                logger.debug("[Asset] match error '%s': %s", name, e)
        if best_result:
            cx, cy, action = best_result
            logger.info("[Asset] HIT: '%s' score=%.3f → (%d,%d)", action, best_score, cx, cy)
        return best_result

    def save_template(self, screenshot_path: Path,
                      x1: int, y1: int, x2: int, y2: int,
                      name: str, action: str,
                      offset: tuple[int, int] = (0, 0),
                      threshold: float = DEFAULT_THRESHOLD) -> bool:
        """
        スクリーンショットの指定領域を切り抜いてテンプレートとして保存。
        次回起動時から [Asset Match] で高速検出可能になる。
        """
        import cv2, json
        img = cv2.imread(str(screenshot_path))
        if img is None:
            return False
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        out_png = self.TEMPLATES_DIR / f"{name}.png"
        meta_path = self.TEMPLATES_DIR / f"{name}.json"
        cv2.imwrite(str(out_png), crop)
        meta = {"action": action, "offset": list(offset), "threshold": threshold}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        # インメモリキャッシュに即時追加
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        self._templates[name] = {
            "img": gray, "threshold": threshold,
            "action": action, "offset": list(offset),
        }
        logger.info("[Asset] テンプレート自動保存: '%s' (%dx%d) action=%s",
                    name, crop.shape[1], crop.shape[0], action)
        return True

    def reload(self) -> None:
        self._templates.clear()
        self._load_templates()


# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


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

    # ─── 【最優先 #0-a】テンプレートマッチング (Asset Match) — 最速 ~0.1s ───
    if analysis_path is not None:
        asset_hit = ASSET_MANAGER.match(analysis_path)
        if asset_hit:
            cx, cy, action = asset_hit
            logger.info(">>> [Asset Match] '%s' → (%d,%d)", action, cx, cy)
            tap_device(cx, cy, state, action)
            return action, 0.5

    # ─── 【最優先 #0】チュートリアルポップアップ (ブロブより優先) ───
    # バトル説明・ロール説明などのポップアップはブロブ検出前に処理
    pre_popup_kws = [
        "ロールについて", "ロールは全部",
        "STEP1", "STEP2", "バトルシステム", "ブレイクし",
        # 全6ロール — 一覧画面・個別詳細ページ両方に対応
        "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "DEFENDER", "HEALER",
        "アタッカー", "ブレイカー", "バッファー", "デバッファー", "ディフェンダー", "ヒーラー",
    ]
    pre_popup = has_any(ocr, pre_popup_kws)
    if pre_popup:
        state.pre_popup_tap_count += 1
        # 同じポップアップが続く場合は異なる座標を試す
        # 通常: 中央、3回目: 右下(閉じるボタン候補)、5回目: 右上
        tap_candidates = [
            (int(W * 0.5), int(H * 0.5)),    # 中央
            (int(W * 0.5), int(H * 0.5)),    # 中央(2回目)
            (int(W * 0.92), int(H * 0.9)),   # 右下
            (int(W * 0.5), int(H * 0.5)),    # 中央(4回目)
            (int(W * 0.92), int(H * 0.1)),   # 右上(×ボタン)
        ]
        idx = min(state.pre_popup_tap_count - 1, len(tap_candidates) - 1)
        cx, cy = tap_candidates[idx]
        logger.info(">>> 【チュートリアルポップアップ】 '%s' → (%d,%d) (試行%d回目)",
                    pre_popup["text"][:10], cx, cy, state.pre_popup_tap_count)
        tap_device(cx, cy, state, "PRE_POPUP_TAP")
        return "TUTORIAL_POPUP", 1.0

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        # 「AUTO」のみはストーリー画面にも表示されるため除外、戦闘固有キーワードで判定
        is_battle_screen = any(kw in " ".join(texts) for kw in
                               ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "必殺技", "BREAK", "WAVE", "Turn"])
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
                logger.info("FINGER_DETECTED (%d,%d) area=%.0f count=%d",
                            fx, fy, fa, state.blob_same_count)
                tap_device(fx, fy, state, f"MOYA_TAP ({fx},{fy})")
                # 左キャラ選択後は char_just_selected フラグをセット
                if fx < 600 and fy > H * 0.76:
                    state.char_just_selected = True
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 1.0

    # ─── 【最優先 #2-a】探索マップ 3D矢印タップ ───
    # 「矢印をタップしてください」が出ている場合、3D空間の矢印を検出してタップ
    arrow_instruction = has_text(ocr, "矢印をタップ", min_conf=0.2)
    if arrow_instruction and analysis_path is not None:
        pos = find_3d_arrow(analysis_path)
        if pos:
            cx, cy = pos
            logger.info(">>> 【3D矢印】 探索マップ矢印 (%d,%d) 検出 → タップ", cx, cy)
            tap_device(cx, cy, state, "MAP_ARROW_TAP")
            # [Auto Save] 初回検出時にテンプレートとして保存
            if "map_arrow" not in ASSET_MANAGER._templates:
                half_w, half_h = 70, 50
                ASSET_MANAGER.save_template(
                    analysis_path,
                    max(0, cx - half_w), max(0, cy - half_h),
                    min(W, cx + half_w), min(H, cy + half_h),
                    name="map_arrow", action="MAP_ARROW_TAP",
                    threshold=0.65,
                )
            return "MAP_ARROW_TAP", 1.0
        else:
            # 自動検出失敗 → キャラ頭上デフォルト座標
            logger.info(">>> 【3D矢印】 自動検出失敗 → デフォルト (760,210) タップ")
            tap_device(760, 210, state, "MAP_ARROW_FALLBACK")
            return "MAP_ARROW_TAP", 1.0

    # ─── 【最優先 #2】ハイライト指示テキスト ───
    tutorial_kws = ["ここをタップ", "タップしてください", "タップして下さい", "タップして"]
    for kw in tutorial_kws:
        match = has_text(ocr, kw, min_conf=0.3)
        if match:
            cx, cy = match["center"]
            logger.info(">>> 【ハイライト指示】 '%s' (%d,%d)", kw, cx, cy)
            tap_device(cx, cy, state, f"HIGHLIGHT '{kw}'")
            return "HIGHLIGHT_TAP", 0.5

    # ─── ストーリーセリフ進行 (バトル外でセリフが出ている) ───
    # 「画面をタップ」系の指示 or バトルでもホームでもない日本語テキストが複数ある
    is_battle_now = any(kw in " ".join(texts) for kw in
                        ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "必殺技", "BREAK", "WAVE", "Turn"])
    tap_screen_kws = ["画面をタップ", "タップして進む", "タップで進む", "タップしてください", "TOUCH TO CONTINUE"]
    tap_screen = has_any(ocr, tap_screen_kws)
    if tap_screen and not is_battle_now:
        cx, cy = tap_screen["center"]
        logger.info(">>> 【画面タップ指示】 '%s' (%d,%d)", tap_screen["text"], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP_HINT")
        return "STORY_TAP", 0.3

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
        return "QUEST_START", 2.0
    elif stage_num:
        fx, fy = int(W * 0.74), int(H * 0.91)
        logger.info(">>> クエストマップ(固定) (%d,%d)", fx, fy)
        tap_device(fx, fy, state, "QUEST_START_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0

    # ─── バトル画面 ───
    # 「AUTO」「HP」「戦闘」はストーリー画面にも出るため除外、戦闘固有キーワードで判定
    battle_keywords = ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "BREAK", "Turn", "WAVE"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1

        # バトルチュートリアル: バフ効果
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx, by = int(W * 0.888), int(H * 0.667)
            logger.info(">>> バフチュートリアル (%d,%d)", bx, by)
            tap_device(bx, by, state, "BUFF_TUTORIAL")
            return "BATTLE_TUTORIAL", 1.0

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
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            hx, hy = int(W * 0.862), int(H * 0.778)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL")
            time.sleep(0.8)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 攻撃対象変更
        if has_any(ocr, ["攻撃対象を変更", "対象を変更"]):
            ex, ey = int(W * 0.651), int(H * 0.361)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 一般ポップアップ
        tutorial_popup = has_any(ocr, [
            "タイムライン", "表示されている", "行動してい",
            "ここをタップ", "タップしてください",
            "ことができます", "することができ",
            "スキルを使用", "スキルを選択", "カードを選択",
            "ましょう", "みましょう", "てみよう",
            "一番上に", "順番に行動", "DEFENDER",
            # ロール説明ポップアップ
            "ロールについて", "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "HEALER",
            # バトル説明ポップアップ
            "STEP1", "STEP2", "バトルシステム", "ブレイクし",
        ])
        # バトル速度ツールチップは速度ボタン本体 (1409,19) をタップして消す
        speed_tip = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if speed_tip:
            logger.info(">>> 速度ツールチップ → 速度ボタン (1409,19) タップ")
            tap_device(1409, 19, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 1.0
        if tutorial_popup:
            # ポップアップは画面中央タップで閉じる
            cx, cy = int(W * 0.5), int(H * 0.5)
            logger.info(">>> バトルチュートリアル popup '%s' → 中央 (%d,%d)",
                        tutorial_popup["text"][:10], cx, cy)
            tap_device(cx, cy, state, "BATTLE_TUTORIAL_POPUP")
            return "BATTLE_TUTORIAL", 1.0

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
                return "BATTLE_STALL", 1.0
            elif stall_phase == 4:
                hx, hy = int(W * 0.862), int(H * 0.778)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU")
                time.sleep(0.8)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 1.0

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
        return "RESULT_TAP", 1.0

    # ─── スキップ ───
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if skip_match:
        cx, cy = skip_match["center"]
        logger.info(">>> スキップ '%s' (%d,%d)", skip_match["text"], cx, cy)
        tap_device(cx, cy, state, f"SKIP '{skip_match['text']}'")
        return "SKIP", 0.5

    # ─── 閉じるボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        logger.info(">>> 閉じる '%s' (%d,%d)", close_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CLOSE '{close_match['text']}'")
        return "CLOSE", 0.5

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
        return "AGREE", 1.0

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
        return "CONFIRM", 1.0

    # ─── ストーリー/会話 (下部テキストボックス) ───
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6]
    if lower_texts and len(ocr) <= 15:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── ログインボーナス等 ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        cx, cy = bonus_match["center"]
        logger.info(">>> ポップアップ '%s' (%d,%d)", bonus_match["text"], cx, cy)
        tap_device(cx, cy, state, "POPUP_TAP")
        return "POPUP_TAP", 1.0

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
            state.pre_popup_tap_count = 0  # ポップアップ試行カウンタもリセット

            # ── ADV 高速モード: OCR スキップして画面下部を即連打 ──
            # 前回 STORY_TAP かつ phash 変化が小さい（テキスト送り）→ 即タップ
            if (state.last_action == "STORY_TAP" and
                    PHASH_THRESHOLD <= dist <= ADV_RAPID_PHASH_MAX):
                logger.info("[iter %d] phash_dist=%d ADV_RAPID → 即タップ (OCR skip)", i, dist)
                time.sleep(0.05)
                adb(f"shell input tap 760 650")
                state.total_taps += 1
                logger.info("  ACTION_TAKEN ADV_RAPID_TAP (760,650)")
                state.last_phash = cur_phash
                time.sleep(0.3)
                continue

        else:
            # 画面変化なし
            state.same_phash_count += 1
            state.total_ocr_skipped += 1

            # N 回変化なし → 強制 OCR (デッドロック防止の核心)
            if state.same_phash_count >= FORCE_ANALYZE_AFTER:
                logger.info("[iter %d] phash_dist=%d same=%d → 強制 OCR",
                            i, dist, state.same_phash_count)
                screen_changed = True  # OCR ブロックへ進む

            else:
                # まだ待機フェーズ — シーン別インターバル
                _poll = SCENE_INTERVAL.get(state.current_scene, POLL_INTERVAL)
                if i % 3 == 0:
                    logger.info("[%s][iter %d] phash_dist=%d same=%d — polling (%.1fs)...",
                                state.current_scene, i, dist, state.same_phash_count, _poll)
                state.last_phash = cur_phash
                time.sleep(_poll)
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
        # ── シーン分類 ──
        scene, next_interval = classify_scene(texts, state.last_action)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
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
            logger.info("  [%s][%s] wait %.1fs | next_check: %.1fs",
                        scene, action, wait_sec, next_interval)
            time.sleep(wait_sec)

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)


if __name__ == "__main__":
    main()
