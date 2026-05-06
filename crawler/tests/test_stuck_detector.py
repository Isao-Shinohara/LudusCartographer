"""タップ無効 stuck 検知のテスト。

設計:
- タップ後に画面が変化しない (= 同一画面) → 失敗 target として set に追加
- 画面が変化したら set をクリア (進捗あり)
- set サイズ >= K=8 → STUCK 確定 → auto_pilot 停止

同一画面判定は phash + dHash + OCR テキスト類似度で:
- phash >= 15: 別画面確定
- OCR テキスト両方あり & 類似度 < 0.85: 別画面 (ADV セリフ進行を区別)
- それ以外で phash + dHash 近い: 同一画面 (エフェクト揺れ吸収)
"""
import pytest

from tools.ap.stuck_detector import (
    StuckTapDetector,
    _is_same_effective_screen,
)


class TestIsSameEffectiveScreen:
    """同一画面判定のテスト。"""

    # ─── 画面が違うケース (False を返すべき) ───

    def test_phash_far_returns_different(self):
        """phash 距離が大きい → 別画面確定。"""
        # 距離 16 (1ビット差を 16 個)
        assert _is_same_effective_screen(
            "0000000000000000", "ffff000000000000",
            None, None, "", "",
        ) is False

    def test_adv_dialogue_change_returns_different(self):
        """ADV セリフ進行: 画像近いが OCR テキストが大きく違う → 別画面。"""
        # phash ほぼ同じ、dhash 同じ
        result = _is_same_effective_screen(
            "abc1234567890123", "abc1234567890123",
            "0000000000000000", "0000000000000000",
            "まどか、危ない！", "ほむら、これは何？",
        )
        assert result is False, "ADV セリフ進行は別画面と判定すべき"

    def test_battle_hp_change_returns_different(self):
        """バトル: HP 等が変化して OCR テキストが違う → 別画面。"""
        result = _is_same_effective_screen(
            "abc1234567890123", "abc1234567890124",  # phash 微差
            "0000000000000000", "0000000000000001",
            "AUTO HP 100%", "AUTO HP 50% ダメージ 500",
        )
        assert result is False

    # ─── 同じ画面のケース (True を返すべき) ───

    def test_identical_returns_same(self):
        """完全一致 → 同じ画面。"""
        assert _is_same_effective_screen(
            "abc1234567890123", "abc1234567890123",
            "0000000000000000", "0000000000000000",
            "", "",
        ) is True

    def test_minor_pixel_drift_returns_same(self):
        """phash 微小揺れ + OCR ほぼ同じ → 同じ画面 (エフェクト揺れ)。"""
        # phash 距離 ~3 (実データの平均)
        result = _is_same_effective_screen(
            "81a8404025210010", "81a840c025210030",
            "0000000000000000", "0000000000000010",
            "U キオク編成 入手順 NEW 福成中",
            "U キオク編成 入手順 NEW 編成中",
        )
        assert result is True, "エフェクト揺れ範囲は同じ画面と判定すべき"

    def test_empty_ocr_both_phash_close_returns_same(self):
        """OCR 両方空 + phash + dhash 近い → 同じ画面 (ロゴ/暗転等)。"""
        result = _is_same_effective_screen(
            "0000000000000000", "0000000000000003",
            "0000000000000000", "0000000000000005",
            "", "",
        )
        assert result is True

    def test_empty_ocr_phash_too_far_returns_different(self):
        """OCR 両方空 + phash 距離 8 (>5 fallback 閾値) → 別画面。"""
        # dhash 入れない → fallback で p_dist < 5 必要
        result = _is_same_effective_screen(
            "0000000000000000", "00000000000000ff",  # 8 ビット差
            None, None, "", "",
        )
        assert result is False

    # ─── OCR が短いケース (両方 5 文字以上必要) ───

    def test_short_ocr_falls_back_to_phash_only(self):
        """OCR が短すぎ (< 5 文字) → 類似度判定をスキップ、phash + dHash で判定。"""
        # phash 近い + 短い違う OCR → OCR は無視して phash 判定 → 同じ画面
        result = _is_same_effective_screen(
            "abc1234567890123", "abc1234567890124",
            "0000000000000000", "0000000000000000",
            "G",  # 短すぎ
            "10",  # 短すぎ
        )
        assert result is True, "OCR < 5 文字は判定材料にしない"


class TestStuckTapDetector:
    """ineffective_targets set を使った K 回判定のテスト。"""

    def test_initial_state_no_stuck(self):
        """初期状態は STUCK ではない。"""
        det = StuckTapDetector(k=8)
        result = det.evaluate("TUTORIAL_TAP_EARLY", 1228, 300,
                              "abc1234567890123", "0000000000000000",
                              "U キオク編成 入手順 NEW")
        assert result is False
        assert len(det.ineffective_targets) == 0  # 初回は前回 phash なし → 記録なし

    def test_screen_change_resets_set(self):
        """画面が変わったら ineffective_targets はクリア。"""
        det = StuckTapDetector(k=8)
        # 初回
        det.evaluate("A", 100, 100, "0000000000000000", "00", "テキスト1 abcde")
        # 同じ画面でタップ失敗
        det.evaluate("A", 200, 200, "0000000000000000", "00", "テキスト1 abcde")
        assert len(det.ineffective_targets) == 1

        # 画面変化 → リセット
        det.evaluate("A", 300, 300, "ffffffffffffffff", "ff", "全然違うテキスト xyz")
        assert len(det.ineffective_targets) == 0

    def test_same_target_within_30px_not_double_counted(self):
        """30px 以内の同じ位置を再タップしても 2 重カウントされない。"""
        det = StuckTapDetector(k=8)
        # 初回
        det.evaluate("TAP", 100, 100, "0000000000000000", "00", "")
        # 同じ画面で (100, 100) に再タップ
        det.evaluate("TAP", 100, 100, "0000000000000000", "00", "")
        # 同じ画面で (110, 105) (30px 以内)
        det.evaluate("TAP", 110, 105, "0000000000000000", "00", "")
        assert len(det.ineffective_targets) == 1

    def test_different_targets_30px_apart_count_separately(self):
        """30px 以上離れた位置は別ターゲットとしてカウント。"""
        det = StuckTapDetector(k=8)
        det.evaluate("TAP", 100, 100, "0000000000000000", "00", "")
        det.evaluate("TAP", 100, 100, "0000000000000000", "00", "")  # 同じ
        det.evaluate("TAP", 200, 200, "0000000000000000", "00", "")  # +100px → 別
        det.evaluate("TAP", 300, 300, "0000000000000000", "00", "")  # +100px → 別
        assert len(det.ineffective_targets) == 3

    def test_different_action_types_count_separately(self):
        """同じ位置でも action_type が違えば別カウント。"""
        det = StuckTapDetector(k=8)
        det.evaluate("ACTION_A", 100, 100, "0000000000000000", "00", "")
        det.evaluate("ACTION_A", 100, 100, "0000000000000000", "00", "")  # 同じ
        det.evaluate("ACTION_B", 100, 100, "0000000000000000", "00", "")  # 別 action
        assert len(det.ineffective_targets) == 2

    def test_k_threshold_triggers_stuck(self):
        """K=8 個の異なる失敗 target で STUCK 確定。"""
        det = StuckTapDetector(k=8)
        det.evaluate("TAP", 0, 0, "0000000000000000", "00", "")  # 初回
        # 7 個の異なる target → まだ stuck じゃない
        for i in range(1, 8):
            stuck = det.evaluate("TAP", 100 * i, 0,
                                 "0000000000000000", "00", "")
            assert stuck is False, f"{i} 個目で stuck 判定はまだ早い"

        # 8 個目で stuck
        stuck = det.evaluate("TAP", 1000, 0, "0000000000000000", "00", "")
        assert stuck is True
        assert len(det.ineffective_targets) >= 8

    def test_screen_change_during_accumulation_resets(self):
        """途中で画面変化があれば set リセット → 再カウント開始。"""
        det = StuckTapDetector(k=8)
        det.evaluate("TAP", 0, 0, "0000000000000000", "00", "テキスト1 abcde")
        # 5 個失敗
        for i in range(1, 6):
            det.evaluate("TAP", 100 * i, 0, "0000000000000000", "00", "テキスト1 abcde")
        assert len(det.ineffective_targets) == 5

        # 画面変化
        det.evaluate("TAP", 0, 0, "ffffffffffffffff", "ff", "全然違うテキスト xyz")
        assert len(det.ineffective_targets) == 0

        # また 5 個失敗 → まだ stuck じゃない
        for i in range(1, 6):
            stuck = det.evaluate("TAP", 100 * i, 0,
                                 "ffffffffffffffff", "ff",
                                 "全然違うテキスト xyz")
            assert stuck is False

    def test_adv_dialogue_progression_no_false_stuck(self):
        """ADV セリフ進行: 画像似てるが OCR 大きく違う → STUCK にならない。"""
        det = StuckTapDetector(k=8)
        # phash 微小変化 + OCR が毎回大きく違う (= ダイアログ進行)
        dialogues = [
            "「まどか、危ない！」",
            "「ほむら、これは何？」",
            "「QB、説明して」",
            "「魔女が現れた」",
            "「契約してください」",
            "「絶望が広がっていく」",
            "「希望を失わないで」",
            "「最後の戦い」",
            "「これで終わりだ」",
            "「永遠の別れ」",
        ]
        for i, line in enumerate(dialogues):
            phash = f"abc{i:02x}1234567890" + "1" * 2
            stuck = det.evaluate("ADV_TAP", 720, 360,
                                 phash, "0000000000000000", line)
            # ADV はセリフ進行 = 画面変化扱い → 毎回リセット → 絶対に stuck にならない
            assert stuck is False, f"ADV {i+1} 行目で誤検知: {line}"

    def test_battle_progression_no_false_stuck(self):
        """バトル: HP/ダメージ表示で OCR が変わる → STUCK にならない。"""
        det = StuckTapDetector(k=8)
        battle_states = [
            "AUTO HP 100% ダメージ 0",
            "AUTO HP 90% ダメージ 100",
            "AUTO HP 70% ダメージ 300",
            "AUTO HP 50% ダメージ 500",
            "AUTO HP 30% ダメージ 700",
            "AUTO HP 10% ダメージ 900",
            "WIN! 経験値 100",
            "クリア！ 報酬獲得",
            "次のステージへ",
            "結果画面",
        ]
        for i, txt in enumerate(battle_states):
            phash = f"def{i:02x}1234567890" + "12"
            stuck = det.evaluate("BATTLE_TAP", 1215, 623,
                                 phash, "abcd000000000000", txt)
            assert stuck is False, f"バトル {i+1}: 誤検知"

    def test_loading_state_no_taps_no_stuck(self):
        """ロード中など、タップしないと evaluate が呼ばれない → 自然と stuck にならない。"""
        det = StuckTapDetector(k=8)
        # evaluate を呼ばない = タップしない
        # 別途 reset をテスト
        det.reset()
        assert len(det.ineffective_targets) == 0

    def test_real_stuck_scenario_kioku_henseei(self):
        """実データで再現: キオク編成 stuck。

        初回 evaluate は baseline として記録のみ → set に追加されない。
        以降 8 個の distinct target で stuck 確定 = 計 9 回 evaluate。

        実シナリオ: TUTORIAL_TAP 6 段階 + WFC_ESCAPE 系 × ボタン 2 個 = 8 distinct
        + 1 baseline 呼び出し = 9 回。
        """
        det = StuckTapDetector(k=8)
        # 実データの phash (5 分間 stuck の隣接フレーム)
        real_phashes = [
            "81a8404025210010", "81a840c025210030", "81aa404025250010",
            "81aa4240253500ab", "8182424835150010", "81a0404935150010",
        ]
        # OCR 微小揺れ (実データ準拠、similarity >= 0.85 で同一画面判定)
        ocrs = [
            "U キオク編成 入手順 NEW 福成中 Lv.1",
            "U キオク編成 入手順 NEW 福成中 Lv.1 NEW",
            "U キオク編成 1i 入手順 NEW 福成中 Lv.1",
            "U キオク編成 入手順 NEW 福成中 Lv.1 NEW1",
            "U キオク編成 入手順 NEW 編成中 Lv.1",
            "U キオク編成 入手順 NEW 福成中 Lv.1 LV.1",
        ]
        # TUTORIAL_TAP fallback (y 30px ずつ違い → 全部 distinct target)
        tutorial_taps = [(1228, 300), (1228, 330), (1228, 360),
                         (1228, 390), (1228, 420), (1228, 450)]
        for i in range(6):
            stuck = det.evaluate("TUTORIAL_TAP_EARLY",
                                 *tutorial_taps[i],
                                 real_phashes[i],
                                 "0000000000000000",
                                 ocrs[i])
            # i=0 は baseline (set 0)、i=1..5 で set 1..5 → どれも stuck ではない
            assert stuck is False, f"{i+1} 回目で誤検知"

        # WFC_ESCAPE × button #1 (別 action_type かつ別座標)
        stuck = det.evaluate("WFC_CLOSE_BTN", 1038, 98,
                             "81a0404935150010",
                             "0000000000000000",
                             "U キオク編成 入手順 NEW 福成中 Lv.1")
        assert stuck is False, "7 個目 (set サイズ 6): まだ"

        # WFC_ESCAPE × button #2 (別座標) → set サイズ 7
        stuck = det.evaluate("WFC_CLOSE_BTN", 1900, 50,
                             "81a0404935150010",
                             "0000000000000000",
                             "U キオク編成 入手順 NEW 福成中 Lv.1")
        assert stuck is False, "8 個目 (set サイズ 7): まだ"

        # 別 fallback → set サイズ 8 → STUCK
        stuck = det.evaluate("BLIND_TAP", 720, 360,
                             "81a0404935150010",
                             "0000000000000000",
                             "U キオク編成 入手順 NEW 福成中 Lv.1")
        assert stuck is True, "9 個目 (set サイズ 8) で stuck 確定すべき"
