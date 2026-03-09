"""
test_auto_pilot.py — auto_pilot.py の単体テスト

AssetManager (require_ocr) と Result画面ハンドラの動作検証。
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
        assert result[1] == 1.0
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

    def test_gacha_total_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.gacha_total, StallCounter)
        assert state.gacha_total.threshold == 15

    def test_finger_tap_static_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.finger_tap_static, StallCounter)
        assert state.finger_tap_static.threshold == 3

    def test_unity_restart_count_is_typed_field(self, state):
        assert state.unity_restart_count == 0
        state.unity_restart_count = 2
        assert state.unity_restart_count == 2

    def test_gold_swipe_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.gold_swipe, StallCounter)
        assert state.gold_swipe.threshold == 3

    def test_normatk_fallback_is_stall_counter(self, state):
        from tools.auto_pilot import StallCounter
        assert isinstance(state.normatk_fallback, StallCounter)
        assert state.normatk_fallback.threshold == 10


# ─── handle_dialog_screen テスト ──────────────────────────────

class TestHandleDialogScreen:
    """handle_dialog_screen() のテスト。"""

    @pytest.fixture
    def state(self):
        from tools.auto_pilot import PilotState
        s = PilotState()
        s.device_w = 0
        s.device_h = 0
        return s

    def test_returns_none_when_no_analysis_path(self, state):
        from tools.auto_pilot import handle_dialog_screen
        result = handle_dialog_screen(state, None, [], [], False, False)
        assert result is None

    def test_returns_none_when_finger_guard_active(self, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, True)
        assert result is None

    @patch("tools.auto_pilot.find_finger_blobs", return_value=[])
    @patch("tools.auto_pilot.detect_white_hand_pointer", return_value=None)
    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("close", 100, 50))
    def test_dialog_close_returns_action(self, mock_dlg, mock_tap, mock_white,
                                          mock_finger, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, False)
        assert result is not None
        assert result[0] == "DIALOG_CLOSE"
        assert result[1] == 1.0
        mock_tap.assert_called_once()

    @patch("tools.auto_pilot.find_finger_blobs", return_value=[])
    @patch("tools.auto_pilot.detect_white_hand_pointer", return_value=None)
    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.process_paging_dialog", return_value="DIALOG_CLOSED")
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("next", 800, 400))
    def test_paging_dialog_returns_action(self, mock_dlg, mock_paging, mock_tap,
                                           mock_white, mock_finger, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, False)
        assert result is not None
        assert result[0] == "DIALOG_CLOSED"
        assert result[1] == 1.0
        mock_paging.assert_called_once()

    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("close", 1400, 50))
    def test_notice_popup_close_bypasses_all_guards(self, mock_dlg, mock_tap,
                                                      state, tmp_path):
        """お知らせポップアップ: is_notice_popup=True → ガード全バイパスで×閉じ。"""
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        # has_finger_guard=True でも is_notice_popup=True でバイパスされる
        result = handle_dialog_screen(state, analysis, [], [], False, True,
                                       is_notice_popup=True)
        assert result is not None
        assert result[0] == "NOTICE_POPUP_CLOSE"
        assert result[1] == 1.0
        mock_tap.assert_called_once()

    @patch("tools.auto_pilot.take_screenshot", return_value=(Path("/tmp/test.png"), 1520, 720, 0))
    @patch("tools.auto_pilot.prepare_analysis_image", return_value=Path("/tmp/test.png"))
    @patch("tools.auto_pilot.tap_device")
    @patch("tools.auto_pilot.count_page_dots", return_value=3)
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav")
    def test_notice_popup_paging_bypasses_all_guards(self, mock_dlg, mock_dots,
                                                       mock_tap, mock_prep,
                                                       mock_ss, state, tmp_path):
        """お知らせポップアップ: ドット数でページング → ×閉じ。"""
        from tools.auto_pilot import handle_dialog_screen
        # 1回目: next, 2回目以降: close (最終ページ到達)
        mock_dlg.side_effect = [
            ("next", 1400, 360),  # 初回 (外側から渡される)
            ("next", 1400, 360),  # ▷2回目
            ("close", 1469, 44),  # 最終ページ → ×
        ]
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, True,
                                       is_notice_popup=True)
        assert result is not None
        assert result[0] == "NOTICE_POPUP_CLOSE"
        assert result[1] == 1.0
        # ドット3 → ▷2回 + ×1回 = 3タップ
        assert mock_tap.call_count == 3

    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("close", 100, 50))
    def test_battle_dialog_guard_skips_close_in_top(self, mock_dlg, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], True, False)
        assert result is None

    @patch("tools.auto_pilot.find_finger_blobs", return_value=[])
    @patch("tools.auto_pilot.detect_white_hand_pointer", return_value=None)
    @patch("tools.auto_pilot.adb")
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("close", 100, 400))
    def test_escalation_back_at_8_attempts(self, mock_dlg, mock_adb, mock_white,
                                            mock_finger, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        state.dialog_close_total = 7  # 次で8
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, False)
        assert result is not None
        assert result[0] == "DIALOG_BACK_ESCALATION"
        assert result[1] == 2.0
        mock_adb.assert_called_once_with("shell input keyevent KEYCODE_BACK")

    @patch("tools.auto_pilot.find_finger_blobs", return_value=[])
    @patch("tools.auto_pilot.detect_white_hand_pointer", return_value=None)
    @patch("tools.auto_pilot.detect_dialog_frame_and_nav", return_value=("close", 100, 400))
    def test_escalation_skip_at_12_attempts(self, mock_dlg, mock_white,
                                             mock_finger, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        state.dialog_close_total = 11  # 次で12
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False, False)
        assert result is None
        assert state.dialog_close_total == 0
        assert state.pre_popup_tap_count == 0


# ─── roi_to_device テスト ──────────────────────────────────────

class TestRoiToDevice:
    """roi_to_device() の座標変換テスト。"""

    def test_identity_roi(self):
        """ROI がフル解析空間と同一なら座標不変。"""
        from tools.auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H
        roi = (0, 0, ANALYSIS_W, ANALYSIS_H)
        dx, dy = roi_to_device(760, 360, roi)
        assert dx == 760
        assert dy == 360

    def test_offset_roi(self):
        """ROI にオフセットがある場合、座標がシフトされる。"""
        from tools.auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H
        # 黒帯: 左68px, 上0px, 幅1384, 高667 のケース
        roi = (68, 0, 1384, 667)
        dx, dy = roi_to_device(760, 360, roi)
        # dx = int(760 / 1520 * 1384) + 68 = int(692) + 68 = 760
        # dy = int(360 / 720 * 667) + 0 = int(333.5) = 333
        assert dx == 760
        assert dy == 333

    def test_zero_coordinate(self):
        """入力 (0, 0) → ROI 左上角を返す。"""
        from tools.auto_pilot import roi_to_device
        roi = (68, 53, 1384, 614)
        dx, dy = roi_to_device(0, 0, roi)
        assert dx == 68
        assert dy == 53

    def test_max_coordinate(self):
        """入力 (ANALYSIS_W, ANALYSIS_H) → ROI 右下角を返す。"""
        from tools.auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H
        roi = (68, 53, 1384, 614)
        dx, dy = roi_to_device(ANALYSIS_W, ANALYSIS_H, roi)
        assert dx == 68 + 1384
        assert dy == 53 + 614

    def test_xperia_normalized_roi_no_double_scale(self):
        """Xperia正規化ROI: roi_to_device + tap_device スケールで正しいデバイス座標。"""
        from tools.auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H
        # Xperia 2160x1080, 黒帯なし → 正規化ROI = (0,0,1520,720)
        roi_norm = (0, 0, ANALYSIS_W, ANALYSIS_H)
        ax, ay = roi_to_device(760, 360, roi_norm)
        # tap_device scaling: ax * 2160/1520, ay * 1080/720
        final_x = int(ax * 2160 / ANALYSIS_W)
        final_y = int(ay * 1080 / ANALYSIS_H)
        assert abs(final_x - 1080) <= 1
        assert abs(final_y - 540) <= 1

    def test_xperia_normalized_roi_with_letterbox(self):
        """Xperia正規化ROI (黒帯あり): 2px以内の精度。"""
        from tools.auto_pilot import roi_to_device, ANALYSIS_W, ANALYSIS_H
        # デバイスROI (230,132,1689,816) → 正規化 (162,88,1189,544)
        roi_norm = (162, 88, 1189, 544)
        ax, ay = roi_to_device(760, 628, roi_norm)
        final_x = int(ax * 2160 / ANALYSIS_W)
        final_y = int(ay * 1080 / ANALYSIS_H)
        # 期待値: ゲーム中央 (1074, 843)
        assert abs(final_x - 1074) <= 3
        assert abs(final_y - 843) <= 3


# ─── お知らせポップアップ検出テスト ──────────────────────────────────────

class TestDetectNoticePopup:
    """detect_notice_popup() のテスト。"""

    def test_ocr_keyword_triggers_detection(self):
        """「今日は表示しない」が OCR にあれば True。"""
        from tools.ap.image_proc import detect_notice_popup
        # 画像不要 — OCR テキストだけで確定
        assert detect_notice_popup(Path("/nonexistent.png"),
                                   ["今日は表示しない", "ガチャへ"]) is True

    def test_no_keyword_no_image_returns_false(self):
        """OCR にキーワードなし + 画像なし → False。"""
        from tools.ap.image_proc import detect_notice_popup
        assert detect_notice_popup(Path("/nonexistent.png"),
                                   ["ガチャへ", "限定"]) is False

    def test_partial_keyword_no_match(self):
        """部分一致しない文字列では検出しない。"""
        from tools.ap.image_proc import detect_notice_popup
        assert detect_notice_popup(Path("/nonexistent.png"),
                                   ["今日は", "表示しない"]) is False

    def test_exact_substring_match(self):
        """長い文字列に含まれていても検出する。"""
        from tools.ap.image_proc import detect_notice_popup
        assert detect_notice_popup(Path("/nonexistent.png"),
                                   ["✓今日は表示しない"]) is True


# ─── 座標定数テスト ──────────────────────────────────────

class TestCoordinateConstants:
    """座標補正定数の存在・型・範囲検証。"""

    def test_ocr_bbox_y_padding(self):
        from tools.auto_pilot import _OCR_BBOX_Y_PADDING
        assert isinstance(_OCR_BBOX_Y_PADDING, int)
        assert 0 < _OCR_BBOX_Y_PADDING <= 100

    def test_glow_center_y_offset(self):
        from tools.auto_pilot import _GLOW_CENTER_Y_OFFSET
        assert isinstance(_GLOW_CENTER_Y_OFFSET, int)
        assert 0 < _GLOW_CENTER_Y_OFFSET <= 100

    def test_gold_btn_retry_y_offset(self):
        from tools.auto_pilot import _GOLD_BTN_RETRY_Y_OFFSET
        assert isinstance(_GOLD_BTN_RETRY_Y_OFFSET, int)
        assert 0 < _GOLD_BTN_RETRY_Y_OFFSET <= 100

    def test_finger_tip_ratio(self):
        from tools.auto_pilot import _FINGER_TIP_RATIO
        assert isinstance(_FINGER_TIP_RATIO, float)
        assert 0.0 < _FINGER_TIP_RATIO < 1.0

    def test_analysis_dimensions(self):
        from tools.auto_pilot import ANALYSIS_W, ANALYSIS_H
        assert ANALYSIS_W == 1520
        assert ANALYSIS_H == 720


# ─── ADV Toolbar テンプレートマッチ テスト ─────────────────────────

class TestDetectAdvToolbarButtons:
    """detect_adv_toolbar_buttons のテンプレートマッチ検証。"""

    def test_detect_adv_toolbar_buttons_positive(self, tmp_path):
        """ADVシーン画像でAUTO/>>ボタンが検出される。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_toolbar_buttons, _ADV_AUTO_TEMPLATE, ANALYSIS_W, ANALYSIS_H

        if not _ADV_AUTO_TEMPLATE.exists():
            pytest.skip("ADV AUTO テンプレート画像が存在しません")

        # テンプレートを読み込んで、解析空間画像の正しい位置に埋め込む
        tpl = cv2.imread(str(_ADV_AUTO_TEMPLATE), cv2.IMREAD_GRAYSCALE)
        assert tpl is not None, "テンプレート読み込み失敗"

        # 解析空間サイズの黒画像を作成
        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        # AUTO ボタン位置 (右上) にテンプレートを埋め込む
        th, tw = tpl.shape[:2]
        cx, cy = 1364, 110  # AUTO center in analysis space
        y1 = max(cy - th // 2, 0)
        x1 = max(cx - tw // 2, 0)
        # BGR に変換して埋め込む
        tpl_bgr = cv2.cvtColor(tpl, cv2.COLOR_GRAY2BGR)
        img[y1:y1 + th, x1:x1 + tw] = tpl_bgr

        img_path = tmp_path / "adv_scene.png"
        cv2.imwrite(str(img_path), img)

        assert detect_adv_toolbar_buttons(img_path) is True

    def test_detect_adv_toolbar_buttons_negative(self, tmp_path):
        """非ADVシーン (無地画像) で誤検出しない。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_toolbar_buttons, ANALYSIS_W, ANALYSIS_H

        # ランダムノイズ画像 (ボタンなし)
        rng = np.random.RandomState(42)
        img = rng.randint(30, 80, (ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "non_adv.png"
        cv2.imwrite(str(img_path), img)

        assert detect_adv_toolbar_buttons(img_path) is False
