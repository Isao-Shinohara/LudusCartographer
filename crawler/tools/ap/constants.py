"""
ap/constants.py — auto_pilot 全定数
"""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

# プロジェクトルート
_CRAWLER_ROOT = Path(__file__).parent.parent.parent

# OS 非依存の一時ディレクトリ
_TMPDIR = Path(tempfile.gettempdir())
SCREENSHOT_PATH = _TMPDIR / "lc_autopilot.png"
ANALYSIS_PATH   = _TMPDIR / "lc_autopilot_analysis.png"
REMOTE_PATH = "/sdcard/lc_autopilot.png"
EVIDENCE_DIR = _CRAWLER_ROOT / "evidence" / f"autopilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# ─── タイミング ───
MAX_ITERATIONS = 2000
POLL_INTERVAL = 0.2         # phash ポーリング間隔 (秒)
PHASH_THRESHOLD = 5         # phash 距離 >= 5 → 画面変化あり
FORCE_ANALYZE_AFTER = 1     # phash 変化なし連続 N 回 → 強制 OCR
STALL_TIMEOUT = 15.0        # 強制OCRでもタップできず続く秒数 → スタック介入
BATTLE_WAIT = 0.0           # バトル待機 (tap_device の MIN_TAP_INTERVAL=1.0s が保証)
DOWNLOAD_WAIT = 10.0
MIN_TAP_INTERVAL = 1.0      # 全場面共通: タップ間隔は最低1.0秒

# ─── Watchdog: デッドロック自動復旧 ───
WATCHDOG_DEADLOCK_THRESHOLD = 600.0  # 10分以上画面変化なし → デッドロック判定
WATCHDOG_MAX_SOFT_RECOVERIES = 3     # force-stop再起動の最大回数 (超えたら人間に報告)
WATCHDOG_MAX_TOTAL_RECOVERIES = 3    # 合計3回で諦めて人間を待つ (pm clear は使わない)
APP_PACKAGE = "com.aniplex.magia.exedra.jp"
APP_ACTIVITY = "com.google.firebase.MessagingUnityPlayerActivity"
# Watchdog免除シーン
WATCHDOG_EXEMPT_ACTIONS = frozenset([
    "DOWNLOAD_WAIT", "BATTLE_WAIT", "LOADING_WAIT",
    "NOTICE_DISMISS", "GO_CHUI_AGREE", "GO_CHUI_FALLBACK",
    "MAIN_STORY_LOADING",
    "GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN", "GOLD_SWIPE_LEFT", "GOLD_SWIPE_RIGHT",
    "GRIND_QUEST_NAV",
    "MOVIE_WAIT", "ADV_WAIT",
])
ADV_RAPID_PHASH_MAX = 25    # ADV高速モード: phash がこれ以下なら OCR スキップ連打
BLACKOUT_BRIGHTNESS = 20

# ─── デバッグ画像保存フラグ (--verbose で True に切替) ───
_DEBUG_SAVE_IMAGES = False

# ─── 動的しきい値: Gold UI アクション後即解析対象 ───
_GOLD_UI_ACTIONS: frozenset = frozenset([
    "GOLD_BTN_TAP", "MOYA_TAP", "BATTLE_TUTORIAL", "SKILL_CARD_TUTORIAL",
    "HISSATSU_TUTORIAL", "BUFF_TUTORIAL", "GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN",
])

# ─── シーン再評価: 同一アクション連続閾値 ───
_SCENE_REEVAL_THRESHOLD = 5

# ─── 確認ダイアログ キーワード (複数箇所で共有) ───
_CONFIRM_POS_KWS: list[str] = ["OK", "はい", "わかった", "了解", "決定", "許可", "Allow", "ALLOW", "リトライ", "Retry"]
_CONFIRM_NEG_KWS: list[str] = ["キャンセル", "いいえ", "戻る", "やめる", "許可しない", "拒否", "Deny"]

# ─── UI テキスト判定キーワード (動画ガード用) ────────────────────────────
_UI_TEXT_KWS: tuple = ("利用規約", "同意", "規約", "プライバシー", "ダウンロード",
                       "Download", "OK", "はい", "キャンセル", "設定", "お知らせ")

# ─── match_single() 専用テンプレート (一般マッチから除外) ───
_SINGLE_ONLY: frozenset = frozenset([
    "adv_next_btn",
    "adv_icon_menu", "adv_icon_log", "adv_icon_auto", "adv_icon_ff", "adv_icon_skip",
])

# ─── ダイアログ・ファースト: 検知キーワード一覧 ───────────────────────────────
_DIALOG_FIRST_KWS: frozenset = frozenset([
    # バトル/ロール説明
    "ロールについて", "ロールは全部", "STEP1", "STEP2", "バトルシステム", "ブレイクし",
    "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "DEFENDER", "HEALER",
    "アタッカー", "ブレイカー", "バッファー", "デバッファー", "ディフェンダー", "ヒーラー",
    # パーティ/編成
    "ポートレイト", "キオクを最大", "ポジションを", "前衛", "後衛",
    "各キオク", "パーティを組", "チームを組",
    # マギアボックス/素材
    "マギアボックス", "最大24時間", "素材が溜", "プレイヤーLv",
])

# ─── バトル判定キーワード ──────────────────────────────────────────
_BATTLE_CORE_KWS: frozenset = frozenset([
    "通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
    "必殺技", "BREAK", "WAVE", "Turn",
])

# BATTLE 高速パス: OCR 前テンプレートマッチングで使用。
_BATTLE_UI_KWS: frozenset = frozenset([
    "通常攻撃", "単体攻撃", "单体攻撃",
    "WAVE", "Turn", "ターン", "必殺技",
])

# ─── 解析基準解像度 ───
ANALYSIS_W = 1520
ANALYSIS_H = 720

# ─── 座標補正定数 ───
_OCR_BBOX_Y_PADDING = 30       # PaddleOCR bbox 下部パディング補正
_GLOW_CENTER_Y_OFFSET = 35     # 発光ブロブ重心→ボタン視覚中心
_GOLD_BTN_RETRY_Y_OFFSET = 30  # 金枠ボタン Y下方リトライ
_FINGER_TIP_RATIO = 0.1        # 指ブロブ上端10% = 指先位置

# ─── 解析空間レイアウト定数 (デバイス非依存: ANALYSIS_W/H 基準) ───
_RIGHT_PANEL_X      = int(ANALYSIS_W * 0.69)    # 1050: 右パネル境界
_CHAR_HEAD_X1       = int(ANALYSIS_W * 0.33)    # 500:  キャラ頭上エリア左端
_CHAR_HEAD_X2       = int(ANALYSIS_W * 0.69)    # 1050: キャラ頭上エリア右端
_CHAR_HEAD_Y1       = int(ANALYSIS_H * 0.17)    # 120:  キャラ頭上エリア上端
_CHAR_HEAD_Y2       = int(ANALYSIS_H * 0.39)    # 280:  キャラ頭上エリア下端
_SPATIAL_MARGIN_TOP = int(ANALYSIS_H * 0.05)    # 36:   上端システムUI除外マージン
_CLOSE_BTN_OFFSET   = int(ANALYSIS_H * 0.056)   # 40:   右上×ボタンのオフセット

OCR_LANG = "japan"
OCR_MIN_CONF = 0.3

# ─── シーン分類ポーリング間隔 ───
SCENE_INTERVAL = {
    "BATTLE":  0.2,
    "ADV":     0.2,
    "STORY":   0.2,
    "LOADING": 1.0,
    "MENU":    0.2,
    "UNKNOWN": 0.2,
}

# ─── 即時アクション: 代替タップ候補を収集しない (副作用/待機系) ───
_IMMEDIATE_ACTIONS: frozenset = frozenset([
    "DOWNLOAD_WAIT", "MAIN_STORY_LOADING", "LOADING_WAIT",
    "GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN", "GOLD_SWIPE_LEFT", "GOLD_SWIPE_RIGHT",
    "SWIPE_UP", "SWIPE_FALLBACK", "SWIPE_AUTO",
    "SETTINGS_BACK", "TEXT_INPUT_NAME", "NAME_INPUT_TEXT",
    "GACHA_FREEZE_RECOVER", "NOTICE_DISMISS", "NOTICE_POPUP_CLOSE",
    "TUTORIAL_POPUP", "AGREE", "AGREE_TOS",
    "HOME_REACHED", "GRIND_COMPLETE",
    "WAIT_FOR_CHANGE", "MAINTENANCE_WAIT", "UPDATE_WAIT", "UPDATE_DIALOG",
    "BATTLE_WAIT", "MOVIE_WAIT", "ADV_WAIT", "HOME_NAV_WAIT",
    "STORY_SKIP_CANCEL", "SCENE_TAP",
])

# テレメトリ定数
_TRANSITION_SLOW_SEC = 10.0
_TRANSITION_HISTORY_MAX = 100
