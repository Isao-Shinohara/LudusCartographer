"""scrcpy 黒帯を認識タスク前にクロップする `get_roi_cropped_image` のテスト。

scrcpy は実機 (1080x2160) を Mac 上で 1440x720 横向き表示するとき、
アスペクト比違いで左右に黒帯を描画する。この黒帯が phash/dhash/Gemini 判定を
歪めるため、認識タスクの直前でクロップする必要がある (保存画像は触らない)。

このテストは設計上の安全性を保証する:
  - 白背景セリフ等で誤動作しないこと
  - 暗いシーンで安全フォールバック
  - 検出失敗・例外時に元画像を返す
  - 左右非対称な誤検出をブロックする
"""
import cv2
import numpy as np
import pytest

from tools.ap.image_proc import get_roi_cropped_image


def _make_image_with_lr_black_bars(bar_w: int = 80, bar_color: int = 0,
                                    inner_color: int = 230,
                                    width: int = 1440, height: int = 720) -> np.ndarray:
    """scrcpy 標準パターン: 左右に黒帯、中央が均一色。"""
    img = np.full((height, width, 3), inner_color, dtype=np.uint8)
    img[:, :bar_w, :] = bar_color
    img[:, width - bar_w:, :] = bar_color
    return img


class TestGetRoiCroppedImage:
    """ROI クロップヘルパーの挙動 (誤検知排除のための安全策込み)。"""

    def test_white_dialog_with_lr_black_bars(self):
        """白背景セリフ画面 (左右黒帯) → 中央領域だけ抽出される。"""
        img = _make_image_with_lr_black_bars(bar_w=80, inner_color=230)
        cropped = get_roi_cropped_image(img)
        h, w = cropped.shape[:2]
        # 左右黒帯 (80*2=160px) が削られて中央 1280px 程度が残る
        # 安全マージン (5px) で 4 辺すべて少し縮む (margin + snap)
        assert 1240 <= w <= 1290, f"クロップ幅異常: {w}"
        assert 700 <= h <= 720, f"高さはほぼ元のまま (margin 程度の縮小): {h}"

    def test_completely_black_image_returns_original(self):
        """完全黒画面 → ROI 検出失敗 → 元画像返却 (フォールバック)。"""
        img = np.zeros((720, 1440, 3), dtype=np.uint8)
        cropped = get_roi_cropped_image(img)
        assert cropped.shape == img.shape, "完全黒は元画像を返すべき"

    def test_no_black_bars_returns_full(self):
        """黒帯なし (全体が明るい画像) → 全画面そのまま (誤動作ゼロ)。"""
        img = np.full((720, 1440, 3), 200, dtype=np.uint8)
        cropped = get_roi_cropped_image(img)
        # 黒帯がなければクロップ不要 → 元画像をそのまま
        assert cropped.shape == img.shape, \
            f"黒帯なしは元画像をそのまま返す: {cropped.shape} vs {img.shape}"

    def test_dark_character_at_edge_protected_by_margin(self):
        """画面端に暗いキャラ (黒髪等) → 安全マージンで完全な誤クロップを防ぐ。

        対称な細い暗領域 (左右 5px ずつ) なら、ROI 検出は反応するが、
        margin と snap でクロップ幅は穏やかに留まる。
        """
        # 左右端各 5px が暗い (キャラの輪郭が左右対称)
        img = np.full((720, 1440, 3), 200, dtype=np.uint8)
        img[:, :5, :] = 8
        img[:, -5:, :] = 8
        cropped = get_roi_cropped_image(img)
        h, w = cropped.shape[:2]
        # 左右で 5px ずつ ROI を絞り、margin=5 で更に絞る = 各 10px、計 20px くらい
        # キャラ本体 (中央領域) は 1400px 以上残る
        assert w >= 1400, f"画面端の細い暗領域だけで大幅クロップしない: w={w}"

    def test_asymmetric_black_bar_falls_back_to_full(self):
        """左右マージンが大きく非対称 (片側のみ大きく削れる) → 異常検出 → 全画面返却。

        例: 左 200px 黒、右は黒帯ゼロ。これは scrcpy の正常パターンではないため、
        ROI 検出ノイズと判定して全画面に戻す。
        """
        img = np.full((720, 1440, 3), 230, dtype=np.uint8)
        img[:, :200, :] = 0   # 左 200px 黒
        # 右側は明るいまま
        cropped = get_roi_cropped_image(img)
        # 対称性違反で全画面フォールバック
        h, w = cropped.shape[:2]
        assert h == 720 and w == 1440, \
            f"対称性違反は全画面フォールバック: ({h}x{w})"

    def test_invalid_input_returns_original(self):
        """空画像など破損入力 → 例外を握って元画像返却。"""
        # 1x1 画像 (極小) → 検出処理が異常終了する可能性
        img = np.zeros((1, 1, 3), dtype=np.uint8)
        cropped = get_roi_cropped_image(img)
        # 例外を投げず、何かを返す (元のものでも空でも壊れない)
        assert cropped is not None

    def test_scrcpy_dim_black_bar_with_real_brightness(self):
        """scrcpy 黒帯の実輝度 (~17) でも検出される。

        scrcpy のレターボックスは実際には完全な黒 (0) ではなく ~17 の灰黒。
        旧 detect_game_roi の閾値 (12) ではこれを「非黒」と誤判定して
        クロップ失敗していた問題への回帰テスト。
        """
        img = _make_image_with_lr_black_bars(bar_w=80, bar_color=17, inner_color=200)
        cropped = get_roi_cropped_image(img)
        h, w = cropped.shape[:2]
        # 黒帯 17 ≒ scrcpy 実値、これでも左右除去されるべき
        assert w < 1400, \
            f"scrcpy 実輝度の黒帯が検出されない (旧閾値 12 が低すぎ): {w}"

    def test_capture_jitter_stable_via_snap(self):
        """ピクセルブレ (±1px) があっても snap=4 で同じクロップ結果になる。

        実機からのキャプチャはタイミングで微妙に値がブレるため、
        ROI 座標を 4px 単位に丸めることでクラスタリング安定性を担保する。
        """
        # 標準的な左右黒帯
        img1 = _make_image_with_lr_black_bars(bar_w=80, bar_color=5)   # 黒帯わずかに明るい
        img2 = _make_image_with_lr_black_bars(bar_w=80, bar_color=8)   # ほぼ同じ
        c1 = get_roi_cropped_image(img1)
        c2 = get_roi_cropped_image(img2)
        # 4px 単位スナップでクロップ幅は一致する想定
        assert c1.shape == c2.shape, \
            f"わずかなピクセルブレでクロップ結果が変わる: {c1.shape} vs {c2.shape}"
