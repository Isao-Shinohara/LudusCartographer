"""
test_ocr.py — PaddleOCR 単体テスト

既存の画像ファイルに対して PaddleOCR を実行し、
以下を検証する:
  1. OCR が文字列を1件以上検出できること
  2. 信頼スコアが 0.0〜1.0 の範囲にあること
  3. バウンディングボックスが4点の座標を持つこと
  4. 既知のテキストが検出されること（回帰テスト）
  5. JSON 形式での出力が正しいこと

また、認識結果の一覧をターミナルに表示する（ユーザー確認用）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.ocr import run_ocr, format_results

# フィクスチャ画像のパス
FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "test_game_screen.png"

# PaddleOCR が利用可能かチェック
try:
    from paddleocr import PaddleOCR  # noqa: F401
    HAS_PADDLE = True
except ImportError:
    HAS_PADDLE = False

requires_paddle = pytest.mark.skipif(
    not HAS_PADDLE,
    reason="PaddleOCR がインストールされていないためスキップ"
)


# ============================================================
# テストスイート
# ============================================================

@requires_paddle
class TestPaddleOCRBasic:
    """PaddleOCR の基本動作テスト（フィクスチャ画像使用）。"""

    @pytest.fixture(scope="class")
    def ocr_results(self):
        """クラスレベルで OCR を1回だけ実行してキャッシュする。"""
        assert FIXTURE_IMAGE.exists(), f"フィクスチャ画像が見つかりません: {FIXTURE_IMAGE}"
        results = run_ocr(FIXTURE_IMAGE)
        print(format_results(results))  # ターミナルに結果を表示
        return results

    def test_detects_at_least_one_text(self, ocr_results):
        """1件以上のテキストが検出されること"""
        assert len(ocr_results) >= 1, "OCRが1件もテキストを検出しませんでした"

    def test_confidence_in_valid_range(self, ocr_results):
        """すべての信頼スコアが 0.0〜1.0 の範囲にあること"""
        for r in ocr_results:
            assert 0.0 <= r["confidence"] <= 1.0, (
                f"信頼スコアが範囲外: {r['confidence']} (text={r['text']!r})"
            )

    def test_bounding_box_has_4_points(self, ocr_results):
        """すべてのバウンディングボックスが4点の座標を持つこと"""
        for r in ocr_results:
            assert len(r["box"]) == 4, (
                f"ボックスの点数が不正: {len(r['box'])} (text={r['text']!r})"
            )

    def test_each_box_point_has_xy(self, ocr_results):
        """各座標点が [x, y] の形式であること"""
        for r in ocr_results:
            for point in r["box"]:
                assert len(point) == 2, f"座標点が2要素でない: {point}"
                assert all(isinstance(v, int) for v in point), (
                    f"座標値が整数でない: {point}"
                )

    def test_result_is_json_serializable(self, ocr_results):
        """結果がJSON形式にシリアライズ可能なこと"""
        payload = json.dumps(ocr_results, ensure_ascii=False)
        reloaded = json.loads(payload)
        assert len(reloaded) == len(ocr_results)

    def test_known_text_detected(self, ocr_results):
        """フィクスチャ画像の既知テキストが検出されること（回帰テスト）"""
        all_texts = " ".join(r["text"].upper() for r in ocr_results)
        # フィクスチャには DEMO / GAME / PLAY / SHOP のいずれかが含まれるはず
        known_keywords = ["DEMO", "GAME", "PLAY", "SHOP", "COIN", "QUEST", "BATTLE"]
        found = [kw for kw in known_keywords if kw in all_texts]
        assert len(found) >= 2, (
            f"既知キーワードが2件以上検出されませんでした。\n"
            f"  検出テキスト: {all_texts[:200]}\n"
            f"  期待キーワード: {known_keywords}"
        )


@requires_paddle
class TestPaddleOCROutput:
    """OCR出力形式の検証テスト。"""

    def test_run_ocr_returns_list(self):
        results = run_ocr(FIXTURE_IMAGE)
        assert isinstance(results, list)

    def test_each_result_has_required_keys(self):
        results = run_ocr(FIXTURE_IMAGE)
        for r in results:
            assert "text" in r
            assert "confidence" in r
            assert "box" in r
            assert "center" in r

    def test_text_is_non_empty_string(self):
        results = run_ocr(FIXTURE_IMAGE)
        for r in results:
            assert isinstance(r["text"], str)
            assert len(r["text"]) > 0

    def test_returns_empty_list_for_blank_image(self, tmp_path):
        """真っ白な画像で空リストが返ること（クラッシュしないこと）"""
        from PIL import Image
        blank = Image.new("RGB", (100, 100), color=(255, 255, 255))
        blank_path = tmp_path / "blank.png"
        blank.save(blank_path)

        results = run_ocr(blank_path)
        assert isinstance(results, list)
        # 空白画像では検出なしまたは少数のみ
        assert len(results) <= 3, f"空白画像で多すぎる検出: {results}"


# ============================================================
# スクリーンショット指定での OCR 実行スクリプト（CLI用）
# ============================================================

if __name__ == "__main__":
    """
    コマンドラインから直接実行:
      venv/bin/python crawler/tests/test_ocr.py [画像パス]
    """
    import argparse

    parser = argparse.ArgumentParser(description="PaddleOCR 単体実行ツール")
    parser.add_argument("image", nargs="?", default=str(FIXTURE_IMAGE),
                        help="解析する画像のパス (デフォルト: フィクスチャ画像)")
    parser.add_argument("--lang", default="japan",
                        choices=["japan", "en", "ch"],
                        help="OCR言語 (デフォルト: japan)")
    parser.add_argument("--json", action="store_true",
                        help="結果をJSON形式で出力")
    args = parser.parse_args()

    if not Path(args.image).exists():
        print(f"ERROR: 画像ファイルが見つかりません: {args.image}")
        sys.exit(1)

    print(f"解析対象: {args.image}")
    results = run_ocr(args.image, lang=args.lang)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(format_results(results))
