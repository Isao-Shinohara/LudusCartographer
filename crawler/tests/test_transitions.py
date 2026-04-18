"""遷移グラフ Phase 1: lc_transitions 記録のテスト。"""
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import cv2
import pytest

from tools.ap.screen_recorder import ScreenRecorder


@pytest.fixture
def tmp_env(tmp_path):
    """テスト用の DB + ストレージ + ダミー画像を用意する。"""
    db_path = tmp_path / "test.db"
    storage = tmp_path / "screenshots"
    storage.mkdir()

    # ダミー画像 (1440x720, 中間輝度 — 明るすぎると白画面判定でスキップされる)
    img_path = tmp_path / "dummy.png"
    img = np.ones((720, 1440, 3), dtype=np.uint8) * 128
    cv2.imwrite(str(img_path), img)

    return db_path, storage, img_path


def _make_ocr(texts, base_x=100, base_y=100, step=60):
    """テスト用 OCR 結果を生成する。"""
    results = []
    for i, text in enumerate(texts):
        results.append({
            "text": text,
            "confidence": 0.9,
            "center": [base_x + i * step, base_y],
        })
    return results


class TestRecordTap:
    """record_tap → maybe_record で遷移が正しく記録される。"""

    def test_basic_transition(self, tmp_env):
        db_path, storage, img_path = tmp_env
        rec = ScreenRecorder(db_path, storage, "test_session_1")

        # 画面 A を記録
        ocr_a = _make_ocr(["ホーム", "クエスト", "ショップ"])
        assert rec.maybe_record(img_path, ocr_a, "MENU", "aaaa1111", force=False)
        screen_a_id = rec._last_inserted_id
        screen_a_fp = rec._last_recorded_fp
        assert screen_a_id is not None
        assert screen_a_fp is not None

        # タップを記録 (from=A, to=未定)
        rec.record_tap(
            from_screen_id=screen_a_id,
            from_fp=screen_a_fp,
            tap_x=200, tap_y=100,
            tap_label="クエスト",
            action_name="STORY_TAP",
        )
        assert rec._pending_transition is not None

        # 画面 B を記録 → to が確定
        ocr_b = _make_ocr(["クエスト選択", "第1章", "第2章"])
        assert rec.maybe_record(img_path, ocr_b, "MENU", "bbbb2222", force=False)
        screen_b_id = rec._last_inserted_id
        screen_b_fp = rec._last_recorded_fp

        # pending がフラッシュされている
        assert rec._pending_transition is None

        # DB 確認
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT * FROM lc_transitions").fetchall()
        assert len(rows) == 1
        row = rows[0]
        # (id, session_id, from_screen_id, to_screen_id, from_fp, to_fp,
        #  tap_x, tap_y, tap_label, action_name, discovered_at)
        assert row[2] == screen_a_id  # from_screen_id
        assert row[3] == screen_b_id  # to_screen_id
        assert row[4] == screen_a_fp  # from_fp
        assert row[5] == screen_b_fp  # to_fp
        assert row[6] == 200          # tap_x
        assert row[7] == 100          # tap_y
        assert row[8] == "クエスト"   # tap_label
        assert row[9] == "STORY_TAP"  # action_name
        conn.close()
        rec.close()

    def test_tap_without_screen_change(self, tmp_env):
        """タップ後に画面が変わらない → to_screen_id=NULL で記録。"""
        db_path, storage, img_path = tmp_env
        rec = ScreenRecorder(db_path, storage, "test_session_2")

        ocr_a = _make_ocr(["ホーム"])
        rec.maybe_record(img_path, ocr_a, "MENU", "aaaa1111")
        rec.record_tap(
            from_screen_id=rec._last_inserted_id,
            from_fp=rec._last_recorded_fp,
            tap_x=100, tap_y=100,
            tap_label="ホーム",
            action_name="TAP",
        )

        # close() でフラッシュ
        rec.close()

        conn = sqlite3.connect(str(db_path))
        rows = conn.execute("SELECT to_screen_id, to_fp FROM lc_transitions").fetchall()
        assert len(rows) == 1
        assert rows[0][0] is None  # to_screen_id = NULL
        assert rows[0][1] is None  # to_fp = NULL
        conn.close()

    def test_consecutive_taps_flush_previous(self, tmp_env):
        """連続タップ: 1つ目の pending が to=NULL でフラッシュされる。"""
        db_path, storage, img_path = tmp_env
        rec = ScreenRecorder(db_path, storage, "test_session_3")

        ocr_a = _make_ocr(["画面A"])
        rec.maybe_record(img_path, ocr_a, "MENU", "aaaa1111")

        # 1回目のタップ
        rec.record_tap(
            from_screen_id=rec._last_inserted_id,
            from_fp=rec._last_recorded_fp,
            tap_x=100, tap_y=100,
            tap_label="ボタン1",
            action_name="TAP_1",
        )

        # 2回目のタップ (画面変わらず) → 1つ目が to=NULL でフラッシュ
        rec.record_tap(
            from_screen_id=rec._last_inserted_id,
            from_fp=rec._last_recorded_fp,
            tap_x=200, tap_y=200,
            tap_label="ボタン2",
            action_name="TAP_2",
        )

        # この時点で DB に 1 レコード (to=NULL), pending に 1 レコード
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT action_name, to_screen_id FROM lc_transitions ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "TAP_1"
        assert rows[0][1] is None

        # 画面 B が来て pending が確定
        ocr_b = _make_ocr(["画面B"])
        rec.maybe_record(img_path, ocr_b, "MENU", "bbbb2222")

        rows = conn.execute(
            "SELECT action_name, to_screen_id FROM lc_transitions ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[1][0] == "TAP_2"
        assert rows[1][1] is not None  # to が確定
        conn.close()
        rec.close()


class TestResolveTapLabel:
    """_resolve_tap_label のテスト。"""

    def test_nearest_text(self):
        ocr = _make_ocr(["ホーム", "クエスト", "ショップ"], base_x=100, step=100)
        # tap (195, 100) → 最近傍は "クエスト" (center=200,100, dist=5)
        label = ScreenRecorder._resolve_tap_label(195, 100, ocr)
        assert label == "クエスト"

    def test_out_of_range(self):
        ocr = _make_ocr(["ホーム"], base_x=100, base_y=100)
        # tap (500, 500) → 半径 50px 外
        label = ScreenRecorder._resolve_tap_label(500, 500, ocr)
        assert label == ""

    def test_empty_ocr(self):
        label = ScreenRecorder._resolve_tap_label(100, 100, [])
        assert label == ""


class TestSessionCleanup:
    """セッション開始時の未完了遷移クリーンアップ。"""

    def test_cleanup_old_null_transitions(self, tmp_env):
        db_path, storage, img_path = tmp_env

        # セッション1: 未完了遷移を残して閉じない (クラッシュ想定)
        rec1 = ScreenRecorder(db_path, storage, "session_old")
        ocr = _make_ocr(["テスト"])
        rec1.maybe_record(img_path, ocr, "MENU", "aaaa1111")
        rec1.record_tap(
            from_screen_id=rec1._last_inserted_id,
            from_fp=rec1._last_recorded_fp,
            tap_x=100, tap_y=100,
            tap_label="テスト",
            action_name="TAP",
        )
        # close() しない = pending は DB に書かれない
        # 手動でフラッシュ (close なしの場合)
        rec1._insert_transition(rec1._pending_transition)
        rec1._pending_transition = None
        rec1._conn.close()

        # 確認: session_old の to=NULL レコードがある
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT * FROM lc_transitions WHERE to_screen_id IS NULL"
        ).fetchall()
        assert len(rows) == 1
        conn.close()

        # セッション2 開始: session_old のゴミが消える
        rec2 = ScreenRecorder(db_path, storage, "session_new")
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT * FROM lc_transitions WHERE to_screen_id IS NULL"
        ).fetchall()
        assert len(rows) == 0
        conn.close()
        rec2.close()
