"""
core.py — 自己修復型クロールのコアコンポーネント

AppHealthMonitor : Appium query_app_state でアプリ生存確認 + activate_app 自動復帰
StuckDetector    : 同一画面での dead-end 連続検知・ジェスチャー制御
FrontierTracker  : DFS フロンティアの記録とパス再構築（スマートバックトラック用）
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ============================================================
# AppHealthMonitor — アプリ生存確認・自動復帰
# ============================================================

class AppHealthMonitor:
    """
    Appium の query_app_state() でアプリの生存を確認し、
    非アクティブ / クラッシュ検知時に activate_app() で自動復帰する。

    【ApplicationState の値 (XCUITest / UIAutomator2)】
      0: NOT_INSTALLED
      1: NOT_RUNNING
      2: RUNNING_IN_BACKGROUND_SUSPENDED
      3: RUNNING_IN_BACKGROUND
      4: RUNNING_IN_FOREGROUND    ← 正常状態

    【使い方】
        monitor = AppHealthMonitor(driver, bundle_id="com.example.app")
        if not monitor.check_and_heal():
            logger.error("アプリ復帰不可 — このブランチをスキップ")
    """

    FOREGROUND_STATE = 4  # RUNNING_IN_FOREGROUND

    def __init__(self, driver, bundle_id: str, max_retries: int = 2) -> None:
        self._driver      = driver
        self._bundle_id   = bundle_id
        self._max_retries = max_retries

    def check_and_heal(self) -> bool:
        """
        アプリが生存しているか確認し、必要なら activate_app() で復帰を試みる。

        Returns:
            True  — アプリが RUNNING_IN_FOREGROUND (or チェック不可でスキップ)
            False — 復帰試行がすべて失敗した
        """
        if not self._bundle_id:
            return True  # bundle_id 未設定 = チェック不可

        try:
            state = self._driver.driver.query_app_state(self._bundle_id)
        except Exception as e:
            logger.debug(f"[HEALTH] query_app_state 失敗 (楽観的継続): {e}")
            return True  # 検査不可のため楽観的に継続

        if state == self.FOREGROUND_STATE:
            return True

        logger.warning(
            f"[HEALTH] アプリ非アクティブ: state={state}"
            f"  bundle={self._bundle_id!r} — 復帰を試みます"
        )

        for attempt in range(1, self._max_retries + 1):
            try:
                self._driver.driver.activate_app(self._bundle_id)
                self._driver.wait(2.0)
                state2 = self._driver.driver.query_app_state(self._bundle_id)
                if state2 == self.FOREGROUND_STATE:
                    logger.info(
                        f"[HEALTH] ✅ アプリ復帰成功 (試行 {attempt}/{self._max_retries})"
                    )
                    return True
                logger.warning(
                    f"[HEALTH] 復帰後も非アクティブ: state={state2} (試行 {attempt})"
                )
            except Exception as e:
                logger.warning(f"[HEALTH] activate_app 失敗 (試行 {attempt}): {e}")

        logger.error(f"[HEALTH] ❌ アプリ復帰失敗: {self._bundle_id!r}")
        return False

    def is_alive(self) -> bool:
        """ヘルスチェックのみ（復帰試行なし）。デバッグ用。"""
        if not self._bundle_id:
            return True
        try:
            state = self._driver.driver.query_app_state(self._bundle_id)
            return state == self.FOREGROUND_STATE
        except Exception:
            return True  # チェック不可 = 楽観的に True


# ============================================================
# StuckDetector — 同一画面スタック検知
# ============================================================

class StuckDetector:
    """
    同一 fingerprint で dead-end が連続する回数を追跡し、
    適切なジェスチャー（スワイプ・長押し）のタイミングを通知する。

    【閾値設計】
      threshold     = N    → N 回以上: スワイプ試行対象
      threshold × 2 = 2N  → 2N 回以上: 長押し試行対象（より積極的な突破）
      threshold × 3 = 3N  → 3N 回以上: 諦め（ジェスチャーも効かない）
    """

    def __init__(self, threshold: int = 2) -> None:
        self._threshold = threshold
        self._counts: dict[str, int] = {}  # fingerprint → consecutive dead-end count

    def record(self, fingerprint: str) -> int:
        """dead-end を記録して現在のカウントを返す。"""
        self._counts[fingerprint] = self._counts.get(fingerprint, 0) + 1
        return self._counts[fingerprint]

    def get_count(self, fingerprint: str) -> int:
        return self._counts.get(fingerprint, 0)

    def should_swipe(self, fingerprint: str) -> bool:
        """スワイプを試みるべきか（threshold ≤ count < threshold×3）。"""
        count = self._counts.get(fingerprint, 0)
        return self._threshold <= count < self._threshold * 3

    def should_long_press(self, fingerprint: str) -> bool:
        """長押しを試みるべきか（count ≥ threshold×2）。"""
        return self._counts.get(fingerprint, 0) >= self._threshold * 2

    def is_hopeless(self, fingerprint: str) -> bool:
        """ジェスチャーでも突破不可能と判断すべきか（count ≥ threshold×3）。"""
        return self._counts.get(fingerprint, 0) >= self._threshold * 3

    def reset(self, fingerprint: str) -> None:
        """fingerprint のカウントをリセットする（復帰成功時など）。"""
        self._counts.pop(fingerprint, None)


# ============================================================
# FrontierTracker — DFS フロンティア記録・経路再構築
# ============================================================

class FrontierTracker:
    """
    DFS で探索したナビゲーション経路を記録し、
    フロンティア（未踏の分岐点）への最短経路を再構築する。

    【記録データ】
      _nav_map  : {child_fp: parent_fp}         — 画面の親子関係
      _tap_map  : {"parent_fp::item_text": child_fp} — タップ→遷移先マッピング

    【スマートバックトラックの流れ】
      1. DFS が max_depth で打ち切られた画面を frontier として記録
      2. build_path_to(target_fp) で root → target のパスを復元
      3. get_nav_recipe(path, visited) でタップ手順リストを取得
      4. アプリ再起動後に手順を再生してフロンティアへナビゲート
    """

    def __init__(self) -> None:
        self._nav_map: dict[str, Optional[str]] = {}  # fp → parent_fp (Noneはroot)
        self._tap_map: dict[str, str]           = {}  # "parent_fp::text" → child_fp

    def record_nav(self, fp: str, parent_fp: Optional[str]) -> None:
        """親子ナビゲーションを記録する（新規画面発見時に呼ぶ）。"""
        self._nav_map[fp] = parent_fp

    def record_tap(self, parent_fp: str, item_text: str, child_fp: str) -> None:
        """タップ→遷移先マッピングを記録する。"""
        key = f"{parent_fp}::{item_text}"
        self._tap_map[key] = child_fp
        logger.debug(f"[FRONTIER] tap_map: {parent_fp[:8]}::{item_text!r} → {child_fp[:8]}")

    def build_path_to(self, target_fp: str) -> list[str]:
        """
        target_fp から root (parent=None) までの指紋パスを返す。

        Returns:
            [root_fp, ..., target_fp] の順。
            target が未記録または循環がある場合は空リスト。
        """
        path: list[str] = []
        fp: Optional[str] = target_fp
        seen: set[str]    = set()

        while fp is not None:
            if fp in seen:
                logger.warning(f"[FRONTIER] nav_map にサイクル検出: {fp[:8]}")
                return []
            seen.add(fp)
            path.append(fp)
            fp = self._nav_map.get(fp)

        path.reverse()
        return path

    def get_tap_for_step(self, parent_fp: str, child_fp: str) -> Optional[str]:
        """
        parent_fp → child_fp 遷移に使われたタップテキストを返す。

        Returns:
            item_text (str) または None（未記録の場合）
        """
        for key, recorded_child in self._tap_map.items():
            if key.startswith(f"{parent_fp}::") and recorded_child == child_fp:
                return key.split("::", 1)[1]
        return None

    def get_nav_recipe(
        self,
        path: list[str],
        visited: dict,  # {vkey: ScreenRecord} — ScreenCrawler._visited
    ) -> list[tuple[str, dict]]:
        """
        path に沿ったナビゲーション手順 [(item_text, item_dict), ...] を返す。

        path[0] = root, path[-1] = target。各ステップのタップ候補を
        visited から引いて返す。

        Returns:
            [(text, item), ...] のリスト (len = len(path)-1) 成功時
            []                  失敗時（経路が再構築できなかった場合）
        """
        recipe: list[tuple[str, dict]] = []

        for i in range(len(path) - 1):
            p_fp = path[i]
            c_fp = path[i + 1]

            text = self.get_tap_for_step(p_fp, c_fp)
            if text is None:
                logger.warning(
                    f"[FRONTIER] ナビゲーション経路不明: {p_fp[:8]} → {c_fp[:8]}"
                )
                return []

            # visited から親の ScreenRecord を取得
            record = next(
                (r for r in visited.values() if r.fingerprint == p_fp),
                None,
            )
            if record is None:
                logger.warning(f"[FRONTIER] ScreenRecord が見つかりません: fp={p_fp[:8]}")
                return []

            item = next(
                (it for it in record.tappable_items if it["text"] == text),
                None,
            )
            if item is None:
                logger.warning(
                    f"[FRONTIER] タップ候補 {text!r} が見つかりません: fp={p_fp[:8]}"
                )
                return []

            recipe.append((text, item))

        return recipe

    def get_root_fp(self) -> Optional[str]:
        """root 画面の fingerprint を返す（parent_fp=None の画面）。"""
        for fp, parent in self._nav_map.items():
            if parent is None:
                return fp
        return None
