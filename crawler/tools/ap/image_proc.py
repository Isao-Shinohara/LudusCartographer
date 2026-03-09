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


def is_dark_screen(img_path: Path) -> bool:
    try:
        from PIL import Image
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
    analysis_path = ANALYSIS_PATH
    img = Image.open(img_path)
    if img.width < img.height:
        img = img.rotate(90, expand=True)
    if img.size != (ANALYSIS_W, ANALYSIS_H):
        img = img.resize((ANALYSIS_W, ANALYSIS_H), Image.LANCZOS)
    img.save(analysis_path)
    return analysis_path


# ─── 指差しアイコン (肌色ブロブ) 検出 ──────────────
def find_finger_blobs(img_path: Path, min_area: int = 400,
                      max_area: int = 15000,
                      dark_mode: bool = False) -> list[tuple[int, int, float, int, int, int, int]]:
    """
    指差しアイコン（肌色）の大きいブロブを検出。
    battle_loop.py と同じ HSV マスク手法。
    max_area: 金色カード等の大面積誤検出を除外（UI カードは 15000px² 超）
    dark_mode: バトル背景など暗い状況では輝度閾値を緩和（V:150→100, S:40→25）
    返値: [(cx, cy, area, bbox_x, bbox_y, bbox_w, bbox_h), ...] 面積降順
    """
    try:
        img = cv2.imread(str(img_path))
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
        if not blobs and _oversized:
            for _ov in _oversized:
                _gf = find_gold_frame_near(img_path, _ov[0], _ov[1], search_radius=200)
                if _gf is not None:
                    logger.info("[FINGER_OVERSIZED_RESCUE] (%d,%d) area=%.0f + 金枠(%d,%d) → 採用",
                                _ov[0], _ov[1], _ov[2], _gf[0], _gf[1])
                    blobs.append(_ov)
                    break  # 最初の1件で十分
        return sorted(blobs, key=lambda b: b[2], reverse=True)
    except ImportError:
        return []
    except Exception as e:
        logger.debug("find_finger_blobs error: %s", e)
        return []


def detect_white_hand_pointer(
    img_path: Path, threshold: float = 0.85
) -> Optional[tuple[int, int, float]]:
    """
    白いハンドポインタ（home_nav_finger / home_nav_finger_up）をテンプレートマッチングで検出。
    find_finger_blobs() が HSV 肌色のみ対象で白ポインタを見逃す問題を補完。
    Returns: (cx, cy, score) or None
    """
    try:
        img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        templates_dir = _CRAWLER_ROOT / "assets" / "templates"
        best: Optional[tuple[int, int, float]] = None
        for name in ("home_nav_finger", "home_nav_finger_up"):
            tpl_path = templates_dir / f"{name}.png"
            if not tpl_path.exists():
                continue
            tmpl = cv2.imread(str(tpl_path), cv2.IMREAD_GRAYSCALE)
            if tmpl is None or tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= threshold and (best is None or max_val > best[2]):
                h, w = tmpl.shape
                best = (max_loc[0] + w // 2, max_loc[1] + h // 2, max_val)
        if best:
            logger.info("[WHITE_HAND] 白ハンドポインタ検出 (%d,%d) score=%.3f", best[0], best[1], best[2])
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
        _img_hm = cv2.imread(str(img_path))
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
        _img_gw = cv2.imread(str(img_path))
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
        # bbox上端 + 高さ1/3 = ボタン視覚中心 (centroidはハロに引かれ下にずれる)
        gx = g["cx"]
        gy = max(1, g["by"] + g["bh"] // 3)
        logger.info("[%s P1] 左キャラ発光 centroid(%d,%d) bbox_y=%d+%d → tap(%d,%d)",
                    tag, g["cx"], g["cy"], g["by"], g["bh"], gx, gy)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR", rapid=True)
        tap_device(gx, gy, state, "GLOW_LEFT_CHAR")  # ダブルタップ
        state.character_selected = True
        state.char_just_selected = True
        state.finger_detections += 1
        return "GLOW_LEFT_CHAR", 0.3

    # P2: 右スキル発光 (キャラ選択済み)
    if state.character_selected and right:
        g = max(right, key=lambda g: g["area"])
        # bbox上端 + 高さ1/3 = ボタン視覚中心
        gx = g["cx"]
        gy = max(1, g["by"] + g["bh"] // 3)
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
        _img = cv2.imread(str(img_path))
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
        img = cv2.imread(str(img_path))
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


def is_adv_toolbar_cached(img_path: Path, state: "PilotState") -> bool:
    """is_adv_toolbar_visible() の phash キャッシュ付きラッパー。同一 phash なら再計算しない。"""
    cur = state.last_phash
    if cur and cur == state._adv_toolbar_cache_phash:
        return state._adv_toolbar_cache_result
    result = is_adv_toolbar_visible(img_path)
    state._adv_toolbar_cache_phash = cur
    state._adv_toolbar_cache_result = result
    return result


def detect_adv_advance_icon(img_path: Path,
                             roi_x: int = int(ANALYSIS_W * 0.875),
                             roi_y: int = int(ANALYSIS_H * 0.847),
                             roi_w: int = int(ANALYSIS_W * 0.112),
                             roi_h: int = int(ANALYSIS_H * 0.125),
                             min_bright: int = 20) -> bool:
    """
    ADV送り待ちアイコン（◆/▼）を検出。
    テキストボックス右下 ROI 内に孤立した明るい小クラスターを探す。

    ROI デフォルト: x=1330-1500, y=610-700 (landscape 1520x720)
    明るい白/淡色ピクセル: HSV V>210, S<60 が min_bright 個以上 → True
    """
    try:
        _img = cv2.imread(str(img_path))
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
        if _bright >= min_bright:
            logger.debug("[ADV_ADVANCE] 明るいピクセル %d 個 @ ROI(%d,%d,%d,%d)",
                         _bright, roi_x, roi_y, roi_w, roi_h)
            return True
        return False
    except Exception as _e:
        logger.debug("detect_adv_advance_icon error: %s", _e)
        return False


def is_adv_toolbar_visible(img_path: Path) -> bool:
    """
    ADVパートの右上ツールバー（5個のアイコン列: メニュー, ログ, AUTO, >>, >|）を検出。
    動画シーン（⏭ 1個のみ）と区別するために使用。

    手法: 右上ROI内でCanny edge密度を計測。
    ADVツールバー: 複数アイコンの輪郭でedge密度が高い (>=0.04)
    動画シーン: アイコン1個のみ or 空で低密度
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return False
        _H, _W = _img.shape[:2]
        # ROI: 右上 78%~100% x, 0~10% y
        _x1 = int(_W * 0.78)
        _y2 = int(_H * 0.10)
        if _y2 < 10 or _W - _x1 < 10:
            return False
        _roi = _img[0:_y2, _x1:_W]
        _gray = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)
        _edges = cv2.Canny(_gray, 50, 150)
        _total = _roi.shape[0] * _roi.shape[1]
        if _total == 0:
            return False
        _edge_ratio = cv2.countNonZero(_edges) / _total
        _visible = _edge_ratio >= 0.04
        if _visible:
            logger.debug("[ADV_TOOLBAR] edge密度=%.3f → ADVパート確定", _edge_ratio)
        return _visible
    except Exception:
        return False


def detect_movie_skip_button(img_path: Path) -> Optional[tuple]:
    """
    動画シーンの⏭スキップボタン（右上の金色円形アイコン）を検出。
    返り値: (cx, cy) or None
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _H, _W = _img.shape[:2]
        # ROI: 右上コーナー (88%~100% x, 0~12% y)
        _x1 = int(_W * 0.88)
        _y2 = int(_H * 0.12)
        if _y2 < 5 or _W - _x1 < 5:
            return None
        _roi = _img[0:_y2, _x1:_W]
        _hsv = cv2.cvtColor(_roi, cv2.COLOR_BGR2HSV)
        # 金色: H=15-40, S>50, V>130
        _mask = cv2.inRange(_hsv, (15, 50, 130), (40, 255, 255))
        _gold_count = int(cv2.countNonZero(_mask))
        if _gold_count >= 30:
            _coords = cv2.findNonZero(_mask)
            if _coords is not None:
                _mx = int(np.mean(_coords[:, 0, 0])) + _x1
                _my = int(np.mean(_coords[:, 0, 1]))
                logger.debug("[MOVIE_SKIP_BTN] 金色ボタン検出 (%d,%d) gold_px=%d", _mx, _my, _gold_count)
                return (_mx, _my)
        return None
    except Exception:
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


def detect_tutorial_dialog_nav(img_path: Path,
                                W: int = 1520, H: int = 720,
                                threshold: float = 0.75) -> Optional[tuple[str, int, int]]:
    """
    チュートリアルダイアログの ▷(次へ) または ×(閉じる) ボタンを検出する。

    テンプレート画像が存在する場合はテンプレートマッチング、
    存在しない場合は固定座標フォールバックを返す。

    Returns: ("next", cx, cy) | ("close", cx, cy) | None
    """
    try:
        _img = cv2.imread(str(img_path))
        if _img is None:
            return None
        _H, _W = _img.shape[:2]

        def _match_template(tmpl_path: Path, roi_x1: int, roi_y1: int,
                            roi_x2: int, roi_y2: int) -> Optional[tuple[int, int]]:
            _tmpl = cv2.imread(str(tmpl_path))
            if _tmpl is None:
                return None
            _roi = _img[roi_y1:roi_y2, roi_x1:roi_x2]
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
        logger.debug("detect_tutorial_dialog_nav error: %s", _e)
        return None


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
        img = cv2.imread(str(img_path))
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
            _tpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
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
            """右サイドにページング矢印 (>) が存在するか確認。
            狭いストリップ (右端3%) × 中央帯 (30%-70%) で白/明るい矢印を検出。
            """
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

        _close_x_pos = _find_close_x(img, _H, _W)
        if _close_x_pos is not None:
            # ページング矢印 (>) チェック: 矢印があれば close ではなく next を優先
            _arrow_pos = _has_page_arrow(img, _H, _W)
            if _arrow_pos is not None:
                logger.debug("[Dialog] STEP0: × 検出(%d,%d) + 矢印(%d,%d) → next 優先 (ページング)",
                             _close_x_pos[0], _close_x_pos[1], _arrow_pos[0], _arrow_pos[1])
                return ("next", _arrow_pos[0], _arrow_pos[1])
            logger.debug("[Dialog×] STEP0 先行検出: (%d,%d)", _close_x_pos[0], _close_x_pos[1])
            return ("close", _close_x_pos[0], _close_x_pos[1])

        # ──────────────────────────────────────────────────────────────
        # STEP 1: HSV 金色枠で大矩形ダイアログを検出
        # ──────────────────────────────────────────────────────────────
        _hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        _mask_g = cv2.inRange(
            _hsv,
            np.array([12, 50, 140], np.uint8),
            np.array([55, 255, 255], np.uint8),
        )
        _k3 = np.ones((3, 3), np.uint8)
        _mask_g = cv2.dilate(_mask_g, _k3, iterations=2)
        _cnts, _ = cv2.findContours(_mask_g, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        _frame: Optional[tuple] = None  # (x, y, w, h)
        _best_area = 0
        _scx, _scy = _W // 2, _H // 2   # 画面中心

        for _c in _cnts:
            _a = cv2.contourArea(_c)
            if _a < 8000:
                continue
            _x, _y, _w, _h = cv2.boundingRect(_c)
            if _w < 280 or _h < 160:          # 小さすぎ → カード等を除外
                continue
            if _w > _W * 0.97 or _h > _H * 0.97:  # 全画面 → 除外
                continue
            _asp = _w / max(_h, 1)
            if not (0.3 < _asp < 5.5):
                continue
            # Golden Rule 3: ダイアログ中心 X が 20%〜80% 範囲内のみ有効
            # 右端パネル・装飾要素による誤タップを防止
            _dcx = _x + _w // 2
            _dcy = _y + _h // 2
            if not (_W * 0.20 <= _dcx <= _W * 0.80):
                continue
            if abs(_dcy - _scy) > _H * 0.45:
                continue
            if _a > _best_area:
                _best_area = _a
                _frame = (_x, _y, _w, _h)

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
                    _tpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
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
            _close_tmpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
            _r = cv2.matchTemplate(
                cv2.imread(str(img_path), cv2.IMREAD_COLOR)[0: int(_H * 0.14), int(_W * 0.88):],
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
        if _DIALOG_NEXT_TEMPLATE.exists():
            _next_tmpl = cv2.imread(str(_DIALOG_NEXT_TEMPLATE))
            _r2 = cv2.matchTemplate(
                img[int(_H * 0.22): int(_H * 0.78), int(_W * 0.83):],
                _next_tmpl,
                cv2.TM_CCOEFF_NORMED,
            )
            _, _mv2, _, _ml2 = cv2.minMaxLoc(_r2)
            if _mv2 >= 0.75:
                _th2, _tw2 = _next_tmpl.shape[:2]
                return ("next",
                        int(_W * 0.83) + _ml2[0] + _tw2 // 2,
                        int(_H * 0.22) + _ml2[1] + _th2 // 2)

        _rx1n, _ry1n = int(_W * 0.83), int(_H * 0.22)
        _rx2n, _ry2n = _W, int(_H * 0.78)
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
        img = cv2.imread(str(img_or_path))
        if img is None:
            return 0
        H, W = img.shape[:2]
    else:
        img = img_or_path
    # ROI: 下部8%, 中央60%
    _y1 = int(H * 0.92)
    _x1 = int(W * 0.20)
    _x2 = int(W * 0.80)
    _roi = img[_y1:H, _x1:_x2]
    if _roi.size == 0:
        return 0
    _gray = cv2.cvtColor(_roi, cv2.COLOR_BGR2GRAY)
    _, _thr = cv2.threshold(_gray, 140, 255, cv2.THRESH_BINARY)
    _cnts, _ = cv2.findContours(_thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    _dot_count = 0
    for _c in _cnts:
        _a = cv2.contourArea(_c)
        if _a < 15 or _a > 400:
            continue
        _x, _y, _w, _h = cv2.boundingRect(_c)
        _asp = _w / max(_h, 1)
        if 0.5 < _asp < 2.0:  # roughly circular
            _dot_count += 1
    return _dot_count


def _detect_page_dots(img, H: int, W: int) -> bool:
    """画面下部にページドットインジケータが3個以上あるか。"""
    return count_page_dots(img, H, W) >= 3


def _detect_background_blur(img, H: int, W: int) -> bool:
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
        img = cv2.imread(str(img_path))
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
                _tpl = cv2.imread(str(_DIALOG_CLOSE_TEMPLATE))
                if (_roi_x.shape[0] >= _tpl.shape[0]
                        and _roi_x.shape[1] >= _tpl.shape[1]):
                    _r = cv2.matchTemplate(_roi_x, _tpl, cv2.TM_CCOEFF_NORMED)
                    _, _mv, _, _ = cv2.minMaxLoc(_r)
                    _has_close = _mv >= 0.65

        # 2b: ページドット (画面下部中央の小円群)
        _has_dots = _detect_page_dots(img, _H, _W)

        # 2c: 背景ぼかし (HSV彩度低下)
        _has_blur = _detect_background_blur(img, _H, _W)

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
    for _page in range(max_pages):
        # page=0 かつ initial_dlg が渡されている場合は外側の検出結果を再利用
        if _page == 0 and initial_dlg is not None:
            _dlg = initial_dlg
        else:
            _dlg = detect_dialog_frame_and_nav(
                analysis_path, W, H, roi=_roi,
                ocr_texts=ocr_texts,
            )
        if _dlg is None:
            logger.info("[PAGING] ダイアログ消失 (page=%d) → 完了", _page)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        _kind, _dx, _dy = _dlg
        if _kind == "close":
            tap_device(_dx, _dy, state, "PAGING_CLOSE")
            logger.info("[PAGING] ×タップ (page=%d) → クローズ完了", _page + 1)
            state.dialog_detections += 1
            return "DIALOG_CLOSED"
        # × ROI 輝度チェック: bright_pixels=0 が続く場合は強制脱出
        try:
            _img_c = cv2.imread(str(analysis_path))
            if _img_c is not None:
                _Hc, _Wc = _img_c.shape[:2]
                _close_roi_c = _img_c[0:int(_Hc * 0.14), int(_Wc * 0.88):]
                _gray_cl = cv2.cvtColor(_close_roi_c, cv2.COLOR_BGR2GRAY)
                _bright_cl = cv2.countNonZero(
                    cv2.threshold(_gray_cl, 155, 255, cv2.THRESH_BINARY)[1]
                )
                if _bright_cl == 0:
                    _no_close_streak += 1
                else:
                    _no_close_streak = 0
        except Exception as _e:
            logger.debug("[PAGING] × ROI 判定例外: %s", _e)
        if _no_close_streak >= 8:
            # × ボタンが画面右上に存在しない → 枠外 or 下部中央を叩いて強制脱出
            _esc_x, _esc_y = W // 2, H - 60
            logger.info(
                "[PAGING] × ROI 暗(%d回連続) → 強制脱出タップ(%d,%d)",
                _no_close_streak, _esc_x, _esc_y,
            )
            tap_device(_esc_x, _esc_y, state, "PAGING_ESCAPE")
            return "DIALOG_PAGING_TIMEOUT"
        # "next" or "bottom" → ▷ タップして次ページ
        tap_device(_dx, _dy, state, "PAGING_NEXT")
        logger.info("[PAGING] ▷タップ (page=%d/%d)", _page + 1, max_pages)
        state.dialog_detections += 1
        time.sleep(0.2)
        # 次ページのスクリーンショットを取得して解析
        _img_path, _aw, _ah, _ = take_screenshot()
        analysis_path = prepare_analysis_image(_img_path, _aw, _ah)
        # phash変化監視: 変化なし → ページが進んでいない → ループ中断
        _new_phash = compute_phash(analysis_path)
        if _prev_phash and _new_phash:
            _ph_dist = phash_distance(_prev_phash, _new_phash)
            if _ph_dist < 4:
                logger.info(
                    "[PAGING] ▷タップ後 phash変化なし(dist=%d<4) → 誤検出▷ → ループ中断",
                    _ph_dist,
                )
                return "DIALOG_PAGING_TIMEOUT"
        _prev_phash = _new_phash
    logger.warning("[PAGING] max_pages=%d 超過 → タイムアウト", max_pages)
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
        _img = cv2.imread(str(img_path))
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
def detect_tutorial_gold_swipe(img_path: Path) -> Optional[tuple[str, int, int, int, int]]:
    """
    HSVフィルタで金色チュートリアルポインター（手アイコン+軌跡）を検出し
    スワイプ方向と座標を返す。

    ユーザー指定HSV: Hue~30-50, Sat~100-250, Val~200-255
    OpenCV HSV では H は 0-180 (標準360°の半分)なのでH=15-50を使用。

    縦長領域(h>=w*2.5) のみ有効 (ボタン等との誤検出防止)。
    手アイコン(幅広部)が上半分 → SWIPE_UP、下半分 → SWIPE_DOWN。

    デバッグ画像: crawler/templates/debug/gold_detect_HHMMSS.png に自動保存。

    Returns: (direction, swipe_x, from_y, to_y, duration_ms) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

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
                                    right_half_only: bool = True
                                    ) -> Optional[tuple[int, int]]:
    """
    チュートリアルバトルで指アイコンが指し示す「金枠ハイライトボタン」を検出し
    タップ座標（ボタン中心）を返す。

    条件:
    - アスペクト比 0.5~2.0 (正方形〜縦長のボタン形状)
    - 面積 8000~150000px² (ボタン相当の大きさ)
    - 幅 100px以上 (細い軌跡線は除外)
    - right_half_only=True の場合: x中心 > W/2 のみ有効 (右側ボタン優先)

    デバッグ画像: crawler/templates/tutorial/gold_btn_HHMMSS.png に自動保存。
    Returns: (tap_x, tap_y) or None
    """
    try:
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        H_img, W_img = img.shape[:2]

        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        lower_gold = np.array([15, 60, 180], dtype=np.uint8)
        upper_gold = np.array([50, 255, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        # モルフォロジー: 枠線の隙間を埋めて矩形を繋ぐ
        k7 = np.ones((7, 7), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k7)
        mask = cv2.dilate(mask, k7, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None

        # ボタン候補: アスペクト比0.5~2.0 かつ面積8000~150000 かつ幅100px以上
        candidates = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < 8000 or area > 150000:
                continue
            x, y, w, h = cv2.boundingRect(c)
            if w < 100:
                continue
            aspect = h / max(w, 1)
            if 0.5 <= aspect <= 2.0:
                cx = x + w // 2
                cy = y + h // 2
                # 右半分のみフィルタ
                if right_half_only and cx < W_img * 0.5:
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


# ─── チュートリアル: 金色ハイライトボタンを全画面スキャンで検出 ──────────────
def find_golden_highlighted_button(img_path: Path) -> Optional[tuple[int, int]]:
    """
    チュートリアル指差しアイコンが指す「金色ハイライトされたボタン/カード」を
    HSV 色域スキャンで検出する。
    指の向き（上下左右）に依存しない方向非依存のアプローチ。

    返値: (cx, cy) ― 最大輝度の金色領域の中心座標、検出失敗時は None
    """
    try:
        img_bgr = cv2.imread(str(img_path))
        if img_bgr is None:
            return None
        h_img, w_img = img_bgr.shape[:2]

        hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)

        # 金色グロー: H=15-42, S=80-220, V=150-255 (より高輝度)
        lower = np.array([15, 80, 150], dtype=np.uint8)
        upper = np.array([42, 220, 255], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower, upper)

        # モルフォロジー: 枠線を繋げて矩形を再現
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (7, 7))
        mask = cv2.dilate(mask, kernel, iterations=3)
        mask = cv2.erode(mask, kernel, iterations=2)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # 最大面積の輪郭を採用 (小さなノイズを除外)
        valid = [(cv2.contourArea(c), c) for c in contours if cv2.contourArea(c) > 500]
        if not valid:
            return None

        _, best_cnt = max(valid, key=lambda x: x[0])
        rx, ry, rw, rh = cv2.boundingRect(best_cnt)
        cx = rx + rw // 2
        cy = ry + rh // 2
        logger.info("  [GoldHighlight] 金色ハイライト検出 rect=(%d,%d,%d,%d) → center=(%d,%d)",
                    rx, ry, rw, rh, cx, cy)
        return cx, cy

    except Exception as e:
        logger.debug("  [GoldHighlight] エラー: %s", e)
        return None


# ─── OCR テキスト検索ヘルパー ──────────────────────
# ─── 探索マップ 3D矢印 検出 ──────────────────────────
def find_3d_arrow(img_path: Path) -> Optional[tuple[int, int]]:
    """
    探索マップ上のキャラ頭上に浮かぶ3D矢印（白い曲線矢印）を検出。
    明るい白色コンターが最大のものを矢印とみなす。
    Returns: (cx, cy) or None
    """
    try:
        img = cv2.imread(str(img_path))
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
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
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

    def match_single(self, name: str, screenshot_path: Path) -> Optional[tuple[int, int, float]]:
        """指定テンプレート1枚だけをマッチング。Returns (cx, cy, score) or None."""
        data = self._templates.get(name)
        if data is None:
            return None
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        tmpl = data["img"]
        if tmpl.shape[0] > img.shape[0] or tmpl.shape[1] > img.shape[1]:
            return None
        try:
            res = cv2.matchTemplate(img, tmpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= data["threshold"]:
                h, w = tmpl.shape
                cx = max_loc[0] + w // 2
                cy = max_loc[1] + h // 2
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



# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


# ─── Result画面ハンドラ ──────────────────────────────
_RESULT_NEXT_X_RATIO = 0.785
_RESULT_NEXT_Y_RATIO = 0.914

# パーティ編成画面の除外キーワード (Lv.1 が出るが Result ではない)
_FORMATION_KWS = ["パーティ", "編成", "キオク", "ポートレイト", "自動編成"]


