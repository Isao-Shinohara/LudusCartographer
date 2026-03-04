#!/usr/bin/env python3
"""
android_setup.py — Android 実機セットアップ確認 & まどドラ探索起動ガイド

使い方:
  venv/bin/python tools/android_setup.py               # 全チェック実行
  venv/bin/python tools/android_setup.py --screenshot  # スクリーンショット + OCR のみ
  venv/bin/python tools/android_setup.py --session     # Appium セッションテスト
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

APPIUM_URL = "http://localhost:4723"
MADODRA_PACKAGE  = "com.aniplex.magia.exedra.jp"
MADODRA_ACTIVITY = "com.google.firebase.MessagingUnityPlayerActivity"


def ok(msg: str)   -> None: print(f"\033[32m[OK]\033[0m {msg}")
def warn(msg: str) -> None: print(f"\033[33m[WARN]\033[0m {msg}")
def fail(msg: str) -> None: print(f"\033[31m[NG]\033[0m {msg}")
def info(msg: str) -> None: print(f"[INFO] {msg}")


# ============================================================
# Step 1: adb デバイス確認
# ============================================================

def check_adb_device() -> str | None:
    """adb devices でデバイスを確認し、UDID を返す。"""
    print("\n--- 1. Android デバイス確認 (adb devices) ---")
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=10)
        lines = result.stdout.strip().splitlines()
        udids = [l.split("\t")[0] for l in lines[1:] if "\tdevice" in l]
        if not udids:
            fail("接続済み Android デバイスが見つかりません")
            print("  → Android デバイスを USB 接続し、USB デバッグを有効にしてください")
            return None
        udid = udids[0]
        ok(f"デバイス検出: {udid}")
        if len(udids) > 1:
            warn(f"複数デバイス接続中 — 最初の {udid} を使用します")
        return udid
    except FileNotFoundError:
        fail("adb コマンドが見つかりません。Android SDK platform-tools をインストールしてください")
        return None


# ============================================================
# Step 2: まどドラ インストール確認
# ============================================================

def check_madodra_installed(udid: str) -> tuple[str, str] | None:
    """まどドラがインストールされているか確認し、(package, activity) を返す。"""
    print("\n--- 2. まどドラ インストール確認 ---")
    try:
        result = subprocess.run(
            ["adb", "-s", udid, "shell", "pm", "list", "packages", MADODRA_PACKAGE],
            capture_output=True, text=True, timeout=10,
        )
        if MADODRA_PACKAGE in result.stdout:
            ok(f"パッケージ: {MADODRA_PACKAGE}")
            ok(f"アクティビティ: {MADODRA_ACTIVITY}")
            return MADODRA_PACKAGE, MADODRA_ACTIVITY
        else:
            fail(f"まどドラ ({MADODRA_PACKAGE}) がインストールされていません")
            return None
    except Exception as e:
        fail(f"パッケージ確認失敗: {e}")
        return None


# ============================================================
# Step 3: スクリーンショット & OCR
# ============================================================

def check_screenshot_ocr(udid: str) -> bool:
    """adb スクリーンショット + OCR でテキストを検出する。"""
    print("\n--- 3. スクリーンショット & OCR ---")
    evidence_dir = Path("/tmp/android_setup")
    evidence_dir.mkdir(exist_ok=True)
    png_path = evidence_dir / "screenshot.png"

    try:
        subprocess.run(
            ["adb", "-s", udid, "shell", "screencap", "-p", "/sdcard/_setup_test.png"],
            check=True, timeout=15,
        )
        subprocess.run(
            ["adb", "-s", udid, "pull", "/sdcard/_setup_test.png", str(png_path)],
            check=True, timeout=15,
        )
        ok(f"スクリーンショット取得: {png_path}")
    except Exception as e:
        fail(f"スクリーンショット失敗: {e}")
        return False

    # OCR
    try:
        from lc.ocr import run_ocr, format_results
        results = run_ocr(str(png_path), min_confidence=0.4)
        ok(f"OCR 検出: {len(results)} 件")
        print(format_results(results))
    except Exception as e:
        warn(f"OCR 失敗 (無視して続行): {e}")

    return True


# ============================================================
# Step 4: Appium サーバー確認
# ============================================================

def check_appium() -> bool:
    """Appium サーバーが起動しているか確認する。"""
    print("\n--- 4. Appium サーバー確認 (port 4723) ---")
    try:
        r = requests.get(f"{APPIUM_URL}/status", timeout=5)
        ready = r.json().get("value", {}).get("ready", False)
        if ready:
            ok("Appium 4723 で応答中")
            return True
        else:
            warn("Appium は起動しているが ready でない")
            return False
    except Exception:
        fail("Appium が起動していません")
        print("  → 別ターミナルで以下を実行してください:")
        print("     ANDROID_HOME=~/Library/Android/sdk \\")
        print("     ANDROID_SDK_ROOT=~/Library/Android/sdk \\")
        print(r"""     PATH="$HOME/.nodebrew/current/bin:$PATH" \""")
        print("     appium --port 4723")
        return False


# ============================================================
# Step 5: Appium セッション作成 + スクリーンショット
# ============================================================

def check_appium_session(udid: str, package: str, activity: str) -> str | None:
    """Appium セッションを作成してスクリーンショットを取得する。"""
    print(f"\n--- 5. Appium セッション作成 (UiAutomator2) ---")

    # 既存セッションをクリア
    try:
        r = requests.get(f"{APPIUM_URL}/sessions", timeout=5)
        for sess in r.json().get("value", []):
            sid = sess.get("id", "")
            if sid:
                requests.delete(f"{APPIUM_URL}/session/{sid}", timeout=10)
                info(f"既存セッション削除: {sid}")
    except Exception:
        pass

    payload = {
        "capabilities": {
            "alwaysMatch": {
                "platformName": "Android",
                "appium:automationName": "UiAutomator2",
                "appium:udid": udid,
                "appium:appPackage": package,
                "appium:appActivity": activity,
                "appium:noReset": True,
                "appium:newCommandTimeout": 300,
                "appium:uiautomator2ServerLaunchTimeout": 60000,
                "appium:adbExecTimeout": 30000,
            }
        }
    }

    info(f"セッション作成中 (パッケージ: {package})...")
    try:
        r = requests.post(f"{APPIUM_URL}/session", json=payload, timeout=120)
        data = r.json()
        sid = data.get("sessionId") or data.get("value", {}).get("sessionId", "")
        if not sid:
            fail(f"セッション作成失敗: {r.text[:300]}")
            return None
        ok(f"セッション作成成功: {sid}")
    except Exception as e:
        fail(f"セッション作成エラー: {e}")
        return None

    # スクリーンショット & サイズ
    time.sleep(3)
    try:
        r2 = requests.get(f"{APPIUM_URL}/session/{sid}/screenshot", timeout=30)
        img_b64 = r2.json().get("value", "")
        if img_b64:
            evidence_dir = Path("/tmp/android_setup")
            evidence_dir.mkdir(exist_ok=True)
            png_path = evidence_dir / "appium_screenshot.png"
            with open(str(png_path), "wb") as f:
                f.write(base64.b64decode(img_b64))
            ok(f"Appium スクリーンショット: {png_path}")

        r3 = requests.get(f"{APPIUM_URL}/session/{sid}/window/size", timeout=10)
        size = r3.json().get("value", {})
        ok(f"ウィンドウサイズ: {size.get('width')}×{size.get('height')} pt")

        # OCR
        try:
            from lc.ocr import run_ocr, format_results
            results = run_ocr(str(png_path), min_confidence=0.4)
            ok(f"OCR 検出: {len(results)} 件")
            print(format_results(results))
        except Exception as e:
            warn(f"OCR 失敗 (無視): {e}")

    except Exception as e:
        warn(f"スクリーンショット取得失敗: {e}")

    return sid


# ============================================================
# メイン
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Android セットアップ確認")
    parser.add_argument("--screenshot", action="store_true", help="スクリーンショット & OCR のみ")
    parser.add_argument("--session",    action="store_true", help="Appium セッションテストのみ")
    parser.add_argument("--udid",       default="",          help="デバイス UDID (省略時は自動検出)")
    args = parser.parse_args()

    print("=== Android セットアップ確認 ===\n")

    # Step 1: デバイス確認
    udid = args.udid or check_adb_device()
    if not udid:
        sys.exit(1)

    if args.screenshot:
        check_screenshot_ocr(udid)
        return

    # Step 2: まどドラ確認
    result = check_madodra_installed(udid)
    package  = result[0] if result else MADODRA_PACKAGE
    activity = result[1] if result else MADODRA_ACTIVITY

    # Step 3: スクリーンショット
    check_screenshot_ocr(udid)

    # Step 4: Appium 確認
    if not check_appium():
        print("\n  Appium 起動後に再実行してください。")
        sys.exit(1)

    if args.session:
        # Step 5: Appium セッション
        check_appium_session(udid, package, activity)
        return

    # 全チェック完了 → 探索コマンドを表示
    print("\n" + "=" * 60)
    print("✅ セットアップ完了！")
    print("\n【まどドラ探索コマンド】")
    print(f"""
  cd ~/Desktop/LudusCartographer/crawler
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\
  ANDROID_HOME=~/Library/Android/sdk \\
  ANDROID_SDK_ROOT=~/Library/Android/sdk \\
  venv/bin/python main.py "まどドラ" \\
    --android \\
    --package {package} \\
    --activity {activity} \\
    --android-udid {udid} \\
    --tap-wait 5.0 \\
    --stuck-threshold 3 \\
    --depth 4 \\
    --duration 600
""")


if __name__ == "__main__":
    main()
