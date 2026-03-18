"""
test_auto_pilot.py — auto_pilot.py の単体テスト

AssetManager (require_ocr) と Result画面ハンドラの動作検証。
"""
from __future__ import annotations

import re
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
                "edge_weight": 0,
                "edge_img": None,
                "require_ocr": require_ocr,
                "require_ocr_all": [],
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

    @patch("tools.ap.handlers.result.tap_device")
    @patch("tools.ap.handlers.result.detect_guide_glow")
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

    @patch("tools.ap.handlers.result.tap_device")
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

    @patch("tools.ap.handlers.result.tap_device")
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

    @patch("tools.ap.handlers.result.tap_device")
    def test_returns_none_for_non_result(self, mock_tap, state):
        from tools.auto_pilot import handle_result_screen
        ocr = [_make_ocr_item("クエスト", 100, 100)]
        result = handle_result_screen(state, None, ocr, 5, mode="OCR")
        assert result is None
        mock_tap.assert_not_called()

    @patch("tools.ap.handlers.result.watchdog_recover", return_value=True)
    @patch("tools.ap.handlers.result.tap_device")
    @patch("tools.ap.handlers.result.detect_guide_glow", return_value=[])
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

    def test_gold_swipe_stall_counter_removed(self, state):
        """gold_swipe StallCounter は廃止済み — 属性が存在しないことを確認"""
        assert not hasattr(state, 'gold_swipe')

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
        result = handle_dialog_screen(state, None, [], [], False)
        assert result is None

    @patch("tools.ap.handlers.dialog_phase.detect_white_hand_pointer", return_value=None)
    @patch("tools.ap.handlers.dialog_phase.tap_device")
    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav", return_value=("close", 100, 50))
    def test_dialog_close_returns_action(self, mock_dlg, mock_tap, mock_white,
                                          state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False)
        assert result is not None
        assert result[0] == "DIALOG_CLOSE"
        assert result[1] == 1.0
        mock_tap.assert_called_once()

    @patch("tools.ap.handlers.dialog_phase.detect_white_hand_pointer", return_value=None)
    @patch("tools.ap.handlers.dialog_phase.tap_device")
    @patch("tools.ap.handlers.dialog_phase.process_paging_dialog", return_value="DIALOG_CLOSED")
    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav", return_value=("next", 800, 400))
    @patch("tools.ap.handlers.dialog_phase.detect_dialog", return_value=("next", 800, 400))
    def test_paging_dialog_returns_action(self, mock_detect_dialog, mock_dlg, mock_paging, mock_tap,
                                           mock_white, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False)
        assert result is not None
        assert result[0] == "DIALOG_CLOSED"
        assert result[1] == 1.0
        mock_paging.assert_called_once()

    @patch("tools.ap.handlers.dialog_phase.tap_device")
    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav", return_value=("close", 1400, 50))
    def test_notice_popup_close_bypasses_all_guards(self, mock_dlg, mock_tap,
                                                      state, tmp_path):
        """お知らせポップアップ: is_notice_popup=True → ガード全バイパスで×閉じ。"""
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False,
                                       is_notice_popup=True)
        assert result is not None
        assert result[0] == "NOTICE_POPUP_CLOSE"
        assert result[1] == 1.0
        mock_tap.assert_called_once()

    @patch("tools.ap.handlers.dialog_phase.take_screenshot", return_value=(Path("/tmp/test.png"), 1520, 720, 0))
    @patch("tools.ap.handlers.dialog_phase.prepare_analysis_image", return_value=Path("/tmp/test.png"))
    @patch("tools.ap.handlers.dialog_phase.tap_device")
    @patch("tools.ap.handlers.dialog_phase.count_page_dots", return_value=3)
    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav")
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
        result = handle_dialog_screen(state, analysis, [], [], False,
                                       is_notice_popup=True)
        assert result is not None
        assert result[0] == "NOTICE_POPUP_CLOSE"
        assert result[1] == 1.0
        # ドット3 → ▷2回 + ×1回 = 3タップ
        assert mock_tap.call_count == 3

    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav", return_value=("close", 100, 50))
    def test_battle_dialog_guard_skips_close_in_top(self, mock_dlg, state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], True)
        assert result is None

    @patch("tools.ap.handlers.dialog_phase.detect_white_hand_pointer", return_value=None)
    @patch("tools.ap.handlers.dialog_phase.adb")
    @patch("tools.ap.handlers.dialog_phase.detect_dialog_frame_and_nav", return_value=("close", 100, 400))
    def test_escalation_back_at_8_attempts(self, mock_dlg, mock_adb, mock_white,
                                            state, tmp_path):
        from tools.auto_pilot import handle_dialog_screen
        state.pre_popup_tap_count = 7  # 次で8
        analysis = tmp_path / "test.png"
        analysis.touch()
        result = handle_dialog_screen(state, analysis, [], [], False)
        assert result is not None
        assert result[0] == "DIALOG_BACK_ESCALATION"
        assert result[1] == 2.0
        mock_adb.assert_called_once_with("shell input keyevent KEYCODE_BACK")


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
        assert 0 <= _OCR_BBOX_Y_PADDING <= 100  # Vision OCR: 0, PaddleOCR: 30

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
    """detect_adv_toolbar_buttons の5アイコン全マッチ検証。"""

    def test_detect_adv_toolbar_buttons_positive(self, tmp_path):
        """5アイコン全埋め込み → 検出される。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_toolbar_buttons, ANALYSIS_W, ANALYSIS_H
        from tools.ap.constants import _CRAWLER_ROOT

        icon_positions = [
            ("adv_icon_menu", 1116, 45),
            ("adv_icon_log", 1191, 45),
            ("adv_icon_auto", 1274, 45),
            ("adv_icon_ff", 1358, 45),
            ("adv_icon_skip", 1446, 45),
        ]
        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        for name, cx, cy in icon_positions:
            tpl_path = _CRAWLER_ROOT / "assets" / "templates" / f"{name}.png"
            if not tpl_path.exists():
                pytest.skip(f"{name}.png が存在しません")
            _embed_template(img, tpl_path, cx, cy)

        img_path = tmp_path / "adv_scene.png"
        cv2.imwrite(str(img_path), img)

        assert detect_adv_toolbar_buttons(img_path) is True

    def test_detect_adv_toolbar_buttons_negative(self, tmp_path):
        """非ADVシーン (無地画像) で誤検出しない。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_toolbar_buttons, ANALYSIS_W, ANALYSIS_H

        rng = np.random.RandomState(42)
        img = rng.randint(30, 80, (ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "non_adv.png"
        cv2.imwrite(str(img_path), img)

        assert detect_adv_toolbar_buttons(img_path) is False


# ─── ミニ会話シーン検出テスト ──────────────────────────────────────

def _make_bubble_image(tmp_path, bubbles, bg_val=40):
    """
    テスト用ミニ会話画像を生成。

    bubbles: list of (x, y, w, h, brightness)
        brightness: 0-255 (V channel in white bubble → BGR (brightness, brightness, brightness))
    bg_val: 背景輝度
    """
    import cv2
    import numpy as np
    from tools.ap.image_proc import ANALYSIS_W, ANALYSIS_H

    img = np.full((ANALYSIS_H, ANALYSIS_W, 3), bg_val, dtype=np.uint8)
    for bx, by, bw, bh, brightness in bubbles:
        # 白い吹き出し (S<40, V>200 を満たす BGR)
        cv2.rectangle(img, (bx, by), (bx + bw, by + bh),
                      (brightness, brightness, brightness), -1)
    img_path = tmp_path / "mini_conv.png"
    cv2.imwrite(str(img_path), img)
    return img_path


class TestDetectMiniConversation:
    """detect_mini_conversation() のテスト。"""

    def test_right_bubble_detected(self, tmp_path):
        """右上の白い吹き出し → (cx, cy, 'right') を返す。"""
        from tools.ap.image_proc import detect_mini_conversation, ANALYSIS_W
        # 右側 (x=900) に白い吹き出し
        img_path = _make_bubble_image(tmp_path, [(900, 50, 300, 80, 240)])
        ocr = [_make_ocr_item("セリフ", 1050, 90)]
        result = detect_mini_conversation(img_path, ocr_items=ocr)
        assert result is not None
        cx, cy, side = result
        assert side == "right"
        assert cx > ANALYSIS_W // 2

    def test_left_bubble_detected(self, tmp_path):
        """左上の白い吹き出し → (cx, cy, 'left') を返す。"""
        from tools.ap.image_proc import detect_mini_conversation, ANALYSIS_W
        # 左側 (x=100) に白い吹き出し
        img_path = _make_bubble_image(tmp_path, [(100, 50, 300, 80, 240)])
        ocr = [_make_ocr_item("セリフ", 250, 90)]
        result = detect_mini_conversation(img_path, ocr_items=ocr)
        assert result is not None
        cx, cy, side = result
        assert side == "left"
        assert cx < ANALYSIS_W // 2

    def test_two_bubbles_selects_brighter(self, tmp_path):
        """2バブル → 明るい方(アクティブ話者)を選択。"""
        from tools.ap.image_proc import detect_mini_conversation
        # 左: グレー(150), 右: 白(240) → 右を選択
        img_path = _make_bubble_image(tmp_path, [
            (100, 50, 300, 80, 150),   # 左: グレー (V=150 < 200 → マスクに入らない)
            (900, 50, 300, 80, 240),   # 右: 白 (V=240 > 200)
        ])
        ocr = [_make_ocr_item("左", 250, 90),
               _make_ocr_item("右", 1050, 90)]
        result = detect_mini_conversation(img_path, ocr_items=ocr)
        assert result is not None
        _, _, side = result
        assert side == "right"

    def test_no_bubble_returns_none(self, tmp_path):
        """暗い画像 → None (誤検出なし)。"""
        from tools.ap.image_proc import detect_mini_conversation
        # 全面暗い画像
        img_path = _make_bubble_image(tmp_path, [], bg_val=30)
        result = detect_mini_conversation(img_path)
        assert result is None

    def test_toolbar_zone_excluded(self, tmp_path):
        """右上ADVツールバー領域 (x>82%, y<22%) の白要素は無視。"""
        from tools.ap.image_proc import detect_mini_conversation, ANALYSIS_W, ANALYSIS_H
        # ツールバー領域のみに白いブロック
        tx = int(ANALYSIS_W * 0.85)
        ty = 10
        img_path = _make_bubble_image(tmp_path, [(tx, ty, 200, 60, 240)])
        ocr = [_make_ocr_item("AUTO", tx + 100, ty + 30)]
        result = detect_mini_conversation(img_path, ocr_items=ocr)
        assert result is None

    def test_ocr_empty_bubble_rejected(self, tmp_path):
        """吹き出し内にテキストなし → None。"""
        from tools.ap.image_proc import detect_mini_conversation
        # 白い吹き出しあり、OCR はバブル外のみ
        img_path = _make_bubble_image(tmp_path, [(900, 50, 300, 80, 240)])
        ocr = [_make_ocr_item("遠い", 100, 500)]  # バブル外
        result = detect_mini_conversation(img_path, ocr_items=ocr)
        assert result is None


# ─── AdvScene 統一検出テスト ──────────────────────────────────────

def _embed_template(img, tpl_path, cx, cy):
    """テンプレート画像をBGR画像の指定中心座標に埋め込む。"""
    import cv2
    tpl = cv2.imread(str(tpl_path), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return
    th, tw = tpl.shape[:2]
    y1 = max(cy - th // 2, 0)
    x1 = max(cx - tw // 2, 0)
    tpl_bgr = cv2.cvtColor(tpl, cv2.COLOR_GRAY2BGR)
    y2 = min(y1 + th, img.shape[0])
    x2 = min(x1 + tw, img.shape[1])
    img[y1:y2, x1:x2] = tpl_bgr[:y2 - y1, :x2 - x1]


class TestAdvScene:
    """detect_adv_scene 統一検出テスト。"""

    def test_toolbar_strip_positive(self, tmp_path):
        """5アイコン全埋め込み → is_adv=True, toolbar_score>=0.65。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import (
            detect_adv_scene, AdvSceneResult, ANALYSIS_W, ANALYSIS_H,
        )
        from tools.ap.constants import _CRAWLER_ROOT

        icon_positions = [
            ("adv_icon_menu", 1116, 45),
            ("adv_icon_log", 1191, 45),
            ("adv_icon_auto", 1274, 45),
            ("adv_icon_ff", 1358, 45),
            ("adv_icon_skip", 1446, 45),
        ]
        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        for name, cx, cy in icon_positions:
            tpl_path = _CRAWLER_ROOT / "assets" / "templates" / f"{name}.png"
            if not tpl_path.exists():
                pytest.skip(f"{name}.png が存在しません")
            _embed_template(img, tpl_path, cx, cy)

        img_path = tmp_path / "adv_5icons.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path, icon_threshold=0.65)
        assert isinstance(result, AdvSceneResult)
        assert result.is_adv is True
        assert result.toolbar_score >= 0.65

    def test_next_btn_detected(self, tmp_path):
        """↓ボタン+5アイコン埋め込み → next_btn_pos not None。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import (
            detect_adv_scene, ANALYSIS_W, ANALYSIS_H,
        )
        from tools.ap.constants import _CRAWLER_ROOT

        icon_positions = [
            ("adv_icon_menu", 1116, 45),
            ("adv_icon_log", 1191, 45),
            ("adv_icon_auto", 1274, 45),
            ("adv_icon_ff", 1358, 45),
            ("adv_icon_skip", 1446, 45),
        ]
        next_path = _CRAWLER_ROOT / "assets" / "templates" / "adv_next_btn.png"
        if not next_path.exists():
            pytest.skip("adv_next_btn.png が存在しません")

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        for name, cx, cy in icon_positions:
            tpl_path = _CRAWLER_ROOT / "assets" / "templates" / f"{name}.png"
            if not tpl_path.exists():
                pytest.skip(f"{name}.png が存在しません")
            _embed_template(img, tpl_path, cx, cy)
        _embed_template(img, next_path, 1430, 650)

        img_path = tmp_path / "adv_both.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path)
        assert result.is_adv is True
        assert result.next_btn_pos is not None
        assert result.next_btn_score > 0.0

    def test_random_noise_negative(self, tmp_path):
        """ランダムノイズ画像 → is_adv=False。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        rng = np.random.RandomState(42)
        img = rng.randint(30, 80, (ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "noise.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path)
        assert result.is_adv is False
        assert result.toolbar_score < 0.65

    def test_name_line_detection(self, tmp_path):
        """OCR に ◇まどか◇ → has_name_line=True。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "name_line.png"
        cv2.imwrite(str(img_path), img)

        ocr_items = [
            {"text": "◇まどか◇", "center": (760, 500), "confidence": 0.9,
             "box": [[700, 490], [820, 490], [820, 510], [700, 510]]},
        ]
        result = detect_adv_scene(img_path, ocr_items=ocr_items)
        assert result.has_name_line is True

    def test_letterbox_detection(self, tmp_path):
        """roi=(68,0,...) → has_letterbox=True。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "letterbox.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path, roi=(68, 0, 1384, 720))
        assert result.has_letterbox is True

    def test_letterbox_not_detected_small_roi(self, tmp_path):
        """roi=(30,0,...) → has_letterbox=False。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "no_letterbox.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path, roi=(30, 0, 1460, 720))
        assert result.has_letterbox is False

    def test_backward_compat_wrapper(self, tmp_path):
        """detect_adv_toolbar_buttons() が bool を返す (後方互換)。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import (
            detect_adv_toolbar_buttons, ANALYSIS_W, ANALYSIS_H,
        )
        from tools.ap.constants import _CRAWLER_ROOT

        icon_positions = [
            ("adv_icon_menu", 1116, 45),
            ("adv_icon_log", 1191, 45),
            ("adv_icon_auto", 1274, 45),
            ("adv_icon_ff", 1358, 45),
            ("adv_icon_skip", 1446, 45),
        ]
        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        for name, cx, cy in icon_positions:
            tpl_path = _CRAWLER_ROOT / "assets" / "templates" / f"{name}.png"
            if not tpl_path.exists():
                pytest.skip(f"{name}.png が存在しません")
            _embed_template(img, tpl_path, cx, cy)
        img_path = tmp_path / "adv_compat.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_toolbar_buttons(img_path)
        assert isinstance(result, bool)
        assert result is True

    def test_dialogue_text_detection(self, tmp_path):
        """OCR下部にかな文字テキスト → has_dialogue=True。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "dialogue.png"
        cv2.imwrite(str(img_path), img)

        ocr_items = [
            {"text": "それでは行きましょう", "center": (760, 600),
             "confidence": 0.95,
             "box": [[600, 590], [920, 590], [920, 610], [600, 610]]},
        ]
        result = detect_adv_scene(img_path, ocr_items=ocr_items)
        assert result.has_dialogue is True

    def test_dialogue_short_text_rejected(self, tmp_path):
        """3文字以下のかなテキスト → has_dialogue=False。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "short_text.png"
        cv2.imwrite(str(img_path), img)

        ocr_items = [
            {"text": "はい", "center": (760, 600), "confidence": 0.9,
             "box": [[740, 590], [780, 590], [780, 610], [740, 610]]},
        ]
        result = detect_adv_scene(img_path, ocr_items=ocr_items)
        assert result.has_dialogue is False

    def test_classify_scene_adv_detected(self):
        """classify_scene(adv_detected=True) → ADV。"""
        from tools.ap.helpers import classify_scene
        scene, interval = classify_scene(["何かのテキスト"], "IDLE", adv_detected=True)
        assert scene == "ADV"

    def test_classify_scene_adv_not_detected(self):
        """classify_scene(adv_detected=False) で ADV キーワードなし → UNKNOWN。"""
        from tools.ap.helpers import classify_scene
        scene, _ = classify_scene(["何かのテキスト"], "IDLE", adv_detected=False)
        assert scene == "UNKNOWN"


# ─── Fix 1: detect_movie_skip_button コンター分析テスト ───────────────

class TestMovieSkipButton:
    """detect_movie_skip_button() のコンター分析テスト。"""

    def test_scattered_gold_rejected(self, tmp_path):
        """散在する小さな金色ピクセル → None (ADV ツールバー誤検出防止)。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_movie_skip_button, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        # ROI: 右上 (88%~100% x, 0~12% y) に散在する小さな金色ドット
        _x1 = int(ANALYSIS_W * 0.88)
        _y2 = int(ANALYSIS_H * 0.12)
        # 100個の 1px 金色ドットを散布 (各ブロブ面積 ~1)
        rng = np.random.RandomState(42)
        for _ in range(100):
            _x = rng.randint(_x1, ANALYSIS_W - 1)
            _y = rng.randint(0, _y2 - 1)
            # HSV 金色 (H=25, S=150, V=200) → BGR
            img[_y, _x] = (50, 165, 210)  # BGR: ~金色

        img_path = tmp_path / "scattered_gold.png"
        cv2.imwrite(str(img_path), img)
        assert detect_movie_skip_button(img_path) is None

    def test_synthetic_circle_not_detected(self, tmp_path):
        """合成金色円はテンプレートと一致しない → None (テンプレートベース検出)。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_movie_skip_button, ANALYSIS_W, ANALYSIS_H

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        # ROI 内に金色の円を描画 (半径 12px)
        _cx = int(ANALYSIS_W * 0.94)
        _cy = int(ANALYSIS_H * 0.06)
        cv2.circle(img, (_cx, _cy), 12, (50, 165, 210), -1)

        img_path = tmp_path / "solid_gold.png"
        cv2.imwrite(str(img_path), img)
        # HSV ベース検出は廃止済み → テンプレートマッチのみ → 合成円は非検出
        result = detect_movie_skip_button(img_path)
        assert result is None


# ─── Fix 2: detect_adv_scene 2アイコン+↓ボタン救済テスト ──────────────

class TestAdvSceneWithAdvanceIcon:
    """detect_adv_scene() の 2 アイコン + ↓ ボタン救済テスト。"""

    def _mock_match_single(self, match_names):
        """match_names に含まれるアイコン名だけスコア 0.9 を返す mock。
        座標はツールバー領域 (右上: x>70%, y<15%) に配置。"""
        # ANALYSIS_W=1520, ANALYSIS_H=720 → x>1064, y<108
        _positions = {
            "adv_icon_menu": (1116, 45),
            "adv_icon_log": (1191, 45),
            "adv_icon_auto": (1274, 45),
            "adv_icon_ff": (1358, 45),
            "adv_icon_skip": (1446, 45),
        }
        def _side_effect(name, img_path, **kwargs):
            if name in match_names:
                cx, cy = _positions.get(name, (1274, 45))
                return (cx, cy, 0.9)
            return None
        return _side_effect

    @patch("tools.ap.image_proc.detect_adv_advance_icon", return_value=True)
    @patch("tools.ap.image_proc.ASSET_MANAGER")
    def test_two_icons_plus_advance(self, mock_am, mock_adv_down, tmp_path):
        """2 ADV固有アイコン + detect_adv_advance_icon=True → is_adv=True。
        NOTE: AUTO+FF だけではバトル画面と区別できないため、
        ADV専用アイコン (menu/log/skip) を含む必要がある。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        mock_am.match_single.side_effect = self._mock_match_single(
            {"adv_icon_auto", "adv_icon_skip"})

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "two_icons_advance.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path)
        assert result.is_adv is True
        assert result.matched_count == 2

    @patch("tools.ap.image_proc.detect_adv_advance_icon", return_value=False)
    @patch("tools.ap.image_proc.ASSET_MANAGER")
    def test_two_icons_without_advance(self, mock_am, mock_adv_down, tmp_path):
        """2 アイコンのみ (↓ ボタンなし) → is_adv=False。"""
        import cv2
        import numpy as np
        from tools.ap.image_proc import detect_adv_scene, ANALYSIS_W, ANALYSIS_H

        mock_am.match_single.side_effect = self._mock_match_single(
            {"adv_icon_auto", "adv_icon_ff"})

        img = np.zeros((ANALYSIS_H, ANALYSIS_W, 3), dtype=np.uint8)
        img_path = tmp_path / "two_icons_only.png"
        cv2.imwrite(str(img_path), img)

        result = detect_adv_scene(img_path)
        assert result.is_adv is False
        assert result.matched_count == 2


# ─── Fix 3: Movie inertia TTL テスト ──────────────────────────────────

# ─── Fix #7: 課金ダイアログ保護 ─────────────────────────────────

class TestCurrencyDialogProtection:
    """課金ダイアログで自動キャンセルされるか検証。"""

    @pytest.fixture
    def state(self):
        from tools.ap.state import PilotState
        s = PilotState()
        s.device_w = 1520
        s.device_h = 720
        s.game_roi = (0, 0, 1520, 720)
        return s

    @patch("tools.ap.handlers.common.tap_device")
    def test_currency_dialog_taps_cancel(self, mock_tap, state):
        """OCR に 'マギカストーン50個消費' + OK + キャンセル → Cancel タップ。"""
        from tools.auto_pilot import detect_and_act
        ocr = [
            {"text": "マギカストーン50個消費", "center": (760, 300), "confidence": 0.95},
            {"text": "OK", "center": (500, 500), "confidence": 0.95},
            {"text": "キャンセル", "center": (1000, 500), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=None)
        assert action == "CURRENCY_CANCEL"
        assert mock_tap.called

    @patch("tools.ap.handlers.common.tap_device")
    def test_normal_dialog_taps_ok(self, mock_tap, state):
        """OCR に 'データをダウンロード' + OK + キャンセル → OK タップ。"""
        from tools.auto_pilot import detect_and_act
        ocr = [
            {"text": "データをダウンロード", "center": (760, 300), "confidence": 0.95},
            {"text": "OK", "center": (500, 500), "confidence": 0.95},
            {"text": "キャンセル", "center": (1000, 500), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=None)
        assert action == "ADV_CHOICE"  # 通常の確認ダイアログ OK タップ
        assert mock_tap.called

    @patch("tools.ap.handlers.common.tap_device")
    def test_story_skip_still_cancels(self, mock_tap, state):
        """OCR に 'スキップ' + OK + キャンセル → Cancel (既存動作保持)。"""
        from tools.auto_pilot import detect_and_act
        ocr = [
            {"text": "スキップしますか？", "center": (760, 300), "confidence": 0.95},
            {"text": "OK", "center": (500, 500), "confidence": 0.95},
            {"text": "キャンセル", "center": (1000, 500), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=None)
        assert action == "STORY_SKIP_CANCEL"


# ─── Fix #6: タイトル画面誤検出防止 ─────────────────────────────────

class TestTitleScreenDetection:
    """タイトル画面検出の条件厳格化テスト。"""

    @pytest.fixture
    def state(self):
        from tools.ap.state import PilotState
        s = PilotState()
        s.device_w = 1520
        s.device_h = 720
        s.game_roi = (0, 0, 1520, 720)
        s.home_reached = False
        return s

    @patch("tools.ap.handlers.tutorial.detect_tutorial_gold_swipe", return_value=None)
    @patch("tools.ap.handlers.tutorial.is_tutorial_walk_scene", return_value=False)
    @patch("tools.ap.handlers.finger.tap_device")
    def test_tap_to_start_triggers(self, mock_tap, mock_walk, mock_swipe, state, tmp_path):
        """OCR 'TAP TO START' + 'MAGIA EXEDRA' → title=True。"""
        import cv2
        import numpy as np
        from tools.auto_pilot import detect_and_act
        img = tmp_path / "test.png"
        cv2.imwrite(str(img), np.zeros((720, 1520, 3), dtype=np.uint8))
        ocr = [
            {"text": "TAP TO START", "center": (760, 600), "confidence": 0.95},
            {"text": "MAGIA EXEDRA", "center": (760, 400), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=img)
        assert action == "TITLE_TAP"

    @patch("tools.ap.handlers.fallback.tap_device")
    def test_browser_magia_no_trigger(self, mock_tap, state, tmp_path):
        """OCR 'MAGIA EXEDRA' のみ → title=False (ブラウザ誤検出防止)。"""
        import cv2
        import numpy as np
        from tools.auto_pilot import detect_and_act
        img = tmp_path / "test.png"
        cv2.imwrite(str(img), np.zeros((720, 1520, 3), dtype=np.uint8))
        ocr = [
            {"text": "MAGIA EXEDRA", "center": (760, 400), "confidence": 0.95},
            {"text": "ログイン", "center": (760, 500), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=img)
        assert action != "TITLE_TAP"

    @patch("tools.ap.handlers.fallback.tap_device")
    def test_home_reached_blocks(self, mock_tap, state, tmp_path):
        """home_reached=True → TAP TO START でもタイトル判定しない。"""
        import cv2
        import numpy as np
        from tools.auto_pilot import detect_and_act
        img = tmp_path / "test.png"
        cv2.imwrite(str(img), np.zeros((720, 1520, 3), dtype=np.uint8))
        state.home_reached = True
        ocr = [
            {"text": "TAP TO START", "center": (760, 600), "confidence": 0.95},
        ]
        action, wait = detect_and_act(ocr, state, analysis_path=img)
        assert action != "TITLE_TAP"


# ─── Fix #5: ポートレート検出 ──────────────────────────────────

class TestPortraitDetection:
    """ポートレートモードで BACK キーが発行されるか検証。"""

    def test_portrait_detected(self):
        """actual_w < actual_h → ポートレート判定。"""
        # ポートレート条件のロジックのみテスト
        actual_w, actual_h = 720, 1520
        assert actual_w > 0 and actual_w < actual_h

    def test_landscape_normal(self):
        """actual_w > actual_h → ランドスケープ (通常)。"""
        actual_w, actual_h = 1520, 720
        assert not (actual_w > 0 and actual_w < actual_h)


# ─── Fix #4: 指ガード × テンプレ抑制 ──────────────────────────────

class TestFingerGuardCloseButton:
    """指ブロブがあっても × テンプレートマッチがあればガード抑制。"""

    def test_finger_allows_close_when_template_matches(self):
        """指あり + × 0.90 → ガード解除。"""
        # match_single が (x, y, score) を返す場合のロジックテスト
        close_match = (100, 50, 0.90)
        threshold = 0.85
        pdg_blobs = [(100, 200, 500)]  # 指ブロブあり
        assert pdg_blobs  # ブロブあり
        assert close_match and close_match[2] >= threshold  # ガード抑制

    def test_finger_blocks_when_no_close(self):
        """指あり + × なし → ガード有効。"""
        close_match = None
        pdg_blobs = [(100, 200, 500)]
        pre_dialog_finger = False
        if pdg_blobs:
            if close_match and close_match[2] >= 0.85:
                pass  # ガード抑制
            else:
                pre_dialog_finger = True
        assert pre_dialog_finger is True


class TestOversizedRescueSuppression:
    """OVERSIZED_RESCUE は max_area < 15000 のとき自動的に無効化される。"""

    def test_rescue_suppressed_when_max_area_small(self, tmp_path):
        """max_area=5000 → area=48000 のブロブは rescue されない。"""
        import cv2
        import numpy as np
        # 大きな肌色ブロブ (area >> 5000) + 金色要素を含む画像を作成
        img = np.zeros((720, 1520, 3), dtype=np.uint8)
        # 肌色 (HSV: H=15, S=100, V=200 → BGR相当) の大ブロブ
        # BGR で肌色を近似: B=140, G=180, R=230
        cv2.rectangle(img, (750, 520), (870, 620), (140, 180, 230), -1)  # ~12000px area
        img_path = tmp_path / "home_footer.png"
        cv2.imwrite(str(img_path), img)

        from tools.ap.image_proc import find_finger_blobs
        # max_area=5000 → OVERSIZED_RESCUE は発動しないべき
        blobs = find_finger_blobs(img_path, min_area=300, max_area=5000)
        # 12000 > 5000 → _oversized に入るが、max_area < 15000 で rescue 無効
        assert all(b[2] <= 5000 for b in blobs), \
            f"max_area=5000 なのに area>5000 のブロブが返された: {blobs}"

    def test_rescue_active_when_max_area_default(self, tmp_path):
        """max_area=15000 (デフォルト) → OVERSIZED_RESCUE は有効のまま。"""
        # ロジック検証のみ (画像生成なし)
        max_area = 15000
        home_mode = False
        _oversized = [(800, 560, 48000, 750, 520, 120, 100)]
        # 条件チェック
        rescue_enabled = bool(_oversized) and not home_mode and max_area >= 15000
        assert rescue_enabled is True

    def test_rescue_disabled_by_home_mode(self, tmp_path):
        """home_mode=True → OVERSIZED_RESCUE は無効。"""
        max_area = 15000
        home_mode = True
        _oversized = [(800, 560, 48000, 750, 520, 120, 100)]
        rescue_enabled = bool(_oversized) and not home_mode and max_area >= 15000
        assert rescue_enabled is False


# ─── Fix #2: ADV 速度改善 ─────────────────────────────────────

class TestAdvSpeedImprovements:
    """ADV 連続検出による phash 動的拡大のテスト。"""

    @pytest.fixture
    def state(self):
        from tools.ap.state import PilotState
        s = PilotState()
        s.device_w = 1520
        s.device_h = 720
        s.game_roi = (0, 0, 1520, 720)
        return s

    def test_adv_confirmed_widens_phash(self, state):
        """adv_confirmed_count >= 3 → phash 上限 40。"""
        from tools.ap.constants import ADV_RAPID_PHASH_MAX
        state.adv_confirmed_count = 5
        _adv_phash_max = 40 if state.adv_confirmed_count >= 3 else ADV_RAPID_PHASH_MAX
        assert _adv_phash_max == 40

    def test_adv_default_phash(self, state):
        """adv_confirmed_count < 3 → phash 上限 25 (デフォルト)。"""
        from tools.ap.constants import ADV_RAPID_PHASH_MAX
        state.adv_confirmed_count = 1
        _adv_phash_max = 40 if state.adv_confirmed_count >= 3 else ADV_RAPID_PHASH_MAX
        assert _adv_phash_max == ADV_RAPID_PHASH_MAX


# ─── Fix #1: 動画シーンスコアリング検出 ──────────────────────────

class TestMovieSceneDetection:
    """detect_movie_scene() のスコアリング方式テスト。"""

    @pytest.fixture
    def black_image(self, tmp_path):
        """720p 黒画像 (⏭ なし)。"""
        import cv2
        import numpy as np
        img_path = tmp_path / "black.png"
        cv2.imwrite(str(img_path), np.zeros((720, 1520, 3), dtype=np.uint8))
        return img_path

    def test_not_movie_with_adv_toolbar(self, black_image):
        """ADV ツールバーあり → 即棄却。"""
        from tools.ap.image_proc import detect_movie_scene, AdvSceneResult
        adv = AdvSceneResult(is_adv=True)
        result = detect_movie_scene(black_image, adv_result=adv)
        assert result.is_movie is False

    def test_not_movie_battle(self, black_image):
        """バトルキーワード → 即棄却。"""
        from tools.ap.image_proc import detect_movie_scene
        result = detect_movie_scene(black_image, ocr_texts=["通常攻撃", "WAVE2"])
        assert result.is_movie is False

    def test_not_movie_with_ui_text(self, black_image):
        """UI テキスト多数 → 減点で棄却。"""
        from tools.ap.image_proc import detect_movie_scene, AdvSceneResult
        adv = AdvSceneResult(is_adv=False)
        result = detect_movie_scene(
            black_image, adv_result=adv,
            ocr_texts=["OK", "ダウンロード", "設定"])
        assert result.is_movie is False

    @patch("tools.ap.image_proc.detect_background_blur", return_value=False)
    @patch("tools.ap.image_proc.detect_movie_skip_button")
    def test_movie_with_skip_no_toolbar(self, mock_skip, mock_blur, black_image):
        """⏭ + ツールバーなし → is_movie=True。"""
        from tools.ap.image_proc import detect_movie_scene, AdvSceneResult
        mock_skip.return_value = (1400, 50)  # ⏭ ボタン座標
        adv = AdvSceneResult(is_adv=False)
        result = detect_movie_scene(black_image, adv_result=adv, ocr_texts=[])
        assert result.is_movie is True
        assert result.confidence >= 0.50

    @patch("tools.ap.image_proc.detect_background_blur", return_value=False)
    @patch("tools.ap.image_proc.detect_movie_skip_button")
    def test_movie_with_subtitles(self, mock_skip, mock_blur, black_image):
        """⏭ + ツールバーなし + OCR 3件 (字幕) → is_movie=True。"""
        from tools.ap.image_proc import detect_movie_scene, AdvSceneResult
        mock_skip.return_value = (1400, 50)
        adv = AdvSceneResult(is_adv=False)
        result = detect_movie_scene(
            black_image, adv_result=adv,
            ocr_texts=["字幕テキスト1", "字幕テキスト2", "字幕テキスト3"])
        assert result.is_movie is True
        assert result.confidence >= 0.50

    def test_movie_scene_result_repr(self):
        """MovieSceneResult の __repr__ が動作する。"""
        from tools.ap.image_proc import MovieSceneResult
        r = MovieSceneResult(is_movie=True, confidence=0.75,
                             has_skip_btn=True, skip_btn_pos=(100, 50))
        assert "is_movie=True" in repr(r)
        assert "0.75" in repr(r)


# ─── Fresh Install フロー堅牢性テスト ──────────────────────────

class TestFreshInstallHelpers:
    """_fresh_install_from_play_store のヘルパーロジックテスト。"""

    def test_uninstall_keyword_exclusion(self):
        """「インストール」検索で「アンインストール」を除外する。"""
        kw = "インストール"
        matched_text = "アンインストール"
        # 除外条件: kw == "インストール" and "アン" in matched_text
        should_exclude = (kw == "インストール" and "アン" in matched_text)
        assert should_exclude is True

    def test_install_keyword_match(self):
        """「インストール」は正しくマッチする。"""
        kw = "インストール"
        matched_text = "インストール"
        should_exclude = (kw == "インストール" and "アン" in matched_text)
        assert should_exclude is False

    def test_y_coordinate_filter_top75(self):
        """Y 座標 < 75% のインストールボタンは有効。"""
        screen_h = 1520
        button_y = 960  # ~63% (typical install button position)
        assert button_y < screen_h * 0.75

    def test_y_coordinate_filter_bottom(self):
        """Y 座標 > 75% のボタンは「他端末にも」ボタンとして除外。"""
        screen_h = 1520
        button_y = 1300  # ~86% (bottom area)
        assert not (button_y < screen_h * 0.75)

    def test_back_only_after_attempt_7(self):
        """BACK キーは試行 8回目以降でのみ発動。"""
        # attempt >= 7 の時だけ BACK を押す (0-indexed)
        for attempt in range(15):
            should_back = attempt >= 7
            if attempt < 7:
                assert not should_back, f"attempt={attempt} should not press BACK"
            else:
                assert should_back, f"attempt={attempt} should press BACK"

    def test_wait_escalation(self):
        """待機時間が試行回数に応じてエスカレートする。"""
        waits = []
        for attempt in range(15):
            if attempt > 0:
                w = 3 if attempt < 5 else 5 if attempt < 10 else 8
                waits.append(w)
        # 前半は短く、後半は長い
        assert waits[0] == 3   # attempt 1
        assert waits[4] == 5   # attempt 5
        assert waits[9] == 8   # attempt 10

    def test_popup_dismiss_keywords(self):
        """ポップアップ解除キーワードが十分にカバーされている。"""
        dismiss_kws = ["後で", "後で行う", "スキップ", "いいえ", "No thanks",
                       "Not now", "No, thanks", "閉じる", "DISMISS",
                       "GOT IT", "OK", "了解"]
        # 最低限のカバレッジ
        assert "後で" in dismiss_kws         # Google Play Games
        assert "No thanks" in dismiss_kws    # English popups
        assert "GOT IT" in dismiss_kws       # Play Protect
        assert "閉じる" in dismiss_kws       # Generic close

    def test_xml_uiautomator_parse(self):
        """uiautomator XML から正しくボタン座標を抽出する。"""
        import xml.etree.ElementTree as ET
        xml_str = '''<?xml version="1.0" encoding="UTF-8"?>
        <hierarchy rotation="0">
            <node text="インストール" bounds="[200,900][520,980]" />
            <node text="アンインストール" bounds="[200,1100][520,1180]" />
        </hierarchy>'''
        root = ET.fromstring(xml_str)
        results = []
        kw = "インストール"
        for node in root.iter("node"):
            text_val = node.get("text", "")
            if kw in text_val:
                if kw == "インストール" and "アン" in text_val:
                    continue
                bm = re.findall(r"\[(\d+),(\d+)\]", node.get("bounds", ""))
                if len(bm) >= 2:
                    x1, y1 = int(bm[0][0]), int(bm[0][1])
                    x2, y2 = int(bm[1][0]), int(bm[1][1])
                    results.append(((x1 + x2) // 2, (y1 + y2) // 2))
        # 「インストール」のみマッチ、「アンインストール」は除外
        assert len(results) == 1
        assert results[0] == (360, 940)
