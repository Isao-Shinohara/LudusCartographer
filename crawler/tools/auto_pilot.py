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

import warnings
warnings.filterwarnings("ignore", message=".*urllib3.*OpenSSL.*")
warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")

import argparse
import gc
import logging
import os
import re
import signal
import sqlite3
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
import cv2
import json
import numpy as np
import random
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルート (ap/constants.py から import)
sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.ocr import run_ocr, find_best
from lc.utils import (
    get_android_serial, compute_phash, phash_distance, ensure_adb_connection,
    uninstall_app, is_app_installed, open_play_store, WIFI_DEVICE_ADDR,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("auto_pilot")

# PIL/Pillow の DEBUG STREAM ログを抑制 (スクリーンショット毎に IHDR/sRGB/IDAT が出る)
logging.getLogger("PIL").setLevel(logging.WARNING)
logging.getLogger("PIL.PngImagePlugin").setLevel(logging.WARNING)

# ─── 永続化: SQLite (ludus.db) ──────────────────────────────
_STATE_DB_PATH = Path(__file__).parent.parent / "storage" / "ludus.db"


def _ensure_state_table():
    """auto_pilot_state テーブルがなければ作成する。"""
    conn = sqlite3.connect(str(_STATE_DB_PATH))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auto_pilot_state "
        "(key TEXT PRIMARY KEY, value TEXT, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()


def persist_state(key: str, value: str):
    """状態を SQLite に永続化する。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        conn.execute(
            "INSERT OR REPLACE INTO auto_pilot_state (key, value, updated_at) "
            "VALUES (?, ?, datetime('now'))",
            (key, value),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[PERSIST] 書き込み失敗 key=%s: %s", key, e)


def load_state(key: str, default: str = "") -> str:
    """SQLite から状態を読み込む。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        row = conn.execute(
            "SELECT value FROM auto_pilot_state WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row[0] if row else default
    except Exception:
        return default


def delete_state(key: str):
    """SQLite から状態を削除する。"""
    try:
        conn = sqlite3.connect(str(_STATE_DB_PATH))
        conn.execute("DELETE FROM auto_pilot_state WHERE key = ?", (key,))
        conn.commit()
        conn.close()
    except Exception:
        pass


_ensure_state_table()

# ─── 定数: ap/constants.py から一括 import ───
from tools.ap.constants import (  # noqa: E402
    _CRAWLER_ROOT, SCREENSHOT_PATH, ANALYSIS_PATH, REMOTE_PATH, EVIDENCE_DIR,
    POLL_INTERVAL, PHASH_THRESHOLD, FORCE_ANALYZE_AFTER,
    STALL_TIMEOUT, BATTLE_WAIT, DOWNLOAD_WAIT, MIN_TAP_INTERVAL, MIN_CAPTURE_INTERVAL,
    WATCHDOG_DEADLOCK_THRESHOLD, WATCHDOG_MAX_SOFT_RECOVERIES,
    WATCHDOG_MAX_TOTAL_RECOVERIES, APP_PACKAGE, APP_ACTIVITY,
    WATCHDOG_EXEMPT_ACTIONS, ADV_RAPID_PHASH_MAX, BLACKOUT_BRIGHTNESS,
    ADV_NEXT_BTN_ROI, ADV_TOOLBAR_ROI, BATTLE_BTN_ROI,
    _DEBUG_SAVE_IMAGES, _GOLD_UI_ACTIONS, _SCENE_REEVAL_THRESHOLD,
    _CONFIRM_POS_KWS, _CONFIRM_NEG_KWS, _CURRENCY_SPEND_KWS,
    _UI_TEXT_KWS, _SINGLE_ONLY,
    _DIALOG_FIRST_KWS, _BATTLE_CORE_KWS, _BATTLE_UI_KWS,
    ANALYSIS_W, ANALYSIS_H,
    _OCR_BBOX_Y_PADDING, _GLOW_CENTER_Y_OFFSET,
    _GOLD_BTN_RETRY_Y_OFFSET, _FINGER_TIP_RATIO,
    _RIGHT_PANEL_X, _CHAR_HEAD_X1, _CHAR_HEAD_X2,
    _CHAR_HEAD_Y1, _CHAR_HEAD_Y2, _SPATIAL_MARGIN_TOP, _CLOSE_BTN_OFFSET,
    OCR_LANG, OCR_MIN_CONF, SCENE_INTERVAL,
    _TRANSITION_SLOW_SEC, _TRANSITION_HISTORY_MAX,
    _IMMEDIATE_ACTIONS,
)

# ─── 状態クラス: ap/state.py から import ───
from tools.ap.state import PilotState, StallCounter, TapCandidate  # noqa: E402
# ─── ヘルパー: ap/helpers.py から import ───
from tools.ap.helpers import (  # noqa: E402
    classify_scene, text_core_center, save_evidence,
    has_any, has_text, all_texts,
    log_milestone as _log_milestone,
    watchdog_recover,
)
# ─── コンテキスト + ハンドラ ───
from tools.ap.context import DetectContext  # noqa: E402
from tools.ap.handlers import dispatch as _dispatch_handlers  # noqa: E402
# Result/Dialog ハンドラ (handlers/ から再 import — テスト互換)
from tools.ap.handlers.result import handle_result_screen  # noqa: E402
from tools.ap.handlers.dialog_phase import handle_dialog_screen  # noqa: E402
from tools.ap.handlers.result import (  # noqa: E402  テスト互換 re-export
    _is_result_screen, _find_next_button,
)
# ─── デバイス操作: ap/device.py から import ───
import tools.ap.device as _ap_device  # noqa: E402
from tools.ap.device import (  # noqa: E402
    set_device_serial, set_scrcpy_device,
    adb, tap_device, swipe, swipe_device, take_screenshot, manage_scrcpy,
    get_device_resolution, _query_status_bar_height, check_adb_liveness,
    pop_last_scrcpy_bgr, check_foreground_app,
)
# DEVICE_SERIAL / SCRCPY_DEVICE はモジュール変数 — 読取りは _ap_device 経由
# 後方互換 re-export 用プロパティ代替
def __getattr__(name):
    if name == "DEVICE_SERIAL":
        return _ap_device.DEVICE_SERIAL
    if name == "SCRCPY_DEVICE":
        return _ap_device.SCRCPY_DEVICE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# 排除された偽の指ブロブキャッシュ (debug_latest_tap.png への [REJECTED] 描画用)
_rejected_finger_blobs: list = []

# Ctrl+C シグナルハンドラ用: main() で設定する PilotState への参照
_pilot_state_ref: Optional["PilotState"] = None



# ─── 画像処理: ap/image_proc.py から import ───
from tools.ap.image_proc import (  # noqa: E402
    detect_game_roi, roi_to_device, is_dark_screen, is_tutorial_walk_scene,
    prepare_analysis_image,
    detect_white_hand_pointer, create_finger_mask_image,
    detect_guide_glow, _run_battle_glow_sm, detect_active_battle_char,
    find_gold_frame_near,
    detect_movie_skip_button, detect_mini_conversation,
    detect_dialog, detect_dialog_nav, detect_dialog_frame_and_nav,
    process_paging_dialog, detect_notice_popup, count_page_dots, detect_background_blur,
    detect_text_input_area,
    detect_tutorial_gold_swipe, detect_tutorial_gold_button_tap, detect_tutorial_overlay,
    smart_tap_button, find_3d_arrow,
    AssetManager, ASSET_MANAGER,
    detect_adv_scene, AdvSceneResult,
    detect_movie_scene, MovieSceneResult,
    clear_imread_cache, imread_cached,
)


# ─── Result/Dialog ハンドラは handlers/result.py, handlers/dialog_phase.py に移動 ──────────
# テスト互換のため auto_pilot.py からも import 可能 (上記 re-import 参照)

# ─── 代替タップ候補収集で使用する定数 (旧 Result 関連) ──────────
_RESULT_NEXT_X_RATIO = 0.785
_RESULT_NEXT_Y_RATIO = 0.914

# パーティ編成画面の除外キーワード (Lv.1 が出るが Result ではない)


# ─── 代替タップ候補収集 ──────────────────────────────
_MAX_CANDIDATES = 5


def collect_secondary_candidates(
    ocr: list, state: PilotState,
    analysis_path: Optional[Path], primary_action: str,
) -> list[TapCandidate]:
    """
    detect_and_act() の主候補が空振りした際に試す代替タップ候補を収集する。
    同じ OCR/画像データを使い回すため追加の OCR コストは発生しない。

    主候補のカテゴリと重複する候補は除外する。
    最大 _MAX_CANDIDATES 個、priority 昇順でソートして返す。
    """
    candidates: list[TapCandidate] = []
    W, H = ANALYSIS_W, ANALYSIS_H
    texts = all_texts(ocr)

    # ── 1. ダイアログ ×/▷ (priority=10) ──
    if not primary_action.startswith("DIALOG") and analysis_path is not None:
        dlg_nav = detect_dialog_frame_and_nav(
            analysis_path, W, H, ocr_texts=texts, roi=state.game_roi)
        if dlg_nav:
            _nav_type, _nx, _ny = dlg_nav
            # 画面右端クランプ: x=1517-1519 (device端) → W*0.95 に制限
            _nx = min(_nx, int(W * 0.95))
            candidates.append(TapCandidate(
                x=_nx, y=_ny, action=f"CAND_DIALOG_{_nav_type.upper()}",
                priority=10, desc=f"dialog {_nav_type}"))

    # ── 2. 金枠ボタン (priority=20) ──
    if primary_action != "GOLD_BTN_TAP" and analysis_path is not None:
        gold_btn = detect_tutorial_gold_button_tap(
            analysis_path, right_half_only=False)
        if gold_btn:
            candidates.append(TapCandidate(
                x=gold_btn[0], y=gold_btn[1], action="CAND_GOLD_BTN",
                priority=20, desc="gold button"))

    # ── 3. OCR 確認ボタン (OK/はい/次へ) (priority=30) ──
    if not primary_action.startswith("CONFIRM") and primary_action != "ADV_CHOICE":
        confirm_btn = has_any(ocr, _CONFIRM_POS_KWS)
        if confirm_btn:
            _cx, _cy = confirm_btn["center"]
            _cy = max(0, _cy - _OCR_BBOX_Y_PADDING)
            candidates.append(TapCandidate(
                x=_cx, y=_cy, action="CAND_CONFIRM_OK",
                priority=30, desc=f"confirm '{confirm_btn['text']}'"))

    # ── 4. (removed) テンプレートマッチは1結果のみ → 代替指ブロブ不要 ──

    # ── 5. 閉じるボタン OCR (閉じる/Close) (priority=50) ──
    if not primary_action.startswith("CLOSE"):
        close_btn = has_any(ocr, ["閉じる", "Close", "CLOSE"])
        if close_btn:
            _clx, _cly = close_btn["center"]
            _cly = max(0, _cly - _OCR_BBOX_Y_PADDING)
            candidates.append(TapCandidate(
                x=_clx, y=_cly, action="CAND_CLOSE",
                priority=50, desc=f"close '{close_btn['text']}'"))

    # ── 6. ストーリータップ (下部テキスト) (priority=60) ──
    if primary_action != "STORY_TAP":
        # 下部1/3にテキストがあればセリフ送りとしてタップ
        _bottom_texts = [item for item in ocr
                         if item.get("center", (0, 0))[1] > H * 0.7
                         and len(item.get("text", "")) >= 3]
        if _bottom_texts:
            _st = _bottom_texts[0]
            candidates.append(TapCandidate(
                x=_st["center"][0], y=_st["center"][1],
                action="CAND_STORY_TAP", priority=60,
                desc=f"story '{_st['text'][:8]}'"))

    # ソート & 制限
    candidates.sort(key=lambda c: c.priority)
    return candidates[:_MAX_CANDIDATES]


# ─── 画面判定・アクション ──────────────────────────
def detect_and_act(ocr: list, state: PilotState,
                   analysis_path: Optional[Path] = None,
                   adv_result: Optional[AdvSceneResult] = None) -> tuple[str, float]:
    """
    OCR + 指差しブロブを分析し、アクションを決定する。
    analysis_path が渡された場合は finger blob 検出も実行。

    シーン別ハンドラに順次委譲するディスパッチャ。
    各ハンドラは ap/handlers/ に配置。

    Returns: (action_name, wait_seconds)
    """
    texts = all_texts(ocr)
    W, H = ANALYSIS_W, ANALYSIS_H
    joined = " ".join(texts)

    # ─── 事前計算: DetectContext 構築 ───
    _is_battle_early = any(kw in joined for kw in _BATTLE_CORE_KWS)
    _confirm_pos = has_any(ocr, _CONFIRM_POS_KWS)
    _confirm_neg = has_any(ocr, _CONFIRM_NEG_KWS)

    ctx = DetectContext(
        ocr=ocr,
        texts=texts,
        joined=joined,
        W=W, H=H,
        analysis_path=analysis_path,
        adv_result=adv_result or AdvSceneResult(),
        confirm_pos=_confirm_pos,
        confirm_neg=_confirm_neg,
        is_battle_early=_is_battle_early,
        in_battle_ctx=_is_battle_early,
        # 以下は dialog_phase ハンドラ内で計算・更新される
        is_notice=False,
        pre_dialog_finger=False,
        white_hand_pos=None,
        is_mini_conv=False,
        is_result_screen_flag=False,
        is_adv_or_movie=False,
    )

    return _dispatch_handlers(ctx, state)




# ─── フェーズタイムライン生成 ────────────────────────────
_PHASE_LABELS: dict[str, str] = {
    "APP_LAUNCH":     "アプリ起動",
    "NOTICE_DISMISS": "ご注意画面",
    "TITLE_TAP":      "タイトル画面",
    "FIRST_BATTLE":   "初回バトル開始",
    "DL_START":       "ダウンロード開始",
    "DL_END":         "ダウンロード完了",
    "NAME_INPUT":     "名前入力",
    "HOME_REACHED":   "ホーム画面到達",
    "GOAL_HOME_REACHED":   "目的達成: ホーム画面到達",
    "GOAL_GRIND_COMPLETE": "目的達成: 周回完了",
}


def _build_phase_timeline(state: PilotState) -> list[str]:
    """マイルストーンから Markdown テーブル形式のフェーズタイムラインを生成する。"""
    milestones = state.milestone_logged
    if not milestones:
        return ["## フェーズタイムライン", "(マイルストーン未記録)"]

    def _fmt_time(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    lines = [
        "## フェーズタイムライン",
        "",
        "| フェーズ | 到達時刻 | 区間時間 |",
        "|---------|---------|---------|",
    ]
    # マイルストーンを到達時刻順にソート
    sorted_ms = sorted(milestones.items(), key=lambda x: x[1])
    prev_time = 0.0
    for name, elapsed in sorted_ms:
        label = _PHASE_LABELS.get(name, name)
        delta = elapsed - prev_time
        lines.append(f"| {label} | {_fmt_time(elapsed)} | +{_fmt_time(delta)} |")
        prev_time = elapsed
    # 合計行
    total = sorted_ms[-1][1] if sorted_ms else 0.0
    lines.append(f"| **合計** | **{_fmt_time(total)}** | |")
    return lines


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
        "## テレメトリ",
        f"- 平均遷移時間         : {sum(state.transition_times) / max(len(state.transition_times), 1):.1f}s",
        f"- 最大遷移時間         : {max(state.transition_times) if state.transition_times else 0:.1f}s",
        f"- 10秒超遷移回数       : {sum(1 for t in state.transition_times if t > _TRANSITION_SLOW_SEC)}",
        f"- 計測サンプル数       : {len(state.transition_times)}",
        "",
        "## 戦績サマリー",
        f"- ホーム到達           : {'✓ CLEARED' if state.home_reached else '未到達'}",
        f"- チュートリアル       : {'All Tutorials Cleared' if state.home_reached else '進行中'}",
        f"- 周回モード           : {'ON' if state.grind_mode else 'OFF'}",
        f"- 周回完了数           : {state.grind_cycles_completed}",
        f"- 最終シーン           : {state.current_scene}",
        f"- Rank                 : {'1 / HOME REACHED' if state.home_reached else 'In Progress'}",
        f"- 起動種別             : {'新規スタート (--fresh-install)' if state.is_fresh_start else '途中再開'}",
        "",
        *_build_phase_timeline(state),
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
        # 距離が短すぎる場合はフルスクリーンスワイプを強制 (解像度差対策)
        _min_dist = int(ANALYSIS_H * 0.6)
        if abs(_fy - _ty) < _min_dist:
            if _dir == "UP":
                _fy = ANALYSIS_H - 50
                _ty = 50
            else:
                _fy = 50
                _ty = ANALYSIS_H - 50
        logger.info("[FAST] GoldSwipe %s → swipe (%d,%d)→(%d,%d) %dms",
                    _dir, _sx, _fy, _sx, _ty, _dur)
        swipe_device(_sx, _fy, _sx, _ty, _dur, state=state, desc=f"FAST_GoldSwipe_{_dir}")
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


# ─── 早期シーン判定 ─────────────────────────────────
def detect_scene_early(img_path: Path, state: PilotState, dist: int) -> str:
    """OCR 前にシーンを判定する。

    優先順位 (特定要素が多い順):
    1. BATTLE 継続 (前回 BATTLE + phash 小変化) → BATTLE
    2. ADV 継続 (前回 ADV + AUTO アイコン) → ADV
    3. ADV ツールバー初回検出 (3/5 アイコン) → ADV
    4. MOVIE 初回検出 (phash連続変化 or ⏭) → MOVIE  ← 最後 (特定要素が最も少ない)
    5. それ以外 → UNKNOWN (フルOCR 必要)

    NOTE: MOVIE 慣性 (MOVIE_INERTIA) は廃止。VisionOCR + scrcpy キャプチャにより
    毎フレーム OCR のコストが許容範囲になったため、毎ループ通常判定フローを通す。
    これによりシーン遷移 (MOVIE→BATTLE 等) の検出遅延がゼロになる。

    NOTE: img_path は呼び出し元で prepare_analysis_image() 済みの 1520x720 画像。
    テンプレートマッチの ROI・スケールが ANALYSIS_W/H と一致する。

    Returns: "MOVIE" | "BATTLE" | "ADV" | "GACHA" | "UNKNOWN"
    """

    # ── MOVIE 継続: phash が安定するまで即 MOVIE を返す ──
    # 動画再生中 (dist >= 3) はフレームが変化するので MOVIE 維持。
    # phash が安定 (dist < 3 が 3 回以上) したら動画終了とみなし ADV/BATTLE 再判定を許可。
    _MOVIE_STABLE_THRESHOLD = 3  # dist < 3 がこの回数続いたら安定とみなす
    if state.current_scene == "MOVIE":
        if dist >= 16:
            # 大きなフレーム変化 → 本物の動画再生、即 MOVIE 維持
            state._movie_stable_count = 0
            # 長期滞留カウンタは全 dist レンジでインクリメント
            state._movie_recheck_count = getattr(state, "_movie_recheck_count", 0) + 1
            if state._movie_recheck_count >= 8:
                logger.info("[SCENE_EARLY] MOVIE長期滞留 (%d回, dist=%d) → UNKNOWN (OCRへ)",
                            state._movie_recheck_count, dist)
                state._movie_recheck_count = 0
                state.current_scene = "UNKNOWN"
                state._from_movie = True  # MOVIE→UNKNOWN 遷移フラグ
                return "UNKNOWN"
            return "MOVIE"
        if dist >= 3:
            # 小さなフレーム変化 → バトル演出/ADV微動の可能性
            # 定期的にバトル/ADVテンプレートをチェック (3回に1回)
            state._movie_stable_count = 0
            state._movie_recheck_count = getattr(state, "_movie_recheck_count", 0) + 1
            if state._movie_recheck_count % 3 == 0:
                _battle_roi = BATTLE_BTN_ROI
                for _btn in ("battle_normal_attack", "battle_skill", "battle_special"):
                    _bm = ASSET_MANAGER.match_single(_btn, img_path, roi=_battle_roi)
                    if _bm and _bm[2] >= 0.60:
                        # battle_special 単独は誤検出リスクあり → UI二重確認
                        if _btn == "battle_special":
                            _ma = ASSET_MANAGER.match_single("adv_icon_auto", img_path)
                            _mf = ASSET_MANAGER.match_single("adv_icon_ff", img_path)
                            if not ((_ma and _ma[2] >= 0.60) or (_mf and _mf[2] >= 0.60)):
                                continue
                        logger.info("[SCENE_EARLY] MOVIE中バトルテンプレ検出 (%s %.2f) → BATTLE",
                                    _btn, _bm[2])
                        state._movie_recheck_count = 0
                        return "BATTLE"
                # ガチャ演出チェック: SKIP ボタン + 暗い背景
                _gacha_skip = detect_movie_skip_button(img_path)
                if _gacha_skip:
                    _gi = imread_cached(img_path)
                    if _gi is not None:
                        _gb = float(cv2.cvtColor(_gi, cv2.COLOR_BGR2GRAY).mean())
                        if _gb < 80:
                            logger.info("[SCENE_EARLY] MOVIE中ガチャ演出検出 (SKIP+暗背景 brightness=%.0f) → GACHA", _gb)
                            state._movie_recheck_count = 0
                            return "GACHA"
                # ADV チェック: 上部ツールバー領域でのみ AUTO を検出
                # ADV/動画のツールバーは画面上部 (y < 15%) にある
                # 字幕 (画面下部) への偽陽性マッチを防止
                # NOTE: 動画フレーム内の映像が ADV テンプレに誤マッチする頻度が高い
                #   (42% の確率で誤判定 → 一時停止/再開ループの原因)
                #   → AUTO 閾値 0.80, 補助証拠 2 個以上, phash 安定後のみチェック
                _movie_stable = getattr(state, "_movie_stable_count", 0)
                if _movie_stable >= 2:
                    _adv_toolbar_roi = ADV_TOOLBAR_ROI
                    _adv_auto_m = ASSET_MANAGER.match_single("adv_icon_auto", img_path, roi=_adv_toolbar_roi)
                    if _adv_auto_m and _adv_auto_m[2] >= 0.80:
                        # さらに ADV 固有アイコン (↓/LOG/MENU/FF) が上部に2つ以上あることを確認
                        _adv_evidence = 0
                        for _adv_icon in ("adv_next_btn", "adv_icon_log", "adv_icon_menu", "adv_icon_ff"):
                            _am = ASSET_MANAGER.match_single(_adv_icon, img_path, roi=_adv_toolbar_roi)
                            if _am and _am[2] >= 0.55:
                                _adv_evidence += 1
                        if _adv_evidence >= 2:
                            logger.info("[SCENE_EARLY] MOVIE中ADV検出 (AUTO score=%.2f, 補助証拠=%d) → UNKNOWN (OCRへ)",
                                        _adv_auto_m[2], _adv_evidence)
                            state._movie_recheck_count = 0
                            state.current_scene = "UNKNOWN"
                            return "UNKNOWN"
                        else:
                            logger.info("[SCENE_EARLY] MOVIE中AUTO検出 (score=%.2f) だが補助証拠不足(%d<2) → MOVIE継続",
                                        _adv_auto_m[2], _adv_evidence)
            # MOVIE 長期滞留脱出: recheck が 8 回 (約5秒) 超えたら MOVIE 誤判定の可能性
            # → UNKNOWN に遷移して OCR で正確なシーン判定を行う
            if state._movie_recheck_count >= 8:
                logger.info("[SCENE_EARLY] MOVIE長期滞留 (%d回) → UNKNOWN (OCRへ)",
                            state._movie_recheck_count)
                state._movie_recheck_count = 0
                state.current_scene = "UNKNOWN"
                state._from_movie = True  # MOVIE→UNKNOWN 遷移フラグ
                return "UNKNOWN"
            return "MOVIE"
        state._movie_stable_count = getattr(state, "_movie_stable_count", 0) + 1
        if state._movie_stable_count < _MOVIE_STABLE_THRESHOLD:
            return "MOVIE"
        # phash 安定 → 動画終了の可能性 → ADV/BATTLE 判定へフォールスルー
        logger.info("[SCENE_EARLY] MOVIE中phash安定 (stable=%d) → ADV/BATTLE再判定",
                    state._movie_stable_count)
        # NOTE: _from_movie は設定しない。ADV_EARLY パスで消費されず残留し、
        # 後続 OCR パスで ADV の SKIP ボタンを movie_skip_button と誤検出する。
        # MINI_CONV/TutorialWalk は state.current_scene=="MOVIE" ガードで防止済み。

    # BATTLE: 前回シーン == BATTLE + phash 小変化 (シーン継続)
    # 10回に1回テンプレートで実在確認 (Result画面等での誤BATTLE継続を防止)
    if state.current_scene == "BATTLE" and dist < 30:
        if state.battle_rapid_consecutive.count > 0 and state.battle_rapid_consecutive.count % 3 == 0:
            from tools.ap.image_proc import ASSET_MANAGER as _AM_verify
            _verify_roi = BATTLE_BTN_ROI
            _v_atk = _AM_verify.match_single("battle_normal_attack", img_path, roi=_verify_roi)
            _v_skl = _AM_verify.match_single("battle_skill", img_path, roi=_verify_roi)
            _v_best = max((_v_atk[2] if _v_atk else 0), (_v_skl[2] if _v_skl else 0))
            if _v_best < 0.70:
                logger.info("[SCENE_EARLY] BATTLE継続チェック: テンプレ未検出 (best=%.2f) → UNKNOWN", _v_best)
                return "UNKNOWN"
        return "BATTLE"

    # チュートリアル歩行シーン (チェッカー床): BATTLE/MOVIE 判定より先に検出
    # 低彩度+アイドルアニメで MOVIE 誤判定されるのを防止
    if img_path and not state.post_download and is_tutorial_walk_scene(img_path):
        logger.info("[SCENE_EARLY] TutorialWalk検出 (チェッカー床) → 即スワイプ")
        return "TUTORIAL_WALK"

    # BATTLE 初回/再検出: 右下の「通常攻撃」or「戦闘スキル」ボタンアイコンで判定
    # ADV ツールバーの AUTO/FF がバトル画面にも存在するため、ADV 判定より先に実行
    # NOTE: ADV 継続チェックより先に実行 — 一度 ADV と誤分類されても
    # バトルテンプレが見つかれば即 BATTLE に復帰する
    from tools.ap.image_proc import ASSET_MANAGER as _AM_battle
    try:
        _battle_roi = BATTLE_BTN_ROI
        _battle_hit = None
        for _btn_name in ("battle_normal_attack", "battle_skill", "battle_special"):
            _battle_m = _AM_battle.match_single(_btn_name, img_path, roi=_battle_roi)
            if _battle_m and _battle_m[2] >= 0.60:
                _battle_hit = (_btn_name, _battle_m[2])
                break
        if _battle_hit:
            _hit_name, _hit_score = _battle_hit
            # ダイアログ四隅テンプレで利用規約等の金枠装飾による誤マッチを棄却
            _has_dialog = False
            try:
                _dlg_check = detect_dialog_frame_and_nav(img_path)
                _has_dialog = _dlg_check is not None
            except Exception:
                pass
            if _has_dialog:
                logger.info("[SCENE_EARLY] %s(%.2f) 検出だがダイアログ四隅あり → BATTLE棄却",
                            _hit_name, _hit_score)
            else:
                logger.info("[SCENE_EARLY] Battle初回検出 (%s score=%.2f) → BATTLE",
                            _hit_name, _hit_score)
                return "BATTLE"
    except Exception:
        pass
    # BATTLE 補助判定: 金枠オーバーレイでテンプレが失敗する場合
    # 右下 (バトルボタン領域) に金枠が存在 → BATTLE 継続
    # ※初回 BATTLE 判定には使わない (ホーム画面のナビバー金枠で偽陽性)
    if state.current_scene == "BATTLE":
        try:
            from tools.ap.image_proc import find_gold_frame_near
            _battle_gold_cx = int(ANALYSIS_W * 0.88)
            _battle_gold_cy = int(ANALYSIS_H * 0.80)
            _bg_result = find_gold_frame_near(
                img_path, _battle_gold_cx, _battle_gold_cy, search_radius=200)
            if _bg_result is not None:
                _bg_cx, _bg_cy, _bg_w, _bg_h = _bg_result
                logger.info("[SCENE_EARLY] Battle補助: 右下金枠(%d,%d %dx%d) → BATTLE",
                            _bg_cx, _bg_cy, _bg_w, _bg_h)
                return "BATTLE"
        except Exception:
            pass

    # ADV 継続: 前回 ADV + phash 小変化 + AUTO アイコン + ADV ツールバー → ADV 高速パス
    # dist ガード: ADV→BATTLE 遷移時 (dist>=20) はフォールスルーして再評価する
    # NOTE: AUTO アイコン単独ではバトル画面の AUTO ボタンと区別不能 (score=0.91 で誤一致)
    #        → ADV ツールバー全体も確認して確定する
    if state.current_scene == "ADV" and dist < 20:
        from tools.ap.image_proc import ASSET_MANAGER as _AM_adv
        try:
            _auto_roi = ADV_TOOLBAR_ROI
            _auto_m = _AM_adv.match_single("adv_icon_auto", img_path, roi=_auto_roi)
            if _auto_m and _auto_m[2] >= 0.50:
                # AUTO あり → ADV ツールバーも確認 (バトル画面の AUTO 誤一致を排除)
                _adv_check = detect_adv_scene(img_path, roi=state.game_roi)
                if _adv_check.is_adv:
                    return "ADV"
                # ツールバーなし + AUTO あり → BATTLE テンプレートで確認
                _battle_roi = BATTLE_BTN_ROI
                _b_atk = _AM_adv.match_single("battle_normal_attack", img_path, roi=_battle_roi)
                _b_skl = _AM_adv.match_single("battle_skill", img_path, roi=_battle_roi)
                _b_best = max((_b_atk[2] if _b_atk else 0), (_b_skl[2] if _b_skl else 0))
                if _b_best >= 0.65:
                    logger.info("[SCENE_EARLY] ADV継続: AUTO(%.2f)+BATTLEテンプレ(%.2f) → BATTLE",
                                _auto_m[2], _b_best)
                    return "BATTLE"
                logger.info("[SCENE_EARLY] ADV継続: AUTO(%.2f) ADVでもBATTLEでもなし → UNKNOWN",
                            _auto_m[2])
        except Exception:
            pass

    # ADV: ↓ボタンテンプレートで直接判定
    # ↓ボタン (adv_next_btn) は ADV シーン固有。バトル/MOVIE には存在しない。
    # detect_adv_scene は OCR 必須のため detect_scene_early では使えない。
    # MOVIE→ADV 誤判定防止: MOVIE直後は ADV ツールバー (AUTO/↓等) の二重確認を行う
    if state.current_scene != "MENU":
        _adv_next_early = ASSET_MANAGER.match_single("adv_next_btn", img_path,
                    roi=ADV_NEXT_BTN_ROI)
        if _adv_next_early:
            # MOVIE からの遷移時は ADV ツールバー (AUTO ボタン等) の存在を二重確認
            # 黒背景＋白文字がテンプレートに誤マッチするケースを防止
            if state.current_scene == "MOVIE":
                _adv_auto = ASSET_MANAGER.match_single("adv_icon_auto", img_path)
                if not _adv_auto or _adv_auto[2] < 0.60:
                    logger.info("[SCENE_EARLY] MOVIE中ADV↓誤検出を棄却 (AUTO未検出)")
                    return "MOVIE"
            return "ADV"

    # ADV: AUTO + ↓ボタン or FF (↓単独が検出できなかった場合のフォールバック)
    # adv_next_btn の ROI 外検出 + AUTO の存在で ADV を確定する
    # BATTLE にも AUTO/FF はあるが、↓ボタン (adv_next_btn) は ADV 固有
    # ADV: AUTO + ↓ボタン (↓ボタンの ROI 外リカバリ)
    # ↓ボタンは ADV 固有のため、AUTO + ↓で ADV 確定。
    # ↓なしでの ADV 固有アイコン判定は探索パート等で誤検出するため廃止。
    if state.current_scene not in ("MENU", "BATTLE", "MOVIE"):
        _adv_auto_init = ASSET_MANAGER.match_single("adv_icon_auto", img_path,
                                                     roi=ADV_TOOLBAR_ROI)
        if _adv_auto_init and _adv_auto_init[2] >= 0.50:
            _adv_next_full = ASSET_MANAGER.match_single("adv_next_btn", img_path)
            if _adv_next_full and _adv_next_full[2] >= 0.70:
                logger.info("[SCENE_EARLY] ADV初回検出 (AUTO=%.2f + ↓=%.2f) → ADV",
                            _adv_auto_init[2], _adv_next_full[2])
                return "ADV"

    # NOTE: ポップアップ検出 (ドット+背景ぼかし) は廃止。
    # ADV セリフ画面で偽陽性が多発し UNKNOWN に落としてスタックする問題の根本原因だった。
    # MOVIE 判定は phash 安定チェック付きなのでポップアップを MOVIE と誤判定するリスクはない。

    # ── phash 連続変化カウンタ更新 ──
    # BATTLE/ADV は上で return 済み。ここに来るのは UNKNOWN 候補のみ。
    _PHASH_MOVING_THRESHOLD = 5  # phash_dist >= 5 で「フレーム変化あり」
    if dist >= _PHASH_MOVING_THRESHOLD:
        state.phash_moving_count += 1
    else:
        state.phash_moving_count = 0

    # ── ガチャ演出画面: SKIP ボタン + 暗い背景 → タップで進行 ──
    # 光の玉が並ぶ画面。MOVIE ではなくタップで 1 つずつキャラが表示される。
    # MOVIE 中はスキップ（動画内のキャラ表示シーンで誤発火防止）
    if img_path and state.current_scene != "MOVIE":
        _gacha_skip = detect_movie_skip_button(img_path)
        if _gacha_skip:
            _gacha_img = imread_cached(img_path)
            if _gacha_img is not None:
                _gacha_brightness = float(cv2.cvtColor(_gacha_img, cv2.COLOR_BGR2GRAY).mean())
                if _gacha_brightness < 80:
                    logger.info("[SCENE_EARLY] ガチャ演出検出 (SKIP+暗背景 brightness=%.0f) → GACHA",
                                _gacha_brightness)
                    return "GACHA"

    # MOVIE 初回検出 (最後): 特定要素が最も少ないため他シーンを先に排除
    # チュートリアル中 + download_active → DL完了ダイアログ優先 (SKIPボタン以外はスキップ)
    if state.download_active and not state.home_reached:
        # SKIPボタン検出 → キャンセル誤タップからの復帰 (MOVIE判定を許可)
        _dl_init_movie = detect_movie_scene(img_path, adv_result=None, phash_dist=dist)
        if _dl_init_movie.is_movie and _dl_init_movie.has_skip_btn:
            logger.warning("[SCENE_EARLY] download_active + SKIPボタン検出 → キャンセル誤タップ復帰 → MOVIE")
            return "MOVIE"
        logger.info("[SCENE_EARLY] download_active=True (チュートリアル) → MOVIE判定スキップ (DL完了ダイアログ優先)")
        return "UNKNOWN"
    _adv = detect_adv_scene(img_path, roi=state.game_roi)
    _movie = detect_movie_scene(img_path, adv_result=_adv, phash_dist=dist,
                                phash_moving_count=state.phash_moving_count)
    if _movie.is_movie:
        if _movie.has_skip_btn:
            logger.info("[SCENE_EARLY] Movie検出 (conf=%.2f, ⏭あり) → MOVIE", _movie.confidence)
        else:
            logger.info("[SCENE_EARLY] Movie検出 (conf=%.2f, phash連続変化=%d) → MOVIE",
                        _movie.confidence, state.phash_moving_count)
        return "MOVIE"

    return "UNKNOWN"


def handle_movie(img_path: Path, state: PilotState, dist: int,
                 cur_phash: str) -> bool:
    """動画シーン専用ハンドラ。指アイコン / GoldSwipe / 金枠ボタン検出なし。

    - post_download + SKIP 表示 → DL完了後ループなので SKIP タップで脱出
    - それ以外の動画 → 待機のみ (タップで一時停止/再開ループに陥る)

    Returns: True if handled (caller should continue), False for fallthrough.
    """
    W, H = ANALYSIS_W, ANALYSIS_H

    # ── DL画面背景動画: SKIPボタンが出たらキャンセル誤タップ → SKIPで脱出 ──
    if state.download_active and not state.home_reached:
        _dl_skip = detect_movie_skip_button(img_path)
        if _dl_skip:
            _dsk_x, _dsk_y = roi_to_device(_dl_skip[0], _dl_skip[1], state.game_roi)
            logger.warning("[MOVIE] download_active + SKIP検出 → キャンセル誤タップ復帰 SKIPタップ (%d,%d)",
                           _dsk_x, _dsk_y)
            tap_device(_dsk_x, _dsk_y, state, "MOVIE_SKIP")
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_phash = ""
            return True
        # SKIP なし → DL完了ダイアログ検出のためフルOCRへ
        logger.info("[MOVIE] download_active=True → MOVIEハンドラ脱出 (DL完了チェック優先)")
        state.movie_wait_consecutive = 0; state.movie_static_count = 0
        return False

    # ── DL直後 + SKIP 表示 → DL完了後ループ脱出 ──
    # ダウンロード完了後に動画がループする現象: SKIP をタップして抜ける
    if state.post_download:
        _skip_pos = detect_movie_skip_button(img_path)
        if _skip_pos:
            _sk_x, _sk_y = roi_to_device(_skip_pos[0], _skip_pos[1], state.game_roi)
            logger.info("[MOVIE] DL直後 + SKIP検出 → SKIPタップ (%d,%d) でループ脱出", _sk_x, _sk_y)
            tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP")
            state.movie_wait_consecutive = 0; state.movie_static_count = 0
            state.last_phash = ""
            return True
        logger.info("[MOVIE] DL直後だが SKIP 未検出 → 待機")

    # ── 待機カウンタ ──
    state.movie_wait_consecutive += 1

    # NOTE: MOVIE_RESUME_IMMEDIATE は廃止。MOVIE→他シーン→MOVIE の再遷移で
    # consecutive がリセットされ、毎回即タップ→一時停止のループを引き起こしていた。
    # 一時停止の検出は movie_static_count に一本化する。

    # ── 静止フレームカウント ──
    # dist==0 (phash完全一致) = 一時停止。dist>0 なら再生中 (字幕シーンでも微差あり)。
    if dist == 0:
        state.movie_static_count += 1
    else:
        state.movie_static_count = 0

    # ── 一時停止検出: dist==0 が5秒 (~8回) 続いたら中央タップで再開 ──
    # 遷移直後 (consecutive <= 5) は直前タップの影響で暗転/静止するため、
    # カウントはするが再開処理はスキップする。
    _PAUSE_THRESHOLD = 8  # ~5秒 (ループ間隔 ~0.6秒)
    if state.movie_static_count >= _PAUSE_THRESHOLD and state.movie_wait_consecutive > 5:
        logger.warning("[MOVIE] 一時停止検出 (dist=0 が %d 回連続) → 中央タップで再開",
                       state.movie_static_count)
        tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "MOVIE_RESUME")
        state.movie_static_count = 0
        state.last_phash = ""
        time.sleep(1.0)
        return True

    # ── 長時間待機: ハードリミット (探索画面等の誤MOVIE判定を脱出) ──
    _MOVIE_HARD_LIMIT = 300  # ~3分: これ以上は動画ではない
    if state.movie_wait_consecutive >= _MOVIE_HARD_LIMIT:
        logger.warning("[MOVIE] ハードリミット %d 回到達 → 動画ではない、MOVIE強制脱出",
                       state.movie_wait_consecutive)
        state.movie_static_count = 0
        state.movie_wait_consecutive = 0
        state.current_scene = "UNKNOWN"
        state.last_action = "SCENE_TAP"
        state.last_phash = ""
        return False  # MOVIE ハンドラ脱出 → フルOCRへ

    if state.movie_wait_consecutive >= 30 and state.movie_wait_consecutive % 30 == 0:
        logger.info("[MOVIE] 長時間待機 %d 回 — 動画自動終了を待機中",
                    state.movie_wait_consecutive)

    # ── 通常待機 (動画は自動終了するのでタップせず待つ) ──
    logger.info("[MOVIE] 待機 (%d) dist=%d static=%d",
                state.movie_wait_consecutive, dist,
                state.movie_static_count)
    state.last_action = "MOVIE_WAIT"
    state.stall_start = 0.0
    time.sleep(0.5)
    state.last_phash = cur_phash
    return True


def handle_battle(analysis_path: Path, state: PilotState, dist: int) -> bool:
    """バトルシーン専用ハンドラ。発光/モヤ/通常攻撃のみ。GoldSwipe なし。

    既存 BATTLE_RAPID のロジックを関数化。
    - Phase 0: チュートリアル金枠 + 指ブロブ
    - Phase A: アクティブキャラ (赤/ピンク発光)
    - Phase B: 右側スキル/攻撃ボタン
    - Phase C: 通常攻撃フォールバック

    Returns: True if handled, False for fallthrough to OCR.
    """
    # ── force OCR override: phash 静止 → ダイアログ可能性 ──
    if dist <= 2 and state.same_phash_count >= FORCE_ANALYZE_AFTER:
        logger.info("[BATTLE] force_ocr (dist=%d, same=%d) → OCR フォールスルー",
                    dist, state.same_phash_count)
        return False

    # 速度チュートリアル表示中は OCR で処理
    if any(any(k in t for k in ("このボタンでバトル", "進行速度を変更"))
           for t in state.last_ocr_texts):
        return False

    # BATTLE_RAPID 連続ループ上限
    if state.battle_rapid_consecutive.stalled:
        logger.info("[BATTLE] 連続 %d 回 → シーンリセット + OCR で再評価",
                    state.battle_rapid_consecutive.count)
        state.battle_rapid_consecutive.reset()
        state.current_scene = "UNKNOWN"
        return False

    # ── バトルUIガード: 通常攻撃ボタンが見えなければ Result/ADV の可能性 → OCR へ ──
    # 3回に1回チェック (テンプレートマッチ ~10ms のコストは無視できる)
    if state.battle_rapid_consecutive.count > 0 and state.battle_rapid_consecutive.count % 3 == 0:
        _atk_m = ASSET_MANAGER.match_single("battle_normal_attack", analysis_path)
        if not _atk_m or _atk_m[2] < 0.70:
            logger.info("[BATTLE] 通常攻撃ボタン未検出 (count=%d) → OCR で再評価",
                        state.battle_rapid_consecutive.count)
            state.battle_rapid_consecutive.reset()
            return False
        # ダイアログコーナー検出: チュートリアルポップアップが表示されていれば即 OCR へ
        _corner_tl = ASSET_MANAGER.match_single("dialog_corner_tl", analysis_path)
        _corner_bl = ASSET_MANAGER.match_single("dialog_corner_bl", analysis_path)
        if (_corner_tl and _corner_tl[2] >= 0.65) or (_corner_bl and _corner_bl[2] >= 0.65):
            _corner_score = max(
                _corner_tl[2] if _corner_tl else 0,
                _corner_bl[2] if _corner_bl else 0)
            logger.info("[BATTLE] ダイアログコーナー検出 (score=%.2f, count=%d) → OCR で再評価",
                        _corner_score, state.battle_rapid_consecutive.count)
            state.battle_rapid_consecutive.reset()
            return False

    _rapid_tx = _rapid_ty = 0
    _rapid_action = ""
    _rapid_double = False

    # ── 共通: 指テンプレートマッチ検出 ──
    _rapid_finger = ASSET_MANAGER.match_single("tutorial_hand_pointer", analysis_path)
    _rapid_blobs = []
    if _rapid_finger and _rapid_finger[2] >= 0.70:
        _rf_cx, _rf_cy = _rapid_finger[0], _rapid_finger[1]
        if _rf_cy > _SPATIAL_MARGIN_TOP and _rf_cx < ANALYSIS_W - _CLOSE_BTN_OFFSET:
            _rapid_blobs = [(_rf_cx, _rf_cy, _rapid_finger[2])]

    # ── Phase 0: チュートリアル金枠 → 最優先タップ ──
    # 指ブロブ有無に関わらず金枠を常時チェック (~10ms)
    # scrcpy キャプチャでは指ブロブ面積が変動するためゲート緩和
    # NOTE: character_selected でもスキップしない — 金枠検出の extent<0.55 フィルタで
    # 通常のボタン発光と区別可能。ガードすると戦闘スキル等のチュートリアル金枠を見逃す。
    # BATTLE: 右半分のみ (左側キャラアイコンの菱形装飾を金枠と誤検出するため)
    # ただしチュートリアル指テンプレが左側で検出された場合は全画面探索を許可
    # (必殺技チュートリアル等で左側キャラカードをタップさせるケース)
    _battle_rho = True  # right_half_only default
    for _ft in ("tutorial_hand_pointer", "tutorial_finger_down", "tutorial_finger_up",
                "tutorial_finger_left", "tutorial_finger_right"):
        _fm = ASSET_MANAGER.match_single(_ft, analysis_path)
        if _fm and _fm[2] >= 0.70 and _fm[0] < ANALYSIS_W * 0.5:
            _battle_rho = False
            logger.info("[BATTLE] 指テンプレ左側検出 (%s, x=%d) → 金枠全画面探索", _ft, _fm[0])
            break
    _gold_tap = detect_tutorial_gold_button_tap(
        analysis_path, right_half_only=_battle_rho, overlay_mode=False,
        skip_upper_filter=True)
    if _gold_tap:
        _rapid_tx, _rapid_ty = _gold_tap
        _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"
    # フォールバック: 指テンプレ検出 + find_gold_frame_near で金枠が見つかればそちらを使用
    # バトル: 右半分 (x>W/2) かつ y>35% のみ (左キャラアイコン・上部UI排除)
    # 暗転オーバーレイ中は全画面許可
    if not _rapid_action and _rapid_blobs:
        _rb = _rapid_blobs[0]
        _gf = find_gold_frame_near(analysis_path, _rb[0], _rb[1], search_radius=200)
        if _gf is not None:
            # バトル中: 右半分・下部のみ有効 (上部UI・左キャラ排除)
            # 左側指テンプレ検出時 (_battle_rho=False) はバイパス
            if not _battle_rho or (_gf[0] >= ANALYSIS_W * 0.5 and _gf[1] >= ANALYSIS_H * 0.35):
                _rapid_tx, _rapid_ty = _gf[0], _gf[1]
                _rapid_action = "BATTLE_RAPID_GOLD_FRAME_FALLBACK"
                logger.info("[BATTLE_RAPID] 金枠フォールバック: finger(%d,%d) → gold(%d,%d)",
                            _rb[0], _rb[1], _gf[0], _gf[1])

    # ── Phase A: アクティブキャラ検出 (赤/ピンク発光) ──
    _active_char = detect_active_battle_char(analysis_path, ANALYSIS_W, ANALYSIS_H)
    if not _rapid_action and not state.character_selected and _active_char is not None:
        _rapid_tx, _rapid_ty = _active_char[0], _active_char[1]
        _rapid_action = "BATTLE_RAPID_ACTIVE_P1"
        _rapid_double = True

    # ── Phase B: 右側スキル/攻撃ボタン ──
    if not _rapid_action:
        _rapid_glows = detect_guide_glow(
            analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
        _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]
        _right_panel = [b for b in _rapid_blobs
                        if b[0] > _RIGHT_PANEL_X and b[1] > ANALYSIS_H * 0.45]
        if state.character_selected or state.char_just_selected:
            # B-0: テンプレートで battle_special / battle_skill / battle_normal_attack を探す (精度最優先)
            for _btn_name in ("battle_special", "battle_skill", "battle_normal_attack"):
                _btn_m = ASSET_MANAGER.match_single(_btn_name, analysis_path)
                if _btn_m and _btn_m[2] >= 0.60:
                    _tmpl_action = f"BATTLE_RAPID_TMPL_{_btn_name.upper()}"
                    logger.info("[BATTLE_RAPID] テンプレ %s (%.2f) → tap(%d,%d)",
                                _btn_name, _btn_m[2], _btn_m[0], _btn_m[1])
                    tap_device(_btn_m[0], _btn_m[1], state, _tmpl_action)
                    state.character_selected = False
                    state.char_just_selected = False
                    state.finger_detections += 1
                    state.battle_rapid_consecutive.tick()
                    return True
            # B-1: テンプレ未検出 → glow フォールバック
            if not _rapid_action and _rapid_right_g:
                _rr = max(_rapid_right_g, key=lambda g: g["area"])
                _rapid_tx = _rr["cx"]
                _rapid_ty = max(1, _rr["by"] + _rr["bh"] * 2 // 3)
                _rapid_action = "BATTLE_RAPID_GLOW_P2"
            elif not _rapid_action and _right_panel:
                _tb = max(_right_panel, key=lambda b: b[2])
                _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                _rapid_action = "BATTLE_RAPID_MOYA_P2"
            elif not _rapid_action:
                _rapid_tx, _rapid_ty = roi_to_device(
                    int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                _rapid_action = "BATTLE_RAPID_NORMATK_P2"

    # ── Phase C: フォールバック → 0.5秒待機+再確認 → 右側攻撃ボタン ──
    if not _rapid_action:
        if state.normatk_fallback.stalled:
            logger.info("[BATTLE] FALLBACK %d回連続 → OCR で再評価",
                        state.normatk_fallback.count)
            state.normatk_fallback.reset()
            return False
        # 敵ターン/アニメーション中の可能性 → 待機+再スクショで確認
        time.sleep(0.5)
        _retry_path, _retry_w, _retry_h, _ = take_screenshot()
        if _retry_path:
            _retry_analysis = prepare_analysis_image(_retry_path, _retry_w, _retry_h)
            _retry_active = detect_active_battle_char(
                _retry_analysis, ANALYSIS_W, ANALYSIS_H)
            if _retry_active:
                # プレイヤーターンに切り替わった → 再解析して正規パスへ
                logger.info("[BATTLE] 再確認でACTIVE_CHAR検出 → 次ループで正規処理")
                return True  # 次ループで handle_battle が再呼出される
        # テンプレマッチで正しいボタン位置を探す (character_selected 不問)
        for _fb_btn in ("battle_special", "battle_skill", "battle_normal_attack"):
            _fb_m = ASSET_MANAGER.match_single(_fb_btn, analysis_path)
            if _fb_m and _fb_m[2] >= 0.60:
                _rapid_tx, _rapid_ty = _fb_m[0], _fb_m[1]
                _rapid_action = f"BATTLE_RAPID_TMPL_{_fb_btn.upper()}"
                logger.info("[BATTLE_RAPID] FALLBACK テンプレ %s (%.2f) → tap(%d,%d)",
                            _fb_btn, _fb_m[2], _rapid_tx, _rapid_ty)
                break
        if not _rapid_action:
            state.normatk_fallback.tick()
            logger.info("[BATTLE] テンプレ未検出 (stall %d/%d) → 盲タップせず次ループで再判定",
                        state.normatk_fallback.count, state.normatk_fallback.threshold)
            return True
        state.normatk_fallback.tick()
    else:
        state.normatk_fallback.reset()

    # ── 共通タップ実行 ──
    if _rapid_action:
        logger.info("[%s] tap(%d,%d)%s",
                    _rapid_action, _rapid_tx, _rapid_ty,
                    " ダブルタップ" if _rapid_double else "")
        tap_device(_rapid_tx, _rapid_ty, state, _rapid_action,
                   rapid=_rapid_double)
        if _rapid_double:
            tap_device(_rapid_tx, _rapid_ty, state, _rapid_action)

        # HSV 金枠タップの空振り検出 → テンプレフォールバック
        if _rapid_action == "BATTLE_RAPID_GOLD_TUTORIAL":
            time.sleep(0.3)
            _verify_path, _vw, _vh, _ = take_screenshot()
            if _verify_path:
                _verify_analysis = prepare_analysis_image(_verify_path, _vw, _vh)
                _verify_hash = compute_phash(_verify_analysis)
                _verify_dist = phash_distance(state.last_phash, _verify_hash) if state.last_phash else 999
                if _verify_dist < 3:
                    # 画面変化なし → HSV 偽陽性。テンプレで再試行
                    for _fb_btn in ("battle_special", "battle_skill", "battle_normal_attack"):
                        _fb_m = ASSET_MANAGER.match_single(_fb_btn, _verify_analysis)
                        if _fb_m and _fb_m[2] >= 0.60:
                            logger.info("[BATTLE_RAPID] HSV金枠空振り → テンプレ %s (%.2f) tap(%d,%d)",
                                        _fb_btn, _fb_m[2], _fb_m[0], _fb_m[1])
                            tap_device(_fb_m[0], _fb_m[1], state,
                                       f"BATTLE_RAPID_TMPL_{_fb_btn.upper()}")
                            break

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
        state.battle_rapid_consecutive.tick()
        return True

    return False


def handle_adv(img_path: Path, state: PilotState, dist: int,
               cur_phash: str, actual_w: int, actual_h: int) -> bool:
    """ADV シーン専用ハンドラ。↓ボタン / ミニ会話。

    detect_scene_early で ADV 判定済みのため、ここでは detect_adv_scene を
    再呼び出ししない（2重チェック廃止）。

    Returns: True if handled, False for fallthrough to OCR.
    """
    _adv_tap_x = int(ANALYSIS_W * 0.93)
    _adv_tap_y = int(ANALYSIS_H * 0.91)

    # ── ↓ボタンテンプレートマッチ → タップ ──
    # ↓ボタンは画面右下 (y > 80%) にある。右上の ⏭ ボタン (動画スキップ) に
    # 誤マッチしないよう y > 80% に限定
    _adv_next = ASSET_MANAGER.match_single("adv_next_btn", img_path,
                roi=ADV_NEXT_BTN_ROI)
    if _adv_next:
        logger.info("[ADV] ↓検出 (score=%.2f) → タップ (%d,%d)", _adv_next[2], _adv_tap_x, _adv_tap_y)
        tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
        state.last_action = "ADV_RAPID_TAP"
        state.last_phash = ""
        return True

    # ── ミニ会話タップ ──
    _mc = detect_mini_conversation(img_path)
    if _mc is not None:
        _mc_cx, _mc_cy, _mc_side = _mc
        logger.info("[ADV] 吹き出し(%s) → タップ (%d,%d)", _mc_side, _mc_cx, _mc_cy)
        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
        state.last_action = "MINI_CONV_TAP"
        state.last_phash = ""
        return True

    # ↓ なし + 吹き出しなし → フォールスルー
    return False


# ─── コマンドライン引数 ───────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(description="まどドラ自律操縦")
    parser.add_argument("--verbose", action="store_true", help="デバッグログ出力")
    parser.add_argument("--grind", action="store_true",
                        help="周回モード: ホーム到達後もクエストへ自動ナビゲート")
    parser.add_argument("--max-cycles", type=int, default=0,
                        help="周回上限 (0=無制限, デフォルト: 0)")
    parser.add_argument("--pairing-code", type=str, default=None,
                        help="adb pair 用ペアリングコード (Android 11+)")
    parser.add_argument("--pairing-port", type=int, default=None,
                        help="adb pair 用ポート番号 (Android 11+)")
    parser.add_argument("--fresh-install", action="store_true",
                        help="アンインストール → Play Store 再インストール (新規アカウント)")
    parser.add_argument("--wifi-addr", type=str, default=None,
                        help="Wi-Fi ADB 接続先アドレス (IP:PORT, 例: 192.168.10.118:5555)")
    # parse_known_args: main.py 経由の場合に --android, --package 等の未知引数を無視
    args, _ = parser.parse_known_args()
    return args


# ─── Play Store 再インストール ──────────────────────
def _fresh_install_from_play_store(serial: str, package: str) -> None:
    """
    アプリをアンインストール → Play Store から再インストールする。

    PilotState はまだ存在しない段階で呼ばれるため、
    tap_device() ではなく直接 adb shell input tap を使用する。

    v2 改善点 (2026-03-13):
    - 画面ウェイク+ロック解除を最初に実行
    - uiautomator dump 前に stale XML を削除
    - uiautomator dump 結果の全テキストをログ出力 (診断用)
    - Play Store ページ読み込み待機フェーズ追加 (BACK 乱発防止)
    - 試行回数に応じた待機時間エスカレーション
    - 診断スクリーンショットを storage/fresh_install/ に保存
    - Google Play Protect / TOS 画面のハンドリング
    - フォアグラウンドアプリ検証 (BACK が Store を閉じた場合のリカバリ)
    """
    INSTALL_KEYWORDS = ["インストール", "Install", "install"]
    ACCEPT_KEYWORDS = ["同意する", "Accept", "OK", "続行", "Continue"]
    OPEN_KEYWORDS = ["開く", "Open", "プレイ", "Play"]  # uiautomator用 (完全一致)
    _OCR_OPEN_KEYWORDS = ["開く", "プレイ"]  # OCR用 (部分一致なので "Play"/"Open" は除外)
    # アプリ詳細ページ確認用キーワード (これらがページ内にあればまどドラページ)
    _APP_PAGE_VERIFY_KWS = ["マギアエクセドラ", "magia", "Magia", "MAGIA", "aniplex", "Aniplex",
                             "まどか", "マドカ", "magireco", "マギレコ"]
    # ページ読み込み中の兆候 (これらが見えたら待機)
    _LOADING_HINTS = ["読み込み中", "Loading", "接続しています", "Connecting"]
    # ポップアップ / ダイアログを閉じるべきキーワード
    _POPUP_DISMISS_KWS = ["後で", "後で行う", "スキップ", "いいえ", "No thanks",
                          "Not now", "No, thanks", "閉じる", "DISMISS",
                          "GOT IT", "OK", "了解"]
    MAX_ATTEMPTS = 15  # 10→15 に増加 (遅い接続対応)
    POLL_INTERVAL_SEC = 5
    MAX_POLL_COUNT = 120  # 5秒 × 120 = 10分 (大容量アプリ対応)
    _PNG_HEADER = b"\x89PNG\r\n\x1a\n"
    MAX_REINSTALL = 2  # 再アンインストール上限
    PLAY_STORE_PKG = "com.android.vending"

    # 診断スクリーンショット保存先
    _diag_dir = Path(__file__).parent.parent / "storage" / "fresh_install"
    _diag_dir.mkdir(parents=True, exist_ok=True)

    def _adb_tap(x: int, y: int) -> None:
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "tap", str(x), str(y)],
            capture_output=True, timeout=5,
        )

    def _adb_key(keycode: str) -> None:
        subprocess.run(
            ["adb", "-s", serial, "shell", "input", "keyevent", keycode],
            capture_output=True, timeout=5,
        )

    def _adb_screenshot(path: str) -> bool:
        """スクリーンショット取得 + PNG ヘッダ検証。3回リトライ。"""
        for _ss_try in range(3):
            try:
                r = subprocess.run(
                    ["adb", "-s", serial, "exec-out", "screencap", "-p"],
                    capture_output=True, timeout=10,
                )
                if (r.returncode == 0 and len(r.stdout) >= 10_000
                        and r.stdout[:8] == _PNG_HEADER):
                    Path(path).write_bytes(r.stdout)
                    return True
            except (subprocess.TimeoutExpired, Exception):
                pass
            time.sleep(0.5)
        return False

    def _save_diag_screenshot(label: str) -> None:
        """診断用スクリーンショットを保存 (失敗しても無視)。"""
        ts = time.strftime("%H%M%S")
        path = str(_diag_dir / f"{label}_{ts}.png")
        if _adb_screenshot(path):
            logger.info("[FRESH_INSTALL] 診断SS保存: %s", path)

    def _get_device_screen_height() -> int:
        """wm size でデバイス画面の高さを取得 (portrait 基準)。"""
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "wm", "size"],
                capture_output=True, text=True, timeout=5,
            )
            m = re.search(r"(\d+)x(\d+)", r.stdout)
            if m:
                w, h = int(m.group(1)), int(m.group(2))
                return max(w, h)
        except Exception as e:
            logger.debug("[FRESH_INSTALL] wm size 取得失敗: %s", e)
        return 1920  # 安全なフォールバック (FHD 相当)

    def _is_play_store_foreground() -> bool:
        """Play Store がフォアグラウンドにあるか確認。"""
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "dumpsys", "window", "displays"],
                capture_output=True, text=True, timeout=5,
            )
            return PLAY_STORE_PKG in r.stdout
        except Exception:
            return False

    def _ensure_play_store_open() -> None:
        """Play Store がフォアグラウンドにない場合、再表示する。"""
        if not _is_play_store_foreground():
            logger.warning("[FRESH_INSTALL] Play Store がフォアグラウンドにない → 再表示")
            open_play_store(serial, package)
            time.sleep(5)

    def _uiautomator_dump_xml() -> Optional[str]:
        """uiautomator dump を実行し XML テキストを返す。stale データ防止付き。"""
        try:
            # stale XML を削除してからダンプ
            subprocess.run(
                ["adb", "-s", serial, "shell", "rm", "-f", "/sdcard/ui.xml"],
                capture_output=True, timeout=5,
            )
            dr = subprocess.run(
                ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/ui.xml"],
                capture_output=True, timeout=15, text=True,
            )
            # dump 成功判定: 出力に "dumped to" が含まれる
            if "dumped" not in dr.stdout.lower() and "dumped" not in dr.stderr.lower():
                logger.debug("[FRESH_INSTALL] uiautomator dump 応答なし: stdout=%s stderr=%s",
                             dr.stdout.strip()[:80], dr.stderr.strip()[:80])
                return None
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "cat", "/sdcard/ui.xml"],
                capture_output=True, timeout=10, text=True,
            )
            if r.returncode != 0 or not r.stdout.strip():
                return None
            return r.stdout
        except Exception as e:
            logger.warning("[FRESH_INSTALL] uiautomator dump 失敗: %s", e)
            return None

    def _uiautomator_find_button(keywords: list, xml_text: str = None) -> Optional[tuple]:
        """uiautomator XML からボタン中心座標を取得。

        xml_text が渡された場合はそれを使う (dump 不要)。
        """
        import xml.etree.ElementTree as ET
        if xml_text is None:
            xml_text = _uiautomator_dump_xml()
        if not xml_text:
            return None
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as _pe:
            logger.debug("[FRESH_INSTALL] XML パースエラー: %s", _pe)
            return None

        for kw in keywords:
            for node in root.iter("node"):
                text_val = node.get("text", "")
                desc_val = node.get("content-desc", "")
                bounds_str = node.get("bounds", "")
                matched_text = ""
                if kw in text_val:
                    matched_text = text_val
                elif kw in desc_val:
                    matched_text = desc_val
                else:
                    continue
                # 「インストール」→「アンインストール」除外
                if kw == "インストール" and "アン" in matched_text:
                    continue
                bm = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
                if len(bm) < 2:
                    continue
                x1, y1 = int(bm[0][0]), int(bm[0][1])
                x2, y2 = int(bm[1][0]), int(bm[1][1])
                cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
                logger.info("[FRESH_INSTALL] uiautomator '%s' (text='%s'): "
                            "bounds=[%d,%d][%d,%d] → (%d,%d)",
                            kw, matched_text[:30], x1, y1, x2, y2, cx, cy)
                return (cx, cy)
        return None

    def _log_ui_texts(xml_text: str) -> list[str]:
        """uiautomator XML から全テキストを抽出してログ出力 (診断用)。"""
        import xml.etree.ElementTree as ET
        texts = []
        try:
            root = ET.fromstring(xml_text)
            for node in root.iter("node"):
                t = node.get("text", "").strip()
                if t:
                    texts.append(t)
        except ET.ParseError:
            pass
        if texts:
            logger.info("[FRESH_INSTALL] UI texts: %s", texts[:20])
        return texts

    def _verify_install_started() -> bool:
        """インストール開始を検証 (5秒待ち + 3回リトライ)。"""
        for _vt in range(3):
            time.sleep(5)
            xml = _uiautomator_dump_xml()
            if xml:
                # 1) 進捗系キーワード
                _progress = _uiautomator_find_button(
                    ["インストール中", "キャンセル", "Cancel",
                     "Installing", "Pending", "ダウンロード中", "Downloading"],
                    xml_text=xml)
                if _progress:
                    logger.info("[FRESH_INSTALL] インストール開始を確認 (進捗検出, 試行%d)", _vt + 1)
                    return True
                # 2) 「インストール」ボタン消失
                if _uiautomator_find_button(INSTALL_KEYWORDS, xml_text=xml) is None:
                    logger.info("[FRESH_INSTALL] インストール開始を確認 (ボタン消失, 試行%d)", _vt + 1)
                    return True
            # 3) pm で既にインストール済み
            if is_app_installed(serial, package):
                logger.info("[FRESH_INSTALL] インストール完了を確認 (pm, 試行%d)", _vt + 1)
                return True
            logger.debug("[FRESH_INSTALL] 検証リトライ %d/3", _vt + 1)
        return False

    def _handle_accept_dialogs() -> None:
        """権限/同意ダイアログを最大5回リトライで処理。"""
        for _ad in range(5):
            time.sleep(2)
            xml = _uiautomator_dump_xml()
            if xml:
                _acc_pos = _uiautomator_find_button(ACCEPT_KEYWORDS, xml_text=xml)
                if _acc_pos:
                    logger.info("[FRESH_INSTALL] 権限ダイアログ → タップ (%d,%d) [%d回目]",
                                _acc_pos[0], _acc_pos[1], _ad + 1)
                    _adb_tap(_acc_pos[0], _acc_pos[1])
                    continue
                # ポップアップ解除キーワード
                _dismiss_pos = _uiautomator_find_button(_POPUP_DISMISS_KWS, xml_text=xml)
                if _dismiss_pos:
                    logger.info("[FRESH_INSTALL] ポップアップ解除 → タップ (%d,%d) [%d回目]",
                                _dismiss_pos[0], _dismiss_pos[1], _ad + 1)
                    _adb_tap(_dismiss_pos[0], _dismiss_pos[1])
                    continue
            # uiautomator で見つからない → OCR フォールバック
            tmp_ss = str(Path(tempfile.gettempdir()) / "fresh_install_ss.png")
            if _adb_screenshot(tmp_ss):
                try:
                    ocr2 = run_ocr(tmp_ss)
                    _found = False
                    for akw in ACCEPT_KEYWORDS:
                        ahit = find_best(ocr2, akw)
                        if ahit:
                            ax, ay = ahit["center"]
                            logger.info("[FRESH_INSTALL] OCR「%s」→ タップ (%d,%d)", akw, ax, ay)
                            _adb_tap(ax, ay)
                            _found = True
                            break
                    if _found:
                        continue
                except Exception:
                    pass
            logger.debug("[FRESH_INSTALL] 権限ダイアログなし [%d回目] → 完了", _ad + 1)
            break

    def _try_dismiss_popup(xml: str) -> bool:
        """Play Games / Protect / TOS 等のポップアップを検出して閉じる。
        閉じた場合 True を返す。"""
        # uiautomator でポップアップ閉じるボタンを探す
        _dismiss = _uiautomator_find_button(_POPUP_DISMISS_KWS, xml_text=xml)
        if _dismiss:
            logger.info("[FRESH_INSTALL] ポップアップ検出 → dismiss タップ (%d,%d)",
                        _dismiss[0], _dismiss[1])
            _adb_tap(_dismiss[0], _dismiss[1])
            time.sleep(2)
            return True
        return False

    # ─── 実行開始 ───

    logger.info("[FRESH_INSTALL] === アプリ再インストール開始 ===")

    # --- Step 0: 画面ウェイク + ロック解除 ---
    _adb_key("KEYCODE_WAKEUP")
    time.sleep(1)
    # スワイプでロック解除 (パスワードなし前提)
    subprocess.run(
        ["adb", "-s", serial, "shell", "input", "swipe", "360", "1200", "360", "400", "300"],
        capture_output=True, timeout=5,
    )
    time.sleep(1)

    # --- Step 1: アンインストール ---
    uninstall_app(serial, package)
    time.sleep(2)

    # --- Step 2: Play Store を開く ---
    if not open_play_store(serial, package):
        logger.error("[FRESH_INSTALL] Play Store を開けませんでした。手動で対応してください。")
        return
    time.sleep(5)

    # デバイス画面高さを動的取得 (Y座標フィルタ用)
    _screen_h = _get_device_screen_height()
    logger.info("[FRESH_INSTALL] デバイス画面高さ (portrait): %dpx", _screen_h)

    # --- Step 2.5: Play Store ページ読み込み待機 ---
    # 初回は十分に待つ (BACK 乱発でページロードが途切れるのを防止)
    # uiautomator + OCR 両方で読み込み完了を検出 (Play Store は WebView ベースのため uiautomator が効かない場合がある)
    # ページ読み込み完了: まどドラ関連キーワード + インストール/開くボタン
    _PAGE_READY_KWS = _APP_PAGE_VERIFY_KWS + ["インストール", "Install", "install",
                       "開く", "Open", "プレイ", "Play", "更新", "Update"]
    logger.info("[FRESH_INSTALL] Play Store ページ読み込み待機中...")
    _page_loaded = False
    for _wait in range(6):  # 最大 30 秒 (5秒 × 6)
        # --- uiautomator で確認 ---
        xml = _uiautomator_dump_xml()
        if xml:
            ui_texts = _log_ui_texts(xml)
            if any(kw in t for t in ui_texts for kw in _PAGE_READY_KWS):
                logger.info("[FRESH_INSTALL] ページ読み込み完了 [uiautomator] (%d秒)", (_wait + 1) * 5)
                _page_loaded = True
                break
            if _try_dismiss_popup(xml):
                continue
            if any(kw in t for t in ui_texts for kw in _LOADING_HINTS):
                logger.info("[FRESH_INSTALL] 読み込み中... (%d秒)", (_wait + 1) * 5)
                time.sleep(5)
                continue
        else:
            logger.debug("[FRESH_INSTALL] uiautomator dump 失敗 — OCR フォールバック")
        # --- OCR フォールバック ---
        _wait_ss = str(Path(tempfile.gettempdir()) / "fresh_install_wait.png")
        if _adb_screenshot(_wait_ss):
            try:
                _wait_ocr = run_ocr(_wait_ss)
                _wait_texts = [r["text"] for r in _wait_ocr]
                logger.info("[FRESH_INSTALL] OCR (待機中): %s", _wait_texts[:10])
                if any(kw in t for t in _wait_texts for kw in _PAGE_READY_KWS):
                    logger.info("[FRESH_INSTALL] ページ読み込み完了 [OCR] (%d秒)", (_wait + 1) * 5)
                    _page_loaded = True
                    break
            except Exception:
                pass
        time.sleep(5)

    if not _page_loaded:
        logger.warning("[FRESH_INSTALL] ページ読み込みタイムアウト — インストール検出を開始")
        _save_diag_screenshot("page_load_timeout")

    # --- Step 3: インストールボタン検出 + タップ ---
    tmp_ss = str(Path(tempfile.gettempdir()) / "fresh_install_ss.png")
    installed_via_tap = False
    _reinstall_count = 0

    for attempt in range(MAX_ATTEMPTS):
        # 待機時間エスカレーション: 試行5回目以降は長めに待つ
        if attempt > 0:
            _wait_sec = 3 if attempt < 5 else 5 if attempt < 10 else 8
            time.sleep(_wait_sec)

        logger.info("[FRESH_INSTALL] 試行 %d/%d", attempt + 1, MAX_ATTEMPTS)

        # Play Store がフォアグラウンドにあるか確認
        _ensure_play_store_open()

        # uiautomator dump (1回だけ取得して使い回す)
        xml = _uiautomator_dump_xml()

        if xml:
            ui_texts = _log_ui_texts(xml)

            # --- 0th-pre: ダウンロード進行中チェック (再タップ防止) ---
            _dl_progress_kws = ["キャンセル", "Cancel", "インストール中", "Installing",
                                "Pending", "ダウンロード中", "Downloading", "待機中"]
            _is_downloading = any(
                kw in t for t in ui_texts for kw in _dl_progress_kws
            )
            if _is_downloading:
                logger.info("[FRESH_INSTALL] ダウンロード進行中 (uiautomator) → タップせず待機")
                if _verify_install_started():
                    installed_via_tap = True
                    break
                continue

            # --- 0th: 既インストール済みチェック ---
            _ui_open = _uiautomator_find_button(OPEN_KEYWORDS, xml_text=xml)
            if _ui_open:
                if _reinstall_count >= MAX_REINSTALL:
                    logger.warning("[FRESH_INSTALL] 再アンインストール上限(%d回)到達 → BACK + 再表示",
                                   MAX_REINSTALL)
                    _adb_key("4")
                    time.sleep(2)
                    open_play_store(serial, package)
                    time.sleep(5)
                    continue
                logger.info("[FRESH_INSTALL] 既インストール済み（'プレイ'/'開く'検出）→ 再アンインストール (%d/%d)",
                            _reinstall_count + 1, MAX_REINSTALL)
                _reinstall_count += 1
                uninstall_app(serial, package)
                time.sleep(2)
                open_play_store(serial, package)
                time.sleep(5)
                continue

            # --- 1st: uiautomator でインストールボタン (まどドラページ確認) ---
            _ui_on_app_page = any(
                kw in t for t in ui_texts for kw in _APP_PAGE_VERIFY_KWS
            )
            _ui_pos = _uiautomator_find_button(INSTALL_KEYWORDS, xml_text=xml)
            if _ui_pos and not _ui_on_app_page:
                logger.warning("[FRESH_INSTALL] uiautomator: まどドラページではない → スキップ")
                _ui_pos = None
            if _ui_pos:
                if _ui_pos[1] < _screen_h * 0.75:
                    _adb_tap(_ui_pos[0], _ui_pos[1])
                    if _verify_install_started():
                        installed_via_tap = True
                        break
                    logger.warning("[FRESH_INSTALL] uiautomator タップ空振り — OCR フォールバック")
                else:
                    logger.debug("[FRESH_INSTALL] uiautomator (%d,%d) y>75%% → 他端末ボタン除外",
                                 _ui_pos[0], _ui_pos[1])

            # --- ポップアップ / ダイアログを閉じる試行 ---
            if _try_dismiss_popup(xml):
                continue

        # --- 2nd: OCR フォールバック ---
        if not _adb_screenshot(tmp_ss):
            logger.warning("[FRESH_INSTALL] スクリーンショット取得失敗 — リトライ")
            continue

        try:
            ocr_results = run_ocr(tmp_ss)
        except Exception as e:
            logger.warning("[FRESH_INSTALL] OCR 失敗: %s", e)
            continue

        _ocr_texts = [r["text"] for r in ocr_results]
        logger.info("[FRESH_INSTALL] OCR texts: %s", _ocr_texts[:15])

        # --- ダウンロード/インストール進行中チェック (OCR) ---
        # NOTE: 「プレイ」チェックより先に実行する。インストール中に「プレイ」が
        # 一時的に表示される場合があり、先にチェックすると誤って再アンインストールしてしまう。
        _DL_PROGRESS_OCR_KWS = ["キャンセル", "Cancel", "MB", "ダウンロード中",
                                "インストール中", "Installing", "Downloading"]
        _ocr_downloading = any(
            kw in t for t in _ocr_texts for kw in _DL_PROGRESS_OCR_KWS
        )
        if _ocr_downloading:
            logger.info("[FRESH_INSTALL] ダウンロード進行中 (OCR) → タップせず待機")
            if _verify_install_started():
                installed_via_tap = True
                break
            continue

        # 「開く」「プレイ」検出 → pm 確認 → 再アンインストール
        _ocr_open_hit = False
        for kw in _OCR_OPEN_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                if is_app_installed(serial, package):
                    if _reinstall_count >= MAX_REINSTALL:
                        logger.warning("[FRESH_INSTALL] 再アンインストール上限到達 → スキップ")
                    else:
                        logger.info("[FRESH_INSTALL] 「%s」+ pm確認 → 再アンインストール (%d/%d)",
                                    kw, _reinstall_count + 1, MAX_REINSTALL)
                        _reinstall_count += 1
                        uninstall_app(serial, package)
                        time.sleep(2)
                        open_play_store(serial, package)
                        time.sleep(5)
                        _ocr_open_hit = True
                else:
                    logger.info("[FRESH_INSTALL] 「%s」検出だがpm未確認 → 継続", kw)
                break
        if _ocr_open_hit:
            continue

        # --- エラーダイアログ検出 (「インストールできません」) → BACK + 再表示 ---
        _install_error = any("できません" in t for t in _ocr_texts)
        if _install_error:
            logger.info("[FRESH_INSTALL] エラーダイアログ検出 → BACK + Play Store 再表示")
            _save_diag_screenshot(f"install_error_{attempt}")
            _adb_key("4")  # BACK
            time.sleep(2)
            open_play_store(serial, package)
            time.sleep(5)
            continue

        # 「インストール」を OCR 検出 — まどドラのページであることを確認してからタップ
        _on_app_page = any(
            kw in t for t in _ocr_texts for kw in _APP_PAGE_VERIFY_KWS
        )
        if not _on_app_page:
            logger.warning("[FRESH_INSTALL] まどドラのページではない (OCR: %s) → Play Store 再表示",
                           _ocr_texts[:5])
            _adb_key("4")  # BACK
            time.sleep(2)
            open_play_store(serial, package)
            time.sleep(5)
            continue

        _install_found = False
        for kw in INSTALL_KEYWORDS:
            hit = find_best(ocr_results, kw)
            if hit:
                # 「アンインストール」「インストールできません」除外
                if "アン" in hit["text"] or "できません" in hit["text"]:
                    continue
                cx, cy = hit["center"]
                logger.info("[FRESH_INSTALL] OCR「%s」検出 → タップ (%d,%d)", kw, cx, cy)
                _adb_tap(cx, cy)
                if _verify_install_started():
                    installed_via_tap = True
                _install_found = True
                break
        if installed_via_tap:
            break
        if _install_found:
            continue  # タップしたが検証失敗 → リトライ

        # インストールボタン未検出 — BACK は最終手段 (試行 8回目以降のみ)
        if attempt >= 7:
            logger.info("[FRESH_INSTALL] インストールボタン未検出 → BACK + Play Store 再表示")
            _save_diag_screenshot(f"no_install_btn_{attempt}")
            _adb_key("4")
            time.sleep(2)
            open_play_store(serial, package)
            time.sleep(5)
        else:
            logger.info("[FRESH_INSTALL] インストールボタン未検出 — ページ読み込み待機 (試行%d)", attempt + 1)
            _save_diag_screenshot(f"waiting_{attempt}")

    # --- 権限ダイアログ処理 ---
    if installed_via_tap:
        _handle_accept_dialogs()
    else:
        logger.error("[FRESH_INSTALL] %d回の試行でインストールボタンをタップできませんでした", MAX_ATTEMPTS)
        _save_diag_screenshot("final_failure")
        # 最終手段: pm install-existing (キャッシュ残りで復活することがある)
        logger.info("[FRESH_INSTALL] pm install-existing を試行...")
        subprocess.run(
            ["adb", "-s", serial, "shell", "pm", "install-existing", package],
            capture_output=True, timeout=30,
        )

    # --- Step 4: インストール完了ポーリング ---
    if not is_app_installed(serial, package):
        logger.info("[FRESH_INSTALL] インストール完了を待機中... (最大%d秒)",
                    POLL_INTERVAL_SEC * MAX_POLL_COUNT)
        for i in range(MAX_POLL_COUNT):
            if is_app_installed(serial, package):
                logger.info("[FRESH_INSTALL] インストール完了を確認 (%d秒経過)",
                            (i + 1) * POLL_INTERVAL_SEC)
                break
            # 30秒毎にダイアログチェック (Play Protect 等がインストールを止めている可能性)
            if (i + 1) % 6 == 0:
                xml = _uiautomator_dump_xml()
                if xml:
                    _try_dismiss_popup(xml)
                    _handle_accept_dialogs()
            time.sleep(POLL_INTERVAL_SEC)
        else:
            logger.error("[FRESH_INSTALL] タイムアウト — 手動でインストールを完了してください")
            _save_diag_screenshot("install_timeout")
            return
    else:
        logger.info("[FRESH_INSTALL] 既にインストール完了済み")

    # --- Step 5: Play Store を閉じる ---
    time.sleep(2)
    subprocess.run(
        ["adb", "-s", serial, "shell", "am", "force-stop", PLAY_STORE_PKG],
        capture_output=True, timeout=5,
    )
    logger.info("[FRESH_INSTALL] Play Store を閉じました")
    time.sleep(2)
    logger.info("[FRESH_INSTALL] === 再インストール完了 → 通常起動シーケンスへ ===")


# ─── メインループ ─────────────────────────────────
def main():
    import tools.ap.constants as _ap_const  # _DEBUG_SAVE_IMAGES 直接書換え用
    from tools.ap.mission import select_mission

    args = parse_args()
    mission = select_mission(args)
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        _ap_const._DEBUG_SAVE_IMAGES = True

    # ─── ADB 自動接続: USB → Wi-Fi フォールバック ───
    try:
        _detected = ensure_adb_connection(
            wifi_addr=args.wifi_addr or WIFI_DEVICE_ADDR,
            pairing_code=args.pairing_code,
            pairing_port=args.pairing_port,
        )
        if not os.environ.get("ANDROID_UDID") and not os.environ.get("ANDROID_SERIAL"):
            os.environ["ANDROID_UDID"] = _detected
        _serial = get_android_serial()
        set_device_serial(_serial)
        set_scrcpy_device(_serial)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # ─── scrcpy 早期起動: fresh-install 含めた全工程を画面で確認可能にする ───
    _scrcpy_proc = manage_scrcpy()
    if _scrcpy_proc is not None:
        logger.info("[SCRCPY] ウィンドウ生成待ち (3秒)...")
        time.sleep(3)

    # ─── --fresh-install: アンインストール → Play Store 再インストール ───
    if args.fresh_install:
        # 永続状態をクリア (新規アカウントでは前回の状態は無効)
        try:
            conn = sqlite3.connect(str(_STATE_DB_PATH))
            conn.execute("DELETE FROM auto_pilot_state")
            conn.commit()
            conn.close()
            logger.info("[PERSIST] fresh-install → auto_pilot_state テーブルクリア")
        except Exception:
            pass
        _fresh_install_from_play_store(_ap_device.DEVICE_SERIAL, APP_PACKAGE)

    # ─── ゲーム未インストール → 自動インストール ───
    if not is_app_installed(_ap_device.DEVICE_SERIAL, APP_PACKAGE):
        logger.info("[AUTO_INSTALL] ゲーム '%s' 未インストール → Play Store から自動インストール", APP_PACKAGE)
        _fresh_install_from_play_store(_ap_device.DEVICE_SERIAL, APP_PACKAGE)
        if not is_app_installed(_ap_device.DEVICE_SERIAL, APP_PACKAGE):
            logger.error("[ABORT] 自動インストール失敗。手動でインストールしてから再実行してください。")
            sys.exit(1)
        # 新規インストール = fresh start 扱い
        args.fresh_install = True

    logger.info("=" * 62)
    logger.info("  まどドラ自律操縦 — Auto Pilot (ハイブリッド版)")
    logger.info("  デバイス: %s", _ap_device.DEVICE_SERIAL)
    logger.info("  ミッション: %s", mission.banner_info())
    logger.info("  ポーリング: %.1fs  強制解析: %d回変化なし  スタックTimeout: %.0fs",
                POLL_INTERVAL, FORCE_ANALYZE_AFTER, STALL_TIMEOUT)
    logger.info("=" * 62)

    state = PilotState()
    mission.configure_state(state, args)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

    # ── SQLite から永続状態を復元 ──
    if load_state("post_download") == "1":
        state.post_download = True
        logger.info("[PERSIST] post_download=True を復元 (前回 DL 中断からの再開)")

    # ─── Ctrl+C シグナルハンドラ登録 (レポート自動生成) ───
    global _pilot_state_ref
    _pilot_state_ref = state

    def _sigint_handler(signum, frame):
        logger.info("\n[Ctrl+C] 手動停止 — レポートを生成します...")
        generate_and_copy_report(_pilot_state_ref, "手動停止 (Ctrl+C / SIGINT)")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    # ─── scrcpy 再確認: fresh-install 中に死んだ場合のリカバリ ───
    if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
        logger.info("[SCRCPY] fresh-install 中に終了 → 再起動")
        _scrcpy_proc = manage_scrcpy()
        if _scrcpy_proc is not None:
            time.sleep(3)

    # 実機物理解像度を取得してログ出力
    _dev_w, _dev_h = get_device_resolution()
    logger.info("[DEVICE_RES] wm size: %dx%d / 解析基準: %dx%d (ROI補正で座標変換)",
                _dev_w, _dev_h, ANALYSIS_W, ANALYSIS_H)

    # ─── 初回アプリ起動: mCurrentFocus で正確な前面アプリ判定 ───
    # NOTE: mResumedActivity はバックグラウンドスタックも含む複数行を返すことがあり
    #       誤って「既に前面」と判定する問題があった。mCurrentFocus は1行のみ確実。
    try:
        adb("shell input keyevent KEYCODE_WAKEUP")
        time.sleep(0.5)
        # ロック画面をスワイプ解除 (PIN/パターンなし端末)
        adb("shell input keyevent 82")  # KEYCODE_MENU でロック解除
        time.sleep(0.5)
        adb("shell input swipe 540 1800 540 500 300")  # スワイプ解除
        time.sleep(0.5)
        _focus = adb("shell dumpsys window | grep mCurrentFocus")
        if APP_PACKAGE not in _focus:
            logger.info("[STARTUP] アプリが前面にない → am start で起動します (mCurrentFocus: %s)",
                        _focus.strip())
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
            # ポーリングで起動確認 (mCurrentFocus が空の端末もあるため ps も併用)
            _app_started = False
            for _poll in range(10):
                time.sleep(2)
                _focus2 = adb("shell dumpsys window | grep mCurrentFocus")
                if APP_PACKAGE in _focus2:
                    logger.info("[STARTUP] アプリ前面確認 (%.1f秒)", (_poll + 1) * 2)
                    _app_started = True
                    break
                # mCurrentFocus が空でもプロセスが起動していれば OK
                _ps = adb(f"shell pidof {APP_PACKAGE}")
                if _ps.strip():
                    logger.info("[STARTUP] アプリプロセス検出 PID=%s (%.1f秒)",
                                _ps.strip(), (_poll + 1) * 2)
                    _app_started = True
                    break
                logger.info("[STARTUP] 起動待ち (%d/10)... mCurrentFocus: %s",
                            _poll + 1, _focus2.strip())
            if not _app_started:
                # 10回失敗 → 再度 am start (Play Store 終了直後のタイミング問題対策)
                logger.warning("[STARTUP] 10回待機後もアプリ未検出 → am start を再試行")
                adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
                time.sleep(5)
        else:
            logger.info("[STARTUP] アプリ既に前面: %s", APP_PACKAGE)
    except Exception as _e:
        logger.warning("[STARTUP] フォーカス確認失敗: %s — am start で起動を試行", _e)
        adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(5)

    _log_milestone(state, "APP_LAUNCH")
    logger.info("[TOKEN_SAVE] 節約モード稼働中。バトル発光検知で OCR スキップ → 爆速モードで進行します")

    # ─── ランドスケープ待機: ポートレートならアプリ起動待ち ───
    for _orient_wait in range(10):
        _ss_check = take_screenshot()
        if _ss_check[0] is not None and _ss_check[1] > _ss_check[2]:
            logger.info("[STARTUP] ランドスケープ確認 (%dx%d)", _ss_check[1], _ss_check[2])
            break
        logger.info("[STARTUP] ポートレート検出 (%dx%d) — アプリ起動待ち (%d/10)",
                    _ss_check[1], _ss_check[2], _orient_wait + 1)
        if _orient_wait == 4:
            logger.info("[STARTUP] アプリ再起動を試行")
            adb(f"shell am force-stop {APP_PACKAGE}")
            time.sleep(2)
            adb(f"shell am start -n '{APP_PACKAGE}/{APP_ACTIVITY}'")
        time.sleep(2)

    i = 0
    while True:
        state.iteration = i
        _loop_t0 = time.time()  # [PERF] ループ開始時刻
        clear_imread_cache()    # 前イテレーションのキャッシュを破棄

        # ── 定期健診 (100 iter ごと) ──
        if i > 0 and i % 100 == 0:
            logger.info("[WATCHDOG] Periodic check (iter=%d). Running physical diagnostics...", i)
            if not check_adb_liveness():
                logger.warning("[WATCHDOG] Periodic check FAILED → attempting reconnect")
                subprocess.run(["adb", "kill-server"], timeout=5)
                time.sleep(2)
                subprocess.run(["adb", "start-server"], timeout=5)
                time.sleep(2)
                # USB シリアル (`:` なし) には adb connect 不要 (DNS解決失敗する)
                if _ap_device.DEVICE_SERIAL and ":" in _ap_device.DEVICE_SERIAL:
                    subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5)
                    time.sleep(1)
                # scrcpy が ADB 再起動で死んだ場合は再起動
                if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] WATCHDOG ADB reconnect 後に再起動")
                    _scrcpy_proc = manage_scrcpy()
            else:
                logger.info("[WATCHDOG] Periodic check OK")

        # ── フォアグラウンドアプリ監視 (20 iter 毎) ──
        # Chrome 等が誤起動していたら am start でゲームに復帰
        if i > 0 and i % 20 == 0:
            if check_foreground_app():
                logger.info("[FOREGROUND] ゲーム復帰完了 → 次イテレーションへ")
                state.last_phash = ""
                state.same_phash_count = 0
                continue

        # ── 0.5) キャプチャ前クールダウン ──
        # a) タップ後: MIN_TAP_INTERVAL 未満なら残り時間を待つ
        #    安全弁: 最大 3.0s キャップ (不具合で無限停止しない)
        if state.last_action_time > 0:
            _cooldown_remaining = MIN_TAP_INTERVAL - (time.time() - state.last_action_time)
            if 0 < _cooldown_remaining <= 3.0:
                time.sleep(_cooldown_remaining)
        # b) キャプチャ間隔: MIN_CAPTURE_INTERVAL 未満なら残り時間を待つ
        #    scrcpy 高速キャプチャ (~100ms) でのCPU/メモリ浪費を抑制
        if state.last_capture_time > 0:
            _cap_remaining = MIN_CAPTURE_INTERVAL - (time.time() - state.last_capture_time)
            if 0 < _cap_remaining <= 3.0:
                time.sleep(_cap_remaining)

        # ── 1) スクリーンショット取得 ──
        img_path, actual_w, actual_h, _ss_retries = take_screenshot()
        state.last_capture_time = time.time()
        state.screenshot_retry_count += _ss_retries
        if _ss_retries > 0:
            logger.info("[SCREENSHOT] 破損リトライ %d回 (累計: %d回)",
                        _ss_retries, state.screenshot_retry_count)
        # ── Wi-Fi 破損防御: 全リトライ失敗 → continue で次ループ ──
        if img_path is None:
            state.wifi_fail_streak += 1
            logger.warning("[WIFI_ERROR] 連続失敗 %d/5 — 次ループで再取得",
                           state.wifi_fail_streak)
            if state.wifi_fail_streak >= 5:
                logger.error("[WIFI_ERROR] 連続5回失敗 → ADB再接続を試行")
                try:
                    subprocess.run(["adb", "disconnect"], timeout=5, capture_output=True)
                    time.sleep(1)
                    subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5, capture_output=True)
                    time.sleep(2)
                except Exception as _rc_e:
                    logger.error("[WIFI_ERROR] ADB再接続例外: %s", _rc_e)
                state.wifi_fail_streak = 0
            time.sleep(1.0)
            continue
        state.wifi_fail_streak = 0  # 成功時リセット
        # ── Portrait 検出: ブラウザ等の外部アプリ → BACK キーで復帰 ──
        if actual_w > 0 and actual_w < actual_h:
            logger.warning("[PORTRAIT] 縦画面 (%dx%d) → BACK キー", actual_w, actual_h)
            adb("shell input keyevent 4")  # KEYCODE_BACK
            time.sleep(1.5)
            state.last_phash = ""
            continue
        # メモリ上に最新画像を保持 + ROI更新 (スロットル: 画面変化時 or 50iter毎)
        try:
            _cached_bgr = pop_last_scrcpy_bgr()
            state.last_screen = _cached_bgr if _cached_bgr is not None else imread_cached(img_path)
            _roi_needed = (state.game_roi is None or i % 50 == 0
                           or state.same_phash_count == 0)  # phash変化直後
            if state.last_screen is not None and _roi_needed:
                _new_roi = detect_game_roi(state.last_screen)
                # 非黒画面のときのみ ROI を更新 (暗転中は前の ROI を維持)
                _img_h, _img_w = state.last_screen.shape[:2]
                if _new_roi[2] >= _img_w * 0.5:
                    # 画像空間 → 解析空間に正規化
                    # NOTE: _img_w は scrcpy (~1440) or adb (2160) で異なるため
                    #        actual_w (wm size) ではなく実画像サイズを使う
                    if _img_w != ANALYSIS_W or _img_h != ANALYSIS_H:
                        _sx = ANALYSIS_W / _img_w
                        _sy = ANALYSIS_H / _img_h
                        _new_roi = (
                            int(_new_roi[0] * _sx), int(_new_roi[1] * _sy),
                            int(_new_roi[2] * _sx), int(_new_roi[3] * _sy),
                        )
                    if _new_roi != state.game_roi:
                        logger.info("[ROI] ゲーム描画領域更新: x=%d y=%d w=%d h=%d (黒帯: L=%d R=%d T=%d B=%d)",
                                    _new_roi[0], _new_roi[1], _new_roi[2], _new_roi[3],
                                    _new_roi[0], ANALYSIS_W - _new_roi[0] - _new_roi[2],
                                    _new_roi[1], ANALYSIS_H - _new_roi[1] - _new_roi[3])
                        state.game_roi = _new_roi
        except Exception as _e:
            logger.debug("[ROI] detect_game_roi 例外: %s", _e)
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

        # ── 1.5) フォアグラウンドアプリ監視 (20iter毎 ≈ ~40秒) ──
        if i > 0 and i % 20 == 0:
            if check_foreground_app():
                logger.info("[FOREGROUND_GUARD] 別アプリ検出 → ゲーム復帰、次ループへ")
                state.last_phash = ""
                time.sleep(3.0)
                continue

        # ── 2) 暗転検出 ──
        if is_dark_screen(img_path):
            # チュートリアルウォーク（暗い廊下）は暗転ではない → スワイプで進行
            # バトル等の暗い画面で誤検出しないようテンプレートで除外
            _is_walk = False
            # post_download 後はお知らせポップアップ等の暗背景を TutorialWalk と誤検出するため除外
            if (state.consecutive_blackouts >= 10
                    and not state.post_download
                    and is_tutorial_walk_scene(img_path)):
                _walk_analysis = prepare_analysis_image(img_path, actual_w, actual_h)
                _battle_roi = BATTLE_BTN_ROI
                _has_battle = any(
                    (m := ASSET_MANAGER.match_single(b, _walk_analysis, roi=_battle_roi))
                    and m[2] >= 0.50
                    for b in ("battle_normal_attack", "battle_skill", "battle_special")
                )
                if not _has_battle:
                    _is_walk = True
            if _is_walk:
                logger.info("[iter %d] 暗転→TutorialWalk検出 → スワイプで進行", i)
                _walk_sx, _walk_sy = roi_to_device(
                    int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.89), state.game_roi)
                _walk_ex, _walk_ey = roi_to_device(
                    int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.07), state.game_roi)
                swipe_device(_walk_sx, _walk_sy, _walk_ex, _walk_ey, 10000,
                             state=state, desc="TutorialWalk_UP")
                state.consecutive_blackouts = 0
                state.last_phash = ""
                continue
            state.total_blackout_skipped += 1
            state.consecutive_blackouts += 1
            if state.total_blackout_skipped % 5 == 1:
                logger.info("[iter %d] 暗転 — 3s 待機 (連続: %d)",
                            i, state.consecutive_blackouts)
            # ── 暗転復帰 ──
            # 連続30回超 → スリープ延長 (0.5s→3.0s) + フォアグラウンドチェック
            if state.consecutive_blackouts >= 30:
                if state.consecutive_blackouts % 10 == 0:
                    # 画面消灯の可能性 → フォアグラウンドチェック + WAKEUP
                    if check_foreground_app():
                        logger.info("[BLACKOUT_RECOVER] 別アプリ前面 → ゲーム復帰")
                    else:
                        logger.info("[BLACKOUT_RECOVER] 連続暗転 %d 回 → WAKEUP + ロック解除 + 画面中央タップ",
                                    state.consecutive_blackouts)
                        adb("shell input keyevent KEYCODE_WAKEUP")
                        time.sleep(0.5)
                        adb("shell input keyevent 82")  # KEYCODE_MENU = ロック解除
                        time.sleep(0.5)
                        adb("shell input swipe 540 1800 540 500 300")  # スワイプ解除
                        time.sleep(0.5)
                        tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "BLACKOUT_RECOVER")
                else:
                    time.sleep(3.0)  # 暗転ポーリング延長 (コールドスタート最適化)
            else:
                time.sleep(0.5)
            state.last_phash = ""
            state.same_phash_count = 0
            continue

        # ── 暗転解除 ──
        if state.consecutive_blackouts > 0:
            state.consecutive_blackouts = 0

        # ── 2.5) ダウンロード/ロード中ショートカット ──
        # 前回アクションが DOWNLOAD_WAIT/LOADING_WAIT → phash/シーン判定をスキップ
        # phash だけ更新して detect_and_act へ直行 (DL 完了判定は detect_and_act 内で行う)
        _dl_force_ocr = False
        if state.last_action in ("DOWNLOAD_WAIT", "LOADING_WAIT", "MAIN_STORY_LOADING"):
            try:
                cur_phash = compute_phash(img_path)
            except Exception:
                cur_phash = ""
            if state.last_phash and cur_phash:
                dist = phash_distance(state.last_phash, cur_phash)
            else:
                dist = 999
            state.last_phash_dist = dist
            if dist < PHASH_THRESHOLD:
                # 画面変化なし → DL/ロード継続中、解析スキップ
                state.same_phash_count += 1
                state.last_phash = cur_phash
                state.last_screen_change_time = time.time()  # Watchdog抑制
                # ── 10回(30秒)変化なし → DL完了/失敗ダイアログの可能性。OCR解析へ ──
                if state.same_phash_count >= 10:
                    logger.info("[iter %d] DL/ロード中: %d回変化なし → 完了ダイアログ確認のため通常解析へ",
                                i, state.same_phash_count)
                    # DL完了は自動遷移 or 完了ダイアログで検出する (強制解除は行わない)
                    state.same_phash_count = 0
                    _dl_force_ocr = True   # 完了ダイアログ即検出のため強制 OCR
                    # fall through to detect_and_act
                else:
                    logger.debug("[iter %d] DL/ロード中: phash変化なし(dist=%d) → 3秒待機", i, dist)
                    time.sleep(3.0)
                    _fms = (time.time() - _loop_t0) * 1000
                    state.total_loop_ms += _fms
                    continue
            # 画面変化あり → DL 完了の可能性。通常フローで判定
            logger.info("[iter %d] DL/ロード中: 画面変化(dist=%d) → 通常解析へ", i, dist)
            state.last_phash = cur_phash
            # DLショートカットで既に phash 計算済み → 通常 phash 計算をスキップ
            state.last_phash_dist = dist
            screen_changed = dist >= PHASH_THRESHOLD or _dl_force_ocr
            state.same_phash_count = 0
            _dl_shortcut_fell_through = True
        else:
            _dl_shortcut_fell_through = False

        if not _dl_shortcut_fell_through:
            # ── 3) phash 粗解析 ──
            try:
                cur_phash = compute_phash(img_path)
            except Exception:
                cur_phash = ""

            if state.last_phash and cur_phash:
                dist = phash_distance(state.last_phash, cur_phash)
            else:
                dist = 999
            state.last_phash_dist = dist

            screen_changed = dist >= PHASH_THRESHOLD

        # ── ダウンロード保護フラグ: phash変化だけではクリアしない ──
        # DL進行中もゲージ更新でphash変化20-38が発生するため、phashベースの解除は不適切。
        # download_active のクリアは OCR でDLテキストが消えた場合のみ (detect_and_act 内)。

        # ── 動的しきい値: Gold UI アクション後はアニメーション変化でも即解析 ──
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

        # ── 早期シーン判定 ──
        # OCR 前にシーンを分類し、シーン別ハンドラへルーティングする。
        # MOVIE: 指/GoldSwipe 誤発動を防止
        # BATTLE: GoldSwipe/ADV チェックをスキップ
        # ADV: 指/GoldSwipe/バトル判定をスキップ
        # UNKNOWN: フルOCR → detect_and_act() (既存フロー)
        _early_analysis = prepare_analysis_image(img_path, actual_w, actual_h)
        _early_scene = detect_scene_early(_early_analysis, state, dist)
        _skip_rapid = False  # True: 早期ハンドラがフォールスルー → インライン RAPID をスキップ
        # SCENE_EARLY が UNKNOWN → ポップアップ等で前シーンが無効化された
        # state.current_scene を UNKNOWN にリセットして BATTLE_RAPID を阻止
        if _early_scene == "UNKNOWN" and state.current_scene == "BATTLE":
            logger.info("[SCENE_EARLY] BATTLE→UNKNOWN 遷移 → BATTLE_RAPID 中断, OCR へ")
            state.current_scene = "UNKNOWN"
            state.battle_rapid_consecutive.reset()
            state._from_battle = True  # ダイアログ誤検出ガード用
        # ADV 連続検出カウンタ (phash 動的拡大用)
        if _early_scene == "ADV":
            state.adv_confirmed_count += 1
        elif _early_scene not in ("UNKNOWN",):
            state.adv_confirmed_count = 0
            state.adv_early_consecutive = 0  # ADV 以外のシーン → カウンタリセット

        # MOVIE→別シーン遷移時にカウンタリセット
        if _early_scene != "MOVIE" and state.current_scene == "MOVIE":
            state.movie_wait_consecutive = 0
            state.movie_static_count = 0
            state._movie_stable_count = 0

        if _early_scene == "TUTORIAL_WALK":
            # チェッカー床シーン: OCR不要、即スワイプ
            state.current_scene = "UNKNOWN"
            _walk_sx = int(ANALYSIS_W * 0.5)
            _walk_fy = int(ANALYSIS_H * 0.89)
            _walk_ty = int(ANALYSIS_H * 0.07)
            swipe_device(_walk_sx, _walk_fy, _walk_sx, _walk_ty, 10000,
                         state=state, desc="TutorialWalk_UP")
            state.last_phash = ""
            _fms = (time.time() - _loop_t0) * 1000
            state.total_loop_ms += _fms
            logger.info("  [PERF] Loop %.0fms (TUTORIAL_WALK_EARLY)", _fms)
            continue

        if _early_scene == "MOVIE":
            if state.current_scene == "BATTLE":
                state.character_selected = False
                state.battle_rapid_consecutive.reset()
                logger.debug("[SCENE_CHANGE_EARLY] BATTLE→MOVIE: バトルフラグリセット")
            state.current_scene = "MOVIE"
            if handle_movie(img_path, state, dist, cur_phash):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (MOVIE_EARLY)", _fms)
                continue

        elif _early_scene == "GACHA":
            # ガチャ演出: 画面中央タップで1つずつキャラ表示
            state.current_scene = "GACHA"
            tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state, "GACHA_TAP")
            logger.info("[GACHA] 画面タップで演出進行")
            state.last_phash = ""
            time.sleep(1.5)
            _fms = (time.time() - _loop_t0) * 1000
            state.total_loop_ms += _fms
            logger.info("  [PERF] Loop %.0fms (GACHA)", _fms)
            continue

        elif _early_scene == "BATTLE":
            state.current_scene = "BATTLE"
            state._from_battle = False  # BATTLE復帰でフラグクリア
            _early_analysis = prepare_analysis_image(img_path, actual_w, actual_h)
            if handle_battle(_early_analysis, state, dist):
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_EARLY)", _fms)
                continue
            _skip_rapid = True  # BATTLE ハンドラがフォールスルー → OCR へ直行

        elif _early_scene == "ADV":
            # ADV_EARLY スタック脱出: 15回連続ハンドル成功 → OCR フォールスルー
            _ADV_EARLY_STALL = 15
            if state.adv_early_consecutive >= _ADV_EARLY_STALL:
                logger.warning("[ADV_EARLY] %d 回連続 → OCR フォールスルー",
                               state.adv_early_consecutive)
                state.adv_early_consecutive = 0
                state.adv_confirmed_count = 0
                state.current_scene = "UNKNOWN"
                _skip_rapid = True
            elif handle_adv(_early_analysis, state, dist, cur_phash, actual_w, actual_h):
                state.adv_early_consecutive += 1
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (ADV_EARLY) [%d/%d]",
                            _fms, state.adv_early_consecutive, _ADV_EARLY_STALL)
                continue
            else:
                state.adv_early_consecutive = 0
                _skip_rapid = True  # ADV ハンドラがフォールスルー → OCR へ直行

        if screen_changed:
            # 画面変化あり → カウンタリセット & Watchdog タイマーリセット
            state.same_phash_count = 0
            state.consecutive_frozen_frames = 0
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.pre_popup_tap_count = 0  # ポップアップ試行カウンタリセット
            state.pending_candidates = []  # 候補リストクリア
            state.pending_candidate_idx = 0
            # 指スワイプ判定: MOYA_TAP 後の微小変化 (dist<PHASH_THRESHOLD) では
            # リセットしない。チェック柄シーンのアニメーション中に永久リセットされる問題を防止。
            if not (state.last_action == "MOYA_TAP" and dist < PHASH_THRESHOLD):
                state.finger_tap_static.reset()
            elif state.last_action == "MOYA_TAP":
                state.finger_tap_static.tick()  # 微小変化でもタップ静止としてカウント
            state.last_screen_change_time = time.time()  # Watchdog: 最終変化時刻更新

            # ── テレメトリ: アクション→画面変化の遷移時間を記録 ──
            if state.last_action_time > 0:
                _trans_sec = time.time() - state.last_action_time
                if len(state.transition_times) >= _TRANSITION_HISTORY_MAX:
                    state.transition_times.pop(0)
                state.transition_times.append(_trans_sec)
                if _trans_sec > _TRANSITION_SLOW_SEC:
                    logger.debug(
                        "[TELEMETRY] SLOW transition %.1fs after '%s'",
                        _trans_sec, state.last_action)

            # ── ADV 高速モード: OCR スキップして画面下部を即連打 ──
            # 前回 STORY_TAP かつ phash 変化が小さい（テキスト送り）→ 即タップ
            # MENU シーンはホームチュートリアル中の可能性があるため除外（指/金枠をOCRで確認）
            # ADV 連続3回以上確認 → phash 上限拡大 (背景変更でも OCR スキップ)
            _adv_phash_max = 40 if state.adv_confirmed_count >= 3 else ADV_RAPID_PHASH_MAX
            if (not _skip_rapid and
                    not state.download_active and
                    state.last_action in ("STORY_TAP", "ADV_RAPID_TAP", "ADV_NEXT_TAP", "ADV_WAIT",
                                      "ADV_NEXT_FALLBACK", "ADV_SKIP_TAP",
                                      "STORY_TAP_HINT", "BUBBLE_TAP",
                                      "MINI_CONV_TAP", "MOYA_TAP",
                                      "ANIM_WAIT", "SCENE_TAP") and
                    PHASH_THRESHOLD <= dist <= _adv_phash_max and
                    state.current_scene not in ("MENU", "BATTLE", "MOVIE")):
                _rapid_adv = detect_adv_scene(img_path, roi=state.game_roi)
                # ── ADV↓アイコン検出 → 1回タップ ──
                # NOTE: detect_adv_advance_icon() 単独ではバトル画面の「通常攻撃」
                # ボタン領域の明るいピクセルを↓と誤検出するため、ADVツールバー判定
                # (is_adv) を必須条件にする。↓単独ではADVに入らない。
                _adv_tap_x = int(ANALYSIS_W * 0.93)
                _adv_tap_y = int(ANALYSIS_H * 0.91)
                if _rapid_adv.is_adv:
                    if ASSET_MANAGER.match_single("adv_next_btn", _early_analysis, roi=ADV_NEXT_BTN_ROI):
                        logger.info("[ADV_RAPID][iter %d] ↓検出 → タップ (%d,%d)",
                                    i, _adv_tap_x, _adv_tap_y)
                        tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                        state.last_action = "ADV_RAPID_TAP"
                    elif _rapid_adv.next_btn_pos:
                        _adv_nx = int(_rapid_adv.next_btn_pos[0] * ANALYSIS_W / actual_w)
                        _adv_ny = int(_rapid_adv.next_btn_pos[1] * ANALYSIS_H / actual_h)
                        logger.info("[iter %d] ADV_RAPID → ↓ボタン座標 (%d,%d)", i, _adv_nx, _adv_ny)
                        tap_device(_adv_nx, _adv_ny, state, "ADV_RAPID_TAP")
                        state.last_action = "ADV_RAPID_TAP"
                    state.last_phash = ""
                    continue
                # ── ミニ会話タップ (1回) ──
                # ホーム画面 (MENU) では通知バナーを吹き出しと誤認するため抑制
                if state.current_scene != "MENU":
                    _mc = detect_mini_conversation(img_path)
                    if _mc is not None:
                        _mc_cx, _mc_cy, _mc_side = _mc
                        logger.info("[MINI_CONV][iter %d] 吹き出し(%s) → タップ (%d,%d)",
                                    i, _mc_side, _mc_cx, _mc_cy)
                        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                        state.last_action = "MINI_CONV_TAP"
                        state.last_phash = ""
                        continue
                # ── ツールバーなし + ↓なし + 吹き出しなし → OCR パスへフォールスルー ──

        else:
            # 画面変化なし
            state.same_phash_count += 1
            state.total_ocr_skipped += 1
            # MOYA_TAP 連続静止カウンタ: 指タップしても画面が変わらない回数を追跡
            if state.last_action == "MOYA_TAP":
                state.finger_tap_static.tick()
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
                    subprocess.run(["adb", "kill-server"], timeout=5)
                    time.sleep(2)
                    subprocess.run(["adb", "start-server"], timeout=5)
                    time.sleep(2)
                    if _ap_device.DEVICE_SERIAL:
                        subprocess.run(["adb", "connect", _ap_device.DEVICE_SERIAL], timeout=5)
                        time.sleep(1)
                    # scrcpy が ADB 再起動で死んだ場合は再起動
                    if _scrcpy_proc is not None and _scrcpy_proc.poll() is not None:
                        logger.info("[SCRCPY] WATCHDOG ADB reconnect 後に再起動")
                        _scrcpy_proc = manage_scrcpy()
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

            # ── 候補リトライ: 残り候補があれば次の候補をタップ ──
            # ホーム画面到達判定中は候補タップ禁止 (画面遷移で HOME_CLEAR_CHECK が中断される)
            if state.home_reached and not state.grind_mode:
                state.pending_candidates = []
                state.pending_candidate_idx = 0
            # 候補タップ後は数イテレーション待って画面変化を確認してから次候補へ
            # (即連打するとシーン遷移後のstaleタップが動画等に当たる)
            _CANDIDATE_WAIT_ITERS = 3
            if (state.pending_candidates
                    and state.pending_candidate_idx < len(state.pending_candidates)
                    and state.same_phash_count >= _CANDIDATE_WAIT_ITERS):
                _cr_path, _cr_w, _cr_h, _ = take_screenshot()
                _cr_stale = False
                if _cr_path:
                    _cr_img = prepare_analysis_image(_cr_path, _cr_w, _cr_h)
                    try:
                        _cr_ph = compute_phash(_cr_img)
                        _cr_dist = phash_distance(cur_phash, _cr_ph) if cur_phash and _cr_ph else 0
                    except Exception:
                        _cr_dist = 0
                    if _cr_dist >= PHASH_THRESHOLD:
                        logger.info("[CANDIDATE_RETRY] phash_dist=%d → シーン変化検出、候補破棄", _cr_dist)
                        state.pending_candidates = []
                        state.pending_candidate_idx = 0
                        state.last_phash = _cr_ph
                        _cr_stale = True
                        img_path = _cr_img
                        actual_w, actual_h = _cr_w, _cr_h
                if not _cr_stale and state.pending_candidates:
                    _cand = state.pending_candidates[state.pending_candidate_idx]
                    state.pending_candidate_idx += 1
                    logger.info("[CANDIDATE_RETRY] #%d/%d: %s (%d,%d) — %s",
                                state.pending_candidate_idx, len(state.pending_candidates),
                                _cand.action, _cand.x, _cand.y, _cand.desc)
                    tap_device(_cand.x, _cand.y, state, _cand.desc or _cand.action)
                    state.last_action = _cand.action
                    state.same_phash_count = 0
                    state.last_phash = cur_phash
                    continue

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
                # ── ADV送り待ちアイコン検知: phash 安定中でも1回タップ ──
                _adv_tap_x = int(ANALYSIS_W * 0.93)
                _adv_tap_y = int(ANALYSIS_H * 0.91)
                if ASSET_MANAGER.match_single("adv_next_btn", _early_analysis, roi=ADV_NEXT_BTN_ROI):
                    logger.info("[ADV][iter %d] ↓検出 → タップ (%d,%d)", i, _adv_tap_x, _adv_tap_y)
                    tap_device(_adv_tap_x, _adv_tap_y, state, "ADV_ADVANCE_TAP")
                    state.last_action = "ADV_RAPID_TAP"
                    state.last_phash = ""
                    state.same_phash_count = 0
                    state.stall_start = 0.0
                    continue
                # ── ミニ会話タップ (phash安定時, 1回) ──
                # MENU: 通知バナーを吹き出しと誤認 / MOVIE: 動画中タップで一時停止
                if state.current_scene not in ("MENU", "MOVIE"):
                    _mc = detect_mini_conversation(img_path)
                    if _mc is not None:
                        _mc_cx, _mc_cy, _mc_side = _mc
                        logger.info("[MINI_CONV][iter %d] 吹き出し(%s) → タップ (%d,%d)",
                                    i, _mc_side, _mc_cx, _mc_cy)
                        tap_device(_mc_cx, _mc_cy, state, "MINI_CONV_TAP")
                        state.last_action = "MINI_CONV_TAP"
                        state.last_phash = ""
                        state.same_phash_count = 0
                        state.stall_start = 0.0
                        continue
                # 動画シーンでは ADV ツールバーが無いためタップ抑制
                if state.current_scene in ("STORY", "ADV", "UNKNOWN"):
                    _aa_adv = detect_adv_scene(img_path, roi=state.game_roi)
                    if _aa_adv.is_adv:
                        if _aa_adv.next_btn_pos:
                            logger.info("[ADV_ADVANCE][iter %d] ADVツールバー検出 → ↓ボタンタップ", i)
                            _aa_x = int(_aa_adv.next_btn_pos[0] * ANALYSIS_W / actual_w)
                            _aa_y = int(_aa_adv.next_btn_pos[1] * ANALYSIS_H / actual_h)
                            tap_device(_aa_x, _aa_y, state, "ADV_ADVANCE")
                            state.last_phash = ""
                            state.same_phash_count = 0
                            state.stall_start = 0.0
                            continue
                    else:
                        # ツールバーなし → >| ボタン有無で動画判定
                        _movie_btn = detect_movie_skip_button(img_path)
                        if _movie_btn and len(state.last_ocr_texts) >= 2:
                            logger.info("[iter %d] >|検出だがOCR%d件+レターボックスなし → UI画面 → SCENE_TAP",
                                        i, len(state.last_ocr_texts))
                            _movie_btn = None
                        if _movie_btn:
                            logger.info("[MOVIE_WAIT] 動画検出(>|のみ) → 待機 (phash stable)")
                            state.last_action = "MOVIE_WAIT"
                            state.stall_start = 0.0  # ムービー待機中はスタックタイマー抑制
                            continue
                        # 金色⏭なし → OCR パスへフォールスルー
                state.last_phash = cur_phash
                time.sleep(_poll)
                continue

            # ── スタック介入 (強制OCRでもタップできず続いた場合) ──
            if state.stall_start == 0.0:
                state.stall_start = time.time()
            stall_elapsed = time.time() - state.stall_start

            # DL中はスタック介入をスキップ (phash 変化小はDLの正常動作)
            if state.download_active:
                logger.debug("[DL_PROTECT] DL中 → STALL_CORNER スキップ")
                state.stall_start = time.time()  # タイマーリセット
                state.last_phash = cur_phash
                time.sleep(3.0)
                continue

            if stall_elapsed >= STALL_TIMEOUT and not state.stall_corner_tried:
                # ADVシーン中は右上タップ禁止 (ツールバー >| スキップを押してしまうため)
                _stall_is_adv = detect_adv_scene(img_path, roi=state.game_roi).is_adv if img_path else False
                if _stall_is_adv:
                    logger.info(">>> %.0f秒スタック — ADVシーン → 右上×スキップ (セリフ送り代用)",
                                stall_elapsed)
                    # ADVでは画面中央下部をタップしてセリフ送りを試みる
                    _sc_x, _sc_y = roi_to_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.85), state.game_roi)
                    tap_device(_sc_x, _sc_y, state, "STALL_ADV_TAP")
                else:
                    logger.warning(">>> %.0f秒スタック — 盲タップせず証拠保存のみ", stall_elapsed)
                    save_evidence(img_path, [], "STALL_NO_ACTION", state)
                state.stall_corner_tried = True
                state.last_phash = ""
                state.same_phash_count = 0
                continue

            if stall_elapsed >= STALL_TIMEOUT * 4 and state.stall_corner_tried:
                _restart_count = state.unity_restart_count
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
                    subprocess.run(["adb", "-s", _ap_device.DEVICE_SERIAL, "shell", "am", "force-stop",
                                    APP_PACKAGE], timeout=5)
                    time.sleep(3)
                    subprocess.run(["adb", "-s", _ap_device.DEVICE_SERIAL, "shell", "am", "start", "-n",
                                    f"{APP_PACKAGE}/{APP_ACTIVITY}"],
                                   timeout=5)
                    logger.info("[UNITY_RESTART] ゲーム再起動完了 — 30秒待機")
                    time.sleep(30)
                except Exception as _uf_e:
                    logger.error("[UNITY_RESTART] 再起動失敗: %s", _uf_e)
                state.unity_restart_count = _restart_count + 1
                state.stall_start = 0.0
                state.stall_corner_tried = False
                state.same_phash_count = 0
                state.last_phash = ""
                # force-stop でタイトル画面に戻るため、進行フラグをリセット
                state.home_reached = False
                state.auto_activated = False
                state.character_selected = False
                state.char_just_selected = False
                state.battle_wait_count = 0
                continue

        # ── 4) 解析用画像の準備 ──
        state.last_phash = cur_phash
        analysis_path = _early_analysis or prepare_analysis_image(img_path, actual_w, actual_h)

        # ── 4.2) Result画面ハンドラ (RAPID mode) ──
        _result_action = handle_result_screen(
            state, analysis_path, [], dist, mode="RAPID")
        if _result_action:
            if _result_action[0] == "RESULT_FREEZE":
                # watchdog_recover は handle_result_screen 内で実行済み
                continue
            state.last_action = _result_action[0]
            state.stall_start = 0.0
            state.same_phash_count = 0
            _fms = (time.time() - _loop_t0) * 1000
            state.total_loop_ms += _fms
            logger.info("  [PERF] Loop %.0fms (%s) [%d/15]",
                        _fms, _result_action[0], state.result_rapid_count)
            continue
        # RESULT状態リセット (Result画面を脱出した時)
        if state.last_action not in ("RESULT_TAP", "RESULT_NEXT",
                                     "RESULT_RAPID", "GACHA_OK"):
            state.result_rapid_count = 0
            state.result_total_taps = 0

        # ── 4.3) BATTLE_RAPID: 発光/MOYA 検知即タップ → OCR 完全スキップ ──
        # detect_guide_glow() + ASSET_MANAGER.match_single() は OpenCV のみ (10-50ms)
        # OCR (6-8s) の 40-50 倍高速
        # ※ 強制 OCR (phash 静止 → ダイアログ可能性) 時は RAPID をスキップして OCR に回す
        #
        # 【永続バトルルール】
        #   左側キャラにモヤ（選択待ち発光）がある → キャラをタップ
        #   左側キャラにモヤがない → 右側の通常攻撃 or 戦闘スキルをタップ
        #   キャラ肖像の王冠/ロール装飾 (area<5000) は偽モヤ → 無視
        _force_ocr_override = (dist <= 2 and state.same_phash_count >= FORCE_ANALYZE_AFTER)
        # 速度チュートリアル表示中は BATTLE_RAPID をスキップして OCR で処理
        _last_texts_br = state.last_ocr_texts
        _speed_tip_in_last = any(
            any(k in t for k in ("このボタンでバトル", "進行速度を変更"))
            for t in _last_texts_br
        )
        if _speed_tip_in_last:
            _force_ocr_override = True
        # BATTLE_RAPID 連続ループ上限: 50回 (~4分) で OCR 再評価を強制
        # ムービーシーン等をバトルと誤分類し続ける問題を防止
        if state.battle_rapid_consecutive.stalled:
            logger.info("[BATTLE_RAPID] 連続 %d 回 → OCR で再評価 (シーン誤分類の可能性)",
                        state.battle_rapid_consecutive.count)
            state.battle_rapid_consecutive.reset()
            _force_ocr_override = True
        # ── バトルUIガード (メインループ BATTLE_RAPID): 3回に1回、通常攻撃ボタンの存在を確認 ──
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override and not _skip_rapid
                and state.battle_rapid_consecutive.count > 0
                and state.battle_rapid_consecutive.count % 3 == 0):
            _atk_m = ASSET_MANAGER.match_single("battle_normal_attack", analysis_path)
            if not _atk_m or _atk_m[2] < 0.70:
                logger.info("[BATTLE_RAPID] 通常攻撃ボタン未検出 (count=%d) → OCR で再評価",
                            state.battle_rapid_consecutive.count)
                state.battle_rapid_consecutive.reset()
                _force_ocr_override = True
        if (state.current_scene == "BATTLE" and analysis_path is not None
                and not _force_ocr_override and not _skip_rapid):
            _rapid_tx = _rapid_ty = 0
            _rapid_action = ""
            _rapid_double = False

            # ── 共通: 指テンプレートマッチ検出 (Phase 0 / Phase B で共用) ──
            _rapid_finger = ASSET_MANAGER.match_single("tutorial_hand_pointer", analysis_path)
            _rapid_blobs = []
            if _rapid_finger and _rapid_finger[2] >= 0.70:
                _rf_cx, _rf_cy = _rapid_finger[0], _rapid_finger[1]
                if _rf_cy > _SPATIAL_MARGIN_TOP and _rf_cx < ANALYSIS_W - _CLOSE_BTN_OFFSET:
                    _rapid_blobs = [(_rf_cx, _rf_cy, _rapid_finger[2])]

            # ── Phase 0: チュートリアル金枠 → 最優先タップ ──
            # 指ブロブ有無に関わらず金枠を常時チェック (extent<0.55 で通常ボタンと区別)
            # バトル中: 右半分のみ + overlay_mode=False (暗い背景で誤判定するため)
            # 非バトル: 全画面 + overlay 判定あり
            _gold_rho2 = state.current_scene == "BATTLE"
            _is_overlay2 = False if _gold_rho2 else detect_tutorial_overlay(analysis_path)
            _gold_tap = detect_tutorial_gold_button_tap(
                analysis_path, right_half_only=_gold_rho2, overlay_mode=_is_overlay2,
                skip_upper_filter=_gold_rho2)
            if _gold_tap:
                _rapid_tx, _rapid_ty = _gold_tap
                _rapid_action = "BATTLE_RAPID_GOLD_TUTORIAL"
                if _is_overlay2:
                    logger.info("[BATTLE_RAPID] 暗転オーバーレイ → 全画面金枠 (%d,%d)",
                                _rapid_tx, _rapid_ty)

            # ── Phase A: アクティブキャラ検出 (赤/ピンク発光ハロー) ──
            # 【永続ルール】キャラ選択モヤ = 赤/ピンクの発光。明度差で識別。
            _active_char = detect_active_battle_char(analysis_path, ANALYSIS_W, ANALYSIS_H)

            if not _rapid_action and not state.character_selected and _active_char is not None:
                _rapid_tx, _rapid_ty = _active_char[0], _active_char[1]
                _rapid_action = "BATTLE_RAPID_ACTIVE_P1"
                _rapid_double = True

            # ── Phase B: 右側スキル/攻撃ボタン ──
            if not _rapid_action:
                _rapid_glows = detect_guide_glow(
                    analysis_path, ANALYSIS_W, ANALYSIS_H, footer_ratio=0.30)
                _rapid_right_g = [g for g in _rapid_glows if g["side"] == "right"]

                _right_panel = [b for b in _rapid_blobs
                                if b[0] > _RIGHT_PANEL_X and b[1] > ANALYSIS_H * 0.45]

                if state.character_selected or state.char_just_selected:
                    # B-0: テンプレートで battle_special / battle_skill / battle_normal_attack を探す
                    for _btn_name in ("battle_special", "battle_skill", "battle_normal_attack"):
                        _btn_m = ASSET_MANAGER.match_single(_btn_name, analysis_path)
                        if _btn_m and _btn_m[2] >= 0.60:
                            _tmpl_action = f"BATTLE_RAPID_TMPL_{_btn_name.upper()}"
                            logger.info("[BATTLE_RAPID] テンプレ %s (%.2f) → tap(%d,%d)",
                                        _btn_name, _btn_m[2], _btn_m[0], _btn_m[1])
                            tap_device(_btn_m[0], _btn_m[1], state, _tmpl_action)
                            state.character_selected = False
                            state.char_just_selected = False
                            state.finger_detections += 1
                            state.battle_rapid_consecutive.tick()
                            _rapid_action = _tmpl_action
                            _rapid_tx, _rapid_ty = _btn_m[0], _btn_m[1]
                            break
                    # B-1: テンプレ未検出 → glow フォールバック
                    if not _rapid_action and _rapid_right_g:
                        _rr = max(_rapid_right_g, key=lambda g: g["area"])
                        _rapid_tx = _rr["cx"]
                        _rapid_ty = max(1, _rr["by"] + _rr["bh"] * 2 // 3)
                        _rapid_action = "BATTLE_RAPID_GLOW_P2"
                    elif not _rapid_action and _right_panel:
                        _tb = max(_right_panel, key=lambda b: b[2])
                        _rapid_tx, _rapid_ty = _tb[0], _tb[1]
                        _rapid_action = "BATTLE_RAPID_MOYA_P2"
                    elif not _rapid_action:
                        _rapid_tx, _rapid_ty = roi_to_device(
                            int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                        _rapid_action = "BATTLE_RAPID_NORMATK_P2"

            # ── Phase C: 左モヤなしフォールバック → 待機+再確認 → 右側攻撃ボタン ──
            # 【永続ルール】左キャラにモヤがない場合は常に右側の通常攻撃/戦闘スキルをタップ
            # 安全弁: 連続10回フォールバック → バトル以外のシーンの可能性 → OCR 再評価
            if not _rapid_action:
                if state.normatk_fallback.stalled:
                    logger.info("[BATTLE_RAPID] FALLBACK %d回連続 → OCR で再評価",
                                state.normatk_fallback.count)
                    state.normatk_fallback.reset()
                    # BATTLE_RAPID を抜けて OCR に回す (continue しない)
                else:
                    # 敵ターン/アニメーション中の可能性 → 待機+再スクショで確認
                    time.sleep(0.5)
                    _fb_path, _fb_w, _fb_h, _ = take_screenshot()
                    if _fb_path:
                        _fb_analysis = prepare_analysis_image(_fb_path, _fb_w, _fb_h)
                        _fb_active = detect_active_battle_char(
                            _fb_analysis, ANALYSIS_W, ANALYSIS_H)
                        if _fb_active:
                            logger.info("[BATTLE_RAPID] 再確認でACTIVE_CHAR検出 → 次ループで正規処理")
                            img_path = _fb_analysis
                            continue
                    # テンプレマッチで正しいボタン位置を探す (character_selected 不問)
                    for _fb_btn in ("battle_special", "battle_skill", "battle_normal_attack"):
                        _fb_m = ASSET_MANAGER.match_single(_fb_btn, analysis_path)
                        if _fb_m and _fb_m[2] >= 0.60:
                            _rapid_tx, _rapid_ty = _fb_m[0], _fb_m[1]
                            _rapid_action = f"BATTLE_RAPID_TMPL_{_fb_btn.upper()}"
                            logger.info("[BATTLE_RAPID] FALLBACK テンプレ %s (%.2f) → tap(%d,%d)",
                                        _fb_btn, _fb_m[2], _rapid_tx, _rapid_ty)
                            break
                    if not _rapid_action:
                        _rapid_tx, _rapid_ty = roi_to_device(
                            int(ANALYSIS_W * 0.90), int(ANALYSIS_H * 0.88), state.game_roi)
                        _rapid_action = "BATTLE_RAPID_NORMATK_FALLBACK"
                    state.normatk_fallback.tick()
            else:
                state.normatk_fallback.reset()

            # ── 共通タップ実行 ──
            if _rapid_action:
                logger.info("[%s] tap(%d,%d)%s",
                            _rapid_action, _rapid_tx, _rapid_ty,
                            " ダブルタップ" if _rapid_double else "")
                tap_device(_rapid_tx, _rapid_ty, state, _rapid_action,
                          rapid=_rapid_double)
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
                state.battle_rapid_consecutive.tick()
                _fms = (time.time() - _loop_t0) * 1000
                state.total_loop_ms += _fms
                logger.info("  [PERF] Loop %.0fms (BATTLE_RAPID)", _fms)
                continue  # OCR スキップ

        # BATTLE_RAPID を通過 → カウンタリセット
        state.battle_rapid_consecutive.reset()

        # ── 4.5) BATTLE 高速パス: OCR 前テンプレートマッチング ──
        # BATTLE シーンで GoldBtn/GoldSwipe が見つかれば OCR (6-8s) をスキップ
        # ※ 強制 OCR 時はスキップ (ダイアログ検出を優先)
        if state.current_scene == "BATTLE" and not _force_ocr_override and not _skip_rapid:
            _fast_action, _fast_wait = _battle_fast_check(analysis_path, state)
            if _fast_action:
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
            time.sleep(0.3)
            continue

        texts = all_texts(ocr_results)

        # ── ロック画面検出: "緊急通報のみ" = デバイスがスリープ → 復帰 ──
        _ocr_text_joined = " ".join(texts) if texts else ""
        if "緊急通報" in _ocr_text_joined or "通報のみ" in _ocr_text_joined:
            logger.warning("[LOCK_SCREEN] ロック画面検出 → WAKEUP + UNLOCK")
            adb("shell input keyevent KEYCODE_WAKEUP")
            time.sleep(1)
            adb("shell input keyevent 82")  # KEYCODE_MENU = swipe unlock
            time.sleep(2)
            adb(f"shell am start -n {APP_PACKAGE}/{APP_ACTIVITY}")
            time.sleep(5)
            state.last_phash = ""
            continue

        # ── ADVシーン検出 (毎回フレッシュ) ──
        _adv_result = detect_adv_scene(
            analysis_path or img_path, ocr_items=ocr_results, roi=state.game_roi)

        # ── シーン分類 ──
        scene, next_interval = classify_scene(
            texts, state.last_action, adv_detected=_adv_result.is_adv)
        # BATTLE 保持: SCENE_EARLY で BATTLE 確定後、OCR 分類が ADV/UNKNOWN でも
        # バトルキーワードが残っていれば BATTLE を維持 (攻撃アニメ中にテンプレ不一致になるため)
        _joined_for_scene = " ".join(texts)
        if (state.current_scene == "BATTLE" and scene not in ("BATTLE", "LOADING", "STORY")
                and any(kw in _joined_for_scene for kw in _BATTLE_UI_KWS)):
            logger.info("[SCENE_STICKY] %s→BATTLE保持 (バトルKW残存)", scene)
            scene = "BATTLE"
        # ── シーン遷移時フラグリセット ──
        if scene != state.current_scene:
            if state.current_scene == "BATTLE" and scene != "BATTLE":
                state.character_selected = False
                state.battle_rapid_consecutive.reset()
                logger.debug("[SCENE_CHANGE] BATTLE→%s: バトルフラグリセット", scene)
        state.current_scene = scene
        logger.info("[%s][iter %d] phash_dist=%d same=%d OCR(%d): %s",
                    scene, i, dist, state.same_phash_count, len(ocr_results), texts[:8])
        state.last_ocr_texts = texts

        # ── DL直後 SKIP 検出 (OCRベース, テンプレート不要) ──
        # post_download 中は動画判定・テンプレートに依存せず OCR の "SKIP" を直接タップ
        if state.post_download and scene not in ("BATTLE", "MENU"):
            _skip_item = next(
                (item for item in ocr_results
                 if "SKIP" in item.get("text", "").upper()
                 or item.get("text", "").upper() == "SK"),
                None)
            if _skip_item:
                _sk_x, _sk_y = _skip_item["center"]
                _sk_x, _sk_y = roi_to_device(_sk_x, _sk_y, state.game_roi)
                logger.info(
                    "[DL_SKIP_OCR] DL直後 SKIP '%s' → タップ (%d,%d)",
                    _skip_item["text"], _sk_x, _sk_y)
                tap_device(_sk_x, _sk_y, state, "MOVIE_SKIP_OCR")
                state.last_action = "MOVIE_SKIP"
                state.last_phash = ""
                continue

        # ── キャラ獲得画面検出: MOVIE と混同しやすいため先に判定 ──
        # 特徴: 左下にキャラ名 + "のキオク"(メモリア名) + 属性アイコン2つ
        # NEW! は初回のみ表示されるため判定に使わない
        # 判定: "XXXのキオク" パターン (OCR単体で "のキオク" を含むテキスト)
        # 除外: OK ボタンがある / 交換所説明 / 長文テキスト (ガチャ説明ダイアログ)
        _kioku_item = next(
            (item for item in ocr_results
             if "のキオク" in item.get("text", "") and len(item.get("text", "")) <= 15),
            None)
        _has_ok_btn = any("OK" in t for t in texts)
        _has_result = any("Result" in t or "result" in t for t in texts)
        # キャラ詳細画面は CHARA_GET ではない (詳細/限界突破/スキル/3D 等が見える)
        _is_chara_detail = any(kw in t for t in texts for kw in ("詳細", "限界突破", "スキル"))
        if _kioku_item and not _has_ok_btn and not _has_result and not _is_chara_detail and scene not in ("BATTLE",):
            _tap_x, _tap_y = roi_to_device(
                int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5), state.game_roi)
            logger.info("[CHARA_GET] キャラ獲得画面検出 (キオク) → 中央タップ (%d,%d)", _tap_x, _tap_y)
            tap_device(_tap_x, _tap_y, state, "CHARA_GET_TAP")
            state.last_action = "CHARA_GET_TAP"
            time.sleep(1.0)
            state.last_phash = ""
            continue

        # ── 6) 判定 & アクション (finger blob も渡す) ──
        # MOVIE→UNKNOWN 遷移直後: テンプレ誤マッチによるタップを抑制
        # (動画クレジット等で DIALOG_NAV_RIGHT, MINI_CONV が誤発火して一時停止する)
        _from_movie = getattr(state, "_from_movie", False)
        if _from_movie:
            state._from_movie = False
            # MOVIE スキップボタンがあれば SKIPタップ、なければ待機
            _skip_btn = detect_movie_skip_button(analysis_path) if analysis_path else None
            if _skip_btn:
                logger.info("[MOVIE→UNKNOWN] SKIPボタン検出 → タップ (%d,%d)", _skip_btn[0], _skip_btn[1])
                tap_device(_skip_btn[0], _skip_btn[1], state, "MOVIE_SKIP")
                action, wait_sec = "MOVIE_SKIP", 2.0
            else:
                logger.info("[MOVIE→UNKNOWN] テンプレタップ抑制 → MOVIE再判定待ち")
                action, wait_sec = "MOVIE_WAIT", 1.0
        else:
            action, wait_sec = detect_and_act(ocr_results, state, analysis_path,
                                                  adv_result=_adv_result)
        state.last_action = action
        # ── ホーム画面到達 → 自動操縦停止 ──
        if action == "GOAL_HOME_REACHED" and not state.grind_mode:
            logger.info("=" * 60)
            logger.info("  ホーム画面到達 — 自動操縦を停止します")
            logger.info("=" * 60)
            generate_and_copy_report(state, "ホーム画面到達")
            break
        # 副作用アクション以外なら代替候補を収集
        if action not in _IMMEDIATE_ACTIONS:
            state.pending_candidates = collect_secondary_candidates(
                ocr_results, state, analysis_path, primary_action=action)
            state.pending_candidate_idx = 0
            if state.pending_candidates:
                logger.info("[CANDIDATES] %d 個の代替候補を収集: %s",
                            len(state.pending_candidates),
                            [(c.action, c.x, c.y) for c in state.pending_candidates])
        else:
            state.pending_candidates = []
            state.pending_candidate_idx = 0
        # ── WAIT_FOR_CHANGE スタック脱出: 3 回連続で中央タップ ──
        if action == "WAIT_FOR_CHANGE":
            state._wfc_consecutive = getattr(state, "_wfc_consecutive", 0) + 1
            if state._wfc_consecutive >= 3:
                # アニメーション検査: 0.5s後に再スクショしphash比較
                time.sleep(0.5)
                _wfc_path, _wfc_w, _wfc_h, _ = take_screenshot()
                _wfc_is_anim = False
                if _wfc_path:
                    _wfc_img = prepare_analysis_image(_wfc_path, _wfc_w, _wfc_h)
                    _wfc_ph = compute_phash(_wfc_img) if _wfc_img else ""
                    _wfc_dist = phash_distance(cur_phash, _wfc_ph) if cur_phash and _wfc_ph else 0
                    if _wfc_dist >= PHASH_THRESHOLD:
                        _wfc_is_anim = True
                if _wfc_is_anim:
                    logger.info("[WFC_ESCAPE] 0.5s後phash_dist=%d → アニメーション中 → タップ抑制",
                                _wfc_dist)
                    state._wfc_consecutive = 0
                    state.last_action = "MOVIE_WAIT"
                    state.last_phash = _wfc_ph
                    continue
                # 長時間スタック (15回以上) → 動画一時停止の可能性 → 中央タップで復帰
                _wfc_total = getattr(state, "_wfc_total_count", 0) + 1
                state._wfc_total_count = _wfc_total
                if _wfc_total >= 5:
                    logger.warning(
                        "[WFC_ESCAPE] WAIT_FOR_CHANGE 累計%d回 → 動画一時停止疑い → 中央タップ",
                        _wfc_total)
                    tap_device(int(ANALYSIS_W * 0.5), int(ANALYSIS_H * 0.5),
                               state, "WFC_PAUSE_RESUME")
                    state._wfc_total_count = 0
                else:
                    logger.warning(
                        "[WFC_ESCAPE] WAIT_FOR_CHANGE %d 回連続 → 盲タップせず次ループへ",
                        state._wfc_consecutive,
                    )
                state._wfc_consecutive = 0
                state.last_phash = ""
        else:
            state._wfc_consecutive = 0
            state._wfc_total_count = 0

        # ── シーン再評価: 同一アクション連続時にシーン認識を疑う ──
        if action == state.last_action and action not in (
            "WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT",
            "MOVIE_WAIT", "LOADING_WAIT", "ADV_WAIT",
            "HOME_CLEAR_CHECK",
        ):
            state.action_repeat_count += 1
        else:
            state.action_repeat_count = 0
            state.scene_reeval_mode = False

        if state.action_repeat_count >= _SCENE_REEVAL_THRESHOLD:
            _pre_reeval_action = action
            logger.warning(
                "[SCENE_REEVAL] '%s' が %d 回連続 → シーン再評価 (ガード緩和)",
                action, state.action_repeat_count,
            )
            state.scene_reeval_mode = True
            # 新しいスクリーンショットでフル再判定 (常にフレッシュOCR)
            try:
                _re_img, _re_w, _re_h, _ = take_screenshot()
                _re_analysis = prepare_analysis_image(_re_img, _re_w, _re_h)
                _re_ocr = run_ocr(str(_re_analysis), lang=OCR_LANG,
                                  min_confidence=OCR_MIN_CONF)
                _re_texts = all_texts(_re_ocr)
                _re_adv = detect_adv_scene(_re_analysis, ocr_items=_re_ocr,
                                            roi=state.game_roi)
                _re_scene, _ = classify_scene(_re_texts, action,
                                              adv_detected=_re_adv.is_adv)
                if _re_scene != state.current_scene:
                    logger.warning(
                        "[SCENE_REEVAL] シーン不一致: %s → %s → 切替+再判定",
                        state.current_scene, _re_scene,
                    )
                    state.current_scene = _re_scene
                # レターボックスガードは廃止 (2:1デバイスで常時誤検出するため)
                action, wait_sec = detect_and_act(_re_ocr, state, _re_analysis,
                                                      adv_result=_re_adv)
                state.last_action = action
                state.action_repeat_count = 0
                logger.info("[SCENE_REEVAL] 再判定結果: %s", action)
                # 再判定後も同じアクション → 動画字幕等でスタックの可能性
                # → 画面中央タップでエスケープ (動画は字幕位置タップでは進まない)
                if action == _pre_reeval_action and action in (
                    "STORY_TAP", "MOYA_TAP", "ADV_NEXT_FALLBACK",
                    "ASSET_TUTORIAL_DIALOG_NEXT", "GOLD_BTN_TAP",
                    "FINGER_GOLD_TAP",
                ):
                    # DIALOG_NEXT スタック → × ボタン検索 → BACK キーで閉じる
                    # GOLD_BTN/MOYA は ADV 中の装飾誤検出が多いため中央タップ
                    if action == "ASSET_TUTORIAL_DIALOG_NEXT":
                        # × ボタンを探してタップ (BACK キーより確実)
                        _esc_close = None
                        if state.analysis_path:
                            for _tpl in ("tutorial_dialog_close", "close_btn", "close_btn_cross"):
                                _cm = ASSET_MANAGER.match_single(_tpl, state.analysis_path)
                                if _cm and _cm[2] >= 0.55:
                                    _esc_close = _cm
                                    break
                        if _esc_close:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] '%s' スタック → × ボタン (%d,%d, %.2f) で脱出",
                                action, _esc_close[0], _esc_close[1], _esc_close[2])
                            tap_device(_esc_close[0], _esc_close[1], state, "REEVAL_CLOSE_ESCAPE")
                        else:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] '%s' スタック → × 未検出、BACK キーで脱出", action)
                            adb("shell input keyevent KEYCODE_BACK")
                        state.last_action = "REEVAL_BACK_ESCAPE"
                    else:
                        # 代替候補があれば最優先で試行 (金枠の空振り脱出)
                        # STORY_TAP 系候補を優先 (CONFIRM_OK は金枠と近い位置のことが多い)
                        _esc_cand = None
                        for _c in state.pending_candidates:
                            if "STORY" in _c.action or "GOLD_BTN" in _c.action:
                                _esc_cand = _c
                                break
                        if _esc_cand is None and state.pending_candidates:
                            _esc_cand = state.pending_candidates[-1]  # 最後の候補にフォールバック
                        if _esc_cand:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] '%s' スタック → 代替候補 %s (%d,%d) にフォールバック",
                                action, _esc_cand.action, _esc_cand.x, _esc_cand.y)
                            tap_device(_esc_cand.x, _esc_cand.y, state,
                                       desc=f"REEVAL_CAND_{_esc_cand.action}")
                            state.pending_candidates = []
                            state.pending_candidate_idx = 0
                        else:
                            logger.warning(
                                "[SCENE_REEVAL_ESCAPE] 再判定でも '%s' → 盲タップせず次ループへ",
                                action,
                            )
                        state.last_action = "REEVAL_CENTER_ESCAPE"
            except Exception as _re_err:
                logger.debug("[SCENE_REEVAL] 再評価例外: %s", _re_err)
            state.scene_reeval_mode = False

        # タップ成功時: スタックカウンタリセット
        if action not in ("WAIT_FOR_CHANGE", "BATTLE_WAIT", "DOWNLOAD_WAIT"):
            state.stall_start = 0.0
            state.stall_corner_tried = False
            state.same_phash_count = 0

        # ── ダウンロード中フラグ管理 (SQLite 永続化) ──
        # ON: DL画面検出時 → 永続化 (プロセス再起動でも復元される)
        # OFF: DL/動画/ロード以外のシーンに遷移した時 → 削除
        _DL_MOVIE_ACTIONS = frozenset((
            "DOWNLOAD_WAIT", "MOVIE_WAIT", "MOVIE_SKIP",
            "LOADING_WAIT", "WAIT_FOR_CHANGE",
            "MAIN_STORY_LOADING",
            "DL_COMPLETE_OK", "SYSTEM_DLG_OK", "MOVIE_SKIP_OCR",
            "ADV_CHOICE", "SCENE_TAP", "STORY_TAP",
        ))
        _POST_DL_GRACE = 20  # DL完了後20イテレーションはpost_downloadを維持
        if action == "DOWNLOAD_WAIT":
            if not state.post_download:
                logger.info("[PERSIST] post_download=True (DL画面検出)")
                state.post_download = True
                state.post_download_ttl = _POST_DL_GRACE
                persist_state("post_download", "1")
            else:
                # DL中は TTL をリセットし続ける
                state.post_download_ttl = _POST_DL_GRACE
        elif action == "DL_COMPLETE_OK":
            # DL完了ダイアログOK → TTL リセット (この後の動画SKIPに備える)
            state.post_download_ttl = _POST_DL_GRACE
            logger.info("[PERSIST] post_download TTL リセット (DL_COMPLETE_OK)")
        elif state.post_download and action not in _DL_MOVIE_ACTIONS:
            state.post_download_ttl -= 1
            if state.post_download_ttl <= 0:
                logger.info("[PERSIST] post_download クリア (TTL切れ, action=%s)", action)
                state.post_download = False
                delete_state("post_download")
            else:
                logger.debug("[PERSIST] post_download 維持 (TTL=%d, action=%s)",
                             state.post_download_ttl, action)

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
        if i % 20 == 0 or action.startswith("GOAL_") or action in (
                "GRIND_QUEST_NAV", "SKIP", "AGREE", "RESULT_TAP"):
            save_evidence(img_path, ocr_results, action, state)

        # ── 7) 目的達成チェック ──
        # GOAL_ プレフィックスを持つアクション → ミッションに判定を委譲
        if action.startswith("GOAL_"):
            if mission.is_goal(action, state):
                _reason = mission.goal_reason(action, state)
                logger.info("=" * 62)
                logger.info("  [%s] %s", mission.name, _reason)
                _log_milestone(state, _reason)
                logger.info("  総タップ: %d  イテレーション: %d  周回: %d",
                            state.total_taps, i + 1, state.grind_cycles_completed)
                logger.info("  OCR実行: %d  スキップ: %d  暗転: %d",
                            state.total_ocr_calls, state.total_ocr_skipped,
                            state.total_blackout_skipped)
                logger.info("=" * 62)
                save_evidence(img_path, ocr_results, action, state)
                if _scrcpy_proc and _scrcpy_proc.poll() is None:
                    _scrcpy_proc.terminate()
                    logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)
                generate_and_copy_report(state, _reason)
                return
            else:
                logger.info("[MISSION] %s は %s の完了条件ではない → 続行",
                            action, mission.name)

        # ── 8) 待機 ──
        # 短い wait (< 5.0s) はスキップ: MIN_TAP_INTERVAL + phash ポーリングが制御
        # 長い wait (>= 5.0s) のみ実施: ダウンロード/メンテ/ロード等
        if wait_sec >= 5.0:
            if action == "DOWNLOAD_WAIT":
                # 適応ポーリング: 3秒ごとに phash チェック → 画面変化で早期脱出
                _dl_remaining = wait_sec
                _DL_POLL = 3.0
                logger.info("  [%s][%s] adaptive wait %.1fs (poll=%.1fs)",
                            scene, action, wait_sec, _DL_POLL)
                while _dl_remaining > 0:
                    _sleep_chunk = min(_DL_POLL, _dl_remaining)
                    time.sleep(_sleep_chunk)
                    _dl_remaining -= _sleep_chunk
                    if _dl_remaining <= 0:
                        break
                    try:
                        _dl_img, _, _, _ = take_screenshot()
                        _dl_ph = compute_phash(_dl_img)
                        _dl_dist = phash_distance(state.last_phash, _dl_ph) if state.last_phash and _dl_ph else 0
                        if _dl_dist >= PHASH_THRESHOLD:
                            logger.info("  [DOWNLOAD_ADAPTIVE] 画面変化検出 (dist=%d) → 早期脱出", _dl_dist)
                            state.last_phash = _dl_ph
                            # DL完了ダイアログの可能性 → 次イテレーションで即OCR
                            state.same_phash_count = 9  # 次の +1 で 10 到達 → 即 fallthrough
                            break
                    except Exception:
                        pass
            else:
                logger.info("  [%s][%s] long wait %.1fs",
                            scene, action, wait_sec)
                time.sleep(wait_sec)

        _loop_elapsed_ms = (time.time() - _loop_t0) * 1000
        state.total_loop_ms += _loop_elapsed_ms
        logger.info("  [PERF] Loop %.0fms (OCR)", _loop_elapsed_ms)

        # ── 9) メモリ解放 (SIGSEGV防止) ──
        # scrcpy 高速ループ (~5iter/sec) では CF/numpy 一時オブジェクトが蓄積しやすい
        if i % 10 == 0:
            gc.collect()
            # scrcpy 不死身モード: 50イテレーションごとにチェック
            if i > 0 and i % 50 == 0:
                if _scrcpy_proc is None or _scrcpy_proc.poll() is not None:
                    logger.info("[SCRCPY] プロセス消滅を検知 — 自動再起動")
                    _scrcpy_proc = manage_scrcpy()

        i += 1

    # scrcpy プロセスを終了
    if _scrcpy_proc and _scrcpy_proc.poll() is None:
        _scrcpy_proc.terminate()
        logger.info("[SCRCPY] 終了 PID=%d", _scrcpy_proc.pid)


if __name__ == "__main__":
    main()
