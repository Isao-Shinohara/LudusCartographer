"""
ap/state.py — PilotState / CycleState / StallCounter
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
class CycleState:
    """周回ごとに破棄・再作成される状態。

    新しいフィールドを追加する際は、周回をまたいで引き継ぐ必要がなければ
    ここに追加する（PilotState ではなく）。
    """
    iteration: int = 0
    last_phash: str = ""
    stall_start: float = 0.0
    stall_corner_tried: bool = False
    last_action: str = ""
    last_ocr_texts: list = field(default_factory=list)
    battle_wait_count: int = 0
    auto_activated: bool = False
    home_reached: bool = False
    game_foreground: bool = False
    tutorial_cleared: bool = False
    total_taps: int = 0
    ineffective_tap_count: int = 0
    _prev_taps_snapshot: int = 0
    _prev_ocr_texts: list = field(default_factory=list)
    total_ocr_calls: int = 0
    total_ocr_skipped: int = 0
    total_blackout_skipped: int = 0
    consecutive_blackouts: int = 0
    dark_ocr_empty_count: int = 0
    screenshots_saved: int = 0
    same_phash_count: int = 0
    consecutive_frozen_frames: int = 0
    last_forced_ocr_at: float = 0.0
    last_blob_xy: tuple = (0, 0)
    blob_same_count: int = 0
    char_just_selected: bool = False
    character_selected: bool = False
    pre_popup_tap_count: int = 0
    current_scene: str = "UNKNOWN"
    tap_suppressed: bool = False
    home_nav_count: int = 0
    last_screen_change_time: float = field(default_factory=time.time)
    watchdog_recovery_count: int = 0
    last_download_progress_log: float = 0.0
    screenshot_retry_count: int = 0
    last_screen: object = None
    game_roi: tuple = (0, 0, ANALYSIS_W, ANALYSIS_H)
    dialog_detections: int = 0
    finger_detections: int = 0
    gold_detections: int = 0
    total_loop_ms: float = 0.0
    finger_tap_static: StallCounter = field(
        default_factory=lambda: StallCounter("finger_tap_static", threshold=3))
    result_rapid_count: int = 0
    result_total_taps: int = 0
    result_subtype: str = ""
    gacha_total: StallCounter = field(
        default_factory=lambda: StallCounter("gacha_total", threshold=15))
    unity_restart_count: int = 0
    wifi_fail_streak: int = 0
    portrait_back_streak: int = 0
    last_phash_dist: int = 999
    home_tutorial_tap_count: int = 0
    action_repeat_count: int = 0
    scene_reeval_mode: bool = False
    normatk_fallback: StallCounter = field(
        default_factory=lambda: StallCounter("normatk_fallback", threshold=1))
    battle_rapid_consecutive: StallCounter = field(
        default_factory=lambda: StallCounter("battle_rapid_consecutive", threshold=50))
    movie_wait_consecutive: int = 0
    movie_static_count: int = 0
    recorder: object = None
    last_analysis_path: object = None
    last_ocr_results: list = field(default_factory=list)
    phash_moving_count: int = 0
    adv_confirmed_count: int = 0
    adv_early_consecutive: int = 0
    download_active: bool = False
    last_action_time: float = 0.0
    last_capture_time: float = 0.0
    transition_times: list = field(default_factory=list)
    pending_candidates: list = field(default_factory=list)
    pending_candidate_idx: int = 0
    is_fresh_start: bool = False
    startup_phase: bool = False
    milestone_logged: dict = field(default_factory=dict)


@dataclass
class PilotState:
    """操縦状態。周回をまたいで引き継ぐフィールドのみ保持する。

    周回ごとにリセットされるフィールドは CycleState に定義する。
    CycleState へのアクセスは state.cycle.xxx で行う。
    """
    # ── 周回をまたいで引き継ぐフィールド ──
    grind_mode: bool = False
    grind_max_cycles: int = 0
    grind_cycles_completed: int = 0
    device_w: int = 0
    device_h: int = 0
    launch_time: float = field(default_factory=time.time)
    # 操縦カテゴリ (Phase 2、起動時に決定、周回をまたいで保持)
    operation_code_key: str = ""
    operation_tag_id: int = 0

    # ── CycleState（周回ごとに破棄・再作成） ──
    cycle: CycleState = field(default_factory=CycleState)

    def reset_for_new_cycle(self) -> None:
        """周回間で状態をリセットする。CycleState を再作成するだけ。"""
        self.cycle = CycleState()
        self.launch_time = time.time()
