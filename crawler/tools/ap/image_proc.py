"""
ap/image_proc.py — 画像処理・テンプレートマッチング・発光検出
"""
from __future__ import annotations

import cv2
import json
import logging
import re
import time
import numpy as np
from pathlib import Path
from typing import Optional

from tools.ap.constants import (
    ANALYSIS_W, ANALYSIS_H, BLACKOUT_BRIGHTNESS, ANALYSIS_PATH,
    _GLOW_CENTER_Y_OFFSET, _CHAR_HEAD_X1, _CHAR_HEAD_X2,
    _CHAR_HEAD_Y1, _CHAR_HEAD_Y2, _SINGLE_ONLY, _CRAWLER_ROOT,
    _DEBUG_SAVE_IMAGES,
)
from lc.utils import compute_phash, phash_distance
from tools.ap.device import tap_device, take_screenshot
from tools.ap.helpers import has_any

logger = logging.getLogger("auto_pilot")

# ─── イテレーション単位 imread キャッシュ ───
# 同一イテレーション内で同じファイルを複数回読むのを防ぐ。
# メインループ冒頭で clear_imread_cache() を呼ぶこと。
_IMREAD_CACHE: dict[tuple[str, int], np.ndarray] = {}


def imread_cached(path, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """cv2.imread のキャッシュ付きラッパー。同一パス+フラグなら再読み込みしない。"""
    key = (str(path), flags)
    cached = _IMREAD_CACHE.get(key)
    if cached is not None:
        return cached
    img = cv2.imread(str(path), flags)
    if img is not None:
        _IMREAD_CACHE[key] = img
    return img


def clear_imread_cache() -> None:
    """イテレーション開始時にキャッシュをクリア。"""
    _IMREAD_CACHE.clear()


def imread_analysis(path, flags: int = cv2.IMREAD_COLOR) -> Optional[np.ndarray]:
    """ANALYSIS_W x ANALYSIS_H にリサイズした画像を返す。

    Retina (2880x1440) 等の高解像度スクショでも ANALYSIS 座標系と一致する。
    imread_cached + リサイズのキャッシュ付き。
    """
    img = imread_cached(path, flags)
    if img is None:
        return None
    _h, _w = img.shape[:2]
    if (_w, _h) == (ANALYSIS_W, ANALYSIS_H):
        return img
    _key = (str(path), flags, "analysis")
    _cached = _IMREAD_CACHE.get(_key)
    if _cached is not None:
        return _cached
    resized = cv2.resize(img, (ANALYSIS_W, ANALYSIS_H))
    _IMREAD_CACHE[_key] = resized
    return resized


# ─── フッターナビ テンプレマッチ (ホーム画面検出) ─────────────────────
# footer_home_*.png を自動収集し、下部20%の ROI でマッチング。
# 同一グループ (例: footer_home_quest, footer_home_quest_2) は最高スコアを採用。
_FOOTER_HOME_TEMPLATES: dict[str, list[np.ndarray]] = {}  # group_name → [gray_template, ...]
_FOOTER_HOME_LOADED = False


def _load_footer_home_templates() -> None:
    """footer_home_*.png テンプレートを自動収集して _FOOTER_HOME_TEMPLATES に格納。"""
    global _FOOTER_HOME_LOADED
    if _FOOTER_HOME_LOADED:
        return
    _FOOTER_HOME_LOADED = True
    from tools.ap.constants import FOOTER_HOME_TEMPLATE_PREFIX
    _tpl_dir = _CRAWLER_ROOT / "assets" / "templates"
    for _p in sorted(_tpl_dir.glob(f"{FOOTER_HOME_TEMPLATE_PREFIX}*.png")):
        _name = _p.stem  # e.g. "footer_home_quest" or "footer_home_quest_2"
        # グループ名: 末尾の _N を除去 (footer_home_quest_2 → footer_home_quest)
        _parts = _name.split("_")
        if _parts[-1].isdigit():
            _group = "_".join(_parts[:-1])
        else:
            _group = _name
        _img = cv2.imread(str(_p), cv2.IMREAD_GRAYSCALE)
        if _img is not None:
            _FOOTER_HOME_TEMPLATES.setdefault(_group, []).append(_img)
    if _FOOTER_HOME_TEMPLATES:
        logger.info("[FooterHome] %d グループ, %d テンプレ読込",
                    len(_FOOTER_HOME_TEMPLATES),
                    sum(len(v) for v in _FOOTER_HOME_TEMPLATES.values()))


def count_home_nav_templates(img_path: Path, threshold: float = 0.75) -> int:
    """フッターナビ テンプレマッチでホーム画面のナビアイコン数を返す。

    下部20%の ROI で各テンプレをマッチし、閾値以上のグループ数を返す。
    同一グループに複数バリアントがある場合、最高スコアを採用。
    """
    _load_footer_home_templates()
    if not _FOOTER_HOME_TEMPLATES:
        return 0
    img = imread_analysis(img_path)
    if img is None:
        return 0
    _H, _W = img.shape[:2]
    # ROI: 下部25% (フッターナビ領域 — テンプレ高さのマージン確保)
    _y1 = int(_H * 0.75)
    _roi = img[_y1:_H, :]
    if _roi.size == 0:
        return 0
    _gray_roi = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)

    _matched = 0
    for _group, _templates in _FOOTER_HOME_TEMPLATES.items():
        _best_score = 0.0
        for _tpl in _templates:
            if _tpl.shape[0] > _gray_roi.shape[0] or _tpl.shape[1] > _gray_roi.shape[1]:
                continue
            _r = cv2.matchTemplate(_gray_roi, _tpl, cv2.TM_CCOEFF_NORMED)
            _, _mv, _, _ = cv2.minMaxLoc(_r)
            if _mv > _best_score:
                _best_score = _mv
        if _best_score >= threshold:
            _matched += 1
    return _matched


def detect_game_roi(img) -> tuple[int, int, int, int]:
    """
    スクリーンショットの黒帯（レターボックス）を検出し、純粋なゲーム描画領域を返す。

    アルゴリズム:
      1. グレースケール変換し、輝度 > 12 の「非黒」ピクセルを検出
      2. 列合計 / 行合計から黒帯の始終端を特定
      3. ROI サイズが全体の50%未満の場合はフォールバック (全画面)

    Returns: (roi_x, roi_y, roi_w, roi_h) in analysis image pixel coordinates
    """
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        _H, _W = img.shape[:2]
        # 列/行ごとの輝度ピクセル数
        col_bright = (np.array(gray, dtype=np.int32) > 12).sum(axis=0)
        row_bright = (np.array(gray, dtype=np.int32) > 12).sum(axis=1)
        # 各辺の黒帯を検出 (ノイズ耐性: min 3px 以上の明るい列/行)
        x0 = next((x for x in range(_W) if col_bright[x] > 3), 0)
        x1 = next((x for x in range(_W - 1, -1, -1) if col_bright[x] > 3), _W - 1)
        y0 = next((y for y in range(_H) if row_bright[y] > 3), 0)
        y1 = next((y for y in range(_H - 1, -1, -1) if row_bright[y] > 3), _H - 1)
        roi_w = x1 - x0 + 1
        roi_h = y1 - y0 + 1
        # 全黒画面 or ROI が異常に小さい場合は全画面を返す
        if roi_w < _W * 0.5 or roi_h < _H * 0.5:
            return 0, 0, _W, _H
        return x0, y0, roi_w, roi_h
    except Exception:
        return 0, 0, ANALYSIS_W, ANALYSIS_H


def roi_to_device(ax: int, ay: int, roi: tuple) -> tuple[int, int]:
    """
    解析座標（比率ベース・ANALYSIS_W×ANALYSIS_H 空間）を
    ROI オフセットを考慮した実機タップ座標に変換する。

    formula:
        real_x = (ax / ANALYSIS_W) * roi_w + roi_x
        real_y = (ay / ANALYSIS_H) * roi_h + roi_y

    使用場面:
      - ratio-based 座標 (int(ANALYSIS_W * 0.91) など) → 必ず本関数で変換
      - OCR / テンプレートマッチング座標 → 既に実機座標のため変換不要

    Args:
        ax, ay : 解析空間 (0..ANALYSIS_W, 0..ANALYSIS_H) の座標
        roi    : detect_game_roi() の戻り値 (roi_x, roi_y, roi_w, roi_h)
    Returns: (device_x, device_y)
    """
    roi_x, roi_y, roi_w, roi_h = roi
    return (
        int(ax / ANALYSIS_W * roi_w) + roi_x,
        int(ay / ANALYSIS_H * roi_h) + roi_y,
    )


def is_tutorial_walk_scene(img_path: Path) -> bool:
    """チュートリアル歩行シーン (白黒市松/階段背景) を検出。

    判定:
      1. 平均彩度が非常に低い (< 25) → ほぼモノクロ
      2. 明度の標準偏差が高い → 市松模様の高コントラスト
         彩度が低いほど市松模様の確信度が高いため、val_std 閾値を緩和:
         adjusted_threshold = max(55, 60 - (25 - mean_sat) * 0.5)
         (タイトル画面・利用規約・パーティ編成等の暗い画面は std < 56)
      3. 下半分の白黒バランスが均等 (>= 0.5) → チェック柄/階段の特徴
         動画暗転やADVシーンは白or黒に偏る (balance 0.2-0.3)
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mean_sat = float(hsv[:, :, 1].mean())
        if mean_sat >= 25:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        val_std = float(gray.std())
        # 彩度が低いほど閾値を緩和 (下限55)
        _std_threshold = max(55.0, 60.0 - (25.0 - mean_sat) * 0.5)
        if val_std < _std_threshold:
            return False
        # 下半分の白黒バランス: チェック柄は白黒が均等 (0.6-0.8)
        h = gray.shape[0]
        floor_gray = gray[h // 2:, :]
        _, floor_bin = cv2.threshold(floor_gray, 128, 255, cv2.THRESH_BINARY)
        floor_white = float(np.mean(floor_bin == 255))
        floor_balance = 1.0 - abs(floor_white - 0.5) * 2
        if floor_balance < 0.5:
            logger.debug("[WalkScene] sat=%.1f std=%.1f balance=%.2f < 0.5 → False",
                         mean_sat, val_std, floor_balance)
            return False
        logger.info("[DEBG][WalkScene] sat=%.1f std=%.1f >= th=%.1f balance=%.2f → True",
                    mean_sat, val_std, _std_threshold, floor_balance)
        return True
    except Exception:
        return False


def detect_gacha_orbs(img_path: Path, min_orbs: int = 1) -> bool:
    """ガチャ演出の光の玉を検出。

    暗い背景上に高輝度の円形ブロブが min_orbs 個以上あれば True。
    ガチャ演出と暗い動画シーンを区別するために使用。
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        h, w = gray.shape
        # 中央領域のみ (黒帯・SKIPボタン等を除外)
        y0, y1 = int(h * 0.15), int(h * 0.85)
        x0, x1 = int(w * 0.1), int(w * 0.9)
        roi = gray[y0:y1, x0:x1]
        # 高輝度閾値: 200以上の明るいピクセルを抽出
        _, binary = cv2.threshold(roi, 200, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        # 光の玉: 面積+円形度でフィルタ (実測: area≈9500-14000, circ≈0.76-0.90)
        _min_area = 50    # 小さすぎるノイズ除外
        _max_area = 15000  # 大きすぎる領域除外
        _min_circ = 0.60   # 円形度下限 (長方形UIを除外)
        orb_count = 0
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if _min_area <= area <= _max_area:
                _peri = cv2.arcLength(cnt, True)
                _circ = (4 * np.pi * area / (_peri * _peri)) if _peri > 0 else 0
                if _circ >= _min_circ:
                    orb_count += 1
        if orb_count >= min_orbs:
            logger.debug("[GACHA_ORBS] 光の玉 %d 個検出 (閾値=%d)", orb_count, min_orbs)
        return orb_count >= min_orbs
    except Exception:
        return False


def is_gacha_scene(img_path: Path) -> bool:
    """ガチャ演出画面を判定: SKIP ボタン + 暗い背景 + 光の玉。

    3条件すべてを満たす場合のみ True。
    動画シーン（SKIP+暗背景のみ）との誤判定を防止する。
    """
    if not detect_movie_skip_button(img_path):
        return False
    img = imread_cached(img_path)
    if img is None:
        return False
    brightness = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean())
    if brightness >= 80:
        return False
    if not detect_gacha_orbs(img_path):
        return False
    logger.info("[GACHA_DETECT] SKIP+暗背景(%.0f)+光の玉 → ガチャ演出確定", brightness)
    return True


def get_screen_p90(img_path: Path) -> float:
    """中央60%領域の 90th percentile 輝度を返す。取得失敗時は 255.0。"""
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            gray = np.array(img.convert("L"))
            h, w = gray.shape
            y0, y1 = int(h * 0.2), int(h * 0.8)
            x0, x1 = int(w * 0.2), int(w * 0.8)
            return float(np.percentile(gray[y0:y1, x0:x1], 90))
    except Exception:
        return 255.0


def is_dark_screen(img_path: Path) -> bool:
    """暗転判定 — 中央60%領域の 90th percentile 輝度で判定。

    黒帯除外のため中央領域のみ使用。平均値ではなく 90th percentile を
    使うことで、暗い背景+UIの画面 (p90≈58) と真の暗転 (p90≈2) を区別する。

    完全暗転 (p90 <= 5) → True
    暗背景+テキスト (p90 = 6〜BLACKOUT_BRIGHTNESS) → False (OCR で処理すべき)
    """
    _p90 = get_screen_p90(img_path)
    _is_dark = _p90 <= 5
    if _p90 <= BLACKOUT_BRIGHTNESS:
        logger.info("[DEBG][DarkScreen] p90=%.1f → %s (threshold=5/blackout=%d)",
                    _p90, "暗転" if _is_dark else "暗背景+テキスト→OCRへ",
                    BLACKOUT_BRIGHTNESS)
    return _is_dark


def prepare_analysis_image(img_path: Path, actual_w: int, actual_h: int) -> Path:
    # actual_w/h (デバイス解像度) ではなく画像ファイルの実サイズで判定する。
    # scrcpy キャプチャは --max-size でリサイズされるためデバイス解像度と異なる。
    try:
        from PIL import Image
        with Image.open(img_path) as _probe:
            actual_w, actual_h = _probe.size
    except Exception:
        pass  # 読めない場合は引数値をフォールバック
    needs_transform = (actual_w < actual_h) or \
        ((actual_w, actual_h) != (ANALYSIS_W, ANALYSIS_H) and
         (actual_h, actual_w) != (ANALYSIS_W, ANALYSIS_H))
    if not needs_transform:
        return img_path
    analysis_path = ANALYSIS_PATH
    try:
        from PIL import Image
        img = Image.open(img_path)
        if img.width < img.height:
            img = img.rotate(90, expand=True)
        if img.size != (ANALYSIS_W, ANALYSIS_H):
            img = img.resize((ANALYSIS_W, ANALYSIS_H), Image.LANCZOS)
        img.save(analysis_path)
    except Exception:
        # PIL が破損 PNG で SyntaxError を投げる場合 cv2 にフォールバック
        _cv_img = imread_cached(img_path)
        if _cv_img is None:
            return img_path  # 完全に読めない → 元画像をそのまま返す
        h, w = _cv_img.shape[:2]
        if w < h:
            _cv_img = cv2.rotate(_cv_img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        if (_cv_img.shape[1], _cv_img.shape[0]) != (ANALYSIS_W, ANALYSIS_H):
            _cv_img = cv2.resize(_cv_img, (ANALYSIS_W, ANALYSIS_H), interpolation=cv2.INTER_LANCZOS4)
        cv2.imwrite(str(analysis_path), _cv_img)
    return analysis_path


def detect_white_hand_pointer(
    img_path: Path, threshold: float = 0.85
) -> Optional[tuple[int, int, float, str]]:
    """
    指アイコン（tutorial_hand_pointer）をテンプレートマッチングで検出。
    Returns: (cx, cy, score, direction) or None
        direction: "down" (固定)
    """
    try:
        img = imread_cached(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        _m = ASSET_MANAGER.match_single("tutorial_hand_pointer", img_path)
        if _m and _m[2] >= threshold:
            logger.info("[WHITE_HAND] 指アイコン検出 (%d,%d) score=%.3f",
                        _m[0], _m[1], _m[2])
            return (_m[0], _m[1], _m[2], "down")
        return None
    except Exception as e:
        logger.debug("detect_white_hand_pointer error: %s", e)
        return None


def create_finger_mask_image(img_path: Path, cx: int, cy: int, half: int = 175) -> Path:
    """
    指アイコン周囲 350×350px (half=175) 以外を純黒に塗りつぶした一時画像を生成して返す。
    Hard Masking 2.0: 右側スキルボタン等の誤検出を物理的に排除。
    失敗した場合は元の img_path を返す。
    """
    try:
        _img_hm = imread_cached(img_path)
        if _img_hm is None:
            return img_path
        _H_hm, _W_hm = _img_hm.shape[:2]
        _masked = np.zeros_like(_img_hm)
        _x1 = max(0, cx - half)
        _x2 = min(_W_hm, cx + half)
        _y1 = max(0, cy - half)
        _y2 = min(_H_hm, cy + half)
        _masked[_y1:_y2, _x1:_x2] = _img_hm[_y1:_y2, _x1:_x2]
        _tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        cv2.imwrite(_tmp.name, _masked)
        _tmp.close()
        return Path(_tmp.name)
    except Exception as _e_hm:
        logger.debug("create_finger_mask_image error: %s", _e_hm)
        return img_path


def detect_guide_glow(img_path: Path, W: int, H: int,
                      footer_ratio: float = 0.30,
                      min_area: int = 800) -> list[dict]:
    """
    チュートリアルガイドの「発光（モヤ）エフェクト」をフッター領域で検知する。
    フッター = 画面下部 footer_ratio (デフォルト30%) に限定。
    白〜金色の高輝度ブロブを検出し、左側(left)/右側(right)を分類して返す。
    返値: [{"cx":int,"cy":int,"area":float,"side":"left"|"right",
            "bx":int,"by":int,"bw":int,"bh":int}, ...] 面積降順
    """
    try:
        _img_gw = imread_cached(img_path)
        if _img_gw is None:
            return []
        _Hg, _Wg = _img_gw.shape[:2]
        _footer_y = int(_Hg * (1.0 - footer_ratio))
        _footer = _img_gw[_footer_y:_Hg, 0:_Wg]
        if _footer.size == 0:
            return []
        _hsv_gw = cv2.cvtColor(_footer, cv2.COLOR_BGR2HSV)
        # 白発光: 低彩度・高輝度 (白いハイライト/ハロー)
        _mask_w = cv2.inRange(_hsv_gw,
                              np.array([0, 0, 215], dtype=np.uint8),
                              np.array([180, 65, 255], dtype=np.uint8))
        # 金発光: 金/黄色系・高輝度 (ゴールドハイライト)
        _mask_g = cv2.inRange(_hsv_gw,
                              np.array([15, 50, 195], dtype=np.uint8),
                              np.array([50, 210, 255], dtype=np.uint8))
        _mask_gw = cv2.bitwise_or(_mask_w, _mask_g)
        # ノイズ除去: 小さいスポット・HPバー等の細線を排除
        _kern = np.ones((4, 4), np.uint8)
        _mask_gw = cv2.morphologyEx(_mask_gw, cv2.MORPH_OPEN, _kern)
        _cnts_gw, _ = cv2.findContours(_mask_gw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        _glows = []
        for _c_gw in _cnts_gw:
            _a_gw = cv2.contourArea(_c_gw)
            if _a_gw < min_area:
                continue
            # HPバー等の細長いブロブを除外: アスペクト比 > 8 はバー状
            _bx_gw, _by_gw, _bw_gw, _bh_gw = cv2.boundingRect(_c_gw)
            _asp_gw = _bw_gw / _bh_gw if _bh_gw > 0 else 1.0
            if _asp_gw > 8.0 or _asp_gw < 0.12:
                continue
            _M_gw = cv2.moments(_c_gw)
            if _M_gw["m00"] <= 0:
                continue
            _cx_gw = int(_M_gw["m10"] / _M_gw["m00"])
            _cy_gw = int(_M_gw["m01"] / _M_gw["m00"]) + _footer_y
            _by_abs = _by_gw + _footer_y
            _side = "left" if _cx_gw < _Wg // 2 else "right"
            _glows.append({
                "cx": _cx_gw, "cy": _cy_gw, "area": _a_gw, "side": _side,
                "bx": _bx_gw, "by": _by_abs, "bw": _bw_gw, "bh": _bh_gw,
            })
        return sorted(_glows, key=lambda g: g["area"], reverse=True)
    except Exception as _e_gw:
        logger.debug("detect_guide_glow error: %s", _e_gw)
        return []


def _run_battle_glow_sm(
    analysis_path: Path,
    W: int, H: int,
    state: "PilotState",
    ocr: list,
    tag: str = "GLOW_SM",
) -> Optional[tuple]:
    """
    バトル発光ステートマシン (統一版)。#0-PRE と #1-pre の共通ロジック。

    P1: 左キャラ発光 (character_selected=False) → タップ → character_selected=True
    P2: 右スキル発光 (character_selected=True)  → タップ → character_selected=False
    P3: 発光なし + character_selected → 通常攻撃 OCR フォールバック

    Returns: (action, wait_sec) or None (発光なし/バトルでない)
    """
    glows = detect_guide_glow(analysis_path, W, H, footer_ratio=0.30)
    left = [g for g in glows if g["side"] == "left"]
    right = [g for g in glows if g["side"] == "right"]
    if glows:
        logger.info("[%s] フッター発光: 左%d個(最大%.0f) 右%d個(最大%.0f)", tag,
                    len(left), left[0]["area"] if left else 0,
                    len(right), right[0]["area"] if right else 0)

    # P1: 左キャラ発光 (キャラ未選択)
    if not state.character_selected and left:
        g = max(left, key=lambda g: g["area"])
        # bbox上端 + 高さ2/3 = ボタン視覚中心 (centroidはハロに引かれ上にずれる)
        gx = g["cx"]
        gy = max(1, g["by"] + g["bh"] * 2 // 3)
        logger.info("[%s P1] 左キャラ発光 centroid(%d,%d) bbox_y=%d+%d → tap(%d,%d)",
                    tag, g["cx"], g["cy"], g["by"], g["bh"], gx, gy)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR", rapid=True)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR")  # ダブルタップ
        state.character_selected = True
        state.char_just_selected = True
        state.finger_detections += 1
        state.phash_moving_count = 0
        return "GLOW_LEFT_CHAR", 0.3

    # P2: 右スキル発光 (キャラ選択済み)
    if state.character_selected and (right or True):
        # P2-a: テンプレートで battle_skill / battle_normal_attack を探す (精度最優先)
        _p2_tmpl_hit = False
        for _btn in ("battle_skill", "battle_normal_attack"):
            _bm = ASSET_MANAGER.match_single(_btn, analysis_path)
            if _bm and _bm[2] >= 0.60:
                gx, gy = _bm[0], _bm[1]
                logger.info("[%s P2] テンプレ %s (%.2f) → tap(%d,%d)",
                            tag, _btn, _bm[2], gx, gy)
                tap_device(gx, gy, state, f"GLOW_RIGHT_{_btn.upper()}")
                state.character_selected = False
                state.char_just_selected = False
                state.finger_detections += 1
                state.phash_moving_count = 0
                _p2_tmpl_hit = True
                return f"GLOW_RIGHT_{_btn.upper()}", 0.3
        # P2-b: テンプレ未検出 → glow フォールバック
        if not _p2_tmpl_hit and right:
            g = max(right, key=lambda g: g["area"])
            gx = g["cx"]
            gy = max(1, g["by"] + g["bh"] * 2 // 3)
            logger.info("[%s P2] 右発光 centroid(%d,%d) bbox_y=%d+%d → tap(%d,%d)",
                        tag, g["cx"], g["cy"], g["by"], g["bh"], gx, gy)
            tap_device(gx, gy, state, "GLOW_RIGHT_SKILL")
            state.character_selected = False
            state.char_just_selected = False
            state.finger_detections += 1
            # バトルアニメーションで MOVIE 誤昇格しないようリセット
            state.phash_moving_count = 0
            return "GLOW_RIGHT_SKILL", 0.3

    # P3: キャラ選択済み + 発光なし → 通常攻撃 OCR フォールバック
    if state.character_selected and not right:
        na = has_any(ocr, ["通常攻撃", "单体攻撃", "単体攻撃"])
        if na:
            nx, ny = na["center"]
            if nx > W * 0.5 and ny > H * 0.5:
                # OCRテキスト中心 ≈ ボタン視覚中心 (オフセット不要)
                logger.info("[%s P3] 攻撃ボタンOCR '%s'(%d,%d) → tap", tag, na["text"], nx, ny)
                tap_device(nx, ny, state, "NORMATK_TAP")
                state.character_selected = False
                state.char_just_selected = False
                # バトルアニメーションで MOVIE 誤昇格しないようリセット
                state.phash_moving_count = 0
                return "NORMATK_TAP", 1.0

    return None


def detect_active_battle_char(
    img_path: Path,
    analysis_w: int = 1520,
    analysis_h: int = 720,
) -> Optional[tuple[int, int, float]]:
    """
    【永続バトルルール】バトル画面で選択待ちモヤ（赤/ピンク発光ハロー）が
    あるキャラクターを検出する。

    アクティブキャラの特徴:
      - 赤/ピンクの発光ハロー（非アクティブにはない）
      - 肖像周辺の全体明度が著しく高い

    方式:
      1. フッター左領域（キャラ肖像エリア）を等幅カラムに分割
      2. 各カラムの「暖色発光ピクセル数」と「平均明度」を計算
      3. 中央値比で突出しているカラムをアクティブキャラと判定

    Returns: (cx, cy, brightness_ratio) or None
    """
    try:
        _img = imread_cached(img_path)
        if _img is None:
            return None
        _h, _w = _img.shape[:2]

        # キャラ肖像エリア: 画面下部25%, 左側 x=100~760
        _y0 = int(_h * 0.75)
        _x0 = 100
        _x1 = min(760, _w)
        _footer = _img[_y0:_h, _x0:_x1]
        if _footer.size == 0:
            return None

        _hsv = cv2.cvtColor(_footer, cv2.COLOR_BGR2HSV)
        _fh, _fw = _footer.shape[:2]

        # 暖色発光マスク: 赤/ピンク/マゼンタ (H:0-20 or 155-180, S>=35, V>=100)
        _m1 = cv2.inRange(_hsv,
                          np.array([0, 35, 100], dtype=np.uint8),
                          np.array([20, 255, 255], dtype=np.uint8))
        _m2 = cv2.inRange(_hsv,
                          np.array([155, 35, 100], dtype=np.uint8),
                          np.array([180, 255, 255], dtype=np.uint8))
        _warm_mask = cv2.bitwise_or(_m1, _m2)

        # 5カラム分割（キャラ5人想定、各カラム ~132px）
        _n_cols = 5
        _col_w = _fw // _n_cols
        _stats = []  # (warm_count, avg_brightness, col_center_x, col_idx)

        for _ci in range(_n_cols):
            _cx0 = _ci * _col_w
            _cx1 = (_ci + 1) * _col_w
            _col_warm = _warm_mask[:, _cx0:_cx1]
            _col_v = _hsv[:, _cx0:_cx1, 2]  # V channel
            _warm_count = int(cv2.countNonZero(_col_warm))
            _avg_v = float(np.mean(_col_v))
            _center_x = _x0 + _cx0 + _col_w // 2
            _stats.append((_warm_count, _avg_v, _center_x, _ci))

        if not _stats:
            return None

        # 中央値の計算
        _warm_counts = [s[0] for s in _stats]
        _avg_vs = [s[1] for s in _stats]
        _med_warm = float(np.median(_warm_counts))
        _med_v = float(np.median(_avg_vs))

        # アクティブキャラ判定: 暖色ピクセルが中央値の3倍以上 OR 明度が中央値の1.4倍以上
        _best = None
        for _wc, _av, _ccx, _ci in _stats:
            _warm_ratio = _wc / max(_med_warm, 1.0)
            _v_ratio = _av / max(_med_v, 1.0)
            _is_active = (_warm_ratio >= 3.0) or (_v_ratio >= 1.4 and _wc > _med_warm)
            if _is_active:
                _score = _warm_ratio + _v_ratio
                if _best is None or _score > _best[3]:
                    _cy = _y0 + _fh // 2
                    _best = (_ccx, _cy, _v_ratio, _score)

        if _best:
            logger.info(
                "[ACTIVE_CHAR] 選択待ちキャラ検出 (%d,%d) brightness_ratio=%.2f",
                _best[0], _best[1], _best[2]
            )
            return (_best[0], _best[1], _best[2])

        return None

    except Exception as _e_abc:
        logger.debug("detect_active_battle_char error: %s", _e_abc)
        return None



# ─── ADV ツールバー: 5個別アイコン名 ──────────────────────────────
_ADV_TOOLBAR_ICON_NAMES = (
    "icon_showhide", "icon_log", "icon_auto",
    "icon_ff", "icon_skip",
)

# ─── ADV シーン統一検出 ────────────────────────────────────────────

_ADV_NAME_LINE_RE = re.compile(r'[◇◆✦♦＋\+].{1,20}[◇◆✦♦＋\+]')


class AdvSceneResult:
    """ADVシーン検出結果。"""
    __slots__ = (
        "is_adv", "confidence", "toolbar_score", "toolbar_pos",
        "next_btn_score", "next_btn_pos", "has_name_line",
        "has_dialogue", "has_letterbox", "matched_count",
    )

    def __init__(
        self,
        is_adv: bool = False,
        confidence: float = 0.0,
        toolbar_score: float = 0.0,
        toolbar_pos: Optional[tuple] = None,
        next_btn_score: float = 0.0,
        next_btn_pos: Optional[tuple] = None,
        has_name_line: bool = False,
        has_dialogue: bool = False,
        has_letterbox: bool = False,
        matched_count: int = 0,
    ):
        self.is_adv = is_adv
        self.confidence = confidence
        self.toolbar_score = toolbar_score
        self.toolbar_pos = toolbar_pos
        self.next_btn_score = next_btn_score
        self.next_btn_pos = next_btn_pos
        self.has_name_line = has_name_line
        self.has_dialogue = has_dialogue
        self.has_letterbox = has_letterbox
        self.matched_count = matched_count


def detect_adv_scene(img_path: Path, ocr_items=None, roi=None,
                     icon_threshold: float = 0.65) -> AdvSceneResult:
    """ADVシーンを統一的に検出。5個別アイコン全マッチ + ↓ボタン + 補助信号。

    判定ロジック:
      1. ツールバー5アイコン (menu/log/AUTO/>>/>) 個別テンプレートマッチ
         → 全アイコンが icon_threshold 以上で toolbar_score = 最小スコア
      2. ↓ボタン テンプレートマッチ → next_btn_score
      3. キャラ名行 (◇name◇) → has_name_line
      4. セリフテキスト (下部35%にかな4文字以上) → has_dialogue
      5. レターボックス (roi[0] >= 60) → has_letterbox
      6. is_adv = 5アイコン全マッチ (全スコア >= icon_threshold)
    """
    result = AdvSceneResult()

    # --- 1. ツールバー5アイコン個別マッチ (上部15%のみ) ---
    # ADV ツールバーは右上にあるため、下部のバトル UI 等への偽マッチを防ぐ
    _adv_roi = (0, 0, ANALYSIS_W, int(ANALYSIS_H * 0.15))
    _icon_scores: list[float] = []
    _icon_matches: list[Optional[tuple]] = []  # (cx, cy, score) or None
    for _name in _ADV_TOOLBAR_ICON_NAMES:
        try:
            _m = ASSET_MANAGER.match_single(_name, img_path, roi=_adv_roi)
            _icon_scores.append(_m[2] if _m else 0.0)
            _icon_matches.append(_m)
        except Exception:
            _icon_scores.append(0.0)
            _icon_matches.append(None)

    _matched_count = sum(1 for s in _icon_scores if s >= icon_threshold)
    # AUTO アイコン (index=2) スコア取得
    _auto_score = _icon_scores[2] if len(_icon_scores) > 2 else 0.0
    _has_auto = _auto_score >= 0.50
    # ADV専用アイコン: menu(0)/log(1)/skip(4) はバトルに存在しない
    _has_adv_only = any(
        _icon_scores[i] >= 0.40
        for i in (0, 1, 4) if i < len(_icon_scores)
    )
    # ↓ボタン検出
    _has_advance_icon = False
    _adv_btn = ASSET_MANAGER.match_single("next_btn", img_path,
                roi=(int(ANALYSIS_W * 0.80), int(ANALYSIS_H * 0.75),
                     int(ANALYSIS_W * 0.20), int(ANALYSIS_H * 0.25)))
    _has_advance_icon = _adv_btn is not None
    # セリフテキスト: 下部35%にかな含む4文字以上テキスト
    _has_dialogue_early = False
    if ocr_items:
        for item in ocr_items:
            _oc = item.get("center", (0, 0))
            if _oc[1] > ANALYSIS_H * 0.65:
                txt = item.get("text", "")
                if len(txt) >= 4 and any(0x3041 <= ord(c) <= 0x309F for c in txt):
                    _has_dialogue_early = True
                    break
    # 判定: AUTO + ↓ボタン + ADV専用アイコン + セリフテキスト
    _all_matched = (_has_auto and _has_advance_icon and _has_adv_only and _has_dialogue_early)
    result.toolbar_score = min(_icon_scores) if _icon_scores else 0.0
    if _all_matched and _icon_scores:
        # toolbar_pos = AUTO アイコンの位置 (3番目)
        try:
            _auto_m = ASSET_MANAGER.match_single("icon_auto", img_path)
            if _auto_m:
                result.toolbar_pos = (_auto_m[0], _auto_m[1])
        except Exception:
            pass

    # --- 2. ↓ボタン ---
    try:
        next_match = ASSET_MANAGER.match_single("next_btn", img_path)
        if next_match:
            result.next_btn_score = next_match[2]
            result.next_btn_pos = (next_match[0], next_match[1])
    except Exception:
        pass

    # --- 3〜5. 補助信号 ---
    if ocr_items:
        # 3. キャラ名行: 下部40%で ◇name◇ パターン
        _name_items = [r for r in ocr_items
                       if r.get("center", (0, 0))[1] > ANALYSIS_H * 0.60]
        for item in _name_items:
            if _ADV_NAME_LINE_RE.search(item.get("text", "")):
                result.has_name_line = True
                break
        # 4. セリフ: 下部35%にかな含む4文字以上テキスト
        _dialogue_items = [r for r in ocr_items
                           if r.get("center", (0, 0))[1] > ANALYSIS_H * 0.65]
        for item in _dialogue_items:
            txt = item.get("text", "")
            if len(txt) >= 4 and any(0x3041 <= ord(c) <= 0x309F for c in txt):
                result.has_dialogue = True
                break

    # 5. レターボックス
    if roi and len(roi) >= 1 and roi[0] >= 60:
        result.has_letterbox = True

    # --- 6. 信頼度スコア ---
    aux_score = sum([
        0.05 if result.has_name_line else 0.0,
        0.05 if result.has_dialogue else 0.0,
        0.05 if result.has_letterbox else 0.0,
    ])
    result.confidence = min(1.0,
                            result.toolbar_score * 0.6
                            + result.next_btn_score * 0.25
                            + aux_score)

    # --- 7. 判定 ---
    result.is_adv = _all_matched
    result.matched_count = _matched_count

    if result.is_adv:
        logger.debug("[ADV_SCENE] 検出: icons=[%s] matched=%d/5 min=%.3f next_btn=%.3f "
                     "name=%s dial=%s lbox=%s",
                     ",".join(f"{s:.2f}" for s in _icon_scores),
                     _matched_count, result.toolbar_score, result.next_btn_score,
                     result.has_name_line, result.has_dialogue,
                     result.has_letterbox)
    else:
        logger.debug("[ADV_SCENE] 未検出: icons=[%s] matched=%d/5 min=%.3f",
                     ",".join(f"{s:.2f}" for s in _icon_scores),
                     _matched_count, result.toolbar_score)
    return result


def detect_adv_toolbar_buttons(img_path: Path, threshold: float = 0.65) -> bool:
    """5アイコン全マッチでADV判定 (後方互換)。detect_adv_scene に委譲。"""
    return detect_adv_scene(img_path, icon_threshold=threshold).is_adv



def detect_movie_skip_button(img_path: Path) -> Optional[tuple]:
    """
    動画シーンの⏭スキップボタン（右上の金色円形アイコン）を検出。
    返り値: (cx, cy, source) or None
      source: "adv_icon" = ADVツールバーの⏭アイコン
              "movie_text" = 動画固有のSKIPテキスト
    """
    try:
        _img = imread_analysis(img_path)
        if _img is None:
            return None
        _H, _W = _img.shape[:2]
        # ROI: 右上コーナー (88%~100% x, 0~12% y)
        _x1 = int(_W * 0.88)
        _y2 = int(_H * 0.12)
        if _y2 < 5 or _W - _x1 < 5:
            return None
        _roi = _img[0:_y2, _x1:_W]
        _skip_roi = (int(ANALYSIS_W * 0.85), 0,
                     int(ANALYSIS_W * 0.15), int(ANALYSIS_H * 0.15))
        # ── プライマリ: テンプレートマッチング (icon_skip) ──
        # HSV はリサイズ後のアイコンサイズに依存するがテンプレートは安定
        try:
            _skip_m = ASSET_MANAGER.match_single("icon_skip", img_path, roi=_skip_roi)
            if _skip_m and _skip_m[2] >= 0.70:
                logger.debug("[MOVIE_SKIP_BTN] テンプレート検出 (%d,%d) score=%.2f",
                             _skip_m[0], _skip_m[1], _skip_m[2])
                return (_skip_m[0], _skip_m[1], "adv_icon")
        except Exception:
            pass

        # ── セカンダリ: 「SKIP」テキストボタン (動画シーン右上) ──
        # ⏭アイコンとは別UIだがどちらもスキップ用
        try:
            _skip_text_m = ASSET_MANAGER.match_single(
                "movie_skip", img_path, roi=_skip_roi)
            if _skip_text_m and _skip_text_m[2] >= 0.70:
                logger.debug("[MOVIE_SKIP_BTN] SKIPテキスト検出 (%d,%d) score=%.2f",
                             _skip_text_m[0], _skip_text_m[1], _skip_text_m[2])
                return (_skip_text_m[0], _skip_text_m[1], "movie_text")
        except Exception:
            pass

        return None
    except Exception:
        return None


class MovieSceneResult:
    """動画シーン判定結果。"""
    __slots__ = ("is_movie", "confidence", "has_skip_btn", "skip_btn_pos")

    def __init__(self, is_movie=False, confidence=0.0,
                 has_skip_btn=False, skip_btn_pos=None):
        self.is_movie = is_movie
        self.confidence = confidence
        self.has_skip_btn = has_skip_btn
        self.skip_btn_pos = skip_btn_pos

    def __repr__(self):
        return (f"MovieSceneResult(is_movie={self.is_movie}, "
                f"conf={self.confidence:.2f}, skip={self.has_skip_btn})")


# バトルキーワード (動画即棄却用)
_MOVIE_REJECT_BATTLE_KWS = frozenset([
    "通常攻撃", "単体攻撃", "单体攻撃", "全体攻撃",
    "必殺技", "BREAK", "WAVE", "Turn",
])

# UI テキスト (減点用)
_MOVIE_UI_PENALTY_KWS = (
    "利用規約", "同意", "規約", "プライバシー", "ダウンロード",
    "Download", "OK", "はい", "キャンセル", "設定", "お知らせ",
    "クエスト", "ショップ", "ガチャ", "編成",
    "ボックス", "プレイヤー", "交換",
)


def detect_movie_scene(img_path, adv_result=None, ocr_texts=None,
                       phash_dist=0, phash_moving_count=0):
    """動画シーン判定 (重み付きスコアリング)。

    正の信号:
      ⏭ スキップボタン検出          +0.40
      ADV ツールバーなし             +0.25
      OCR テキスト少ない (<=1件)     +0.15
      phash 変化大 (アニメーション)  +0.10
      phash 連続変化 (>=3回連続)     +0.30  ← 動画の最も確実な証拠

    負の信号 (即棄却):
      ADV ツールバーあり             → 即 False
      バトルキーワード               → 即 False

    減点:
      UI キーワード (OK, ダウンロード等) → -0.30

    閾値: confidence >= 0.50 → is_movie = True
    """
    texts = ocr_texts or []
    joined = " ".join(texts) if texts else ""

    # ── ポップアップ即棄却: ページドット + 背景ぼかし → 動画ではない ──
    _popup_dots = False
    _popup_blur = False
    if img_path:
        _pi = imread_cached(img_path)
        if _pi is not None:
            _popup_dots = count_page_dots(_pi, _pi.shape[0], _pi.shape[1]) >= 1
            _popup_blur = detect_background_blur(_pi, _pi.shape[0], _pi.shape[1])
        if _popup_dots and _popup_blur:
            return MovieSceneResult()

    # ── ⏭ スキップボタン検出 ──
    skip_btn = detect_movie_skip_button(img_path) if img_path else None
    has_skip = skip_btn is not None
    # icon_skip がマッチ → ADV ツールバーの存在を示す直接証拠
    # movie_skip がマッチ → 動画固有の SKIP ボタン
    _skip_source = skip_btn[2] if skip_btn else None

    # ── ADV 証拠チェック (⏭有無に関わらず共通) ──
    # ADV の構造的特徴 (MOVIE にはどれもない):
    #   1. ↓送りボタン (右下) — セリフ送り可能時に表示
    #   2. ADV ツールバー (右上5アイコン: menu,log,AUTO,>>,>|)
    #   3. icon_skip マッチ — ADV ツールバーのアイコンそのもの
    from tools.ap.constants import ADV_NEXT_BTN_ROI
    _adv_btn_movie = ASSET_MANAGER.match_single("next_btn", img_path,
                    roi=ADV_NEXT_BTN_ROI) if img_path else None
    _has_adv_advance = _adv_btn_movie is not None
    _has_auto_icon = False
    if img_path:
        try:
            from tools.ap.constants import ADV_TOOLBAR_ROI
            _auto_roi_chk = ADV_TOOLBAR_ROI
            _auto_chk = ASSET_MANAGER.match_single(
                "icon_auto", img_path, roi=_auto_roi_chk)
            _has_auto_icon = _auto_chk is not None and _auto_chk[2] >= 0.70
        except Exception:
            pass

    # ADV 証拠の評価
    # ↓ボタン: 最も確実 (MOVIE には絶対にない)
    # ADVツールバー: 確実 (5アイコン検出、MOVIE には存在しない)
    # icon_skip 単独は ADV 確定にしない (CLAUDE.md: ⏭+ADV証拠なし→動画確定)
    _adv_evidence_strong = None
    if _has_adv_advance:
        _adv_evidence_strong = "↓ボタン"
    elif adv_result is not None and adv_result.is_adv:
        _adv_evidence_strong = "ADVツールバー"

    # ── ADV 証拠による即棄却 ──
    if _adv_evidence_strong:
        logger.info("[MOVIE_SCENE] %s → ADV確定, MOVIE棄却", _adv_evidence_strong)
        return MovieSceneResult()
    if has_skip:
        # ⏭ボタンあり + ADV証拠(↓/ツールバー)なし → 動画確定 (CLAUDE.md §0)
        logger.info("[MOVIE_SCENE] ⏭検出(%s) + ADV証拠なし → MOVIE確定", _skip_source)
    else:
        # ⏭ なし: phash 連続変化があれば動画の可能性を残す
        # AUTO 単独でも ADV 判定 OK (⏭なし時)
        if _has_auto_icon:
            logger.debug("[MOVIE_SCENE] AUTOボタン → ADV確定, MOVIE棄却")
            return MovieSceneResult()

        # 即棄却: バトルテンプレート (通常攻撃/スキル/必殺)
        from tools.ap.constants import BATTLE_BTN_ROI
        _battle_roi_chk = BATTLE_BTN_ROI
        for _b_name in ("battle_normal_attack", "battle_skill", "battle_special"):
            _b_m = ASSET_MANAGER.match_single(_b_name, img_path, roi=_battle_roi_chk)
            if _b_m and _b_m[2] >= 0.50:
                logger.info("[MOVIE_SCENE] バトルテンプレ %s(%.2f) → MOVIE棄却", _b_name, _b_m[2])
                return MovieSceneResult()

        # 即棄却: バトルキーワード
        if any(kw in joined for kw in _MOVIE_REJECT_BATTLE_KWS):
            return MovieSceneResult()

        # 即棄却: お知らせポップアップ
        if "今日は表示し" in joined:
            logger.info("[MOVIE_SCENE] 「今日は表示しない」→ MOVIE棄却 (お知らせ)")
            return MovieSceneResult()

        # 即棄却: ダイアログ枠 (×ボタン / ゴールド枠) が視覚検出された場合
        if img_path:
            try:
                _dlg = detect_dialog_frame_and_nav(img_path)
                if _dlg is not None:
                    logger.debug("[MOVIE_SCENE] ダイアログ枠検出 (%s) → MOVIE棄却",
                                 _dlg[0])
                    return MovieSceneResult()
            except Exception:
                pass
            # 即棄却: ページドット + 背景ぼかし or 背景ぼかし単独 → ポップアップ
            # (ドット/ぼかしは冒頭で計算済みのキャッシュを再利用)
            if _popup_dots and _popup_blur:
                logger.debug("[MOVIE_SCENE] ドット+背景ぼかし → MOVIE棄却 (ポップアップ)")
                return MovieSceneResult()
            # NOTE: blur 単独での棄却は廃止。動画字幕の黒帯が blur と誤判定される。
            # ポップアップ判定は dots + blur の組み合わせのみで行う。

    score = 0.0
    # ⏭ スキップボタン (既に上で検出済み)
    if has_skip:
        score += 0.40

    # ADV ツールバーなし
    if adv_result is None or not adv_result.is_adv:
        score += 0.25

    # OCR テキスト少ない
    if len(texts) <= 1:
        score += 0.15
    elif len(texts) <= 3:
        score += 0.05  # 字幕程度

    # phash 変化大 (アニメーション)
    if phash_dist >= 8:
        score += 0.10

    # phash 連続変化 (動画の最も確実な証拠)
    # フレームが3回以上連続で変化 → 動画再生中の可能性が高い
    _PHASH_MOVING_MIN = 3
    if phash_moving_count >= _PHASH_MOVING_MIN:
        score += 0.30

    # 減点: UI テキスト
    if any(kw in joined for kw in _MOVIE_UI_PENALTY_KWS):
        score -= 0.30

    is_movie = score >= 0.50
    logger.debug("[MOVIE_SCENE] score=%.2f skip=%s texts=%d → %s",
                 score, has_skip, len(texts), is_movie)
    return MovieSceneResult(
        is_movie=is_movie, confidence=score,
        has_skip_btn=has_skip, skip_btn_pos=skip_btn,
    )


def detect_mini_conversation(img_path: Path, ocr_items=None,
                             min_bubble_area: int = 3000,
                             upper_ratio: float = 0.35,
                             skip_ocr_verify: bool = False):
    """
    ミニ会話シーン（上部の吹き出し）を検出しアクティブ話者の中心座標を返す。

    検出方法: 固定位置 (Y=103, H=88) に角丸矩形 (R=46) マスクを配置し、
    マスク内のベージュピクセル割合で吹き出しの有無を判定。
    左端固定 / 右端固定（左右反転）の2箇所をチェック。

    Returns: (cx, cy, "left"|"right") or None
    """
    # 吹き出し位置・サイズ (解析解像度からの比率で算出)
    _BUBBLE_Y = int(ANALYSIS_H * 0.143)     # 吹き出し上端 Y (≈103)
    _BUBBLE_H = int(ANALYSIS_H * 0.122)     # 吹き出し高さ (≈88)
    _BUBBLE_R = int(ANALYSIS_H * 0.064)     # 角丸半径 (≈46)
    _BUBBLE_MAX_W = int(ANALYSIS_W * 0.5)   # 探索する最大幅 (画面半分)
    _BEIGE_THRESHOLD = 0.25                 # マスク内ベージュ割合の閾値

    try:
        # ダイアログ表示中は吹き出し検出をスキップ (金色装飾枠の誤検出防止)
        if detect_dialog_corners(img_path):
            logger.debug("[MINI_CONV] ダイアログ四隅検出 → スキップ")
            return None

        img = imread_cached(img_path)
        if img is None:
            return None
        resized = cv2.resize(img, (ANALYSIS_W, ANALYSIS_H))

        # ── 角丸矩形マスク生成 ──
        def _rounded_rect_mask(width, height, radius):
            m = np.zeros((height, width), dtype=np.uint8)
            r = min(radius, width // 2, height // 2)
            cv2.rectangle(m, (r, 0), (width - r, height), 255, -1)
            cv2.rectangle(m, (0, r), (width, height - r), 255, -1)
            cv2.circle(m, (r, r), r, 255, -1)
            cv2.circle(m, (width - r, r), r, 255, -1)
            cv2.circle(m, (r, height - r), r, 255, -1)
            cv2.circle(m, (width - r, height - r), r, 255, -1)
            return m

        # ── ベージュピクセルマスク (アクティブ吹き出しのみ) ──
        # ベージュ (暖色): R > B (差 >= 10)。グレー (非アクティブ): R ≈ B で除外。
        _rgb_lo = np.array([160, 170, 170], dtype=np.uint8)  # BGR order
        _rgb_hi = np.array([255, 255, 255], dtype=np.uint8)
        _rgb_mask = cv2.inRange(resized, _rgb_lo, _rgb_hi)
        _ch_max = np.max(resized, axis=2)
        _ch_min = np.min(resized, axis=2)
        _low_spread = ((_ch_max.astype(int) - _ch_min.astype(int)) < 50).astype(np.uint8) * 255
        # R - B >= 10: ベージュ (暖色) のみ通過、グレー (無彩色) を除外
        _r_ch = resized[:, :, 2].astype(int)
        _b_ch = resized[:, :, 0].astype(int)
        _warm_tone = ((_r_ch - _b_ch) >= 10).astype(np.uint8) * 255
        beige_mask = cv2.bitwise_and(_rgb_mask, _low_spread)
        beige_mask = cv2.bitwise_and(beige_mask, _warm_tone)

        # ── キャラアイコン位置をベージュ扱いにしたマスク (幅推定用) ──
        # キャラアイコン (黒いキャラ等) でベージュが途切れるのを防ぐため、
        # 吹き出し端のアイコン円をベージュで埋めてからスキャンする。
        _icon_r_est = _BUBBLE_H // 2  # アイコン半径 ≈ 吹き出し高さ/2
        # resized は常に ANALYSIS_W x ANALYSIS_H なので stretch=1.0 が正しい。
        # imread_cached のキャッシュが旧サイズを返す場合に備え resized.shape を使う。
        _H_resized, _W_resized = resized.shape[:2]
        _aspect_src = _W_resized / _H_resized if _H_resized > 0 else 2.0
        _aspect_dst = ANALYSIS_W / ANALYSIS_H
        _stretch = _aspect_dst / _aspect_src if _aspect_src > 0 else 1.0
        _icon_rx_est = max(1, int(_icon_r_est * _stretch))
        beige_for_scan = beige_mask.copy()
        # アイコン中心 = 角丸半径 + アイコン半径 (角丸の内側にアイコンが配置)
        _icon_cx_offset = int((_BUBBLE_R + _icon_r_est) * _stretch)
        # 左端アイコン
        _char_left = np.zeros((ANALYSIS_H, ANALYSIS_W), dtype=np.uint8)
        cv2.ellipse(_char_left,
                    (_icon_cx_offset, _BUBBLE_Y + _BUBBLE_H // 2),
                    (_icon_rx_est, _icon_r_est), 0, 0, 360, 255, -1)
        beige_for_scan[_char_left > 0] = 255
        # 右端アイコン
        _char_right = np.zeros((ANALYSIS_H, ANALYSIS_W), dtype=np.uint8)
        cv2.ellipse(_char_right,
                    (ANALYSIS_W - _icon_cx_offset, _BUBBLE_Y + _BUBBLE_H // 2),
                    (_icon_rx_est, _icon_r_est), 0, 0, 360, 255, -1)
        beige_for_scan[_char_right > 0] = 255

        # ── 吹き出し幅を推定: 端からベージュが途切れるまでスキャン ──
        def _find_bubble_width(beige, y0, h, from_left):
            """端からベージュピクセルの密度が高い列の数を数え、吹き出し幅を推定。
            アイコン等で一時的に密度が落ちても、連続10列以上途切れなければ継続する。"""
            roi = beige[y0:y0 + h, :]
            _min_w = _BUBBLE_R * 2  # 最小幅 = 角丸直径
            _GAP_TOLERANCE = 10  # 連続N列で密度<0.1なら終了
            _gap_count = 0
            _last_valid = _min_w
            for col in range(_min_w, _BUBBLE_MAX_W):
                x = col if from_left else ANALYSIS_W - 1 - col
                col_pixels = roi[:, x]
                density = np.count_nonzero(col_pixels) / h
                if density < 0.1:
                    _gap_count += 1
                    if _gap_count >= _GAP_TOLERANCE:
                        return _last_valid
                else:
                    _gap_count = 0
                    _last_valid = col + 1
            return _BUBBLE_MAX_W

        # ── 左右それぞれチェック ──
        shape_mask = _rounded_rect_mask(_BUBBLE_MAX_W, _BUBBLE_H, _BUBBLE_R)
        candidates = []

        for side, from_left in [("left", True), ("right", False)]:
            bubble_w = _find_bubble_width(beige_for_scan, _BUBBLE_Y, _BUBBLE_H, from_left)
            if bubble_w < _BUBBLE_R * 2:
                continue

            # 角丸矩形マスクを吹き出し幅で切り出し
            bubble_mask = _rounded_rect_mask(bubble_w, _BUBBLE_H, _BUBBLE_R)

            # 画像上の対応領域
            if from_left:
                x0 = 0
            else:
                x0 = ANALYSIS_W - bubble_w

            # 範囲チェック
            y0 = _BUBBLE_Y
            y1 = min(y0 + _BUBBLE_H, ANALYSIS_H)
            x1 = min(x0 + bubble_w, ANALYSIS_W)
            _bw = x1 - x0
            _bh = y1 - y0
            if _bw <= 0 or _bh <= 0:
                continue

            # マスク内のベージュ割合を計算
            roi_beige = beige_mask[y0:y1, x0:x1]
            roi_shape = bubble_mask[0:_bh, 0:_bw]
            masked = cv2.bitwise_and(roi_beige, roi_shape)
            shape_pixels = np.count_nonzero(roi_shape)
            beige_pixels = np.count_nonzero(masked)
            ratio = beige_pixels / shape_pixels if shape_pixels > 0 else 0

            if ratio >= _BEIGE_THRESHOLD:
                # ── 異色ピクセル棄却: キャラアイコン円をマスクした上で判定 ──
                # キャラアイコンは吹き出し端に埋め込まれた円形 (直径≈吹き出し高さ)。
                # マスクでアイコンを除外し、残りがベージュ+黒のみかを判定。
                # カラフルなキャラでもアイコン部分が異色に計上されない。
                #
                # 【マスク位置・形状はデバイス解像度の比率に依存】
                # 実機上のアイコンは正円だが、元画像 (ADB: 2160x1080,
                # Galaxy: 1520x720 等) を ANALYSIS_W x ANALYSIS_H に
                # resize する際、アスペクト比の差で横方向に伸縮する。
                # そのため正円ではなく楕円マスクを使い、X 半径を
                # アスペクト比の差分で補正する。
                # 例: Xperia 2:1 → (43,43), Galaxy 2.11:1 → (40,43)
                _OTHER_THRESHOLD = 0.20
                _OTHER_THRESHOLD_RELAXED = 0.30  # 2色支配時の緩和閾値
                _HIST_TOP2_THRESHOLD = 0.70      # ヒストグラム2色支配率の閾値
                _H_r, _W_r = resized.shape[:2]
                _aspect_src = _W_r / _H_r if _H_r > 0 else 2.0
                _aspect_dst = ANALYSIS_W / ANALYSIS_H
                _stretch_x = _aspect_dst / _aspect_src if _aspect_src > 0 else 1.0
                _icon_ry = _bh // 2           # Y 半径 = 吹き出し高さの半分
                _icon_rx = max(1, int(_icon_ry * _stretch_x))  # X 半径 = アスペクト補正
                _icon_cy = _bh // 2
                # アイコン中心 = 角丸半径 + アイコン半径 (ROI内座標)
                _icon_cx_off = int((_BUBBLE_R + _icon_ry) * _stretch_x)
                char_circle = np.zeros((_bh, _bw), dtype=np.uint8)
                if from_left:
                    cv2.ellipse(char_circle, (_icon_cx_off, _icon_cy), (_icon_rx, _icon_ry), 0, 0, 360, 255, -1)
                else:
                    cv2.ellipse(char_circle, (_bw - _icon_cx_off, _icon_cy), (_icon_rx, _icon_ry), 0, 0, 360, 255, -1)
                # キャラ円を除外した角丸マスク
                _shape_no_char = roi_shape.copy()
                _shape_no_char[char_circle > 0] = 0
                _shape_no_char_px = np.count_nonzero(_shape_no_char)
                if _shape_no_char_px <= 0:
                    continue
                roi_img = resized[y0:y1, x0:x1]
                roi_gray = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
                dark_mask = (roi_gray < 80).astype(np.uint8) * 255
                beige_or_dark = cv2.bitwise_or(roi_beige, dark_mask)
                beige_or_dark_shaped = cv2.bitwise_and(beige_or_dark, _shape_no_char)
                known_pixels = np.count_nonzero(beige_or_dark_shaped)
                other_pixels = _shape_no_char_px - known_pixels
                other_ratio = other_pixels / _shape_no_char_px

                # ── 2色支配判定: ヒストグラム bin=32 の top2 率 ──
                # 吹き出しは背景色+文字色の2色構成のため、色に依存せず判定可能。
                # マスク適用済みグレースケールで計算。
                _masked_gray = roi_gray.copy()
                _masked_gray[_shape_no_char == 0] = 0
                _valid_pixels = roi_gray[_shape_no_char > 0]
                _hist_top2 = 0.0
                if len(_valid_pixels) > 0:
                    _quantized = (_valid_pixels // 32) * 32
                    _vals, _counts = np.unique(_quantized, return_counts=True)
                    _sorted_idx = np.argsort(-_counts)
                    _top2_cnt = _counts[_sorted_idx[0]]
                    if len(_sorted_idx) > 1:
                        _top2_cnt += _counts[_sorted_idx[1]]
                    _hist_top2 = _top2_cnt / len(_valid_pixels)

                # 判定: ベージュ+黒と2色支配の両方を加味
                # (1) other_ratio <= 20%: 従来通り通過
                # (2) other_ratio <= 30% かつ hist_top2 >= 70%: 2色支配で緩和通過
                # (3) それ以外: 棄却
                _effective_thresh = _OTHER_THRESHOLD
                if _hist_top2 >= _HIST_TOP2_THRESHOLD:
                    _effective_thresh = _OTHER_THRESHOLD_RELAXED
                if other_ratio > _effective_thresh:
                    logger.debug(
                        "[MINI_CONV] %s: 異色棄却 other=%.1f%% hist_top2=%.1f%% thresh=%.0f%%",
                        side, other_ratio * 100, _hist_top2 * 100, _effective_thresh * 100)
                    continue

                cx = x0 + bubble_w // 2
                cy = y0 + _BUBBLE_H // 2
                candidates.append({
                    "cx": cx, "cy": cy, "side": side,
                    "x": x0, "y": y0, "w": bubble_w, "h": _BUBBLE_H,
                    "ratio": ratio, "area": beige_pixels,
                    "other_ratio": other_ratio,
                })
                logger.debug("[MINI_CONV] %s bubble: w=%d ratio=%.2f other=%.1f%% (%d/%d)",
                             side, bubble_w, ratio, other_ratio * 100, beige_pixels, shape_pixels)

        if not candidates:
            return None

        best = max(candidates, key=lambda c: c["ratio"])

        # OCR 検証: 吹き出しのY範囲 + 左右半分にテキストが存在するか
        # skip_ocr_verify=True の場合はスキップ (rapid パスでは直前ループで検証済み)
        if not skip_ocr_verify:
            if ocr_items is None:
                logger.debug("[MINI_CONV] ocr_items=None → テキスト検証不可、スキップ")
                return None
            # OCR座標を ANALYSIS_W x ANALYSIS_H にスケーリング (Retina等の高解像度対応)
            # resized.shape を使う (imread_cached のキャッシュ旧サイズ問題を回避)
            _H_r, _W_r = resized.shape[:2]
            _sx = ANALYSIS_W / _W_r if _W_r > 0 else 1.0
            _sy = ANALYSIS_H / _H_r if _H_r > 0 else 1.0
            by1, by2 = best["y"], best["y"] + best["h"]
            if best["side"] == "left":
                bx1, bx2 = 0, ANALYSIS_W // 2
            else:
                bx1, bx2 = ANALYSIS_W // 2, ANALYSIS_W
            has_text_inside = any(
                bx1 <= r["center"][0] * _sx <= bx2 and by1 <= r["center"][1] * _sy <= by2
                for r in ocr_items
                if r["text"] not in ("AUTO", ">>", ">|", "D1", "×")
            )
            if not has_text_inside:
                logger.debug("[MINI_CONV] %s: テキスト未検出 (bbox=(%d,%d)-(%d,%d) scale=%.2f,%.2f)",
                             best["side"], bx1, by1, bx2, by2, _sx, _sy)
                return None

        logger.debug("[MINI_CONV] bubble (%d,%d) side=%s ratio=%.2f",
                     best["cx"], best["cy"], best["side"], best["ratio"])
        return (best["cx"], best["cy"], best["side"])

    except Exception:
        logger.debug("[MINI_CONV] 例外発生", exc_info=True)
        return None


# ─── チュートリアルダイアログ ページ送り/閉じるボタン検出 ─────────────────
# ダイアログにはページング可能な間 ◁▷ 矢印が表示され、
# 最終ページでは × ボタンが右上に出現して閉じることができる。
#
# 検出優先順位:
#   1. assets/templates/tutorial_dialog_close.png が存在 → テンプレートマッチで × 位置を返す
#   2. assets/templates/tutorial_dialog_next.png が存在 → テンプレートマッチで ▷ 位置を返す
#   3. どちらも存在しない → ("close", 固定座標) or ("next", 固定座標) をフォールバック
#
# 戻り値: ("next", cx, cy) | ("close", cx, cy) | None

_DIALOG_CLOSE_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "close_btn.png"
_DIALOG_NEXT_TEMPLATE  = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_dialog_next.png"
_DIALOG_CORNER_TL      = _CRAWLER_ROOT / "assets" / "templates" / "dialog_corner_tl.png"
_DIALOG_CORNER_BL      = _CRAWLER_ROOT / "assets" / "templates" / "dialog_corner_bl.png"


def detect_dialog_nav(img_path: Path,
                      W: int = 1520, H: int = 720,
                      threshold: float = 0.85) -> Optional[tuple[str, int, int]]:
    """
    ダイアログの ▷(次へ) または ×(閉じる) ボタンを検出する。

    テンプレート画像が存在する場合はテンプレートマッチング、
    存在しない場合は固定座標フォールバックを返す。

    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
    try:
        _img = imread_cached(img_path)
        if _img is None:
            return None
        _H, _W = _img.shape[:2]

        def _match_template(tmpl_path: Path, roi_x1: int, roi_y1: int,
                            roi_x2: int, roi_y2: int) -> Optional[tuple[int, int]]:
            _tmpl = imread_cached(tmpl_path, cv2.IMREAD_GRAYSCALE)
            if _tmpl is None:
                return None
            _roi = cv2.cvtColor(_img[roi_y1:roi_y2, roi_x1:roi_x2], cv2.COLOR_BGR2GRAY)
            _res = cv2.matchTemplate(_roi, _tmpl, cv2.TM_CCOEFF_NORMED)
            _, _max_val, _, _max_loc = cv2.minMaxLoc(_res)
            if _max_val >= threshold:
                _th, _tw = _tmpl.shape[:2]
                _cx = roi_x1 + _max_loc[0] + _tw // 2
                _cy = roi_y1 + _max_loc[1] + _th // 2
                return (_cx, _cy)
            return None

        # 1. × ボタン (右上隅: x=W*0.92~W, y=0~H*0.15)
        if _DIALOG_CLOSE_TEMPLATE.exists():
            _r = _match_template(
                _DIALOG_CLOSE_TEMPLATE,
                int(_W * 0.92), 0, _W, int(_H * 0.15),
            )
            if _r:
                logger.debug("[DialogNav] × ボタン検出 (template): (%d,%d)", *_r)
                return ("close", _r[0], _r[1])

        # 2. ▷ 矢印 (右エッジ: x=W*0.85~W, y=H*0.25~H*0.75)
        if _DIALOG_NEXT_TEMPLATE.exists():
            _r2 = _match_template(
                _DIALOG_NEXT_TEMPLATE,
                int(_W * 0.85), int(_H * 0.25), _W, int(_H * 0.75),
            )
            if _r2:
                logger.debug("[DialogNav] ▷ 矢印検出 (template): (%d,%d)", *_r2)
                return ("next", _r2[0], _r2[1])

        # 3. テンプレートなし → 判断できないため None を返し、呼び出し側のシーケンスに委ねる
        return None

    except Exception as _e:
        logger.debug("detect_dialog_nav error: %s", _e)
        return None


def detect_dialog_corners(img_path: Path) -> bool:
    """ダイアログ四隅テンプレ (TL の反転で BL も検出) + X座標一致で判定。

    dialog_corner_tl を上下反転して BL 用テンプレートとしても使う。
    全てのダイアログ判定で共通利用する。
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        _H, _W = img.shape[:2]
        _CORNER_THRESHOLD = 0.65
        _X_TOLERANCE = int(_W * 0.15)
        if not _DIALOG_CORNER_TL.exists():
            return False
        _tpl_tl = imread_cached(_DIALOG_CORNER_TL)
        if _tpl_tl is None:
            return False
        # BL: 専用テンプレ。なければ TL の上下反転をフォールバック
        _tpl_bl = imread_cached(_DIALOG_CORNER_BL) if _DIALOG_CORNER_BL.exists() else cv2.flip(_tpl_tl, 0)
        _corners = {}
        _scores = {}  # 閾値未満のスコアも記録
        for _key, _tpl in (("tl", _tpl_tl), ("bl", _tpl_bl)):
            if _key == "tl":
                _roi = img[: _H // 2, : _W // 2]
                _oy = 0
            else:
                _roi = img[_H // 2 :, : _W // 2]
                _oy = _H // 2
            if (_roi.shape[0] < _tpl.shape[0]
                    or _roi.shape[1] < _tpl.shape[1]):
                continue
            _r = cv2.matchTemplate(_roi, _tpl, cv2.TM_CCOEFF_NORMED)
            _, _mv, _, _ml = cv2.minMaxLoc(_r)
            _scores[_key] = _mv
            if _mv >= _CORNER_THRESHOLD:
                _corners[_key] = (_ml[0], _oy + _ml[1], _mv)
        if "tl" in _corners and "bl" in _corners:
            _dx = abs(_corners["tl"][0] - _corners["bl"][0])
            if _dx <= _X_TOLERANCE:
                return True
            logger.info("[DialogCorners] TL(%d,%d,%.3f) BL(%d,%d,%.3f) X差=%d > %d → 棄却",
                        _corners["tl"][0], _corners["tl"][1], _corners["tl"][2],
                        _corners["bl"][0], _corners["bl"][1], _corners["bl"][2],
                        _dx, _X_TOLERANCE)
        else:
            _tl_s = f"TL={_corners['tl'][2]:.3f}" if "tl" in _corners else f"TL=未検出({_scores.get('tl', 0):.3f})"
            _bl_s = f"BL={_corners['bl'][2]:.3f}" if "bl" in _corners else f"BL=未検出({_scores.get('bl', 0):.3f})"
            logger.debug("[DialogCorners] %s %s (閾値=%.2f) img=%dx%d",
                         _tl_s, _bl_s, _CORNER_THRESHOLD, _W, _H)
        return False
    except Exception:
        return False


def detect_dialog(img_path: Path, W: int = 1520, H: int = 720,
                  require_blur: bool = True) -> Optional[tuple[str, int, int]]:
    """ダイアログ検出 (四隅テンプレ + ぼかし + ▷/× ボタン)。

    require_blur=False: 四隅テンプレのみで判定 (ぼかしなしでも検出)
    require_blur=True: 四隅テンプレ + 背景ぼかし必須
    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
    # 四隅テンプレ: 全モードで必須
    if not detect_dialog_corners(img_path):
        return None
    if require_blur:
        img = imread_cached(img_path)
        if img is not None:
            bH, bW = img.shape[:2]
            if not detect_background_blur(img, bH, bW):
                return None
    return detect_dialog_nav(img_path, W, H)


# ─── ダイアログ枠検出 + × / ▷ ボタン探索 ──────────────────────────────────
def detect_dialog_frame_and_nav(
    img_path: Path, W: int = 1520, H: int = 720,
    ocr_texts: Optional[list] = None,
    roi: Optional[tuple] = None,
) -> Optional[tuple]:
    """
    ダイアログ枠（形状）を視覚的に検出し、その内部/周辺で ×(閉じる)/▷(次へ) を探す。

    トリガー優先順:
      1. HSV金色枠の大矩形検出 (主: 形状ベース)
      2. OCR キーワード補助     (副: テキストベース、枠検出失敗時フォールバック)

    ボタン探索優先順:
      ×: 1.テンプレート 2.Canny+Hough 3.輝度   → 固定座標 (W*0.975, H*0.055)
      ▷: 1.テンプレート 2.Canny+Hough 3.輝度   → 固定座標 (W*0.91,  H*0.49)
      未特定時: ダイアログ下部中央 ("bottom")

    Returns: ("close",  cx, cy)
             ("next",   cx, cy)
             ("bottom", cx, cy)
             None  — ダイアログ未検出
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        _H, _W = img.shape[:2]

        # ──────────────────────────────────────────────────────────────
        # STEP 0: × ボタン先行検出 (無条件)
        #   画面右上に × があれば「ダイアログ」と即断定して close を返す。
        #   これにより金枠装飾がある画面でも × を見逃さない。
        # ──────────────────────────────────────────────────────────────
        def _find_close_x(img_full, _H, _W):
            """画面右上領域でテンプレートマッチにより × ボタンを探す。"""
            if not _DIALOG_CLOSE_TEMPLATE.exists():
                return None
            # 探索 ROI: 右端 15%, 上端 15%
            _rx1 = int(_W * 0.85)
            _ry2 = int(_H * 0.15)
            _roi_x = img_full[0:_ry2, _rx1:_W]
            if _roi_x.size == 0:
                return None
            _tpl = imread_cached(_DIALOG_CLOSE_TEMPLATE)
            if (_roi_x.shape[0] < _tpl.shape[0]
                    or _roi_x.shape[1] < _tpl.shape[1]):
                return None
            _r = cv2.matchTemplate(_roi_x, _tpl, cv2.TM_CCOEFF_NORMED)
            _, _mv, _, _ml = cv2.minMaxLoc(_r)
            if _mv >= 0.65:
                _tw, _th = _tpl.shape[1], _tpl.shape[0]
                return (_rx1 + _ml[0] + _tw // 2, _ml[1] + _th // 2)
            return None

        def _has_page_arrow(img_full, _H, _W) -> Optional[tuple[int, int]]:
            """右サイドにページング矢印 (▷) が存在するか確認。
            PRIMARY: テンプレートマッチング (tutorial_dialog_next)
            FALLBACK: 輝度ベース検出 (右端6% × 中央帯)
            """
            # PRIMARY: テンプレートマッチング
            try:
                _nav_roi = (int(_W * 0.90), int(_H * 0.25),
                            int(_W * 0.10), int(_H * 0.50))
                _nav_m = ASSET_MANAGER.match_single(
                    "tutorial_dialog_next", img_path, roi=_nav_roi)
                if _nav_m and _nav_m[2] >= 0.65:
                    logger.debug("[Dialog▷] テンプレート検出: (%d,%d) score=%.3f",
                                 _nav_m[0], _nav_m[1], _nav_m[2])
                    return (_nav_m[0], _nav_m[1])
            except Exception:
                pass
            # FALLBACK: 輝度ベース
            _rx1n = int(_W * 0.94)  # 右端6%のみ
            _ry1n, _ry2n = int(_H * 0.30), int(_H * 0.70)
            _roi_n = img_full[_ry1n:_ry2n, _rx1n:_W]
            if _roi_n.size == 0:
                return None
            _g = cv2.cvtColor(_roi_n, cv2.COLOR_BGR2GRAY)
            # 高閾値で白い矢印のみ検出 (金色背景ノイズを排除)
            _, _thr = cv2.threshold(_g, 180, 255, cv2.THRESH_BINARY)
            _bright = cv2.countNonZero(_thr)
            if _bright >= 15:
                # 矢印の固定位置 (右端中央): ページ送り座標
                _ax = int(_W * 0.97)
                _ay = _H // 2
                return (_ax, _ay)
            return None

        # ──────────────────────────────────────────────────────────────
        # STEP 1: ダイアログボックス検出 (コーナー装飾テンプレートマッチ)
        #   ダイアログ共通のコーナー装飾パターンで中央ダイアログ存在を判定。
        # ──────────────────────────────────────────────────────────────
        _frame_detected = detect_dialog_corners(img_path)

        # ──────────────────────────────────────────────────────────────
        # STEP 0: × ボタン先行検出
        #   × + 中央ダイアログボックス(コーナー装飾)の両方が揃って初めて
        #   ダイアログと判定。カード詳細等の非ダイアログ画面での誤検出を防止。
        # ──────────────────────────────────────────────────────────────
        _close_x_pos = _find_close_x(img, _H, _W)
        if _close_x_pos is not None and _frame_detected:
            # ページング矢印 (>) チェック: 矢印があれば close ではなく next を優先
            _arrow_pos = _has_page_arrow(img, _H, _W)
            if _arrow_pos is not None:
                logger.debug("[Dialog] STEP0: × 検出(%d,%d) + 矢印(%d,%d) + 枠あり → next 優先 (ページング)",
                             _close_x_pos[0], _close_x_pos[1], _arrow_pos[0], _arrow_pos[1])
                return ("next", _arrow_pos[0], _arrow_pos[1])
            logger.debug("[Dialog×] STEP0 先行検出: (%d,%d) + 枠あり", _close_x_pos[0], _close_x_pos[1])
            return ("close", _close_x_pos[0], _close_x_pos[1])
        elif _close_x_pos is not None:
            logger.debug("[Dialog×] STEP0: × 検出(%d,%d) だが中央ダイアログ枠なし → 棄却",
                         _close_x_pos[0], _close_x_pos[1])

        if not _frame_detected:
            return None                           # ダイアログ未検出

        # ──────────────────────────────────────────────────────────────
        # STEP 2: フレーム内/周辺で × と ▷ を探す
        # ──────────────────────────────────────────────────────────────
        def _canny_lines(roi_img, thr_lo=40, thr_hi=120, min_len=6, max_gap=4):
            _g = cv2.cvtColor(roi_img, cv2.COLOR_BGR2GRAY)
            _e = cv2.Canny(_g, thr_lo, thr_hi)
            return (
                cv2.HoughLinesP(_e, 1, np.pi / 180,
                                 threshold=8, minLineLength=min_len, maxLineGap=max_gap),
                _g,
            )

        def _chevron_tip(lines):
            """HoughLinesP 結果から ▷ 形状の先端を返す"""
            if lines is None or len(lines) < 2:
                return None
            _ul, _dl = [], []
            for _ln in lines:
                _x1, _y1, _x2, _y2 = _ln[0]
                if _x2 == _x1:
                    continue
                if _x1 > _x2:
                    _x1, _y1, _x2, _y2 = _x2, _y2, _x1, _y1
                _ang = np.degrees(np.arctan2(_y2 - _y1, _x2 - _x1))
                if -70 < _ang < -20:
                    _ul.append((_x1, _y1, _x2, _y2))
                elif 20 < _ang < 70:
                    _dl.append((_x1, _y1, _x2, _y2))
            if _ul and _dl:
                _ur = max(_ul, key=lambda l: l[2])
                _dr = max(_dl, key=lambda l: l[2])
                return (int((_ur[2] + _dr[2]) / 2), int((_ur[3] + _dr[3]) / 2))
            return None

        # ── × ボタン検索 (画面右上隅) ────────────────────────────────────
        if _DIALOG_CLOSE_TEMPLATE.exists():
            _close_tmpl = imread_cached(_DIALOG_CLOSE_TEMPLATE)
            _r = cv2.matchTemplate(
                imread_cached(img_path, cv2.IMREAD_COLOR)[0: int(_H * 0.14), int(_W * 0.88):],
                _close_tmpl,
                cv2.TM_CCOEFF_NORMED,
            )
            _, _mv, _, _ml = cv2.minMaxLoc(_r)
            if _mv >= 0.65:
                _th, _tw = _close_tmpl.shape[:2]
                return ("close",
                        int(_W * 0.88) + _ml[0] + _tw // 2,
                        _ml[1] + _th // 2)

        # Note: Phase B Canny / 輝度フォールバックは廃止 (ホーム画面バナー誤検出防止)。
        # × 検出は STEP 0 テンプレートマッチングに一元化。

        # ── ▷ ボタン (スクリーン右エッジ) ────────────────────────────────
        # ROI を右端6% × 中央帯に限定 (枠装飾の誤マッチ防止)
        _nav_rx1 = int(_W * 0.94)
        _nav_ry1 = int(_H * 0.30)
        _nav_ry2 = int(_H * 0.70)
        if _DIALOG_NEXT_TEMPLATE.exists():
            _next_tmpl = imread_cached(_DIALOG_NEXT_TEMPLATE, cv2.IMREAD_GRAYSCALE)
            _roi_next = cv2.cvtColor(
                img[_nav_ry1:_nav_ry2, _nav_rx1:],
                cv2.COLOR_BGR2GRAY)
            if (_roi_next.shape[0] >= _next_tmpl.shape[0]
                    and _roi_next.shape[1] >= _next_tmpl.shape[1]):
                _r2 = cv2.matchTemplate(
                    _roi_next,
                    _next_tmpl,
                    cv2.TM_CCOEFF_NORMED,
                )
                _, _mv2, _, _ml2 = cv2.minMaxLoc(_r2)
                if _mv2 >= 0.65:
                    _th2, _tw2 = _next_tmpl.shape[:2]
                    return ("next",
                            _nav_rx1 + _ml2[0] + _tw2 // 2,
                            _nav_ry1 + _ml2[1] + _th2 // 2)

        _rx1n, _ry1n = _nav_rx1, _nav_ry1
        _rx2n, _ry2n = _W, _nav_ry2
        _roi_n = img[_ry1n:_ry2n, _rx1n:_rx2n]
        if _roi_n.size > 0:
            _lns_n, _gray_n = _canny_lines(_roi_n)
            _np_tip = _chevron_tip(_lns_n)
            if _np_tip:
                logger.debug("[Dialog▷] Canny検出: (%d,%d)", _rx1n + _np_tip[0], _ry1n + _np_tip[1])
                return ("next", _rx1n + _np_tip[0], _ry1n + _np_tip[1])
            if cv2.countNonZero(cv2.threshold(_gray_n, 140, 255, cv2.THRESH_BINARY)[1]) >= 20:
                _r = roi if roi else (0, 0, _W, _H)
                _nx_fb, _ny_fb = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), _r)
                logger.debug("[Dialog▷] 輝度FB(ROI補正): (%d,%d)", _nx_fb, _ny_fb)
                return ("next", _nx_fb, _ny_fb)

        # ── フォールバック: ROI 補正済み固定座標 ▷ ─────────────────────
        _r = roi if roi else (0, 0, _W, _H)
        _nx_fb, _ny_fb = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), _r)
        logger.debug("[Dialog] フォールバック▷(ROI補正): (%d,%d)", _nx_fb, _ny_fb)
        return ("next", _nx_fb, _ny_fb)

    except Exception as _e:
        logger.debug("detect_dialog_frame_and_nav error: %s", _e)
        return None


# ─── お知らせポップアップ検出 ─────────────────────────────────────────────


_DOT_ACTIVE_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "popup_dot_active.png"
_DOT_INACTIVE_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "popup_dot_inactive.png"
_DOT_MATCH_THRESH = 0.85
_DOT_Y_TOLERANCE = 5   # 同一行と見なす Y 差 (px)
_DOT_SUPPRESS_R = 8     # NMS 抑制半径 (px)


def count_page_dots(img_or_path, H: int = 720, W: int = 1520) -> int:
    """画面下部のページドットインジケータ (● ○ ○ …) の個数を返す。

    アクティブ/非アクティブの2テンプレートでマッチし、
    Y軸整列フィルタで水平に並ぶドットのみカウントする。

    Args:
        img_or_path: cv2画像(ndarray) または画像ファイルパス(Path)
    Returns:
        ドット数 (0 = 未検出)
    """
    if isinstance(img_or_path, (str, Path)):
        img = imread_cached(img_or_path)
        if img is None:
            return 0
        H, W = img.shape[:2]
    else:
        img = img_or_path

    # ROI: y=82-95%, x=15-85% (中央揃いで最大10個程度のドットに対応)
    _y1 = int(H * 0.82)
    _y2 = int(H * 0.95)
    _x1 = int(W * 0.15)
    _x2 = int(W * 0.85)
    _roi = img[_y1:_y2, _x1:_x2]
    if _roi.size == 0:
        return 0
    _gray = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)

    # 2テンプレートでマッチ → 候補座標を統合
    _candidates = []  # (cx, cy, score) — ROI 内座標
    for _tpl_path in (_DOT_ACTIVE_TEMPLATE, _DOT_INACTIVE_TEMPLATE):
        if not _tpl_path.exists():
            continue
        _tpl = imread_cached(_tpl_path)
        if _tpl is None:
            continue
        _g = cv2.cvtColor(_tpl, cv2.COLOR_BGR2GRAY) if len(_tpl.shape) == 3 else _tpl
        _th, _tw = _g.shape[:2]
        if _gray.shape[0] < _th or _gray.shape[1] < _tw:
            continue
        _res = cv2.matchTemplate(_gray, _g, cv2.TM_CCOEFF_NORMED)
        # 閾値超えの全位置を収集
        _locs = np.where(_res >= _DOT_MATCH_THRESH)
        for _py, _px in zip(*_locs):
            _cx = _px + _tw // 2
            _cy = _py + _th // 2
            _s = float(_res[_py, _px])
            _candidates.append((_cx, _cy, _s))

    if not _candidates:
        return 0

    # NMS: 近接候補を統合 (同一ドットの重複除去)
    _candidates.sort(key=lambda c: -c[2])  # スコア降順
    _kept = []
    for _cx, _cy, _s in _candidates:
        _dup = False
        for _kx, _ky, _ in _kept:
            if abs(_cx - _kx) < _DOT_SUPPRESS_R and abs(_cy - _ky) < _DOT_SUPPRESS_R:
                _dup = True
                break
        if not _dup:
            _kept.append((_cx, _cy, _s))

    if len(_kept) < 1:
        return 0

    # Y 軸整列フィルタ: 同一行 (Y差 ≤ _DOT_Y_TOLERANCE) の最大グループ
    _best_row: list = []
    for _ref_y in set(d[1] for d in _kept):
        _row = [d for d in _kept if abs(d[1] - _ref_y) <= _DOT_Y_TOLERANCE]
        if len(_row) > len(_best_row):
            _best_row = _row

    if len(_best_row) < 2:
        return len(_best_row)

    # 等間隔補完: 薄いドット (アクティブ直後等) がテンプレマッチで検出漏れするため、
    # 検出済みドットの間隔中央値を基準に、大きなギャップに欠損ドットを補間する。
    _xs = sorted(d[0] for d in _best_row)
    _gaps = [_xs[i] - _xs[i - 1] for i in range(1, len(_xs))]
    _median_gap = float(np.median(_gaps))
    if _median_gap < 5:
        return len(_best_row)
    _total = 1  # 先頭ドット分
    for _g in _gaps:
        _n_dots_in_gap = round(_g / _median_gap)
        _total += max(1, _n_dots_in_gap)

    return _total


def _detect_page_dots(img, H: int, W: int) -> bool:
    """画面下部にページドットインジケータが3個以上あるか。"""
    return count_page_dots(img, H, W) >= 3


def detect_background_blur(img, H: int, W: int) -> bool:
    """ポップアップ外の背景がぼかし or 半透明ダークオーバーレイかを検出。"""
    # ── 方式1: 左端ストリップ HSV 彩度分散 (ガウシアンぼかし検出) ──
    _lx2 = int(W * 0.06)
    _ly1, _ly2 = int(H * 0.15), int(H * 0.85)
    _left = img[_ly1:_ly2, 0:_lx2]
    if _left.size > 0:
        _hsv = cv2.cvtColor(_left, cv2.COLOR_BGR2HSV)
        _sat_var = float(_hsv[:, :, 1].var())
        if _sat_var < 800:
            logger.debug("[BG_OVERLAY] ぼかし検出: sat_var=%.1f < 800", _sat_var)
            return True

    # ── 方式2: 半透明ダークオーバーレイ (四隅の暗さ) ──
    # お知らせポップアップ等: 背景にダーク半透明を重ねるため四隅が暗くなる
    _gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    _sz = 40
    _corners = [
        _gray[0:_sz, 0:_sz],           # TL
        _gray[0:_sz, W - _sz:W],       # TR
        _gray[H - _sz:H, 0:_sz],       # BL
        _gray[H - _sz:H, W - _sz:W],   # BR
    ]
    _dark_count = sum(1 for c in _corners if c.mean() < 80)
    if _dark_count >= 3:
        logger.debug("[BG_OVERLAY] ダークオーバーレイ検出: %d/4 隅が暗い", _dark_count)
        return True

    return False


def detect_popup_overlay(
    img_path: Path, ocr_texts: Optional[list[str]] = None,
) -> Optional[dict]:
    """ポップアップオーバーレイ（お知らせ/ホーム共通）を検出する。

    必須条件 (全て AND):
      1. ポップアップ専用四隅テンプレ — 長方形整合性チェック付き
      2. ページドット ≥ 1
      3. 背景ぼかし

    付加的要素:
      - popup_home_next テンプレ (▷ボタン) — 検出スコアを返すが必須ではない
      - OCR「今日は表示し」検出時は is_notice=True

    Returns:
      {"dots": int, "next_score": float, "is_notice": bool,
       "corners": (tl, tr, bl, br)} or None
    """
    if ocr_texts is None:
        ocr_texts = []

    # OCR「今日は表示しない」→ お知らせ確定フラグ
    _is_notice_ocr = any("今日は表示し" in t for t in ocr_texts)

    if not img_path:
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (画像なし)")
            return {"dots": 0, "next_score": 0.0, "is_notice": True, "corners": None}
        return None

    _img = imread_cached(img_path)
    if _img is None:
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (画像読込失敗)")
            return {"dots": 0, "next_score": 0.0, "is_notice": True, "corners": None}
        return None

    # 1. 四隅テンプレ
    _img_gray = cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY) if len(_img.shape) == 3 else _img
    _corners = _detect_popup_corners(_img_gray)
    if _corners is None:
        # 四隅なしでも OCR で確定できる
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (四隅なし)")
            return {"dots": 0, "next_score": 0.0, "is_notice": True, "corners": None}
        return None

    # 2. ページドット
    _dots = count_page_dots(img_path)
    if _dots < 1:
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (ドットなし)")
            return {"dots": 0, "next_score": 0.0, "is_notice": True, "corners": _corners}
        return None

    # 3. 背景ぼかし
    _bH, _bW = _img.shape[:2]
    if not detect_background_blur(_img, _bH, _bW):
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (ぼかしなし)")
            return {"dots": _dots, "next_score": 0.0, "is_notice": True, "corners": _corners}
        return None

    # 4. ▷ボタンまたは×ボタンのどちらかが存在すること
    _next_score = _match_popup_next_roi(_img, _bH, _bW)
    _close_score = _match_popup_close(_img_gray)
    _NAV_THRESH = 0.75
    _has_nav = _next_score >= _NAV_THRESH or _close_score >= _NAV_THRESH
    if not _has_nav:
        if _is_notice_ocr:
            logger.info("[POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定 (▷/×なし)")
            return {"dots": _dots, "next_score": _next_score, "close_score": _close_score,
                    "is_notice": True, "corners": _corners}
        logger.debug("[POPUP] ▷(%.3f)/×(%.3f) いずれも閾値%.2f未満 → 棄却",
                     _next_score, _close_score, _NAV_THRESH)
        return None

    _tl, _tr, _bl, _br = _corners

    logger.info("[POPUP] 四隅TL(%.3f)TR(%.3f)BL(%.3f)BR(%.3f)+ドット=%d+背景ぼかし"
                "+next(%.3f)+close(%.3f) notice_ocr=%s → ポップアップ確定",
                _tl[2], _tr[2], _bl[2], _br[2], _dots, _next_score, _close_score, _is_notice_ocr)

    return {
        "dots": _dots,
        "next_score": _next_score,
        "close_score": _close_score,
        "is_notice": _is_notice_ocr,
        "corners": _corners,
    }



# ═══════════════════════════════════════════════════════════════════
#  ポップアップ検出 (お知らせポップアップ・ダイアログとは独立)
# ═══════════════════════════════════════════════════════════════════

_POPUP_NEXT_TEMPLATE      = _CRAWLER_ROOT / "assets" / "templates" / "popup_home_next.png"
_POPUP_NEXT_DARK_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "popup_home_next_dark.png"
_POPUP_CLOSE_TEMPLATE     = _CRAWLER_ROOT / "assets" / "templates" / "popup_home_close.png"
_POPUP_CORNER_BL_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "popup_notice_corner_bl.png"

# マスク付きマッチ: テンプレの枠線+1px膨張で判定 (背景の影響を考慮)
_POPUP_CORNER_FRAME_THRESH = 75   # 枠線抽出の輝度閾値
_POPUP_CORNER_MATCH_THRESH = 0.40  # 座標一致を優先するため低めに設定
_POPUP_CORNER_Y_TOLERANCE = 30   # 上辺/下辺の Y 差許容 (px)
_POPUP_CORNER_X_TOLERANCE = 30   # 左辺/右辺の X 差許容 (px)
_POPUP_CORNER_MIN_WIDTH = 200    # ポップアップ最小幅 (px)
_POPUP_CORNER_MAX_WIDTH = 1200   # ポップアップ最大幅 (px)
_POPUP_CORNER_MIN_HEIGHT = 100   # ポップアップ最小高さ (px)
_POPUP_CORNER_MAX_HEIGHT = 600   # ポップアップ最大高さ (px)
_POPUP_CORNER_ROI_PAD = 50       # BL/BR 座標から TL/TR を探す際のパディング (px)


def _build_popup_corner_masks() -> Optional[tuple]:
    """ポップアップ角テンプレートからマスク付き4隅データを構築。

    テンプレートのアルファチャンネルを利用し、枠線(輝度>=75)を抽出後
    1px膨張(アルファ内)で背景影響を含めたマスクを生成する。

    Returns: (tpl_bl, tpl_br, tpl_tl, tpl_tr,
              mask_bl, mask_br, mask_tl, mask_tr, th, tw) or None
    """
    if not _POPUP_CORNER_BL_TEMPLATE.exists():
        return None
    _tpl_rgba = cv2.imread(str(_POPUP_CORNER_BL_TEMPLATE), cv2.IMREAD_UNCHANGED)
    if _tpl_rgba is None:
        return None

    # アルファチャンネルがある場合: 枠線+1px膨張マスク
    if len(_tpl_rgba.shape) == 3 and _tpl_rgba.shape[2] == 4:
        _gray = cv2.cvtColor(_tpl_rgba[:, :, :3], cv2.COLOR_BGR2GRAY)
        _alpha = _tpl_rgba[:, :, 3]
        _frame = ((_alpha > 0) & (_gray >= _POPUP_CORNER_FRAME_THRESH)).astype(np.uint8) * 255
        _kernel = np.ones((3, 3), np.uint8)
        _dilated = cv2.dilate(_frame, _kernel, iterations=1)
        _mask_bl = cv2.bitwise_and(_dilated, _alpha)
    else:
        # アルファなしフォールバック: 輝度閾値マスク
        _gray = cv2.cvtColor(_tpl_rgba, cv2.COLOR_BGR2GRAY) if len(_tpl_rgba.shape) == 3 else _tpl_rgba
        _, _mask_bl = cv2.threshold(_gray, _POPUP_CORNER_FRAME_THRESH, 255, cv2.THRESH_BINARY)

    _tpl_bl = cv2.cvtColor(_tpl_rgba[:, :, :3], cv2.COLOR_BGR2GRAY) if len(_tpl_rgba.shape) == 3 else _tpl_rgba
    _tpl_br = cv2.flip(_tpl_bl, 1)
    _tpl_tl = cv2.flip(_tpl_bl, 0)
    _tpl_tr = cv2.flip(_tpl_bl, -1)
    _mask_br = cv2.flip(_mask_bl, 1)
    _mask_tl = cv2.flip(_mask_bl, 0)
    _mask_tr = cv2.flip(_mask_bl, -1)

    return (_tpl_bl, _tpl_br, _tpl_tl, _tpl_tr,
            _mask_bl, _mask_br, _mask_tl, _mask_tr,
            _tpl_bl.shape[0], _tpl_bl.shape[1])


def _detect_popup_corners(
    img_gray: np.ndarray,
) -> Optional[tuple[tuple[int, int, float], tuple[int, int, float],
                    tuple[int, int, float], tuple[int, int, float]]]:
    """ポップアップ四隅 (TL, TR, BL, BR) をマスク付きテンプレマッチで検出。

    段階的 ROI 探索:
      1. BL を左下象限、BR を右下象限で検出 (信頼度が高い)
      2. TR を BR.x ± PAD の縦帯 (上半分) で検出
      3. TL を BL.x ± PAD かつ TR.y ± PAD の狭い ROI で検出

    2隅以上が閾値超え + 長方形整合性チェック通過で結果を返す。
    閾値未満のコーナーもベスト位置を記録し、対辺からの推定座標で補完する。

    Returns: ((tl_cx, tl_cy, tl_score), (tr_cx, tr_cy, tr_score),
              (bl_cx, bl_cy, bl_score), (br_cx, br_cy, br_score)) or None
    """
    _data = _build_popup_corner_masks()
    if _data is None:
        return None
    _tpl_bl, _tpl_br, _tpl_tl, _tpl_tr, \
        _mask_bl, _mask_br, _mask_tl, _mask_tr, _th, _tw = _data

    _H, _W = img_gray.shape
    _PAD = _POPUP_CORNER_ROI_PAD
    _THRESH = _POPUP_CORNER_MATCH_THRESH
    _MIN_CORNERS = 2  # 最低限必要な閾値超えコーナー数

    def _match_best(tpl, mask, roi):
        """閾値に関係なくベストマッチを返す。"""
        if roi.shape[0] < _th or roi.shape[1] < _tw:
            return None
        try:
            _res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED, mask=mask)
            _res = np.where(np.isfinite(_res), _res, -1)
            _, _s, _, _loc = cv2.minMaxLoc(_res)
            return (_loc[0], _loc[1], _s)
        except Exception:
            return None

    # ── Step 1: BL (左下象限) ──
    _roi_bl = img_gray[_H // 2:, :_W // 2]
    _m_bl = _match_best(_tpl_bl, _mask_bl, _roi_bl)
    if _m_bl is None:
        logger.debug("[POPUP_CORNER] BL マッチ失敗")
        return None
    _bl_cx = _m_bl[0] + _tw // 2
    _bl_cy = _H // 2 + _m_bl[1] + _th // 2
    _bl = (_bl_cx, _bl_cy, _m_bl[2])

    # ── Step 2: BR (右下象限) ──
    _roi_br = img_gray[_H // 2:, _W // 2:]
    _m_br = _match_best(_tpl_br, _mask_br, _roi_br)
    if _m_br is None:
        logger.debug("[POPUP_CORNER] BR マッチ失敗")
        return None
    _br_cx = _W // 2 + _m_br[0] + _tw // 2
    _br_cy = _H // 2 + _m_br[1] + _th // 2
    _br = (_br_cx, _br_cy, _m_br[2])

    # ── Step 3: TR (BR.x ± PAD, 上半分) ──
    _tr_x1 = max(0, _br_cx - _tw // 2 - _PAD)
    _tr_x2 = min(_W, _br_cx + _tw // 2 + _PAD)
    _roi_tr = img_gray[:_H // 2, _tr_x1:_tr_x2]
    _m_tr = _match_best(_tpl_tr, _mask_tr, _roi_tr)
    if _m_tr is None:
        logger.debug("[POPUP_CORNER] TR マッチ失敗 (ROI x=%d-%d)", _tr_x1, _tr_x2)
        return None
    _tr_cx = _tr_x1 + _m_tr[0] + _tw // 2
    _tr_cy = _m_tr[1] + _th // 2
    _tr = (_tr_cx, _tr_cy, _m_tr[2])

    # ── Step 4: TL (BL.x ± PAD, TR.y ± PAD) ──
    _tl_x1 = max(0, _bl_cx - _tw // 2 - _PAD)
    _tl_x2 = min(_W, _bl_cx + _tw // 2 + _PAD)
    _tl_y1 = max(0, _tr_cy - _th // 2 - _PAD)
    _tl_y2 = min(_H, _tr_cy + _th // 2 + _PAD)
    _roi_tl = img_gray[_tl_y1:_tl_y2, _tl_x1:_tl_x2]
    _m_tl = _match_best(_tpl_tl, _mask_tl, _roi_tl)
    if _m_tl is None:
        logger.debug("[POPUP_CORNER] TL マッチ失敗 (ROI x=%d-%d, y=%d-%d)",
                     _tl_x1, _tl_x2, _tl_y1, _tl_y2)
        return None
    _tl_cx = _tl_x1 + _m_tl[0] + _tw // 2
    _tl_cy = _tl_y1 + _m_tl[1] + _th // 2
    _tl = (_tl_cx, _tl_cy, _m_tl[2])

    # ── 閾値超えコーナー数チェック (2隅以上必須) ──
    _corners = {"TL": _tl, "TR": _tr, "BL": _bl, "BR": _br}
    _passed = {k: v for k, v in _corners.items() if v[2] >= _THRESH}
    _pass_count = len(_passed)
    if _pass_count < _MIN_CORNERS:
        _scores = " ".join(f"{k}={v[2]:.3f}" for k, v in _corners.items())
        logger.debug("[POPUP_CORNER] 閾値超え %d/%d < %d → 棄却 (%s)",
                     _pass_count, len(_corners), _MIN_CORNERS, _scores)
        return None

    # ── 長方形整合性チェック (閾値超えコーナーのみで判定) ──
    # 低スコアコーナーは位置精度が悪いため、整合性チェックから除外する。
    # 対辺ペア (上辺TL-TR, 下辺BL-BR, 左辺TL-BL, 右辺TR-BR) のうち
    # 両方が閾値超えのペアのみチェックする。
    _pairs = [
        ("上辺Y", "TL", "TR", lambda a, b: abs(a[1] - b[1]), _POPUP_CORNER_Y_TOLERANCE),
        ("下辺Y", "BL", "BR", lambda a, b: abs(a[1] - b[1]), _POPUP_CORNER_Y_TOLERANCE),
        ("左辺X", "TL", "BL", lambda a, b: abs(a[0] - b[0]), _POPUP_CORNER_X_TOLERANCE),
        ("右辺X", "TR", "BR", lambda a, b: abs(a[0] - b[0]), _POPUP_CORNER_X_TOLERANCE),
    ]
    for _label, _k1, _k2, _diff_fn, _tol in _pairs:
        if _k1 in _passed and _k2 in _passed:
            _diff = _diff_fn(_corners[_k1], _corners[_k2])
            if _diff > _tol:
                logger.debug("[POPUP_CORNER] %s差=%d > %d → 棄却", _label, _diff, _tol)
                return None

    # 幅・高さチェック: 閾値超えペアから算出
    if "BL" in _passed and "BR" in _passed:
        _width = _br[0] - _bl[0]
        if _width < _POPUP_CORNER_MIN_WIDTH or _width > _POPUP_CORNER_MAX_WIDTH:
            logger.debug("[POPUP_CORNER] 幅=%.0f (範囲外 %d-%d) → 棄却",
                         _width, _POPUP_CORNER_MIN_WIDTH, _POPUP_CORNER_MAX_WIDTH)
            return None
    elif "TL" in _passed and "TR" in _passed:
        _width = _tr[0] - _tl[0]
        if _width < _POPUP_CORNER_MIN_WIDTH or _width > _POPUP_CORNER_MAX_WIDTH:
            logger.debug("[POPUP_CORNER] 幅=%.0f (範囲外 %d-%d) → 棄却",
                         _width, _POPUP_CORNER_MIN_WIDTH, _POPUP_CORNER_MAX_WIDTH)
            return None

    if "TL" in _passed and "BL" in _passed:
        _height = _bl[1] - _tl[1]
        if _height < _POPUP_CORNER_MIN_HEIGHT or _height > _POPUP_CORNER_MAX_HEIGHT:
            logger.debug("[POPUP_CORNER] 高さ=%.0f (範囲外 %d-%d) → 棄却",
                         _height, _POPUP_CORNER_MIN_HEIGHT, _POPUP_CORNER_MAX_HEIGHT)
            return None
    elif "TR" in _passed and "BR" in _passed:
        _height = _br[1] - _tr[1]
        if _height < _POPUP_CORNER_MIN_HEIGHT or _height > _POPUP_CORNER_MAX_HEIGHT:
            logger.debug("[POPUP_CORNER] 高さ=%.0f (範囲外 %d-%d) → 棄却",
                         _height, _POPUP_CORNER_MIN_HEIGHT, _POPUP_CORNER_MAX_HEIGHT)
            return None

    if _pass_count < 4:
        _scores = " ".join(f"{k}={v[2]:.3f}{'✓' if v[2] >= _THRESH else '✗'}"
                           for k, v in _corners.items())
        logger.info("[POPUP_CORNER] %d/4隅閾値超え + 整合OK → 通過 (%s)",
                    _pass_count, _scores)

    return (_tl, _tr, _bl, _br)


def _match_popup_next_roi(img, _H: int, _W: int, threshold: float = 0.89) -> float:
    """popup_home_next テンプレ (light/dark 2種) を ROI 制限付きでマッチし最高スコアを返す。

    ROI: x=W*0.70〜W, y=H*0.25〜H*0.75 (ポップアップ枠右側・縦中央)
    """
    _x1, _x2 = int(_W * 0.70), _W
    _y1, _y2 = int(_H * 0.25), int(_H * 0.75)
    _roi = img[_y1:_y2, _x1:_x2]
    if _roi.size == 0:
        return 0.0
    _gray_roi = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)
    _best = 0.0
    for _tpl_path in (_POPUP_NEXT_TEMPLATE, _POPUP_NEXT_DARK_TEMPLATE):
        if not _tpl_path.exists():
            continue
        _tpl = imread_cached(_tpl_path)
        if _tpl is None:
            continue
        _g = cv2.cvtColor(_tpl, cv2.COLOR_BGR2GRAY)
        if _gray_roi.shape[0] < _g.shape[0] or _gray_roi.shape[1] < _g.shape[1]:
            continue
        _r = cv2.matchTemplate(_gray_roi, _g, cv2.TM_CCOEFF_NORMED)
        _, _mv, _, _ = cv2.minMaxLoc(_r)
        if _mv > _best:
            _best = _mv
    return _best


def _match_popup_close(img_gray: np.ndarray) -> float:
    """popup_home_close テンプレを全画面でマッチし最高スコアを返す。"""
    if not _POPUP_CLOSE_TEMPLATE.exists():
        return 0.0
    _tpl = imread_cached(_POPUP_CLOSE_TEMPLATE)
    if _tpl is None:
        return 0.0
    _g = cv2.cvtColor(_tpl, cv2.COLOR_BGR2GRAY) if len(_tpl.shape) == 3 else _tpl
    if img_gray.shape[0] < _g.shape[0] or img_gray.shape[1] < _g.shape[1]:
        return 0.0
    _r = cv2.matchTemplate(img_gray, _g, cv2.TM_CCOEFF_NORMED)
    _, _mv, _, _ = cv2.minMaxLoc(_r)
    return _mv


def detect_popup_home_nav(
    img_path: Path, W: int = ANALYSIS_W, H: int = ANALYSIS_H,
    threshold: float = 0.75,
    prefer_close: bool = False,
) -> Optional[tuple[str, int, int]]:
    """ホームポップアップの ▷(次へ) または ×(閉じる) ボタンを検出する。

    ▷ 検出は ROI (右側70%〜, 縦中央25%〜75%) + light/dark 2テンプレで行う。
    × 検出は全画面で行う (最終ページでは × がポップアップ右上に出現)。

    Args:
        prefer_close: True なら × を先に検出 (最終ページ用)。
                      False なら ▷ を先に検出 (ページ送り中)。

    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
    img = imread_analysis(img_path)
    if img is None:
        return None
    _H, _W = img.shape[:2]
    _gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def _find_close():
        if _POPUP_CLOSE_TEMPLATE.exists():
            _tpl_c = imread_cached(_POPUP_CLOSE_TEMPLATE)
            if _tpl_c is not None:
                _g_c = cv2.cvtColor(_tpl_c, cv2.COLOR_BGR2GRAY)
                _r_c = cv2.matchTemplate(_gray, _g_c, cv2.TM_CCOEFF_NORMED)
                _, _mv_c, _, _ml_c = cv2.minMaxLoc(_r_c)
                if _mv_c >= threshold:
                    _th_c, _tw_c = _g_c.shape[:2]
                    _cx = _ml_c[0] + _tw_c // 2
                    _cy = _ml_c[1] + _th_c // 2
                    logger.debug("[PopupHomeNav] × 検出 (%d,%d) score=%.3f", _cx, _cy, _mv_c)
                    return ("close", _cx, _cy)
        return None

    def _find_next():
        _rx1, _ry1 = int(_W * 0.70), int(_H * 0.25)
        _rx2, _ry2 = _W, int(_H * 0.75)
        _roi = _gray[_ry1:_ry2, _rx1:_rx2]
        if _roi.size == 0:
            return None
        _best_n = None
        for _tpl_path in (_POPUP_NEXT_TEMPLATE, _POPUP_NEXT_DARK_TEMPLATE):
            if not _tpl_path.exists():
                continue
            _tpl_n = imread_cached(_tpl_path)
            if _tpl_n is None:
                continue
            _g_n = cv2.cvtColor(_tpl_n, cv2.COLOR_BGR2GRAY)
            if _roi.shape[0] < _g_n.shape[0] or _roi.shape[1] < _g_n.shape[1]:
                continue
            _r_n = cv2.matchTemplate(_roi, _g_n, cv2.TM_CCOEFF_NORMED)
            _, _mv_n, _, _ml_n = cv2.minMaxLoc(_r_n)
            if _mv_n >= threshold:
                _th_n, _tw_n = _g_n.shape[:2]
                _cx_n = _ml_n[0] + _rx1 + _tw_n // 2
                _cy_n = _ml_n[1] + _ry1 + _th_n // 2
                if _best_n is None or _mv_n > _best_n[2]:
                    _best_n = (_cx_n, _cy_n, _mv_n)
        if _best_n:
            logger.debug("[PopupHomeNav] ▷ 検出 (%d,%d) score=%.3f",
                         _best_n[0], _best_n[1], _best_n[2])
            return ("next", _best_n[0], _best_n[1])
        return None

    if prefer_close:
        return _find_close() or _find_next()
    return _find_next() or _find_close()


def _find_close_by_asset(analysis_path: Path) -> Optional[tuple]:
    """ASSET_MANAGER テンプレートで × ボタンを検出する (枠検出不要)。

    detect_dialog_frame_and_nav は枠(コーナー装飾)必須で棄却されるケースがある。
    このヘルパーはテンプレート単独で右上の × を探す。
    Returns: (cx, cy) or None
    """
    _close_roi = (int(ANALYSIS_W * 0.85), 0,
                  int(ANALYSIS_W * 0.15), int(ANALYSIS_H * 0.15))
    for _tpl_name in ("close_btn",):
        _m = ASSET_MANAGER.match_single(_tpl_name, analysis_path, roi=_close_roi)
        if _m and _m[2] >= 0.65:
            logger.debug("[PAGING_CLOSE_ASSET] %s 検出 (%d,%d) score=%.2f",
                         _tpl_name, _m[0], _m[1], _m[2])
            return (_m[0], _m[1])
    return None


# ─── ページング式ダイアログ完全処理 ────────────────────────────────────────
def process_paging_dialog(
    analysis_path: Path, W: int, H: int,
    state: "PilotState", max_pages: int = 10,
    initial_dlg: Optional[tuple] = None,
    ocr_texts: Optional[list] = None,
) -> str:
    """
    ▷ → ▷ → … → × のシーケンスを一括処理する。

    - "next"/"bottom" を検出するたびにタップ → 次ページスクリーンショット取得
    - "close" を検出したらタップして終了
    - ダイアログが消えたら完了扱い
    - phash変化なし → ループ中断 (誤検出▷への無限タップ防止)
    - × ROI bright_pixels=0 が2回続く → 枠外タップで強制脱出
    - max_pages 超過でタイムアウト

    Returns: "DIALOG_CLOSED" | "DIALOG_PAGING_TIMEOUT"
    """
    _roi = state.game_roi
    _prev_phash = compute_phash(analysis_path)
    _no_close_streak = 0  # × ROI bright_pixels=0 の連続回数
    # ページドットで総ページ数を把握
    _total_dots = count_page_dots(analysis_path)
    if _total_dots >= 2:
        logger.info("[PAGING] ページドット=%d検出", _total_dots)
    _phash_fail_count = 0  # phash変化なし連続回数
    _EXTRA_PAGES_FOR_CLOSE = 5  # ドット数送った後 × が出るまでの追加試行回数
    _max_iter = max(max_pages, _total_dots + _EXTRA_PAGES_FOR_CLOSE) if _total_dots >= 2 else max_pages
    for _page in range(_max_iter):
        # page=0 かつ initial_dlg が渡されている場合は外側の検出結果を再利用
        if _page == 0 and initial_dlg is not None:
            _dlg = initial_dlg
        else:
            _dlg = detect_dialog_frame_and_nav(
                analysis_path, W, H, roi=_roi,
                ocr_texts=ocr_texts,
            )
        if _dlg is None:
            # ページドットが残っているなら固定座標で▷続行 (▷が背景同化で検出不能な場合)
            if _total_dots >= 2 and _page < _total_dots - 1 and initial_dlg is not None:
                _dlg = initial_dlg  # 初回の固定座標を再利用
                logger.info("[PAGING] ▷未検出だがドット残(%d/%d) → 固定座標で続行", _page + 1, _total_dots)
            else:
                logger.info("[PAGING] ダイアログ消失 (page=%d) → 完了", _page)
                state.dialog_detections += 1
                return "DIALOG_CLOSED"
        _kind, _dx, _dy = _dlg
        if _kind == "close":
            tap_device(_dx, _dy, state, "PAGING_CLOSE")
            logger.info("[PAGING] ×タップ (page=%d) → クローズ完了", _page + 1)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        # ドット数到達: 最終ページのはず → × を強制探索
        if _total_dots >= 2 and _page >= _total_dots - 1:
            _close_asset = _find_close_by_asset(analysis_path)
            if _close_asset:
                tap_device(_close_asset[0], _close_asset[1], state, "PAGING_CLOSE_DOTS_END")
                logger.info("[PAGING] ドット%d到達 → ×アセット(%d,%d) クローズ",
                            _total_dots, _close_asset[0], _close_asset[1])
                state.dialog_detections += 1
                return "DIALOG_CLOSED"
            # アセットでも見つからない → 右上固定×
            _fx = int(W * 0.975)
            _fy = int(H * 0.055)
            tap_device(_fx, _fy, state, "PAGING_CLOSE_DOTS_FIXED")
            logger.info("[PAGING] ドット%d到達 → 右上固定×(%d,%d) クローズ", _total_dots, _fx, _fy)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        # "next" or "bottom" → ▷ タップして次ページ
        tap_device(_dx, _dy, state, "PAGING_NEXT")
        logger.info("[PAGING] ▷タップ (page=%d/%d, dots=%d)", _page + 1, _max_iter, _total_dots)
        state.dialog_detections += 1
        time.sleep(0.05)
        # 次ページのスクリーンショットを取得して解析
        _img_path, _aw, _ah, _ = take_screenshot()
        analysis_path = prepare_analysis_image(_img_path, _aw, _ah)
        # phash変化監視: 変化なし → ページが進んでいない → × を探す
        try:
            _new_phash = compute_phash(analysis_path)
        except (ValueError, Exception):
            _new_phash = None
        if _prev_phash and _new_phash:
            _ph_dist = phash_distance(_prev_phash, _new_phash)
            if _ph_dist < 4:
                _phash_fail_count += 1
                _phash_tolerance = 3 if _total_dots >= 2 else 2
                if _phash_fail_count >= _phash_tolerance:
                    logger.info(
                        "[PAGING] phash変化なし %d回連続 → ×クローズ試行",
                        _phash_fail_count,
                    )
                    # ▷ が効かない = 最終ページ → × ボタンを探してクローズ
                    _fallback_dlg = detect_dialog_frame_and_nav(
                        analysis_path, W, H, roi=_roi,
                        ocr_texts=ocr_texts,
                    )
                    if _fallback_dlg and _fallback_dlg[0] == "close":
                        tap_device(_fallback_dlg[1], _fallback_dlg[2], state, "PAGING_CLOSE_FALLBACK")
                        logger.info("[PAGING] ×フォールバッククローズ成功")
                        state.dialog_detections += 1
                        return "DIALOG_CLOSED"
                    # ASSET_MANAGER テンプレートで × を直接探す (枠検出不要)
                    _close_asset = _find_close_by_asset(analysis_path)
                    if _close_asset:
                        tap_device(_close_asset[0], _close_asset[1], state, "PAGING_CLOSE_ASSET")
                        logger.info("[PAGING] ×アセットテンプレクローズ成功 (%d,%d)",
                                    _close_asset[0], _close_asset[1])
                        state.dialog_detections += 1
                        return "DIALOG_CLOSED"
                    # × がまだ出ていない → もう少し▷を叩く (アニメーション遅延など)
                    if _phash_fail_count < _phash_tolerance + 3:
                        logger.info("[PAGING] ×未検出 → ▷追加試行 (%d/%d)",
                                    _phash_fail_count, _phash_tolerance + 3)
                        _prev_phash = _new_phash
                        continue
                    return "DIALOG_PAGING_TIMEOUT"
                logger.debug("[PAGING] phash変化小(dist=%d) %d/%d → 続行",
                             _ph_dist, _phash_fail_count, _phash_tolerance)
            else:
                _phash_fail_count = 0
        _prev_phash = _new_phash
    logger.warning("[PAGING] max_iter=%d 超過 → ×クローズ試行", _max_iter)
    # 最終ページ到達後 → × ボタンを探してクローズ
    _final_dlg = detect_dialog_frame_and_nav(
        analysis_path, W, H, roi=_roi,
        ocr_texts=ocr_texts,
    )
    if _final_dlg and _final_dlg[0] == "close":
        tap_device(_final_dlg[1], _final_dlg[2], state, "PAGING_CLOSE_MAXPAGE")
        logger.info("[PAGING] max超過後 ×クローズ成功")
        state.dialog_detections += 1
        return "DIALOG_CLOSED"
    # ASSET_MANAGER テンプレートで × を直接探す (枠検出不要)
    _close_asset = _find_close_by_asset(analysis_path)
    if _close_asset:
        tap_device(_close_asset[0], _close_asset[1], state, "PAGING_CLOSE_ASSET_MAX")
        logger.info("[PAGING] max超過後 ×アセットテンプレクローズ成功 (%d,%d)",
                    _close_asset[0], _close_asset[1])
        state.dialog_detections += 1
        return "DIALOG_CLOSED"
    return "DIALOG_PAGING_TIMEOUT"


# ─── テキスト入力エリア検出 ────────────────────────────────────────────────
def detect_text_input_area(
    img_path: Path,
    W: int = 1520,
    H: int = 720,
    ocr_items: Optional[list] = None,
) -> Optional[tuple[int, int]]:
    """
    テキスト入力エリア（横長の暗い矩形 + 文字数カウンター）を検出してフィールド中心座標を返す。

    検出手順:
    1. OCR で "0/N" パターン（文字数カウンター）を検索 → カウンター位置からフィールド中心を推定
    2. OCR で入力プレースホルダー（"を入力", "Enter" 等）を含む項目を探す
    3. 上記いずれも失敗した場合、HSV で暗い横長矩形を探す

    Returns: (field_cx, field_cy) or None
    """
    # --- 1. OCR 文字数カウンター "0/N" パターン ---
    if ocr_items:
        for _item in ocr_items:
            _txt = _item.get("text", "").strip()
            if re.match(r"^0/\d+$", _txt):
                _cx, _cy = _item["center"]
                # カウンターはフィールド右端にある → フィールド中心は左へ ~13% (200px / 1520)
                return max(0, _cx - int(W * 0.131)), _cy
        # --- 2. プレースホルダーテキスト検出 ---
        for _item in ocr_items:
            _txt = _item.get("text", "").strip()
            if "を入力" in _txt or "Enter" in _txt.lower():
                return _item["center"][0], _item["center"][1]
    # --- 3. HSV 暗い横長矩形 ---
    try:
        _img = imread_cached(img_path)
        if _img is None:
            return None
        _roi_y1, _roi_y2 = int(H * 0.3), int(H * 0.75)
        _roi = _img[_roi_y1:_roi_y2, :]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        # 入力フィールド特有の暗めの背景 (S低め、V中〜低)
        _dark = cv2.inRange(_hsv, np.array([0, 0, 20]), np.array([180, 80, 110]))
        _cnts, _ = cv2.findContours(_dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for _cnt in sorted(_cnts, key=cv2.contourArea, reverse=True)[:8]:
            _x, _y, _w, _h = cv2.boundingRect(_cnt)
            if _w > W * 0.25 and 25 < _h < 100 and _w / max(_h, 1) > 3.5:
                return _x + _w // 2, _roi_y1 + _y + _h // 2
    except Exception as _e:
        logger.debug("detect_text_input_area error: %s", _e)
    return None


# ─── HSV金色チュートリアルポインター検出 → ホールドスワイプ ─────────────
_SWIPE_FINGER_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_swipe_finger.png"


def detect_tutorial_gold_swipe(img_path: Path) -> Optional[tuple[str, int, int, int, int]]:
    """
    チュートリアル移動シーンの金色指アイコン+軌跡を検出しスワイプ方向を返す。

    テンプレートマッチ (指アイコン) + 白い縦軌跡の確認のみで判定。
    HSV フォールバックは廃止 (金色UIとの誤検出防止)。

    Returns: (direction, swipe_x, from_y, to_y, duration_ms) or None
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        if not _SWIPE_FINGER_TEMPLATE.exists():
            return None
        _tpl = imread_cached(_SWIPE_FINGER_TEMPLATE)
        if _tpl is None or img.shape[0] < _tpl.shape[0] or img.shape[1] < _tpl.shape[1]:
            return None
        _r = cv2.matchTemplate(img, _tpl, cv2.TM_CCOEFF_NORMED)
        _, _mv, _, _ml = cv2.minMaxLoc(_r)
        if _mv < 0.75:
            return None
        _th, _tw = _tpl.shape[:2]
        _fx = _ml[0] + _tw // 2  # 指アイコン中心X
        _fy = _ml[1] + _th // 2  # 指アイコン中心Y
        # 指の下方に白い縦軌跡があるかチェック
        _trail_x1 = max(0, _fx - 15)
        _trail_x2 = min(W_img, _fx + 15)
        _trail_y1 = _ml[1] + _th  # 指の下端から
        _trail_y2 = min(H_img, _trail_y1 + 200)  # 200px下まで
        _has_trail = False
        if _trail_y2 > _trail_y1 + 20:
            _trail_roi = img[_trail_y1:_trail_y2, _trail_x1:_trail_x2]
            if _trail_roi.size > 0:
                _gray_t = cv2.cvtColor(_trail_roi, cv2.COLOR_BGR2GRAY)
                _bright = cv2.countNonZero(
                    cv2.threshold(_gray_t, 160, 255, cv2.THRESH_BINARY)[1])
                _has_trail = _bright >= 30
        if not _has_trail:
            logger.debug(
                "[GoldSwipe] テンプレ指検出 score=%.2f (%d,%d) だが軌跡なし → スキップ",
                _mv, _fx, _fy,
            )
            return None
        # 指が上 + 軌跡が下 → SWIPE_UP
        _from_y = min(H_img - 60, _trail_y2 + 50)
        _to_y = max(50, _fy - 80)
        logger.info(
            "[GoldSwipe] テンプレ検出: score=%.2f finger=(%d,%d) trail=%s "
            "→ UP swipe_x=%d from=%d to=%d",
            _mv, _fx, _fy, _has_trail, _fx, _from_y, _to_y,
        )
        return "UP", _fx, _from_y, _to_y, 10000

    except ImportError:
        return None
    except Exception as e:
        logger.debug("detect_tutorial_gold_swipe error: %s", e)
        return None


# ─── 金枠テンプレマッチ (4隅) ─────────────────────────────────────────
_GOLD_CORNER_TL_PATH = _CRAWLER_ROOT / "assets" / "templates" / "gold_frame_corner_tl.png"
_GOLD_CORNER_CACHE: dict = {}


def _load_gold_corners() -> dict:
    """金枠左上テンプレを読み込み、4隅バリエーションを生成してキャッシュ。"""
    if _GOLD_CORNER_CACHE:
        return _GOLD_CORNER_CACHE
    if not _GOLD_CORNER_TL_PATH.exists():
        return {}
    tpl = cv2.imread(str(_GOLD_CORNER_TL_PATH))
    if tpl is None:
        return {}
    _GOLD_CORNER_CACHE["TL"] = cv2.cvtColor(tpl, cv2.COLOR_BGR2GRAY)
    _GOLD_CORNER_CACHE["TR"] = cv2.cvtColor(cv2.flip(tpl, 1), cv2.COLOR_BGR2GRAY)
    _GOLD_CORNER_CACHE["BL"] = cv2.cvtColor(cv2.flip(tpl, 0), cv2.COLOR_BGR2GRAY)
    _GOLD_CORNER_CACHE["BR"] = cv2.cvtColor(cv2.flip(tpl, -1), cv2.COLOR_BGR2GRAY)
    return _GOLD_CORNER_CACHE


def find_gold_frame_by_template(
    img_path: Path, threshold: float = 0.80,
    rect_tolerance: int = 5,
) -> Optional[tuple[int, int, int, int]]:
    """金枠4隅テンプレマッチで金枠を検出する。

    Returns: (center_x, center_y, width, height) or None
    """
    corners = _load_gold_corners()
    if not corners:
        return None
    img = imread_analysis(img_path)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    tpl_h, tpl_w = corners["TL"].shape[:2]

    # 各隅の最良マッチ位置を検出
    matches = {}
    for name, g_tpl in corners.items():
        if gray.shape[0] < g_tpl.shape[0] or gray.shape[1] < g_tpl.shape[1]:
            return None
        result = cv2.matchTemplate(gray, g_tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < threshold:
            return None
        matches[name] = (max_loc[0], max_loc[1], max_val)

    # 矩形整合性チェック
    tl_x, tl_y, _ = matches["TL"]
    tr_x, tr_y, _ = matches["TR"]
    bl_x, bl_y, _ = matches["BL"]
    br_x, br_y, _ = matches["BR"]

    # TL-TR: 水平 (y差が小さい)
    if abs(tl_y - tr_y) > rect_tolerance:
        return None
    # BL-BR: 水平
    if abs(bl_y - br_y) > rect_tolerance:
        return None
    # TL-BL: 垂直 (x差が小さい)
    if abs(tl_x - bl_x) > rect_tolerance:
        return None
    # TR-BR: 垂直
    if abs(tr_x - br_x) > rect_tolerance:
        return None
    # 幅・高さが正の値
    frame_w = (tr_x + tpl_w) - tl_x
    frame_h = (bl_y + tpl_h) - tl_y
    if frame_w < 10 or frame_h < 10:
        return None

    cx = tl_x + frame_w // 2
    cy = tl_y + frame_h // 2
    scores = [matches[k][2] for k in ("TL", "TR", "BL", "BR")]
    logger.debug("[GoldFrame:TMPL] 4隅検出 TL(%d,%d) BR(%d,%d) %dx%d scores=%.2f/%.2f/%.2f/%.2f",
                 tl_x, tl_y, br_x, br_y, frame_w, frame_h, *scores)
    return cx, cy, frame_w, frame_h


# ─── Type B: 金枠ハイライトボタン検出 → 中心タップ ─────────────────────
def find_gold_button(img_path: Path, **_kwargs) -> Optional[tuple[int, int, str]]:
    """
    チュートリアルの「金枠ハイライトボタン」をテンプレマッチ (4隅) で検出。

    Returns: (tap_x, tap_y, "TMPL") or None
    """
    try:
        _tmpl_result = find_gold_frame_by_template(img_path)
        if _tmpl_result:
            _tcx, _tcy, _tw, _th = _tmpl_result
            logger.debug("[GoldBtn:TMPL] 検出OK: (%d,%d) %dx%d → tap(%d,%d)",
                         _tcx - _tw // 2, _tcy - _th // 2, _tw, _th, _tcx, _tcy)
            return _tcx, _tcy, "TMPL"
        return None
    except Exception as e:
        logger.debug("find_gold_button error: %s", e)
        return None


# ─── チュートリアルオーバーレイ（暗転）検出 ──────────────────────────


def detect_tutorial_overlay(img_path: Path, brightness_threshold: int = 90) -> bool:
    """チュートリアル中の暗転オーバーレイを検出する。

    チュートリアル時は指アイコン+金枠のハイライト以外が半透明の暗いオーバーレイで覆われる。
    2段階判定:
      方式1: 画面全体の中央値輝度が低い (< brightness_threshold)
      方式2: 四隅のうち2つ以上が暗い (mean < 80) — ハイライト部分の輝度に影響されない

    Returns: True = 暗転オーバーレイあり（チュートリアル中の可能性が高い）
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        H, W = gray.shape[:2]
        median_brightness = int(np.median(gray))
        # 方式1: 中央値輝度
        if median_brightness < brightness_threshold:
            logger.debug("[TutOverlay] 暗転検出 (median=%d < %d)",
                         median_brightness, brightness_threshold)
            return True
        # 方式2: 四隅の暗さ (ハイライト対象が明るくても隅は暗い)
        # 閾値30: チュートリアル暗転時は隅が < 10 レベル。通常ホーム画面の
        # フッターナビ背景 (38-57) を誤検出しないよう設定
        sz = 40
        corners = [
            gray[0:sz, 0:sz],           # TL
            gray[0:sz, W - sz:W],       # TR
            gray[H - sz:H, 0:sz],       # BL
            gray[H - sz:H, W - sz:W],   # BR
        ]
        dark_count = sum(1 for c in corners if c.mean() < 30)
        if dark_count >= 2:
            logger.debug("[TutOverlay] 暗転検出 (dark_corners=%d/4, median=%d)",
                         dark_count, median_brightness)
            return True
        logger.debug("[TutOverlay] 暗転なし (median=%d, dark_corners=%d/4)",
                     median_brightness, dark_count)
        return False
    except Exception as e:
        logger.debug("detect_tutorial_overlay error: %s", e)
        return False


# ─── Smart Tap: 金色ボタン矩形の幾何学的中心を検出 ──────────────────


def smart_tap_button(
    img_path: Path,
    ocr_cx: int,
    ocr_cy: int,
    search_r: int = 120,
    ocr_items: list[dict] | None = None,
) -> tuple[int, int]:
    """Text-Core 対応 SmartTap: 金枠テンプレートマッチでボタンを検出し、テキスト中心優先でタップ座標を返す。

    1. OCR 中心周辺から gold_frame_small テンプレートでボタン枠を検出
    2. 見つかったらテンプレート位置からボタン領域を推定し text_core_center() で座標を返す
    3. 見つからない場合は OCR 座標をそのまま返す

    返値: (tap_x, tap_y)
    """
    try:
        # gold_frame_small テンプレートで OCR 中心周辺を検索
        _roi = (max(0, ocr_cx - search_r), max(0, ocr_cy - search_r),
                search_r * 2, search_r * 2)
        _gf = ASSET_MANAGER.match_single("gold_frame_small", img_path, roi=_roi)
        if _gf and _gf[2] >= 0.70:
            # テンプレートはコーナーなので、ボタン領域をコーナーから推定
            # コーナーの右下方向にボタン本体がある → OCR テキストを含む領域を探索
            _btn_rect = (_gf[0] - 10, _gf[1] - 10, search_r, search_r // 2)
            _tc = text_core_center(_btn_rect, ocr_items or [], label="SmartTap")
            logger.debug("[SmartTap] gold_frame_small(%.2f) → TextCore(%d,%d)", _gf[2], _tc[0], _tc[1])
            return _tc

    except Exception as e:
        logger.debug("  [SmartTap] エラー: %s", e)

    # フォールバック: OCR 座標をそのまま使用
    logger.debug("[SmartTap] fallback OCR-direct (%d,%d)", ocr_cx, ocr_cy)
    return ocr_cx, ocr_cy


# ─── OCR テキスト検索ヘルパー ──────────────────────
# ─── 探索マップ 3D矢印 検出 ──────────────────────────
def find_3d_arrow(img_path: Path) -> Optional[tuple[int, int]]:
    """
    探索マップ上のキャラ頭上に浮かぶ3D矢印（白い曲線矢印）を検出。
    明るい白色コンターが最大のものを矢印とみなす。
    Returns: (cx, cy) or None
    """
    try:
        img = imread_analysis(img_path)
        if img is None:
            return None
        # キャラ頭上エリア (ANALYSIS 座標)
        roi_y1, roi_y2 = _CHAR_HEAD_Y1, _CHAR_HEAD_Y2
        roi_x1, roi_x2 = _CHAR_HEAD_X1, _CHAR_HEAD_X2
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
        count = 0
        for png in sorted(self.TEMPLATES_DIR.glob("*.png")):
            name = png.stem
            img = imread_cached(png, cv2.IMREAD_GRAYSCALE)
            if img is None:
                continue
            meta: dict = {}
            meta_path = png.with_suffix(".json")
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                except Exception:
                    pass
            # edge_weight: エッジマッチング重み (0.0=ピクセルのみ, 1.0=エッジのみ)
            # 背景依存の偽陽性が多いテンプレートに有効
            _ew = float(meta.get("edge_weight", 0.0))
            _edge_img = cv2.Canny(img, 50, 150) if _ew > 0 else None
            # scenes: このテンプレートが有効なシーン。JSON で指定可能。
            # 未指定時はテンプレート名から自動推定。
            _scenes = meta.get("scenes")
            if _scenes is None:
                if name.startswith("battle_"):
                    _scenes = ["BATTLE"]
                elif name.startswith("dialog_") or name.startswith("tutorial_dialog"):
                    _scenes = ["DIALOG"]
                elif name.startswith("movie_"):
                    _scenes = ["MOVIE"]
                else:
                    _scenes = []  # 空 = 全シーン対象
            self._templates[name] = {
                "img": img,
                "edge_img": _edge_img,
                "edge_weight": _ew,
                "threshold": float(meta.get("threshold", self.DEFAULT_THRESHOLD)),
                "action": meta.get("action", f"ASSET_{name.upper()}"),
                "offset": meta.get("offset", [0, 0]),
                "require_ocr": meta.get("require_ocr", []),
                "require_ocr_all": meta.get("require_ocr_all", []),
                "scenes": _scenes,
            }
            count += 1
        if count:
            logger.info("[AssetManager] %d テンプレート読込: %s",
                        count, list(self._templates.keys()))

    def match(self, screenshot_path: Path,
              ocr_texts: Optional[list[str]] = None,
              scene: str = "",
              ) -> Optional[tuple[int, int, str, tuple[int, int, int, int]]]:
        """
        スクリーンショットとテンプレートを比較。
        scene が指定された場合、そのシーンに該当するテンプレートのみ照合。
        ocr_texts が渡された場合、require_ocr 条件を満たすテンプレートのみ照合。
        Returns: (tap_x, tap_y, action_name, button_region) or None
            button_region = (bx, by, bw, bh) — テンプレートマッチ領域
        """
        if not self._templates:
            return None
        _color = imread_analysis(screenshot_path)
        if _color is None:
            return None
        img = cv2.cvtColor(_color, cv2.COLOR_BGR2GRAY)
        best_score = 0.0
        best_result: Optional[tuple[int, int, str, tuple[int, int, int, int]]] = None
        for name, data in self._templates.items():
            if name in _SINGLE_ONLY:
                continue
            # シーンフィルタ: テンプレートの対象シーンに一致しない場合スキップ
            _tmpl_scenes = data.get("scenes", [])
            if scene and _tmpl_scenes and scene not in _tmpl_scenes:
                continue
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
            # tutorial_hand_pointer: 4方向回転でマッチング (上下左右の指を検出)
            _rotations = (self._FINGER_ROTATIONS
                          if name == "tutorial_hand_pointer"
                          else [(None, None)])
            for _rot_dir, _rot_code in _rotations:
                _tmpl = cv2.rotate(tmpl, _rot_code) if _rot_code is not None else tmpl
                if _tmpl.shape[0] > img.shape[0] or _tmpl.shape[1] > img.shape[1]:
                    continue
                try:
                    res = cv2.matchTemplate(img, _tmpl, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    # エッジ重みスコアリング: edge_weight > 0 なら形状重視
                    _ew = data["edge_weight"]
                    if _ew > 0 and data["edge_img"] is not None:
                        _img_edge = cv2.Canny(img, 50, 150)
                        _res_e = cv2.matchTemplate(_img_edge, data["edge_img"], cv2.TM_CCOEFF_NORMED)
                        _, _ev, _, _ = cv2.minMaxLoc(_res_e)
                        max_val = (1 - _ew) * max_val + _ew * _ev
                    if max_val >= data["threshold"] and max_val > best_score:
                        best_score = max_val
                        h, w = _tmpl.shape
                        bx = max_loc[0] + int(data["offset"][0])
                        by = max_loc[1] + int(data["offset"][1])
                        cx = bx + w // 2
                        cy = by + h // 2
                        best_result = (cx, cy, data["action"], (bx, by, w, h))
                        logger.debug("[Asset] '%s' score=%.3f at (%d,%d) rot=%s",
                                     name, max_val, cx, cy, _rot_dir)
                except Exception as e:
                    logger.debug("[Asset] match error '%s': %s", name, e)
        if best_result:
            cx, cy, action, _ = best_result
            logger.info("[Asset] HIT: '%s' score=%.3f → (%d,%d)", action, best_score, cx, cy)
        return best_result

    def match_single(self, name: str, screenshot_path: Path,
                     roi: Optional[tuple[int, int, int, int]] = None,
                     ) -> Optional[tuple[int, int, float]]:
        """指定テンプレート1枚だけをマッチング。Returns (cx, cy, score) or None.

        roi: (x, y, w, h) — 検索領域を制限。座標は ANALYSIS_W x ANALYSIS_H 基準で返す。
        画像は ANALYSIS サイズにリサイズしてからマッチング (テンプレートと解像度を統一)。
        """
        data = self._templates.get(name)
        if data is None:
            return None
        _color = imread_analysis(screenshot_path)
        if _color is None:
            return None
        img = cv2.cvtColor(_color, cv2.COLOR_BGR2GRAY)
        if img is None:
            return None
        # ROI 切り出し (指定時)
        _roi_ox, _roi_oy = 0, 0
        if roi is not None:
            _rx, _ry, _rw, _rh = roi
            img = img[_ry:_ry + _rh, _rx:_rx + _rw]
            _roi_ox, _roi_oy = _rx, _ry
        tmpl = data["img"]
        if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
            return None
        try:
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            # エッジ重みスコアリング
            _ew = data["edge_weight"]
            if _ew > 0 and data["edge_img"] is not None:
                _img_edge = cv2.Canny(img, 50, 150)
                _tmpl_edge = data["edge_img"]
                if roi is not None:
                    _tmpl_edge_h, _tmpl_edge_w = _tmpl_edge.shape[:2]
                    if _tmpl_edge_h <= _img_edge.shape[0] and _tmpl_edge_w <= _img_edge.shape[1]:
                        _img_edge = _img_edge[_ry:_ry + _rh, _rx:_rx + _rw]
                if _tmpl_edge.shape[0] <= _img_edge.shape[0] and _tmpl_edge.shape[1] <= _img_edge.shape[1]:
                    _res_e = cv2.matchTemplate(_img_edge, _tmpl_edge, cv2.TM_CCOEFF_NORMED)
                    _, _ev, _, _ = cv2.minMaxLoc(_res_e)
                    max_val = (1 - _ew) * max_val + _ew * _ev
            if max_val >= data["threshold"]:
                h, w = tmpl.shape
                cx = max_loc[0] + w // 2 + _roi_ox
                cy = max_loc[1] + h // 2 + _roi_oy
                return (cx, cy, max_val)
        except Exception:
            pass
        return None

    # ─── 回転指テンプレマッチ ───
    _FINGER_ROTATIONS: list[tuple[str, Optional[int]]] = [
        ("down", None),                        # tutorial_hand_pointer は下向き
        ("up", cv2.ROTATE_180),
        ("left", cv2.ROTATE_90_CLOCKWISE),
        ("right", cv2.ROTATE_90_COUNTERCLOCKWISE),
    ]

    # マスク付きマッチ用の閾値 (テンプレの白い手部分を抽出)
    _FINGER_MASK_THRESH = 140

    def match_finger_rotated(
        self, screenshot_path: Path,
        threshold: float = 0.70,
    ) -> Optional[tuple[int, int, float, str]]:
        """tutorial_hand_pointer を4方向回転してマッチング。

        1. マスク付きマッチ (TM_CCOEFF_NORMED + 白手マスク): 背景の影響を排除
        2. 通常マッチ (TM_CCOEFF_NORMED): フォールバック

        Returns: (cx, cy, score, direction) or None
            direction: "up" / "down" / "left" / "right"
        """
        data = self._templates.get("tutorial_hand_pointer")
        if data is None:
            return None
        _color = imread_analysis(screenshot_path)
        if _color is None:
            return None
        img = cv2.cvtColor(_color, cv2.COLOR_BGR2GRAY)
        base_tmpl = data["img"]

        # --- Phase 1: マスク付きマッチ (白い手の形状のみで判定) ---
        best: Optional[tuple[int, int, float, str]] = None
        for direction, rot_code in self._FINGER_ROTATIONS:
            tmpl = cv2.rotate(base_tmpl, rot_code) if rot_code is not None else base_tmpl
            if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            try:
                _, mask = cv2.threshold(tmpl, self._FINGER_MASK_THRESH, 255, cv2.THRESH_BINARY)
                res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED, mask=mask)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if not np.isfinite(max_val):
                    continue
                if max_val >= threshold and (best is None or max_val > best[2]):
                    h, w = tmpl.shape
                    cx = max_loc[0] + w // 2
                    cy = max_loc[1] + h // 2
                    best = (cx, cy, max_val, direction)
            except Exception:
                pass
        if best is not None:
            return best

        # --- Phase 2: 通常マッチ (フォールバック) ---
        for direction, rot_code in self._FINGER_ROTATIONS:
            tmpl = cv2.rotate(base_tmpl, rot_code) if rot_code is not None else base_tmpl
            if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            try:
                res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold and (best is None or max_val > best[2]):
                    h, w = tmpl.shape
                    cx = max_loc[0] + w // 2
                    cy = max_loc[1] + h // 2
                    best = (cx, cy, max_val, direction)
            except Exception:
                pass
        return best

    def match_best_in_roi(self, screenshot_path: Path,
                         roi: tuple[int, int, int, int],
                         threshold: float = 0.65,
                         ) -> Optional[tuple[int, int, float, str]]:
        """ROI 内で全テンプレートを検索し、最高スコアの結果を返す。

        Returns: (cx, cy, score, template_name) or None
            座標は元画像基準。
        """
        _color = imread_analysis(screenshot_path)
        if _color is None:
            return None
        img = cv2.cvtColor(_color, cv2.COLOR_BGR2GRAY)
        _rx, _ry, _rw, _rh = roi
        _roi_img = img[max(0, _ry):min(img.shape[0], _ry + _rh),
                       max(0, _rx):min(img.shape[1], _rx + _rw)]
        if _roi_img.size == 0:
            return None
        _best_score = 0.0
        _best: Optional[tuple[int, int, float, str]] = None
        for name, data in self._templates.items():
            tmpl = data["img"]
            if tmpl.shape[0] > _roi_img.shape[0] or tmpl.shape[1] > _roi_img.shape[1]:
                continue
            try:
                res = cv2.matchTemplate(_roi_img, tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold and max_val > _best_score:
                    _best_score = max_val
                    h, w = tmpl.shape
                    cx = max(0, _rx) + max_loc[0] + w // 2
                    cy = max(0, _ry) + max_loc[1] + h // 2
                    _best = (cx, cy, max_val, name)
            except Exception:
                pass
        return _best

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
        img = imread_cached(screenshot_path)
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



# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


# ─── Result画面ハンドラ ──────────────────────────────
_RESULT_NEXT_X_RATIO = 0.785
_RESULT_NEXT_Y_RATIO = 0.914

# パーティ編成画面の除外キーワード (Lv.1 が出るが Result ではない)
_FORMATION_KWS = ["パーティ", "編成", "キオク", "ポートレイト", "自動編成"]


# ═══════════════════════════════════════════════════════════════════
#  detect_login_bonus_popup — ログインボーナスポップアップ検出
#
#  エッジ投影 (Sobel) で画面上の大型矩形の枠線を検出し、
#  画面占有率 + 背景ぼかし + close_btn テンプレで判定する。
#  四隅テンプレ / ページドット / OCR に依存しない汎用検出。
# ═══════════════════════════════════════════════════════════════════

# エッジ投影ピーク検出の最小ピーク強度 (最大値に対する比率)
_LBP_PEAK_RATIO = 0.25
# ピーク間の最小距離 (px) — 近接ピークを除外
_LBP_PEAK_MIN_DIST = 80
# 検出矩形の最小画面占有率
_LBP_MIN_SCREEN_RATIO = 0.35


def _find_projection_peaks(
    projection: np.ndarray, min_ratio: float, min_dist: int,
) -> list[int]:
    """1次元投影からピーク位置を返す (scipy 不要)。"""
    _max_val = projection.max()
    _thresh = _max_val * min_ratio
    # 単純ピーク検出: 前後より大きい & 閾値以上
    _peaks: list[int] = []
    for i in range(1, len(projection) - 1):
        if (projection[i] > projection[i - 1]
                and projection[i] >= projection[i + 1]
                and projection[i] >= _thresh):
            _peaks.append(i)
    # 近接ピーク除去: 強度が高い方を残す
    if not _peaks:
        return _peaks
    _filtered: list[int] = [_peaks[0]]
    for p in _peaks[1:]:
        if p - _filtered[-1] < min_dist:
            if projection[p] > projection[_filtered[-1]]:
                _filtered[-1] = p
        else:
            _filtered.append(p)
    return _filtered


def detect_login_bonus_popup(
    img_path: Path,
) -> Optional[dict]:
    """ログインボーナスポップアップを検出する。

    Sobel エッジ投影で大型矩形の枠線を検出し、
    画面占有率 ≥ 35% + 背景ぼかし + close_btn テンプレの3条件で判定。

    Returns:
      {"rect": (left, top, right, bottom), "screen_ratio": float,
       "close_btn": (cx, cy, score) | None}
      or None (未検出)
    """
    _img = imread_cached(img_path)
    if _img is None:
        return None
    _H, _W = _img.shape[:2]
    _gray = cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY) if len(_img.shape) == 3 else _img

    # ── Sobel エッジ投影 ──
    _sobel_x = cv2.Sobel(_gray, cv2.CV_64F, 1, 0, ksize=3)
    _sobel_y = cv2.Sobel(_gray, cv2.CV_64F, 0, 1, ksize=3)
    _vert_proj = np.mean(np.abs(_sobel_x), axis=0)   # 垂直エッジ → 左右枠線
    _horiz_proj = np.mean(np.abs(_sobel_y), axis=1)   # 水平エッジ → 上下枠線

    _v_peaks = _find_projection_peaks(_vert_proj, _LBP_PEAK_RATIO, _LBP_PEAK_MIN_DIST)
    _h_peaks = _find_projection_peaks(_horiz_proj, _LBP_PEAK_RATIO, _LBP_PEAK_MIN_DIST)

    if len(_v_peaks) < 2 or len(_h_peaks) < 1:
        return None

    # 最外の左右ピーク = 枠線候補
    _left = _v_peaks[0]
    _right = _v_peaks[-1]
    _top = _h_peaks[0]
    _bottom = _h_peaks[-1] if len(_h_peaks) >= 2 else _H

    _rect_w = _right - _left
    _rect_h = _bottom - _top
    if _rect_w <= 0 or _rect_h <= 0:
        return None

    _screen_ratio = (_rect_w * _rect_h) / (_W * _H)

    if _screen_ratio < _LBP_MIN_SCREEN_RATIO:
        logger.debug("[LOGIN_BONUS] 面積比 %.1f%% < %.0f%% → 棄却",
                     _screen_ratio * 100, _LBP_MIN_SCREEN_RATIO * 100)
        return None

    # ── 背景ぼかし確認 ──
    if not detect_background_blur(_img, _H, _W):
        logger.debug("[LOGIN_BONUS] 背景ぼかし未検出 → 棄却")
        return None

    # ── close_btn テンプレマッチ (必須) ──
    _close_match = ASSET_MANAGER.match_single("close_btn", img_path)
    _close_info = None
    if _close_match and _close_match[2] >= 0.50:
        _close_info = (_close_match[0], _close_match[1], _close_match[2])

    if _close_info is None:
        logger.debug("[LOGIN_BONUS] close_btn 未検出 → 棄却 (面積比=%.1f%%)",
                     _screen_ratio * 100)
        return None

    logger.info(
        "[LOGIN_BONUS] 検出: rect=(%d,%d)-(%d,%d) 面積比=%.1f%% close_btn=(%d,%d score=%.2f)",
        _left, _top, _right, _bottom,
        _screen_ratio * 100,
        _close_info[0], _close_info[1], _close_info[2],
    )

    return {
        "rect": (_left, _top, _right, _bottom),
        "screen_ratio": _screen_ratio,
        "close_btn": _close_info,
    }


