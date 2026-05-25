"""見切れ検出 (黒ピクセル ≥50%) ロジックのテスト。

問題:
  ee728cd (4/23) で導入された「黒ピクセル ≥50% → artifact」検出が、
  STARTUP/LOADING シーンの正常な暗背景 (ロゴ画面、ローディング) も
  artifact として弾いていた。さらに ocr_text_gemini を '' で上書きして
  後続の _remerge_text_clusters の COALESCE フォールバックを破壊し、
  異なる画面 (ANIPLEX/POKELABO/注意事項) が同一クラスタに統合される
  連鎖バグが起きていた。

修正:
  1. scene が STARTUP/LOADING の場合は黒比率検出をスキップ
  2. 検出時の DB 書き込みから ocr_text_gemini = '' を削除
     (is_artifact=1 のみマーク。OCR テキストは保持)
"""
import cv2
import numpy as np
import pytest

from tools.ap.background_worker import BackgroundWorker


@pytest.fixture
def black_image(tmp_path):
    """98% 以上が黒の画像を生成 (ロゴ画面想定)。"""
    img = np.zeros((720, 1440, 3), dtype=np.uint8)
    # 中央にロゴ風の白いテキスト (約 2% 占有)
    cv2.rectangle(img, (650, 340), (790, 380), (255, 255, 255), -1)
    p = tmp_path / "black.png"
    cv2.imwrite(str(p), img)
    return p


@pytest.fixture
def bright_image(tmp_path):
    """明るい画像 (通常の MENU 等)。"""
    img = np.full((720, 1440, 3), 200, dtype=np.uint8)
    p = tmp_path / "bright.png"
    cv2.imwrite(str(p), img)
    return p


class TestIsTruncatedCapture:
    """見切れ判定の単体テスト。"""

    def test_startup_logo_is_not_truncated(self, black_image):
        """STARTUP シーンの黒背景ロゴ画面は見切れ判定されない。"""
        assert BackgroundWorker._is_truncated_capture("STARTUP", str(black_image)) is False

    def test_loading_screen_is_not_truncated(self, black_image):
        """LOADING シーンの暗画面も見切れ判定されない。"""
        assert BackgroundWorker._is_truncated_capture("LOADING", str(black_image)) is False

    def test_menu_with_mostly_black_is_truncated(self, black_image):
        """MENU で 98% 黒のキャプチャは見切れ扱い (本来の用途)。"""
        assert BackgroundWorker._is_truncated_capture("MENU", str(black_image)) is True

    def test_unknown_scene_with_mostly_black_is_truncated(self, black_image):
        """UNKNOWN/その他シーンでの黒キャプチャも見切れ扱い。"""
        assert BackgroundWorker._is_truncated_capture("UNKNOWN", str(black_image)) is True

    def test_battle_with_mostly_black_is_truncated(self, black_image):
        """BATTLE で 98% 黒は見切れ (キャプチャ事故)。"""
        assert BackgroundWorker._is_truncated_capture("BATTLE", str(black_image)) is True

    def test_bright_image_never_truncated(self, bright_image):
        """明るい画像はどの scene でも見切れ判定されない。"""
        for scene in ["STARTUP", "LOADING", "MENU", "ADV", "BATTLE", "MOVIE", "UNKNOWN"]:
            assert BackgroundWorker._is_truncated_capture(scene, str(bright_image)) is False, scene

    def test_missing_file_returns_false(self):
        """存在しないファイルは False を返す (例外は出さない)。"""
        assert BackgroundWorker._is_truncated_capture("MENU", "/nonexistent/path.png") is False

    def test_empty_path_returns_false(self):
        """空文字パスは False を返す。"""
        assert BackgroundWorker._is_truncated_capture("MENU", "") is False

    def test_none_scene_treats_as_check(self, black_image):
        """scene が None の場合は通常通り黒比率チェックする。"""
        assert BackgroundWorker._is_truncated_capture(None, str(black_image)) is True

    def test_threshold_boundary_below(self, tmp_path):
        """黒ピクセル比率が閾値 (50%) を僅かに下回る場合は見切れではない。"""
        img = np.full((720, 1440, 3), 200, dtype=np.uint8)
        # 49% を黒で塗りつぶす
        img[:, :706, :] = 0  # 706/1440 ≈ 49.0%
        p = tmp_path / "boundary_below.png"
        cv2.imwrite(str(p), img)
        assert BackgroundWorker._is_truncated_capture("MENU", str(p)) is False

    def test_threshold_boundary_above(self, tmp_path):
        """黒ピクセル比率が閾値を僅かに上回ると見切れ判定される。"""
        img = np.full((720, 1440, 3), 200, dtype=np.uint8)
        img[:, :740, :] = 0  # 740/1440 ≈ 51.4%
        p = tmp_path / "boundary_above.png"
        cv2.imwrite(str(p), img)
        assert BackgroundWorker._is_truncated_capture("MENU", str(p)) is True
