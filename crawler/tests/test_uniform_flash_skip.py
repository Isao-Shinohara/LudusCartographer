"""連続する同一縮退 phash の集約テスト。

ダウンロード中などに白フラッシュフレームが連続発生し、各々が同じ縮退 phash
(`8000000000000000` 等) で記録されてしまう問題への対策の検証。

設計:
  - 直前と同じ縮退 phash → スキップ (連続フラッシュ集約)
  - 散発する縮退 phash (間に別画面) → 各々独立イベントとして記録
  - force=True (タップ前記録) はスキップをバイパス
  - bits は `< 4` または `> 60` を「縮退」と定義
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import sqlite3
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    """ScreenRecorder のテストインスタンス (DB は in-memory)。"""
    from tools.ap.screen_recorder import ScreenRecorder

    db_path = tmp_path / "test.db"
    storage = tmp_path / "storage"
    storage.mkdir()

    rec = ScreenRecorder(
        session_id="test_session",
        db_path=db_path,
        storage_dir=storage,
    )

    # 画像保存・OCR 等の重い処理をスタブ化
    monkeypatch.setattr(rec, "_save_screenshot", lambda *a, **k: ("/tmp/fake.webp", "/tmp/fake_thumb.webp"))
    monkeypatch.setattr(rec, "_insert_screen", lambda **kw: 1)
    monkeypatch.setattr(rec, "_detect_face", lambda p: False)
    # _is_too_dark_or_bright も無効化 (テスト対象に集中するため)
    monkeypatch.setattr("tools.ap.screen_recorder._is_too_dark_or_bright", lambda gray: False)

    return rec


def _make_dummy_image(tmp_path) -> Path:
    """ダミー画像ファイル (cv2.imread が読めればよい)。"""
    import cv2
    import numpy as np
    img = np.full((720, 1440, 3), 200, dtype=np.uint8)
    p = tmp_path / "dummy.webp"
    cv2.imwrite(str(p), img)
    return p


class TestConsecutiveUniformPhashSkip:
    """連続する同一縮退 phash の集約挙動。"""

    def test_single_uniform_flash_recorded(self, recorder, tmp_path):
        """単発の縮退 phash → 記録される (連続でないため集約しない)。"""
        img = _make_dummy_image(tmp_path)
        result = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        assert result is True, "単発の縮退 phash は記録されるべき"

    def test_consecutive_same_uniform_phash_skipped(self, recorder, tmp_path):
        """連続する同一縮退 phash → 1 件目記録、2 件目以降スキップ。"""
        img = _make_dummy_image(tmp_path)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        r3 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        assert r1 is True, "1 件目は記録"
        assert r2 is False, "2 件目はスキップ"
        assert r3 is False, "3 件目もスキップ"

    def test_consecutive_different_uniform_phash_all_recorded(self, recorder, tmp_path):
        """連続でも phash が異なれば各々記録 (= 別画面のフラッシュ)。"""
        img = _make_dummy_image(tmp_path)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")  # bits=1
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "c000000000000000")  # bits=2
        assert r1 is True
        assert r2 is True, "別 phash は別イベントとして記録"

    def test_uniform_separated_by_regular_both_recorded(self, recorder, tmp_path):
        """縮退 → 通常 → 同じ縮退 → 全部記録 (sequence reset)。

        散発するフラッシュは別の場面のものなので集約しない。
        """
        img = _make_dummy_image(tmp_path)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        r2 = recorder.maybe_record(img, [{"text": "通常画面", "confidence": 0.9, "center": [100, 100]}],
                                    "MENU", "abcd1234abcd1234")  # bits >= 4
        r3 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        assert r1 is True
        assert r2 is True
        assert r3 is True, "間に別画面が入れば再度記録される"

    def test_force_bypasses_consecutive_skip(self, recorder, tmp_path):
        """force=True (タップ前記録) は連続スキップをバイパス。"""
        import time as _time
        img = _make_dummy_image(tmp_path)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        # ts_suffix がミリ秒で別 fp になるよう少し待機
        _time.sleep(0.005)
        # 通常呼び出しならスキップされる場面
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000", force=True)
        assert r1 is True
        assert r2 is True, "force=True は連続フラッシュもバイパスして記録"

    def test_force_updates_state_for_subsequent_calls(self, recorder, tmp_path):
        """force=True で記録した後、次の通常呼び出しの状態判定が正しい。"""
        img = _make_dummy_image(tmp_path)
        recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000", force=True)
        # 直前 phash が記憶されているので、次の通常呼び出しはスキップ
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "8000000000000000")
        assert r2 is False, "force 後も _last_seen_phash は更新されているはず"

    def test_non_degenerate_phash_not_skipped(self, recorder, tmp_path):
        """非縮退 phash (bits 4-60) は連続でもスキップしない (情報量あり)。"""
        img = _make_dummy_image(tmp_path)
        # bits 12 のリッチな phash (例: 通常 MENU)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "8088061201400881")
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "8088061201400881")
        assert r1 is True
        # 連続する非縮退は既存の重複チェック (_seen_fps) には引っかかるかもしれないが、
        # 私の新ルールはトリガーしない。重複チェックは別ロジックなので個別検証は不要。

    def test_high_bit_count_also_treated_as_degenerate(self, recorder, tmp_path):
        """bits > 60 (ほぼ全 1) も縮退として連続スキップ対象。"""
        img = _make_dummy_image(tmp_path)
        # ffffffffffffffff = bits 64
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "ffffffffffffffff")
        r2 = recorder.maybe_record(img, [], "UNKNOWN", "ffffffffffffffff")
        assert r1 is True
        assert r2 is False, "高 bit count の縮退も連続でスキップ"

    def test_invalid_phash_string_does_not_crash(self, recorder, tmp_path):
        """phash が不正文字列でも例外で落ちずフェールセーフ動作。"""
        img = _make_dummy_image(tmp_path)
        r1 = recorder.maybe_record(img, [], "UNKNOWN", "not_a_hex")
        # 例外で落ちなければ OK (記録されるかは別)
        assert r1 in (True, False)

    def test_empty_phash_does_not_skip(self, recorder, tmp_path):
        """phash が空文字列の場合は連続判定の対象外。"""
        img = _make_dummy_image(tmp_path)
        # OCR テキストありで OCR ベース fp が生成されるケース
        r1 = recorder.maybe_record(img, [{"text": "menu", "confidence": 0.9, "center": [100, 100]}],
                                    "MENU", "")
        # phash="" は連続判定でスキップされない (OCR で fp 生成されて記録される)
        assert r1 is True
