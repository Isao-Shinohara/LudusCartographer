"""
test_ai_analyzer.py — GameAnalyzer ユニットテスト

Vertex AI への実際の接続は不要。
モックを使って JSON パース・エラーハンドリング・
インターフェースを完全に検証する。
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai_analyzer import (
    AnalysisResult,
    ButtonInfo,
    GameAnalyzer,
    analyzer_from_env,
)

FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "test_game_screen.png"


# ============================================================
# サンプルレスポンス
# ============================================================

SAMPLE_RESPONSE = json.dumps({
    "screen_type": "ホーム画面",
    "confidence": 0.95,
    "buttons": [
        {
            "name":        "クエスト",
            "position":    "画面下部タブ左",
            "priority":    1,
            "description": "クエスト一覧画面へ遷移する",
        },
        {
            "name":        "ショップ",
            "position":    "画面下部タブ中央",
            "priority":    1,
            "description": "アイテム購入画面へ遷移する",
        },
        {
            "name":        "ガチャ",
            "position":    "画面下部タブ右",
            "priority":    2,
            "description": "ガチャ画面へ遷移する",
        },
    ],
}, ensure_ascii=False)

MINIMAL_RESPONSE = json.dumps({
    "screen_type": "タイトル画面",
    "confidence": 0.99,
    "buttons": [
        {"name": "タップしてスタート", "position": "画面中央", "priority": 1, "description": ""},
    ],
}, ensure_ascii=False)


# ============================================================
# GameAnalyzer 初期化テスト
# ============================================================

class TestGameAnalyzerInit:

    def test_project_id_from_arg(self):
        analyzer = GameAnalyzer(project_id="my-project")
        assert analyzer.project_id == "my-project"

    def test_project_id_from_env(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "env-project")
        analyzer = GameAnalyzer(project_id="")
        assert analyzer.project_id == "env-project"

    def test_raises_without_project_id(self, monkeypatch):
        monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
        with pytest.raises(ValueError, match="GCP プロジェクト ID"):
            GameAnalyzer(project_id="")

    def test_default_location_is_asia(self):
        analyzer = GameAnalyzer(project_id="p")
        assert analyzer.location == "asia-northeast1"

    def test_default_model_is_flash(self):
        analyzer = GameAnalyzer(project_id="p")
        assert "flash" in analyzer.model.lower()

    def test_custom_model(self):
        analyzer = GameAnalyzer(project_id="p", model="gemini-1.5-pro-002")
        assert analyzer.model == "gemini-1.5-pro-002"


# ============================================================
# _parse_response テスト（Vertex AI 不要）
# ============================================================

class TestParseResponse:

    @pytest.fixture
    def analyzer(self):
        return GameAnalyzer(project_id="test-project")

    def test_parses_screen_type(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.screen_type == "ホーム画面"

    def test_parses_confidence(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.confidence == 0.95

    def test_parses_buttons_count(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert len(result.buttons) == 3

    def test_parses_button_name(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.buttons[0].name == "クエスト"

    def test_parses_button_position(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.buttons[0].position == "画面下部タブ左"

    def test_parses_button_priority(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.buttons[0].priority == 1

    def test_is_ok_on_success(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.is_ok is True
        assert result.error is None

    def test_handles_invalid_json(self, analyzer):
        result = analyzer._parse_response("not valid json {{{")
        assert result.is_ok is False
        assert result.error is not None
        assert result.screen_type == "不明"

    def test_handles_json_in_code_block(self, analyzer):
        """```json ... ``` で囲まれた Gemini レスポンスを処理できること"""
        wrapped = f"```json\n{SAMPLE_RESPONSE}\n```"
        result = analyzer._parse_response(wrapped)
        assert result.screen_type == "ホーム画面"
        assert len(result.buttons) == 3

    def test_skips_buttons_without_name(self, analyzer):
        """name が空のボタンは除外されること"""
        resp = json.dumps({
            "screen_type": "X",
            "confidence": 1.0,
            "buttons": [
                {"name": "", "position": "top", "priority": 1},
                {"name": "OK", "position": "center", "priority": 1},
            ],
        })
        result = analyzer._parse_response(resp)
        assert len(result.buttons) == 1
        assert result.buttons[0].name == "OK"

    def test_minimal_response(self, analyzer):
        result = analyzer._parse_response(MINIMAL_RESPONSE)
        assert result.screen_type == "タイトル画面"
        assert result.confidence == 0.99

    def test_raw_response_is_stored(self, analyzer):
        result = analyzer._parse_response(SAMPLE_RESPONSE)
        assert result.raw_response == SAMPLE_RESPONSE


# ============================================================
# analyze() モックテスト（Vertex AI 接続なし）
# ============================================================

class TestAnalyzeMock:

    @pytest.fixture
    def analyzer(self):
        return GameAnalyzer(project_id="test-project")

    def _make_mock_client(self, response_text: str):
        mock_response = MagicMock()
        mock_response.text = response_text
        mock_client = MagicMock()
        mock_client.generate_content.return_value = mock_response
        return mock_client

    def test_analyze_returns_result(self, analyzer):
        mock_client = self._make_mock_client(SAMPLE_RESPONSE)
        analyzer._client = mock_client

        result = analyzer.analyze(FIXTURE_IMAGE)

        assert isinstance(result, AnalysisResult)
        assert result.screen_type == "ホーム画面"
        assert len(result.buttons) == 3

    def test_analyze_calls_generate_content(self, analyzer):
        mock_client = self._make_mock_client(SAMPLE_RESPONSE)
        analyzer._client = mock_client

        analyzer.analyze(FIXTURE_IMAGE)

        mock_client.generate_content.assert_called_once()

    def test_analyze_nonexistent_file_returns_error(self, analyzer):
        result = analyzer.analyze("/nonexistent/path/screenshot.png")
        assert result.is_ok is False
        assert "見つかりません" in (result.error or "")

    def test_analyze_bytes_returns_result(self, analyzer):
        mock_client = self._make_mock_client(MINIMAL_RESPONSE)
        analyzer._client = mock_client

        with open(FIXTURE_IMAGE, "rb") as f:
            image_bytes = f.read()

        result = analyzer.analyze_bytes(image_bytes, "image/png")
        assert result.screen_type == "タイトル画面"

    def test_analyze_handles_api_exception(self, analyzer):
        mock_client = MagicMock()
        mock_client.generate_content.side_effect = Exception("API quota exceeded")
        analyzer._client = mock_client

        result = analyzer.analyze(FIXTURE_IMAGE)
        assert result.is_ok is False
        assert "API quota exceeded" in (result.error or "")


# ============================================================
# AnalysisResult の to_dict テスト
# ============================================================

class TestAnalysisResultSerialization:

    def test_to_dict_keys(self):
        result = AnalysisResult(
            screen_type="ホーム画面",
            buttons=[ButtonInfo(name="クエスト", position="下部")],
            confidence=0.9,
        )
        d = result.to_dict()
        assert set(d.keys()) == {"screen_type", "buttons", "confidence", "error"}

    def test_to_dict_buttons_are_dicts(self):
        result = AnalysisResult(
            screen_type="X",
            buttons=[ButtonInfo(name="A", position="top")],
        )
        d = result.to_dict()
        assert isinstance(d["buttons"][0], dict)
        assert d["buttons"][0]["name"] == "A"

    def test_is_json_serializable(self):
        result = AnalysisResult(
            screen_type="バトル画面",
            buttons=[ButtonInfo(name="攻撃", position="右下", priority=1)],
            confidence=0.88,
        )
        payload = json.dumps(result.to_dict(), ensure_ascii=False)
        assert "バトル画面" in payload
        assert "攻撃" in payload


# ============================================================
# analyzer_from_env テスト
# ============================================================

class TestAnalyzerFromEnv:

    def test_from_env_uses_gcp_project_id(self, monkeypatch):
        monkeypatch.setenv("GCP_PROJECT_ID", "my-env-project")
        monkeypatch.setenv("GCP_LOCATION", "us-central1")
        monkeypatch.setenv("VERTEX_AI_MODEL", "gemini-1.5-pro-002")

        analyzer = analyzer_from_env()
        assert analyzer.project_id == "my-env-project"
        assert analyzer.location == "us-central1"
        assert analyzer.model == "gemini-1.5-pro-002"
