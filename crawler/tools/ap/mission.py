"""
ap/mission.py — ミッション定義

auto_pilot の目的と終了条件を宣言するレイヤー。
ハンドラやメインループは変更不要で、ミッション追加は
このファイルに新クラスを追加するだけで完結する。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


class Mission:
    """ミッション基底クラス。"""

    name: str = "base"
    description: str = "基底ミッション"

    def configure_state(self, state: PilotState, args: argparse.Namespace) -> None:
        """PilotState にミッション固有のフラグを設定する。"""

    def pre_loop(self, args: argparse.Namespace, serial: str) -> None:
        """メインループ前の準備処理 (fresh install 等)。
        auto_pilot.py 側から呼ばれる。重い処理はここに委譲。
        """

    def is_goal(self, action: str, state: PilotState) -> bool:
        """GOAL_ アクションがこのミッションの完了条件か判定する。"""
        return action.startswith("GOAL_")

    def goal_reason(self, action: str, state: PilotState) -> str:
        """完了時のレポート理由文を返す。"""
        return f"目的達成 ({action})"

    def banner_info(self) -> str:
        """起動バナーに表示するミッション情報。"""
        return f"{self.name}: {self.description}"


class TutorialMission(Mission):
    """新規アカウント: チュートリアル突破 → ホーム到達で停止。"""

    name = "tutorial"
    description = "チュートリアル突破 (ホーム画面到達で停止)"

    def configure_state(self, state, args):
        state.is_fresh_start = True
        state.grind_mode = False

    def pre_loop(self, args, serial):
        # fresh install は auto_pilot.py の _fresh_install_from_play_store を使う
        # (循環 import 回避のため、ここでは呼ばず auto_pilot.main() 側で制御)
        pass

    def is_goal(self, action, state):
        return action == "GOAL_HOME_REACHED"

    def goal_reason(self, action, state):
        return "ホーム画面到達 (チュートリアル完了)"


class GrindMission(Mission):
    """周回モード: クエスト自動繰り返し。"""

    name = "grind"
    description = "クエスト周回"

    def __init__(self, max_cycles: int = 0):
        self.max_cycles = max_cycles

    def configure_state(self, state, args):
        state.grind_mode = True
        state.grind_max_cycles = self.max_cycles

    def is_goal(self, action, state):
        return action == "GOAL_GRIND_COMPLETE"

    def goal_reason(self, action, state):
        return f"周回完了 ({state.grind_cycles_completed}/{state.grind_max_cycles}周)"

    def banner_info(self):
        _cycle_str = f"{self.max_cycles}周" if self.max_cycles > 0 else "無制限"
        return f"{self.name}: {self.description} ({_cycle_str})"


class ResumeMission(Mission):
    """途中再開: ホーム到達で停止 (デフォルト動作)。"""

    name = "resume"
    description = "途中再開 (ホーム画面到達で停止)"

    def configure_state(self, state, args):
        state.grind_mode = False

    def is_goal(self, action, state):
        return action == "GOAL_HOME_REACHED"

    def goal_reason(self, action, state):
        return "ホーム画面到達 (チュートリアル完了)"


def select_mission(args: argparse.Namespace) -> Mission:
    """CLI 引数からミッションを選択する。"""
    if args.fresh_install:
        return TutorialMission()
    if args.grind:
        return GrindMission(max_cycles=args.max_cycles)
    return ResumeMission()
