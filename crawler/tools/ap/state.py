"""
ap/state.py — PilotState / StallCounter
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H


@dataclass
class TapCandidate:
    """代替タップ候補。detect_and_act の主候補が空振りした際に順次試行する。"""
    x: int
    y: int
    action: str
    wait_sec: float = 1.0
    priority: int = 50
    desc: str = ""


class StallCounter:
    """宣言的な停滞カウンタ。閾値到達時のアクションを簡潔に記述する。

    Usage:
        counter = StallCounter("gold_swipe", threshold=6)
        counter.tick()           # +1
        if counter.stalled:      # >= threshold ?
            counter.reset()
    """
    __slots__ = ("name", "threshold", "_count")

    def __init__(self, name: str, threshold: int):
        self.name = name
        self.threshold = threshold
        self._count = 0

    def tick(self) -> int:
        """カウンタを +1 して現在値を返す。"""
        self._count += 1
        return self._count

    @property
    def count(self) -> int:
        return self._count

    @property
    def stalled(self) -> bool:
        """閾値に到達したか。"""
        return self._count >= self.threshold

    def reset(self) -> None:
        self._count = 0

    def __repr__(self) -> str:
        return f"StallCounter({self.name}, {self._count}/{self.threshold})"


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
    consecutive_blackouts: int = 0
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
    gold_swipe: StallCounter = field(default_factory=lambda: StallCounter("gold_swipe", threshold=3))
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
    # ─── 指アイコン静止画面カウンタ (スワイプシーン検出用) ───
    finger_tap_static: StallCounter = field(
        default_factory=lambda: StallCounter("finger_tap_static", threshold=3))
    # ─── ダイアログclose累計試行 (リセットなし、エスカレーション用) ───
    dialog_close_total: int = 0  # close失敗が蓄積 → 8回でBACK, 12回でスキップ
    # ─── Result画面ハンドラ状態 ───
    result_rapid_count: int = 0       # RESULT_RAPID ループ反復 [0..15]
    result_total_taps: int = 0        # Result画面での累積タップ [0..30]
    result_subtype: str = ""          # "GACHA" | "BATTLE" | ""
    # ─── 隠れ動的属性の昇格 ───
    gacha_total: StallCounter = field(
        default_factory=lambda: StallCounter("gacha_total", threshold=15))
    # ─── DIALOG_NAV_RIGHT 連続空振りカウンタ ───
    dialog_nav_stall: StallCounter = field(
        default_factory=lambda: StallCounter("dialog_nav_stall", threshold=8))
    unity_restart_count: int = 0      # Unity force-restart 試行回数 [0..3]
    wifi_fail_streak: int = 0         # Wi-Fi破損連続失敗カウンタ [0..5]
    last_phash_dist: int = 999        # 直近の phash 距離 (detect_and_act 内で参照)
    home_tutorial_tap_count: int = 0  # EARLY HOME検出でのHOME_TUTORIAL_TAP連続回数 (脱出閾値=10)
    action_repeat_count: int = 0     # 同一アクション連続回数 (シーン再評価トリガー)
    scene_reeval_mode: bool = False  # True: ガード緩和して再判定中
    # ─── 周回モード ───
    grind_mode: bool = False          # True: ホーム到達後もクエストへ自動ナビゲート
    grind_max_cycles: int = 0         # 0=無制限, N>0=N周で停止
    grind_cycles_completed: int = 0   # 周回完了回数
    normatk_fallback: StallCounter = field(
        default_factory=lambda: StallCounter("normatk_fallback", threshold=10))
    battle_rapid_consecutive: StallCounter = field(
        default_factory=lambda: StallCounter("battle_rapid_consecutive", threshold=50))
    # ─── MOVIE_WAIT 脱出カウンタ ───
    movie_wait_consecutive: int = 0
    # ─── ADV 連続検出カウンタ (phash 動的拡大用) ───
    adv_confirmed_count: int = 0
    # ─── ADV_EARLY 連続ハンドル回数 (スタック脱出用) ───
    adv_early_consecutive: int = 0
    # ─── ダウンロード直後フラグ (動画SKIP許可) ───
    post_download: bool = False
    # ─── ダウンロード中フラグ (誤タップ保護) ───
    download_active: bool = False
    # ─── キャッシュ (per-phash サイクル) ───
    _adv_toolbar_cache_phash: str = ""     # キャッシュ有効な phash 値
    _adv_toolbar_cache_result: bool = False # 後方互換 (is_adv_toolbar_cached用)
    _adv_scene_cache_result: object = None  # AdvSceneResult (循環import回避でobject型)
    # ─── テレメトリ (DEBUG レベル) ───
    last_action_time: float = 0.0          # 直近アクションの実行時刻
    last_capture_time: float = 0.0         # 直近スクショ取得時刻
    transition_times: list = field(default_factory=list)  # 遷移時間ヒストリ (最大100件)
    # ─── タップ候補リスト (OCR再解析省略) ───
    pending_candidates: list = field(default_factory=list)   # list[TapCandidate]
    pending_candidate_idx: int = 0
    # ─── 計測: 起動時刻 & 新規/途中再開判定 ───
    launch_time: float = field(default_factory=time.time)    # auto_pilot 起動時刻
    is_fresh_start: bool = False     # True: --fresh-install 新規開始, False: 途中再開
    milestone_logged: dict = field(default_factory=dict)  # {milestone_name: logged_time}
