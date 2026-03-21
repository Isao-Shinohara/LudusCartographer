#!/bin/bash
# ─── まどドラ自律操縦ランチャー ───
# macOS 26 の Vision framework が Terminal フォアグラウンドプロセスで
# SIGBUS クラッシュするため、nohup でバックグラウンド実行する。
#
# 使い方:
#   ./tools/run_autopilot.sh                  # 途中再開
#   ./tools/run_autopilot.sh --fresh-install  # 新規アカウント
#   ./tools/run_autopilot.sh --help           # ヘルプ表示
#
# 停止方法:
#   pkill -f auto_pilot.py
#
# ログ監視:
#   tail -f /tmp/auto_pilot.log

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CRAWLER_DIR="$(dirname "$SCRIPT_DIR")"
LOG_FILE="/tmp/auto_pilot.log"

# 環境変数
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True
export ANDROID_HOME="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
export ANDROID_SDK_ROOT="${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}"
export PATH="/opt/homebrew/bin:$HOME/.nodebrew/current/bin:$PATH"

# 既存プロセスチェック
if pgrep -f "auto_pilot.py" > /dev/null 2>&1; then
    echo "⚠️  auto_pilot.py は既に実行中です (PID: $(pgrep -f auto_pilot.py | head -1))"
    echo "   停止: pkill -f auto_pilot.py"
    echo "   ログ: tail -f $LOG_FILE"
    exit 1
fi

# nohup でバックグラウンド起動
cd "$CRAWLER_DIR"
nohup venv/bin/python -u tools/auto_pilot.py "$@" > "$LOG_FILE" 2>&1 &
PID=$!

echo "🚀 auto_pilot 起動 (PID: $PID)"
echo "   引数: $*"
echo "   ログ: $LOG_FILE"
echo ""
echo "📋 コマンド:"
echo "   監視: tail -f $LOG_FILE"
echo "   停止: pkill -f auto_pilot.py"
echo ""

# 起動確認 (3秒待って生存チェック)
sleep 3
if kill -0 "$PID" 2>/dev/null; then
    echo "✅ 起動成功。ログ監視を開始します..."
    echo "   (Ctrl+C でログ監視を終了。プロセスは動き続けます)"
    echo ""
    tail -f "$LOG_FILE"
else
    echo "❌ 起動失敗。ログを確認してください:"
    cat "$LOG_FILE"
    exit 1
fi
