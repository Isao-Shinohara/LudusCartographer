"""Gemini プロンプトを SYSTEM (固定) + USER (動的) に分割し、Implicit Cache を
有効化するリファクタの検証テスト。

設計意図:
  - _GEMINI_SYSTEM_PROMPT は固定 (動的値 placeholder を含まない) で
    リクエスト毎に同一 → Gemini の Implicit Cache 対象になる
  - _GEMINI_USER_TEMPLATE は scene_hint / ocr_text を埋め込むテンプレ
  - REST API: body に systemInstruction を含める
  - SDK API: config に system_instruction を渡す
  - 後方互換: _GEMINI_PROMPT / _GEMINI_BATCH_PROMPT は SYSTEM + USER 連結版
"""
from __future__ import annotations

import json
import pytest


# ─── SYSTEM プロンプトの「固定性」検証 ─────────────────

class TestSystemPromptIsStatic:
    def test_single_system_has_no_scene_placeholder(self):
        """_GEMINI_SYSTEM_PROMPT に動的値 placeholder が含まれない。

        含まれているとリクエスト毎に文字列が変わり Implicit Cache が効かない。
        """
        from tools.ap.ocr_correction import _GEMINI_SYSTEM_PROMPT
        assert "{scene_hint}" not in _GEMINI_SYSTEM_PROMPT, \
            "SYSTEM に scene_hint placeholder があると Cache が効かない"
        assert "{ocr_text}" not in _GEMINI_SYSTEM_PROMPT, \
            "SYSTEM に ocr_text placeholder があると Cache が効かない"

    def test_batch_system_has_no_block_placeholder(self):
        from tools.ap.ocr_correction import _GEMINI_BATCH_SYSTEM_PROMPT
        assert "{ocr_block}" not in _GEMINI_BATCH_SYSTEM_PROMPT, \
            "BATCH_SYSTEM に ocr_block placeholder があると Cache が効かない"

    def test_single_system_is_large_enough_for_implicit_cache(self):
        """Implicit Cache は 1024 tok 以上の prefix で発動するため、SYSTEM が
        十分な大きさを持つことを確認する。日本語 1 文字 ≒ 1 tok の概算で
        SYSTEM が 1500 文字以上あれば余裕で発動圏内。"""
        from tools.ap.ocr_correction import _GEMINI_SYSTEM_PROMPT
        assert len(_GEMINI_SYSTEM_PROMPT) >= 1500, \
            f"SYSTEM が {len(_GEMINI_SYSTEM_PROMPT)} 文字 - Implicit Cache 発動圏外の可能性"

    def test_batch_system_is_large_enough_for_implicit_cache(self):
        from tools.ap.ocr_correction import _GEMINI_BATCH_SYSTEM_PROMPT
        assert len(_GEMINI_BATCH_SYSTEM_PROMPT) >= 1500


# ─── USER テンプレの「動的性」検証 ────────────────────

class TestUserTemplateHasPlaceholders:
    def test_single_user_template_has_required_placeholders(self):
        from tools.ap.ocr_correction import _GEMINI_USER_TEMPLATE
        assert "{scene_hint}" in _GEMINI_USER_TEMPLATE
        assert "{ocr_text}" in _GEMINI_USER_TEMPLATE

    def test_batch_user_template_has_required_placeholder(self):
        from tools.ap.ocr_correction import _GEMINI_BATCH_USER_TEMPLATE
        assert "{ocr_block}" in _GEMINI_BATCH_USER_TEMPLATE

    def test_single_user_template_formats_without_error(self):
        """USER テンプレが指定キーで format できる (KeyError ガード)。"""
        from tools.ap.ocr_correction import _GEMINI_USER_TEMPLATE
        out = _GEMINI_USER_TEMPLATE.format(
            scene_hint="検出器の推定シーン: MOVIE",
            ocr_text="今度こそ",
        )
        assert "MOVIE" in out
        assert "今度こそ" in out


# ─── 後方互換: 連結版 _GEMINI_PROMPT ────────────────────

class TestBackwardCompatProperties:
    """既存テストや legacy code が参照する _GEMINI_PROMPT を維持する。"""

    def test_gemini_prompt_is_concatenated(self):
        from tools.ap.ocr_correction import (
            _GEMINI_SYSTEM_PROMPT, _GEMINI_USER_TEMPLATE, _GEMINI_PROMPT,
        )
        assert _GEMINI_SYSTEM_PROMPT in _GEMINI_PROMPT
        assert _GEMINI_USER_TEMPLATE in _GEMINI_PROMPT

    def test_gemini_batch_prompt_is_concatenated(self):
        from tools.ap.ocr_correction import (
            _GEMINI_BATCH_SYSTEM_PROMPT, _GEMINI_BATCH_USER_TEMPLATE,
            _GEMINI_BATCH_PROMPT,
        )
        assert _GEMINI_BATCH_SYSTEM_PROMPT in _GEMINI_BATCH_PROMPT
        assert _GEMINI_BATCH_USER_TEMPLATE in _GEMINI_BATCH_PROMPT


# ─── REST API: systemInstruction の送信検証 ───────────

class TestSystemInstructionInRequestBody:
    """gemini_correct_single の REST body が systemInstruction を持ち、
    SYSTEM プロンプトが contents 側 (動的) に重複していないことを確認。"""

    def _fake_resp(self, payload_json: dict):
        outer = json.dumps({
            "candidates": [{"content": {"parts": [{
                "text": json.dumps(payload_json),
            }]}}],
            "usageMetadata": {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
                "totalTokenCount": 2,
            },
        }).encode()

        class FakeResp:
            def __init__(self, data): self._data = data
            def read(self): return self._data
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return FakeResp(outer)

    def test_request_body_contains_system_instruction(self, tmp_path, monkeypatch):
        from tools.ap.ocr_correction import gemini_correct_single
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        img = tmp_path / "x.webp"
        img.write_bytes(b"webp")

        captured = {"body": None}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode()
            return self._fake_resp({"corrected_text": "ok", "is_artifact": False})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("tools.ap.api_usage.record_api_usage", lambda *a, **k: None)

        gemini_correct_single(str(img), "test", scene="MOVIE")

        assert captured["body"] is not None
        body = json.loads(captured["body"])
        assert "systemInstruction" in body, \
            "systemInstruction が body にない → Implicit Cache が機能しない"
        sys_parts = body["systemInstruction"]["parts"]
        assert isinstance(sys_parts, list) and sys_parts
        # SYSTEM 側に静的指示が入っている
        assert "マスターリスト" in sys_parts[0]["text"]

    def test_dynamic_values_not_in_system_instruction(self, tmp_path, monkeypatch):
        """scene_hint / ocr_text が systemInstruction 側に入っていない
        (= 入っていれば毎リクエスト異なり Cache が効かない)。"""
        from tools.ap.ocr_correction import gemini_correct_single
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        img = tmp_path / "x.webp"
        img.write_bytes(b"webp")

        captured = {"body": None}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode()
            return self._fake_resp({"corrected_text": "ok", "is_artifact": False})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("tools.ap.api_usage.record_api_usage", lambda *a, **k: None)

        # SYSTEM に絶対含まれないユニークな文字列を埋め込んで、それが
        # systemInstruction 側に混入しないことを確認する。
        unique_ocr = "ZZZTESTOCRZZZ12345"
        unique_scene = "ZZZUNIQUESCENE99"
        gemini_correct_single(str(img), unique_ocr, scene=unique_scene)

        body = json.loads(captured["body"])
        sys_text = body["systemInstruction"]["parts"][0]["text"]
        assert unique_ocr not in sys_text
        assert unique_scene not in sys_text
        # contents 側には入っている
        user_parts = body["contents"][0]["parts"]
        user_texts = [p.get("text", "") for p in user_parts if "text" in p]
        joined = "\n".join(user_texts)
        assert unique_ocr in joined, "USER 側に ocr_text が反映されていない"
        assert unique_scene in joined, "USER 側に scene が反映されていない"

    def test_system_instruction_is_identical_across_requests(self, tmp_path, monkeypatch):
        """異なる scene / ocr_text で 2 回呼んでも systemInstruction の文字列が
        完全同一であること (Implicit Cache 発動の必要条件)。"""
        from tools.ap.ocr_correction import gemini_correct_single
        monkeypatch.setenv("GEMINI_API_KEY", "dummy")
        img = tmp_path / "x.webp"
        img.write_bytes(b"webp")

        bodies: list[str] = []

        def fake_urlopen(req, timeout=None):
            bodies.append(req.data.decode())
            return self._fake_resp({"corrected_text": "ok", "is_artifact": False})

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        monkeypatch.setattr("tools.ap.api_usage.record_api_usage", lambda *a, **k: None)

        gemini_correct_single(str(img), "first", scene="MOVIE")
        gemini_correct_single(str(img), "second", scene="ADV")

        assert len(bodies) == 2
        sys1 = json.loads(bodies[0])["systemInstruction"]
        sys2 = json.loads(bodies[1])["systemInstruction"]
        assert sys1 == sys2, "systemInstruction が変動するとキャッシュが効かない"


# ─── SDK 経由 (gemini_correct_multi) の system_instruction 引数検証 ─

class TestSdkSystemInstruction:
    def test_multi_passes_system_instruction_via_config(self, tmp_path, monkeypatch):
        """gemini_correct_multi が GenerateContentConfig に system_instruction
        を渡している (= SDK 側でも Implicit Cache が効く構造)。"""
        # ソースコード文字列レベルで確認 (SDK モックが複雑なため)
        import inspect
        from tools.ap import ocr_correction
        src = inspect.getsource(ocr_correction.gemini_correct_multi)
        assert "system_instruction=" in src, \
            "gemini_correct_multi が system_instruction を渡していない"
        assert "_GEMINI_BATCH_SYSTEM_PROMPT" in src, \
            "gemini_correct_multi が SYSTEM プロンプトを参照していない"
