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
import gc
import logging
import os
import subprocess
import sys
import time

# ─── SIGSEGV 防止: OpenMP / cv2 スレッド競合対策 ─────────────────
# OpenMP スレッドの重複を許可 (PaddlePaddle + OpenCV 共存時のSIGSEGV防止)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
# OpenMP スレッド数を制限してメモリ競合を防ぐ
os.environ.setdefault("OMP_NUM_THREADS", "2")
# OpenCV のスレッド数も制限
os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
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
    # StrategicDecisionEngine: 予測トラッキング
    last_prediction: str = ""
    last_prediction_desc: str = ""
    last_tap_text: str = ""
    last_action_pre_phash: str = ""
    # ホーム画面からクエスト等への遷移試行回数 (遷移中の誤停止を防ぐ)
    home_nav_count: int = 0


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


# ─── Smart Tap: 金色ボタン矩形の幾何学的中心を検出 ──────────────────
# OCR の center y はテキスト領域中心であり、ボタン hitbox 中心より約 36px 下に
# ずれる傾向がある (テキストのパディング + レイアウト起因)。
# このオフセットはフォールバックとして使用する。
_BUTTON_Y_OFFSET = -36  # OCR y → 実 hitbox y (フォールバック用補正量)


def smart_tap_button(
    img_path: Path,
    ocr_cx: int,
    ocr_cy: int,
    search_r: int = 120,
) -> tuple[int, int]:
    """OCR テキスト座標周辺から金色ボタン枠を検出し、幾何学的中心を返す。

    検出失敗時は OCR 座標に _BUTTON_Y_OFFSET を加算してフォールバック。
    返値: (tap_x, tap_y)
    """
    try:
        import cv2
        import numpy as np

        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            raise ValueError("imread failed")
        h_img, w_img = img_bgr.shape[:2]

        # 探索エリア: OCR 中心から search_r px の矩形
        x1 = max(0, ocr_cx - search_r)
        y1 = max(0, ocr_cy - search_r)
        x2 = min(w_img, ocr_cx + search_r)
        y2 = min(h_img, ocr_cy + search_r)

        roi = img_bgr[y1:y2, x1:x2]
        roi_hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # 金色ボタン枠の HSV レンジ (実測: RGB≈(190,165,122) → H≈30,S≈80,V≈190)
        lower_gold = np.array([15, 50, 120], dtype=np.uint8)
        upper_gold = np.array([42, 190, 235], dtype=np.uint8)
        mask = cv2.inRange(roi_hsv, lower_gold, upper_gold)

        # モルフォロジー: ノイズ除去 + 枠の繋ぎ合わせ
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.dilate(mask, kernel, iterations=2)
        mask = cv2.erode(mask, kernel, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best_rect = None
        best_area = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 200:
                continue
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            # ボタンらしい形状: 横長 or 正方形、かつ適切なサイズ
            if rw > 30 and rh > 10 and rw >= rh * 0.5:
                if area > best_area:
                    best_area = area
                    best_rect = (rx + x1, ry + y1, rw, rh)

        if best_rect:
            bx, by, bw, bh = best_rect
            cx = bx + bw // 2
            cy = by + bh // 2
            logger.info("  [SmartTap] 金色ボタン検出 rect=(%d,%d,%d,%d) → center=(%d,%d)",
                        bx, by, bw, bh, cx, cy)
            return cx, cy

    except Exception as e:
        logger.debug("  [SmartTap] エラー: %s", e)

    # フォールバック: OCR 座標に定数オフセット適用
    fallback_y = ocr_cy + _BUTTON_Y_OFFSET
    logger.info("  [SmartTap] フォールバック OCR(%d,%d) → (%d,%d) (offset=%d)",
                ocr_cx, ocr_cy, ocr_cx, fallback_y, _BUTTON_Y_OFFSET)
    return ocr_cx, fallback_y


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
                "require_ocr": meta.get("require_ocr", []),
                "require_ocr_all": meta.get("require_ocr_all", []),
            }
            count += 1
        if count:
            logger.info("[AssetManager] %d テンプレート読込: %s",
                        count, list(self._templates.keys()))

    def match(self, screenshot_path: Path,
              ocr_texts: Optional[list[str]] = None) -> Optional[tuple[int, int, str]]:
        """
        スクリーンショットと全テンプレートを比較。
        ocr_texts が渡された場合、require_ocr 条件を満たすテンプレートのみ照合。
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
            # require_ocr チェック: いずれか1つのキーワードがOCRにあればOK (OR条件)
            required = data.get("require_ocr", [])
            if required and ocr_texts is not None:
                if not any(kw in t for kw in required for t in ocr_texts):
                    logger.debug("[Asset] '%s' skip: require_ocr not found in OCR", name)
                    continue
            # require_ocr_all チェック: すべてのキーワードがOCRに存在しなければスキップ (AND条件)
            required_all = data.get("require_ocr_all", [])
            if required_all and ocr_texts is not None:
                if not all(any(kw in t for t in ocr_texts) for kw in required_all):
                    logger.debug("[Asset] '%s' skip: require_ocr_all not all found in OCR", name)
                    continue
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
                      threshold: float = DEFAULT_THRESHOLD,
                      require_ocr: list[str] | None = None) -> bool:
        """
        スクリーンショットの指定領域を切り抜いてテンプレートとして保存。
        次回起動時から [Asset Match] で高速検出可能になる。
        require_ocr: このテンプレートを使うのに必要なOCRキーワードリスト
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
        meta: dict = {"action": action, "offset": list(offset), "threshold": threshold}
        if require_ocr:
            meta["require_ocr"] = require_ocr
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        # インメモリキャッシュに即時追加
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        self._templates[name] = {
            "img": gray, "threshold": threshold,
            "action": action, "offset": list(offset),
            "require_ocr": require_ocr or [],
        }
        logger.info("[Asset] テンプレート自動保存: '%s' (%dx%d) action=%s require_ocr=%s",
                    name, crop.shape[1], crop.shape[0], action, require_ocr)
        return True

    def reload(self) -> None:
        self._templates.clear()
        self._load_templates()


# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


# ─── 戦略的意思決定エンジン (StrategicDecisionEngine) ──────────
class StrategicDecisionEngine:
    """
    UIアフォーダンス解析 + 行動予測 + 経験学習エンジン。

    1. find_buttons()     : 視覚的特徴（色・形）からタップ可能領域を抽出
    2. predict_outcome()  : OCRテキストの意味から結果を予測
    3. verify_and_learn() : タップ結果を検証し knowledge_base.json に蓄積
    """

    KNOWLEDGE_PATH = _CRAWLER_ROOT / "storage" / "knowledge_base.json"

    # テキストキーワード → (action_type, 予測説明)
    PREDICTION_MAP: dict[str, tuple[str, str]] = {
        # ガチャ・召喚
        "ガシャ":    ("GACHA_DRAW",     "召喚演出・アイテム獲得シーンが発生する"),
        "ガチャ":    ("GACHA_DRAW",     "召喚演出・アイテム獲得シーンが発生する"),
        "召喚":      ("GACHA_DRAW",     "召喚演出が発生する"),
        "受け取る":  ("RECEIVE_ITEM",   "アイテム受け取り処理が実行される"),
        "獲得":      ("RECEIVE_ITEM",   "アイテム獲得処理が実行される"),
        # 進行・スキップ
        "次へ":      ("SCENE_ADVANCE",  "シーンが遷移してストーリーが進む"),
        "スキップ":  ("SKIP_STORY",     "ストーリーシーンがスキップされる"),
        "SKIP":      ("SKIP_STORY",     "ストーリーシーンがスキップされる"),
        "進む":      ("SCENE_ADVANCE",  "シーンが遷移する"),
        "TAP TO":    ("SCENE_ADVANCE",  "シーンが進む"),
        "START":     ("GAME_START",     "ゲームまたはバトルが開始する"),
        "開始":      ("BATTLE_START",   "バトルまたはクエストが開始する"),
        "出撃":      ("BATTLE_START",   "クエストが開始しバトル画面へ遷移する"),
        "戦闘":      ("BATTLE_START",   "クエストが開始しバトル画面へ遷移する"),
        # バトル
        "AUTO":      ("AUTO_BATTLE",    "バトルがAUTOモードで自動進行する"),
        "攻撃":      ("BATTLE_ATTACK",  "戦闘ターンが進行する"),
        "通常攻撃":  ("NORMAL_ATTACK",  "通常攻撃が実行される"),
        "必殺技":    ("SPECIAL_ATTACK", "必殺技演出が発生し大ダメージが入る"),
        "スキル":    ("SKILL_USE",      "スキルが発動する"),
        # 閉じる・確認
        "OK":        ("CONFIRM",        "確認ダイアログが閉じてメニューに戻る"),
        "閉じる":    ("CLOSE_DIALOG",   "ダイアログが閉じる"),
        "確認":      ("CONFIRM",        "確認処理が実行される"),
        "完了":      ("COMPLETE",       "処理が完了してメニューに戻る"),
        "決定":      ("CONFIRM",        "選択が確定される"),
        "了解":      ("CONFIRM",        "確認ダイアログが閉じる"),
        "わかった":  ("CONFIRM",        "確認ダイアログが閉じる"),
        "リザルト":  ("RESULT",         "バトル結果画面が表示される"),
        "Result":    ("RESULT",         "バトル結果画面が表示される"),
        # ナビゲーション
        "ホーム":    ("GO_HOME",        "ホーム画面に戻る"),
        "メニュー":  ("OPEN_MENU",      "メニューが開く"),
        "クエスト":  ("OPEN_QUEST",     "クエスト選択画面へ遷移する"),
        "ショップ":  ("OPEN_SHOP",      "ショップ画面へ遷移する"),
        "編成":      ("OPEN_FORMATION", "パーティ編成画面へ遷移する"),
    }

    # ゲームUIの色彩意味論: 色 → タップ優先度
    COLOR_PRIORITY: dict[str, int] = {
        "orange": 10,   # 橙: 攻撃・決定（最優先）
        "red":     9,   # 赤: 攻撃・警告
        "blue":    7,   # 青: 回復・進む
        "green":   6,   # 緑: 回復・安全
        "purple":  5,   # 紫: 魔法・特殊
        "yellow":  4,   # 黄: 注意・ハイライト
        "gray":    2,   # 灰: キャンセル・戻る
        "white":   1,   # 白: 中立
        "unknown": 0,
    }

    def __init__(self):
        self._knowledge: dict = self._load_knowledge()

    def _load_knowledge(self) -> dict:
        if self.KNOWLEDGE_PATH.exists():
            try:
                import json
                return json.loads(self.KNOWLEDGE_PATH.read_text())
            except Exception:
                pass
        return {"patterns": {}, "stats": {"total_taps": 0, "verified": 0}}

    def _save_knowledge(self) -> None:
        import json
        self.KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.KNOWLEDGE_PATH.write_text(
            json.dumps(self._knowledge, ensure_ascii=False, indent=2)
        )

    def _classify_color(self, roi_bgr) -> str:
        """BGR ROI の主要色をゲームUI色彩設計に基づいて分類。"""
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        s = float(np.mean(hsv[:, :, 1]))
        v = float(np.mean(hsv[:, :, 2]))
        h = float(np.mean(hsv[:, :, 0]))
        if s < 40:
            return "white" if v > 180 else "gray"
        # OpenCV HSV: H は 0-180
        if h < 10 or h > 155:
            return "red"
        if h < 25:
            return "orange"
        if h < 35:
            return "yellow"
        if h < 85:
            return "green"
        if h < 125:
            return "blue"
        return "purple"

    def find_buttons(self, img_path: Path) -> list[dict]:
        """
        エッジ検出 + 輪郭抽出でボタン候補領域を検出。
        矩形・丸みを帯びた角・高コントラスト縁を持つ領域を「タップ可能」と判定。
        Returns: [{"cx","cy","w","h","color","priority","area"}, ...] 優先度降順
        """
        try:
            import cv2
            import numpy as np
            img = cv2.imread(str(img_path))
            if img is None:
                return []
            H, W = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
            dilated = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
            contours, _ = cv2.findContours(
                dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            buttons = []
            for c in contours:
                area = cv2.contourArea(c)
                # ボタンサイズフィルタ: 1000px² ≤ area ≤ 25% 画面
                if area < 1000 or area > W * H * 0.25:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                asp = w / h if h > 0 else 0
                # ボタンのアスペクト比: 0.5 〜 12
                if asp < 0.5 or asp > 12 or w < 40 or h < 20:
                    continue
                color = self._classify_color(img[y:y + h, x:x + w])
                priority = self.COLOR_PRIORITY.get(color, 0)
                buttons.append({
                    "x": x, "y": y, "w": w, "h": h,
                    "cx": x + w // 2, "cy": y + h // 2,
                    "area": int(area), "color": color, "priority": priority,
                })
            buttons.sort(key=lambda b: (b["priority"], b["area"]), reverse=True)
            return buttons[:20]
        except Exception as e:
            logger.debug("[SDE] find_buttons error: %s", e)
            return []

    def predict_outcome(self, text: str) -> tuple[str, str]:
        """
        OCRテキストからタップ後の結果を予測。
        長いキーワードを優先（"通常攻撃" > "攻撃" など）。
        Returns: (action_type, description)
        """
        # キーワード長の降順でマッチング（長い=具体的なキーワードを優先）
        for kw, (action_type, desc) in sorted(
            self.PREDICTION_MAP.items(), key=lambda x: len(x[0]), reverse=True
        ):
            if kw in text:
                return action_type, desc
        return "UNKNOWN", "未知の操作が実行される"

    def log_prediction(self, text: str, cx: int, cy: int) -> tuple[str, str]:
        """予測を生成してログ出力。Returns: (action_type, description)"""
        action_type, desc = self.predict_outcome(text)
        if action_type != "UNKNOWN":
            logger.info(
                "[PREDICTION] Tapping '%s' at (%d,%d) -> Expecting %s: %s",
                text[:20], cx, cy, action_type, desc,
            )
        return action_type, desc

    def verify_and_learn(self, pre_phash: str, post_phash: str,
                         action_type: str, desc: str, tap_text: str) -> None:
        """
        タップ後のphash変化から予測の正否を検証し、knowledge_base.jsonに記録。
        - phash距離 >= PHASH_THRESHOLD → 画面変化あり = SUCCESS
        - phash距離 < PHASH_THRESHOLD  → 画面変化なし = NO_CHANGE
        """
        if not pre_phash or not post_phash or action_type == "UNKNOWN":
            return
        try:
            dist = phash_distance(pre_phash, post_phash)
            scene_changed = dist >= PHASH_THRESHOLD
            key = f"{action_type}:{tap_text[:20]}"
            stats = self._knowledge["stats"]
            stats["total_taps"] = stats.get("total_taps", 0) + 1
            stats["verified"] = stats.get("verified", 0) + 1
            pat = self._knowledge["patterns"].setdefault(key, {
                "prediction": action_type, "description": desc,
                "text": tap_text, "success_count": 0, "failure_count": 0,
                "last_seen": "",
            })
            if scene_changed:
                pat["success_count"] += 1
                logger.info("[LEARNING] '%s'→%s ✓ dist=%d (ok=%d)",
                            tap_text[:15], action_type, dist, pat["success_count"])
            else:
                pat["failure_count"] += 1
                logger.info("[LEARNING] '%s'→%s ✗ dist=%d (fail=%d)",
                            tap_text[:15], action_type, dist, pat["failure_count"])
            pat["last_seen"] = datetime.now().isoformat()
            # 10タップごとに保存
            if stats["total_taps"] % 10 == 0:
                self._save_knowledge()
        except Exception as e:
            logger.debug("[SDE] verify_and_learn error: %s", e)

    def report_screen_affordances(self, img_path: Path, ocr_results: list) -> None:
        """
        現在画面のUIアフォーダンス解析レポートをログ出力。
        ボタン候補領域を検出し、各領域内のOCRテキストから行動を予測する。
        """
        buttons = self.find_buttons(img_path)
        if not buttons:
            return
        logger.info("[SDE] === UIアフォーダンス解析: %d個のボタン候補 ===", len(buttons))
        for i, btn in enumerate(buttons[:5]):
            # ボタン領域内のOCRテキストを抽出
            btn_texts = [
                r["text"] for r in ocr_results
                if (btn["x"] <= r["center"][0] <= btn["x"] + btn["w"] and
                    btn["y"] <= r["center"][1] <= btn["y"] + btn["h"])
            ]
            text_str = " ".join(btn_texts) if btn_texts else "(no text)"
            action_type, _ = self.predict_outcome(text_str)
            logger.info(
                "[SDE] #%d (%d,%d) %dx%d color=%s prio=%d '%s' → %s",
                i + 1, btn["cx"], btn["cy"], btn["w"], btn["h"],
                btn["color"], btn["priority"], text_str[:20], action_type,
            )

    # ─── 要素キーワード → (英語名, 検出方法) ───
    _ELEMENT_MAP: dict[str, tuple[str, str]] = {
        "矢印":     ("arrow",   "arrow"),
        "矩形":     ("rect",    "button"),
        "ボタン":   ("btn",     "button"),
        "アイコン": ("icon",    "button"),
        "スキップ": ("skip",    "ocr:スキップ"),
        "次へ":     ("next",    "ocr:次へ"),
        "OK":       ("ok",      "ocr:OK"),
        "閉じる":   ("close",   "ocr:閉じる"),
        "ホーム":   ("home",    "ocr:ホーム"),
        "ガチャ":   ("gacha",   "ocr:ガチャ"),
        "ガシャ":   ("gacha",   "ocr:ガシャ"),
        "戦闘":     ("battle",  "ocr:戦闘"),
        "出撃":     ("deploy",  "ocr:出撃"),
        "クエスト": ("quest",   "ocr:クエスト"),
    }

    # ─── 役割キーワード → プレフィックス ───
    _ROLE_MAP: dict[str, str] = {
        "ボタン": "btn",
        "アイコン": "icon",
        "タブ": "tab",
        "メニュー": "menu",
        "リスト": "list",
    }

    def learn_from_instruction(
        self,
        instruction: str,
        screenshot_path: Path,
        ocr_results: list,
        asset_manager: "AssetManager",
    ) -> Optional[str]:
        """
        ユーザーの曖昧な指示から UI 要素を自律的に抽出・命名・保存する。

        例:
            "矢印はボタン"   → 矢印を検出 → "btn_arrow" として保存
            "スキップはボタン"→ OCRでスキップ検出 → "btn_skip" として保存

        Returns: 保存したテンプレート名 or None
        """
        # 役割パース
        role = "btn"
        for kw, r in self._ROLE_MAP.items():
            if kw in instruction:
                role = r
                break

        # 要素パース
        element = "unknown"
        find_method = "button"
        for kw, (en_name, method) in self._ELEMENT_MAP.items():
            if kw in instruction:
                element = en_name
                find_method = method
                break

        name = f"{role}_{element}"
        W, H = ANALYSIS_W, ANALYSIS_H
        x1 = y1 = x2 = y2 = 0
        cx: Optional[int] = None

        if find_method == "arrow":
            pos = find_3d_arrow(screenshot_path)
            if pos:
                cx, cy_val = pos
                half_w, half_h = 80, 60
                x1 = max(0, cx - half_w)
                y1 = max(0, cy_val - half_h)
                x2 = min(W, cx + half_w)
                y2 = min(H, cy_val + half_h)

        elif find_method.startswith("ocr:"):
            ocr_kw = find_method[4:]
            match = find_best(ocr_results, ocr_kw)
            if match:
                cx, _ = match["center"]
                box = match["box"]
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                pad = 10
                x1 = max(0, min(xs) - pad)
                y1 = max(0, min(ys) - pad)
                x2 = min(W, max(xs) + pad)
                y2 = min(H, max(ys) + pad)

        else:
            # ボタン検出: find_buttons から最優先候補を使用
            buttons = self.find_buttons(screenshot_path)
            if buttons:
                btn = buttons[0]
                cx = btn["cx"]
                x1, y1 = btn["x"], btn["y"]
                x2, y2 = btn["x"] + btn["w"], btn["y"] + btn["h"]

        if cx is None:
            logger.warning("[SemanticAsset] '%s' から要素を検出できませんでした", instruction)
            return None

        saved = asset_manager.save_template(
            screenshot_path, x1, y1, x2, y2,
            name=name,
            action=f"SEMANTIC_{name.upper()}",
            threshold=0.75,
        )
        if saved:
            logger.info(
                "[SemanticAsset] '%s' → '%s' 登録完了 (%d,%d)-(%d,%d)",
                instruction, name, x1, y1, x2, y2,
            )
            return name
        return None


# グローバル StrategicDecisionEngine インスタンス
STRATEGIC_ENGINE = StrategicDecisionEngine()


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
        asset_hit = ASSET_MANAGER.match(analysis_path, ocr_texts=texts)
        if asset_hit:
            cx, cy, action = asset_hit
            logger.info(">>> [Asset Match] '%s' → (%d,%d)", action, cx, cy)
            # スワイプ系アクションの処理
            if action == "SWIPE_UP":
                tmpl_meta = ASSET_MANAGER._templates.get("tutorial_swipe_pointer", {})
                sx = tmpl_meta.get("swipe_from_x", cx)
                sy = tmpl_meta.get("swipe_from_y", H - 50)
                ex = tmpl_meta.get("swipe_to_x", cx)
                ey = tmpl_meta.get("swipe_to_y", 50)
                dur = tmpl_meta.get("swipe_duration_ms", 3000)
                logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms", sx, sy, ex, ey, dur)
                swipe(sx, sy, ex, ey, dur)
                return "SWIPE_UP", 1.5
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

    # ─── 【最優先 #0-b-extra】プレイヤー名入力ダイアログ ───
    # 「プレイヤー名を入力してください」→ 名前入力 → OKタップ
    # 注意: OCR で "OK" の center が y≈593 と検出されるが、
    #        実際のボタンヒットゾーンはゴールデンエリア y≈555-575 (実測)
    name_input = has_text(ocr, "プレイヤー名を入力", min_conf=0.3)
    if name_input:
        # 入力済みテキストを確認 (プレースホルダー・UI テキスト以外のひらがな/英字)
        ui_words = {"プレイヤー名を入力してください", "プレイヤー名は", "変更後3日間", "名前入力", "OK"}
        name_texts = [t for t in texts if t not in ui_words and len(t) >= 2
                      and not t.startswith("プレイヤー") and "/" not in t]
        ok_item = next(
            (item for item in ocr if "OK" in item.get("text", "") and item["center"][1] > H * 0.5),
            None
        )
        if name_texts and ok_item:
            # 名前入力済み → OKタップ (実測ヒットゾーン y=560、OCR center y=593 より上)
            cx = ok_item["center"][0]
            cy = 560  # ゴールデンボタン中心 (ピクセル解析: y=540-580がゴールデン)
            logger.info(">>> 【名前入力 OK】 入力済み='%s' → (%d,%d) タップ", name_texts[0], cx, cy)
            tap_device(cx, cy, state, "NAME_INPUT_OK")
            return "NAME_INPUT_OK", 2.0
        elif ok_item:
            # 名前未入力 → テキストフィールドをタップして "MadoDora" 入力 → Enter → OK
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (700,417)")
            tap_device(700, 417, state, "NAME_INPUT_FOCUS")
            time.sleep(0.5)
            import subprocess as _sp
            _sp.run(["adb", "-s", DEVICE_SERIAL, "shell", "input", "text", "MadoDora"], check=False)
            time.sleep(0.3)
            _sp.run(["adb", "-s", DEVICE_SERIAL, "shell", "input", "keyevent", "66"], check=False)
            logger.info(">>> 【名前入力】 'MadoDora' 入力完了 → OK タップ待ち")
            return "NAME_INPUT_TEXT", 1.5

    # ─── 【最優先 #0-b】報酬/強化結果ポップアップを即時処理 (ブロブ誤検出防止) ───
    # 「以下の内容でよろしいですか」確認ダイアログ → SmartTap で OK 物理中心をタップ
    confirm_dlg = has_text(ocr, "以下の内容でよろしいですか", min_conf=0.3)
    if confirm_dlg:
        ok_bottom = next(
            (item for item in ocr
             if "OK" in item.get("text", "") and item["center"][1] > H * 0.6),
            None
        )
        if ok_bottom:
            ocr_cx, ocr_cy = ok_bottom["center"]
        else:
            ocr_cx, ocr_cy = 1060, 633  # フォールバック推定値
        cx, cy = smart_tap_button(analysis_path, ocr_cx, ocr_cy)
        logger.info(">>> 【確認ダイアログ】 SmartTap OK (%d,%d)", cx, cy)
        tap_device(cx, cy, state, "CONFIRM_DIALOG_OK")
        return "CONFIRM_DIALOG_OK", 1.5

    # 「タップして次へ」: 報酬獲得画面の次へ進む
    tap_next = has_text(ocr, "タップして次へ", min_conf=0.3)
    if tap_next:
        cx, cy = tap_next["center"]
        logger.info(">>> 【報酬/次へ】 'タップして次へ' (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "REWARD_NEXT")
        return "REWARD_NEXT", 1.0

    # 限界突破/強化完了/レベルアップ系ポップアップ → 右上 × ボタンで閉じる
    close_popup_kws = ["限界突破", "強化完了", "レベルアップ", "称号獲得", "エピソード解放",
                       "ランクアップ", "新しいコンテンツ", "アンロック",
                       "マギアボックス", "ミッション達成", "デイリーミッション",
                       "ログインボーナス", "初心者ログイン", "キャンペーン"]

    # カルーセル型チュートリアルポップアップ (「メインクエストをPLAYして」等の複数ページ説明)
    # 閉じるボタン: ポップアップフレーム右上 (1430, 88) — 実測 2026-03-05
    carousel_popup_kws = ["メインクエストをPLAY", "ピュエラピクトゥーラ", "POWER UP"]
    carousel_match = has_any(ocr, carousel_popup_kws)
    if carousel_match:
        # 最終ページへ移動 (右ナビゲーション × 6) → フレーム右上 × をタップ
        for _ in range(6):
            tap_device(1465, 360, state, "CAROUSEL_NAV_RIGHT")
            time.sleep(0.3)
        close_x, close_y = 1430, 88
        logger.info(">>> 【カルーセルポップアップ】 '%s' → フレーム右上 (%d,%d) タップ",
                    carousel_match["text"][:10], close_x, close_y)
        tap_device(close_x, close_y, state, "CAROUSEL_CLOSE")
        return "CLOSE_POPUP", 2.0
    close_popup = has_any(ocr, close_popup_kws)
    if close_popup:
        close_x = W - 40  # 右上 × ボタン (1520-40=1480)
        close_y = 40
        logger.info(">>> 【%s ポップアップ】 → × (%d,%d) タップ", close_popup["text"][:6], close_x, close_y)
        tap_device(close_x, close_y, state, f"CLOSE_POPUP_{close_popup['text'][:6]}")
        return "CLOSE_POPUP", 1.5

    # 「〜してみましょう」型チュートリアルガイド + ブロブスタック → × で閉じる
    # 例: "今回は自動編成をしてみましょう。" が表示されたまま動かない場合
    if state.blob_same_count >= 5:
        tutorial_guide = (has_text(ocr, "てみましょう", min_conf=0.3) or
                          has_text(ocr, "しましょう", min_conf=0.3))
        is_battle_guide = any(kw in " ".join(texts) for kw in ["通常攻撃", "BREAK", "WAVE"])
        if tutorial_guide and not is_battle_guide:
            close_x = W - 40  # 右上 × ボタン (1480, 40)
            close_y = 40
            logger.info(">>> 【チュートリアルガイド スタック】 '%s' → × (%d,%d) タップ",
                        tutorial_guide["text"][:10], close_x, close_y)
            tap_device(close_x, close_y, state, "TUTORIAL_GUIDE_CLOSE")
            state.blob_same_count = 0
            return "CLOSE_POPUP", 1.5

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        # 「AUTO」のみはストーリー画面にも表示されるため除外、戦闘固有キーワードで判定
        is_battle_screen = any(kw in " ".join(texts) for kw in
                               ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "必殺技", "BREAK", "WAVE", "Turn"])
        # タイトル画面 / ホーム画面検出: ブロブ誤検出を防ぐ
        _nav_joined = " ".join(texts)
        # 利用規約画面・同意ダイアログが存在する場合はタイトル画面と区別する
        _is_tos_screen = "利用規約" in _nav_joined or "同意してゲームを始める" in _nav_joined
        is_title_screen = (
            not _is_tos_screen and (
                any(kw in _nav_joined for kw in ["TAP TO START", "Magia Exedra"]) or
                ("動画配信" in _nav_joined and any(kw in _nav_joined for kw in ["魔法", "少女", "まどか", "マギカ"])) or
                ("VID" in _nav_joined and any(kw in _nav_joined for kw in ["魔法", "少女", "まどか", "マギカ"]))
            )
        )
        if is_title_screen:
            logger.info("  タイトル画面検出 → TAP TO START (760,628) タップ")
            tap_device(760, 628, state, "TITLE_TAP_START")
            return "TITLE_TAP", 3.0
        # ホーム画面検出: ホームナビキーワードが2個以上 → キャラ画像のブロブ誤検出をスキップ
        _home_nav_kws = ["クエスト", "ショップ", "ガチャ", "ガシャ", "ユニオン",
                         "光の間", "パーティ", "プレイヤーマッチ", "お知らせ",
                         "イベント", "マイページ", "編成", "MAGIA EXEDRA"]
        _home_kw_count = sum(1 for h in _home_nav_kws if any(h in t for t in texts))
        # ガチャ結果画面検出: "NEW" が 3件以上 → キャラ画像のオレンジ色を誤検出するためブロブ無効化
        new_count = sum(1 for t in texts if t == "NEW")
        is_gacha_result = new_count >= 3 and not is_battle_screen
        if is_gacha_result:
            logger.info("  ガチャ結果画面検出 (NEW×%d) → もや誤検出スキップ", new_count)
            # OKボタンをダブルタップして進む (シングルタップでは反応しないゲームの挙動対策)
            ok_match = has_text(ocr, "OK", min_conf=0.5)
            if ok_match:
                cx, cy = ok_match["center"]
                action_type, desc = STRATEGIC_ENGINE.log_prediction("OK", cx, cy)
                state.last_prediction = action_type
                state.last_prediction_desc = desc
                state.last_tap_text = "OK"
                state.last_action_pre_phash = state.last_phash
                logger.info(">>> 【ガチャ結果】 OK (%d,%d) → ダブルタップ", cx, cy)
                tap_device(cx, cy, state, "GACHA_RESULT_OK_1")
                time.sleep(0.3)
                tap_device(cx, cy, state, "GACHA_RESULT_OK_2")
                return "GACHA_OK", 2.0
            # OKがない場合は画面中央をダブルタップ (NEW×8の初期表示 = タップで詳細へ)
            logger.info(">>> 【ガチャ結果初期】 OK未検出 → 画面中央ダブルタップ")
            tap_device(760, 360, state, "GACHA_RESULT_CENTER_1")
            time.sleep(0.3)
            tap_device(760, 360, state, "GACHA_RESULT_CENTER_2")
            return "GACHA_OK", 2.0
        # min_area は常に400。空間フィルタ(下記)で誤検出を排除するため過大閾値は不要
        # ホーム画面 / 利用規約ダイアログ / システムダイアログはブロブ誤検出になるためスキップ
        _is_system_dialog = any(kw in _nav_joined for kw in
                                ["画質を設定", "高画質", "省エネ", "省工ネ", "データ引き継ぎ",
                                 "サポート", "お問い合わせ", "キャッシュクリア"])
        if _home_kw_count >= 2:
            logger.info("  ホーム画面検出 (nav×%d) → MOYA_TAP スキップ", _home_kw_count)
            blobs = []
        elif _is_tos_screen or _is_system_dialog:
            logger.info("  システムダイアログ/利用規約検出 → MOYA_TAP スキップ")
            blobs = []
        else:
            blobs = find_finger_blobs(analysis_path, min_area=400)
            # 画面端の誤検出を除去: y<36px(上端)または x>W-40px(右端最端)はシステムUI
            blobs = [(x, y, a) for x, y, a in blobs if y > 36 and x < W - 40]
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
            # 50px 近接判定: アニメーション中のブロブ (±20px移動) でもカウントが継続する
            if state.last_blob_xy == (0, 0):
                # 初回検出: 基準座標を設定してカウントを0にリセット
                state.last_blob_xy = (fx, fy)
                state.blob_same_count = 0
            elif abs(fx - state.last_blob_xy[0]) <= 50 and abs(fy - state.last_blob_xy[1]) <= 50:
                state.blob_same_count += 1
                state.last_blob_xy = (fx, fy)  # 追跡: 次回比較基準を更新
            else:
                state.blob_same_count = 0
                state.last_blob_xy = (fx, fy)
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
                    require_ocr=["矢印をタップ"],
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
    tap_screen_kws = ["画面をタップ", "タップして進む", "タップで進む", "タップしてください",
                      "タップして次へ", "TOUCH TO CONTINUE"]
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
        state.home_reached = True
        # チュートリアルポインタが同一座標でスタックしている → クエスト探索へ移行
        if state.blob_same_count >= 5:
            logger.info(">>> ホーム画面 + もやスタック → クエストへナビゲート")
            state.blob_same_count = 0  # リセット: 次回はまたブロブ検出を試みる
            state.home_nav_count += 1
            quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
            if quest_btn:
                cx, cy = quest_btn["center"]
                logger.info(">>> クエストボタン (%d,%d) タップ", cx, cy)
                tap_device(cx, cy, state, "QUEST_FROM_HOME")
                return "QUEST_FROM_HOME", 3.0
            # OCR未検出 → 右下固定座標 (1520×720 画面での位置)
            tap_device(1337, 707, state, "QUEST_FIXED")
            return "QUEST_FROM_HOME", 3.0
        # クエストへの遷移を試みた後、まだホーム画面が表示されている → 遷移待ち
        if state.home_nav_count > 0:
            logger.info(">>> ホーム画面 + 遷移試行 %d回目 → 画面変化待ち", state.home_nav_count)
            return "HOME_NAV_WAIT", 2.0
        logger.info(">>> ホーム画面検出! (%d個) チュートリアル誘導中...", home_count)
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
        text = result_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> バトル結果 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, "RESULT_TAP")
        return "RESULT_TAP", 1.0

    # ─── スキップ ───
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if skip_match:
        cx, cy = skip_match["center"]
        text = skip_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> スキップ '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"SKIP '{text}'")
        return "SKIP", 0.5

    # ─── 閉じるボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        logger.info(">>> 閉じる '%s' (%d,%d)", close_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CLOSE '{close_match['text']}'")
        return "CLOSE", 0.5

    # ─── ゲーム内システムダイアログ (画質設定・ダウンロード確認 等) ───
    # smart_tap_button で金色ボタン枠の幾何学的中心を取得 (OCR ずれを排除)
    sys_dlg_kws = ["画質を設定", "アセット更新", "ダウンロードを開始", "ダウンロードしますか",
                   "Wi-Fiを使用", "モバイル通信でダウンロード", "ダウンロードが完了しました"]
    sys_dlg_match = has_any(ocr, sys_dlg_kws, min_conf=0.3)
    if sys_dlg_match:
        ok_item = next((item for item in ocr if "OK" in item.get("text", "")), None)
        if ok_item and ok_item["center"][0] > W * 0.5:
            ocr_ok_x, ocr_ok_y = ok_item["center"]
        else:
            ocr_ok_x, ocr_ok_y = int(W * 0.65), 633  # フォールバック推定値
        ok_x, ok_y = smart_tap_button(analysis_path, ocr_ok_x, ocr_ok_y)
        logger.info(">>> 【システムダイアログ】 '%s' → SmartTap OK (%d,%d)",
                    sys_dlg_match["text"][:15], ok_x, ok_y)
        tap_device(ok_x, ok_y, state, "SYSTEM_DLG_OK")
        return "SYSTEM_DLG_OK", 2.0

    # ─── 利用規約同意ダイアログ ───
    # 「同意してゲームを始める」ボタンを右下の固定座標または OCR 座標でタップ
    tos_screen = has_any(ocr, ["同意してゲームを始める", "プライバシーポリシー"], min_conf=0.3)
    if tos_screen and has_text(ocr, "利用規約", min_conf=0.3):
        # "始める" または "ゲームを始める" を OCR で探して座標タップ
        agree_ocr = has_any(ocr, ["始める", "ゲームを始める", "同意してゲームを始める"], min_conf=0.3)
        if agree_ocr:
            cx, cy = agree_ocr["center"]
            logger.info(">>> 【利用規約同意】 '%s' (%d,%d) タップ", agree_ocr["text"][:10], cx, cy)
            tap_device(cx, cy, state, "AGREE_TOS")
        else:
            logger.info(">>> 【利用規約同意】 固定座標 (1100,640) タップ")
            tap_device(1100, 640, state, "AGREE_TOS")
        return "AGREE_TOS", 3.0

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
                                   "戦闘", "出撃", "クエスト開始", "バトル開始",
                                   # チュートリアルで案内されるボタン名
                                   "自動編成", "一括受取", "強化", "合成", "強化素材",
                                   "クエスト", "探索開始", "バトル"])
    if confirm_match:
        cx, cy = confirm_match["center"]
        text = confirm_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> 確認 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"CONFIRM '{text}'")
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

        # ── 前回タップの予測を検証 (phash変化で判定) ──
        if state.last_action_pre_phash and state.last_prediction and cur_phash:
            STRATEGIC_ENGINE.verify_and_learn(
                state.last_action_pre_phash, cur_phash,
                state.last_prediction, state.last_prediction_desc,
                state.last_tap_text,
            )
            state.last_action_pre_phash = ""
            state.last_prediction = ""

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

        # ── UIアフォーダンス解析 (UNKNOWN or 30OCRごと) ──
        if scene == "UNKNOWN" or state.total_ocr_calls % 30 == 0:
            STRATEGIC_ENGINE.report_screen_affordances(analysis_path, ocr_results)

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
        # "HOME_REACHED" が返った時のみ停止 (QUEST_FROM_HOME 等の遷移中は続行)
        if action == "HOME_REACHED":
            logger.info("=" * 62)
            logger.info("  ホーム画面に到達しました! (チュートリアル完了)")
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

        # ── 9) メモリ解放 (SIGSEGV防止) ──
        # cv2 オブジェクトを毎イテレーション解放してメモリ断片化を防ぐ
        if i % 50 == 0:
            gc.collect()

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)


if __name__ == "__main__":
    main()
