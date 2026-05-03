#!/bin/bash
# ─── まどドラ自律操縦ランチャー ───
# macOS 26 の Vision framework が Terminal フォアグラウンドプロセスで
# SIGBUS クラッシュするため、nohup でバックグラウンド実行する。
#
# 使い方:
#   ./tools/run_autopilot.sh -S -s            # 途中再開 (スクリーン記録有効)
#   ./tools/run_autopilot.sh -S -s -r         # 新規アカウント (--reinstall)
#   ./tools/run_autopilot.sh -S -s -c 3       # 3周回 (--cycles)
#   ./tools/run_autopilot.sh -S -s -c 0       # 無限周回
#   ./tools/run_autopilot.sh --help           # ヘルプ表示
#
# オプション:
#   -S, --screenshot   スクリーン記録を有効化（UI地図の構築に必須）
#   -s, --stop-on-home ホーム画面到達で停止
#   -r, --reinstall    アプリを再インストールして新規アカウントで開始
#   -c N, --cycles N   N周回実行 (0 = 無限)
#   -V N, --version N  バージョンID指定 (未指定=Activeバージョン)
#
# Ctrl+C で自動操縦も停止します。
#
# ログ監視のみ (別ターミナル):
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
# 操縦カテゴリ (Phase 2): --operation 未指定時のフォールバック
export OPERATION="${OPERATION:-tutorial}"

# 残留 tail プロセスを掃除 (前回の cleanup が不完全だった場合)
pkill -f "tail -f $LOG_FILE" 2>/dev/null || true

# 既存プロセスチェック
if pgrep -f "auto_pilot.py" > /dev/null 2>&1; then
    echo "⚠️  auto_pilot.py は既に実行中です (PID: $(pgrep -f auto_pilot.py | head -1))"
    echo "   ログ: tail -f $LOG_FILE"
    exit 1
fi

# バックグラウンド起動 (nohup 不要 — trap で SIGHUP を処理)
cd "$CRAWLER_DIR"
venv/bin/python -u tools/auto_pilot.py "$@" >"$LOG_FILE" 2>&1 </dev/null &
PID=$!

# Ctrl+C / ターミナル終了時にバックグラウンドプロセスも停止
cleanup() {
    echo ""
    echo "🛑 自動操縦を停止します (PID: $PID)..."
    echo "   詳細はログ参照: tail -f $LOG_FILE"
    kill "$PID" 2>/dev/null
    [ -n "${TAIL_PID:-}" ] && kill "$TAIL_PID" 2>/dev/null
    # 停止待ち (定期的に進捗を表示)
    _wait_secs=0
    while kill -0 "$PID" 2>/dev/null; do
        sleep 2
        _wait_secs=$((_wait_secs + 2))
        if [ $((_wait_secs % 10)) -eq 0 ]; then
            echo "   停止待機中... (${_wait_secs}秒経過 / 最終ログ: $(tail -1 "$LOG_FILE" 2>/dev/null | head -c 100))"
        fi
        if [ "$_wait_secs" -ge 120 ]; then
            echo "   ⚠️ 120秒経過 → 強制終了"
            kill -9 "$PID" 2>/dev/null
            break
        fi
    done
    echo "   停止完了 (${_wait_secs}秒)"
    exit 0
}
trap cleanup INT TERM EXIT

echo "🚀 auto_pilot 起動 (PID: $PID)"
echo "   引数: $*"
echo "   ログ: $LOG_FILE"
echo ""
echo "   Ctrl+C で自動操縦を停止します"
echo ""

# 起動確認 (3秒待って生存チェック)
sleep 3
if kill -0 "$PID" 2>/dev/null; then
    echo "✅ 起動成功。ログ監視を開始します..."
    echo ""
    tail -f "$LOG_FILE" &
    TAIL_PID=$!
    # auto_pilot の終了を待つ (正常停止 or クラッシュ)
    wait "$PID" 2>/dev/null
    EXIT_CODE=$?
    kill "$TAIL_PID" 2>/dev/null
    # trap の cleanup が二重実行されないよう解除
    trap - INT TERM EXIT
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo ""
        echo "✅ 自動操縦が正常終了しました (exit code: $EXIT_CODE)"
    else
        echo ""
        echo "⚠️  自動操縦が終了しました (exit code: $EXIT_CODE)"
    fi
else
    # trap 解除してから終了
    trap - INT TERM EXIT
    echo "❌ 起動失敗。ログを確認してください:"
    cat "$LOG_FILE"
    exit 1
fi
