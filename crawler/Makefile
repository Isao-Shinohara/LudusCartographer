# ============================================================
# LudusCartographer — Makefile (macOS)
# ============================================================
# 使用方法:
#   make connect   — ADB Wi-Fi 接続（切断時の復旧）
#   make run       — scrcpy + auto_pilot 起動
#   make restart   — ADB 再接続 → 再起動
#   make stop      — auto_pilot / scrcpy 停止
#   make ss        — 現在のスクリーンショットを /tmp/ss.png に保存して表示
# ============================================================

# ---- 変数定義 ------------------------------------------------
TARGET_IP   ?= 192.168.10.118
TARGET_PORT ?= 5555
SERIAL       = $(TARGET_IP):$(TARGET_PORT)

VENV        ?= venv
PYTHON       = $(VENV)/bin/python

SCRCPY_FLAGS ?= -S --always-on-top --no-audio -m 800 --window-title "Madodora-Auto"

# ---- ターゲット -----------------------------------------------

.PHONY: connect run restart stop ss help

## ADB Wi-Fi 接続（切断・タイムアウト後の復旧）
connect:
	@echo ">>> ADB connect $(SERIAL)"
	adb connect $(SERIAL)
	adb -s $(SERIAL) get-state

## scrcpy ミラーリング + auto_pilot 起動（フォアグラウンド）
run: connect
	@echo ">>> scrcpy 起動中 (バックグラウンド)"
	scrcpy -s $(SERIAL) $(SCRCPY_FLAGS) &
	@sleep 1
	@echo ">>> auto_pilot 起動"
	ANDROID_UDID=$(SERIAL) $(PYTHON) -u tools/auto_pilot.py

## ADB 再接続 → auto_pilot 再起動
restart: stop connect run

## auto_pilot / scrcpy を停止
stop:
	@echo ">>> プロセス停止"
	-pkill -f "auto_pilot.py"  2>/dev/null || true
	-pkill -f "scrcpy"         2>/dev/null || true
	@sleep 1
	@echo ">>> 停止完了"

## 現在のスクリーンショット取得 → /tmp/ss.png
ss:
	@adb -s $(SERIAL) shell screencap -p /sdcard/ss.png
	@adb -s $(SERIAL) pull /sdcard/ss.png /tmp/ss.png
	@echo ">>> /tmp/ss.png に保存しました"
	@open /tmp/ss.png 2>/dev/null || echo "open コマンド非対応環境"

## ヘルプ
help:
	@echo ""
	@echo "  make connect   — ADB Wi-Fi 接続復旧"
	@echo "  make run       — auto_pilot 起動 (scrcpy 含む)"
	@echo "  make restart   — 停止 → 再接続 → 再起動"
	@echo "  make stop      — 全プロセス停止"
	@echo "  make ss        — スクリーンショット取得"
	@echo ""
	@echo "  カスタム IP 指定例:"
	@echo "    make run TARGET_IP=192.168.1.200"
	@echo ""
