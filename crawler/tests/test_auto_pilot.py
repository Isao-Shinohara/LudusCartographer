"""
test_auto_pilot.py — auto_pilot.py の単体テスト

AssetManager (require_ocr) と StrategicDecisionEngine の動作検証。
"""
from __future__ import annotations

import sys
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─── AssetManager テスト ──────────────────────────────────────

class TestAssetManagerRequireOcr:
    """AssetManager.match() の require_ocr 条件フィルタリングテスト。"""

    def _make_manager(self, require_ocr: list[str]) -> "AssetManager":
        """require_ocr 付きのダミーテンプレートを持つ AssetManager を生成。"""
        from tools.auto_pilot import AssetManager
        import numpy as np
        manager = AssetManager.__new__(AssetManager)
        # ダミーテンプレート (16x16 のグレー画像)
        dummy_img = np.full((16, 16), 128, dtype="uint8")
        manager._templates = {
            "test_tmpl": {
                "img": dummy_img,
                "threshold": 0.5,
                "action": "TEST_ACTION",
                "offset": [0, 0],
                "require_ocr": require_ocr,
            }
        }
        return manager

    def test_match_passes_when_require_ocr_empty(self, tmp_path):
        """require_ocr が空リストなら ocr_texts に関係なくマッチ対象になる。"""
        import cv2, numpy as np
        manager = self._make_manager([])
        # 全白画像を作成（マッチしやすい）
        img = np.full((100, 100), 128, dtype="uint8")
        img_path = tmp_path / "test.png"
        cv2.imwrite(str(img_path), img)
        # ocr_texts=None でもマッチ試行する（結果はマッチしないが例外は出ない）
        result = manager.match(img_path, ocr_texts=None)
        # ダミー画像なのでマッチしないが、処理が正常完了することを確認
        assert result is None or isinstance(result, tuple)

    def test_require_ocr_skips_when_keyword_absent(self, tmp_path):
        """require_ocr のキーワードが OCR テキストにない場合はスキップ。"""
        import cv2, numpy as np
        manager = self._make_manager(["矢印をタップ"])
        img = np.full((100, 100), 200, dtype="uint8")
        img_path = tmp_path / "test.png"
        cv2.imwrite(str(img_path), img)
        # "矢印をタップ" がない OCR テキスト
        result = manager.match(img_path, ocr_texts=["OK", "次へ"])
        assert result is None

    def test_require_ocr_allows_when_keyword_present(self, tmp_path):
        """require_ocr のキーワードが OCR テキストにある場合はマッチ試行する。"""
        import cv2, numpy as np
        manager = self._make_manager(["矢印をタップ"])
        # テンプレートと同じ画像を使えばマッチするはず
        tmpl_img = manager._templates["test_tmpl"]["img"]
        # テンプレートを含む画像 (128x128 にテンプレートを埋め込む)
        full_img = np.full((128, 128), 100, dtype="uint8")
        full_img[10:26, 10:26] = tmpl_img
        img_path = tmp_path / "test.png"
        cv2.imwrite(str(img_path), full_img)
        # "矢印をタップ" が含まれる OCR テキスト → マッチ試行
        result = manager.match(img_path, ocr_texts=["矢印をタップしてください"])
        # テンプレートサイズが画像に対して小さいのでマッチするはず
        assert result is not None
        assert result[2] == "TEST_ACTION"

    def test_require_ocr_skips_when_ocr_texts_none(self, tmp_path):
        """ocr_texts=None のとき require_ocr チェックをスキップして通常マッチを試みる。"""
        import cv2, numpy as np
        manager = self._make_manager(["矢印をタップ"])
        tmpl_img = manager._templates["test_tmpl"]["img"]
        full_img = np.full((128, 128), 100, dtype="uint8")
        full_img[10:26, 10:26] = tmpl_img
        img_path = tmp_path / "test.png"
        cv2.imwrite(str(img_path), full_img)
        # ocr_texts=None → require_ocr チェックなし → マッチ試行
        result = manager.match(img_path, ocr_texts=None)
        assert result is not None


# ─── StrategicDecisionEngine テスト ──────────────────────────

class TestStrategicDecisionEnginePrediction:
    """predict_outcome() のキーワードマッピングテスト。"""

    @pytest.fixture
    def engine(self, tmp_path):
        from tools.auto_pilot import StrategicDecisionEngine
        eng = StrategicDecisionEngine.__new__(StrategicDecisionEngine)
        eng.KNOWLEDGE_PATH = tmp_path / "knowledge_base.json"
        eng._knowledge = {"patterns": {}, "stats": {"total_taps": 0, "verified": 0}}
        return eng

    @pytest.mark.parametrize("text,expected_type", [
        ("ガシャを引く", "GACHA_DRAW"),
        ("スキップ", "SKIP_STORY"),
        ("SKIP", "SKIP_STORY"),
        ("次へ進む", "SCENE_ADVANCE"),
        ("OK", "CONFIRM"),
        ("了解", "CONFIRM"),
        ("出撃する", "BATTLE_START"),
        ("AUTO", "AUTO_BATTLE"),
        ("通常攻撃", "NORMAL_ATTACK"),
        ("必殺技を使う", "SPECIAL_ATTACK"),
        ("リザルト確認", "RESULT"),
        ("Result", "RESULT"),
    ])
    def test_predict_known_keywords(self, engine, text, expected_type):
        action_type, desc = engine.predict_outcome(text)
        assert action_type == expected_type, f"'{text}' → expected {expected_type}, got {action_type}"
        assert len(desc) > 0

    def test_predict_unknown_text(self, engine):
        action_type, desc = engine.predict_outcome("xyz_unknown_text_1234")
        assert action_type == "UNKNOWN"

    def test_log_prediction_returns_tuple(self, engine, caplog):
        import logging
        with caplog.at_level(logging.INFO):
            result = engine.log_prediction("スキップ", 100, 200)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0] == "SKIP_STORY"
        assert "[PREDICTION]" in caplog.text

    def test_log_prediction_unknown_no_log(self, engine, caplog):
        """UNKNOWNのときは[PREDICTION]ログを出さない。"""
        import logging
        with caplog.at_level(logging.INFO):
            result = engine.log_prediction("xyz_unknown", 100, 200)
        assert result[0] == "UNKNOWN"
        assert "[PREDICTION]" not in caplog.text


class TestStrategicDecisionEngineVerify:
    """verify_and_learn() の経験記録テスト。"""

    @pytest.fixture
    def engine(self, tmp_path):
        from tools.auto_pilot import StrategicDecisionEngine
        eng = StrategicDecisionEngine.__new__(StrategicDecisionEngine)
        eng.KNOWLEDGE_PATH = tmp_path / "knowledge_base.json"
        eng._knowledge = {"patterns": {}, "stats": {"total_taps": 0, "verified": 0}}
        return eng

    def test_verify_success_increments_success_count(self, engine, caplog):
        """phash距離大 → SUCCESS として success_count を増加。"""
        import logging
        # phash_distance が PHASH_THRESHOLD 以上になるよう pre/post を異なる値に
        # compute_phash の代わりにモックを使う
        with patch("tools.auto_pilot.phash_distance", return_value=10):
            engine.verify_and_learn("aaa", "bbb", "CONFIRM", "test", "OK")
        key = "CONFIRM:OK"
        assert key in engine._knowledge["patterns"]
        assert engine._knowledge["patterns"][key]["success_count"] == 1
        assert engine._knowledge["patterns"][key]["failure_count"] == 0

    def test_verify_failure_increments_failure_count(self, engine):
        """phash距離小 → NO_CHANGE として failure_count を増加。"""
        with patch("tools.auto_pilot.phash_distance", return_value=1):
            engine.verify_and_learn("aaa", "bbb", "CONFIRM", "test", "OK")
        key = "CONFIRM:OK"
        assert engine._knowledge["patterns"][key]["failure_count"] == 1
        assert engine._knowledge["patterns"][key]["success_count"] == 0

    def test_verify_skips_when_action_unknown(self, engine):
        """action_type=UNKNOWN のときは記録しない。"""
        with patch("tools.auto_pilot.phash_distance", return_value=10):
            engine.verify_and_learn("aaa", "bbb", "UNKNOWN", "test", "xyz")
        assert len(engine._knowledge["patterns"]) == 0

    def test_verify_saves_knowledge_every_10_taps(self, engine, tmp_path):
        """10タップごとに knowledge_base.json を保存する。"""
        engine.KNOWLEDGE_PATH = tmp_path / "kb.json"
        with patch("tools.auto_pilot.phash_distance", return_value=10):
            for i in range(10):
                engine.verify_and_learn("a", "b", "CONFIRM", "desc", f"OK{i}")
        assert engine.KNOWLEDGE_PATH.exists()
        data = json.loads(engine.KNOWLEDGE_PATH.read_text())
        assert data["stats"]["total_taps"] == 10

    def test_verify_skips_with_empty_phash(self, engine):
        """pre_phash or post_phash が空のときはスキップ。"""
        engine.verify_and_learn("", "bbb", "CONFIRM", "test", "OK")
        engine.verify_and_learn("aaa", "", "CONFIRM", "test", "OK")
        assert len(engine._knowledge["patterns"]) == 0


class TestStrategicDecisionEngineFindButtons:
    """find_buttons() のボタン検出テスト。"""

    @pytest.fixture
    def engine(self, tmp_path):
        from tools.auto_pilot import StrategicDecisionEngine
        eng = StrategicDecisionEngine.__new__(StrategicDecisionEngine)
        eng.KNOWLEDGE_PATH = tmp_path / "knowledge_base.json"
        eng._knowledge = {"patterns": {}, "stats": {}}
        return eng

    def test_find_buttons_returns_list(self, engine, tmp_path):
        """正常な画像ファイルを渡すとリストが返る。"""
        import cv2, numpy as np
        # シンプルな画像に矩形を描画
        img = np.zeros((200, 400, 3), dtype="uint8")
        cv2.rectangle(img, (50, 80), (200, 130), (0, 120, 255), -1)  # orange-ish rect
        img_path = tmp_path / "test.png"
        cv2.imwrite(str(img_path), img)
        result = engine.find_buttons(img_path)
        assert isinstance(result, list)

    def test_find_buttons_returns_empty_on_missing_file(self, engine):
        result = engine.find_buttons(Path("/nonexistent/file.png"))
        assert result == []

    def test_classify_color_gray(self, engine):
        import numpy as np
        gray_roi = np.full((20, 40, 3), 100, dtype="uint8")  # neutral gray
        color = engine._classify_color(gray_roi)
        assert color == "gray"

    def test_classify_color_white(self, engine):
        import numpy as np
        white_roi = np.full((20, 40, 3), 240, dtype="uint8")
        color = engine._classify_color(white_roi)
        assert color == "white"

    def test_report_screen_affordances_no_crash(self, engine, tmp_path):
        """空の画像でも例外を出さない。"""
        import cv2, numpy as np
        img = np.zeros((100, 100, 3), dtype="uint8")
        img_path = tmp_path / "empty.png"
        cv2.imwrite(str(img_path), img)
        # OCR結果なし → ログだけ出す（クラッシュしない）
        engine.report_screen_affordances(img_path, [])


# ─── Result画面ハンドラ テスト ──────────────────────────────────────

def _make_ocr_item(text: str, cx: int, cy: int, confidence: float = 0.9) -> dict:
    """テスト用の OCR アイテムを生成。"""
    return {
        "text": text,
        "center": (cx, cy),
        "confidence": confidence,
        "box": [[cx - 20, cy - 10], [cx + 20, cy - 10],
                [cx + 20, cy + 10], [cx - 20, cy + 10]],
    }


class TestIsResultScreen:
    """_is_result_screen() のテスト。"""

    def test_gacha_result_detected(self):
        from tools.auto_pilot import _is_result_screen
        ocr = [_make_ocr_item("NEW", 100, 100),
               _make_ocr_item("NEW", 300, 100),
               _make_ocr_item("NEW", 500, 100)]
        texts = [r["text"] for r in ocr]
        is_result, subtype = _is_result_screen(ocr, texts)
        assert is_result is True
        assert subtype == "GACHA"

    def test_battle_result_detected(self):
        from tools.auto_pilot import _is_result_screen
        ocr = [_make_ocr_item("Result", 760, 50),
               _make_ocr_item("EXP", 400, 300)]
        texts = [r["text"] for r in ocr]
        is_result, subtype = _is_result_screen(ocr, texts)
        assert is_result is True
        assert subtype == "BATTLE"

    def test_formation_excluded(self):
        from tools.auto_pilot import _is_result_screen
        ocr = [_make_ocr_item("Lv.1", 200, 200),
               _make_ocr_item("パーティ", 400, 50)]
        texts = [r["text"] for r in ocr]
        is_result, subtype = _is_result_screen(ocr, texts)
        assert is_result is False
        assert subtype == ""

    def test_no_result_keywords(self):
        from tools.auto_pilot import _is_result_screen
        ocr = [_make_ocr_item("クエスト", 100, 100),
               _make_ocr_item("ショップ", 300, 100)]
        texts = [r["text"] for r in ocr]
        is_result, subtype = _is_result_screen(ocr, texts)
        assert is_result is False
        assert subtype == ""


class TestFindNextButton:
    """_find_next_button() のテスト。"""

    def test_finds_next_in_bottom_right(self):
        from tools.auto_pilot import _find_next_button
        ocr = [_make_ocr_item("次へ", 1100, 650)]
        result = _find_next_button(ocr, 1520, 720, "BATTLE")
        assert result is not None
        assert result["center"] == (1100, 650)

    def test_ignores_next_in_top_left(self):
        from tools.auto_pilot import _find_next_button
        ocr = [_make_ocr_item("次へ", 100, 100)]
        result = _find_next_button(ocr, 1520, 720, "BATTLE")
        assert result is None

    def test_finds_ok_for_gacha(self):
        from tools.auto_pilot import _find_next_button
        ocr = [_make_ocr_item("OK", 762, 680, confidence=0.8)]
        result = _find_next_button(ocr, 1520, 720, "GACHA")
        assert result is not None
        assert result["text"] == "OK"


class TestHandleResultScreen:
    """handle_result_screen() のテスト。"""

    @pytest.fixture
    def state(self):
        from tools.auto_pilot import PilotState
        s = PilotState()
        s.device_w = 0
        s.device_h = 0
        return s

    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.detect_guide_glow")
    def test_rapid_mode_with_glow(self, mock_glow, mock_tap, state, tmp_path):
        from tools.auto_pilot import handle_result_screen
        state.last_action = "RESULT_TAP"
        mock_glow.return_value = [
            {"cx": 1200, "cy": 600, "area": 5000, "side": "right",
             "bx": 1100, "by": 550, "bw": 200, "bh": 100}
        ]
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_result_screen(state, analysis, [], 5, mode="RAPID")
        assert result is not None
        assert result[0] == "RESULT_RAPID"
        assert result[1] == 1.0
        assert state.result_rapid_count == 1
        mock_tap.assert_called_once()

    @patch("tools.auto_pilot.tap_device")
    def test_ocr_mode_gacha(self, mock_tap, state):
        from tools.auto_pilot import handle_result_screen
        ocr = [_make_ocr_item("NEW", 100, 100),
               _make_ocr_item("NEW", 300, 100),
               _make_ocr_item("NEW", 500, 100),
               _make_ocr_item("OK", 762, 680)]
        result = handle_result_screen(state, None, ocr, 5, mode="OCR")
        assert result is not None
        assert result[0] == "GACHA_OK"
        assert result[1] == 2.0
        assert mock_tap.call_count == 2  # ダブルタップ

    @patch("tools.auto_pilot.tap_device")
    def test_ocr_mode_battle(self, mock_tap, state):
        from tools.auto_pilot import handle_result_screen
        ocr = [_make_ocr_item("Result", 760, 50),
               _make_ocr_item("EXP", 400, 300),
               _make_ocr_item("次へ", 1100, 650)]
        result = handle_result_screen(state, None, ocr, 5, mode="OCR")
        assert result is not None
        assert result[0] == "RESULT_TAP"
        assert result[1] == 1.0
        assert mock_tap.call_count == 1  # シングルタップ

    @patch("tools.auto_pilot.tap_device")
    def test_returns_none_for_non_result(self, mock_tap, state):
        from tools.auto_pilot import handle_result_screen
        ocr = [_make_ocr_item("クエスト", 100, 100)]
        result = handle_result_screen(state, None, ocr, 5, mode="OCR")
        assert result is None
        mock_tap.assert_not_called()

    @patch("tools.auto_pilot.watchdog_recover", return_value=True)
    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.detect_guide_glow", return_value=[])
    def test_freeze_recovery_at_30_taps(self, mock_glow, mock_tap,
                                         mock_watchdog, state, tmp_path):
        from tools.auto_pilot import handle_result_screen
        state.last_action = "RESULT_TAP"
        state.result_total_taps = 29  # 次で30
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_result_screen(state, analysis, [], 5, mode="RAPID")
        assert result is not None
        assert result[0] == "RESULT_FREEZE"
        mock_watchdog.assert_called_once()
        assert state.result_total_taps == 0
        assert state.result_rapid_count == 0


# ─── StallCounter テスト ──────────────────────────────────────

class TestStallCounter:
    """StallCounter ユーティリティクラスのテスト。"""

    def _make(self, name: str = "test", threshold: int = 3):
        from tools.auto_pilot import StallCounter
        return StallCounter(name, threshold)

    def test_tick_increments(self):
        c = self._make()
        assert c.tick() == 1
        assert c.count == 1

    def test_stalled_at_threshold(self):
        c = self._make(threshold=3)
        c.tick()
        c.tick()
        c.tick()
        assert c.stalled is True

    def test_not_stalled_below_threshold(self):
        c = self._make(threshold=3)
        c.tick()
        c.tick()
        assert c.stalled is False

    def test_reset_clears_count(self):
        c = self._make(threshold=3)
        for _ in range(5):
            c.tick()
        c.reset()
        assert c.count == 0
        assert c.stalled is False

    def test_repr(self):
        c = self._make("x", threshold=3)
        c.tick()
        c.tick()
        assert repr(c) == "StallCounter(x, 2/3)"


# ─── PilotState 動的属性昇格テスト ──────────────────────────

class TestPilotStateDynamicAttrsRemoved:
    """隠れ動的属性が PilotState の正式フィールドに昇格したことを確認。"""

    @pytest.fixture
    def state(self):
        from tools.auto_pilot import PilotState
        return PilotState()

    def test_gacha_total_taps_is_typed_field(self, state):
        assert state.gacha_total_taps == 0
        state.gacha_total_taps = 5
        assert state.gacha_total_taps == 5

    def test_unity_restart_count_is_typed_field(self, state):
        assert state.unity_restart_count == 0
        state.unity_restart_count = 2
        assert state.unity_restart_count == 2

    def test_gold_swipe_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.gold_swipe, StallCounter)
        assert state.gold_swipe.threshold == 6

    def test_normatk_fallback_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.normatk_fallback, StallCounter)
        assert state.normatk_fallback.threshold == 10
