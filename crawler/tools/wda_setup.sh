#!/usr/bin/env bash
# =============================================================
# wda_setup.sh — 実機 WDA セットアップ & Bundle ID 取得
# 使い方: bash tools/wda_setup.sh
# =============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
fail() { echo -e "${RED}[NG]${NC} $*"; }

echo "=== WDA セットアップ確認スクリプト ==="
echo ""

# --- 1. USB デバイス確認 ---
echo "--- 1. iPhone USB 接続確認 ---"
UDID=$(idevice_id -l 2>/dev/null | head -1)
if [ -z "$UDID" ]; then
  fail "iPhone が USB 接続されていません"
  echo "  → iPhone を USB ケーブルで接続し、「信頼」をタップしてから再実行"
  exit 1
fi
ok "UDID: $UDID"

# --- 2. iproxy 起動 ---
echo ""
echo "--- 2. iproxy (WDA ポート転送) ---"
if pgrep -x iproxy > /dev/null 2>&1; then
  ok "iproxy すでに実行中"
else
  warn "iproxy を起動します: iproxy 8100 8100 &"
  iproxy 8100 8100 &
  IPROXY_PID=$!
  sleep 2
  if kill -0 $IPROXY_PID 2>/dev/null; then
    ok "iproxy 起動成功 (PID=$IPROXY_PID)"
  else
    fail "iproxy 起動失敗"
    exit 1
  fi
fi

# --- 3. Appium サーバー確認 ---
echo ""
echo "--- 3. Appium サーバー確認 (port 4723) ---"
if curl -s --connect-timeout 2 http://localhost:4723/status > /dev/null 2>&1; then
  ok "Appium 4723 で応答中"
else
  warn "Appium が起動していません"
  echo "  → 別ターミナルで以下を実行してください:"
  echo "     PATH=\"\$HOME/.nodebrew/current/bin:\$PATH\" appium --port 4723"
  echo ""
  echo "  Appium を起動してから、このスクリプトを再実行してください"
  exit 1
fi

# --- 4. WDA 起動確認 ---
echo ""
echo "--- 4. WebDriverAgent (port 8100) ---"
WDA_RESP=$(curl -s --connect-timeout 3 http://localhost:8100/status 2>/dev/null || echo "")
if echo "$WDA_RESP" | grep -q '"ready".*true'; then
  IS_SIM=$(echo "$WDA_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
info = d.get('value', {})
# /wda/device/info で確認が必要
print('sim_hint:', 'simulatorVersion' in str(info))
" 2>/dev/null || echo "unknown")
  ok "WDA 8100 で応答中"

  # 実機かシミュレータか確認
  DEV_INFO=$(curl -s http://localhost:8100/wda/device/info 2>/dev/null || echo "{}")
  IS_SIMULATOR=$(echo "$DEV_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
v = d.get('value', {})
print(v.get('isSimulator', 'unknown'))
" 2>/dev/null || echo "unknown")

  if [ "$IS_SIMULATOR" = "True" ] || [ "$IS_SIMULATOR" = "true" ]; then
    warn "WDA がシミュレータに接続中です"
    echo "  → 実機用 WDA を起動するには Appium セッションを作成してください"
    echo "  → 次の STEP で自動セットアップします"
  else
    ok "WDA が実機に接続中 (isSimulator=$IS_SIMULATOR)"
  fi
else
  warn "WDA port 8100 が応答しません"
  echo "  → Appium が XCUITest セッションを起動すると WDA が自動インストールされます"
fi

# --- 5. Appium セッション作成 → WDA 実機接続 ---
echo ""
echo "--- 5. Appium セッション作成 (実機 WDA 起動) ---"

SESSION_PAYLOAD=$(cat <<JSON
{
  "capabilities": {
    "alwaysMatch": {
      "platformName": "iOS",
      "appium:automationName": "XCUITest",
      "appium:udid": "$UDID",
      "appium:bundleId": "com.apple.Preferences",
      "appium:noReset": true,
      "appium:newCommandTimeout": 300,
      "appium:wdaLocalPort": 8100
    }
  }
}
JSON
)

echo "  セッション作成中 (UDID=$UDID)..."
SESSION_RESP=$(curl -s -X POST \
  -H "Content-Type: application/json" \
  -d "$SESSION_PAYLOAD" \
  --connect-timeout 120 \
  http://localhost:4723/session 2>/dev/null || echo "")

SESSION_ID=$(echo "$SESSION_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
sid = d.get('sessionId') or d.get('value', {}).get('sessionId', '')
print(sid)
" 2>/dev/null || echo "")

if [ -n "$SESSION_ID" ]; then
  ok "セッション作成成功: $SESSION_ID"
  echo "$SESSION_ID" > /tmp/wda_session_id.txt
else
  fail "セッション作成失敗"
  echo "  レスポンス: ${SESSION_RESP:0:300}"
  exit 1
fi

# --- 6. Bundle ID 取得 ---
echo ""
echo "--- 6. フォアグラウンドアプリ確認 ---"
echo "  ※ 今すぐ iPhone でまどドラを開いてください..."
echo "  (10秒待機)"
sleep 10

APP_INFO=$(curl -s "http://localhost:4723/session/$SESSION_ID/wda/activeAppInfo" 2>/dev/null || \
           curl -s "http://localhost:8100/wda/activeAppInfo" 2>/dev/null || echo "{}")

BUNDLE_ID=$(echo "$APP_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
v = d.get('value', {})
print(v.get('bundleId', 'unknown'))
" 2>/dev/null || echo "unknown")

APP_NAME=$(echo "$APP_INFO" | python3 -c "
import sys, json
d = json.load(sys.stdin)
v = d.get('value', {})
print(v.get('name', ''))
" 2>/dev/null || echo "")

ok "アクティブアプリ: bundleId=$BUNDLE_ID  name=$APP_NAME"
echo "$BUNDLE_ID" > /tmp/madodra_bundle_id.txt

# --- 7. タップテスト ---
echo ""
echo "--- 7. 目と手の連動テスト (OCR → WDA タップ) ---"
cd "$(dirname "$0")/.."

PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True venv/bin/python - <<PYEOF
import sys, os, json, requests, time
sys.path.insert(0, ".")
os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

SESSION_ID = open("/tmp/wda_session_id.txt").read().strip()
APPIUM_BASE = f"http://localhost:4723/session/{SESSION_ID}"

# UxPlay から1フレームキャプチャ
from tools.window_manager import find_mirroring_window_ex, capture_region
from lc.ocr import run_ocr
import cv2, numpy as np

print("  [1/4] UxPlay フレームキャプチャ中...")
result = find_mirroring_window_ex(["UxPlay", "QuickTime Player", "iPhone", "scrcpy"])
if not result:
    print("  [NG] UxPlay ウィンドウが見つかりません")
    sys.exit(1)

rect, owner = result
img = capture_region(rect)
win_w, win_h = img.shape[1], img.shape[0]
print(f"  [OK] キャプチャ: {win_w}x{win_h}px  owner={owner!r}")

cv2.imwrite("/tmp/tap_test_before.png", img)

# OCR でタップ可能テキストを探す
print("  [2/4] OCR 解析中...")
ocr_results = run_ocr("/tmp/tap_test_before.png", min_confidence=0.5)
print(f"  [OK] {len(ocr_results)} 件検出")

if not ocr_results:
    print("  [SKIP] テキスト未検出 — スキップ")
    sys.exit(0)

# 最も信頼スコアの高いテキストを選択
best = max(ocr_results, key=lambda r: r["confidence"])
px, py = best["center"]
text = best["text"]
conf = best["confidence"]
print(f"  [TARGET] '{text}' conf={conf:.2f} pixel=({px},{py})")

# WDA デバイスサイズ取得
try:
    r = requests.get(f"{APPIUM_BASE}/window/size", timeout=5)
    size = r.json().get("value", {})
    dev_w = size.get("width", 393)
    dev_h = size.get("height", 852)
except:
    dev_w, dev_h = 393, 852

# ウィンドウ座標 → デバイス論理座標に変換
tap_x = int(px * dev_w / win_w)
tap_y = int(py * dev_h / win_h)
print(f"  [COORD] pixel({px},{py}) → device({tap_x},{tap_y})  scale=({dev_w/win_w:.2f},{dev_h/win_h:.2f})")

# WDA でタップ
print(f"  [3/4] WDA タップ実行: ({tap_x},{tap_y})...")
tap_payload = {"x": tap_x, "y": tap_y}
r = requests.post(
    f"http://localhost:8100/wda/tap/0",
    json=tap_payload, timeout=10
)
print(f"  [WDA tap] status={r.status_code}  resp={r.text[:100]}")

time.sleep(1.5)

# アフタースクリーン
result2 = find_mirroring_window_ex(["UxPlay", "QuickTime Player", "iPhone", "scrcpy"])
if result2:
    rect2, _ = result2
    img_after = capture_region(rect2)
    cv2.imwrite("/tmp/tap_test_after.png", img_after)

    # 画面変化チェック
    diff = cv2.absdiff(img, img_after)
    change = float(diff.mean())
    print(f"  [4/4] 画面変化量: {change:.2f}  {'→ 変化あり ✅' if change > 3 else '→ 変化なし（タップ後もUI同じ）'}")

print()
print("=== タップテスト完了 ===")
print(f"  before: /tmp/tap_test_before.png")
print(f"  after:  /tmp/tap_test_after.png")
PYEOF

echo ""
echo "=== セットアップ完了 ==="
echo ""
echo "まどドラ Bundle ID: $(cat /tmp/madodra_bundle_id.txt 2>/dev/null || echo '未取得')"
echo ""
echo "【次のステップ】まどドラを iPhone で開いてから以下を実行:"
echo ""
BUNDLE=$(cat /tmp/madodra_bundle_id.txt 2>/dev/null || echo "BUNDLE_ID_HERE")
echo "  cd ~/Desktop/LudusCartographer/crawler"
echo "  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\"
echo "    venv/bin/python main.py \"まどドラ\" \\"
echo "    --mirror \\"
echo "    --bundle $BUNDLE \\"
echo "    --tap-wait 5.0 \\"
echo "    --stuck-threshold 3 \\"
echo "    --depth 4 \\"
echo "    --duration 600 \\"
echo "    --open-web"
