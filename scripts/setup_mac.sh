#!/usr/bin/env bash
# =============================================================
# LudusCartographer — Mac セットアップスクリプト
# =============================================================
# 使い方:
#   chmod +x scripts/setup_mac.sh
#   ./scripts/setup_mac.sh
#
# 前提: macOS 12+ (Monterey 以上), Xcode Command Line Tools インストール済み
# =============================================================

set -euo pipefail

# カラー出力
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo ""
echo "======================================================"
echo "  LudusCartographer — Mac セットアップ"
echo "======================================================"
echo ""

# -------------------------------------------------------
# 1. Homebrew
# -------------------------------------------------------
info "1/8 Homebrew を確認..."
if ! command -v brew &>/dev/null; then
    warn "Homebrew が見つかりません。インストールします..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || eval "$(/usr/local/bin/brew shellenv)"
else
    info "  Homebrew $(brew --version | head -1) — OK"
fi

# -------------------------------------------------------
# 2. Node.js v18 LTS (nodebrew 経由)
#    Appium 2.x は Node.js v18 LTS が必要 (v21 は ERR_REQUIRE_ESM で失敗)
# -------------------------------------------------------
info "2/8 Node.js v18 LTS を確認..."
if ! command -v nodebrew &>/dev/null; then
    warn "  nodebrew が見つかりません。インストールします..."
    brew install nodebrew
    mkdir -p "$HOME/.nodebrew/src"
fi

if ! nodebrew ls | grep -q "v18"; then
    info "  Node.js v18.20.8 をインストール..."
    nodebrew install v18.20.8
fi

nodebrew use v18.20.8
export PATH="$HOME/.nodebrew/current/bin:$PATH"
NODE_VER=$(node --version)
info "  Node.js $NODE_VER — OK"

# -------------------------------------------------------
# 3. libimobiledevice + ios-deploy (iOS ツール)
# -------------------------------------------------------
info "3/8 iOS ツールを確認..."
for pkg in libimobiledevice ideviceinstaller ios-deploy; do
    if brew list "$pkg" &>/dev/null; then
        info "  $pkg — already installed"
    else
        info "  $pkg をインストール..."
        brew install "$pkg"
    fi
done

# -------------------------------------------------------
# 4. android-platform-tools (adb)
# -------------------------------------------------------
info "4/8 android-platform-tools (adb) を確認..."
if brew list android-platform-tools &>/dev/null; then
    info "  android-platform-tools — already installed"
else
    info "  android-platform-tools をインストール..."
    brew install android-platform-tools
fi
info "  adb $(adb version | head -1) — OK"

# -------------------------------------------------------
# 5. Python 3 仮想環境
# -------------------------------------------------------
info "5/8 Python 仮想環境を確認..."
VENV_DIR="$PROJECT_ROOT/crawler/venv"
if [ ! -d "$VENV_DIR" ]; then
    info "  仮想環境を作成: $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

info "  依存パッケージをインストール..."
REQUIREMENTS="$PROJECT_ROOT/crawler/requirements.txt"
if [ -f "$REQUIREMENTS" ]; then
    "$VENV_DIR/bin/pip" install -q --upgrade pip
    "$VENV_DIR/bin/pip" install -q -r "$REQUIREMENTS"
    info "  requirements.txt インストール完了"
else
    warn "  crawler/requirements.txt が見つかりません (スキップ)"
fi

# -------------------------------------------------------
# 6. Appium 2.x + ドライバーインストール
# -------------------------------------------------------
info "6/8 Appium を確認..."
if ! command -v appium &>/dev/null || [[ "$(appium --version 2>/dev/null)" < "2" ]]; then
    info "  Appium 2.x をインストール..."
    npm install -g appium
fi
APPIUM_VER=$(appium --version)
info "  Appium $APPIUM_VER — OK"

# xcuitest ドライバー
info "  xcuitest ドライバーを確認..."
if appium driver list 2>&1 | grep -q "xcuitest.*installed"; then
    info "  xcuitest — already installed"
else
    info "  xcuitest ドライバーをインストール..."
    appium driver install xcuitest
fi

# uiautomator2 ドライバー
info "  uiautomator2 ドライバーを確認..."
if appium driver list 2>&1 | grep -q "uiautomator2.*installed"; then
    info "  uiautomator2 — already installed"
else
    info "  uiautomator2 ドライバーをインストール..."
    appium driver install uiautomator2
fi

# -------------------------------------------------------
# 7. .env ファイル作成
# -------------------------------------------------------
info "7/8 設定ファイルを確認..."
ENV_EXAMPLE="$PROJECT_ROOT/crawler/config/.env.example"
ENV_FILE="$PROJECT_ROOT/crawler/config/.env"
if [ ! -f "$ENV_FILE" ] && [ -f "$ENV_EXAMPLE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "  .env を作成しました: $ENV_FILE"
    warn "  必ず IOS_BUNDLE_ID を設定してください:"
    warn "    vi $ENV_FILE"
else
    info "  .env — OK"
fi

# -------------------------------------------------------
# 8. 接続診断の実行
# -------------------------------------------------------
info "8/8 デバイス接続診断..."
cd "$PROJECT_ROOT/crawler"
"$VENV_DIR/bin/python" -c "
from lc.utils import diagnose_device_connection
import json
report = diagnose_device_connection()
print(json.dumps(report, indent=2, ensure_ascii=False))
" 2>/dev/null || warn "診断に失敗しました (デバイス未接続の可能性)"

echo ""
echo "======================================================"
echo "  セットアップ完了!"
echo "======================================================"
echo ""
echo "  次のステップ:"
echo "  1. iPhone を USB 接続して「信頼」をタップ"
echo "  2. IOS_BUNDLE_ID を設定:"
echo "     export IOS_BUNDLE_ID='com.apple.Preferences'"
echo "  3. 疎通確認スクリプトを実行:"
echo "     cd crawler"
echo "     venv/bin/python appium/minimal_launch.py"
echo ""
echo "  ※ Appium サーバーは minimal_launch.py が自動起動します"
echo ""
