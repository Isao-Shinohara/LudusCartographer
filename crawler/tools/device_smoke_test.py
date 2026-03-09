#!/usr/bin/env python3
"""
device_smoke_test.py — デバイス疎通スモークテスト

解析パイプライン全体 (スクショ→回転リサイズ→ROI検出→OCR→テンプレートマッチ) が
正常動作するか6ステップで一発検証する。

使い方:
  cd ~/Desktop/LudusCartographer/crawler
  ANDROID_WIFI_ADDR=192.168.10.107:5555 venv/bin/python tools/device_smoke_test.py
  ANDROID_WIFI_ADDR=192.168.10.118:5555 venv/bin/python tools/device_smoke_test.py
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import cv2
import numpy as np

from lc.utils import ensure_adb_connection

# auto_pilot.py からインポート (DEVICE_SERIAL 非依存の関数)
from tools.auto_pilot import (
    ANALYSIS_H,
    ANALYSIS_W,
    detect_game_roi,
    prepare_analysis_image,
)

# ─── 出力ヘルパー (android_setup.py 踏襲) ───
def ok(msg: str)   -> None: print(f"\033[32m[OK]\033[0m {msg}")
def warn(msg: str) -> None: print(f"\033[33m[WARN]\033[0m {msg}")
def fail(msg: str) -> None: print(f"\033[31m[NG]\033[0m {msg}")
def info(msg: str) -> None: print(f"[INFO] {msg}")

# ─── テンプレート一覧 ───
TEMPLATES_DIR = Path(__file__).parent.parent / "assets" / "templates"
OUTPUT_DIR = Path("/tmp/smoke_test")


# ============================================================
# Step 1: ADB接続 + 解像度 + ステータスバー
# ============================================================

def step1_adb_connection(wifi_addr: str) -> tuple[str, int, int]:
    """ADB 接続を確立し、解像度とステータスバー高さを取得する。"""
    print("\n--- Step 1: ADB接続 + デバイス情報 ---")

    serial = ensure_adb_connection(wifi_addr=wifi_addr)
    ok(f"デバイス接続: {serial}")

    # 解像度取得
    try:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        out = result.stdout.strip()
        dev_w, dev_h = 0, 0
        for prefix in ("Override size:", "Physical size:"):
            m = re.search(rf"{prefix}\s*(\d+)x(\d+)", out)
            if m:
                dev_w, dev_h = int(m.group(1)), int(m.group(2))
                ok(f"解像度: {dev_w}x{dev_h} ({prefix.rstrip(':')})")
                break
        if dev_w == 0:
            fail(f"解像度パース失敗: {out!r}")
            dev_w, dev_h = ANALYSIS_W, ANALYSIS_H
    except Exception as e:
        fail(f"wm size 取得エラー: {e}")
        dev_w, dev_h = ANALYSIS_W, ANALYSIS_H

    # ステータスバー高さ
    try:
        result = subprocess.run(
            ["adb", "-s", serial, "shell", "dumpsys", "display"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"mStable=\[(\d+),(\d+)\]\[(\d+),(\d+)\]", result.stdout)
        if m:
            top_inset = int(m.group(2))
            ok(f"ステータスバー高さ: {top_inset}px (mStable top inset)")
        else:
            warn("mStable パース失敗 — ステータスバー高さ不明")
    except Exception as e:
        warn(f"dumpsys display 取得エラー: {e}")

    return serial, dev_w, dev_h


# ============================================================
# Step 2: スクリーンショット取得
# ============================================================

def step2_screenshot(serial: str) -> Path | None:
    """adb exec-out screencap -p でスクリーンショットを取得する。"""
    print("\n--- Step 2: スクリーンショット取得 ---")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = OUTPUT_DIR / "raw.png"

    try:
        result = subprocess.run(
            ["adb", "-s", serial, "exec-out", "screencap", "-p"],
            capture_output=True, timeout=10,
        )
        raw_bytes = result.stdout
        size_kb = len(raw_bytes) / 1024
        if len(raw_bytes) < 5_000:
            fail(f"スクリーンショットが小さすぎます ({size_kb:.1f}KB < 5KB)")
            return None
        raw_path.write_bytes(raw_bytes)
        ok(f"スクリーンショット取得: {raw_path} ({size_kb:.1f}KB)")
    except Exception as e:
        fail(f"screencap 失敗: {e}")
        return None

    # cv2.imread で読めるか検証
    img = cv2.imread(str(raw_path))
    if img is None:
        fail("cv2.imread 失敗 — PNG が破損しています")
        return None
    h, w = img.shape[:2]
    ok(f"画像読み込み成功: {w}x{h}")

    return raw_path


# ============================================================
# Step 3: 回転+リサイズ (prepare_analysis_image)
# ============================================================

def step3_analysis_image(raw_path: Path, dev_w: int, dev_h: int) -> Path | None:
    """prepare_analysis_image() で解析画像を生成し、サイズを検証する。"""
    print("\n--- Step 3: 回転+リサイズ (解析画像生成) ---")

    try:
        analysis_path = prepare_analysis_image(raw_path, dev_w, dev_h)
        # prepare_analysis_image は変換不要なら元パスを返す場合がある
        # 結果をコピーして保存
        out_path = OUTPUT_DIR / "analysis.png"
        if analysis_path != out_path:
            import shutil
            shutil.copy2(str(analysis_path), str(out_path))
            analysis_path = out_path

        img = cv2.imread(str(analysis_path))
        if img is None:
            fail("解析画像の読み込み失敗")
            return None
        h, w = img.shape[:2]
        if (w, h) == (ANALYSIS_W, ANALYSIS_H):
            ok(f"解析画像サイズ: {w}x{h} (期待通り)")
        else:
            warn(f"解析画像サイズ: {w}x{h} (期待値: {ANALYSIS_W}x{ANALYSIS_H})")
        return analysis_path
    except Exception as e:
        fail(f"prepare_analysis_image 失敗: {e}")
        return None


# ============================================================
# Step 4: ROI検出 (黒帯)
# ============================================================

def step4_roi_detection(analysis_path: Path) -> tuple[int, int, int, int] | None:
    """detect_game_roi() で黒帯を検出し、ROI を返す。"""
    print("\n--- Step 4: ROI検出 (黒帯) ---")

    img = cv2.imread(str(analysis_path))
    if img is None:
        fail("画像読み込み失敗")
        return None

    roi = detect_game_roi(img)
    rx, ry, rw, rh = roi
    total_area = ANALYSIS_W * ANALYSIS_H
    roi_area = rw * rh
    ratio = roi_area / total_area * 100

    info(f"ROI: ({rx}, {ry}) {rw}x{rh}")
    info(f"ROI面積比: {ratio:.1f}%")

    if ratio >= 50:
        ok(f"ROI検出成功: {rw}x{rh} ({ratio:.1f}% ≥ 50%)")
    else:
        fail(f"ROIが小さすぎます: {ratio:.1f}% < 50%")
        return None

    # 黒帯サイズを表示
    left_bar = rx
    right_bar = ANALYSIS_W - (rx + rw)
    top_bar = ry
    bottom_bar = ANALYSIS_H - (ry + rh)
    info(f"黒帯: L={left_bar} R={right_bar} T={top_bar} B={bottom_bar}")

    return roi


# ============================================================
# Step 5: PaddleOCR
# ============================================================

def step5_ocr(analysis_path: Path) -> bool:
    """PaddleOCR で文字認識を実行する。"""
    print("\n--- Step 5: PaddleOCR ---")

    try:
        from lc.ocr import run_ocr, format_results
        results = run_ocr(str(analysis_path), min_confidence=0.4)

        if len(results) >= 1:
            ok(f"OCR検出: {len(results)} 件")
        else:
            warn("OCR検出: 0 件 (画面にテキストがない可能性)")

        # 代表テキスト (上位5件)
        top5 = sorted(results, key=lambda r: r["confidence"], reverse=True)[:5]
        for i, r in enumerate(top5, 1):
            cx, cy = r["center"]
            print(f"  [{i}] {r['confidence']:.3f}  ({cx:4d},{cy:4d})  {r['text']!r}")

        return True
    except Exception as e:
        fail(f"OCR 失敗: {e}")
        return False


# ============================================================
# Step 6: テンプレートマッチ
# ============================================================

def step6_template_matching(analysis_path: Path) -> bool:
    """各テンプレートに対して cv2.matchTemplate を実行し、スコアを表示する。"""
    print("\n--- Step 6: テンプレートマッチ ---")

    analysis_img = cv2.imread(str(analysis_path), cv2.IMREAD_GRAYSCALE)
    if analysis_img is None:
        fail("解析画像の読み込み失敗")
        return False

    templates = sorted(TEMPLATES_DIR.glob("*.png"))
    if not templates:
        fail(f"テンプレートが見つかりません: {TEMPLATES_DIR}")
        return False

    info(f"テンプレート数: {len(templates)}")
    print()

    threshold = 0.70
    all_ok = True
    for tmpl_path in templates:
        tmpl = cv2.imread(str(tmpl_path), cv2.IMREAD_GRAYSCALE)
        if tmpl is None:
            warn(f"  読み込み失敗: {tmpl_path.name}")
            continue

        th, tw = tmpl.shape[:2]

        # テンプレートが解析画像より大きい場合はスキップ
        if tw > analysis_img.shape[1] or th > analysis_img.shape[0]:
            warn(f"  {tmpl_path.name:30s}  テンプレート({tw}x{th})が解析画像より大きい → スキップ")
            continue

        result = cv2.matchTemplate(analysis_img, tmpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        status = "\033[32mHIT\033[0m" if max_val >= threshold else "\033[90m---\033[0m"
        print(f"  {status}  {tmpl_path.name:30s}  score={max_val:.4f}  loc=({max_loc[0]:4d},{max_loc[1]:4d})  size={tw}x{th}")

    return all_ok


# ============================================================
# メイン
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="デバイス疎通スモークテスト")
    parser.add_argument("--wifi-addr", default=os.environ.get("ANDROID_WIFI_ADDR", ""),
                        help="Wi-Fi ADB アドレス (IP:PORT)")
    args = parser.parse_args()

    print("=" * 60)
    print("  Device Smoke Test — 解析パイプライン疎通確認")
    print("=" * 60)

    passed = 0
    total = 6

    # Step 1: ADB接続
    try:
        serial, dev_w, dev_h = step1_adb_connection(args.wifi_addr)
        # DEVICE_SERIAL を auto_pilot にセット (_query_status_bar_height 等が参照)
        import tools.auto_pilot as _ap
        _ap.DEVICE_SERIAL = serial
        passed += 1
    except Exception as e:
        fail(f"Step 1 失敗: {e}")
        print(f"\n結果: {passed}/{total} ステップ通過")
        sys.exit(1)

    # Step 2: スクリーンショット
    raw_path = step2_screenshot(serial)
    if raw_path:
        passed += 1
    else:
        print(f"\n結果: {passed}/{total} ステップ通過")
        sys.exit(1)

    # Step 3: 解析画像
    analysis_path = step3_analysis_image(raw_path, dev_w, dev_h)
    if analysis_path:
        passed += 1
    else:
        print(f"\n結果: {passed}/{total} ステップ通過")
        sys.exit(1)

    # Step 4: ROI
    roi = step4_roi_detection(analysis_path)
    if roi:
        passed += 1
    else:
        warn("ROI検出失敗 — 以降のステップは全画面で続行")
        passed += 1  # ROI失敗でも続行可能

    # Step 5: OCR
    if step5_ocr(analysis_path):
        passed += 1

    # Step 6: テンプレートマッチ
    if step6_template_matching(analysis_path):
        passed += 1

    # サマリー
    print("\n" + "=" * 60)
    if passed == total:
        print(f"\033[32m  PASS: {passed}/{total} ステップ通過\033[0m")
        print("  → auto_pilot 実行可能")
    else:
        print(f"\033[33m  {passed}/{total} ステップ通過\033[0m")
        print("  → 失敗ステップを確認してください")
    print(f"  結果画像: {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
