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


_test_img_counter = 0

def _make_test_image(tmp_path: Path, brightness: int = 128,
                     w: int = 1440, h: int = 720) -> Path:
    """テスト用 PNG 画像を生成。brightness で明るさを指定 (0-255)。"""
    global _test_img_counter
    import cv2
    import numpy as np
    _test_img_counter += 1
    img = np.full((h, w, 3), brightness, dtype=np.uint8)
    path = tmp_path / f"test_{_test_img_counter}.png"
    cv2.imwrite(str(path), img)
    return path


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

    def test_battle_pure_number_tokens_excluded(self):
        """バトル中: 純数字トークン (ダメージ値等) は除外されて同 fingerprint。

        例: ['1234'] や ['5678'] は _PURE_NUMBER_RE で除外されるので、
        テキスト部分が同じなら同 fingerprint になる。
        """
        ocr_turn1 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("BUFFER", cx=200, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("1234", cx=400, cy=200),   # 純数字 → 除外
        ]
        ocr_turn2 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("BUFFER", cx=200, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("5678", cx=400, cy=200),   # 純数字 → 除外
        ]
        norm1 = ScreenRecorder._normalize_ocr(ocr_turn1)
        norm2 = ScreenRecorder._normalize_ocr(ocr_turn2)
        fp1 = ScreenRecorder._content_fingerprint(norm1)
        fp2 = ScreenRecorder._content_fingerprint(norm2)
        assert fp1 == fp2, f"純数字除外でも fingerprint が変わる: {norm1!r} vs {norm2!r}"

    def test_battle_mixed_token_with_number_different_fingerprint(self):
        """バトル中: 数字混じりトークン (例: 'Turn 3') が変わると別 fingerprint。

        実 OCR は 'Turn 3' を 1 トークンで返すことがある。これを 'Turn' に
        正規化していたのが Download 進捗等の bug 衝突の原因だった。
        数字保持により別画面として正しく区別される。クラスタリングは phash
        ベースで動くため、Turn 別画面が冗長化することはない (= 異なる
        Turn は phash も異なるので元から別 cluster)。
        """
        ocr_turn3 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("Turn 3", cx=500, cy=50),
        ]
        ocr_turn5 = [
            _make_ocr("ATTACKER", cx=100, cy=100),
            _make_ocr("AUTO", cx=300, cy=50),
            _make_ocr("Turn 5", cx=500, cy=50),
        ]
        fp1 = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_turn3))
        fp2 = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_turn5))
        assert fp1 != fp2, "Turn N の N が変わったら別 fingerprint であるべき"

    def test_download_progress_different_fingerprint(self):
        """Download 進捗が違えば別 fingerprint (旧バグの解消検証)。

        旧: 'Download 1083.64MB / 3665.99 MB' → 数字除去 → 'Download MB MB' → 同 fp
        新: 数字保持 → 別 fp
        """
        ocr_a = [_make_ocr("Download 1083.64MB / 3665.99 MB", cx=400, cy=400)]
        ocr_b = [_make_ocr("Download 2046.34MB / 3665.99 MB", cx=400, cy=400)]
        ocr_c = [_make_ocr("Download 3202.68MB / 3665.99 MB", cx=400, cy=400)]
        fp_a = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_a))
        fp_b = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_b))
        fp_c = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_c))
        assert len({fp_a, fp_b, fp_c}) == 3, \
            f"進捗違いで全て別 fingerprint であるべき: {fp_a} {fp_b} {fp_c}"

    def test_version_string_different_fingerprint(self):
        """バージョン文字列が違えば別 fingerprint。"""
        ocr_v34 = [_make_ocr("Ver.3.4.0", cx=400, cy=400)]
        ocr_v35 = [_make_ocr("Ver.3.5.0", cx=400, cy=400)]
        fp_34 = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_v34))
        fp_35 = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_v35))
        assert fp_34 != fp_35, "バージョン違いで別 fingerprint であるべき"

    def test_same_dialog_same_fingerprint(self):
        """同一 dialog (数字なし) は同 fingerprint (正しい衝突を維持)。"""
        text = "それだけは覚えていたし、それだけしか覚えていない"
        ocr_a = [_make_ocr(text, cx=400, cy=600)]
        ocr_b = [_make_ocr(text, cx=400, cy=600)]
        fp_a = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_a))
        fp_b = ScreenRecorder._content_fingerprint(ScreenRecorder._normalize_ocr(ocr_b))
        assert fp_a == fp_b, "同一テキストは同 fingerprint"

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
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("Now Loading")]
            assert rec.maybe_record(img, ocr, "LOADING", "abc123") is False
        finally:
            rec.close()

    def test_movie_scene_recorded(self, tmp_path):
        """MOVIE シーンもセリフキャプチャのため記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path, brightness=128)
            ocr = [_make_ocr("SKIP")]
            assert rec.maybe_record(img, ocr, "MOVIE", "abc123") is True
        finally:
            rec.close()

    def test_record_empty_ocr_with_phash(self, tmp_path):
        """空 OCR でも phash があれば記録される（寛容撮影）。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path, brightness=128)
            assert rec.maybe_record(img, [], "MENU", "abc123") is True
        finally:
            rec.close()

    def test_skip_too_dark(self, tmp_path):
        """暗すぎる画面は記録されない。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path, brightness=10)
            assert rec.maybe_record(img, [], "UNKNOWN", "abc123") is False
        finally:
            rec.close()

    def test_skip_too_bright(self, tmp_path):
        """白すぎる画面は記録されない。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path, brightness=230)
            assert rec.maybe_record(img, [], "UNKNOWN", "abc123") is False
        finally:
            rec.close()

    def test_record_battle_scene(self, tmp_path):
        """BATTLE シーンは記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("ATTACKER"), _make_ocr("AUTO")]
            assert rec.maybe_record(img, ocr, "BATTLE", "abc123") is True
        finally:
            rec.close()

    def test_record_gacha_scene(self, tmp_path):
        """GACHA シーンは記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("栗根こころ"), _make_ocr("SKIP")]
            assert rec.maybe_record(img, ocr, "GACHA", "abc123") is True
        finally:
            rec.close()

    def test_duplicate_skip(self, tmp_path):
        """同一テキストの2回目はスキップ。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("ホーム画面"), _make_ocr("クエスト")]
            assert rec.maybe_record(img, ocr, "MENU", "abc123") is True
            assert rec.maybe_record(img, ocr, "MENU", "abc123") is False
        finally:
            rec.close()

    def test_different_text_recorded(self, tmp_path):
        """テキストが異なれば連続でも記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr1 = [_make_ocr("画面A")]
            ocr2 = [_make_ocr("画面B")]
            assert rec.maybe_record(img, ocr1, "MENU", "abc123") is True
            assert rec.maybe_record(img, ocr2, "MENU", "def456") is True
        finally:
            rec.close()

    def test_record_when_all_ocr_filtered(self, tmp_path):
        """全トークンがフィルタされても phash で記録される（寛容撮影）。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("1234"), _make_ocr("♦●")]
            assert rec.maybe_record(img, ocr, "MENU", "abc123") is True
        finally:
            rec.close()


# ─── parent_fp テスト ─────────────────────────────────

class TestParentFp:

    def test_first_record_has_null_parent(self, tmp_path):
        """1枚目の parent_fp は None。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("画面A")]
            rec.maybe_record(img, ocr, "MENU", "abc123")

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
            img = _make_test_image(tmp_path)
            ocr1 = [_make_ocr("画面A")]
            rec.maybe_record(img, ocr1, "MENU", "abc123")
            first_fp = rec._last_recorded_fp

            ocr2 = [_make_ocr("画面B")]
            rec.maybe_record(img, ocr2, "MENU", "def456")

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
        img = _make_test_image(tmp_path)
        rec1 = _make_recorder(tmp_path, session_id="session_1")
        ocr = [_make_ocr("共通画面")]
        rec1.maybe_record(img, ocr, "MENU", "abc123")
        rec1.close()

        # セッション2: 同じ画面はスキップされるはず
        rec2 = _make_recorder(tmp_path, session_id="session_2")
        try:
            assert rec2.maybe_record(img, ocr, "MENU", "def456") is False
        finally:
            rec2.close()

    def test_new_screen_in_new_session(self, tmp_path):
        """別セッションで新しい画面は記録される。"""
        img = _make_test_image(tmp_path)
        rec1 = _make_recorder(tmp_path, session_id="session_1")
        rec1.maybe_record(img, [_make_ocr("画面A")], "MENU", "abc123")
        rec1.close()

        rec2 = _make_recorder(tmp_path, session_id="session_2")
        try:
            new_ocr = [_make_ocr("画面B")]
            assert rec2.maybe_record(img, new_ocr, "MENU", "def456") is True
        finally:
            rec2.close()


# ─── DB 書き込みテスト ────────────────────────────────

class TestDbWrite:

    def test_screen_inserted(self, tmp_path):
        """lc_screens にレコードが INSERT される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path)
            ocr = [_make_ocr("テスト画面", confidence=0.95)]
            rec.maybe_record(img, ocr, "MENU", "phash_abc")

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
            img = _make_test_image(tmp_path)
            ocr = [
                _make_ocr("ボタンA", confidence=0.9),
                _make_ocr("ボタンB", confidence=0.8),
            ]
            rec.maybe_record(img, ocr, "MENU", "abc123")

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
            img = _make_test_image(tmp_path)
            rec.maybe_record(img, [_make_ocr("ホーム画面")], "MENU", "abc")
            rec.maybe_record(img, [_make_ocr("クエスト画面")], "MENU", "def")

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT screens_found FROM lc_sessions WHERE session_id = ?",
                ("test_session",),
            ).fetchone()
            conn.close()
            assert row[0] == 2
        finally:
            rec.close()

    def test_session_status_paused_on_manual_close(self, tmp_path):
        """close() デフォルト (goal_reached=False) でセッション status が 'paused' になる (resume 可能)。"""
        rec = _make_recorder(tmp_path)
        img = _make_test_image(tmp_path)
        rec.maybe_record(img, [_make_ocr("画面")], "MENU", "abc")
        rec.close()

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = conn.execute(
            "SELECT status FROM lc_sessions WHERE session_id = ?",
            ("test_session",),
        ).fetchone()
        conn.close()
        assert row[0] == "paused"


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

    def test_normal_brightness_recorded(self, tmp_path):
        """通常の明るさの画像は記録される。"""
        rec = _make_recorder(tmp_path)
        try:
            img = _make_test_image(tmp_path, brightness=128)
            ocr = [_make_ocr("テスト画面")]
            assert rec.maybe_record(img, ocr, "MENU", "abc123") is True

            conn = sqlite3.connect(str(tmp_path / "test.db"))
            row = conn.execute(
                "SELECT screenshot_path FROM lc_screens"
                " WHERE session_id = ?", ("test_session",)
            ).fetchone()
            conn.close()
            assert row[0].endswith(".webp")
        finally:
            rec.close()



# ─── セッションライフサイクル: paused 状態のテスト ─────────

class TestSessionLifecycle:
    """close() / __init__ の status 遷移テスト。"""

    def test_close_manual_stop_sets_paused(self, tmp_path):
        """close(goal_reached=False) で status='paused', completion_type='manual_stop'。"""
        rec = _make_recorder(tmp_path, session_id='paused_session')
        rec.close(goal_reached=False)
        conn = sqlite3.connect(str(tmp_path / 'test.db'))
        row = conn.execute(
            "SELECT status, completion_type FROM lc_sessions WHERE session_id = ?",
            ('paused_session',),
        ).fetchone()
        conn.close()
        assert row[0] == 'paused', f'expected paused, got {row[0]}'
        assert row[1] == 'manual_stop', f'expected manual_stop, got {row[1]}'

    def test_close_goal_reached_sets_completed(self, tmp_path):
        """close(goal_reached=True) で status='completed', completion_type='goal_reached'。"""
        rec = _make_recorder(tmp_path, session_id='goal_session')
        rec.close(goal_reached=True)
        conn = sqlite3.connect(str(tmp_path / 'test.db'))
        row = conn.execute(
            "SELECT status, completion_type FROM lc_sessions WHERE session_id = ?",
            ('goal_session',),
        ).fetchone()
        conn.close()
        assert row[0] == 'completed'
        assert row[1] == 'goal_reached'

    def test_resume_from_paused_sets_running(self, tmp_path):
        """paused のセッションを再オープンすると status='running' / completion_type=NULL に戻る。"""
        rec1 = _make_recorder(tmp_path, session_id='resume_test')
        rec1.close(goal_reached=False)
        # 再オープン (= resume)
        rec2 = _make_recorder(tmp_path, session_id='resume_test')
        try:
            conn = sqlite3.connect(str(tmp_path / 'test.db'))
            row = conn.execute(
                "SELECT status, completion_type FROM lc_sessions WHERE session_id = ?",
                ('resume_test',),
            ).fetchone()
            conn.close()
            assert row[0] == 'running', f'expected running, got {row[0]}'
            assert row[1] is None, f'expected NULL, got {row[1]}'
        finally:
            rec2.close(goal_reached=False)  # cleanup

