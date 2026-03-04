"""
test_human_teacher.py — HumanTeacher ユニットテスト (stdin モック)

Appium・実機・PaddleOCR 不要。
_parse_input() の動作と HumanTeacher.ask_for_action() の stdin モックを検証する。

【実行方法】
  cd crawler
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True venv/bin/python -m pytest tests/test_human_teacher.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.human_teacher import HumanTeacher, _parse_input


# ============================================================
# テスト用ヘルパー
# ============================================================

def _make_png(path: Path, seed: int = 42, size: tuple = (200, 200)) -> Path:
    """再現可能な合成 PNG を生成する。"""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (*size, 3), dtype=np.uint8)
    cv2.imwrite(str(path), img)
    return path


# ============================================================
# TestParseInput — _parse_input() のパース動作
# ============================================================

class TestParseInput:

    def test_parse_tap_comma(self) -> None:
        """'540,1200' → tap アクション"""
        result = _parse_input("540,1200")
        assert result == [{"type": "tap", "x": 540, "y": 1200}]

    def test_parse_tap_prefix(self) -> None:
        """'tap 540 1200' → tap アクション"""
        result = _parse_input("tap 540 1200")
        assert result == [{"type": "tap", "x": 540, "y": 1200}]

    def test_parse_tap_prefix_comma(self) -> None:
        """'tap 540,1200' → tap アクション"""
        result = _parse_input("tap 540,1200")
        assert result == [{"type": "tap", "x": 540, "y": 1200}]

    def test_parse_back(self) -> None:
        """'back' → back アクション"""
        result = _parse_input("back")
        assert result == [{"type": "back"}]

    def test_parse_back_short(self) -> None:
        """'b' → back アクション"""
        result = _parse_input("b")
        assert result == [{"type": "back"}]

    def test_parse_skip(self) -> None:
        """'skip' → [] (通常 DFS フォールバック)"""
        result = _parse_input("skip")
        assert result == []

    def test_parse_skip_short(self) -> None:
        """'s' → [] (skip 短縮形)"""
        result = _parse_input("s")
        assert result == []

    def test_parse_swipe_4args(self) -> None:
        """'swipe 300,600,300,200' → duration=300ms デフォルト"""
        result = _parse_input("swipe 300,600,300,200")
        assert result == [{"type": "swipe", "x1": 300, "y1": 600, "x2": 300, "y2": 200, "duration": 300}]

    def test_parse_swipe_5args(self) -> None:
        """'swipe 300,600,300,200,500' → duration=500ms 指定"""
        result = _parse_input("swipe 300,600,300,200,500")
        assert result == [{"type": "swipe", "x1": 300, "y1": 600, "x2": 300, "y2": 200, "duration": 500}]

    def test_parse_wait(self) -> None:
        """'wait 2.0' → wait アクション"""
        result = _parse_input("wait 2.0")
        assert result == [{"type": "wait", "duration": 2.0}]

    def test_parse_invalid(self) -> None:
        """'hello' → None (認識不可)"""
        result = _parse_input("hello")
        assert result is None

    def test_parse_empty_string(self) -> None:
        """空文字列 → None ではなく [] (skip 扱いにはならない)"""
        # 空文字列は _parse_input では "s" や "skip" にマッチしないので None
        # (実際の ask_for_action では空行は continue で再入力を促す)
        result = _parse_input("")
        # "" は "s" や "skip" に含まれない → None
        assert result is None

    def test_parse_negative_coordinates(self) -> None:
        """負の座標も受け付ける"""
        result = _parse_input("-10,200")
        assert result == [{"type": "tap", "x": -10, "y": 200}]


# ============================================================
# TestHumanTeacherAsk — ask_for_action() の stdin モック
# ============================================================

class TestHumanTeacherAsk:

    def test_ask_returns_tap_on_valid_input(self, tmp_path: Path) -> None:
        """有効な座標入力 '540,1200' → tap アクションを返す"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)

        with patch("builtins.input", return_value="540,1200"):
            actions = teacher.ask_for_action(shot, "テスト画面")

        assert actions == [{"type": "tap", "x": 540, "y": 1200}]

    def test_ask_returns_empty_on_eof(self, tmp_path: Path) -> None:
        """EOFError → [] を返す (クローラーが通常 DFS に継続)"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)

        with patch("builtins.input", side_effect=EOFError):
            actions = teacher.ask_for_action(shot, "テスト画面")

        assert actions == []

    def test_ask_returns_empty_on_skip(self, tmp_path: Path) -> None:
        """'skip' → [] を返す"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)

        with patch("builtins.input", return_value="skip"):
            actions = teacher.ask_for_action(shot, "テスト画面")

        assert actions == []

    def test_ask_retries_on_invalid_input(self, tmp_path: Path) -> None:
        """無効入力 'bad' の後に有効入力 '540,1200' → tap アクションを返す"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)

        with patch("builtins.input", side_effect=["bad", "540,1200"]):
            actions = teacher.ask_for_action(shot, "テスト画面")

        assert actions == [{"type": "tap", "x": 540, "y": 1200}]

    def test_ask_with_ocr_results(self, tmp_path: Path) -> None:
        """OCR 結果付きで呼んでも正常動作する"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)
        ocr = [
            {"text": "ショップ", "confidence": 0.98, "center": [196, 340], "box": []},
            {"text": "ガチャ",   "confidence": 0.92, "center": [196, 430], "box": []},
        ]

        with patch("builtins.input", return_value="back"):
            actions = teacher.ask_for_action(shot, "メイン画面", ocr_results=ocr)

        assert actions == [{"type": "back"}]

    def test_ask_back_action(self, tmp_path: Path) -> None:
        """'back' → back アクション"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=False)

        with patch("builtins.input", return_value="back"):
            actions = teacher.ask_for_action(shot, "テスト画面")

        assert actions == [{"type": "back"}]

    def test_auto_open_calls_subprocess(self, tmp_path: Path) -> None:
        """auto_open_screenshot=True の場合、open コマンドが呼ばれる"""
        shot = _make_png(tmp_path / "screen.png")
        teacher = HumanTeacher(auto_open_screenshot=True)

        with patch("subprocess.run") as mock_run, \
             patch("builtins.input", return_value="skip"):
            teacher.ask_for_action(shot, "テスト画面")
            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            assert call_args[0] == "open"


# ============================================================
# TestScreenCacheHumanSolved — human_solved/ サブディレクトリ
# (test_screen_cache.py からの補足テスト)
# ============================================================

class TestScreenCacheHumanSolved:

    def _make_png(self, path: Path, seed: int = 42) -> Path:
        rng = np.random.default_rng(seed)
        img = rng.integers(0, 256, (200, 200, 3), dtype=np.uint8)
        cv2.imwrite(str(path), img)
        return path

    def test_save_human_solved_creates_subdir(self, tmp_path: Path) -> None:
        """source='human_solved' で保存すると human_solved/ サブディレクトリが作成される"""
        from lc.screen_cache import ScreenCache
        kd = tmp_path / "knowledge"
        cache = ScreenCache(kd, platform="test")

        shot = self._make_png(tmp_path / "shot.png", seed=1)
        cache.save(shot, title="テスト画面", actions=[{"type": "back"}], source="human_solved")

        hs_dir = kd / "human_solved"
        assert hs_dir.exists(), "human_solved/ ディレクトリが作成されていない"
        json_files = list(hs_dir.glob("*.json"))
        assert len(json_files) == 1, "human_solved/ に JSON が 1 件保存されていない"

    def test_load_index_includes_human_solved(self, tmp_path: Path) -> None:
        """再起動後も human_solved/ のエントリがインデックスに含まれる"""
        from lc.screen_cache import ScreenCache
        kd = tmp_path / "knowledge"

        # 1. 保存
        cache1 = ScreenCache(kd, platform="test")
        shot = self._make_png(tmp_path / "shot.png", seed=2)
        saved_hash = cache1.save(
            shot, title="HS 画面", actions=[{"type": "tap", "x": 100, "y": 200}],
            source="human_solved",
        )

        # 2. 再起動（新インスタンス）
        cache2 = ScreenCache(kd, platform="test")
        assert saved_hash in cache2._index, "再起動後に human_solved エントリが見つからない"

    def test_root_takes_priority_over_subdir(self, tmp_path: Path) -> None:
        """同一ハッシュが root と human_solved/ 両方にある場合、root が優先される"""
        from lc.screen_cache import ScreenCache
        import json as _json
        kd = tmp_path / "knowledge"
        cache = ScreenCache(kd, platform="test")

        shot = self._make_png(tmp_path / "shot.png", seed=3)
        # まず human_solved に保存
        h = cache.save(shot, title="HS", actions=[{"type": "back"}], source="human_solved")

        # 同じハッシュで root に保存（手動で JSON を置く）
        root_json = kd / f"{h}.json"
        root_data = {
            "hash": h, "screenshot_path": "", "actions": [{"type": "tap", "x": 1, "y": 1}],
            "success": True, "title": "Root", "created_at": "2026-01-01T00:00:00",
            "hit_count": 0, "platform": "test", "source": "auto",
        }
        root_json.write_text(_json.dumps(root_data), encoding="utf-8")

        # 再起動してインデックス再構築
        cache2 = ScreenCache(kd, platform="test")
        assert cache2._index[h] == root_json, "root の JSON が優先されていない"
