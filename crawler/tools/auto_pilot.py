#!/usr/bin/env python3
"""
auto_pilot.py — まどドラ自律操縦スクリプト (ハイブリッド版)

1秒 phash ポーリング → 5秒変化なしで強制 OCR → 指差しアイコン最優先タップ。
WAIT_FOR_CHANGE 後の閾値引き上げを廃止し、デッドロックを根絶。

使い方:
    cd crawler
    PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True \\
    venv/bin/python -u tools/auto_pilot.py
"""
from __future__ import annotations

import argparse
import gc
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

# ─── SIGSEGV 防止: OpenMP / cv2 スレッド競合対策 ─────────────────
# OpenMP スレッドの重複を許可 (PaddlePaddle + OpenCV 共存時のSIGSEGV防止)
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
# OpenMP スレッド数を制限してメモリ競合を防ぐ
os.environ.setdefault("OMP_NUM_THREADS", "2")
# OpenCV のスレッド数も制限
os.environ.setdefault("OPENCV_VIDEOIO_PRIORITY_MSMF", "0")
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルート
_CRAWLER_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_CRAWLER_ROOT))

from lc.ocr import run_ocr, find_text, find_best, format_results
from lc.utils import get_android_serial, compute_phash, phash_distance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_pilot")

# ─── 設定 ────────────────────────────────────────────
try:
    DEVICE_SERIAL = get_android_serial()
except RuntimeError as e:
    logger.error(str(e))
    sys.exit(1)

# OS 非依存の一時ディレクトリ (Windows=AppData/Temp, macOS=/var/folders, Linux=/tmp)
_TMPDIR = Path(tempfile.gettempdir())
SCREENSHOT_PATH = _TMPDIR / "lc_autopilot.png"
ANALYSIS_PATH   = _TMPDIR / "lc_autopilot_analysis.png"
REMOTE_PATH = "/sdcard/lc_autopilot.png"
EVIDENCE_DIR = _CRAWLER_ROOT / "evidence" / f"autopilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# ─── タイミング ───
MAX_ITERATIONS = 2000
POLL_INTERVAL = 0.3         # phash ポーリング間隔 (秒) — 高速化
PHASH_THRESHOLD = 5         # phash 距離 >= 5 → 画面変化あり
FORCE_ANALYZE_AFTER = 3     # phash 変化なし連続 N 回 → 強制 OCR — 高速化
STALL_TIMEOUT = 20.0        # 強制OCRでもタップできず続く秒数 → スタック介入
BATTLE_WAIT = 0.8           # バトル待機 — 高速化 (旧1.5)
DOWNLOAD_WAIT = 10.0

# ─── Watchdog: デッドロック自動復旧 ───
WATCHDOG_DEADLOCK_THRESHOLD = 600.0  # 10分以上画面変化なし → デッドロック判定
WATCHDOG_MAX_SOFT_RECOVERIES = 3     # force-stop再起動の最大回数 (超えたら人間に報告)
WATCHDOG_MAX_TOTAL_RECOVERIES = 3    # 合計3回で諦めて人間を待つ (pm clear は使わない)
APP_PACKAGE = "com.aniplex.magia.exedra.jp"
APP_ACTIVITY = "com.google.firebase.MessagingUnityPlayerActivity"
# Watchdog免除シーン: これらのシーンでは意図的に待機中なのでWatchdogを発動しない
WATCHDOG_EXEMPT_ACTIONS = frozenset([
    "DOWNLOAD_WAIT", "BATTLE_WAIT", "LOADING_WAIT",
    "NOTICE_DISMISS", "GO_CHUI_AGREE", "GO_CHUI_FALLBACK",  # ご注意: 初期化待ち
    "MAIN_STORY_LOADING",  # MAIN STORY ローディング背景: 自動遷移待ち
    "GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN", "GOLD_SWIPE_LEFT", "GOLD_SWIPE_RIGHT",  # チュートリアル移動
])
ADV_RAPID_PHASH_MAX = 25    # ADV高速モード: phash がこれ以下なら OCR スキップ連打
BLACKOUT_BRIGHTNESS = 20

# ─── ダイアログ・ファースト: 検知キーワード一覧 ───────────────────────────────
# detect_and_act #0-DIALOG ブロックで使用。枠検出に失敗した場合の OCR 補助トリガー。
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

# ─── 解析基準解像度 ───
ANALYSIS_W = 1520
ANALYSIS_H = 720

# 排除された偽の指ブロブキャッシュ (debug_latest_tap.png への [REJECTED] 描画用)
_rejected_finger_blobs: list = []

OCR_LANG = "japan"
OCR_MIN_CONF = 0.3

# ─── scrcpy 管理 ───
SCRCPY_DEVICE = "192.168.10.118:5555"
SCRCPY_ARGS = [
    "scrcpy",
    "-s", SCRCPY_DEVICE,
    "--turn-screen-off",   # 物理画面消灯
    "--stay-awake",
    "--always-on-top",
    "--no-audio",
    "-m", "800",
]

# Ctrl+C シグナルハンドラ用: main() で設定する PilotState への参照
_pilot_state_ref: Optional["PilotState"] = None


@dataclass
class PilotState:
    """操縦状態"""
    iteration: int = 0
    last_phash: str = ""
    stall_start: float = 0.0
    stall_corner_tried: bool = False
    last_action: str = ""
    last_ocr_texts: list = field(default_factory=list)
    battle_wait_count: int = 0
    auto_activated: bool = False
    home_reached: bool = False
    total_taps: int = 0
    total_ocr_calls: int = 0
    total_ocr_skipped: int = 0
    total_blackout_skipped: int = 0
    screenshots_saved: int = 0
    device_w: int = 0
    device_h: int = 0
    # 強制解析カウンタ (phash 変化なしの連続回数)
    same_phash_count: int = 0
    # 完全凍結フレームカウンタ (phash_dist=0 の連続回数) — 階層型Watchdog用
    consecutive_frozen_frames: int = 0
    # 最後に強制解析を実行した時刻
    last_forced_ocr_at: float = 0.0
    # 同一位置の指差しブロブ連続検出カウンタ (誤検出抑制)
    last_blob_xy: tuple = (0, 0)
    blob_same_count: int = 0
    # フリーバトル: 左キャラ選択済みフラグ (True なら次は右スキルを優先)
    char_just_selected: bool = False
    # 発光SMバトル: キャラ選択済みフラグ (GLOW State Machine 用)
    character_selected: bool = False
    # チュートリアルポップアップ連続タップ回数 (高くなると異なる座標を試す)
    pre_popup_tap_count: int = 0
    # 現在のシーン分類 (BATTLE / ADV / LOADING / MENU / UNKNOWN)
    current_scene: str = "UNKNOWN"
    # StrategicDecisionEngine: 予測トラッキング
    last_prediction: str = ""
    last_prediction_desc: str = ""
    last_tap_text: str = ""
    last_action_pre_phash: str = ""
    # ホーム画面からクエスト等への遷移試行回数 (遷移中の誤停止を防ぐ)
    home_nav_count: int = 0
    # ─── Watchdog ───
    # 最後に画面変化(phash変化)を検出した時刻
    last_screen_change_time: float = field(default_factory=time.time)
    # Watchdog による復旧試行回数
    watchdog_recovery_count: int = 0
    # ─── ダウンロード進捗ログ ───
    # 最後にダウンロード進捗をログ出力した時刻
    last_download_progress_log: float = 0.0
    # ─── スクリーンショット破損リトライ統計 ───
    screenshot_retry_count: int = 0  # SIGSEGV防止リトライ発生回数
    # GoldSwipe連続検出カウンタ (N回超えたらOCRへフォールバック)
    gold_swipe_count: int = 0
    # ─── デバッグ: 最新スクリーンショット (numpy ndarray) ───
    last_screen: object = None  # cv2.imread 結果を格納 (型ヒント省略でdataclass互換)
    # ─── ROI: ゲーム描画領域 (レターボックス除外) ───
    # detect_game_roi() で更新: (roi_x, roi_y, roi_w, roi_h)
    game_roi: tuple = (0, 0, ANALYSIS_W, ANALYSIS_H)
    # ─── アナリティクス: 主要検知カウンタ ───
    dialog_detections: int = 0   # ダイアログ検知成功 (DIALOG_CLOSE/NEXT)
    finger_detections: int = 0   # 指アイコン検知成功 (MOYA_TAP)
    gold_detections: int = 0     # 金枠ボタン検知成功 (FINGER+GOLD_FRAME)
    total_loop_ms: float = 0.0   # 全ループ経過時間合計 [ms] (平均算出用)
    # ─── ダイアログclose累計試行 (リセットなし、エスカレーション用) ───
    dialog_close_total: int = 0  # close失敗が蓄積 → 8回でBACK, 12回でスキップ


# ─── シーン分類 ──────────────────────────────────────
# シーン別ポーリング間隔 (ユーザー指定)
SCENE_INTERVAL = {
    "BATTLE":  0.5,   # バトル画面: 爆速反応 (旧1.0→0.5)
    "ADV":     1.0,   # アドベンチャー/会話: 最速反応
    "STORY":   0.5,   # ストーリー(スキップなし): 爆速化 (旧2.0→0.5)
    "LOADING": 5.0,   # ロード中: 負荷軽減
    "MENU":    1.0,   # ホーム/メニュー
    "UNKNOWN": 1.0,   # 不明
}

def classify_scene(texts: list[str], last_action: str) -> tuple[str, float]:
    """
    OCR テキストからシーンを分類し (scene_label, poll_interval) を返す。
    - BATTLE  : バトル画面 — 戦闘固有キーワードあり
    - ADV     : アドベンチャー — スキップボタンあり or 直前に STORY_TAP
    - STORY   : ストーリー送り — スキップなし・会話テキストのみ
    - LOADING : ロード/ダウンロード中
    - MENU    : ホーム/メニュー画面
    - UNKNOWN : 判定不能
    """
    joined = " ".join(texts)
    if any(kw in joined for kw in ["ダウンロード", "Loading", "Now Loading", "ロード中", "通信中"]):
        return "LOADING", SCENE_INTERVAL["LOADING"]
    if any(kw in joined for kw in ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                                    "必殺技", "BREAK", "WAVE", "ENEMY TURN", "Turn"]):
        return "BATTLE", SCENE_INTERVAL["BATTLE"]
    if any(kw in joined for kw in ["クエスト", "ショップ", "ガシャ", "ガチャ",
                                    "ホーム", "メニュー", "お知らせ", "編成", "光の間"]):
        return "MENU", SCENE_INTERVAL["MENU"]
    # ADV = スキップボタンあり（能動的に会話が進む）
    if any(kw in joined for kw in ["スキップ", "SKIP"]):
        return "ADV", SCENE_INTERVAL["ADV"]
    # STORY = 直前アクションが会話送り、またはスキップなし会話テキスト
    if last_action in ("STORY_TAP", "ADV_RAPID_TAP", "STORY_TAP_HINT"):
        return "STORY", SCENE_INTERVAL["STORY"]
    # STORY ヒューリスティック: 長い日本語文章 (8文字超 + ひらがな含む) が2件以上
    story_lines = [t for t in texts if len(t) >= 8 and
                   any(0x3041 <= ord(c) <= 0x30FF for c in t)]
    if len(story_lines) >= 2:
        return "STORY", SCENE_INTERVAL["STORY"]
    return "UNKNOWN", SCENE_INTERVAL["UNKNOWN"]


# ─── ADB ユーティリティ ─────────────────────────────
def adb(cmd: str) -> str:
    full = f"adb -s {DEVICE_SERIAL} {cmd}"
    try:
        result = subprocess.run(
            full, shell=True, capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.warning("adb timeout: %s", cmd)
        return ""


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
        import cv2 as _cv2r
        import numpy as _npr
        gray = _cv2r.cvtColor(img, _cv2r.COLOR_BGR2GRAY)
        _H, _W = img.shape[:2]
        # 列/行ごとの輝度ピクセル数
        col_bright = (_npr.array(gray, dtype=_npr.int32) > 12).sum(axis=0)
        row_bright = (_npr.array(gray, dtype=_npr.int32) > 12).sum(axis=1)
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


def _correct_btn_tap_y(img, cx: int, cy: int, box: list) -> int:
    """
    OCR検出ボタン (OK/はい など) のタップY座標を補正する。

    まどドラのゲームボタンはOCR枠が実際のボタン視覚領域より下に延びることがある。
    (枠中心が暗いピクセル域に落ちる) → 中心が暗い場合は枠top付近から上方向を走査して
    輝度の高い領域（実際のボタン面）の中心を返す。

    Args:
        img : cv2 imread 済み画像 (BGRまたはNone)
        cx  : OCR枠の中心X
        cy  : OCR枠の中心Y
        box : OCR box [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
    Returns: 補正後のタップY座標 (補正不要時はcyをそのまま返す)
    """
    try:
        import cv2 as _cv2b
        import numpy as _npb
        if img is None:
            return cy
        gray = _cv2b.cvtColor(img, _cv2b.COLOR_BGR2GRAY)
        H, W = img.shape[:2]
        # 中心ピクセルの輝度確認
        cy_c = max(0, min(cy, H - 1))
        cx_c = max(0, min(cx, W - 1))
        if gray[cy_c, cx_c] >= 60:
            return cy  # 十分明るい → 補正不要
        # 枠top Y を取得 (box[0][1])
        box_top = int(box[0][1])
        # 枠top から上100px の範囲でX=cx付近の輝度を走査
        scan_start = max(0, box_top - 100)
        scan_end = min(H - 1, box_top + 5)
        half_w = 40
        x0 = max(0, cx - half_w)
        x1 = min(W, cx + half_w)
        bright_ys = []
        for y in range(scan_start, scan_end):
            row_mean = float(_npb.mean(gray[y, x0:x1]))
            if row_mean >= 60:
                bright_ys.append(y)
        if bright_ys:
            corrected = (bright_ys[0] + bright_ys[-1]) // 2
            return corrected
        return cy
    except Exception:
        return cy


def get_device_resolution() -> tuple[int, int]:
    """
    `adb shell wm size` で実機の物理解像度を取得する。

    - landscape デバイスでは "Physical size: 1520x720" のように返る
    - Override がある場合は "Override size: ..." が優先される
    - 取得失敗時は (ANALYSIS_W, ANALYSIS_H) をフォールバックとして返す

    Returns: (width, height)
    """
    try:
        _out = subprocess.run(
            ["adb", "-s", DEVICE_SERIAL, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        # "Override size: 1520x720" を優先、なければ "Physical size: 1520x720"
        for _prefix in ("Override size:", "Physical size:"):
            _m = re.search(rf"{_prefix}\s*(\d+)x(\d+)", _out)
            if _m:
                _w, _h = int(_m.group(1)), int(_m.group(2))
                logger.info("[WM_SIZE] %s → %dx%d", _prefix.rstrip(":"), _w, _h)
                return _w, _h
        logger.warning("[WM_SIZE] パース失敗: %r — フォールバック %dx%d", _out, ANALYSIS_W, ANALYSIS_H)
    except Exception as _e:
        logger.warning("[WM_SIZE] 取得エラー: %s — フォールバック %dx%d", _e, ANALYSIS_W, ANALYSIS_H)
    return ANALYSIS_W, ANALYSIS_H


def take_screenshot(retries: int = 3, min_bytes: int = 50_000) -> tuple[Optional[Path], int, int, int]:
    """
    スクリーンショット取得。破損PNG によるSIGSEGV防止のためリトライ付き。

    - retries: 破損検出時の再試行回数
    - min_bytes: 正常PNGの最小ファイルサイズ (50KB未満は破損と判定)
    Returns: (path, width, height, retry_count)
            path=None の場合は全リトライ失敗（呼び出し側で continue すること）
    """
    path = Path(SCREENSHOT_PATH)
    _retried = 0
    for _attempt in range(retries):
        # exec-out で直接パイプ → ファイル転送の中間ステップを省略 (高速化)
        try:
            _result = subprocess.run(
                ["adb", "-s", DEVICE_SERIAL, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=10,
            )
            if _result.returncode == 0 and len(_result.stdout) >= min_bytes:
                path.write_bytes(_result.stdout)
            else:
                # exec-out 失敗 → 従来の shell + pull にフォールバック
                adb(f"shell screencap -p {REMOTE_PATH}")
                subprocess.run(
                    f"adb -s {DEVICE_SERIAL} pull {REMOTE_PATH} {SCREENSHOT_PATH}",
                    shell=True, capture_output=True, timeout=10,
                )
        except Exception as _ss_exc:
            logger.warning("[SCREENSHOT] 取得例外: %s (attempt %d/%d)", _ss_exc, _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # ── 整合性チェック1: ファイルサイズ ──
        _fsize = path.stat().st_size if path.exists() else 0
        if _fsize < min_bytes:
            logger.warning("[SCREENSHOT] 破損疑い: size=%d bytes (attempt %d/%d) — 再取得",
                           _fsize, _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # ── 整合性チェック2: OpenCV で読み込み確認 ──
        import cv2 as _cv2_chk
        _test = _cv2_chk.imread(str(path))
        if _test is None or _test.size == 0:
            logger.warning("[SCREENSHOT] cv2.imread 失敗/空 (attempt %d/%d) — 再取得",
                           _attempt + 1, retries)
            _retried += 1
            time.sleep(0.5)
            continue
        # 正常
        _h, _w = _test.shape[:2]
        return path, _w, _h, _retried
    # 全リトライ失敗: クラッシュせず None を返す (呼び出し側で continue)
    logger.error("[WIFI_ERROR] Corrupted frame dropped (%d retries exhausted). Returning None.", retries)
    return None, 0, 0, _retried


def manage_scrcpy() -> Optional[subprocess.Popen]:
    """scrcpy を規定オプションで起動。不整合プロセスは Kill → 再起動。"""
    try:
        ps = subprocess.run(
            ["ps", "aux"], capture_output=True, text=True, timeout=5
        )
    except Exception as e:
        logger.warning("[SCRCPY] ps aux 失敗: %s", e)
        return None

    conforming_pid = None
    for line in ps.stdout.splitlines():
        if "scrcpy" not in line or "grep" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        has_device = SCRCPY_DEVICE in line
        has_screen_off = "--turn-screen-off" in line
        if has_device and has_screen_off:
            conforming_pid = pid
            logger.info("[SCRCPY] 規定プロセス検出 PID=%d — 継続", pid)
        else:
            logger.info("[SCRCPY] 不整合プロセス Kill PID=%d (cmdline: ...%s)",
                        pid, line[-80:])
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    if conforming_pid is not None:
        return None  # 既に規定オプションで動作中

    # 新規起動
    try:
        proc = subprocess.Popen(
            SCRCPY_ARGS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("[SCRCPY] 規定オプションで起動 PID=%d (device=%s, --turn-screen-off)",
                    proc.pid, SCRCPY_DEVICE)
        return proc
    except FileNotFoundError:
        logger.warning("[SCRCPY] scrcpy が見つかりません — Stay Awake なしで続行 "
                       "(brew install scrcpy で導入可能)")
    except Exception as e:
        logger.warning("[SCRCPY] 起動失敗: %s — Stay Awake なしで続行", e)
    return None


def tap_device(x: int, y: int, state: PilotState, desc: str = "",
               finger_box: Optional[tuple] = None,
               gold_box: Optional[tuple] = None,
               post_wait: float = 1.0) -> None:
    if state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        real_x = int(x * sx)
        real_y = int(y * sy)
    else:
        real_x, real_y = x, y
    # ─── デバッグオーバーレイ描画 ───
    # 青枠: 指アイコン検出領域 / 緑枠: 金枠検出領域 / 赤ドット: 実際のタップ点
    try:
        import cv2 as _cv2
        if state.last_screen is not None:
            _dbg = state.last_screen.copy()
            if finger_box is not None:
                fbx, fby, fbw, fbh = finger_box
                _cv2.rectangle(_dbg, (fbx, fby), (fbx + fbw, fby + fbh),
                                (255, 0, 0), 2)  # 青枠: 指アイコン
            if gold_box is not None:
                gbx, gby, gbw, gbh = gold_box
                _cv2.rectangle(_dbg, (gbx, gby), (gbx + gbw, gby + gbh),
                                (0, 255, 0), 2)  # 緑枠: 金枠
            _cv2.circle(_dbg, (x, y), 10, (0, 0, 255), -1)  # 赤ドット: タップ点
            # 排除された偽の指ブロブを描画 ([REJECTED: SHAPE/SPATIAL])
            if _rejected_finger_blobs:
                for _rx, _ry, _rr in _rejected_finger_blobs:
                    _cv2.drawMarker(_dbg, (_rx, _ry), (0, 0, 255),
                                    _cv2.MARKER_CROSS, 22, 2)
                    _cv2.putText(_dbg, "[REJECTED]", (_rx - 42, _ry - 14),
                                 _cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 255), 1)
            _out = str(Path(__file__).parent.parent / "debug_latest_tap.png")
            _cv2.imwrite(_out, _dbg)
            logger.info("  [DEBUG_TAP] Target=(%d,%d) → %s", x, y, _out)
    except Exception:
        pass
    logger.info(
        "  [DEBUG] TAP: 解析座標=(%d,%d) → デバイス座標=(%d,%d) | %s",
        x, y, real_x, real_y, desc
    )
    adb(f"shell input tap {real_x} {real_y}")
    state.total_taps += 1
    time.sleep(post_wait)


def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
          state: "PilotState | None" = None) -> None:
    if state and state.device_w and state.device_h:
        sx = state.device_w / ANALYSIS_W
        sy = state.device_h / ANALYSIS_H
        rx1, ry1 = int(x1 * sx), int(y1 * sy)
        rx2, ry2 = int(x2 * sx), int(y2 * sy)
    else:
        rx1, ry1, rx2, ry2 = x1, y1, x2, y2
    adb(f"shell input swipe {rx1} {ry1} {rx2} {ry2} {duration_ms}")
    logger.info("  SWIPE (%d,%d)->(%d,%d) %dms", rx1, ry1, rx2, ry2, duration_ms)


def save_evidence(img_path: Path, ocr_results: list, action: str, state: PilotState) -> None:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%H%M%S")
    dest = EVIDENCE_DIR / f"{ts}_iter{state.iteration:03d}_{action}.png"
    try:
        import shutil
        shutil.copy2(str(img_path), str(dest))
        state.screenshots_saved += 1
    except Exception as e:
        logger.warning("Evidence save failed: %s", e)


def is_dark_screen(img_path: Path) -> bool:
    try:
        from PIL import Image
        import numpy as np
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
        import cv2
        import numpy as np
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
        for c in contours:
            area = cv2.contourArea(c)
            if area < min_area or area > max_area:
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
                logger.debug("[REJECTED: SHAPE] (%d,%d) solidity=%.2f<0.35", cx, cy, solidity)
                continue

            # ── 【形状検証 2】アスペクト比チェック ─────────────────────────────
            # 指アイコンは概ね 0.28〜3.5 の範囲。過度に横長な蝶の羽を排除。
            asp = bw / bh if bh > 0 else 1.0
            if asp > 3.5 or asp < 0.28:
                _rejected_finger_blobs.append((cx, cy, "SHAPE(asp=%.1f)" % asp))
                logger.debug("[REJECTED: SHAPE] (%d,%d) asp=%.1f out of [0.28,3.5]", cx, cy, asp)
                continue

            # ── 【空間的バイアス 3】バトル(dark_mode)上部30%の小面積ブロブ排除 ────
            # 蝶エネミーは上部(バトルフィールド)に出現、チュートリアル指は下部UIに出現
            if dark_mode and cy < img_h_fb * 0.30 and area < 1500:
                _rejected_finger_blobs.append((cx, cy, "SPATIAL(y=%d,area=%.0f)" % (cy, area)))
                logger.info("[REJECTED: SPATIAL] (%d,%d) 上部30%%内 area=%.0f<1500 → エネミー誤検出排除",
                            cx, cy, area)
                continue

            blobs.append((cx, cy, area, bx, by, bw, bh))
        return sorted(blobs, key=lambda b: b[2], reverse=True)
    except ImportError:
        return []
    except Exception as e:
        logger.debug("find_finger_blobs error: %s", e)
        return []


def create_finger_mask_image(img_path: Path, cx: int, cy: int, half: int = 175) -> Path:
    """
    指アイコン周囲 350×350px (half=175) 以外を純黒に塗りつぶした一時画像を生成して返す。
    Hard Masking 2.0: 右側スキルボタン等の誤検出を物理的に排除。
    失敗した場合は元の img_path を返す。
    """
    try:
        import cv2
        import numpy as _np_hm
        import tempfile
        _img_hm = cv2.imread(str(img_path))
        if _img_hm is None:
            return img_path
        _H_hm, _W_hm = _img_hm.shape[:2]
        _masked = _np_hm.zeros_like(_img_hm)
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
        import cv2
        import numpy as _np_gw
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
                              _np_gw.array([0, 0, 215], dtype=_np_gw.uint8),
                              _np_gw.array([180, 65, 255], dtype=_np_gw.uint8))
        # 金発光: 金/黄色系・高輝度 (ゴールドハイライト)
        _mask_g = cv2.inRange(_hsv_gw,
                              _np_gw.array([15, 50, 195], dtype=_np_gw.uint8),
                              _np_gw.array([50, 210, 255], dtype=_np_gw.uint8))
        _mask_gw = cv2.bitwise_or(_mask_w, _mask_g)
        # ノイズ除去: 小さいスポット・HPバー等の細線を排除
        _kern = _np_gw.ones((4, 4), _np_gw.uint8)
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


def find_gold_frame_near(img_path: Path, cx: int, cy: int,
                         search_radius: int = 150) -> Optional[tuple[int, int, int, int]]:
    """
    指アイコン中心(cx,cy)の近傍150px以内で金枠（装飾ボタン枠）を検索。
    スワイプポインター（縦長細い）は除外し、ボタン形状の金枠を返す。
    Returns: (frame_cx, frame_cy, frame_w, frame_h) or None
    """
    try:
        import cv2
        import numpy as np
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
            if not (0.3 < aspect < 4.0):
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


def detect_adv_advance_icon(img_path: Path,
                             roi_x: int = 1330, roi_y: int = 610,
                             roi_w: int = 170, roi_h: int = 90,
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
        import cv2 as _cv2d
        import numpy as _npd

        _img = _cv2d.imread(str(img_path))
        if _img is None:
            return None
        _H, _W = _img.shape[:2]

        def _match_template(tmpl_path: Path, roi_x1: int, roi_y1: int,
                            roi_x2: int, roi_y2: int) -> Optional[tuple[int, int]]:
            _tmpl = _cv2d.imread(str(tmpl_path))
            if _tmpl is None:
                return None
            _roi = _img[roi_y1:roi_y2, roi_x1:roi_x2]
            _res = _cv2d.matchTemplate(_roi, _tmpl, _cv2d.TM_CCOEFF_NORMED)
            _, _max_val, _, _max_loc = _cv2d.minMaxLoc(_res)
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


def save_tutorial_dialog_templates(img_path: Path, W: int = 1520, H: int = 720) -> None:
    """
    チュートリアルダイアログが表示されている画像から × ボタンと ▷ 矢印の
    テンプレート画像を自動保存する (各テンプレートが未存在の場合のみ)。

    呼び出し: チュートリアルポップアップが検出された最初の数回。
    """
    try:
        import cv2 as _cv2s
        _img = _cv2s.imread(str(img_path))
        if _img is None:
            return
        _H, _W = _img.shape[:2]
        _tpl_dir = _CRAWLER_ROOT / "assets" / "templates"
        _tpl_dir.mkdir(parents=True, exist_ok=True)

        # × ボタン領域: 右上隅 (x: W*0.93~W, y: 0~H*0.13)
        if not _DIALOG_CLOSE_TEMPLATE.exists():
            _x1, _y1 = int(_W * 0.93), 0
            _x2, _y2 = _W, int(_H * 0.13)
            _crop = _img[_y1:_y2, _x1:_x2]
            if _crop.size > 0:
                _cv2s.imwrite(str(_DIALOG_CLOSE_TEMPLATE), _crop)
                logger.info("[DialogNav] × テンプレート自動保存: %s", _DIALOG_CLOSE_TEMPLATE)

        # ▷ 矢印領域: 右エッジ (x: W*0.87~W*0.97, y: H*0.3~H*0.7)
        if not _DIALOG_NEXT_TEMPLATE.exists():
            _x1n, _y1n = int(_W * 0.87), int(_H * 0.3)
            _x2n, _y2n = int(_W * 0.97), int(_H * 0.7)
            _cropn = _img[_y1n:_y2n, _x1n:_x2n]
            if _cropn.size > 0:
                _cv2s.imwrite(str(_DIALOG_NEXT_TEMPLATE), _cropn)
                logger.info("[DialogNav] ▷ テンプレート自動保存: %s", _DIALOG_NEXT_TEMPLATE)

    except Exception as _e:
        logger.debug("save_tutorial_dialog_templates error: %s", _e)


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
        import cv2 as _cv
        import numpy as _np

        img = _cv.imread(str(img_path))
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
            _tpl = _cv.imread(str(_DIALOG_CLOSE_TEMPLATE))
            if (_roi_x.shape[0] < _tpl.shape[0]
                    or _roi_x.shape[1] < _tpl.shape[1]):
                return None
            _r = _cv.matchTemplate(_roi_x, _tpl, _cv.TM_CCOEFF_NORMED)
            _, _mv, _, _ml = _cv.minMaxLoc(_r)
            if _mv >= 0.70:
                _tw, _th = _tpl.shape[1], _tpl.shape[0]
                return (_rx1 + _ml[0] + _tw // 2, _ml[1] + _th // 2)
            return None

        _close_x_pos = _find_close_x(img, _H, _W)
        if _close_x_pos is not None:
            logger.debug("[Dialog×] STEP0 先行検出: (%d,%d)", _close_x_pos[0], _close_x_pos[1])
            return ("close", _close_x_pos[0], _close_x_pos[1])

        # ──────────────────────────────────────────────────────────────
        # STEP 1: HSV 金色枠で大矩形ダイアログを検出
        # ──────────────────────────────────────────────────────────────
        _hsv = _cv.cvtColor(img, _cv.COLOR_BGR2HSV)
        _mask_g = _cv.inRange(
            _hsv,
            _np.array([12, 50, 140], _np.uint8),
            _np.array([55, 255, 255], _np.uint8),
        )
        _k3 = _np.ones((3, 3), _np.uint8)
        _mask_g = _cv.dilate(_mask_g, _k3, iterations=2)
        _cnts, _ = _cv.findContours(_mask_g, _cv.RETR_EXTERNAL, _cv.CHAIN_APPROX_SIMPLE)

        _frame: Optional[tuple] = None  # (x, y, w, h)
        _best_area = 0
        _scx, _scy = _W // 2, _H // 2   # 画面中心

        for _c in _cnts:
            _a = _cv.contourArea(_c)
            if _a < 8000:
                continue
            _x, _y, _w, _h = _cv.boundingRect(_c)
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
            _g = _cv.cvtColor(roi_img, _cv.COLOR_BGR2GRAY)
            _e = _cv.Canny(_g, thr_lo, thr_hi)
            return (
                _cv.HoughLinesP(_e, 1, _np.pi / 180,
                                 threshold=8, minLineLength=min_len, maxLineGap=max_gap),
                _g,
            )

        def _cross_center(lines):
            """HoughLinesP 結果から × 形状の中心を返す"""
            if lines is None or len(lines) < 2:
                return None
            _pd, _nd = [], []
            for _ln in lines:
                _x1, _y1, _x2, _y2 = _ln[0]
                if _x2 == _x1:
                    continue
                _ang = _np.degrees(_np.arctan2(_y2 - _y1, _x2 - _x1))
                if 20 < abs(_ang) < 75:
                    (_pd if _ang > 0 else _nd).append(_ln[0])
            if _pd and _nd:
                _pts = _pd[0] + _nd[0]
                return (int(sum(_pts[::2]) / 4), int(sum(_pts[1::2]) / 4))
            return None

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
                _ang = _np.degrees(_np.arctan2(_y2 - _y1, _x2 - _x1))
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
                    _tpl = _cv.imread(str(_DIALOG_CLOSE_TEMPLATE))
                    if _froi.shape[0] >= _tpl.shape[0] and _froi.shape[1] >= _tpl.shape[1]:
                        _r_f = _cv.matchTemplate(_froi, _tpl, _cv.TM_CCOEFF_NORMED)
                        _, _mv_f, _, _ml_f = _cv.minMaxLoc(_r_f)
                        if _mv_f >= 0.70:
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
            _r = _cv.matchTemplate(
                _cv.imread(str(img_path), _cv.IMREAD_COLOR)[0: int(_H * 0.14), int(_W * 0.88):],
                _cv.imread(str(_DIALOG_CLOSE_TEMPLATE)),
                _cv.TM_CCOEFF_NORMED,
            )
            _, _mv, _, _ml = _cv.minMaxLoc(_r)
            if _mv >= 0.75:
                _tw, _th = _cv.imread(str(_DIALOG_CLOSE_TEMPLATE)).shape[1], _cv.imread(str(_DIALOG_CLOSE_TEMPLATE)).shape[0]
                return ("close",
                        int(_W * 0.88) + _ml[0] + _tw // 2,
                        _ml[1] + _th // 2)

        # Note: Phase B Canny / 輝度フォールバックは廃止 (ホーム画面バナー誤検出防止)。
        # × 検出は STEP 0 テンプレートマッチングに一元化。

        # ── ▷ ボタン (スクリーン右エッジ) ────────────────────────────────
        if _DIALOG_NEXT_TEMPLATE.exists():
            _r2 = _cv.matchTemplate(
                img[int(_H * 0.22): int(_H * 0.78), int(_W * 0.83):],
                _cv.imread(str(_DIALOG_NEXT_TEMPLATE)),
                _cv.TM_CCOEFF_NORMED,
            )
            _, _mv2, _, _ml2 = _cv.minMaxLoc(_r2)
            if _mv2 >= 0.75:
                _tw2, _th2 = _cv.imread(str(_DIALOG_NEXT_TEMPLATE)).shape[1], _cv.imread(str(_DIALOG_NEXT_TEMPLATE)).shape[0]
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
            if _cv.countNonZero(_cv.threshold(_gray_n, 140, 255, _cv.THRESH_BINARY)[1]) >= 20:
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
    import cv2 as _cv2p
    import numpy as _npp
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
            _img_c = _cv2p.imread(str(analysis_path))
            if _img_c is not None:
                _Hc, _Wc = _img_c.shape[:2]
                _close_roi_c = _img_c[0:int(_Hc * 0.14), int(_Wc * 0.88):]
                _gray_cl = _cv2p.cvtColor(_close_roi_c, _cv2p.COLOR_BGR2GRAY)
                _bright_cl = _cv2p.countNonZero(
                    _cv2p.threshold(_gray_cl, 155, 255, _cv2p.THRESH_BINARY)[1]
                )
                if _bright_cl == 0:
                    _no_close_streak += 1
                else:
                    _no_close_streak = 0
        except Exception:
            pass
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
        time.sleep(0.4)
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
    import re as _re
    # --- 1. OCR 文字数カウンター "0/N" パターン ---
    if ocr_items:
        for _item in ocr_items:
            _txt = _item.get("text", "").strip()
            if _re.match(r"^0/\d+$", _txt):
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
        import cv2 as _cv2
        import numpy as _np
        _img = _cv2.imread(str(img_path))
        if _img is None:
            return None
        _roi_y1, _roi_y2 = int(H * 0.3), int(H * 0.75)
        _roi = _img[_roi_y1:_roi_y2, :]
        _hsv = _cv2.cvtColor(_roi, _cv2.COLOR_BGR2HSV)
        # 入力フィールド特有の暗めの背景 (S低め、V中〜低)
        _dark = _cv2.inRange(_hsv, _np.array([0, 0, 20]), _np.array([180, 80, 110]))
        _cnts, _ = _cv2.findContours(_dark, _cv2.RETR_EXTERNAL, _cv2.CHAIN_APPROX_SIMPLE)
        for _cnt in sorted(_cnts, key=_cv2.contourArea, reverse=True)[:8]:
            _x, _y, _w, _h = _cv2.boundingRect(_cnt)
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
        import cv2
        import numpy as np

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

        # ── デバッグ画像保存 ──
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
        return direction, cx_bb, from_y, to_y, 3000

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
        import cv2
        import numpy as np

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

        # ── デバッグ/テンプレート保存 ──
        tut_dir = _CRAWLER_ROOT / "templates" / "tutorial"
        tut_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        vis = img.copy()
        cv2.rectangle(vis, (x_b, y_b), (x_b + w_b, y_b + h_b), (255, 0, 0), 3)
        cv2.circle(vis, (tap_x, tap_y), 12, (0, 255, 255), -1)
        cv2.putText(vis, f"GoldBtn area={int(area_b)} asp={h_b/max(w_b,1):.1f}",
                    (x_b, max(0, y_b - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 0, 0), 2)
        cv2.imwrite(str(tut_dir / f"gold_btn_{ts}.png"), vis)

        # テンプレート画像 (ボタン部分のROI) も保存 (後日 AssetManager で使えるように)
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
) -> tuple[int, int]:
    """OCR テキスト座標周辺から金色ボタン枠を検出し、幾何学的中心を返す。

    検出失敗時は OCR 座標に _BUTTON_Y_OFFSET を加算してフォールバック。
    返値: (tap_x, tap_y)
    """
    try:
        import cv2
        import numpy as np

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

        # 金色ボタン枠の HSV レンジ (実測: RGB≈(190,165,122) → H≈30,S≈80,V≈190)
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
            if area < 2000:  # ボタン最小面積: 2000px² (影・境界線を除外)
                continue
            rx, ry, rw, rh = cv2.boundingRect(cnt)
            # ボタンらしい形状: 横長かつ適切なサイズ (アスペクト比 2〜15)
            if rw < 80 or rh < 20:
                continue
            aspect = rw / max(rh, 1)
            if aspect < 2.0 or aspect > 15.0:
                continue  # 細すぎ(影)・正方形すぎ(アイコン)を除外
            if area > best_area:
                best_area = area
                best_rect = (rx + x1, ry + y1, rw, rh)

        if best_rect:
            bx, by, bw, bh = best_rect
            cx = bx + bw // 2
            cy = by + bh // 2
            logger.info(
                "  [SmartTap] OCR中心=(%d,%d) → 金色ボタン検出 rect=(%d,%d,%d,%d) → タップ座標=(%d,%d)",
                ocr_cx, ocr_cy, bx, by, bw, bh, cx, cy
            )
            return cx, cy

    except Exception as e:
        logger.debug("  [SmartTap] エラー: %s", e)

    # フォールバック: OCR 座標をそのまま使用（数学的中心点）
    logger.info(
        "  [SmartTap] HSV検出失敗 → フォールバック OCR中心=(%d,%d) をそのままタップ",
        ocr_cx, ocr_cy
    )
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
        import cv2
        import numpy as np

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
def has_any(ocr: list, keywords: list[str], min_conf: float = 0.3) -> Optional[dict]:
    for kw in keywords:
        match = find_best(ocr, kw, min_confidence=min_conf)
        if match:
            return match
    return None


def has_text(ocr: list, keyword: str, min_conf: float = 0.3) -> Optional[dict]:
    return find_best(ocr, keyword, min_confidence=min_conf)


def all_texts(ocr: list) -> list[str]:
    return [r["text"] for r in ocr]


# ─── 探索マップ 3D矢印 検出 ──────────────────────────
def find_3d_arrow(img_path: Path) -> Optional[tuple[int, int]]:
    """
    探索マップ上のキャラ頭上に浮かぶ3D矢印（白い曲線矢印）を検出。
    明るい白色コンターが最大のものを矢印とみなす。
    Returns: (cx, cy) or None
    """
    try:
        import cv2
        import numpy as np
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        # キャラ頭上エリア (y=120-280, x=500-1050)
        roi_y1, roi_y2 = 120, 280
        roi_x1, roi_x2 = 500, 1050
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
        import cv2, json
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
              ocr_texts: Optional[list[str]] = None) -> Optional[tuple[int, int, str]]:
        """
        スクリーンショットと全テンプレートを比較。
        ocr_texts が渡された場合、require_ocr 条件を満たすテンプレートのみ照合。
        Returns: (tap_x, tap_y, action_name) or None
        """
        import cv2
        if not self._templates:
            return None
        img = cv2.imread(str(screenshot_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        best_score = 0.0
        best_result: Optional[tuple[int, int, str]] = None
        for name, data in self._templates.items():
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
                    cx = max_loc[0] + w // 2 + int(data["offset"][0])
                    cy = max_loc[1] + h // 2 + int(data["offset"][1])
                    best_result = (cx, cy, data["action"])
                    logger.debug("[Asset] '%s' score=%.3f at (%d,%d)", name, max_val, cx, cy)
            except Exception as e:
                logger.debug("[Asset] match error '%s': %s", name, e)
        if best_result:
            cx, cy, action = best_result
            logger.info("[Asset] HIT: '%s' score=%.3f → (%d,%d)", action, best_score, cx, cy)
        return best_result

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
        import cv2, json
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

    def reload(self) -> None:
        self._templates.clear()
        self._load_templates()


# グローバル AssetManager インスタンス (起動時に1回ロード)
ASSET_MANAGER = AssetManager()


# ─── 戦略的意思決定エンジン (StrategicDecisionEngine) ──────────
class StrategicDecisionEngine:
    """
    UIアフォーダンス解析 + 行動予測 + 経験学習エンジン。

    1. find_buttons()     : 視覚的特徴（色・形）からタップ可能領域を抽出
    2. predict_outcome()  : OCRテキストの意味から結果を予測
    3. verify_and_learn() : タップ結果を検証し knowledge_base.json に蓄積
    """

    KNOWLEDGE_PATH = _CRAWLER_ROOT / "storage" / "knowledge_base.json"

    # テキストキーワード → (action_type, 予測説明)
    PREDICTION_MAP: dict[str, tuple[str, str]] = {
        # ガチャ・召喚
        "ガシャ":    ("GACHA_DRAW",     "召喚演出・アイテム獲得シーンが発生する"),
        "ガチャ":    ("GACHA_DRAW",     "召喚演出・アイテム獲得シーンが発生する"),
        "召喚":      ("GACHA_DRAW",     "召喚演出が発生する"),
        "受け取る":  ("RECEIVE_ITEM",   "アイテム受け取り処理が実行される"),
        "獲得":      ("RECEIVE_ITEM",   "アイテム獲得処理が実行される"),
        # 進行・スキップ
        "次へ":      ("SCENE_ADVANCE",  "シーンが遷移してストーリーが進む"),
        "スキップ":  ("SKIP_STORY",     "ストーリーシーンがスキップされる"),
        "SKIP":      ("SKIP_STORY",     "ストーリーシーンがスキップされる"),
        "進む":      ("SCENE_ADVANCE",  "シーンが遷移する"),
        "TAP TO":    ("SCENE_ADVANCE",  "シーンが進む"),
        "START":     ("GAME_START",     "ゲームまたはバトルが開始する"),
        "開始":      ("BATTLE_START",   "バトルまたはクエストが開始する"),
        "出撃":      ("BATTLE_START",   "クエストが開始しバトル画面へ遷移する"),
        "戦闘":      ("BATTLE_START",   "クエストが開始しバトル画面へ遷移する"),
        # バトル
        "AUTO":      ("AUTO_BATTLE",    "バトルがAUTOモードで自動進行する"),
        "攻撃":      ("BATTLE_ATTACK",  "戦闘ターンが進行する"),
        "通常攻撃":  ("NORMAL_ATTACK",  "通常攻撃が実行される"),
        "必殺技":    ("SPECIAL_ATTACK", "必殺技演出が発生し大ダメージが入る"),
        "スキル":    ("SKILL_USE",      "スキルが発動する"),
        # 閉じる・確認
        "OK":        ("CONFIRM",        "確認ダイアログが閉じてメニューに戻る"),
        "閉じる":    ("CLOSE_DIALOG",   "ダイアログが閉じる"),
        "確認":      ("CONFIRM",        "確認処理が実行される"),
        "完了":      ("COMPLETE",       "処理が完了してメニューに戻る"),
        "決定":      ("CONFIRM",        "選択が確定される"),
        "了解":      ("CONFIRM",        "確認ダイアログが閉じる"),
        "わかった":  ("CONFIRM",        "確認ダイアログが閉じる"),
        "リザルト":  ("RESULT",         "バトル結果画面が表示される"),
        "Result":    ("RESULT",         "バトル結果画面が表示される"),
        # ナビゲーション
        "ホーム":    ("GO_HOME",        "ホーム画面に戻る"),
        "メニュー":  ("OPEN_MENU",      "メニューが開く"),
        "クエスト":  ("OPEN_QUEST",     "クエスト選択画面へ遷移する"),
        "ショップ":  ("OPEN_SHOP",      "ショップ画面へ遷移する"),
        "編成":      ("OPEN_FORMATION", "パーティ編成画面へ遷移する"),
    }

    # ゲームUIの色彩意味論: 色 → タップ優先度
    COLOR_PRIORITY: dict[str, int] = {
        "orange": 10,   # 橙: 攻撃・決定（最優先）
        "red":     9,   # 赤: 攻撃・警告
        "blue":    7,   # 青: 回復・進む
        "green":   6,   # 緑: 回復・安全
        "purple":  5,   # 紫: 魔法・特殊
        "yellow":  4,   # 黄: 注意・ハイライト
        "gray":    2,   # 灰: キャンセル・戻る
        "white":   1,   # 白: 中立
        "unknown": 0,
    }

    def __init__(self):
        self._knowledge: dict = self._load_knowledge()

    def _load_knowledge(self) -> dict:
        if self.KNOWLEDGE_PATH.exists():
            try:
                import json
                return json.loads(self.KNOWLEDGE_PATH.read_text())
            except Exception:
                pass
        return {"patterns": {}, "stats": {"total_taps": 0, "verified": 0}}

    def _save_knowledge(self) -> None:
        import json
        self.KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.KNOWLEDGE_PATH.write_text(
            json.dumps(self._knowledge, ensure_ascii=False, indent=2)
        )

    def _classify_color(self, roi_bgr) -> str:
        """BGR ROI の主要色をゲームUI色彩設計に基づいて分類。"""
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        s = float(np.mean(hsv[:, :, 1]))
        v = float(np.mean(hsv[:, :, 2]))
        h = float(np.mean(hsv[:, :, 0]))
        if s < 40:
            return "white" if v > 180 else "gray"
        # OpenCV HSV: H は 0-180
        if h < 10 or h > 155:
            return "red"
        if h < 25:
            return "orange"
        if h < 35:
            return "yellow"
        if h < 85:
            return "green"
        if h < 125:
            return "blue"
        return "purple"

    def find_buttons(self, img_path: Path) -> list[dict]:
        """
        エッジ検出 + 輪郭抽出でボタン候補領域を検出。
        矩形・丸みを帯びた角・高コントラスト縁を持つ領域を「タップ可能」と判定。
        Returns: [{"cx","cy","w","h","color","priority","area"}, ...] 優先度降順
        """
        try:
            import cv2
            import numpy as np
            img = cv2.imread(str(img_path))
            if img is None:
                return []
            H, W = img.shape[:2]
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 40, 120)
            dilated = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)
            contours, _ = cv2.findContours(
                dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            buttons = []
            for c in contours:
                area = cv2.contourArea(c)
                # ボタンサイズフィルタ: 1000px² ≤ area ≤ 25% 画面
                if area < 1000 or area > W * H * 0.25:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                asp = w / h if h > 0 else 0
                # ボタンのアスペクト比: 0.5 〜 12
                if asp < 0.5 or asp > 12 or w < 40 or h < 20:
                    continue
                color = self._classify_color(img[y:y + h, x:x + w])
                priority = self.COLOR_PRIORITY.get(color, 0)
                buttons.append({
                    "x": x, "y": y, "w": w, "h": h,
                    "cx": x + w // 2, "cy": y + h // 2,
                    "area": int(area), "color": color, "priority": priority,
                })
            buttons.sort(key=lambda b: (b["priority"], b["area"]), reverse=True)
            return buttons[:20]
        except Exception as e:
            logger.debug("[SDE] find_buttons error: %s", e)
            return []

    def predict_outcome(self, text: str) -> tuple[str, str]:
        """
        OCRテキストからタップ後の結果を予測。
        長いキーワードを優先（"通常攻撃" > "攻撃" など）。
        Returns: (action_type, description)
        """
        # キーワード長の降順でマッチング（長い=具体的なキーワードを優先）
        for kw, (action_type, desc) in sorted(
            self.PREDICTION_MAP.items(), key=lambda x: len(x[0]), reverse=True
        ):
            if kw in text:
                return action_type, desc
        return "UNKNOWN", "未知の操作が実行される"

    def log_prediction(self, text: str, cx: int, cy: int) -> tuple[str, str]:
        """予測を生成してログ出力。Returns: (action_type, description)"""
        action_type, desc = self.predict_outcome(text)
        if action_type != "UNKNOWN":
            logger.info(
                "[PREDICTION] Tapping '%s' at (%d,%d) -> Expecting %s: %s",
                text[:20], cx, cy, action_type, desc,
            )
        return action_type, desc

    def verify_and_learn(self, pre_phash: str, post_phash: str,
                         action_type: str, desc: str, tap_text: str) -> None:
        """
        タップ後のphash変化から予測の正否を検証し、knowledge_base.jsonに記録。
        - phash距離 >= PHASH_THRESHOLD → 画面変化あり = SUCCESS
        - phash距離 < PHASH_THRESHOLD  → 画面変化なし = NO_CHANGE
        """
        if not pre_phash or not post_phash or action_type == "UNKNOWN":
            return
        try:
            dist = phash_distance(pre_phash, post_phash)
            scene_changed = dist >= PHASH_THRESHOLD
            key = f"{action_type}:{tap_text[:20]}"
            stats = self._knowledge["stats"]
            stats["total_taps"] = stats.get("total_taps", 0) + 1
            stats["verified"] = stats.get("verified", 0) + 1
            pat = self._knowledge["patterns"].setdefault(key, {
                "prediction": action_type, "description": desc,
                "text": tap_text, "success_count": 0, "failure_count": 0,
                "last_seen": "",
            })
            if scene_changed:
                pat["success_count"] += 1
                logger.info("[LEARNING] '%s'→%s ✓ dist=%d (ok=%d)",
                            tap_text[:15], action_type, dist, pat["success_count"])
            else:
                pat["failure_count"] += 1
                logger.info("[LEARNING] '%s'→%s ✗ dist=%d (fail=%d)",
                            tap_text[:15], action_type, dist, pat["failure_count"])
            pat["last_seen"] = datetime.now().isoformat()
            # 10タップごとに保存
            if stats["total_taps"] % 10 == 0:
                self._save_knowledge()
        except Exception as e:
            logger.debug("[SDE] verify_and_learn error: %s", e)

    def report_screen_affordances(self, img_path: Path, ocr_results: list) -> None:
        """
        現在画面のUIアフォーダンス解析レポートをログ出力。
        ボタン候補領域を検出し、各領域内のOCRテキストから行動を予測する。
        """
        buttons = self.find_buttons(img_path)
        if not buttons:
            return
        logger.info("[SDE] === UIアフォーダンス解析: %d個のボタン候補 ===", len(buttons))
        for i, btn in enumerate(buttons[:5]):
            # ボタン領域内のOCRテキストを抽出
            btn_texts = [
                r["text"] for r in ocr_results
                if (btn["x"] <= r["center"][0] <= btn["x"] + btn["w"] and
                    btn["y"] <= r["center"][1] <= btn["y"] + btn["h"])
            ]
            text_str = " ".join(btn_texts) if btn_texts else "(no text)"
            action_type, _ = self.predict_outcome(text_str)
            logger.info(
                "[SDE] #%d (%d,%d) %dx%d color=%s prio=%d '%s' → %s",
                i + 1, btn["cx"], btn["cy"], btn["w"], btn["h"],
                btn["color"], btn["priority"], text_str[:20], action_type,
            )

    # ─── 要素キーワード → (英語名, 検出方法) ───
    _ELEMENT_MAP: dict[str, tuple[str, str]] = {
        "矢印":     ("arrow",   "arrow"),
        "矩形":     ("rect",    "button"),
        "ボタン":   ("btn",     "button"),
        "アイコン": ("icon",    "button"),
        "スキップ": ("skip",    "ocr:スキップ"),
        "次へ":     ("next",    "ocr:次へ"),
        "OK":       ("ok",      "ocr:OK"),
        "閉じる":   ("close",   "ocr:閉じる"),
        "ホーム":   ("home",    "ocr:ホーム"),
        "ガチャ":   ("gacha",   "ocr:ガチャ"),
        "ガシャ":   ("gacha",   "ocr:ガシャ"),
        "戦闘":     ("battle",  "ocr:戦闘"),
        "出撃":     ("deploy",  "ocr:出撃"),
        "クエスト": ("quest",   "ocr:クエスト"),
    }

    # ─── 役割キーワード → プレフィックス ───
    _ROLE_MAP: dict[str, str] = {
        "ボタン": "btn",
        "アイコン": "icon",
        "タブ": "tab",
        "メニュー": "menu",
        "リスト": "list",
    }

    def learn_from_instruction(
        self,
        instruction: str,
        screenshot_path: Path,
        ocr_results: list,
        asset_manager: "AssetManager",
    ) -> Optional[str]:
        """
        ユーザーの曖昧な指示から UI 要素を自律的に抽出・命名・保存する。

        例:
            "矢印はボタン"   → 矢印を検出 → "btn_arrow" として保存
            "スキップはボタン"→ OCRでスキップ検出 → "btn_skip" として保存

        Returns: 保存したテンプレート名 or None
        """
        # 役割パース
        role = "btn"
        for kw, r in self._ROLE_MAP.items():
            if kw in instruction:
                role = r
                break

        # 要素パース
        element = "unknown"
        find_method = "button"
        for kw, (en_name, method) in self._ELEMENT_MAP.items():
            if kw in instruction:
                element = en_name
                find_method = method
                break

        name = f"{role}_{element}"
        W, H = ANALYSIS_W, ANALYSIS_H
        x1 = y1 = x2 = y2 = 0
        cx: Optional[int] = None

        if find_method == "arrow":
            pos = find_3d_arrow(screenshot_path)
            if pos:
                cx, cy_val = pos
                half_w, half_h = 80, 60
                x1 = max(0, cx - half_w)
                y1 = max(0, cy_val - half_h)
                x2 = min(W, cx + half_w)
                y2 = min(H, cy_val + half_h)

        elif find_method.startswith("ocr:"):
            ocr_kw = find_method[4:]
            match = find_best(ocr_results, ocr_kw)
            if match:
                cx, _ = match["center"]
                box = match["box"]
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                pad = 10
                x1 = max(0, min(xs) - pad)
                y1 = max(0, min(ys) - pad)
                x2 = min(W, max(xs) + pad)
                y2 = min(H, max(ys) + pad)

        else:
            # ボタン検出: find_buttons から最優先候補を使用
            buttons = self.find_buttons(screenshot_path)
            if buttons:
                btn = buttons[0]
                cx = btn["cx"]
                x1, y1 = btn["x"], btn["y"]
                x2, y2 = btn["x"] + btn["w"], btn["y"] + btn["h"]

        if cx is None:
            logger.warning("[SemanticAsset] '%s' から要素を検出できませんでした", instruction)
            return None

        saved = asset_manager.save_template(
            screenshot_path, x1, y1, x2, y2,
            name=name,
            action=f"SEMANTIC_{name.upper()}",
            threshold=0.75,
        )
        if saved:
            logger.info(
                "[SemanticAsset] '%s' → '%s' 登録完了 (%d,%d)-(%d,%d)",
                instruction, name, x1, y1, x2, y2,
            )
            return name
        return None


# グローバル StrategicDecisionEngine インスタンス
STRATEGIC_ENGINE = StrategicDecisionEngine()


# ─── 画面判定・アクション ──────────────────────────
def detect_and_act(ocr: list, state: PilotState,
                   analysis_path: Optional[Path] = None) -> tuple[str, float]:
    """
    OCR + 指差しブロブを分析し、アクションを決定する。
    analysis_path が渡された場合は finger blob 検出も実行。

    Returns: (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    W, H = ANALYSIS_W, ANALYSIS_H
    joined = " ".join(texts)

    # ─── 【最優先 #-2】タイトル画面 設定/サポートメニュー ───
    # 「動画配信設定」アイコンを誤タップして開く設定ポップアップ → BACK で閉じる
    # ただし、ストーリー/バトル/マップシーン中は「サポート」がセリフに含まれるため除外
    _settings_menu_kws = ["サポート", "データ引き継ぎ", "キャッシュクリア", "お問い合わせ"]
    _story_context_kws = ["1-1", "1-2", "第1幕", "第1階層", "第2幕", "WAVE", "AUTO", "1-3", "2-1"]
    _in_story_ctx = any(kw in joined for kw in _story_context_kws)
    # 設定メニューはストーリーコンテキスト外かつ2つ以上のキーワードが揃った時のみ判定
    _settings_hits = sum(1 for kw in _settings_menu_kws
                         if has_text(ocr, kw, min_conf=0.3) is not None)
    if not _in_story_ctx and _settings_hits >= 1:
        logger.info(">>> 【設定メニュー誤起動】 BACK キーで閉じる")
        import subprocess as _sp
        _sp.run(["adb", "-s", DEVICE_SERIAL, "shell", "input", "keyevent", "4"], check=False)
        return "SETTINGS_BACK", 1.5

    # ─── 【最優先 #-1】「ご注意」画面 (Google Play 起動時 portrait 注意書き) ───
    # アプリ初回起動時に portrait で表示される法的注意画面。
    # 「同意してゲームを始める」ボタン (右側ゴールドボタン) をOCRで検出してタップ。
    if has_text(ocr, "ご注意", min_conf=0.3) or (
        has_text(ocr, "基本無料", min_conf=0.3) and has_text(ocr, "未成年", min_conf=0.3)
    ):
        # 「同意」ボタンをOCRで検出
        # 注意: OCR center (1023,585) ≠ 実際タップ有効点 (1000,570)
        # ゲームのUIはOCRテキスト中心より少し左上がボタンのヒットゾーン (実測 2026-03-06)
        agree_btn = (has_text(ocr, "同意してゲーム", min_conf=0.2) or
                     has_text(ocr, "同意して", min_conf=0.2) or
                     has_text(ocr, "ゲームを始める", min_conf=0.2))
        if agree_btn:
            cx, cy = agree_btn["center"]
            logger.info(">>> 【ご注意画面】 同意ボタン検出 OCR(%d,%d) タップ", cx, cy)
        else:
            # フォールバック: 比率ベース (W*0.66, H*0.79) + ROI 補正
            cx, cy = roi_to_device(int(W * 0.66), int(H * 0.79), state.game_roi)
            logger.info(">>> 【ご注意画面】 同意ボタン未検出 → ROI補正フォールバック (%d,%d)", cx, cy)

        # ─── phash監視付き動的リトライ (固定120秒スリープを廃止) ───
        # 仕様: タップ → 2s待機 → phash変化確認 → 変化なし → x+20pxずらして最大5回リトライ
        # 変化検知 → Unity初期化待機(60s)へ即移行
        _base_ph = compute_phash(analysis_path) if analysis_path else ""
        _agree_changed = False
        for _retry_i in range(5):
            _tap_x = cx + _retry_i * 20  # x方向に +20px ずつ調整
            _tap_y = cy
            tap_device(_tap_x, _tap_y, state,
                       f"GO_CHUI_AGREE_R{_retry_i}({'OCR' if agree_btn else 'FB'})")
            logger.info(">>> 【ご注意→phash監視】 #%d タップ(%d,%d) → 1s待機",
                        _retry_i + 1, _tap_x, _tap_y)
            time.sleep(1.0)
            _new_ss, _, _, _ = take_screenshot()
            _new_ph = compute_phash(_new_ss)
            if _base_ph and _new_ph:
                _dist = phash_distance(_base_ph, _new_ph)
                if _dist >= PHASH_THRESHOLD:
                    logger.info(
                        ">>> 【ご注意→変化検知!】 #%d tap(%d,%d) phash_dist=%d → Unity初期化待機へ",
                        _retry_i + 1, _tap_x, _tap_y, _dist
                    )
                    _agree_changed = True
                    break
                logger.info(">>> 【ご注意→変化なし】 #%d phash_dist=%d → 座標+20pxで再試行",
                            _retry_i + 1, _dist)
                _base_ph = _new_ph  # 次回比較の基準を更新
            else:
                logger.info(">>> 【ご注意→phash計算失敗】 #%d → 次座標で再試行", _retry_i + 1)

        if _agree_changed:
            logger.info(">>> 【Unity初期化待機】 60秒 Watchdog停止 (NOTICE_DISMISS exempt)")
            return "NOTICE_DISMISS", 60.0
        else:
            logger.info(">>> 【ご注意→リトライ上限(5回)】 次ループで再検出")
            return "NOTICE_DISMISS", 3.0

    # ─── 【最優先 #-1b】MAIN STORY ローディング背景 ───
    # タイトル画面TAP後に表示される非インタラクティブなローディング背景。
    # 「Main」カードと「推奨」テキストが同時に存在する場合は待機のみ（タップ不要）。
    # この画面は自動でホーム画面へ遷移する。
    _is_main_story_bg = (
        any("MAIN" in t or "Main" in t for t in texts) and
        any("推奨" in t or "STORY" in t for t in texts) and
        not any(kw in joined for kw in ["クエスト", "ショップ", "ガチャ", "ガシャ", "光の間", "パーティ"])
    )
    if _is_main_story_bg:
        logger.info(">>> MAIN STORY ローディング背景 — 自動遷移待ち (10s)")
        return "MAIN_STORY_LOADING", 10.0

    # ─── 【最優先 #0-PRE】バトル発光SM ガード ─────────────────────────────────
    # DIALOG_CLOSE が「通常攻撃」等のバトルアクションを踏み越えるのを防ぐ。
    # ① 「メニューが使用できません」トースト → DIALOG 誤検出スキップ (toast 自然消滅)
    # ② P1: 左キャラ発光 (character_selected=False) → GLOW_LEFT_CHAR
    # ③ P2: 右スキル発光 (character_selected=True) → GLOW_RIGHT_SKILL
    # ④ P3: 発光なし + character_selected → 通常攻撃 OCR フォールバック
    _is_battle_early = any(kw in " ".join(texts) for kw in
                           ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "BREAK", "WAVE", "Turn"])
    _battle_menu_toast = "メニューが使用できません" in " ".join(texts)
    if _is_battle_early and _battle_menu_toast:
        # メニューボタン誤タップ → トースト表示中。DIALOG_CLOSE を完全スキップして2秒待機
        logger.info("[#0-PRE] 「メニューが使用できません」トースト検出 → DIALOG_CLOSE スキップ (2s wait)")
        return "BATTLE_MENU_TOAST_WAIT", 2.0
    if _is_battle_early and analysis_path is not None:
        _pre_glows = detect_guide_glow(analysis_path, W, H, footer_ratio=0.30)
        _pre_left = [g for g in _pre_glows if g["side"] == "left"]
        _pre_right = [g for g in _pre_glows if g["side"] == "right"]
        # P1: 左キャラ発光 (キャラ未選択) → DIALOG_CLOSE より前にタップ
        if not state.character_selected and _pre_left:
            _pl = max(_pre_left, key=lambda g: g["area"])
            _pl_x, _pl_y = _pl["cx"], max(1, _pl["cy"] - 35)
            logger.info("[GLOW_SM P1] 左キャラ発光(%d,%d)→tap(%d,%d) [#0前ガード]",
                        _pl["cx"], _pl["cy"], _pl_x, _pl_y)
            tap_device(_pl_x, _pl_y, state, "GLOW_LEFT_CHAR", post_wait=0.3)
            tap_device(_pl_x, _pl_y, state, "GLOW_LEFT_CHAR")  # ダブルタップ(追いタップ)
            state.character_selected = True
            state.char_just_selected = True
            state.finger_detections += 1
            return "GLOW_LEFT_CHAR", 0.3
        # P2: 右スキル発光 (キャラ選択済み) → DIALOG_CLOSE より前にタップ
        if state.character_selected and _pre_right:
            _prg = max(_pre_right, key=lambda g: g["area"])
            _prg_x, _prg_y = _prg["cx"], max(1, _prg["cy"] - 35)
            logger.info("[GLOW_SM P2] 右発光(%d,%d)→tap(%d,%d) [#0前ガード]",
                        _prg["cx"], _prg["cy"], _prg_x, _prg_y)
            tap_device(_prg_x, _prg_y, state, "GLOW_RIGHT_SKILL")
            state.character_selected = False
            state.char_just_selected = False
            state.finger_detections += 1
            return "GLOW_RIGHT_SKILL", 0.3
        # P3: キャラ選択済み + 発光なし → 通常攻撃/单体攻撃をOCRで直接タップ
        if state.character_selected and not _pre_right:
            _pre_na = has_any(ocr, ["通常攻撃", "单体攻撃", "単体攻撃"])
            if _pre_na:
                _pnx, _pny = _pre_na["center"]
                if _pnx > W * 0.5 and _pny > H * 0.5:
                    _pny = max(1, _pny - 35)
                    logger.info("[GLOW_SM P3] 攻撃ボタンOCR '%s'(%d,%d) → tap [#0前ガード]",
                                _pre_na["text"], _pnx, _pny)
                    tap_device(_pnx, _pny, state, "NORMATK_TAP")
                    state.character_selected = False
                    state.char_just_selected = False
                    return "NORMATK_TAP", 1.0

    # ─── 【最優先 #0-DIALOG】ダイアログ・ファースト (枠形状+Canny) ────────────
    # 主トリガー: HSV金色枠の大矩形検出 (形状ベース)
    # 副トリガー: OCR キーワード補助 (枠検出失敗時フォールバック)
    # ダイアログ検出中は指アイコン・金枠探索を完全スキップ (即 return)
    if analysis_path is not None:
        _dlg = detect_dialog_frame_and_nav(
            analysis_path, W, H, ocr_texts=texts, roi=state.game_roi
        )
        if _dlg is not None:
            _dlg_type, _dlg_x, _dlg_y = _dlg
            # ── [SPATIAL GATE] ▷ページングより指アイコンを最優先 ──────────────
            # 指アイコンが存在し、かつ検出した▷が指から300px以上離れている場合、
            # 右パネル等の誤検出▷を無視して指アイコン処理(#1)へフォールスルー
            if _dlg_type in ("next", "bottom"):
                _sg_blobs = find_finger_blobs(analysis_path, min_area=400)
                _sg_blobs = [b for b in _sg_blobs if b[1] > 36 and b[0] < W - 40]
                if _sg_blobs:
                    _sg_best = max(_sg_blobs, key=lambda b: b[2])
                    _sg_dist = ((_dlg_x - _sg_best[0]) ** 2 + (_dlg_y - _sg_best[1]) ** 2) ** 0.5
                    if _sg_dist > 300:
                        logger.info(
                            ">>> [SPATIAL_GATE] 指(%d,%d)↔▷(%d,%d) 距離=%.0fpx>300 → #0-DIALOG スキップ",
                            _sg_best[0], _sg_best[1], _dlg_x, _dlg_y, _sg_dist,
                        )
                        _dlg = None  # ダイアログをなかったことにして #1 へ
            # ── バトル中 × 誤検出ガード ──────────────────────────────────────────
            # バトル画面では y < 100 は UI ボタン帯 (倍速/メニュー等)。
            # DIALOG_CLOSE としてタップすると「メニューが使用できません」トーストが出るため除外。
            if (_dlg is not None and _dlg_type == "close"
                    and _is_battle_early and _dlg_y < 100):
                logger.info(
                    "[BATTLE_DIALOG_GUARD] close(%d,%d) y<100 → バトル上部UI誤検出 スキップ",
                    _dlg_x, _dlg_y,
                )
                _dlg = None  # フッター発光SM (#1-pre) へフォールスルー

            if _dlg is not None:
                state.pre_popup_tap_count += 1
                state.dialog_close_total += 1
                state.dialog_detections += 1

                # ── エスカレーション: 12回以上 → ダイアログ検出自体をスキップ ──
                if state.dialog_close_total >= 12:
                    logger.warning(
                        ">>> 【ダイアログ#0-DIALOG】累計%d回スタック → ダイアログ検出スキップ (他処理へ)",
                        state.dialog_close_total,
                    )
                    state.dialog_close_total = 0
                    state.pre_popup_tap_count = 0
                    _dlg = None  # フォールスルーで他の処理に任せる
                # ── エスカレーション: 8回以上 → Android BACK キー ──
                elif state.dialog_close_total >= 8:
                    logger.warning(
                        ">>> 【ダイアログ#0-DIALOG】累計%d回失敗 → BACK キー押下",
                        state.dialog_close_total,
                    )
                    try:
                        import subprocess as _sp_bk
                        _sp_bk.run(
                            ["adb", "-s", DEVICE_SERIAL, "shell", "input", "keyevent", "KEYCODE_BACK"],
                            timeout=5, capture_output=True,
                        )
                    except Exception:
                        pass
                    state.pre_popup_tap_count = 0
                    return "DIALOG_BACK_ESCALATION", 2.0

            if _dlg is not None:
                if _dlg_type in ("next", "bottom"):
                    # ページング式ダイアログ: ▷ → … → × を一括処理
                    logger.info(
                        ">>> 【ダイアログ#0-DIALOG-PAGING】%s(%d,%d) (試行%d回) → process_paging_dialog",
                        _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count,
                    )
                    _pg_result = process_paging_dialog(
                        analysis_path, W, H, state,
                        initial_dlg=(_dlg_type, _dlg_x, _dlg_y),
                        ocr_texts=texts,
                    )
                    return _pg_result, 1.0   # ← 必ず return。fallthrough なし。
                else:
                    # "close": × ボタンを即タップ
                    # ── 4回連続失敗 → OK/確認ボタンを探してフォールバック ──
                    if state.pre_popup_tap_count >= 4:
                        _ok_ocr = has_any(ocr, ["OK", "確認", "決定", "おまかせ"])
                        if _ok_ocr:
                            _ok_cx, _ok_cy = _ok_ocr["center"]
                            logger.info(
                                ">>> 【ダイアログ#0-DIALOG】close失敗%d回 → OKフォールバック '%s'(%d,%d)",
                                state.pre_popup_tap_count, _ok_ocr["text"], _ok_cx, _ok_cy,
                            )
                            tap_device(_ok_cx, _ok_cy, state, "DIALOG_OK_FALLBACK")
                            state.pre_popup_tap_count = 0
                            return "DIALOG_OK_FALLBACK", 1.5
                        # OCR で OK 未検出 → ダイアログ下部中央をタップ
                        _ok_fb_x, _ok_fb_y = roi_to_device(int(W * 0.7), int(H * 0.92), state.game_roi)
                        logger.info(
                            ">>> 【ダイアログ#0-DIALOG】close失敗%d回 → 下部中央フォールバック(%d,%d)",
                            state.pre_popup_tap_count, _ok_fb_x, _ok_fb_y,
                        )
                        tap_device(_ok_fb_x, _ok_fb_y, state, "DIALOG_BOTTOM_FALLBACK")
                        state.pre_popup_tap_count = 0
                        return "DIALOG_BOTTOM_FALLBACK", 1.5
                    logger.info(
                        ">>> 【ダイアログ#0-DIALOG】%s(%d,%d) (試行%d回/累計%d)",
                        _dlg_type, _dlg_x, _dlg_y, state.pre_popup_tap_count, state.dialog_close_total,
                    )
                    tap_device(_dlg_x, _dlg_y, state, "DIALOG_CLOSE")
                    return "DIALOG_CLOSE", 1.0   # ← 必ず return。fallthrough なし。

    # ─── 【最優先 #0-aa】HSV金色ポインター検出 → ホールドスワイプ (Type A) ───
    # 縦長金色領域 h/w>=3.5 かつ幅<=100px のみ有効 (ボタン/カード誤検出防止)。
    # チュートリアル3D移動シーン(チェッカー床/階段/廊下)で発火。
    # phash監視: スワイプ後2s待機 → 変化なければ再実行 (最大2回)
    # バトルUI（通常攻撃・単体攻撃・WAVE・Turn）が見えるとき はバトル中なのでスキップ
    _battle_ui_kws = ["通常攻撃", "単体攻撃", "WAVE", "Turn", "ターン"]
    _is_battle_ui = any(kw in joined for kw in _battle_ui_kws)
    if analysis_path is not None and not _is_battle_ui:
        _gold = detect_tutorial_gold_swipe(analysis_path)
        if _gold:
            # 連続スワイプ上限チェック: 6回超えたら GoldSwipe をスキップして他の処理へ
            if state.gold_swipe_count > 6:
                logger.warning(
                    "[GoldSwipe] detect_and_act: 連続 %d 回 → スキップ (別アクション探索)",
                    state.gold_swipe_count,
                )
                state.gold_swipe_count = 0
            else:
                _dir, _sx, _fy, _ty, _dur = _gold
                state.gold_swipe_count += 1
                _base_ph_gs = compute_phash(analysis_path)
                for _gs_retry in range(2):
                    if _dir == "UP":
                        logger.info(">>> [GoldSwipe] SWIPE_UP (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
                    else:
                        logger.info(">>> [GoldSwipe] SWIPE_DOWN (%d,%d)→(%d,%d) %dms (試行%d)",
                                    _sx, _fy, _sx, _ty, _dur, _gs_retry + 1)
                        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
                    time.sleep(1.0)
                    _new_ss, _, _, _ = take_screenshot()
                    _new_ph = compute_phash(_new_ss)
                    if _base_ph_gs and _new_ph and phash_distance(_base_ph_gs, _new_ph) >= PHASH_THRESHOLD:
                        state.gold_swipe_count = 0  # 画面変化 → リセット
                        break  # 変化検知 → 成功
                    _base_ph_gs = _new_ph
                    # 座標を少しずらして再試行 (+40px x方向)
                    _sx += 40
                return "GOLD_SWIPE_UP" if _dir == "UP" else "GOLD_SWIPE_DOWN", BATTLE_WAIT

    # ─── 【最優先 #0-ab】HSV金枠ボタン検出 → 中心タップ (Type B) ───
    # バトルチュートリアルで指アイコンが金枠ハイライトボタンを指している場面。
    # OCR が "隣接攻撃" "必殺技" を検出し、かつ右半分に金枠ボタンがある場合に発火。
    _battle_tut_kws = ["隣接攻撃", "必殺技", "巫殺技", "ATTACKER", "通常攻撃"]
    _is_battle_tut_context = any(kw in joined for kw in _battle_tut_kws)
    # バトルUI確認済みの場合はフッター外GoldBtnをスキップ → Glow SM (フッター) に委ねる
    if analysis_path is not None and _is_battle_tut_context and not _is_battle_early:
        _gold_btn = detect_tutorial_gold_button_tap(analysis_path, right_half_only=True)
        if _gold_btn:
            _bx, _by = _gold_btn
            logger.info(">>> [GoldBtn] 金枠ボタン検出 → tap(%d,%d)", _bx, _by)
            _base_ph_gb = compute_phash(analysis_path)
            tap_device(_bx, _by, state, "GOLD_BTN_TAP")
            _new_ss_gb, _, _, _ = take_screenshot()
            try:
                _new_ph_gb = compute_phash(_new_ss_gb)
            except Exception:
                _new_ph_gb = None
            if (not _base_ph_gb or not _new_ph_gb or
                    phash_distance(_base_ph_gb, _new_ph_gb) < PHASH_THRESHOLD):
                # 変化なし → Y方向に+30pxずらして再試行
                logger.info(">>> [GoldBtn] phash変化なし → +30px 再タップ (%d,%d)", _bx, _by + 30)
                tap_device(_bx, _by + 30, state, "GOLD_BTN_TAP_RETRY")
            return "GOLD_BTN_TAP", BATTLE_WAIT

    # ─── 【最優先 #0-a】テンプレートマッチング (Asset Match) — 最速 ~0.1s ───
    # チュートリアル中は指アイコン検出(TAP_HIGHLIGHTED_NAV/SWIPE_UP)が最高優先。
    # 指アイコン検出後 → 金色ハイライト要素をタップ。
    # 次優先: セリフ/ADVテキスト確認 (後続の#0/#3-ADV処理)
    if analysis_path is not None:
        asset_hit = ASSET_MANAGER.match(analysis_path, ocr_texts=texts)
        if asset_hit:
            cx, cy, action = asset_hit
            logger.info(">>> [Asset Match] '%s' → (%d,%d)", action, cx, cy)
            # スワイプ系アクションの処理
            if action == "SWIPE_UP":
                # 安全ネット: #0-DIALOG が例外等で抜けた場合の最終防衛
                # _DIALOG_FIRST_KWS キーワードがあればポップアップと判断してスキップ
                _swipe_skip = any(kw in joined for kw in _DIALOG_FIRST_KWS)
                if not _swipe_skip:
                    tmpl_meta = ASSET_MANAGER._templates.get("tutorial_swipe_pointer", {})
                    sx = tmpl_meta.get("swipe_from_x", cx)
                    sy = tmpl_meta.get("swipe_from_y", H - 50)
                    ex = tmpl_meta.get("swipe_to_x", cx)
                    ey = tmpl_meta.get("swipe_to_y", 50)
                    dur = tmpl_meta.get("swipe_duration_ms", 3000)
                    logger.info(">>> [SWIPE_UP] (%d,%d)→(%d,%d) %dms", sx, sy, ex, ey, dur)
                    swipe(sx, sy, ex, ey, dur, state=state)
                    return "SWIPE_UP", 1.5
                else:
                    logger.info(">>> [SWIPE_UP] ダイアログKW検出 → スキップ (#0-DIALOGへ)  ← safety net")
                    # ここに到達した場合は #0-DIALOG が None を返した異常ケース
                    # 固定座標でダイアログ ▷ をタップして続行
                    _dnf_x, _dnf_y = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
                    tap_device(_dnf_x, _dnf_y, state, "DIALOG_NEXT_FALLBACK")
                    return "DIALOG_NEXT_FALLBACK", 1.0
            # チュートリアル指差し: 金色ハイライトされたUI要素を方向非依存で検出→タップ
            if action == "TAP_HIGHLIGHTED_NAV":
                gold_pos = find_golden_highlighted_button(analysis_path)
                if gold_pos:
                    tap_x, tap_y = gold_pos
                else:
                    # フォールバック: 指アイコン直下160px
                    tap_x, tap_y = smart_tap_button(analysis_path, cx, cy + 160, search_r=160)
                logger.info(">>> [TAP_HIGHLIGHTED_NAV] 指(%d,%d) → 金色ハイライト(%d,%d)",
                            cx, cy, tap_x, tap_y)
                tap_device(tap_x, tap_y, state, "TAP_HIGHLIGHTED_NAV")
                return "TAP_HIGHLIGHTED_NAV", 1.5
            # ── NAME_INPUT_OK_TAP: 名前未入力(0/N)の場合は入力シーケンスへ ──
            if action == "NAME_INPUT_OK_TAP":
                import re as _re2
                _is_empty_field = any(_re2.match(r"^0/\d+$", t.strip()) for t in texts)
                if _is_empty_field:
                    _field_pos = detect_text_input_area(analysis_path, W, H, ocr_items=ocr)
                    if _field_pos:
                        _fx, _fy = _field_pos
                    else:
                        _fx, _fy = roi_to_device(int(W * 0.46), int(H * 0.58), state.game_roi)
                    logger.info(
                        ">>> [TEXT_INPUT_AREA] 空フィールド検出(0/N) → (%d,%d)タップ → adb input text",
                        _fx, _fy,
                    )
                    tap_device(_fx, _fy, state, "TEXT_INPUT_FOCUS")
                    import subprocess as _sp2
                    _sp2.run(
                        ["adb", "-s", DEVICE_SERIAL, "shell", "input", "text", "MadoDora"],
                        check=False,
                    )
                    time.sleep(0.5)
                    logger.info(">>> [TEXT_INPUT_AREA] 'MadoDora' 入力完了 → 次ループでOK")
                    return "TEXT_INPUT_NAME", 1.5
            # その他のアセットアクション: タップして return (fallthrough なし)
            tap_device(cx, cy, state, action)
            # GACHA_OK: 演出終了待ち (演出中はタップが無視されるため長めに待つ)
            _asset_wait = 5.0 if action == "GACHA_OK" else 0.5
            return action, _asset_wait

    # ─── 【優先 #0】チュートリアルポップアップ セカンダリセーフネット ───
    # #0-DIALOG (形状ベース) が失敗した場合の OCR キーワードによるバックアップ。
    # キーワードリストは _DIALOG_FIRST_KWS (定数) と共有して管理。
    pre_popup = has_any(ocr, list(_DIALOG_FIRST_KWS))
    if pre_popup:
        state.pre_popup_tap_count += 1
        # ── テンプレートマッチングで ▷/× を優先検出 ──
        _nav = detect_tutorial_dialog_nav(analysis_path, W, H) if analysis_path else None
        if _nav:
            _nav_type, cx, cy = _nav
            logger.info(">>> 【チュートリアルポップアップ】 '%s' %s→(%d,%d) [template]",
                        pre_popup["text"][:10], "×" if _nav_type == "close" else "▷", cx, cy)
            tap_device(cx, cy, state, "PRE_POPUP_TAP")
            return "TUTORIAL_POPUP", 1.0
        # ── フォールバック: 固定座標シーケンス ──
        # ▷ 矢印: 右エッジ (W*0.91, H*0.49) ≈ (1383, 353)
        # × ボタン: 右上隅 (W*0.98, H*0.056) ≈ (1490, 40)
        _arr = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)   # ▷ 矢印
        _cls = roi_to_device(int(W * 0.98), int(H * 0.056), state.game_roi)  # × ボタン
        tap_candidates = [
            _arr,   # ▷ 矢印 (1回目)
            _arr,   # ▷ 矢印 (2回目)
            _arr,   # ▷ 矢印 (3回目)
            _arr,   # ▷ 矢印 (4回目)
            _cls,   # × ボタン (最終ページ)
            _cls,   # × ボタン (リトライ)
        ]
        idx = min(state.pre_popup_tap_count - 1, len(tap_candidates) - 1)
        cx, cy = tap_candidates[idx]
        _label = "×" if (cx, cy) == _cls else "▷"
        logger.info(">>> 【チュートリアルポップアップ】 '%s' %s→(%d,%d) (試行%d回目)",
                    pre_popup["text"][:10], _label, cx, cy, state.pre_popup_tap_count)
        tap_device(cx, cy, state, "PRE_POPUP_TAP")
        return "TUTORIAL_POPUP", 1.0

    # ─── 【最優先 #0-b-extra】プレイヤー名入力ダイアログ ───
    # 「プレイヤー名を入力してください」→ 名前入力 → OKタップ
    # 注意: OCR で "OK" の center が y≈593 と検出されるが、
    #        実際のボタンヒットゾーンはゴールデンエリア y≈555-575 (実測)
    name_input = has_text(ocr, "プレイヤー名を入力", min_conf=0.3)
    if name_input:
        # 入力済みテキストを確認 (プレースホルダー・UI テキスト以外のひらがな/英字)
        ui_words = {"プレイヤー名を入力してください", "プレイヤー名は", "変更後3日間", "名前入力", "OK"}
        name_texts = [t for t in texts if t not in ui_words and len(t) >= 2
                      and not t.startswith("プレイヤー") and "/" not in t]
        ok_item = next(
            (item for item in ocr if "OK" in item.get("text", "") and item["center"][1] > H * 0.5),
            None
        )
        if name_texts and ok_item:
            # 名前入力済み → OKタップ (ROI補正: OCR-X + 比率Y=H*0.78)
            cx, cy = roi_to_device(ok_item["center"][0], int(H * 0.78), state.game_roi)
            logger.info(">>> 【名前入力 OK】 入力済み='%s' → ROI補正(%d,%d) タップ", name_texts[0], cx, cy)
            tap_device(cx, cy, state, "NAME_INPUT_OK")
            return "NAME_INPUT_OK", 2.0
        elif ok_item:
            # 名前未入力 → テキストフィールドをタップして "MadoDora" 入力 → Enter → OK
            _nf_x, _nf_y = roi_to_device(int(W * 0.46), int(H * 0.58), state.game_roi)
            logger.info(">>> 【名前入力】 テキストフィールドをフォーカス (%d,%d)", _nf_x, _nf_y)
            tap_device(_nf_x, _nf_y, state, "NAME_INPUT_FOCUS")
            import subprocess as _sp
            _sp.run(["adb", "-s", DEVICE_SERIAL, "shell", "input", "text", "MadoDora"], check=False)
            time.sleep(0.3)
            _sp.run(["adb", "-s", DEVICE_SERIAL, "shell", "input", "keyevent", "66"], check=False)
            logger.info(">>> 【名前入力】 'MadoDora' 入力完了 → OK タップ待ち")
            return "NAME_INPUT_TEXT", 1.5

    # ─── 【最優先 #0-b】報酬/強化結果ポップアップを即時処理 (ブロブ誤検出防止) ───
    # 「以下の内容でよろしいですか」確認ダイアログ → SmartTap で OK 物理中心をタップ
    confirm_dlg = has_text(ocr, "以下の内容でよろしいですか", min_conf=0.3)
    if confirm_dlg:
        ok_bottom = next(
            (item for item in ocr
             if "OK" in item.get("text", "") and item["center"][1] > H * 0.6),
            None
        )
        if ok_bottom:
            ocr_cx, ocr_cy = ok_bottom["center"]
        else:
            ocr_cx, ocr_cy = int(W * 0.70), int(H * 0.88)  # 比率ベースフォールバック
        cx, cy = smart_tap_button(analysis_path, ocr_cx, ocr_cy)
        logger.info(">>> 【確認ダイアログ】 SmartTap OK (%d,%d)", cx, cy)
        tap_device(cx, cy, state, "CONFIRM_DIALOG_OK")
        return "CONFIRM_DIALOG_OK", 1.5

    # 「タップして次へ」: 報酬獲得画面の次へ進む
    tap_next = has_text(ocr, "タップして次へ", min_conf=0.3)
    if tap_next:
        cx, cy = tap_next["center"]
        logger.info(">>> 【報酬/次へ】 'タップして次へ' (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "REWARD_NEXT")
        return "REWARD_NEXT", 1.0

    # 限界突破/強化完了/レベルアップ系ポップアップ → 右上 × ボタンで閉じる
    close_popup_kws = ["限界突破", "強化完了", "レベルアップ", "称号獲得", "エピソード解放",
                       "ランクアップ", "新しいコンテンツ", "アンロック",
                       "マギアボックス", "ミッション達成", "デイリーミッション",
                       "ログインボーナス", "初心者ログイン", "キャンペーン"]

    # カルーセル型チュートリアルポップアップ (「メインクエストをPLAYして」等の複数ページ説明)
    # 閉じるボタン: ポップアップフレーム右上 (1430, 88) — 実測 2026-03-05
    carousel_popup_kws = ["メインクエストをPLAY", "ピュエラピクトゥーラ", "POWER UP"]
    carousel_match = has_any(ocr, carousel_popup_kws)
    if carousel_match:
        # 最終ページへ移動 (右ナビゲーション × 6) → フレーム右上 × をタップ
        _cn_x, _cn_y = roi_to_device(int(W * 0.96), int(H * 0.5), state.game_roi)
        for _ in range(6):
            tap_device(_cn_x, _cn_y, state, "CAROUSEL_NAV_RIGHT", post_wait=0.3)
        close_x, close_y = roi_to_device(int(W * 0.94), int(H * 0.12), state.game_roi)
        logger.info(">>> 【カルーセルポップアップ】 '%s' → フレーム右上 (%d,%d) タップ",
                    carousel_match["text"][:10], close_x, close_y)
        tap_device(close_x, close_y, state, "CAROUSEL_CLOSE")
        return "CLOSE_POPUP", 2.0
    close_popup = has_any(ocr, close_popup_kws)
    if close_popup:
        close_x = W - 40  # 右上 × ボタン (1520-40=1480)
        close_y = 40
        logger.info(">>> 【%s ポップアップ】 → × (%d,%d) タップ", close_popup["text"][:6], close_x, close_y)
        tap_device(close_x, close_y, state, f"CLOSE_POPUP_{close_popup['text'][:6]}")
        return "CLOSE_POPUP", 1.5

    # 「〜してみましょう」型チュートリアルガイド + ブロブスタック → × で閉じる
    # 例: "今回は自動編成をしてみましょう。" が表示されたまま動かない場合
    if state.blob_same_count >= 5:
        tutorial_guide = (has_text(ocr, "てみましょう", min_conf=0.3) or
                          has_text(ocr, "しましょう", min_conf=0.3))
        is_battle_guide = any(kw in " ".join(texts) for kw in ["通常攻撃", "BREAK", "WAVE"])
        if tutorial_guide and not is_battle_guide:
            close_x = W - 40  # 右上 × ボタン (1480, 40)
            close_y = 40
            logger.info(">>> 【チュートリアルガイド スタック】 '%s' → × (%d,%d) タップ",
                        tutorial_guide["text"][:10], close_x, close_y)
            tap_device(close_x, close_y, state, "TUTORIAL_GUIDE_CLOSE")
            state.blob_same_count = 0
            return "CLOSE_POPUP", 1.5

    # ─── 【最優先 #1-pre】バトル発光 State Machine (フッター下部30%限定) ─────────
    # 優先度 1: 左キャラ発光 → タップ → character_selected=True
    # 優先度 2: 右スキル発光 (character_selected=True) → タップ
    # 優先度 3: 発光なし + character_selected → 通常攻撃 OCR フォールバック
    if _is_battle_early and analysis_path is not None:
        _gsm_glows = detect_guide_glow(analysis_path, W, H, footer_ratio=0.30)
        _gsm_left = [g for g in _gsm_glows if g["side"] == "left"]
        _gsm_right = [g for g in _gsm_glows if g["side"] == "right"]
        if _gsm_glows:
            logger.info("[GLOW_SM] フッター発光: 左%d個(最大%.0f) 右%d個(最大%.0f)",
                        len(_gsm_left), _gsm_left[0]["area"] if _gsm_left else 0,
                        len(_gsm_right), _gsm_right[0]["area"] if _gsm_right else 0)

        # Priority 1: 左キャラ発光検出 (キャラ未選択時)
        if not state.character_selected and _gsm_left:
            _gl = max(_gsm_left, key=lambda g: g["area"])
            _gl_x, _gl_y = _gl["cx"], max(1, _gl["cy"] - 35)
            logger.info("[GLOW_SM P1] 左キャラ発光(%d,%d) → tap(%d,%d)", _gl["cx"], _gl["cy"], _gl_x, _gl_y)
            tap_device(_gl_x, _gl_y, state, "GLOW_LEFT_CHAR", post_wait=0.3)
            tap_device(_gl_x, _gl_y, state, "GLOW_LEFT_CHAR")  # ダブルタップ(追いタップ)
            state.character_selected = True
            state.char_just_selected = True
            state.finger_detections += 1
            return "GLOW_LEFT_CHAR", 0.3

        # Priority 2: 右スキル発光検出 (キャラ選択済み)
        elif state.character_selected and _gsm_right:
            _gr = max(_gsm_right, key=lambda g: g["area"])
            _gr_x, _gr_y = _gr["cx"], max(1, _gr["cy"] - 35)
            logger.info("[GLOW_SM P2] 右スキル発光(%d,%d) → tap(%d,%d)", _gr["cx"], _gr["cy"], _gr_x, _gr_y)
            tap_device(_gr_x, _gr_y, state, "GLOW_RIGHT_SKILL")
            state.character_selected = False
            state.char_just_selected = False
            state.finger_detections += 1
            return "GLOW_RIGHT_SKILL", 0.3

        # Priority 3: 発光なし + character_selected → 通常攻撃/单体攻撃 OCR フォールバック
        elif state.character_selected and not _gsm_right:
            _na_item = has_any(ocr, ["通常攻撃", "单体攻撃", "単体攻撃"])
            if _na_item:
                _na_x, _na_y = _na_item["center"]
                if _na_x > W * 0.5 and _na_y > H * 0.5:
                    _na_y = max(1, _na_y - 35)
                    logger.info("[GLOW_SM P3] 攻撃ボタンOCR '%s'(%d,%d) → tap (発光なしフォールバック)",
                                _na_item["text"], _na_x, _na_y)
                    tap_device(_na_x, _na_y, state, "NORMATK_TAP")
                    state.character_selected = False
                    state.char_just_selected = False
                    return "NORMATK_TAP", 1.0

    # ─── 【最優先 #1】指差しアイコン (肌色ブロブ) 検出 ───
    if analysis_path is not None:
        # 「AUTO」のみはストーリー画面にも表示されるため除外、戦闘固有キーワードで判定
        is_battle_screen = any(kw in " ".join(texts) for kw in
                               ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "必殺技", "BREAK", "WAVE", "Turn"])
        # タイトル画面 / ホーム画面検出: ブロブ誤検出を防ぐ
        _nav_joined = " ".join(texts)
        # 利用規約画面・同意ダイアログが存在する場合はタイトル画面と区別する
        _is_tos_screen = "利用規約" in _nav_joined or "同意してゲームを始める" in _nav_joined
        _title_kws_game = ["魔法", "少女", "まどか", "マギカ", "まどかハ", "MADOKA", "MAGICA"]
        is_title_screen = (
            not _is_tos_screen and (
                any(kw in _nav_joined for kw in ["TAP TO START", "Magia Exedra",
                                                  "MAGIA EXEDRA", "TAPTOSTART"]) or
                # 「動画配信設定」「Ver.」はタイトル画面固有の上部 UI
                (any(kw in _nav_joined for kw in ["動画配信", "勤画配信", "Ver.2", "Ver.2."])
                 and any(kw in _nav_joined for kw in _title_kws_game + ["PUELLA"])) or
                ("VID" in _nav_joined and any(kw in _nav_joined for kw in _title_kws_game)) or
                # フォールバック: ゲームタイトルロゴ文字 + Rank がない + ホームナビがない
                # "Rank" "Main" "推奨" は MAIN STORY 選択画面なのでタイトルと区別する
                (any(kw in _nav_joined for kw in ["PUELLA MAGI", "PUELLAHAGI", "PUELLAMAGI",
                                                   "PUELLA MAGIMADOKA"])
                 and any(kw in _nav_joined for kw in _title_kws_game)
                 and not any(kw in _nav_joined for kw in ["クエスト", "ショップ", "ガチャ",
                                                           "Rank", "Main", "推奨"]))
            )
        )
        if is_title_screen:
            logger.info("  タイトル画面検出 → TAP TO START (760,628) タップ")
            _tt_x, _tt_y = roi_to_device(int(W * 0.5), int(H * 0.87), state.game_roi)
            tap_device(_tt_x, _tt_y, state, "TITLE_TAP_START")
            return "TITLE_TAP", 3.0
        # ホーム画面検出: ホームナビキーワードが2個以上 → キャラ画像のブロブ誤検出をスキップ
        _home_nav_kws = ["クエスト", "ショップ", "ガチャ", "ガシャ", "ユニオン",
                         "光の間", "パーティ", "プレイヤーマッチ", "お知らせ",
                         "イベント", "マイページ", "編成", "MAGIA EXEDRA"]
        _home_kw_count = sum(1 for h in _home_nav_kws if any(h in t for t in texts))
        # ガチャ結果画面検出: "NEW" が 3件以上 → キャラ画像のオレンジ色を誤検出するためブロブ無効化
        new_count = sum(1 for t in texts if t == "NEW")
        is_gacha_result = new_count >= 3 and not is_battle_screen
        if is_gacha_result:
            logger.info("  ガチャ結果画面検出 (NEW×%d) → もや誤検出スキップ", new_count)
            # OKボタンをダブルタップして進む (シングルタップでは反応しないゲームの挙動対策)
            ok_match = has_text(ocr, "OK", min_conf=0.5)
            if ok_match:
                cx, cy = ok_match["center"]
                action_type, desc = STRATEGIC_ENGINE.log_prediction("OK", cx, cy)
                state.last_prediction = action_type
                state.last_prediction_desc = desc
                state.last_tap_text = "OK"
                state.last_action_pre_phash = state.last_phash
                logger.info(">>> 【ガチャ結果】 OK (%d,%d) → ダブルタップ", cx, cy)
                tap_device(cx, cy, state, "GACHA_RESULT_OK_1", post_wait=0.3)
                tap_device(cx, cy, state, "GACHA_RESULT_OK_2")
                return "GACHA_OK", 2.0
            # OKがない場合は画面中央をダブルタップ (NEW×8の初期表示 = タップで詳細へ)
            _gc_x, _gc_y = roi_to_device(int(W * 0.5), int(H * 0.5), state.game_roi)
            logger.info(">>> 【ガチャ結果初期】 OK未検出 → 画面中央ダブルタップ (%d,%d)", _gc_x, _gc_y)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_1", post_wait=0.3)
            tap_device(_gc_x, _gc_y, state, "GACHA_RESULT_CENTER_2")
            return "GACHA_OK", 2.0
        # ─── ADV選択肢「はい」「いいえ」など ─────────────────────────────
        # 選択肢ボタンはブロブ検出より優先タップ (ブロブが選択肢を隠す場合がある)
        _adv_choice_kws = ["はい", "いいえ", "わかった", "了解", "OK"]
        _adv_choice = has_any(ocr, _adv_choice_kws)
        if _adv_choice:
            _ac_x, _ac_y = _adv_choice["center"]
            # OCR枠が実際のボタン視覚領域より下にずれることがある → 輝度ベースで補正
            _ac_y = _correct_btn_tap_y(state.last_screen, _ac_x, _ac_y, _adv_choice["box"])
            logger.info(">>> 【ADV選択肢】 '%s' (%d,%d) タップ", _adv_choice["text"], _ac_x, _ac_y)
            tap_device(_ac_x, _ac_y, state, f"ADV_CHOICE '{_adv_choice['text']}'")
            return "ADV_CHOICE", 1.0

        # ─── バトルResultリザルト画面 ───────────────────────────────────────
        # "次へ" + Resultコンテキスト(EXP/Lv.1/リザルト)が見えている間は優先タップ
        # ※ OCRが "Result" を "kesuit" 等と誤読する場合も EXP/Lv.1 で補完
        _result_ctx = (has_text(ocr, "Result") or has_text(ocr, "EXP")
                       or has_text(ocr, "Lv.1") or has_text(ocr, "リザルト"))
        if _result_ctx:
            # 「次へ」は右下エリア(y>H*0.6, x>W*0.5)にあるはず。誤検出を位置フィルタで排除
            _nxt_btn = None
            for _ocr_item in ocr:
                _txt = _ocr_item.get("text", "")
                if "次へ" in _txt or "NEXT" in _txt:
                    _cx_n, _cy_n = _ocr_item["center"]
                    if _cy_n > H * 0.6 and _cx_n > W * 0.5:
                        _nxt_btn = _ocr_item
                        break
            if _nxt_btn:
                _nx, _ny = _nxt_btn["center"]
                logger.info(">>> 【バトルResult】 次へ (%d,%d) タップ", _nx, _ny)
                tap_device(_nx, _ny, state, "RESULT_NEXT")
                return "RESULT_TAP", 1.0

        # バトル時は dark_mode=True で輝度閾値を緩和し min_area=200 に下げる（暗背景対応）
        # ホーム画面 / 利用規約ダイアログ / システムダイアログはブロブ誤検出になるためスキップ
        _is_system_dialog = any(kw in _nav_joined for kw in
                                ["画質を設定", "高画質", "省エネ", "省工ネ", "データ引き継ぎ",
                                 "サポート", "お問い合わせ", "キャッシュクリア"])
        if _home_kw_count >= 2:
            logger.info("  ホーム画面検出 (nav×%d) → MOYA_TAP スキップ", _home_kw_count)
            # ── 【最優先】ダンジョン挑戦ボタン: 「挑戦」OCR検出 → 直接タップ (ホーム誤検出突破) ──
            _chal_btn = has_text(ocr, "挑戦", min_conf=0.3)
            if _chal_btn:
                _cb_x, _cb_y = _chal_btn["center"]
                # 挑戦ボタンは右下(x>W*0.5, y>H*0.5)にあるはず
                if _cb_x > W * 0.5 and _cb_y > H * 0.5:
                    logger.info("  [CHALLENGE_BTN] 挑戦(%d,%d) → 直接タップ (ホーム誤検出突破)", _cb_x, _cb_y)
                    tap_device(_cb_x, _cb_y, state, "CHALLENGE_TAP")
                    return "CHALLENGE_TAP", 1.5
            # ── ホームチュートリアル: 指アイコン+金枠がある場合は優先タップ ──
            _ht_blobs = find_finger_blobs(analysis_path) if analysis_path else []
            # ホーム画面では中央ナビバー (ショップ等) が右半分境界付近に来るため right_half_only=False
            _ht_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
            if _ht_blobs or _ht_gold:
                _ht_target = None
                if _ht_blobs:
                    _ht_chosen = max(_ht_blobs, key=lambda b: b[2])
                    _ht_bx, _ht_by = _ht_chosen[0], _ht_chosen[1]
                    _ht_gf = find_gold_frame_near(analysis_path, _ht_bx, _ht_by) if analysis_path else None
                    if _ht_gf:
                        _ht_target = (_ht_gf[0], _ht_gf[1])
                        logger.info("  ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) タップ",
                                    _ht_bx, _ht_by, _ht_gf[0], _ht_gf[1])
                    else:
                        _ht_tip_y = _ht_chosen[4] + int(_ht_chosen[6] * 0.1)
                        _ht_target = (_ht_chosen[3] + _ht_chosen[5] // 2, _ht_tip_y)
                        logger.info("  ホームチュートリアル: 指(%d,%d)→指先(%d,%d) タップ",
                                    _ht_bx, _ht_by, *_ht_target)
                elif _ht_gold:
                    _ht_target = _ht_gold
                    logger.info("  ホームチュートリアル: 金ボタン(%d,%d) タップ", *_ht_gold)
                if _ht_target:
                    tap_device(_ht_target[0], _ht_target[1], state, "HOME_TUTORIAL_TAP")
                    return "HOME_TUTORIAL_TAP", 1.5
            blobs = []
        elif _is_tos_screen or _is_system_dialog:
            logger.info("  システムダイアログ/利用規約検出 → MOYA_TAP スキップ")
            blobs = []
        else:
            # バトル中は dark_mode=True + min_area=200 で暗背景の指アイコンも検知
            _blob_dark = is_battle_screen
            blobs = find_finger_blobs(analysis_path,
                                      min_area=200 if _blob_dark else 400,
                                      dark_mode=_blob_dark)
            # 画面端の誤検出を除去: y<36px(上端)または x>W-40px(右端最端)はシステムUI
            blobs = [b for b in blobs if b[1] > 36 and b[0] < W - 40]
        if blobs:
            # バトル中は中央エリア(バトルフィールド)の肌色は誤検出なので無視
            # 優先順位: 左キャラカード(x<600,y>550) > 右パネル(x>1050) > 下部UI(y>H*0.8)
            if is_battle_screen:
                left_char = [b for b in blobs if b[0] < 600 and b[1] > H * 0.76]
                # right_panel: スキルボタンは下半分(y>H*0.45)のみ。上部の蝶エネミーを排除
                right_panel = [b for b in blobs if b[0] > 1050 and b[1] > H * 0.45]
                bottom_ui = [b for b in blobs if b[1] > H * 0.8 and b[0] >= 600]
                if state.char_just_selected:
                    # 左キャラ選択済み → 右スキルを選択 (左キャラ再タップしない)
                    if right_panel:
                        blobs = right_panel
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 右スキルもや %d個", len(blobs))
                    elif bottom_ui:
                        blobs = bottom_ui
                        state.char_just_selected = False
                        logger.info("  バトル: キャラ選択後 → 下部UIもや %d個", len(blobs))
                    else:
                        # 右スキルがまだ表示されていない → 少し待つ
                        logger.info("  バトル: キャラ選択後 → 右スキル待ち")
                        blobs = []
                elif left_char:
                    # フリーバトル: 左キャラ選択が最優先
                    blobs = left_char
                    logger.info("  バトル: 左キャラもや %d個 (最優先)", len(blobs))
                elif right_panel:
                    blobs = right_panel
                    logger.info("  バトル: 右パネルもや %d個", len(blobs))
                elif bottom_ui:
                    blobs = bottom_ui
                    logger.info("  バトル: 下部UIもや %d個", len(blobs))
                else:
                    logger.info("  バトル中: 有効もやなし(中央は誤検出) → OCR判定へ")
                    blobs = []

        if blobs:
            # ── Hard Masking 2.0: バトル中は先頭blobの350×350以外を黒塗り ──
            # 右側スキルボタンの金枠を物理的に排除し、指アイコンの示すターゲットだけを照準
            _hm_analysis = analysis_path
            if is_battle_screen and analysis_path is not None:
                _hm_cx, _hm_cy = blobs[0][0], blobs[0][1]
                _hm_analysis = create_finger_mask_image(analysis_path, _hm_cx, _hm_cy, half=175)
                if _hm_analysis != analysis_path:
                    logger.info("[HARD_MASK] 指(%d,%d)周囲350×350px以外 黒塗り → 認識対象限定",
                                _hm_cx, _hm_cy)

            # ── 2段階ターゲット選択 ──────────────────────────────
            # Step1: 金枠が見つかる blobs を優先（真のGUIガイド要素）
            # Step2: 金枠なし blobs は右側優先フォールバック
            _blob_with_gold = None
            _blob_gold_frame = None
            _blob_fallback = None
            for _b in blobs:
                _bx0, _by0 = _b[0], _b[1]
                # Hard Masking 2.0 適用: バトル時はマスク済み画像で金枠検索
                _gf = find_gold_frame_near(_hm_analysis, _bx0, _by0, search_radius=150)
                if _gf is not None:
                    # 金枠が見つかった最初のblobを使用
                    if _blob_with_gold is None:
                        _blob_with_gold = _b
                        _blob_gold_frame = _gf
                if _blob_fallback is None and _b[0] > 1050:
                    _blob_fallback = _b  # 右側優先フォールバック
            if _blob_fallback is None and blobs:
                _blob_fallback = blobs[0]

            if _blob_with_gold is not None:
                chosen = _blob_with_gold
                _gold_frame = _blob_gold_frame
                logger.info("  (金枠ありblob優先: %d個中1個)", len(blobs))
            else:
                chosen = _blob_fallback
                _gold_frame = None
                if len(blobs) > 1:
                    logger.info("  (金枠なし → 右パネル優先: %d個中1個を選択)", len(blobs))
            # ────────────────────────────────────────────────────
            fx, fy, fa = chosen[0], chosen[1], chosen[2]
            f_bx, f_by, f_bw, f_bh = chosen[3], chosen[4], chosen[5], chosen[6]
            # 50px 近接判定: アニメーション中のブロブ (±20px移動) でもカウントが継続する
            if state.last_blob_xy == (0, 0):
                # 初回検出: 基準座標を設定してカウントを0にリセット
                state.last_blob_xy = (fx, fy)
                state.blob_same_count = 0
            elif abs(fx - state.last_blob_xy[0]) <= 50 and abs(fy - state.last_blob_xy[1]) <= 50:
                state.blob_same_count += 1
                state.last_blob_xy = (fx, fy)  # 追跡: 次回比較基準を更新
            else:
                state.blob_same_count = 0
                state.last_blob_xy = (fx, fy)
            if state.blob_same_count >= 5:
                import random as _rnd_rec
                _stg = state.blob_same_count
                logger.info(">>> [RECOVERY] スタック stage=%d (%d,%d)", _stg, fx, fy)
                # 移動シーン(OCR無し) + 10回以上 → SWIPE_UP 強制 (最優先)
                if _stg >= 10 and len(texts) == 0:
                    logger.info(">>> [SWIPE_FALLBACK] フィンガースタック%d回+OCR無し → SWIPE_UP強制", _stg)
                    swipe(fx, H - 50, fx, 50, 3000, state=state)
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "SWIPE_FALLBACK", 1.5
                # Stage 1-3 (count=5,6,7): ジッター±10px タップ
                if _stg <= 7:
                    _jx = max(50, min(W - 50, fx + _rnd_rec.randint(-10, 10)))
                    _jy = max(50, min(H - 50, fy + _rnd_rec.randint(-10, 10)))
                    logger.info(">>> [RECOVERY s%d] ジッタータップ (%d,%d)", _stg - 4, _jx, _jy)
                    tap_device(_jx, _jy, state, f"RECOVERY_JITTER_s{_stg - 4}")
                    if _stg == 7:
                        time.sleep(1.5)
                    return "RECOVERY_JITTER", 0.5
                # Stage 4-6 (count=8,9,10): キャッシュ破棄・広域金枠再スキャン
                elif _stg <= 10:
                    _rf_gf = find_gold_frame_near(analysis_path, fx, fy, search_radius=300) if analysis_path else None
                    if _rf_gf:
                        _rf_tx, _rf_ty = _rf_gf[0], _rf_gf[1]
                        logger.info(">>> [RECOVERY s%d] 広域金枠(%d,%d) タップ", _stg - 7, _rf_tx, _rf_ty)
                        tap_device(_rf_tx, _rf_ty, state, f"RECOVERY_RESCAN_s{_stg - 7}")
                        state.blob_same_count = 0
                        state.last_blob_xy = (0, 0)
                        return "RECOVERY_RESCAN", 1.0
                    logger.info(">>> [RECOVERY s%d] 広域金枠なし → OCRフォールバックへ", _stg - 7)
                    # OCR ベース処理に落ちる (fall through)
                # Stage 7-9 (count=11,12,13): ランダムブラインドタップ
                elif _stg <= 13:
                    _bx = max(50, min(W - 50, fx + _rnd_rec.randint(-80, 80)))
                    _by = max(50, min(H - 50, fy + _rnd_rec.randint(-60, 60)))
                    logger.info(">>> [RECOVERY s%d] ブラインドタップ (%d,%d)", _stg - 10, _bx, _by)
                    tap_device(_bx, _by, state, f"RECOVERY_BLIND_s{_stg - 10}")
                    return "RECOVERY_BLIND", 0.8
                # Stage 10 (count>=14): 5秒待機 + リセット
                else:
                    logger.info(">>> [RECOVERY s10] 5秒待機 + カウンタリセット")
                    time.sleep(5.0)
                    state.blob_same_count = 0
                    state.last_blob_xy = (0, 0)
                    return "RECOVERY_FINAL_WAIT", 0.5
            else:
                # ── Step3: タップ座標決定 ──
                # 金枠あり → 金枠の中心点を射抜く
                # 金枠なし → 矩形上端から10%の位置（指先）
                if _gold_frame is not None:
                    gfx, gfy, gfw, gfh = _gold_frame
                    tap_x, tap_y = gfx, gfy
                    _gbox = (gfx - gfw // 2, gfy - gfh // 2, gfw, gfh)
                    logger.info("FINGER+GOLD_FRAME (%d,%d) → tap_center(%d,%d) frame=%dx%d",
                                fx, fy, tap_x, tap_y, gfw, gfh)
                else:
                    # 指アイコン矩形の上端10%をタップ (指先位置)
                    tap_x = fx
                    tap_y = f_by + max(1, int(f_bh * 0.1))
                    _gbox = None
                    logger.info("FINGER_DETECTED (%d,%d) area=%.0f → tip(%d,%d) count=%d",
                                fx, fy, fa, tap_x, tap_y, state.blob_same_count)
                # ── Y -35px 座標補正 (System Bar Fix) ──────────────────────────
                # バトル画面でボタン下端を叩くズレを補正: 35px 上方向シフト
                if is_battle_screen and tap_y > 35:
                    tap_y -= 35
                    logger.info("  [Y_SHIFT-35] バトル座標補正 → tap_y=%d", tap_y)
                tap_device(tap_x, tap_y, state, f"MOYA_TAP ({tap_x},{tap_y})",
                           finger_box=(f_bx, f_by, f_bw, f_bh),
                           gold_box=_gbox)
                state.finger_detections += 1
                if _gold_frame is not None:
                    state.gold_detections += 1
                # 左キャラ選択後は char_just_selected / character_selected フラグをセット
                if fx < 600 and fy > H * 0.76:
                    state.char_just_selected = True
                    state.character_selected = True  # GLOW SM 用にも同期
                    logger.info("  (左キャラ選択完了 → 次は右スキル)")
                return "MOYA_TAP", 1.0

    # ─── 【最優先 #2-a】探索マップ 3D矢印タップ ───
    # 「矢印をタップしてください」が出ている場合、3D空間の矢印を検出してタップ
    arrow_instruction = has_text(ocr, "矢印をタップ", min_conf=0.2)
    if arrow_instruction and analysis_path is not None:
        pos = find_3d_arrow(analysis_path)
        if pos:
            cx, cy = pos
            logger.info(">>> 【3D矢印】 探索マップ矢印 (%d,%d) 検出 → タップ", cx, cy)
            tap_device(cx, cy, state, "MAP_ARROW_TAP")
            # [Auto Save] 初回検出時にテンプレートとして保存
            if "map_arrow" not in ASSET_MANAGER._templates:
                half_w, half_h = 70, 50
                ASSET_MANAGER.save_template(
                    analysis_path,
                    max(0, cx - half_w), max(0, cy - half_h),
                    min(W, cx + half_w), min(H, cy + half_h),
                    name="map_arrow", action="MAP_ARROW_TAP",
                    threshold=0.65,
                    require_ocr=["矢印をタップ"],
                )
            return "MAP_ARROW_TAP", 1.0
        else:
            # 自動検出失敗 → キャラ頭上デフォルト座標
            _ma_x, _ma_y = roi_to_device(int(W * 0.5), int(H * 0.29), state.game_roi)
            logger.info(">>> 【3D矢印】 自動検出失敗 → デフォルト (%d,%d) タップ", _ma_x, _ma_y)
            tap_device(_ma_x, _ma_y, state, "MAP_ARROW_FALLBACK")
            return "MAP_ARROW_TAP", 1.0

    # ─── 【最優先 #2】ハイライト指示テキスト ───
    tutorial_kws = ["ここをタップ", "タップしてください", "タップして下さい", "タップして"]
    for kw in tutorial_kws:
        match = has_text(ocr, kw, min_conf=0.3)
        if match:
            cx, cy = match["center"]
            logger.info(">>> 【ハイライト指示】 '%s' (%d,%d)", kw, cx, cy)
            tap_device(cx, cy, state, f"HIGHLIGHT '{kw}'")
            return "HIGHLIGHT_TAP", 0.5

    # ─── ストーリーセリフ進行 (バトル外でセリフが出ている) ───
    # 「画面をタップ」系の指示 or バトルでもホームでもない日本語テキストが複数ある
    is_battle_now = any(kw in " ".join(texts) for kw in
                        ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃", "必殺技", "BREAK", "WAVE", "Turn"])
    tap_screen_kws = ["画面をタップ", "タップして進む", "タップで進む", "タップしてください",
                      "タップして次へ", "TOUCH TO CONTINUE"]
    tap_screen = has_any(ocr, tap_screen_kws)
    if tap_screen and not is_battle_now:
        cx, cy = tap_screen["center"]
        logger.info(">>> 【画面タップ指示】 '%s' (%d,%d)", tap_screen["text"], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP_HINT")
        return "STORY_TAP", 0.3

    # ─── ホーム画面検出 ───
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成"]
    home_count = sum(1 for h in home_indicators if any(h in t for t in texts))
    if home_count >= 3:
        state.home_reached = True
        # ── 指アイコン or 金枠がある場合 → まだホームチュートリアル中 ──
        # 「ホーム画面かつ指アイコン+金枠がない」状態が本当のチュートリアル終了
        _home_blobs = find_finger_blobs(analysis_path) if analysis_path else []
        # ホーム画面ではナビバーが中央付近に来るため right_half_only=False で全域検索
        _home_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
        if _home_blobs or _home_gold:
            # 指アイコン+金枠が存在 → ガイドに従いタップして続行
            _tap_target = None
            if _home_blobs:
                _chosen_blob = max(_home_blobs, key=lambda b: b[2])  # area最大
                _bx, _by = _chosen_blob[0], _chosen_blob[1]
                # 金枠があれば金枠中心を優先
                _gf = find_gold_frame_near(analysis_path, _bx, _by) if analysis_path else None
                if _gf:
                    _tap_target = (_gf[0], _gf[1])  # (frame_cx, frame_cy)
                else:
                    # blob tuple: (cx, cy, area, bx, by, bw, bh) → 指先は bx+bw/2, by+bh*0.1
                    _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                    _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
            elif _home_gold:
                _tap_target = _home_gold
            if _tap_target:
                logger.info(">>> ホームチュートリアル継続: 指/金枠 → (%d,%d) タップ", *_tap_target)
                tap_device(_tap_target[0], _tap_target[1], state, "HOME_TUTORIAL_TAP")
                return "HOME_TUTORIAL_TAP", 1.5
        # チュートリアルポインタが同一座標でスタックしている → クエスト探索へ移行
        if state.blob_same_count >= 5:
            logger.info(">>> ホーム画面 + もやスタック → クエストへナビゲート")
            state.blob_same_count = 0  # リセット: 次回はまたブロブ検出を試みる
            state.home_nav_count += 1
            quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
            if quest_btn:
                cx, cy = quest_btn["center"]
                logger.info(">>> クエストボタン (%d,%d) タップ", cx, cy)
                tap_device(cx, cy, state, "QUEST_FROM_HOME")
                return "QUEST_FROM_HOME", 3.0
            # OCR未検出 → 右下固定座標 (1520×720 画面での位置)
            _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
            tap_device(_qf_x, _qf_y, state, "QUEST_FIXED")
            return "QUEST_FROM_HOME", 3.0
        # クエストへの遷移を試みた後、まだホーム画面が表示されている → 遷移待ち
        if state.home_nav_count > 0:
            logger.info(">>> ホーム画面 + 遷移試行 %d回目 → 画面変化待ち", state.home_nav_count)
            return "HOME_NAV_WAIT", 2.0
        # 指アイコンも金枠もない → 真のチュートリアル終了
        logger.info(">>> ホーム画面検出! (%d個) 指/金枠なし → チュートリアル完了!", home_count)
        return "HOME_REACHED", 0

    # ─── ダウンロード/ロード中 ───
    dl = has_any(ocr, ["ダウンロード", "追加データ", "Loading", "ロード中",
                       "通信中", "Now Loading", "Download", "Downloading"])
    # 進捗バー: %, GB, MB を含む文字列も進捗画面と判定 (英語ダウンロード画面対応)
    # ※ バトル中のダメージ倍率 (例: BREAK200%) との誤検知を防ぐため、
    #   バトルキーワードが OCR に含まれる場合は % による判定をスキップ
    if not dl:
        _battle_guard_kws = ["通常攻撃", "单体攻撃", "単体攻撃", "BREAK", "WAVE",
                             "ENEMY TURN", "BATTLE", "AUTO", "必殺技"]
        _in_battle_context = any(kw in t for t in texts for kw in _battle_guard_kws)
        if not _in_battle_context:
            # バッテリー残量 (「電池」「Battery」コンテキスト) と誤認しないガード
            _battery_ctx = any(kw in joined for kw in ["電池", "Battery", "バッテリー", "電池切れ"])
            _progress_texts = [t for t in texts if ("%" in t or "MB" in t or "GB" in t)
                               and not ("電池" in t or "Battery" in t or "%" not in t and "電池" in joined)]
            if _progress_texts and not _battery_ctx:
                logger.info(">>> ダウンロード進捗テキスト検出: %s", _progress_texts[:3])
                # has_any 互換の形式で返す
                dl = {"text": _progress_texts[0], "center": (0, 0), "confidence": 0.5, "box": []}
    if dl:
        logger.info(">>> ロード/ダウンロード中: '%s' — 待機 (Watchdog免除)", dl["text"])
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ─── クエストマップ/ステージ選択 ───
    stage_num = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                               "3-1", "3-2", "4-1", "4-2", "Main"])
    sentu_btn = None
    for _skw in ["戦闘", "出撃", "挑戦"]:
        _sb = has_text(ocr, _skw)
        if _sb and _sb["center"][1] > H * 0.5:
            sentu_btn = _sb
            break
    if not sentu_btn:
        expl = has_text(ocr, "探索")
        if expl and expl["center"][1] > H * 0.6:
            sentu_btn = expl
    if stage_num and sentu_btn:
        cx, cy = sentu_btn["center"]
        logger.info(">>> クエストマップ — 「%s」(%d,%d)", sentu_btn["text"], cx, cy)
        tap_device(cx, cy, state, f"QUEST_START {sentu_btn['text']}")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0
    elif stage_num:
        fx, fy = roi_to_device(int(W * 0.74), int(H * 0.91), state.game_roi)
        logger.info(">>> クエストマップ(固定) (%d,%d)", fx, fy)
        tap_device(fx, fy, state, "QUEST_START_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0

    # ─── バトル画面 ───
    # 「AUTO」「HP」「戦闘」はストーリー画面にも出るため除外、戦闘固有キーワードで判定
    battle_keywords = ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "隣接攻撃", "必殺技", "巫殺技",  # チュートリアルバトルキーワード追加
                       "BREAK", "Turn", "WAVE"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1

        # バトルチュートリアル: バフ効果
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx, by = roi_to_device(int(W * 0.888), int(H * 0.667), state.game_roi)
            logger.info(">>> バフチュートリアル (%d,%d)", bx, by)
            tap_device(bx, by, state, "BUFF_TUTORIAL")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: スキル使用
        skill_tut = has_any(ocr, ["スキルを使ってみましょう", "スキを使ってみ",
                                   "戦闘スキルを使", "戦闘スキを使",
                                   "スキルを使用してみ", "使ってみましょう"])
        if skill_tut:
            sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
            logger.info(">>> スキルチュートリアル (%d,%d)", sx, sy)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL", post_wait=0.8)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技"])
        if hissatsu_tut:
            hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL", post_wait=0.8)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 攻撃対象変更
        if has_any(ocr, ["攻撃対象を変更", "対象を変更"]):
            ex, ey = roi_to_device(int(W * 0.651), int(H * 0.361), state.game_roi)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 1.0

        # バトルチュートリアル: 一般ポップアップ
        tutorial_popup = has_any(ocr, [
            "タイムライン", "表示されている", "行動してい",
            "ここをタップ", "タップしてください",
            "ことができます", "することができ",
            "スキルを使用", "スキルを選択", "カードを選択",
            "ましょう", "みましょう", "てみよう",
            "一番上に", "順番に行動", "DEFENDER",
            # ロール説明ポップアップ
            "ロールについて", "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "HEALER",
            # バトル説明ポップアップ
            "STEP1", "STEP2", "バトルシステム", "ブレイクし",
        ])
        # バトル速度ツールチップは速度ボタン本体 (1409,19) をタップして消す
        speed_tip = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if speed_tip:
            _sp_x, _sp_y = roi_to_device(int(W * 0.927), int(H * 0.026), state.game_roi)
            logger.info(">>> 速度ツールチップ → 速度ボタン (%d,%d) タップ", _sp_x, _sp_y)
            tap_device(_sp_x, _sp_y, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 1.0
        if tutorial_popup:
            # ── テンプレートマッチングで ▷/× を優先検出 ──
            _btl_nav = detect_tutorial_dialog_nav(analysis_path, W, H) if analysis_path else None
            if _btl_nav:
                _btn, _bx, _by = _btl_nav
                logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) [template]",
                            tutorial_popup["text"][:10], "×" if _btn == "close" else "▷", _bx, _by)
                tap_device(_bx, _by, state, "BATTLE_TUTORIAL_POPUP")
                return "BATTLE_TUTORIAL", 1.0
            # フォールバック: ▷ 矢印 → × ボタンのシーケンス
            state.pre_popup_tap_count += 1
            _arr_b = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
            _cls_b = roi_to_device(int(W * 0.98), int(H * 0.056), state.game_roi)
            _btl_candidates = [_arr_b, _arr_b, _arr_b, _arr_b, _cls_b, _cls_b]
            _bidx = min(state.pre_popup_tap_count - 1, len(_btl_candidates) - 1)
            cx, cy = _btl_candidates[_bidx]
            _blabel = "×" if (cx, cy) == _cls_b else "▷"
            logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) (試行%d回目)",
                        tutorial_popup["text"][:10], _blabel, cx, cy, state.pre_popup_tap_count)
            tap_device(cx, cy, state, "BATTLE_TUTORIAL_POPUP")
            return "BATTLE_TUTORIAL", 1.0

        # AUTO ボタン
        if not state.auto_activated:
            ax, ay = roi_to_device(int(W * 0.845), int(H * 0.090), state.game_roi)
            logger.info(">>> AUTO タップ (%d,%d)", ax, ay)
            tap_device(ax, ay, state, "AUTO_ON")
            state.auto_activated = True
            return "BATTLE_AUTO", BATTLE_WAIT

        # バトル停滞時: ハイライト候補を順番にタップ試行
        if state.battle_wait_count > 8:
            stall_phase = (state.battle_wait_count - 8) % 12
            if stall_phase == 0:
                sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
                logger.info(">>> バトル停滞 — スキルタップ (%d,%d)", sx, sy)
                tap_device(sx, sy, state, "STALL_SKILL", post_wait=0.8)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 1.0
            elif stall_phase == 4:
                hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU", post_wait=0.8)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 1.0
            elif stall_phase == 8:
                # 探索バトル: 左パネルのキャラカードを再タップ (char_just_selected リセット)
                state.char_just_selected = False
                lx, ly = roi_to_device(int(W * 0.141), int(H * 0.875), state.game_roi)
                logger.info(">>> バトル停滞 — 左カード再タップ (%d,%d)", lx, ly)
                tap_device(lx, ly, state, "STALL_LEFT_CARD")
                return "BATTLE_STALL", 1.0

        # 高回数停滞: auto_activated リセットで再検出
        if state.battle_wait_count > 30:
            logger.warning(">>> バトル長期停滞 (count=%d) — auto_activated/char_justSelected リセット",
                           state.battle_wait_count)
            state.auto_activated = False
            state.char_just_selected = False
            state.battle_wait_count = 0

        logger.info(">>> バトル中 — 待機 (count=%d, auto=%s)",
                    state.battle_wait_count, state.auto_activated)
        return "BATTLE_WAIT", BATTLE_WAIT

    # バトル終了検出
    if state.battle_wait_count > 0:
        logger.info(">>> バトル終了検出 (wait_count was %d)", state.battle_wait_count)
        state.battle_wait_count = 0
        state.auto_activated = False

    # ─── バトル結果/リザルト ───
    result_match = has_any(ocr, ["リザルト", "Result", "RESULT", "勝利", "Victory",
                                  "クリア", "CLEAR", "EXP", "経験値", "ランクアップ"])
    if result_match:
        # まず「次へ」ボタンを優先タップ (右下エリアのみ: y>H*0.6 & x>W*0.5)
        _nxt_r = None
        for _ocr_item_r in ocr:
            _txt_r = _ocr_item_r.get("text", "")
            if "次へ" in _txt_r or "NEXT" in _txt_r:
                _rx_c, _ry_c = _ocr_item_r["center"]
                if _ry_c > H * 0.6 and _rx_c > W * 0.5:
                    _nxt_r = _ocr_item_r
                    break
        if _nxt_r:
            _rx, _ry = _nxt_r["center"]
            logger.info(">>> バトル結果【次へ優先】 (%d,%d) タップ", _rx, _ry)
            tap_device(_rx, _ry, state, "RESULT_NEXT")
            return "RESULT_TAP", 1.0
        cx, cy = result_match["center"]
        text = result_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> バトル結果 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, "RESULT_TAP")
        return "RESULT_TAP", 1.0

    # ─── スキップ ───
    # OCRがSKIPを'SK'と短縮検出するケースも考慮: 右上エリア(x>1000, y<100)に限定
    skip_match = has_any(ocr, ["スキップ", "SKIP", "Skip"])
    if not skip_match:
        _sk_match = has_any(ocr, ["SK", "Sk"])
        if _sk_match:
            _sk_cx, _sk_cy = _sk_match["center"]
            if _sk_cx > 1000 and _sk_cy < 100:
                skip_match = _sk_match
    if skip_match:
        cx, cy = skip_match["center"]
        text = skip_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> スキップ '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"SKIP '{text}'")
        return "SKIP", 0.5

    # ─── 閉じるボタン ───
    close_match = has_any(ocr, ["閉じる", "Close", "CLOSE", "とじる"])
    if close_match:
        cx, cy = close_match["center"]
        logger.info(">>> 閉じる '%s' (%d,%d)", close_match["text"], cx, cy)
        tap_device(cx, cy, state, f"CLOSE '{close_match['text']}'")
        return "CLOSE", 0.5

    # ─── ゲーム内システムダイアログ (画質設定・ダウンロード確認 等) ───
    # smart_tap_button で金色ボタン枠の幾何学的中心を取得 (OCR ずれを排除)
    sys_dlg_kws = ["画質を設定", "アセット更新", "ダウンロードを開始", "ダウンロードしますか",
                   "Wi-Fiを使用", "モバイル通信でダウンロード", "ダウンロードが完了しました"]
    sys_dlg_match = has_any(ocr, sys_dlg_kws, min_conf=0.3)
    if sys_dlg_match:
        ok_item = next((item for item in ocr if "OK" in item.get("text", "")), None)
        if ok_item and ok_item["center"][0] > W * 0.5:
            ocr_ok_x, ocr_ok_y = ok_item["center"]
        else:
            ocr_ok_x, ocr_ok_y = int(W * 0.65), int(H * 0.88)  # 比率ベースフォールバック
        ok_x, ok_y = smart_tap_button(analysis_path, ocr_ok_x, ocr_ok_y)
        logger.info(">>> 【システムダイアログ】 '%s' → SmartTap OK (%d,%d)",
                    sys_dlg_match["text"][:15], ok_x, ok_y)
        tap_device(ok_x, ok_y, state, "SYSTEM_DLG_OK")
        return "SYSTEM_DLG_OK", 2.0

    # ─── 利用規約同意ダイアログ ───
    # 「同意してゲームを始める」ボタンを右下の固定座標または OCR 座標でタップ
    tos_screen = has_any(ocr, ["同意してゲームを始める", "プライバシーポリシー"], min_conf=0.3)
    if tos_screen and has_text(ocr, "利用規約", min_conf=0.3):
        # "始める" または "ゲームを始める" を OCR で探して座標タップ
        agree_ocr = has_any(ocr, ["始める", "ゲームを始める", "同意してゲームを始める"], min_conf=0.3)
        if agree_ocr:
            cx, cy = agree_ocr["center"]
            logger.info(">>> 【利用規約同意】 '%s' (%d,%d) タップ", agree_ocr["text"][:10], cx, cy)
            tap_device(cx, cy, state, "AGREE_TOS")
        else:
            _tos_x, _tos_y = roi_to_device(int(W * 0.72), int(H * 0.89), state.game_roi)
            logger.info(">>> 【利用規約同意】 固定座標 (%d,%d) タップ", _tos_x, _tos_y)
            tap_device(_tos_x, _tos_y, state, "AGREE_TOS")
        return "AGREE_TOS", 3.0

    # ─── 規約同意 ───
    agree_match = has_any(ocr, ["同意", "規約", "利用規約"])
    if agree_match:
        logger.info(">>> 規約画面 — スクロール→同意")
        for _ in range(3):
            swipe(int(W * 0.46), int(H * 0.69), int(W * 0.46), int(H * 0.28), 500, state=state)
            time.sleep(0.8)
        agree_btn = has_any(ocr, ["同意"])
        if agree_btn:
            cx, cy = agree_btn["center"]
            tap_device(cx, cy, state, "AGREE")
        return "AGREE", 1.0

    # ─── 確認ダイアログ ───
    confirm_match = has_any(ocr, ["OK", "はい", "次へ", "確認", "完了", "決定",
                                   "受け取る", "受取", "了解", "わかった",
                                   "進む", "START", "開始",
                                   "TAP TO START", "TOUCH", "始める",
                                   "戦闘", "出撃", "クエスト開始", "バトル開始",
                                   # チュートリアルで案内されるボタン名
                                   "自動編成", "一括受取", "強化", "合成", "強化素材",
                                   "クエスト", "探索開始", "バトル"])
    if confirm_match:
        cx, cy = confirm_match["center"]
        text = confirm_match["text"]
        action_type, desc = STRATEGIC_ENGINE.log_prediction(text, cx, cy)
        state.last_prediction = action_type
        state.last_prediction_desc = desc
        state.last_tap_text = text
        state.last_action_pre_phash = state.last_phash
        logger.info(">>> 確認 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, f"CONFIRM '{text}'")
        return "CONFIRM", 1.0

    # ─── ストーリー/会話 (下部テキストボックス) ───
    lower_texts = [r for r in ocr if r["center"][1] > H * 0.6]
    if lower_texts and len(ocr) <= 15:
        target = lower_texts[-1]
        cx, cy = target["center"]
        logger.info(">>> ストーリー送り '%s' (%d,%d)", target["text"][:10], cx, cy)
        tap_device(cx, cy, state, "STORY_TAP")
        return "STORY_TAP", 0.3

    # ─── ログインボーナス等 ───
    bonus_match = has_any(ocr, ["ログイン", "ボーナス", "プレゼント", "獲得"])
    if bonus_match:
        cx, cy = bonus_match["center"]
        logger.info(">>> ポップアップ '%s' (%d,%d)", bonus_match["text"], cx, cy)
        tap_device(cx, cy, state, "POPUP_TAP")
        return "POPUP_TAP", 1.0

    # ─── フォールバック: 何も見つからない ───
    logger.info(">>> 不明な画面 — WAIT_FOR_CHANGE (OCR %d件)", len(ocr))
    return "WAIT_FOR_CHANGE", 0


# ─── Watchdog: 物理的 ADB 生存確認 ────────────────────────
def check_adb_liveness() -> bool:
    """
    ADB 接続の物理的な生存を確認する。
    - adb shell echo 1: 応答確認 (timeout 3s)
    - adb shell screencap: 転送サービスのハング確認 (timeout 3s)
    Returns: True=接続正常, False=タイムアウト/エラー(要再起動)
    """
    import subprocess as _sp
    _serial_arg = ["-s", DEVICE_SERIAL] if DEVICE_SERIAL else []
    try:
        # echo テスト
        _r1 = _sp.run(
            ["adb"] + _serial_arg + ["shell", "echo", "1"],
            capture_output=True, timeout=3, text=True,
        )
        if _r1.returncode != 0 or _r1.stdout.strip() != "1":
            logger.warning("[WATCHDOG] echo 応答異常: rc=%d out=%r", _r1.returncode, _r1.stdout.strip())
            return False
        # screencap パイプテスト (実際には読まない — ハングを検出するだけ)
        _r2 = _sp.run(
            ["adb"] + _serial_arg + ["shell", "screencap", "-p", "/dev/null"],
            capture_output=True, timeout=3,
        )
        if _r2.returncode != 0:
            logger.warning("[WATCHDOG] screencap ハング検出: rc=%d", _r2.returncode)
            return False
        return True
    except _sp.TimeoutExpired:
        logger.warning("[WATCHDOG] ADB コマンドタイムアウト — 物理診断失敗")
        return False
    except Exception as _e:
        logger.warning("[WATCHDOG] 物理診断例外: %s", _e)
        return False


# ─── Watchdog: デッドロック自動復旧 ─────────────────────
def watchdog_recover(state: PilotState) -> bool:
    """
    Unityメインスレッドのデッドロックを検出した際の自動復旧。

    戦略 (pm clear は一切使用しない — BAN リスク排除):
      1〜3回目: am force-stop → am start (ソフト再起動のみ)
      4回目以降: 諦めて False を返す (人間に委譲)

    Returns: True=復旧試行を実施, False=諦め(mainが終了する)
    """
    state.watchdog_recovery_count += 1
    count = state.watchdog_recovery_count
    elapsed = time.time() - state.last_screen_change_time

    # 3回を超えたら諦めて人間に報告
    if count > WATCHDOG_MAX_TOTAL_RECOVERIES:
        logger.error(
            "[WATCHDOG] 復旧試行%d回失敗 (last_action=%s, %.0f秒経過) — 人間の介入が必要です。停止します。",
            count - 1, state.last_action, elapsed
        )
        return False

    # ソフト再起動のみ (pm clear は使用しない)
    logger.warning(
        "[WATCHDOG] デッドロック判定: 画面変化なし %.0f秒 / last_action=%s / 復旧試行 #%d",
        elapsed, state.last_action, count
    )
    logger.warning("[WATCHDOG] → am force-stop → am start (ソフト再起動のみ。pm clearは使用しない)")
    adb(f"shell am force-stop {APP_PACKAGE}")
    time.sleep(3)

    # 再起動
    adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
    logger.info("[WATCHDOG] am start 実行 — 15秒待機 (初期化 + ご注意画面の出現を待つ)")
    time.sleep(15)  # 起動＋スプラッシュ待機

    # 状態リセット (デバイス解像度・回数は保持)
    state.last_phash = ""
    state.same_phash_count = 0
    state.stall_start = 0.0
    state.stall_corner_tried = False
    state.last_action = "WATCHDOG_RECOVERY"
    state.last_screen_change_time = time.time()
    return True


# ─── レポート生成 + クリップボードコピー ────────────────
def generate_and_copy_report(state: PilotState, reason: str) -> None:
    """
    ホーム到達またはエラー停止時に状況レポートを生成し pbcopy でコピー。
    Gemini へのペースト用。
    """
    # Git コミット情報
    try:
        commit_id = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True,
            cwd=str(_CRAWLER_ROOT.parent), timeout=5,
        ).stdout.strip()
    except Exception:
        commit_id = "unknown"

    last_ocr_preview = ", ".join(state.last_ocr_texts[:6]) if state.last_ocr_texts else "(なし)"
    report_lines = [
        "=" * 64,
        "# まどドラ自律操縦レポート (auto_pilot.py)",
        f"停止理由: {reason}",
        f"日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## 修正/確認したファイルと定数",
        "- `crawler/tools/auto_pilot.py`",
        f"  - WATCHDOG_DEADLOCK_THRESHOLD: {WATCHDOG_DEADLOCK_THRESHOLD} 秒",
        f"  - WATCHDOG_MAX_TOTAL_RECOVERIES: {WATCHDOG_MAX_TOTAL_RECOVERIES}",
        f"  - WATCHDOG_EXEMPT_ACTIONS: {', '.join(sorted(WATCHDOG_EXEMPT_ACTIONS))}",
        f"  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒",
        f"  - DOWNLOAD_WAIT: {DOWNLOAD_WAIT} 秒/ループ (無限忍耐・Watchdog免除)",
        f"  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB",
        "",
        "## 現在の画面状況",
        f"- 最終アクション : {state.last_action}",
        f"- 現在シーン    : {state.current_scene}",
        f"- 最終OCRテキスト: {last_ocr_preview}",
        f"- ホーム到達    : {state.home_reached}",
        "",
        "## 実行統計",
        f"- 総ループ数           : {state.iteration + 1}",
        f"- 総タップ数           : {state.total_taps}",
        f"- OCR実行回数          : {state.total_ocr_calls}",
        f"- OCRスキップ          : {state.total_ocr_skipped}",
        f"- 暗転スキップ         : {state.total_blackout_skipped}",
        f"- SIGSEGV回避回数      : {state.screenshot_retry_count}",
        f"- Watchdog復旧試行     : {state.watchdog_recovery_count}",
        f"- エビデンス保存数     : {state.screenshots_saved}",
        f"- 平均判定速度         : {state.total_loop_ms / max(state.iteration + 1, 1):.0f} ms/loop",
        "",
        "## 主要検知成功率",
        f"- Dialog検知           : {state.dialog_detections} 回",
        f"- Finger検知           : {state.finger_detections} 回",
        f"- GoldBtn検知          : {state.gold_detections} 回",
        "",
        "## 戦績サマリー",
        f"- ホーム到達           : {'✓ CLEARED' if state.home_reached else '未到達'}",
        f"- チュートリアル       : {'All Tutorials Cleared' if state.home_reached else '進行中'}",
        f"- 最終シーン           : {state.current_scene}",
        f"- Rank                 : {'1 / HOME REACHED' if state.home_reached else 'In Progress'}",
        "",
        "## 最新コミット",
        f"- commit: {commit_id}",
        f"- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/{commit_id}",
        "=" * 64,
    ]
    report = "\n".join(report_lines)

    try:
        storage_dir = _CRAWLER_ROOT / "storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        report_path = storage_dir / "auto_pilot_report.md"
        report_path.write_text(report, encoding="utf-8")
        # pbcopy でクリップボードにコピー
        subprocess.run(
            ["bash", "-c", f"cat '{report_path}' | pbcopy"],
            check=False, timeout=5,
        )
        logger.info("[REPORT] レポート保存: %s", report_path)
        logger.info("[REPORT] クリップボードにコピー完了 — Gemini にペーストしてください")
    except Exception as e:
        logger.error("[REPORT] コピー失敗: %s", e)

    print("\n" + report)
    print("\n>>> 報告をクリップボードにコピーしました。Geminiにペーストしてください。")


# ─── BATTLE 高速パス: OCR 前テンプレートマッチング ──────────────────
_BATTLE_UI_KWS = frozenset([
    "通常攻撃", "単体攻撃", "单体攻撃",  # 单=簡体字 OCR 誤認対応
    "WAVE", "Turn", "ターン", "必殺技",
])

def _battle_fast_check(analysis_path: Path,
                       state: "PilotState") -> tuple[str, float]:
    """
    BATTLE シーン専用 OCR 前高速判定。
    OpenCV のみで GoldSwipe / GoldBtn を検出し、見つかれば即タップして (action, wait) を返す。
    見つからなければ ("", 0.0) を返して通常 OCR フローへ移行する。
    """
    # 1. GoldSwipe (Type A) — チュートリアル移動シーン
    # 前回OCRでバトルUIキーワード(通常攻撃/WAVE/Turn等)を確認済みならSPゲージ誤検出の
    # 可能性が高いためスキップ
    _confirmed_battle_ui = any(kw in state.last_ocr_texts for kw in _BATTLE_UI_KWS)
    if _confirmed_battle_ui:
        logger.debug("[FAST] バトルUI確認済み → GoldSwipe スキップ (SPゲージ誤検出防止)")
    gs = None if _confirmed_battle_ui else detect_tutorial_gold_swipe(analysis_path)
    if gs:
        _dir, _sx, _fy, _ty, _dur = gs
        logger.info("[FAST] GoldSwipe %s → swipe (%d,%d)→(%d,%d) %dms",
                    _dir, _sx, _fy, _sx, _ty, _dur)
        swipe(_sx, _fy, _sx, _ty, _dur, state=state)
        return ("GOLD_SWIPE_UP" if _dir == "UP" else "GOLD_SWIPE_DOWN"), BATTLE_WAIT

    # 2. GoldBtn (Type B) — バトルチュートリアルボタン (右半分)
    # バトルUI確認済み (通常攻撃/WAVE等) の場合はフッター外の金色オブジェクトに
    # 誤反応しないよう GoldBtn もスキップし、Glow SM (フッター優先) に委ねる。
    if _confirmed_battle_ui:
        logger.debug("[FAST] バトルUI確認済み → GoldBtn スキップ (フッター発光SMへ委譲)")
    else:
        gb = detect_tutorial_gold_button_tap(analysis_path, right_half_only=True)
        if gb:
            gx, gy = gb
            logger.info("[FAST] GoldBtn → tap(%d,%d)", gx, gy)
            tap_device(gx, gy, state, "GOLD_BTN_TAP")
            return "GOLD_BTN_TAP", BATTLE_WAIT

    return "", 0.0


# ─── コマンドライン引数 ───────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="まどドラ自律操縦")
    parser.add_argument("--verbose", action="store_true", help="デバッグログ出力")
    return parser.parse_args()


# ─── メインループ ─────────────────────────────────
def main():
    args = parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (ハイブリッド版)")
    logger.info("  デバイス: %s", DEVICE_SERIAL)
    logger.info("  ポーリング: %.1fs  強制解析: %d回変化なし  スタックTimeout: %.0fs",
                POLL_INTERVAL, FORCE_ANALYZE_AFTER, STALL_TIMEOUT)
    logger.info("=" * 62)

    state = PilotState()
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # ─── Ctrl+C シグナルハンドラ登録 (レポート自動生成) ───
    global _pilot_state_ref
    _pilot_state_ref = state

    def _sigint_handler(signum, frame):
        logger.info("\n[Ctrl+C] 手動停止 — レポートを生成します...")
        generate_and_copy_report(_pilot_state_ref, "手動停止 (Ctrl+C / SIGINT)")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    # ─── scrcpy 管理: 不整合 Kill → 規定オプション起動 ───
    _scrcpy_proc = manage_scrcpy()

    # 起動直後に実機物理解像度を取得してログ出力 (ループ内の初回 take_screenshot より早期)
    _dev_w, _dev_h = get_device_resolution()
    logger.info("[DEVICE_RES] 物理解像度: %dx%d / 解析基準: %dx%d / ratio_x=%.3f ratio_y=%.3f",
                _dev_w, _dev_h, ANALYSIS_W, ANALYSIS_H,
                _dev_w / ANALYSIS_W, _dev_h / ANALYSIS_H)

    logger.info("[TOKEN_SAVE] 節約モード稼働中。バトル発光検知で OCR スキップ → 爆速モードで進行します")

    for i in range(MAX_ITERATIONS):
        state.iteration = i
        _loop_t0 = time.time()  # [PERF] ループ開始時刻

        # ── 定期健診 (100 iter ごと) ──
        if i > 0 and i % 100 == 0:
            logger.info("[WATCHDOG] Periodic check (iter=%d). Running physical diagnostics...", i)
            if not check_adb_liveness():
                logger.warning("[WATCHDOG] Periodic check FAILED → attempting reconnect")
                import subprocess as _sp_pc
                _sp_pc.run(["adb", "kill-server"], timeout=5)
                time.sleep(2)
                _sp_pc.run(["adb", "start-server"], timeout=5)
                time.sleep(2)
                if DEVICE_SERIAL:
                    _sp_pc.run(["adb", "connect", DEVICE_SERIAL], timeout=5)
                    time.sleep(1)
            else:
                logger.info("[WATCHDOG] Periodic check OK")

        # ── 1) スクリーンショット取得 ──
        img_path, actual_w, actual_h, _ss_retries = take_screenshot()
        state.screenshot_retry_count += _ss_retries
        if _ss_retries > 0:
            logger.info("[SCREENSHOT] 破損リトライ %d回 (累計: %d回)",
                        _ss_retries, state.screenshot_retry_count)
        # ── Wi-Fi 破損防御: 全リトライ失敗 → continue で次ループ ──
        if img_path is None:
            state._wifi_fail_streak = getattr(state, '_wifi_fail_streak', 0) + 1
            logger.warning("[WIFI_ERROR] 連続失敗 %d/5 — 次ループで再取得",
                           state._wifi_fail_streak)
            if state._wifi_fail_streak >= 5:
                logger.error("[WIFI_ERROR] 連続5回失敗 → ADB再接続を試行")
                try:
                    subprocess.run(["adb", "disconnect"], timeout=5, capture_output=True)
                    time.sleep(1)
                    subprocess.run(["adb", "connect", DEVICE_SERIAL], timeout=5, capture_output=True)
                    time.sleep(2)
                except Exception as _rc_e:
                    logger.error("[WIFI_ERROR] ADB再接続例外: %s", _rc_e)
                state._wifi_fail_streak = 0
            time.sleep(1.0)
            continue
        state._wifi_fail_streak = 0  # 成功時リセット
        # メモリ上に最新画像を保持 + ROI更新
        try:
            import cv2 as _cv2_main
            state.last_screen = _cv2_main.imread(str(img_path))
            if state.last_screen is not None:
                _new_roi = detect_game_roi(state.last_screen)
                # 非黒画面のときのみ ROI を更新 (暗転中は前の ROI を維持)
                if _new_roi[2] >= ANALYSIS_W * 0.5:
                    if _new_roi != state.game_roi:
                        logger.info("[ROI] ゲーム描画領域更新: x=%d y=%d w=%d h=%d (黒帯: L=%d R=%d T=%d B=%d)",
                                    _new_roi[0], _new_roi[1], _new_roi[2], _new_roi[3],
                                    _new_roi[0], ANALYSIS_W - _new_roi[0] - _new_roi[2],
                                    _new_roi[1], ANALYSIS_H - _new_roi[1] - _new_roi[3])
                        state.game_roi = _new_roi
        except Exception:
            state.last_screen = None

        if not state.device_w:
            state.device_w = actual_w
            state.device_h = actual_h
            logger.info("実機解像度: %dx%d (解析基準: %dx%d)",
                        actual_w, actual_h, ANALYSIS_W, ANALYSIS_H)
        elif (actual_w, actual_h) != (state.device_w, state.device_h):
            # portrait→landscape 遷移など解像度変化を追跡
            logger.info("解像度変化: %dx%d → %dx%d", state.device_w, state.device_h, actual_w, actual_h)
            state.device_w = actual_w
            state.device_h = actual_h

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            state.total_blackout_skipped += 1
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転 — 3s 待機", i)
            state.last_phash = ""
            state.same_phash_count = 0
            time.sleep(2.0)
            continue

        # ── 3) phash 粗解析 ──
        try:
            cur_phash = compute_phash(img_path)
        except Exception:
            cur_phash = ""

        if state.last_phash and cur_phash:
            dist = phash_distance(state.last_phash, cur_phash)
        else:
            dist = 999

        # ── 前回タップの予測を検証 (phash変化で判定) ──
        if state.last_action_pre_phash and state.last_prediction and cur_phash:
            STRATEGIC_ENGINE.verify_and_learn(
                state.last_action_pre_phash, cur_phash,
                state.last_prediction, state.last_prediction_desc,
                state.last_tap_text,
            )
            state.last_action_pre_phash = ""
            state.last_prediction = ""

        screen_changed = dist >= PHASH_THRESHOLD

        # ── 動的しきい値: Gold UI アクション後はアニメーション変化でも即解析 ──
        # GoldBtn/MOYA_TAP 後に微小な phash 変化(アニメーション)があっても
        # OCR をスキップせず即時解析して次のアクションを実行する。
        _GOLD_UI_ACTIONS = frozenset([
            "GOLD_BTN_TAP", "MOYA_TAP", "BATTLE_TUTORIAL", "SKILL_CARD_TUTORIAL",
            "HISSATSU_TUTORIAL", "BUFF_TUTORIAL", "GOLD_SWIPE_UP", "GOLD_SWIPE_DOWN",
        ])
        if not screen_changed and state.last_action in _GOLD_UI_ACTIONS and dist >= 1:
            screen_changed = True
            state.same_phash_count = 0
            logger.debug("  [DYN_PHASH] %s 後 dist=%d → 即解析", state.last_action, dist)

        # ── AUTO バトル中: dist>=1 でもウォッチドッグタイマーをリセット ──
        # バトルアニメーションは phash_dist=1-4 の微小変化が続くため、
        # PHASH_THRESHOLD=5 を超えなくてもバトルは進行中なのでウォッチドッグを抑制。
        if (not screen_changed and dist >= 1
                and state.last_action in ("BATTLE_WAIT", "BATTLE_AUTO", "BATTLE_STALL")
                and state.auto_activated):
            state.last_screen_change_time = time.time()
            state.stall_start = 0.0

        if screen_changed:
            # 画面変化あり → カウンタリセット & Watchdog タイマーリセット
            state.same_phash_count = 0
            state.consecutive_frozen_frames = 0
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.pre_popup_tap_count = 0  # ポップアップ試行カウンタもリセット
            state.dialog_close_total = 0  # ダイアログclose累計もリセット
            state.last_screen_change_time = time.time()  # Watchdog: 最終変化時刻更新

            # ── ADV 高速モード: OCR スキップして画面下部を即連打 ──
            # 前回 STORY_TAP かつ phash 変化が小さい（テキスト送り）→ 即タップ
            # MENU シーンはホームチュートリアル中の可能性があるため除外（指/金枠をOCRで確認）
            if (state.last_action in ("STORY_TAP", "ADV_RAPID_TAP", "STORY_TAP_HINT") and
                    PHASH_THRESHOLD <= dist <= ADV_RAPID_PHASH_MAX and
                    state.current_scene != "MENU"):
                logger.info("[iter %d] phash_dist=%d ADV_RAPID → 即タップ (OCR skip)", i, dist)
                _adv_x, _adv_y = roi_to_device(int(W * 0.5), int(H * 0.9), state.game_roi)
                tap_device(_adv_x, _adv_y, state, "ADV_RAPID_TAP")
                logger.info("  ACTION_TAKEN ADV_RAPID_TAP (%d,%d)", _adv_x, _adv_y)
                state.last_phash = cur_phash
                continue

        else:
            # 画面変化なし
            state.same_phash_count += 1
            state.total_ocr_skipped += 1
            # 完全凍結 (dist=0) のみカウント
            if dist == 0:
                state.consecutive_frozen_frames += 1
            else:
                state.consecutive_frozen_frames = 0  # わずかな変化でもリセット

            # ── 階層型 Watchdog 第2段階: 5フレーム凍結 → 物理診断 ──
            if state.consecutive_frozen_frames == 5:
                logger.info(
                    "[WATCHDOG] Suspect freeze (%d consecutive dist=0). "
                    "Running physical diagnostics...",
                    state.consecutive_frozen_frames,
                )
                if not check_adb_liveness():
                    # 第3段階: 物理診断失敗 → kill-server + 再接続
                    logger.warning("[WATCHDOG] Physical diagnostics FAILED → adb kill-server + reconnect")
                    import subprocess as _sp_wd
                    _sp_wd.run(["adb", "kill-server"], timeout=5)
                    time.sleep(2)
                    _sp_wd.run(["adb", "start-server"], timeout=5)
                    time.sleep(2)
                    if DEVICE_SERIAL:
                        _sp_wd.run(["adb", "connect", DEVICE_SERIAL], timeout=5)
                        time.sleep(1)
                    state.consecutive_frozen_frames = 0
                    state.last_phash = ""  # 次ループで強制再取得
                else:
                    logger.info("[WATCHDOG] Physical diagnostics OK — game screen is static")

            # ── Watchdog チェック: 10分以上変化なし → 本当のデッドロック ──
            watchdog_elapsed = time.time() - state.last_screen_change_time
            if watchdog_elapsed >= WATCHDOG_DEADLOCK_THRESHOLD:
                if state.last_action in WATCHDOG_EXEMPT_ACTIONS:
                    # 免除シーン: まだ待機中なのでWatchdogを発動しない
                    if int(watchdog_elapsed) % 60 == 0:  # 1分ごとにログ
                        logger.info(
                            "[WATCHDOG] 免除: %.0f秒経過 (last_action=%s) — 引き続き待機",
                            watchdog_elapsed, state.last_action
                        )
                else:
                    logger.warning(
                        "[WATCHDOG] デッドロック疑い: %.0f秒間画面変化なし / last_action=%s "
                        "/ iter=%d → 自動復旧開始",
                        watchdog_elapsed, state.last_action, state.iteration
                    )
                    save_evidence(img_path, [], "WATCHDOG_DEADLOCK", state)
                    if not watchdog_recover(state):
                        generate_and_copy_report(
                            state, f"Watchdog復旧不能 (elapsed={watchdog_elapsed:.0f}秒, count={state.watchdog_recovery_count})"
                        )
                        return  # 復旧不能 → 終了
                    continue   # 復旧後は次のイテレーションから

            # N 回変化なし → 強制 OCR (デッドロック防止の核心)
            if state.same_phash_count >= FORCE_ANALYZE_AFTER:
                logger.info("[iter %d] phash_dist=%d same=%d → 強制 OCR",
                            i, dist, state.same_phash_count)
                screen_changed = True  # OCR ブロックへ進む

            else:
                # まだ待機フェーズ — シーン別インターバル
                _poll = SCENE_INTERVAL.get(state.current_scene, POLL_INTERVAL)
                if i % 3 == 0:
                    logger.info("[%s][iter %d] phash_dist=%d same=%d — polling (%.1fs)...",
                                state.current_scene, i, dist, state.same_phash_count, _poll)
                # ── ADV送り待ちアイコン検知: phash 安定中でも即タップ ──
                if state.current_scene in ("STORY", "ADV"):
                    if detect_adv_advance_icon(img_path):
                        logger.info("[ADV_ADVANCE][iter %d] 送り待ちアイコン検出 → 即タップ", i)
                        _aa_x, _aa_y = roi_to_device(int(W * 0.5), int(H * 0.9), state.game_roi)
                        adb(f"shell input tap {_aa_x} {_aa_y}")
                        state.total_taps += 1
                        state.last_phash = ""
                        state.same_phash_count = 0
                        state.stall_start = 0.0
                        time.sleep(0.5)
                        continue
                state.last_phash = cur_phash
                time.sleep(_poll)
                continue

            # ── スタック介入 (強制OCRでもタップできず続いた場合) ──
            if state.stall_start == 0.0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                logger.warning(">>> %.0f秒スタック — 右上×ボタン試行", stall_elapsed)
                save_evidence(img_path, [], "STALL_CORNER", state)
                time.sleep(0.3)
                _sc_x, _sc_y = roi_to_device(int(W * 0.97), int(H * 0.06), state.game_roi)
                adb(f"shell input tap {_sc_x} {_sc_y}")
                state.total_taps += 1
                state.stall_corner_tried = True
                state.last_phash = ""
                state.same_phash_count = 0
                time.sleep(1)
                continue

            if stall_elapsed >= STALL_TIMEOUT * 2 and state.stall_corner_tried:
                _restart_count = getattr(state, '_unity_restart_count', 0)
                if _restart_count >= 3:
                    logger.error(">>> %.0f秒スタック解消不能 (再起動%d回失敗) — 停止",
                                 stall_elapsed, _restart_count)
                    save_evidence(img_path, [], "STALL_FATAL", state)
                    generate_and_copy_report(state, f"スタック解消不能 (restart={_restart_count})")
                    return
                # ── Unity 入力フリーズ自動復帰 ──
                logger.warning(">>> %.0f秒スタック — Unity入力フリーズ疑い → ゲーム再起動 (試行%d)",
                               stall_elapsed, _restart_count + 1)
                save_evidence(img_path, [], "UNITY_FREEZE_RESTART", state)
                try:
                    import subprocess as _sp_uf
                    _sp_uf.run(["adb", "-s", DEVICE_SERIAL, "shell", "am", "force-stop",
                                "com.aniplex.magia.exedra.jp"], timeout=5)
                    time.sleep(3)
                    _sp_uf.run(["adb", "-s", DEVICE_SERIAL, "shell", "am", "start", "-n",
                                "com.aniplex.magia.exedra.jp/com.google.firebase.MessagingUnityPlayerActivity"],
                               timeout=5)
                    logger.info("[UNITY_RESTART] ゲーム再起動完了 — 30秒待機")
                    time.sleep(30)
                except Exception as _uf_e:
                    logger.error("[UNITY_RESTART] 再起動失敗: %s", _uf_e)
                state._unity_restart_count = _restart_count + 1
                state.stall_start = 0.0
                state.stall_corner_tried = False
                state.same_phash_count = 0
                state.last_phash = ""
                continue

        # ── 4) 解析用画像の準備 ──
        state.last_phash = cur_phash
        analysis_path = prepare_analysis_image(img_path, actual_w, actual_h)

        # ── 4.2) RESULT_RAPID: リザルト/報酬画面で GLOW 検知 → 0.2s 連打で突破 ──
        # 安全弁: phash 大変化 (dist>30) = シーン遷移 → OCR で再評価
        _result_rapid_ok = (
            state.last_action in ("RESULT_TAP", "RESULT_NEXT", "RESULT_RAPID")
            and analysis_path is not None
            and dist <= 30
        )
        if _result_rapid_ok:
            _result_glows = detect_guide_glow(analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.10)
            if _result_glows:
                _rg = max(_result_glows, key=lambda g: g["area"])
                _rgx, _rgy = _rg["cx"], _rg["cy"]
                logger.info("[RESULT_RAPID] glow(%d,%d) → 即タップ", _rgx, _rgy)
                tap_device(_rgx, _rgy, state, "RESULT_RAPID")
            else:
                _rc_x, _rc_y = roi_to_device(int(W * 0.5), int(H * 0.5), state.game_roi)
                logger.info("[RESULT_RAPID] no glow → center tap (%d,%d)", _rc_x, _rc_y)
                tap_device(_rc_x, _rc_y, state, "RESULT_RAPID")
            state.last_action = "RESULT_RAPID"
            state.stall_start = 0.0
            state.same_phash_count = 0
            time.sleep(1.0)
            _fms = (time.time() - _loop_t0) * 1000
            state.total_loop_ms += _fms
            logger.info("  [PERF] Loop %.0fms (RESULT_RAPID)", _fms)
            continue

        # ── 4.3) BATTLE_RAPID: 発光/MOYA 検知即タップ → OCR 完全スキップ ──
        # detect_guide_glow() + find_finger_blobs() は OpenCV のみ (10-50ms)
        # OCR (6-8s) の 40-50 倍高速
        # ※ 強制 OCR (phash 静止 → ダイアログ可能性) 時は RAPID をスキップして OCR に回す
        _force_ocr_override = (dist <= 2 and state.same_phash_count >= FORCE_ANALYZE_AFTER)
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override):
            _rapid_tx = _rapid_ty = 0
            _rapid_action = ""
            _rapid_double = False

            # ── Phase A: GLOW 検知 (HSV 発光) ──
            _rapid_glows = detect_guide_glow(analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
            # 左パネル (x<150) のアイコン発光を除外 — 実キャラ位置は x≈200-500
            _rapid_left_g = [g for g in _rapid_glows if g["side"] == "left" and g["cx"] >= 150]
            _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]

            if not state.character_selected and _rapid_left_g:
                _rl = max(_rapid_left_g, key=lambda g: g["area"])
                _rapid_tx, _rapid_ty = _rl["cx"], max(1, _rl["cy"] - 35)
                _rapid_action = "BATTLE_RAPID_GLOW_P1"
                _rapid_double = True
            elif state.character_selected and _rapid_right_g:
                _rr = max(_rapid_right_g, key=lambda g: g["area"])
                _rapid_tx, _rapid_ty = _rr["cx"], max(1, _rr["cy"] - 35)
                _rapid_action = "BATTLE_RAPID_GLOW_P2"

            # ── Phase B: MOYA 検知 (肌色ブロブ) — GLOW 未検出時のフォールバック ──
            if not _rapid_action:
                _rapid_blobs = find_finger_blobs(analysis_path, min_area=200, dark_mode=True)
                # 画面端の誤検出を除去
                _rapid_blobs = [b for b in _rapid_blobs
                                if b[1] > 36 and b[0] < ANALYSIS_W - 40]
                _H = ANALYSIS_H
                _left_char = [b for b in _rapid_blobs if b[0] < 600 and b[1] > _H * 0.76]
                _right_panel = [b for b in _rapid_blobs if b[0] > 1050 and b[1] > _H * 0.45]
                _bottom_ui = [b for b in _rapid_blobs if b[1] > _H * 0.8 and b[0] >= 600]

                if state.char_just_selected:
                    # キャラ選択済み → 右スキル (x>1050) 優先
                    if _right_panel:
                        _tb = max(_right_panel, key=lambda b: b[2])
                        _rapid_tx, _rapid_ty = _tb[0], max(1, _tb[1] - 35)
                        _rapid_action = "BATTLE_RAPID_MOYA_P2"
                    else:
                        # 通常攻撃ボタン: 比率ベース (W*0.90, H*0.88) + ROI 補正
                        _rapid_tx, _rapid_ty = roi_to_device(int(W * 0.90), int(H * 0.88), state.game_roi)
                        _rapid_action = "BATTLE_RAPID_NORMATK_P2"
                elif _left_char:
                    _tb = max(_left_char, key=lambda b: b[2])
                    _rapid_tx, _rapid_ty = _tb[0], max(1, _tb[1] - 35)
                    _rapid_action = "BATTLE_RAPID_MOYA_P1"
                    _rapid_double = True
                elif _right_panel:
                    _tb = max(_right_panel, key=lambda b: b[2])
                    _rapid_tx, _rapid_ty = _tb[0], max(1, _tb[1] - 35)
                    _rapid_action = "BATTLE_RAPID_MOYA_P2"

            # ── 共通タップ実行 ──
            if _rapid_action:
                logger.info("[%s] tap(%d,%d)%s",
                            _rapid_action, _rapid_tx, _rapid_ty,
                            " ダブルタップ" if _rapid_double else "")
                tap_device(_rapid_tx, _rapid_ty, state, _rapid_action,
                          post_wait=0.5 if _rapid_double else 1.0)
                if _rapid_double:
                    tap_device(_rapid_tx, _rapid_ty, state, _rapid_action)
                # 状態更新
                if "P1" in _rapid_action:
                    state.character_selected = True
                    state.char_just_selected = True
                else:
                    state.character_selected = False
                    state.char_just_selected = False
                state.finger_detections += 1
                state.last_action = _rapid_action
                state.stall_start = 0.0
                state.stall_corner_tried = False
                state.same_phash_count = 0
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_RAPID)", _fms)
                continue  # OCR スキップ

        # ── 4.5) BATTLE 高速パス: OCR 前テンプレートマッチング ──
        # BATTLE シーンで GoldBtn/GoldSwipe が見つかれば OCR (6-8s) をスキップ
        # ※ 強制 OCR 時はスキップ (ダイアログ検出を優先)
        if state.current_scene == "BATTLE" and not _force_ocr_override:
            _fast_action, _fast_wait = _battle_fast_check(analysis_path, state)
            if _fast_action:
                # GoldSwipe 連続回数制限: 6回超えたら OCR へフォールバック
                if "GOLD_SWIPE" in _fast_action:
                    state.gold_swipe_count += 1
                    if state.gold_swipe_count > 6:
                        logger.warning(
                            "[GoldSwipe] 連続 %d 回 → OCR フォールバック (ループ脱出)",
                            state.gold_swipe_count,
                        )
                        state.gold_swipe_count = 0
                        # FAST_PATH をスキップして通常 OCR へ
                    else:
                        state.last_action = _fast_action
                        state.stall_start = 0.0
                        state.stall_corner_tried = False
                        state.same_phash_count = 0
                        if _fast_wait > 0:
                            logger.info("  [FAST][%s] wait %.1fs (OCR skip)", _fast_action, _fast_wait)
                            time.sleep(_fast_wait)
                        _fms = (time.time() - _loop_t0) * 1000
                        state.total_loop_ms += _fms
                        logger.info("  [PERF] Loop %.0fms (FAST_PATH)", _fms)
                        continue  # OCR スキップ
                else:
                    state.gold_swipe_count = 0  # GoldSwipe 以外でカウンタリセット
                    state.last_action = _fast_action
                    state.stall_start = 0.0
                    state.stall_corner_tried = False
                    state.same_phash_count = 0
                    if _fast_wait > 0:
                        logger.info("  [FAST][%s] wait %.1fs (OCR skip)", _fast_action, _fast_wait)
                        time.sleep(_fast_wait)
                    _fms = (time.time() - _loop_t0) * 1000
                    state.total_loop_ms += _fms
                    logger.info("  [PERF] Loop %.0fms (FAST_PATH)", _fms)
                    continue  # OCR スキップ

        # ── 5) OCR 精査 ──
        state.total_ocr_calls += 1
        try:
            ocr_results = run_ocr(str(analysis_path), lang=OCR_LANG,
                                  min_confidence=OCR_MIN_CONF)
        except Exception as e:
            logger.error("OCR failed: %s", e)
            state.last_phash = ""  # 次回も確実に解析
            time.sleep(1)
            continue

        texts = all_texts(ocr_results)
        # ── シーン分類 ──
        scene, next_interval = classify_scene(texts, state.last_action)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── UIアフォーダンス解析 (UNKNOWN or 30OCRごと) ──
        if scene == "UNKNOWN" or state.total_ocr_calls % 30 == 0:
            STRATEGIC_ENGINE.report_screen_affordances(analysis_path, ocr_results)

        # ── 6) 判定 & アクション (finger blob も渡す) ──
        action, wait_sec = detect_and_act(ocr_results, state, analysis_path)
        state.last_action = action

        # タップ成功時: スタックカウンタリセット
        if action not in ("WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT"):
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.same_phash_count = 0

        # ── ダウンロード進捗ログ (30秒ごとに生存確認) ──
        if action == "DOWNLOAD_WAIT":
            _now = time.time()
            if _now - state.last_download_progress_log >= 30.0:
                _prog = [t for t in texts if "%" in t or "MB" in t or "GB" in t]
                logger.info(
                    "[DOWNLOAD_PROGRESS] 進捗数値: %s | OCR全体: %s",
                    _prog if _prog else "(数値なし)", texts[:6],
                )
                state.last_download_progress_log = _now

        # エビデンス保存
        if i % 20 == 0 or action in ("HOME_REACHED", "SKIP", "AGREE", "RESULT_TAP"):
            save_evidence(img_path, ocr_results, action, state)

        # ── 7) ホーム到達チェック ──
        # "HOME_REACHED" が返った時のみ停止 (QUEST_FROM_HOME 等の遷移中は続行)
        if action == "HOME_REACHED":
            logger.info("=" * 62)
            logger.info("  ホーム画面に到達しました! (チュートリアル完了)")
            logger.info("  総タップ: %d  イテレーション: %d", state.total_taps, i + 1)
            logger.info("  OCR実行: %d  スキップ: %d  暗転: %d",
                        state.total_ocr_calls, state.total_ocr_skipped,
                        state.total_blackout_skipped)
            logger.info("=" * 62)
            save_evidence(img_path, ocr_results, "FINAL_HOME", state)
            if _scrcpy_proc and _scrcpy_proc.poll() is None:
                _scrcpy_proc.terminate()
                logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)
            generate_and_copy_report(state, "ホーム画面到達 (チュートリアル完了)")
            return

        # ── 8) 待機 ──
        if wait_sec > 0:
            logger.info("  [%s][%s] wait %.1fs | next_check: %.1fs",
                        scene, action, wait_sec, next_interval)
            time.sleep(wait_sec)

        _loop_elapsed_ms = (time.time() - _loop_t0) * 1000
        state.total_loop_ms += _loop_elapsed_ms
        logger.info("  [PERF] Loop %.0fms (OCR)", _loop_elapsed_ms)

        # ── 9) メモリ解放 (SIGSEGV防止) ──
        # cv2 オブジェクトを毎イテレーション解放してメモリ断片化を防ぐ
        if i % 50 == 0:
            gc.collect()
            # scrcpy 不死身モード: 50イテレーションごとにチェック
            if i > 0:
                if _scrcpy_proc is None or _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] プロセス消滅を検知 — 自動再起動")
                    _scrcpy_proc = manage_scrcpy()

    logger.warning("最大イテレーション(%d)に到達。手動確認が必要です。", MAX_ITERATIONS)
    generate_and_copy_report(state, f"最大イテレーション({MAX_ITERATIONS})到達")

    # scrcpy プロセスを終了
    if _scrcpy_proc and _scrcpy_proc.poll() is None:
        _scrcpy_proc.terminate()
        logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)


if __name__ == "__main__":
    main()
