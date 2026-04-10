"""
ScreenRecorder ユニットテスト (Phase 1)

検証項目:
  - _normalize_ocr: ソート順、数値除外、記号除外、空入力、時刻パターン
  - _content_fingerprint: 決定性、異テキスト→異ハッシュ
  - maybe_record: 重複スキップ、LOADING/MOVIE スキップ、空 OCR、5秒インターバル
  - セッション横断重複チェック
  - DB 書き込み (lc_screens, lc_tappable_items)
  - parent_fp: 初回 None、2回目以降は直前の fingerprint
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from tools.ap.screen_recorder import ScreenRecorder


# ─── テストヘルパー ───────────────────────────────────

def _make_ocr(text: str, confidence: float = 0.9,
              cx: int = 100, cy: int = 100) -> dict:
    """テスト用 OCR アイテムを生成。"""
    return {
        "text": text,
        "confidence": confidence,
        "box": [[cx - 20, cy - 10], [cx + 20, cy - 10],
                [cx + 20, cy + 10], [cx - 20, cy + 10]],
        "center": [cx, cy],
    }


def _make_recorder(tmp_path: Path, session_id: str = "test_session",
                   game_title: str = "TestGame") -> ScreenRecorder:
    """テスト用 ScreenRecorder を生成。"""
    db_path = tmp_path / "test.db"
    storage_dir = tmp_path / "screenshots"
    return ScreenRecorder(
        db_path=db_path,
        storage_dir=storage_dir,
        session_id=session_id,
        game_title=game_title,
    )


# ─── _normalize_ocr テスト ────────────────────────────

class TestNormalizeOcr:
    """OCR テキスト正規化のテスト。"""

    def test_basic_sort_by_position(self):
        """Y バケット → X でソートされること。"""
        ocr = [
            _make_ocr("下のテキスト", cx=100, cy=200),
            _make_ocr("上のテキスト", cx=100, cy=50),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "上のテキスト|下のテキスト"

    def test_same_y_bucket_sort_by_x(self):
        """同じ Y バケット内では X でソート。"""
        ocr = [
            _make_ocr("右", cx=300, cy=25),
            _make_ocr("左", cx=100, cy=25),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "左|右"

    def test_exclude_pure_numbers(self):
        """純粋な数字トークンは除外。"""
        ocr = [
            _make_ocr("HP", cx=100, cy=100),
            _make_ocr("1234", cx=200, cy=100),
            _make_ocr("5,678", cx=300, cy=100),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "HP"

    def test_exclude_time_patterns(self):
        """時刻パターンは除外。"""
        ocr = [
            _make_ocr("12:34", cx=100, cy=100),
            _make_ocr("01:23:45", cx=200, cy=100),
            _make_ocr("残り時間", cx=300, cy=100),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "残り時間"

    def test_exclude_symbols_only(self):
        """記号のみのトークンは除外（日本語/英単語を含まない）。"""
        ocr = [
            _make_ocr("♦●▶", cx=100, cy=100),
            _make_ocr("★★★", cx=200, cy=100),
            _make_ocr("ガチャ", cx=300, cy=100),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "ガチャ"

    def test_exclude_low_confidence(self):
        """confidence < 0.3 は除外。"""
        ocr = [
            _make_ocr("高信頼", confidence=0.9, cx=100, cy=100),
            _make_ocr("低信頼", confidence=0.1, cx=200, cy=100),
        ]
        result = ScreenRecorder._normalize_ocr(ocr)
        assert result == "高信頼"

    def test_empty_ocr(self):
        """空の OCR 結果は空文字列。"""
        assert ScreenRecorder._normalize_ocr([]) == ""

    def test_all_filtered_out(self):
        """全トークンがフィルタされた場合は空文字列。"""
        ocr = [
            _make_ocr("1234", cx=100, cy=100),
            _make_ocr("♦●", cx=200, cy=100),
        ]
        assert ScreenRecorder._normalize_ocr(ocr) == ""

    def test_battle_number_variation_same_fingerprint(self):
        """バトル中: ダメージ数値・ターン数が変わっても同じ fingerprint。"""
        ocr_turn1 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("BUFFER", cx=200, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("1234", cx=400, cy=200),   # ダメージ
            _make_ocr("Turn 3", cx=500, cy=50),   # ←「Turn 3」は数字じゃないので残る
        ]
        ocr_turn2 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("BUFFER", cx=200, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("5678", cx=400, cy=200),   # ダメージ変化
            _make_ocr("Turn 5", cx=500, cy=50),   # ←同じ「Turn」を含む
        ]
        norm1 = ScreenRecorder._normalize_ocr(ocr_turn1)
        norm2 = ScreenRecorder._normalize_ocr(ocr_turn2)
        fp1 = ScreenRecorder._content_fingerprint(norm1)
        fp2 = ScreenRecorder._content_fingerprint(norm2)
        assert fp1 == fp2, f"バトルターン数値変化で fingerprint が変わってしまう: {norm1} vs {norm2}"

    def test_battle_skill_change_different_fingerprint(self):
        """バトル中: 右下アイコン（スキル名）が変わったら別 fingerprint。"""
        ocr_normal = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("通常攻撃", cx=300, cy=600),
            _make_ocr("AUTO", cx=300, cy=50),
        ]
        ocr_skill = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("フロウイング・ブラスト", cx=300, cy=600),
            _make_ocr("AUTO", cx=300, cy=50),
        ]
        norm1 = ScreenRecorder._normalize_ocr(ocr_normal)
        norm2 = ScreenRecorder._normalize_ocr(ocr_skill)
        fp1 = ScreenRecorder._content_fingerprint(norm1)
        fp2 = ScreenRecorder._content_fingerprint(norm2)
        assert fp1 != fp2, "スキル名変化で fingerprint が同じになってしまう"

    def test_hp_variation_same_fingerprint(self):
        """HP 値が変わっても同じ fingerprint。"""
        ocr_hp1 = [
            _make_ocr("HP", cx=100, cy=100),
            _make_ocr("1234", cx=150, cy=100),
            _make_ocr("5678", cx=200, cy=100),
            _make_ocr("メニュー", cx=300, cy=50),
        ]
        ocr_hp2 = [
            _make_ocr("HP", cx=100, cy=100),
            _make_ocr("999", cx=150, cy=100),
            _make_ocr("5678", cx=200, cy=100),
            _make_ocr("メニュー", cx=300, cy=50),
        ]
        norm1 = ScreenRecorder._normalize_ocr(ocr_hp1)
        norm2 = ScreenRecorder._normalize_ocr(ocr_hp2)
        assert ScreenRecorder._content_fingerprint(norm1) == \
               ScreenRecorder._content_fingerprint(norm2)

    def test_timer_variation_same_fingerprint(self):
        """タイマー表示が変わっても同じ fingerprint。"""
        ocr_t1 = [
            _make_ocr("残り時間", cx=100, cy=100),
            _make_ocr("12:34", cx=200, cy=100),
        ]
        ocr_t2 = [
            _make_ocr("残り時間", cx=100, cy=100),
            _make_ocr("11:59", cx=200, cy=100),
        ]
        norm1 = ScreenRecorder._normalize_ocr(ocr_t1)
        norm2 = ScreenRecorder._normalize_ocr(ocr_t2)
        assert ScreenRecorder._content_fingerprint(norm1) == \
               ScreenRecorder._content_fingerprint(norm2)


# ─── _content_fingerprint テスト ──────────────────────

class TestContentFingerprint:

    def test_deterministic(self):
        """同じ入力に対して同じハッシュを返す。"""
        fp1 = ScreenRecorder._content_fingerprint("テスト|データ")
        fp2 = ScreenRecorder._content_fingerprint("テスト|データ")
        assert fp1 == fp2

    def test_different_input_different_hash(self):
        """異なる入力に対して異なるハッシュを返す。"""
        fp1 = ScreenRecorder._content_fingerprint("画面A")
        fp2 = ScreenRecorder._content_fingerprint("画面B")
        assert fp1 != fp2

    def test_length_is_16(self):
        """fingerprint は16文字。"""
        fp = ScreenRecorder._content_fingerprint("テスト")
        assert len(fp) == 16

    def test_hex_characters(self):
        """fingerprint は16進数文字のみ。"""
        fp = ScreenRecorder._content_fingerprint("テスト")
        assert all(c in "0123456789abcdef" for c in fp)


# ─── _make_title テスト ───────────────────────────────

class TestMakeTitle:

    def test_top_3_by_confidence(self):
        """信頼度上位3つを / 区切りで返す。"""
        ocr = [
            _make_ocr("低い", confidence=0.5),
            _make_ocr("最高", confidence=0.99),
            _make_ocr("中間", confidence=0.8),
            _make_ocr("高い", confidence=0.95),
        ]
        title = ScreenRecorder._make_title(ocr)
        assert title == "最高 / 高い / 中間"

    def test_excludes_numbers(self):
        """数字のみのトークンはタイトルに含めない。"""
        ocr = [
            _make_ocr("ホーム", confidence=0.9),
            _make_ocr("1234", confidence=0.95),
        ]
        title = ScreenRecorder._make_title(ocr)
        assert title == "ホーム"

    def test_empty_returns_unknown(self):
        """候補がなければ 'Unknown'。"""
        assert ScreenRecorder._make_title([]) == "Unknown"


# ─── maybe_record テスト ──────────────────────────────

class TestMaybeRecord:

    def test_skip_loading_scene(self, tmp_path):
        """LOADING シーンはスキップ。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("Now Loading")]
            assert rec.maybe_record(None, ocr, "LOADING", "abc123") is False
        finally:
            rec.close()

    def test_skip_movie_scene(self, tmp_path):
        """MOVIE シーンはスキップ。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("SKIP")]
            assert rec.maybe_record(None, ocr, "MOVIE", "abc123") is False
        finally:
            rec.close()

    def test_record_empty_ocr_with_phash(self, tmp_path):
        """空 OCR でも phash があれば記録される（寛容撮影）。"""
        rec = _make_recorder(tmp_path)
        try:
            assert rec.maybe_record(None, [], "MENU", "abc123") is True
        finally:
            rec.close()

    def test_record_battle_scene(self, tmp_path):
        """BATTLE シーンは記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("ATTACKER"), _make_ocr("AUTO")]
            assert rec.maybe_record(None, ocr, "BATTLE", "abc123") is True
        finally:
            rec.close()

    def test_record_gacha_scene(self, tmp_path):
        """GACHA シーンは記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("栗根こころ"), _make_ocr("SKIP")]
            assert rec.maybe_record(None, ocr, "GACHA", "abc123") is True
        finally:
            rec.close()

    def test_duplicate_skip(self, tmp_path):
        """同一テキストの2回目はスキップ。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("ホーム画面"), _make_ocr("クエスト")]
            assert rec.maybe_record(None, ocr, "MENU", "abc123") is True
            assert rec.maybe_record(None, ocr, "MENU", "abc123") is False
        finally:
            rec.close()

    def test_different_text_recorded(self, tmp_path):
        """テキストが異なれば連続でも記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr1 = [_make_ocr("画面A")]
            ocr2 = [_make_ocr("画面B")]
            assert rec.maybe_record(None, ocr1, "MENU", "abc123") is True
            assert rec.maybe_record(None, ocr2, "MENU", "def456") is True
        finally:
            rec.close()

    def test_record_when_all_ocr_filtered(self, tmp_path):
        """全トークンがフィルタされても phash で記録される（寛容撮影）。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("1234"), _make_ocr("♦●")]
            assert rec.maybe_record(None, ocr, "MENU", "abc123") is True
        finally:
            rec.close()


# ─── parent_fp テスト ─────────────────────────────────

class TestParentFp:

    def test_first_record_has_null_parent(self, tmp_path):
        """1枚目の parent_fp は None。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("画面A")]
            rec.maybe_record(None, ocr, "MENU", "abc123")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT parent_fp FROM lc_screens WHERE session_id = ?",
                ("test_session",),
            ).fetchone()
            conn.close()
            assert row[0] is None
        finally:
            rec.close()

    def test_second_record_has_parent(self, tmp_path):
        """2枚目の parent_fp は1枚目の fingerprint。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr1 = [_make_ocr("画面A")]
            rec.maybe_record(None, ocr1, "MENU", "abc123")
            first_fp = rec._last_recorded_fp

            ocr2 = [_make_ocr("画面B")]
            rec.maybe_record(None, ocr2, "MENU", "def456")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            rows = conn.execute(
                "SELECT fingerprint, parent_fp FROM lc_screens"
                " WHERE session_id = ? ORDER BY id",
                ("test_session",),
            ).fetchall()
            conn.close()

            assert rows[0][1] is None            # 1枚目: parent なし
            assert rows[1][1] == first_fp         # 2枚目: 1枚目の fp
        finally:
            rec.close()


# ─── セッション横断テスト ─────────────────────────────

class TestCrossSession:

    def test_loads_existing_fingerprints(self, tmp_path):
        """別セッションで記録済みの fingerprint がロードされ、重複スキップされる。"""
        # セッション1で記録
        rec1 = _make_recorder(tmp_path, session_id="session_1")
        ocr = [_make_ocr("共通画面")]
        rec1.maybe_record(None, ocr, "MENU", "abc123")
        rec1.close()

        # セッション2: 同じ画面はスキップされるはず
        rec2 = _make_recorder(tmp_path, session_id="session_2")
        try:
            assert rec2.maybe_record(None, ocr, "MENU", "def456") is False
        finally:
            rec2.close()

    def test_new_screen_in_new_session(self, tmp_path):
        """別セッションで新しい画面は記録される。"""
        rec1 = _make_recorder(tmp_path, session_id="session_1")
        rec1.maybe_record(None, [_make_ocr("画面A")], "MENU", "abc123")
        rec1.close()

        rec2 = _make_recorder(tmp_path, session_id="session_2")
        try:
            new_ocr = [_make_ocr("画面B")]
            assert rec2.maybe_record(None, new_ocr, "MENU", "def456") is True
        finally:
            rec2.close()


# ─── DB 書き込みテスト ────────────────────────────────

class TestDbWrite:

    def test_screen_inserted(self, tmp_path):
        """lc_screens にレコードが INSERT される。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面", confidence=0.95)]
            rec.maybe_record(None, ocr, "MENU", "phash_abc")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT fingerprint, title, phash, ocr_text, session_id"
                " FROM lc_screens WHERE session_id = ?",
                ("test_session",),
            ).fetchone()
            conn.close()

            assert row is not None
            assert len(row[0]) == 16              # fingerprint 16文字
            assert "テスト画面" in row[1]          # title に含まれる
            assert row[2] == "phash_abc"           # phash
            assert "テスト画面" in row[3]           # ocr_text
            assert row[4] == "test_session"        # session_id
        finally:
            rec.close()

    def test_tappable_items_inserted(self, tmp_path):
        """lc_tappable_items にレコードが INSERT される。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [
                _make_ocr("ボタンA", confidence=0.9),
                _make_ocr("ボタンB", confidence=0.8),
            ]
            rec.maybe_record(None, ocr, "MENU", "abc123")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            rows = conn.execute(
                "SELECT text, confidence FROM lc_tappable_items"
                " ORDER BY text"
            ).fetchall()
            conn.close()

            assert len(rows) == 2
            assert rows[0][0] == "ボタンA"
            assert rows[1][0] == "ボタンB"
        finally:
            rec.close()

    def test_session_screens_found_updated(self, tmp_path):
        """lc_sessions.screens_found がインクリメントされる。"""
        rec = _make_recorder(tmp_path)
        try:
            rec.maybe_record(None, [_make_ocr("ホーム画面")], "MENU", "abc")
            rec.maybe_record(None, [_make_ocr("クエスト画面")], "MENU", "def")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT screens_found FROM lc_sessions WHERE session_id = ?",
                ("test_session",),
            ).fetchone()
            conn.close()
            assert row[0] == 2
        finally:
            rec.close()

    def test_session_status_completed_on_close(self, tmp_path):
        """close() でセッション status が 'completed' になる。"""
        rec = _make_recorder(tmp_path)
        rec.maybe_record(None, [_make_ocr("画面")], "MENU", "abc")
        rec.close()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT status FROM lc_sessions WHERE session_id = ?",
            ("test_session",),
        ).fetchone()
        conn.close()
        assert row[0] == "completed"


# ─── Phase 2: 画像保存テスト ──────────────────────────

class TestScreenshotSave:
    """WebP + サムネイル画像保存のテスト。"""

    def _create_test_image(self, tmp_path: Path, w: int = 1440, h: int = 720) -> Path:
        """テスト用 PNG 画像を生成。"""
        import cv2
        import numpy as np
        img = np.random.randint(0, 255, (h, w, 3), dtype=np.uint8)
        path = tmp_path / "test_analysis.png"
        cv2.imwrite(str(path), img)
        return path

    def test_webp_file_created(self, tmp_path):
        """フルサイズ WebP が生成される。"""
        img_path = self._create_test_image(tmp_path)
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            rec.maybe_record(img_path, ocr, "MENU", "abc123")

            webp_files = list((tmp_path / "screenshots" / "test_session").glob("*.webp"))
            full_files = [f for f in webp_files if "_thumb" not in f.name]
            assert len(full_files) == 1
            assert full_files[0].stat().st_size > 0
        finally:
            rec.close()

    def test_thumbnail_created(self, tmp_path):
        """サムネイル WebP が生成される。"""
        img_path = self._create_test_image(tmp_path)
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            rec.maybe_record(img_path, ocr, "MENU", "abc123")

            thumb_files = list((tmp_path / "screenshots" / "test_session").glob("*_thumb.webp"))
            assert len(thumb_files) == 1
            assert thumb_files[0].stat().st_size > 0
        finally:
            rec.close()

    def test_thumbnail_width_320(self, tmp_path):
        """サムネイルの幅が 320px。"""
        import cv2
        img_path = self._create_test_image(tmp_path)
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            rec.maybe_record(img_path, ocr, "MENU", "abc123")

            thumb_files = list((tmp_path / "screenshots" / "test_session").glob("*_thumb.webp"))
            thumb = cv2.imread(str(thumb_files[0]))
            assert thumb.shape[1] == 320  # width
        finally:
            rec.close()

    def test_thumbnail_aspect_ratio(self, tmp_path):
        """サムネイルのアスペクト比が元画像と一致。"""
        import cv2
        img_path = self._create_test_image(tmp_path, w=1440, h=720)
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            rec.maybe_record(img_path, ocr, "MENU", "abc123")

            thumb_files = list((tmp_path / "screenshots" / "test_session").glob("*_thumb.webp"))
            thumb = cv2.imread(str(thumb_files[0]))
            # 1440:720 = 2:1 → 320:160
            assert thumb.shape[1] == 320
            assert thumb.shape[0] == 160
        finally:
            rec.close()

    def test_db_screenshot_path(self, tmp_path):
        """DB の screenshot_path が正しいパス。"""
        img_path = self._create_test_image(tmp_path)
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            rec.maybe_record(img_path, ocr, "MENU", "abc123")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT screenshot_path, thumbnail_path FROM lc_screens"
                " WHERE session_id = ?", ("test_session",)
            ).fetchone()
            conn.close()

            assert row[0].endswith(".webp")
            assert "_thumb" not in row[0]
            assert row[1].endswith("_thumb.webp")
            # ファイルが実在する
            assert Path(row[0]).exists()
            assert Path(row[1]).exists()
        finally:
            rec.close()

    def test_no_image_still_records_db(self, tmp_path):
        """analysis_path が None でも DB には記録される（パスは空）。"""
        rec = _make_recorder(tmp_path)
        try:
            ocr = [_make_ocr("テスト画面")]
            assert rec.maybe_record(None, ocr, "MENU", "abc123") is True

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT screenshot_path, thumbnail_path FROM lc_screens"
                " WHERE session_id = ?", ("test_session",)
            ).fetchone()
            conn.close()
            assert row[0] == ""
            assert row[1] is None or row[1] == ""
        finally:
            rec.close()


