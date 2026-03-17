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
    _DEBUG_SAVE_IMAGES, _DIALOG_FIRST_KWS,
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

    判定: 画像全体の平均彩度が非常に低い (< 25) → ほぼモノクロ。
    このパターンはチュートリアル冒頭の歩行シーンに固有。
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        mean_sat = float(hsv[:, :, 1].mean())
        return mean_sat < 25
    except Exception:
        return False


def is_dark_screen(img_path: Path) -> bool:
    """暗転判定 — 中央60%領域の 90th percentile 輝度で判定。

    黒帯除外のため中央領域のみ使用。平均値ではなく 90th percentile を
    使うことで、暗い背景+UIの画面 (p90≈58) と真の暗転 (p90≈2) を区別する。
    """
    try:
        from PIL import Image
        with Image.open(img_path) as img:
            gray = np.array(img.convert("L"))
            h, w = gray.shape
            y0, y1 = int(h * 0.2), int(h * 0.8)
            x0, x1 = int(w * 0.2), int(w * 0.8)
            return float(np.percentile(gray[y0:y1, x0:x1], 90)) <= BLACKOUT_BRIGHTNESS
    except Exception:
        return False


def prepare_analysis_image(img_path: Path, actual_w: int, actual_h: int) -> Path:
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


# ─── 指差しアイコン (肌色ブロブ) 検出 ──────────────
def find_finger_blobs(img_path: Path, min_area: int = 400,
                      max_area: int = 15000,
                      dark_mode: bool = False,
                      home_mode: bool = False) -> list[tuple[int, int, float, int, int, int, int]]:
    """
    指差しアイコン（肌色）の大きいブロブを検出。
    battle_loop.py と同じ HSV マスク手法。
    max_area: 金色カード等の大面積誤検出を除外（UI カードは 15000px² 超）
    dark_mode: バトル背景など暗い状況では輝度閾値を緩和（V:150→100, S:40→25）
    home_mode: ホーム画面では OVERSIZED_RESCUE を無効化 (装飾の誤検出防止)
    返値: [(cx, cy, area, bbox_x, bbox_y, bbox_w, bbox_h), ...] 面積降順
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return []
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # dark_mode: バトル暗背景向けに輝度・彩度閾値を緩和
        if dark_mode:
            lower = np.array([5, 25, 100])
        else:
            lower = np.array([5, 40, 150])
        upper = np.array([25, 180, 255])
        mask = cv2.inRange(hsv, lower, upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        img_h_fb = img.shape[0]
        blobs = []
        global _rejected_finger_blobs
        _rejected_finger_blobs = []  # 毎回リセット
        # max_area 超のブロブを一時保存 (後で金枠チェックで救済する候補)
        _oversized: list[tuple] = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area:
                continue
            if area > max_area:
                # 指アイコン+隣接ゴールドUIが融合した巨大ブロブ候補 → 一時保存
                M_ov = cv2.moments(c)
                if M_ov["m00"] > 0:
                    _ov_cx = int(M_ov["m10"] / M_ov["m00"])
                    _ov_cy = int(M_ov["m01"] / M_ov["m00"])
                    _ov_bx, _ov_by, _ov_bw, _ov_bh = cv2.boundingRect(c)
                    _oversized.append((_ov_cx, _ov_cy, area, _ov_bx, _ov_by, _ov_bw, _ov_bh))
                continue
            M = cv2.moments(c)
            if M["m00"] <= 0:
                continue
            cx = int(M["m10"] / M["m00"])
            cy = int(M["m01"] / M["m00"])
            bx, by, bw, bh = cv2.boundingRect(c)

            # ── 【形状検証 1】Solidity（充填率）チェック ───────────────────────
            # 蝶の王冠/トゲトゲ形状は solidity 低い。指アイコンは輪郭が滑らかで高い。
            hull = cv2.convexHull(c)
            hull_area = cv2.contourArea(hull)
            solidity = area / hull_area if hull_area > 0 else 0.0
            if solidity < 0.35:
                _rejected_finger_blobs.append((cx, cy, "SHAPE(sol=%.2f)" % solidity))
                continue

            # ── 【形状検証 2】アスペクト比チェック ─────────────────────────────
            # 指アイコンは概ね 0.28〜3.5 の範囲。過度に横長な蝶の羽を排除。
            asp = bw / bh if bh > 0 else 1.0
            if asp > 3.5 or asp < 0.28:
                _rejected_finger_blobs.append((cx, cy, "SHAPE(asp=%.1f)" % asp))
                continue

            # ── 【空間的バイアス 3】バトル(dark_mode)上部30%の小面積ブロブ排除 ────
            # 蝶エネミーは上部(バトルフィールド)に出現、チュートリアル指は下部UIに出現
            if dark_mode and cy < img_h_fb * 0.30 and area < 1500:
                _rejected_finger_blobs.append((cx, cy, "SPATIAL(y=%d,area=%.0f)" % (cy, area)))
                logger.info("[REJECTED: SPATIAL] (%d,%d) 上部30%%内 area=%.0f<1500 → エネミー誤検出排除",
                            cx, cy, area)
                continue

            blobs.append((cx, cy, area, bx, by, bw, bh))
        # ── 大面積ブロブ救済: 近傍に金枠があれば指+ゴールドUI融合と判定して採用 ──
        # 通常ブロブの有無に関わらず、金枠付き大面積ブロブは常に最優先で挿入
        # home_mode: 装飾 (area >= 40000) は引き続き排除。20000-39999 は金枠付きなら許可
        # max_area < デフォルト(15000): 呼び出し元が明示的にサイズ制限 → RESCUE 不要
        if _oversized and max_area >= 15000:
            # 絶対上限: area >= 100000 (画面の9%超) は指ではありえない
            _OVERSIZED_ABS_MAX = 100000
            for _ov in _oversized:
                if _ov[2] >= _OVERSIZED_ABS_MAX:
                    logger.info("[FINGER_OVERSIZED_SKIP] (%d,%d) area=%.0f >= %d → 巨大すぎて除外",
                                _ov[0], _ov[1], _ov[2], _OVERSIZED_ABS_MAX)
                    continue
                # home_mode: area >= 40000 は装飾確定 → 金枠があっても排除
                if home_mode and _ov[2] >= 40000:
                    logger.info("[FINGER_OVERSIZED_SKIP] (%d,%d) area=%.0f >= 40000 (home_mode) → 装飾除外",
                                _ov[0], _ov[1], _ov[2])
                    continue
                _gf = find_gold_frame_near(img_path, _ov[0], _ov[1], search_radius=200)
                if _gf is not None:
                    logger.info("[FINGER_OVERSIZED_RESCUE] (%d,%d) area=%.0f + 金枠(%d,%d) → 採用",
                                _ov[0], _ov[1], _ov[2], _gf[0], _gf[1])
                    blobs.insert(0, _ov)  # 最優先 (先頭に挿入)
                    break  # 最初の1件で十分
        return sorted(blobs, key=lambda b: b[2], reverse=True)
    except ImportError:
        return []
    except Exception as e:
        logger.debug("find_finger_blobs error: %s", e)
        return []


def detect_white_hand_pointer(
    img_path: Path, threshold: float = 0.85
) -> Optional[tuple[int, int, float, str]]:
    """
    白いハンドポインタ（home_nav_finger / home_nav_finger_up）をテンプレートマッチングで検出。
    find_finger_blobs() が HSV 肌色のみ対象で白ポインタを見逃す問題を補完。
    Returns: (cx, cy, score, direction) or None
        direction: "down" (home_nav_finger) / "up" (home_nav_finger_up)
    """
    try:
        img = imread_cached(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        templates_dir = _CRAWLER_ROOT / "assets" / "templates"
        best: Optional[tuple[int, int, float, str]] = None
        _dir_map = {"home_nav_finger": "down", "home_nav_finger_up": "up",
                    "tutorial_hand_pointer": "up"}
        for name in ("home_nav_finger", "home_nav_finger_up", "tutorial_hand_pointer"):
            tpl_path = templates_dir / f"{name}.png"
            if not tpl_path.exists():
                continue
            tmpl = imread_cached(tpl_path, cv2.IMREAD_GRAYSCALE)
            if tmpl is None or tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= threshold and (best is None or max_val > best[2]):
                h, w = tmpl.shape
                best = (max_loc[0] + w // 2, max_loc[1] + h // 2, max_val, _dir_map[name])
        if best:
            logger.info("[WHITE_HAND] 白ハンドポインタ検出 (%d,%d) score=%.3f dir=%s",
                        best[0], best[1], best[2], best[3])
        return best
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


def find_gold_frame_near(img_path: Path, cx: int, cy: int,
                         search_radius: int = 150) -> Optional[tuple[int, int, int, int]]:
    """
    指アイコン中心(cx,cy)の近傍150px以内で金枠（装飾ボタン枠）を検索。
    スワイプポインター（縦長細い）は除外し、ボタン形状の金枠を返す。
    Returns: (frame_cx, frame_cy, frame_w, frame_h) or None
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        H_img, W_img = img.shape[:2]
        x1 = max(0, cx - search_radius)
        y1 = max(0, cy - search_radius)
        x2 = min(W_img, cx + search_radius)
        y2 = min(H_img, cy + search_radius)
        roi = img[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)
        k5 = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k5)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best = None
        best_area = 0
        for c in contours:
            area = cv2.contourArea(c)
            if area < 3000:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < 60:
                continue
            aspect = w / max(h, 1)
            if not (0.3 < aspect < 5.5):
                continue
            # スワイプポインター（縦長細い: h>w*3.5 かつ w<100）は除外
            if h > w * 3.5 and w < 100:
                continue
            if area > best_area:
                best_area = area
                frame_cx = x1 + x + w // 2
                frame_cy = y1 + y + h // 2
                best = (frame_cx, frame_cy, w, h)
        return best
    except Exception as e:
        logger.debug("find_gold_frame_near error: %s", e)
        return None



def detect_adv_advance_icon(img_path: Path,
                             roi_x: int = int(ANALYSIS_W * 0.875),
                             roi_y: int = int(ANALYSIS_H * 0.847),
                             roi_w: int = int(ANALYSIS_W * 0.112),
                             roi_h: int = int(ANALYSIS_H * 0.125),
                             min_bright: int = 20,
                             max_bright: int = 500) -> bool:
    """
    ADV送り待ちアイコン（◆/▼）を検出。
    テキストボックス右下 ROI 内に孤立した明るい小クラスターを探す。

    ROI デフォルト: x=1330-1500, y=610-700 (landscape 1520x720)
    明るい白/淡色ピクセル: HSV V>210, S<60 が min_bright〜max_bright 個 → True
    max_bright で大量の白テキスト (利用規約画面等) を排除。
    """
    try:
        _img = imread_cached(img_path)
        if _img is None:
            return False
        _H, _W = _img.shape[:2]
        _x1 = max(0, roi_x)
        _y1 = max(0, roi_y)
        _x2 = min(_W, roi_x + roi_w)
        _y2 = min(_H, roi_y + roi_h)
        if _x2 <= _x1 or _y2 <= _y1:
            return False
        _roi = _img[_y1:_y2, _x1:_x2]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        _mask = cv2.inRange(_hsv, (0, 0, 210), (180, 60, 255))
        _bright = int(cv2.countNonZero(_mask))
        if _bright >= min_bright and _bright <= max_bright:
            logger.debug("[ADV_ADVANCE] 明るいピクセル %d 個 @ ROI(%d,%d,%d,%d)",
                         _bright, roi_x, roi_y, roi_w, roi_h)
            return True
        if _bright > max_bright:
            logger.debug("[ADV_ADVANCE] 白テキスト排除: bright=%d > max=%d", _bright, max_bright)
        # HSV 失敗時: テンプレートマッチフォールバック
        try:
            _tmpl = ASSET_MANAGER.match_single("adv_next_btn", img_path,
                                                roi=(roi_x, roi_y, roi_w, roi_h))
            if _tmpl and _tmpl[2] >= 0.65:
                logger.debug("[ADV_ADVANCE] テンプレートFB: score=%.3f → True", _tmpl[2])
                return True
        except Exception:
            pass
        return False
    except Exception as _e:
        logger.debug("detect_adv_advance_icon error: %s", _e)
        return False


# ─── ADV ツールバー: 5個別アイコン名 ──────────────────────────────
_ADV_TOOLBAR_ICON_NAMES = (
    "adv_icon_menu", "adv_icon_log", "adv_icon_auto",
    "adv_icon_ff", "adv_icon_skip",
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
    # ADV固有アイコン判定: AUTO(2)/FF(3)/SKIP(4) — menu/log は汎用UIで偽陽性多い
    _has_adv_specific = any(
        _icon_scores[i] >= icon_threshold
        for i in (2, 3, 4) if i < len(_icon_scores)
    )
    # バトル画面との区別: menu(0)/log(1)/skip(4) はバトルに存在しないADV専用アイコン
    # AUTO(2)/FF(3) はバトルにも存在するため区別に使えない
    _has_adv_only = any(
        _icon_scores[i] >= 0.40
        for i in (0, 1, 4) if i < len(_icon_scores)
    )
    # ↓ボタン検出 (AUTO あり or ADV固有アイコン含む2アイコン救済時のみ実行)
    _has_advance_icon = False
    if _has_auto or (_matched_count >= 2 and _matched_count < 3 and _has_adv_specific):
        _has_advance_icon = detect_adv_advance_icon(img_path)
    # 判定: 3アイコン | 2アイコン(ADV固有含む)+↓ | AUTO+↓+ADV専用アイコン
    # NOTE: AUTO+↓ だけではバトル画面でも成立するため、ADV専用アイコンを要求
    _all_matched = (_matched_count >= 3
                    or (_matched_count >= 2 and _has_advance_icon and _has_adv_only)
                    or (_has_auto and _has_advance_icon and _has_adv_only))
    result.toolbar_score = min(_icon_scores) if _icon_scores else 0.0
    if _all_matched and _icon_scores:
        # toolbar_pos = AUTO アイコンの位置 (3番目)
        try:
            _auto_m = ASSET_MANAGER.match_single("adv_icon_auto", img_path)
            if _auto_m:
                result.toolbar_pos = (_auto_m[0], _auto_m[1])
        except Exception:
            pass

    # --- 2. ↓ボタン ---
    try:
        next_match = ASSET_MANAGER.match_single("adv_next_btn", img_path)
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
    返り値: (cx, cy) or None
    """
    try:
        _img = imread_cached(img_path)
        if _img is None:
            return None
        _H, _W = _img.shape[:2]
        # ROI: 右上コーナー (88%~100% x, 0~12% y)
        _x1 = int(_W * 0.88)
        _y2 = int(_H * 0.12)
        if _y2 < 5 or _W - _x1 < 5:
            return None
        _roi = _img[0:_y2, _x1:_W]
        # ── プライマリ: テンプレートマッチング (adv_icon_skip) ──
        # HSV はリサイズ後のアイコンサイズに依存するがテンプレートは安定
        try:
            _skip_roi = (int(ANALYSIS_W * 0.85), 0,
                         int(ANALYSIS_W * 0.15), int(ANALYSIS_H * 0.15))
            _skip_m = ASSET_MANAGER.match_single("adv_icon_skip", img_path, roi=_skip_roi)
            if _skip_m and _skip_m[2] >= 0.70:
                logger.debug("[MOVIE_SKIP_BTN] テンプレート検出 (%d,%d) score=%.2f",
                             _skip_m[0], _skip_m[1], _skip_m[2])
                return (_skip_m[0], _skip_m[1])
        except Exception:
            pass

        # ── セカンダリ: 「SKIP」テキストボタン (動画シーン右上) ──
        # ⏭アイコンとは別UIだがどちらもスキップ用
        try:
            _skip_text_m = ASSET_MANAGER.match_single(
                "movie_skip_text", img_path, roi=_skip_roi)
            if _skip_text_m and _skip_text_m[2] >= 0.70:
                logger.debug("[MOVIE_SKIP_BTN] SKIPテキスト検出 (%d,%d) score=%.2f",
                             _skip_text_m[0], _skip_text_m[1], _skip_text_m[2])
                return (_skip_text_m[0], _skip_text_m[1])
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
            _popup_dots = count_page_dots(_pi, _pi.shape[0], _pi.shape[1]) >= 3
            _popup_blur = detect_background_blur(_pi, _pi.shape[0], _pi.shape[1])
        if _popup_dots and _popup_blur:
            return MovieSceneResult()

    # ── ⏭ スキップボタン検出 ──
    skip_btn = detect_movie_skip_button(img_path) if img_path else None
    has_skip = skip_btn is not None

    # ── ADV 証拠チェック (⏭有無に関わらず共通) ──
    # ADV の構造的特徴 (MOVIE にはどれもない):
    #   1. ↓送りボタン (右下) — セリフ送り可能時に表示
    #   2. ADV ツールバー (右上5アイコン: menu,log,AUTO,>>,>|)
    #   3. 上部 AUTO ボタン単独 — ADV 確定
    _has_adv_advance = detect_adv_advance_icon(img_path) if img_path else False
    _has_auto_icon = False
    if img_path:
        try:
            _auto_roi_chk = (0, 0, ANALYSIS_W, int(ANALYSIS_H * 0.15))
            _auto_chk = ASSET_MANAGER.match_single(
                "adv_icon_auto", img_path, roi=_auto_roi_chk)
            _has_auto_icon = _auto_chk is not None and _auto_chk[2] >= 0.70
        except Exception:
            pass

    # ADV 証拠の評価
    # ↓ボタン: 最も確実 (MOVIE には絶対にない)
    # ADVツールバー: 確実 (5アイコン検出、MOVIE には存在しない)
    # AUTOボタン単独: ⏭なし時のみ信頼 (⏭あり時は動画シーンで偽陽性 score~0.77)
    _adv_evidence_strong = None  # ⏭あり時でも信頼できる証拠
    if _has_adv_advance:
        _adv_evidence_strong = "↓ボタン"
    elif adv_result is not None and adv_result.is_adv:
        _adv_evidence_strong = "ADVツールバー"

    # ── ADV 証拠による即棄却 ──
    if _adv_evidence_strong:
        logger.info("[MOVIE_SCENE] %s → ADV確定, MOVIE棄却", _adv_evidence_strong)
        return MovieSceneResult()
    if has_skip:
        # ⏭あり + 強い証拠なし → MOVIE (AUTO単独は信頼しない)
        logger.info("[MOVIE_SCENE] ⏭検出 + ADV証拠なし → MOVIE確定")
    else:
        # ⏭ なし: phash 連続変化があれば動画の可能性を残す
        # AUTO 単独でも ADV 判定 OK (⏭なし時)
        if _has_auto_icon:
            logger.debug("[MOVIE_SCENE] AUTOボタン → ADV確定, MOVIE棄却")
            return MovieSceneResult()

        # 即棄却: バトルキーワード
        if any(kw in joined for kw in _MOVIE_REJECT_BATTLE_KWS):
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
            if _popup_blur:
                logger.debug("[MOVIE_SCENE] 背景ぼかし検出 → MOVIE棄却 (ポップアップ)")
                return MovieSceneResult()

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
                             upper_ratio: float = 0.45):
    """
    ミニ会話シーン（上部の白い吹き出し）を検出しアクティブ話者の中心座標を返す。

    Returns: (cx, cy, "left"|"right") or None
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        resized = cv2.resize(img, (ANALYSIS_W, ANALYSIS_H))
        h_cut = int(ANALYSIS_H * upper_ratio)
        upper = resized[0:h_cut, :]

        # HSV 白色マスク (S<40, V>200)
        hsv = cv2.cvtColor(upper, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (0, 0, 200), (180, 40, 255))

        # morphology cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)

        # ADVツールバー除外ゾーン (x>82%, y<22%)
        toolbar_x = int(ANALYSIS_W * 0.82)
        toolbar_y = int(ANALYSIS_H * 0.22)

        candidates = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_bubble_area:
                continue
            x, y, w, bh = cv2.boundingRect(cnt)
            if bh == 0:
                continue
            aspect = w / bh
            if aspect < 1.2 or aspect > 8.0:
                continue
            # ツールバー除外
            cx_cnt = x + w // 2
            cy_cnt = y + bh // 2
            if cx_cnt > toolbar_x and cy_cnt < toolbar_y:
                continue

            # 平均輝度 (V チャンネル)
            cnt_mask = np.zeros(mask.shape, dtype=np.uint8)
            cv2.drawContours(cnt_mask, [cnt], -1, 255, -1)
            mean_v = float(cv2.mean(hsv[:, :, 2], mask=cnt_mask)[0])

            side = "left" if cx_cnt < ANALYSIS_W // 2 else "right"
            candidates.append({
                "cx": cx_cnt, "cy": cy_cnt,
                "x": x, "y": y, "w": w, "h": bh,
                "mean_v": mean_v, "side": side, "area": area,
            })

        if not candidates:
            return None

        # 最も明るい = アクティブ話者
        best = max(candidates, key=lambda c: c["mean_v"])

        # OCR 検証: 吹き出し BBox 内にテキストが存在するか
        if ocr_items is not None:
            bx1, by1 = best["x"], best["y"]
            bx2, by2 = bx1 + best["w"], by1 + best["h"]
            has_text_inside = any(
                bx1 <= r["center"][0] <= bx2 and by1 <= r["center"][1] <= by2
                for r in ocr_items
                if r["text"] not in ("AUTO", ">>", ">|", "D1", "×")
            )
            if not has_text_inside:
                return None

        logger.debug("[MINI_CONV] bubble (%d,%d) side=%s area=%d mean_v=%.1f",
                     best["cx"], best["cy"], best["side"], best["area"],
                     best["mean_v"])
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

_DIALOG_CLOSE_TEMPLATE = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_dialog_close.png"
_DIALOG_NEXT_TEMPLATE  = _CRAWLER_ROOT / "assets" / "templates" / "tutorial_dialog_next.png"


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


def detect_dialog(img_path: Path, W: int = 1520, H: int = 720,
                  require_blur: bool = True) -> Optional[tuple[str, int, int]]:
    """背景ぼかし確認 + ▷/× ボタン検出を一括で行う。

    require_blur=True (default): 背景ぼかしがない場合は None を返す。
    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
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
            PRIMARY: テンプレートマッチング (dialog_nav_right)
            FALLBACK: 輝度ベース検出 (右端6% × 中央帯)
            """
            # PRIMARY: テンプレートマッチング
            try:
                _nav_roi = (int(_W * 0.90), int(_H * 0.25),
                            int(_W * 0.10), int(_H * 0.50))
                _nav_m = ASSET_MANAGER.match_single(
                    "dialog_nav_right", img_path, roi=_nav_roi)
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
        # STEP 1: HSV 金色枠で大矩形ダイアログを検出
        # ──────────────────────────────────────────────────────────────
        def _detect_gold_frame(img_bgr, _H, _W):
            """画面中央付近に金色枠のダイアログ矩形があるか検出する。"""
            _hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
            _mask_g = cv2.inRange(
                _hsv,
                np.array([12, 50, 140], np.uint8),
                np.array([55, 255, 255], np.uint8),
            )
            _k3 = np.ones((3, 3), np.uint8)
            _mask_g = cv2.dilate(_mask_g, _k3, iterations=2)
            _cnts, _ = cv2.findContours(_mask_g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            _frame: Optional[tuple] = None
            _best_area = 0
            _scx, _scy = _W // 2, _H // 2

            for _c in _cnts:
                _a = cv2.contourArea(_c)
                if _a < 8000:
                    continue
                _x, _y, _w, _h = cv2.boundingRect(_c)
                if _w < 280 or _h < 160:
                    continue
                if _w > _W * 0.97 or _h > _H * 0.97:
                    continue
                _asp = _w / max(_h, 1)
                if not (0.3 < _asp < 5.5):
                    continue
                _dcx = _x + _w // 2
                _dcy = _y + _h // 2
                if not (_W * 0.20 <= _dcx <= _W * 0.80):
                    continue
                if abs(_dcy - _scy) > _H * 0.45:
                    continue
                if _a > _best_area:
                    _best_area = _a
                    _frame = (_x, _y, _w, _h)
            return _frame

        _frame = _detect_gold_frame(img, _H, _W)

        # ──────────────────────────────────────────────────────────────
        # STEP 0: × ボタン先行検出
        #   × + 中央ダイアログ枠の両方が揃って初めてダイアログと判定。
        #   カード詳細等の非ダイアログ画面での誤検出を防止。
        # ──────────────────────────────────────────────────────────────
        _close_x_pos = _find_close_x(img, _H, _W)
        if _close_x_pos is not None and _frame is not None:
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

        _frame_detected = _frame is not None

        # OCR キーワード補助: 枠未検出でもキーワードがあればフォールバック実行
        _ocr_trigger = False
        if not _frame_detected and ocr_texts:
            _joined_ocr = " ".join(ocr_texts)
            _ocr_trigger = any(kw in _joined_ocr for kw in _DIALOG_FIRST_KWS)
        if not _frame_detected and not _ocr_trigger:
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

        # 検索 ROI: テンプレートなければ Canny、それも失敗したら輝度、最後に固定座標
        # フレーム検出時はフレーム右上を優先、画面右上はフォールバック

        # ── × ボタン検索 ──────────────────────────────────────────────
        # Phase A: フレーム検出時 → フレーム右上隅で × を探す
        if _frame_detected:
            _fx, _fy, _fw, _fh = _frame
            # フレーム右上角周辺を探索 (±40px マージン)
            _frx1 = max(0, _fx + _fw - 60)
            _fry1 = max(0, _fy - 30)
            _frx2 = min(_W, _fx + _fw + 40)
            _fry2 = min(_H, _fy + 50)
            _froi = img[_fry1:_fry2, _frx1:_frx2]
            if _froi.size > 0:
                # テンプレートマッチング
                if _DIALOG_CLOSE_TEMPLATE.exists():
                    _tpl = imread_cached(_DIALOG_CLOSE_TEMPLATE)
                    if _froi.shape[0] >= _tpl.shape[0] and _froi.shape[1] >= _tpl.shape[1]:
                        _r_f = cv2.matchTemplate(_froi, _tpl, cv2.TM_CCOEFF_NORMED)
                        _, _mv_f, _, _ml_f = cv2.minMaxLoc(_r_f)
                        if _mv_f >= 0.65:
                            _tw_f = _tpl.shape[1]
                            _th_f = _tpl.shape[0]
                            _cx_f = _frx1 + _ml_f[0] + _tw_f // 2
                            _cy_f = _fry1 + _ml_f[1] + _th_f // 2
                            logger.debug("[Dialog×] フレーム右上テンプレ: (%d,%d) score=%.2f", _cx_f, _cy_f, _mv_f)
                            return ("close", _cx_f, _cy_f)
                # Note: Canny / 輝度フォールバックは誤検出率が高いため廃止。
                # × 検出は STEP 0 テンプレートマッチングのみが権威ある判定。

        # Phase B: フレーム未検出 or フレーム右上で × 未発見 → 画面右上隅で探す
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

        # ── フォールバック: 固定座標 ▷ ──────────────────────────────────
        if _frame_detected:
            # 枠が確認できている場合は枠下部中央を安全タップ
            _fx, _fy, _fw, _fh = _frame
            _fb_x, _fb_y = _fx + _fw // 2, _fy + int(_fh * 0.85)
            logger.debug("[Dialog] 枠下部フォールバック: (%d,%d)", _fb_x, _fb_y)
            return ("bottom", _fb_x, _fb_y)

        # OCR キーワードのみで枠未検出 → ROI 補正済み固定座標 ▷
        _r = roi if roi else (0, 0, _W, _H)
        _nx_ocr, _ny_ocr = roi_to_device(int(ANALYSIS_W * 0.91), int(ANALYSIS_H * 0.49), _r)
        return ("next", _nx_ocr, _ny_ocr)

    except Exception as _e:
        logger.debug("detect_dialog_frame_and_nav error: %s", _e)
        return None


# ─── お知らせポップアップ検出 ─────────────────────────────────────────────


def count_page_dots(img_or_path, H: int = 720, W: int = 1520) -> int:
    """画面下部のページドットインジケータ (● ○ ○ …) の個数を返す。

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
    # ROI: 下部20%, 中央80%  (ドットが y=85% 付近に出るケースに対応)
    _y1 = int(H * 0.80)
    _x1 = int(W * 0.10)
    _x2 = int(W * 0.90)
    _roi = img[_y1:H, _x1:_x2]
    if _roi.size == 0:
        return 0
    _gray = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)
    _, _thr = cv2.threshold(_gray, 140, 255, cv2.THRESH_BINARY)
    _cnts, _ = cv2.findContours(_thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    # 面積閾値を解像度でスケーリング (基準: 1520x720)
    # max_area=800: 金色アクティブドット (輝度が高く膨張) を含む
    _scale = (W * H) / (1520 * 720)
    _min_area = 15 * _scale
    _max_area = 800 * _scale
    _dots = []  # (cx, cy) リスト — 水平整列チェック用
    for _c in _cnts:
        _a = cv2.contourArea(_c)
        if _a < _min_area or _a > _max_area:
            continue
        _x, _y, _w, _h = cv2.boundingRect(_c)
        _asp = _w / max(_h, 1)
        if 0.5 < _asp < 2.0:  # roughly circular
            _dots.append((_x + _w // 2, _y + _h // 2))
    if len(_dots) < 2:
        return len(_dots)
    # 水平整列チェック: 実際のページドットは同一Y座標に並ぶ
    # 最頻Y座標 ±5px 以内のドットだけカウント (装飾散乱を除外)
    _roi_w = _x2 - _x1
    _best_count = 0
    for _ref_y in set(d[1] for d in _dots):
        _row = [d for d in _dots if abs(d[1] - _ref_y) <= 5]
        if len(_row) < 2:
            continue
        # x方向クラスタリング: ドットが中央に集中しているか確認
        # 実際のページドットは ROI幅の40%以内に収まる (6ドットでも ~200px/1216px ≈ 16%)
        _xs = [d[0] for d in _row]
        _x_span = max(_xs) - min(_xs)
        if _x_span > _roi_w * 0.30:
            continue  # 散らばりすぎ → 装飾要素
        _best_count = max(_best_count, len(_row))
    return _best_count


def _detect_page_dots(img, H: int, W: int) -> bool:
    """画面下部にページドットインジケータが3個以上あるか。"""
    return count_page_dots(img, H, W) >= 3


def detect_background_blur(img, H: int, W: int) -> bool:
    """ポップアップ外の左端ストリップがぼかされているか (HSV彩度分散低下) を検出。"""
    # 左端ストリップ: x=0~6%, y=15~85% (ポップアップ外の背景領域)
    _lx2 = int(W * 0.06)
    _ly1, _ly2 = int(H * 0.15), int(H * 0.85)
    _left = img[_ly1:_ly2, 0:_lx2]
    if _left.size == 0:
        return False
    _hsv = cv2.cvtColor(_left, cv2.COLOR_BGR2HSV)
    _sat_var = float(_hsv[:, :, 1].var())
    # ぼかし背景: 彩度の分散が低い (鮮明な画像は分散が大きい)
    _is_blur = _sat_var < 800
    logger.debug("[NOTICE_POPUP] 背景ぼかし: sat_var=%.1f (threshold=800) → %s",
                 _sat_var, "blur" if _is_blur else "sharp")
    return _is_blur


def detect_notice_popup(
    img_path: Path, ocr_texts: list[str], W: int = 1520, H: int = 720,
) -> bool:
    """お知らせポップアップを検出する。

    判定条件 (いずれかで確定):
      1. OCR で「今日は表示しない」を検出 (確定条件)
      2. 補助条件: × ボタン + ページドット + 背景ぼかし の全組合せ
    """
    # ── 条件1: OCR テキスト (確定) ──
    if any("今日は表示しない" in t for t in ocr_texts):
        logger.info("[NOTICE_POPUP] 「今日は表示しない」検出 → お知らせポップアップ確定")
        return True

    # ── 条件2: 視覚的特徴の組合せ ──
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        _H, _W = img.shape[:2]

        # 2a: × ボタン (右上テンプレートマッチ)
        _has_close = False
        if _DIALOG_CLOSE_TEMPLATE.exists():
            _rx1 = int(_W * 0.85)
            _ry2 = int(_H * 0.15)
            _roi_x = img[0:_ry2, _rx1:_W]
            if _roi_x.size > 0:
                _tpl = imread_cached(_DIALOG_CLOSE_TEMPLATE)
                if (_roi_x.shape[0] >= _tpl.shape[0]
                        and _roi_x.shape[1] >= _tpl.shape[1]):
                    _r = cv2.matchTemplate(_roi_x, _tpl, cv2.TM_CCOEFF_NORMED)
                    _, _mv, _, _ = cv2.minMaxLoc(_r)
                    _has_close = _mv >= 0.65

        # 2b: ページドット (画面下部中央の小円群)
        _has_dots = _detect_page_dots(img, _H, _W)

        # 2c: 背景ぼかし (HSV彩度低下)
        _has_blur = detect_background_blur(img, _H, _W)

        if _has_close and _has_dots and _has_blur:
            logger.info("[NOTICE_POPUP] 補助条件成立: ×=%s dots=%s blur=%s",
                        _has_close, _has_dots, _has_blur)
            return True
    except Exception as _e:
        logger.debug("[NOTICE_POPUP] 検出エラー: %s", _e)

    return False


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

    検出優先順:
      1. テンプレートマッチ (指アイコン) + 白い縦軌跡の確認
      2. HSV金色フィルタ (フォールバック)

    Returns: (direction, swipe_x, from_y, to_y, duration_ms) or None
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        # ── Phase 1: テンプレートマッチ (指アイコン) + 白い縦軌跡 ──
        if _SWIPE_FINGER_TEMPLATE.exists():
            _tpl = imread_cached(_SWIPE_FINGER_TEMPLATE)
            if _tpl is not None and img.shape[0] >= _tpl.shape[0] and img.shape[1] >= _tpl.shape[1]:
                _r = cv2.matchTemplate(img, _tpl, cv2.TM_CCOEFF_NORMED)
                _, _mv, _, _ml = cv2.minMaxLoc(_r)
                if _mv >= 0.75:
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
                    if _has_trail:
                        # 指が上 + 軌跡が下 → SWIPE_UP
                        _from_y = min(H_img - 60, _trail_y2 + 50)
                        _to_y = max(50, _fy - 80)
                        logger.info(
                            "[GoldSwipe] テンプレ検出: score=%.2f finger=(%d,%d) trail=%s "
                            "→ UP swipe_x=%d from=%d to=%d",
                            _mv, _fx, _fy, _has_trail, _fx, _from_y, _to_y,
                        )
                        return "UP", _fx, _from_y, _to_y, 10000
                    else:
                        logger.debug(
                            "[GoldSwipe] テンプレ指検出 score=%.2f (%d,%d) だが軌跡なし → HSVへ",
                            _mv, _fx, _fy,
                        )

        # ── Phase 2: HSV金色フィルタ (フォールバック) ──
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        # 金色 (手アイコン+軌跡): H=15-50, S=60-255, V=180-255
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        # モルフォロジー: 小ノイズ除去 → 拡張で手+軌跡を繋ぐ
        k3 = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k3)
        k7 = np.ones((7, 7), np.uint8)
        mask = cv2.dilate(mask, k7, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # 最大輪郭を選択
        largest = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(largest)

        # 面積フィルタ: 2000~100000px (ポインター想定範囲)
        if area < 2000 or area > 100000:
            return None

        x_bb, y_bb, w_bb, h_bb = cv2.boundingRect(largest)

        # アスペクト比チェック: 縦長(h>=w*3.5)のみ有効
        # 2.0→3.5に引き上げ: キャラカード金装飾(h/w≈2.0-2.5)や金枠ボタン(h/w≈1.0)との誤検出防止
        # さらに幅制限: w>100px の太いものはボタン/カード → スワイプポインターは細い
        if h_bb < w_bb * 3.5 or w_bb > 100:
            return None

        cx_bb = x_bb + w_bb // 2

        # ── デバッグ画像保存 (--verbose 時のみ) ──
        if _DEBUG_SAVE_IMAGES:
            debug_dir = _CRAWLER_ROOT / "templates" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            vis = img.copy()
            cv2.rectangle(vis, (x_bb, y_bb), (x_bb + w_bb, y_bb + h_bb), (0, 0, 255), 3)
            cv2.putText(vis, f"GoldSwipe area={int(area)} h/w={h_bb/max(w_bb,1):.1f}",
                        (x_bb, max(0, y_bb - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.imwrite(str(debug_dir / f"gold_detect_{ts}.png"), vis)

        # ── 方向判定: 上半分 vs 下半分のゴールドピクセル面積で判断 ──
        # 手アイコン(幅広・濃い)が多い方が「手」の端 → その逆方向へスワイプ
        mask_roi = mask[y_bb:y_bb + h_bb, x_bb:x_bb + w_bb]
        mid_y = h_bb // 2
        upper_area = int(np.sum(mask_roi[:mid_y] > 0))
        lower_area = int(np.sum(mask_roi[mid_y:] > 0))

        # 上半分が大きい → 手が上 → SWIPE_UP
        if upper_area >= lower_area:
            direction = "UP"
            from_y = min(H_img - 60, y_bb + h_bb + 100)
            to_y   = max(50, y_bb - 80)
        else:
            direction = "DOWN"
            from_y = max(50, y_bb - 80)
            to_y   = min(H_img - 60, y_bb + h_bb + 100)

        logger.info(
            "[GoldSwipe] 検出OK: area=%d bbox=(%d,%d,%d,%d) h/w=%.1f "
            "upper=%d lower=%d → %s  swipe_x=%d from_y=%d to_y=%d",
            area, x_bb, y_bb, w_bb, h_bb, h_bb / max(w_bb, 1),
            upper_area, lower_area, direction, cx_bb, from_y, to_y,
        )
        return direction, cx_bb, from_y, to_y, 10000

    except ImportError:
        return None
    except Exception as e:
        logger.debug("detect_tutorial_gold_swipe error: %s", e)
        return None


# ─── Type B: 金枠ハイライトボタン検出 → 中心タップ ─────────────────────
def detect_tutorial_gold_button_tap(img_path: Path,
                                    right_half_only: bool = True,
                                    overlay_mode: bool = False,
                                    ) -> Optional[tuple[int, int]]:
    """
    チュートリアルバトルで指アイコンが指し示す「金枠ハイライトボタン」を検出し
    タップ座標（ボタン中心）を返す。

    条件:
    - アスペクト比 0.5~2.0 (正方形〜縦長のボタン形状)
    - 面積 8000~150000px² (ボタン相当の大きさ)
    - 幅 100px以上 (細い軌跡線は除外)
    - right_half_only=True の場合: x中心 > W/2 のみ有効 (右側ボタン優先)
    - overlay_mode=True の場合: チュートリアル暗転確定 → 上部除外・右半分フィルタをバイパス

    デバッグ画像: crawler/templates/tutorial/gold_btn_HHMMSS.png に自動保存。
    Returns: (tap_x, tap_y) or None
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        # モルフォロジー: 枠線の隙間を埋めて矩形を繋ぐ (膨張は1回に抑制→bbox下方ズレ防止)
        k7 = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k7)
        mask = cv2.dilate(mask, k7, iterations=1)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # ボタン候補: アスペクト比0.5~2.0 かつ面積5000~50000 かつ幅80px以上
        # キャラアイコン除外: 金色の充填率 (extent) が高い = アイコン (金色が密)
        #   チュートリアルボタン = 金色の枠線のみ → extent 低め (<0.55)
        # NOTE: ホーム画面の編成ボタン金枠は area~7800 のため 8000 では漏れる
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 5000 or area > 50000:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < 80:
                continue
            # 充填率: 金色ピクセル密度 (キャラアイコンは金色が密集、ボタン枠は枠線のみ)
            bbox_area = w * h
            extent = area / max(bbox_area, 1)
            # right_half_only (バトル) では右半分にキャラアイコンがないため閾値を緩和
            # 戦闘スキルボタンは morphology 後に extent=0.7-0.8 になるため 0.55 では弾かれる
            _extent_limit = 0.85 if right_half_only else 0.55
            if extent > _extent_limit:
                logger.debug("[GoldBtn] 充填率排除: bbox=(%d,%d,%d,%d) extent=%.2f > %.2f (キャラアイコン疑い)",
                             x, y, w, h, extent, _extent_limit)
                continue
            aspect = h / max(w, 1)
            if 0.5 <= aspect <= 2.0:
                cx = x + w // 2
                cy = y + h // 2
                # 画面上部 (y<35%) は除外 — ホーム画面装飾の誤検出防止
                # overlay_mode (チュートリアル暗転確定) 時はバイパス
                if not overlay_mode and cy < H_img * 0.35:
                    logger.debug("[GoldBtn] 上部除外: bbox=(%d,%d,%d,%d) cy=%d", x, y, w, h, cy)
                    continue
                # 右半分のみフィルタ (overlay_mode 時はバイパス)
                if right_half_only and not overlay_mode and cx < W_img * 0.5:
                    continue
                candidates.append((cx, cy, area, x, y, w, h))

        if not candidates:
            return None

        # 最大面積のボタン候補を選択
        best = max(candidates, key=lambda c: c[2])
        tap_x, tap_y, area_b, x_b, y_b, w_b, h_b = best

        # ── デバッグ/テンプレート保存 (--verbose 時のみ) ──
        if _DEBUG_SAVE_IMAGES:
            tut_dir = _CRAWLER_ROOT / "templates" / "tutorial"
            tut_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%H%M%S")
            vis = img.copy()
            cv2.rectangle(vis, (x_b, y_b), (x_b + w_b, y_b + h_b), (255, 0, 0), 3)
            cv2.circle(vis, (tap_x, tap_y), 12, (0, 255, 255), -1)
            cv2.putText(vis, f"GoldBtn area={int(area_b)} asp={h_b/max(w_b,1):.1f}",
                        (x_b, max(0, y_b - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
            cv2.imwrite(str(tut_dir / f"gold_btn_{ts}.png"), vis)
            roi = img[y_b:y_b + h_b, x_b:x_b + w_b]
            if roi.size > 0:
                cv2.imwrite(str(tut_dir / f"gold_btn_roi_{ts}.png"), roi)

        logger.info("[GoldBtn] 検出OK: area=%d bbox=(%d,%d,%d,%d) asp=%.1f → tap(%d,%d)",
                    area_b, x_b, y_b, w_b, h_b, h_b / max(w_b, 1), tap_x, tap_y)
        return tap_x, tap_y

    except ImportError:
        return None
    except Exception as e:
        logger.debug("detect_tutorial_gold_button_tap error: %s", e)
        return None


# ─── チュートリアルオーバーレイ（暗転）検出 ──────────────────────────


def detect_tutorial_overlay(img_path: Path, brightness_threshold: int = 90) -> bool:
    """チュートリアル中の暗転オーバーレイを検出する。

    チュートリアル時は指アイコン+金枠のハイライト以外が半透明の暗いオーバーレイで覆われる。
    画面全体の中央値輝度が低い (< brightness_threshold) なら暗転中と判定。

    Returns: True = 暗転オーバーレイあり（チュートリアル中の可能性が高い）
    """
    try:
        img = imread_cached(img_path)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        median_brightness = int(np.median(gray))
        logger.debug("[TutOverlay] median_brightness=%d threshold=%d",
                     median_brightness, brightness_threshold)
        return median_brightness < brightness_threshold
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
    """Text-Core 対応 SmartTap: 金色ボタン枠を検出し、テキスト中心優先でタップ座標を返す。

    1. OCR 中心周辺から HSV で金色ボタン枠 (B) を検出
    2. B が見つかったら text_core_center() でテキスト中心優先の座標を返す
    3. B が見つからない場合は OCR 座標をそのまま返す

    返値: (tap_x, tap_y)
    """
    try:
        img_bgr = imread_cached(img_path)
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

        # 金色ボタン枠の HSV レンジ
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
            if area < 2000:
                continue
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            if rw < 80 or rh < 20:
                continue
            aspect = rw / max(rh, 1)
            if aspect < 2.0 or aspect > 15.0:
                continue
            if area > best_area:
                best_area = area
                best_rect = (rx + x1, ry + y1, rw, rh)

        if best_rect:
            # Text-Core: ボタン枠 (B) 内のテキスト中心を優先
            return text_core_center(
                best_rect,
                ocr_items or [],
                label="SmartTap",
            )

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
        img = imread_cached(img_path)
        if img is None:
            return None
        # キャラ頭上エリア
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
            self._templates[name] = {
                "img": img,
                "edge_img": _edge_img,
                "edge_weight": _ew,
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
              ocr_texts: Optional[list[str]] = None,
              ) -> Optional[tuple[int, int, str, tuple[int, int, int, int]]]:
        """
        スクリーンショットと全テンプレートを比較。
        ocr_texts が渡された場合、require_ocr 条件を満たすテンプレートのみ照合。
        Returns: (tap_x, tap_y, action_name, button_region) or None
            button_region = (bx, by, bw, bh) — テンプレートマッチ領域
        """
        if not self._templates:
            return None
        img = imread_cached(screenshot_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        best_score = 0.0
        best_result: Optional[tuple[int, int, str, tuple[int, int, int, int]]] = None
        for name, data in self._templates.items():
            if name in _SINGLE_ONLY:
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
            if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            try:
                res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
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
                    h, w = tmpl.shape
                    bx = max_loc[0] + int(data["offset"][0])
                    by = max_loc[1] + int(data["offset"][1])
                    cx = bx + w // 2
                    cy = by + h // 2
                    best_result = (cx, cy, data["action"], (bx, by, w, h))
                    logger.debug("[Asset] '%s' score=%.3f at (%d,%d)", name, max_val, cx, cy)
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

        roi: (x, y, w, h) — 検索領域を制限。座標は元画像基準で返す。
        """
        data = self._templates.get(name)
        if data is None:
            return None
        img = imread_cached(screenshot_path, cv2.IMREAD_GRAYSCALE)
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


