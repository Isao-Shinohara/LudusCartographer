"""タップ無効 stuck 検知。

設計目的:
    アプリ側の不具合等でタップしても画面が変わらない状態を検知して
    auto_pilot を自動停止し、API/CPU リソースの無駄遣いを防ぐ。

検知ロジック:
    各タップ実行後、画面が「効果的に変化していない」(エフェクト揺れは許容) なら
    失敗 target (action_type, x//30, y//30) を set に追加。
    画面変化があれば set をクリア (= 進捗あり)。
    set サイズが K (default 8) に到達 → STUCK 確定。

同一画面判定:
    - phash 距離 >= 15: 別画面確定
    - OCR テキスト両方あり (>= 5 文字) & 類似度 < 0.85: 別画面 (ADV セリフ進行を区別)
    - phash + dHash 両方近い: 同一画面 (エフェクト揺れ吸収)
    - dHash なしフォールバック: phash < 5 で同一画面
"""
from __future__ import annotations

from typing import Optional


def _is_same_effective_screen(
    prev_phash: str, curr_phash: str,
    prev_dhash: Optional[str], curr_dhash: Optional[str],
    prev_text: str, curr_text: str,
) -> bool:
    """エフェクト揺れを吸収しつつ ADV セリフ進行を区別する同一画面判定。

    OCR テキストは「区別する方向にのみ」使う非対称設計:
      - phash 似てても OCR 大きく違う → 別画面 (セリフ進行検知)
      - phash 違うのに OCR 似てるからと言って merge はしない
    """
    from lc.image_comparator import phash_distance, dhash_distance

    # ① phash が大きく違ったら確実に別画面
    p_dist = phash_distance(prev_phash, curr_phash)
    if p_dist >= 15:
        return False

    # ② OCR テキスト両方あり (5 文字以上) & 類似度低い → 別画面
    if (prev_text and curr_text
            and len(prev_text) >= 5 and len(curr_text) >= 5):
        from tools.anchor_matcher import _text_similarity
        sim = _text_similarity(prev_text, curr_text)
        if sim < 0.85:
            return False

    # ③ phash 範囲 (< 15) + dHash 近い → 同一画面 (エフェクト揺れ範囲)
    # ① で >= 15 を弾いているので p_dist は 0-14 の範囲。OCR 類似度も >= 0.85 確認済。
    # 残り条件は dHash の近さで微小揺れ vs 別画面を区別する。
    if prev_dhash and curr_dhash:
        d_dist = dhash_distance(prev_dhash, curr_dhash)
        return d_dist < 20

    # ④ dHash なしフォールバック (p_dist < 5 で同一とみなす)
    return p_dist < 5


class StuckTapDetector:
    """タップしても画面が変化しない状態を検知する。

    Usage:
        detector = StuckTapDetector(k=8)
        # 各タップ実行後:
        is_stuck = detector.evaluate(
            action_type="TUTORIAL_TAP_EARLY",
            tap_x=1228, tap_y=300,
            curr_phash=phash, curr_dhash=dhash, curr_text=ocr_text,
        )
        if is_stuck:
            # auto_pilot 停止
            ...
    """

    # 座標の丸め単位 (この粒度で同じなら同じ target とみなす)
    _ROUND_PX = 30

    def __init__(self, k: int = 8):
        self.k = k
        self.ineffective_targets: set = set()
        self.last_phash: str = ""
        self.last_dhash: Optional[str] = None
        self.last_text: str = ""

    def evaluate(
        self,
        action_type: str,
        tap_x: int, tap_y: int,
        curr_phash: str,
        curr_dhash: Optional[str],
        curr_text: str,
    ) -> bool:
        """タップ後の画面状態を評価。STUCK 確定なら True。

        初回呼び出し (= last_phash 未設定) は記録のみで False。
        """
        # 初回: 記録のみ
        if not self.last_phash:
            self.last_phash = curr_phash
            self.last_dhash = curr_dhash
            self.last_text = curr_text
            return False

        same = _is_same_effective_screen(
            self.last_phash, curr_phash,
            self.last_dhash, curr_dhash,
            self.last_text, curr_text,
        )

        if same:
            # 画面変化なし → 失敗 target として記録 (重複は set で自動除外)
            target = (action_type,
                      tap_x // self._ROUND_PX,
                      tap_y // self._ROUND_PX)
            self.ineffective_targets.add(target)
            # last_* は更新しない (= 直前の "進捗ある画面" との比較を維持)
            return len(self.ineffective_targets) >= self.k

        # 画面変化あり → 進捗 → リセット
        self.ineffective_targets.clear()
        self.last_phash = curr_phash
        self.last_dhash = curr_dhash
        self.last_text = curr_text
        return False

    def reset(self) -> None:
        """周回境界等で全状態をクリア。"""
        self.ineffective_targets.clear()
        self.last_phash = ""
        self.last_dhash = None
        self.last_text = ""
