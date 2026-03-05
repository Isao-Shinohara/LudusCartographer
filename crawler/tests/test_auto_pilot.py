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
