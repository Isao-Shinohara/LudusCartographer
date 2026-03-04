#!/usr/bin/env python3
"""
tap_test.py — UxPlay (目) + WDA (手) の連動テスト

使い方:
  # Appium セッション ID を引数で渡す（appium_session_idがある場合）
  venv/bin/python tools/tap_test.py --session <SESSION_ID>

  # Appium から自動取得（UDID が分かっている場合）
  venv/bin/python tools/tap_test.py --udid <DEVICE_UDID>

  # Bundle ID をログに出力するだけ（タップなし）
  venv/bin/python tools/tap_test.py --bundle-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

APPIUM_URL = "http://localhost:4723"
WDA_URL    = "http://localhost:8100"

# ============================================================
# ユーティリティ
# ============================================================

def _log(tag: str, msg: str) -> None:
    print(f"[{tag}] {msg}")


def get_active_app_bundle(session_id: str | None = None) -> str:
    """フォアグラウンドアプリの Bundle ID を取得。"""
    endpoints = []
    if session_id:
        endpoints.append(f"{APPIUM_URL}/session/{session_id}/wda/activeAppInfo")
    endpoints.append(f"{WDA_URL}/wda/activeAppInfo")

    for url in endpoints:
        try:
            r = requests.get(url, timeout=5)
            data = r.json().get("value", {})
            bundle_id = data.get("bundleId", "")
            if bundle_id:
                _log("APP", f"bundleId={bundle_id}  name={data.get('name', '')!r}  pid={data.get('pid', '')}")
                return bundle_id
        except Exception as e:
            _log("WARN", f"{url}: {e}")
    return ""


def create_appium_session(udid: str) -> str:
    """Appium セッションを作成して session_id を返す。"""
    _log("APPIUM", f"セッション作成中 UDID={udid} ...")
    payload = {
        "capabilities": {
            "alwaysMatch": {
                "platformName": "iOS",
                "appium:automationName": "XCUITest",
                "appium:udid": udid,
                "appium:bundleId": "com.apple.Preferences",
                "appium:noReset": True,
                "appium:newCommandTimeout": 300,
                "appium:wdaLocalPort": 8100,
                "appium:usePrebuiltWDA": True,
            }
        }
    }
    r = requests.post(f"{APPIUM_URL}/session", json=payload, timeout=120)
    data = r.json()
    sid = data.get("sessionId") or data.get("value", {}).get("sessionId", "")
    if not sid:
        raise RuntimeError(f"セッション作成失敗: {r.text[:300]}")
    _log("APPIUM", f"セッション作成成功: {sid}")
    return sid


def get_device_size(session_id: str) -> tuple[int, int]:
    """デバイスの論理サイズ (width, height) を返す。"""
    try:
        r = requests.get(f"{APPIUM_URL}/session/{session_id}/window/size", timeout=5)
        v = r.json().get("value", {})
        return int(v.get("width", 393)), int(v.get("height", 852))
    except Exception:
        return 393, 852


def wda_tap(x: int, y: int) -> dict:
    """WDA で座標タップ。"""
    r = requests.post(f"{WDA_URL}/wda/tap/0", json={"x": x, "y": y}, timeout=10)
    return r.json()


# ============================================================
# メイン処理
# ============================================================

def run_tap_test(session_id: str, bundle_only: bool = False) -> None:
    from tools.window_manager import capture_region, find_mirroring_window_ex
    from lc.ocr import run_ocr

    evidence_dir = Path("/tmp/tap_test")
    evidence_dir.mkdir(exist_ok=True)

    # --- 1. Bundle ID 取得 ---
    _log("STEP1", "フォアグラウンドアプリの Bundle ID を取得")
    bundle_id = get_active_app_bundle(session_id)
    if bundle_id:
        Path("/tmp/madodra_bundle_id.txt").write_text(bundle_id)
        print(f"\n  ✅ Bundle ID: {bundle_id}\n")
    else:
        _log("WARN", "Bundle ID 取得失敗 — WDA に接続できていない可能性があります")

    if bundle_only:
        return

    # --- 2. UxPlay キャプチャ ---
    _log("STEP2", "UxPlay フレームキャプチャ")
    result = find_mirroring_window_ex(["UxPlay", "QuickTime Player", "iPhone", "scrcpy"])
    if not result:
        _log("NG", "UxPlay ウィンドウが見つかりません")
        return
    rect, owner = result
    img_before = capture_region(rect)
    win_h, win_w = img_before.shape[:2]
    _log("CAPTURE", f"owner={owner!r}  {win_w}x{win_h}px")
    cv2.imwrite(str(evidence_dir / "before.png"), img_before)

    # --- 3. OCR ---
    _log("STEP3", "OCR 解析 (min_confidence=0.5)")
    ocr_results = run_ocr(str(evidence_dir / "before.png"), min_confidence=0.5)
    _log("OCR", f"{len(ocr_results)} 件検出")

    if not ocr_results:
        _log("SKIP", "テキスト未検出 — タップテストをスキップ")
        _log("HINT", "まどドラを起動してホーム画面が表示された状態で再実行してください")
        return

    # OCR 結果を表示（y座標順）
    for r in sorted(ocr_results, key=lambda x: x["center"][1]):
        cx, cy = r["center"]
        print(f"     [{r['confidence']:.2f}] ({cx:4d},{cy:4d})  {r['text']!r}")

    # タップ対象: 最高信頼スコアのテキスト
    target = max(ocr_results, key=lambda r: r["confidence"])
    px, py = target["center"]
    _log("TARGET", f"'{target['text']}' conf={target['confidence']:.2f} pixel=({px},{py})")

    # --- 4. 座標変換 ---
    dev_w, dev_h = get_device_size(session_id)
    tap_x = int(px * dev_w / win_w)
    tap_y = int(py * dev_h / win_h)
    scale_x = dev_w / win_w
    scale_y = dev_h / win_h
    _log("COORD", f"pixel({px},{py}) → device({tap_x},{tap_y})  scale=({scale_x:.3f},{scale_y:.3f})")

    # デバッグ: タップ位置を画像に描画
    img_debug = img_before.copy()
    cv2.circle(img_debug, (px, py), 15, (0, 0, 255), 3)
    cv2.putText(img_debug, f"({tap_x},{tap_y})", (px + 20, py),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
    cv2.imwrite(str(evidence_dir / "tap_target.png"), img_debug)

    # --- 5. WDA タップ ---
    _log("STEP4", f"WDA タップ: device({tap_x},{tap_y})")
    resp = wda_tap(tap_x, tap_y)
    status = resp.get("value", resp)
    _log("WDA", f"レスポンス: {str(status)[:120]}")

    time.sleep(2.0)

    # --- 6. アフタースクリーン & 変化確認 ---
    result2 = find_mirroring_window_ex(["UxPlay", "QuickTime Player", "iPhone", "scrcpy"])
    if result2:
        rect2, _ = result2
        img_after = capture_region(rect2)
        cv2.imwrite(str(evidence_dir / "after.png"), img_after)

        diff = cv2.absdiff(img_before, img_after)
        change = float(diff.mean())
        _log("DIFF", f"画面変化量: {change:.2f}  {'→ 変化あり ✅ タップが効いています' if change > 3 else '→ 変化なし'}")

    print()
    print("=== テスト完了 ===")
    print(f"  before:     {evidence_dir}/before.png")
    print(f"  tap_target: {evidence_dir}/tap_target.png  (赤丸がタップ予定座標)")
    print(f"  after:      {evidence_dir}/after.png")
    print()
    if bundle_id:
        print(f"  Bundle ID: {bundle_id}")
        print()
        print("【探索コマンド】")
        print(f"  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\")
        print(f"  venv/bin/python main.py \"まどドラ\" \\")
        print(f"    --mirror \\")
        print(f"    --bundle {bundle_id} \\")
        print(f"    --tap-wait 5.0 \\")
        print(f"    --stuck-threshold 3 \\")
        print(f"    --depth 4 \\")
        print(f"    --duration 600 \\")
        print(f"    --open-web")


def main() -> None:
    parser = argparse.ArgumentParser(description="UxPlay + WDA 連動タップテスト")
    parser.add_argument("--session",     help="Appium セッション ID（省略時は自動作成）")
    parser.add_argument("--udid",        help="デバイス UDID（--session 未指定時に使用）")
    parser.add_argument("--bundle-only", action="store_true",
                        help="Bundle ID の取得のみ行い、タップはしない")
    args = parser.parse_args()

    # セッション ID を解決
    session_id = args.session

    if not session_id:
        # 既存セッションを再利用
        try:
            r = requests.get(f"{APPIUM_URL}/sessions", timeout=5)
            sessions = r.json().get("value", [])
            if sessions:
                session_id = sessions[0].get("id", "")
                _log("REUSE", f"既存セッション: {session_id}")
        except Exception:
            pass

    if not session_id and args.udid:
        session_id = create_appium_session(args.udid)

    if not session_id:
        # WDA 直接接続（Appium セッションなし）
        _log("INFO", "Appium セッションなし — WDA 直接接続モード")
        session_id = "DIRECT"

    run_tap_test(session_id, bundle_only=args.bundle_only)


if __name__ == "__main__":
    main()
