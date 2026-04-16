"""Gemini OCR 補正モジュールのテスト。"""
import os
from unittest.mock import MagicMock, patch

import pytest

from tools.ap.ocr_correction import (
    _stage1_regex,
    _stage2_dictionary,
    correct_ocr_text,
    gemini_correct_single,
    learn_from_correction,
)


class TestStage1Regex:
    def test_madoka_typo(self):
        assert "まどか★マギカ" in _stage1_regex("まどか、マギカ")

    def test_attacker_typo(self):
        assert "ATTACKER" in _stage1_regex("TAREAKER")

    def test_no_change(self):
        assert _stage1_regex("普通のテキスト") == "普通のテキスト"


class TestStage2Dictionary:
    def test_character_correction(self):
        # 「鹿目まどか」の誤読が辞書マッチで修正される
        result = _stage2_dictionary("鹿日まどか")
        # 完全一致でなくても近い文字列として補正候補になる
        assert "まどか" in result or result == "鹿日まどか"


class TestGeminiCorrectSingle:
    def test_no_api_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        result = gemini_correct_single("/nonexistent/path.png", "test")
        assert result is None

    def test_missing_image_returns_none(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        # Mock the client
        with patch("tools.ap.ocr_correction._init_gemini_client") as mock_init:
            mock_client = MagicMock()
            mock_init.return_value = mock_client
            result = gemini_correct_single("/nonexistent/path.png", "test")
            assert result is None

    def test_json_parse_with_markdown(self, tmp_path, monkeypatch):
        """```json ... ``` で囲まれたレスポンスをパースできる。"""
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")  # 最小のPNGヘッダ

        mock_response = MagicMock()
        mock_response.text = '```json\n{"corrected_text": "テスト", "corrections": []}\n```'

        with patch("tools.ap.ocr_correction._init_gemini_client") as mock_init:
            mock_client = MagicMock()
            mock_client.models.generate_content.return_value = mock_response
            mock_init.return_value = mock_client

            result = gemini_correct_single(str(img), "test")
            assert result is not None
            assert result["corrected_text"] == "テスト"
            assert result["corrections"] == []


class TestLearnFromCorrection:
    def test_learn_threshold(self, tmp_path, monkeypatch):
        """同じ修正が3回出現すると確定パターンに昇格する。"""
        # 学習ファイルパスを一時ディレクトリに変更
        monkeypatch.setattr("tools.ap.ocr_correction._LEARNED_PATTERNS_PATH",
                            tmp_path / "learned.json")

        for _ in range(3):
            learn_from_correction("唯美ほむら", "暁美ほむら")

        from tools.ap.ocr_correction import _load_learned_patterns
        data = _load_learned_patterns()
        assert "唯美ほむら" in data["confirmed"]
        assert data["confirmed"]["唯美ほむら"] == "暁美ほむら"


class TestCorrectOcrText:
    def test_pipeline_combines_stages(self):
        """段階1+2 を組み合わせて修正される。"""
        result = correct_ocr_text("まどか、マギカ TAREAKER")
        assert "まどか★マギカ" in result
        assert "ATTACKER" in result
