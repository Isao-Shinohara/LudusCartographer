"""
test_ocr_parsing.py — iOS シミュレータ スクリーンショット OCR 解析テスト

minimal_launch.py で取得した設定アプリの画像に対して PaddleOCR を実行し、
以下を検証する:
  1. 文字・座標のペアが正しく抽出できること
  2. 設定アプリの主要項目（一般・カメラ・設定 など）が座標付きで検出できること
  3. center_of_box() が画像サイズ内の座標を返すこと
  4. find_text() で指定した文字列が検索できること

スクリーンショットが存在しない環境では自動スキップする。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.ocr import run_ocr, center_of_box, find_text, find_best, format_results

# ============================================================
# テスト対象スクリーンショット検索
# ============================================================

EVIDENCE_DIR = Path(__file__).parent.parent / "evidence"

# iOS シミュレータ: iPhone 16 @2x の実寸 (points × 2)
# launch.png のサイズ: 1178 × 2556
SIMULATOR_IMG_SIZE = (1178, 2556)


def _find_latest_launch_screenshot() -> Path | None:
    """evidence/ 配下の最新 launch.png を返す。なければ None。"""
    if not EVIDENCE_DIR.exists():
        return None
    candidates = sorted(EVIDENCE_DIR.glob("*/*launch.png"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


LATEST_SCREENSHOT = _find_latest_launch_screenshot()

requires_screenshot = pytest.mark.skipif(
    LATEST_SCREENSHOT is None,
    reason="シミュレータのスクリーンショットが見つかりません。"
           "先に minimal_launch.py を実行してください。"
)

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
# フィクスチャ: セッションスコープで OCR を1回だけ実行
# ============================================================

@pytest.fixture(scope="session")
def sim_ocr_results():
    """シミュレータスクリーンショットに対して OCR を実行し、結果をキャッシュする。"""
    assert LATEST_SCREENSHOT is not None
    results = run_ocr(LATEST_SCREENSHOT, lang="japan")
    # 全結果をターミナルに出力（ユーザー確認用）
    print(f"\n[OCR] 解析対象: {LATEST_SCREENSHOT}")
    print(format_results(results))
    return results


# ============================================================
# 基本 OCR 品質テスト
# ============================================================

@requires_paddle
@requires_screenshot
class TestSimulatorOCRBasic:
    """シミュレータ画像に対する OCR の基本品質テスト。"""

    def test_detects_multiple_texts(self, sim_ocr_results):
        """設定アプリには多数のメニュー項目があるため、10件以上検出されること"""
        assert len(sim_ocr_results) >= 10, (
            f"検出数が少なすぎます: {len(sim_ocr_results)} 件\n"
            f"  設定アプリには多数の項目があるはずです。"
        )

    def test_confidence_in_valid_range(self, sim_ocr_results):
        """全エントリの信頼スコアが 0.0〜1.0 の範囲にあること"""
        for r in sim_ocr_results:
            assert 0.0 <= r["confidence"] <= 1.0, (
                f"信頼スコアが範囲外: {r['confidence']} ({r['text']!r})"
            )

    def test_each_result_has_required_keys(self, sim_ocr_results):
        """全エントリが text / confidence / box / center キーを持つこと"""
        for r in sim_ocr_results:
            for key in ("text", "confidence", "box", "center"):
                assert key in r, f"キー '{key}' がありません: {r}"

    def test_bounding_box_has_4_points(self, sim_ocr_results):
        """全エントリのバウンディングボックスが4点であること"""
        for r in sim_ocr_results:
            assert len(r["box"]) == 4, f"ボックスが4点でない: {r['box']} ({r['text']!r})"

    def test_center_coordinates_within_image(self, sim_ocr_results):
        """center 座標が画像サイズ内にあること"""
        w, h = SIMULATOR_IMG_SIZE
        for r in sim_ocr_results:
            cx, cy = r["center"]
            assert 0 <= cx <= w, f"cx={cx} が画像幅 {w} を超えています ({r['text']!r})"
            assert 0 <= cy <= h, f"cy={cy} が画像高さ {h} を超えています ({r['text']!r})"

    def test_results_are_json_serializable(self, sim_ocr_results):
        """全結果が JSON シリアライズ可能なこと"""
        payload = json.dumps(sim_ocr_results, ensure_ascii=False)
        reloaded = json.loads(payload)
        assert len(reloaded) == len(sim_ocr_results)


# ============================================================
# 設定アプリ固有テスト（座標検証）
# ============================================================

@requires_paddle
@requires_screenshot
class TestSettingsAppItems:
    """
    設定アプリ（com.apple.Preferences）の主要項目が
    正しい座標で検出できることを検証する。

    スクリーンショットに含まれるはずの項目:
        設定, 一般, カメラ, アクセシビリティ, スクリーンタイム, など
    """

    # 設定アプリで確実に表示されるはずの項目
    EXPECTED_ITEMS = ["設定", "一般", "カメラ"]

    def test_settings_title_detected(self, sim_ocr_results):
        """「設定」タイトルが検出されること"""
        matches = find_text(sim_ocr_results, "設定", min_confidence=0.5)
        assert matches, (
            "「設定」が検出されませんでした。\n"
            f"  全テキスト: {[r['text'] for r in sim_ocr_results]}"
        )

    def test_general_menu_detected_with_coordinate(self, sim_ocr_results):
        """「一般」メニュー項目が座標付きで検出されること"""
        item = find_best(sim_ocr_results, "一般", min_confidence=0.5)
        assert item is not None, (
            "「一般」が検出されませんでした。\n"
            f"  全テキスト: {[r['text'] for r in sim_ocr_results]}"
        )
        cx, cy = item["center"]
        w, h = SIMULATOR_IMG_SIZE
        # 「一般」は画面上半分にあるはず（縦中央より上）
        assert cy < h * 0.7, (
            f"「一般」の y 座標が予期より下です: cy={cy} (h={h})\n"
            f"  center={item['center']}, text={item['text']!r}"
        )
        print(f"\n[座標] 「一般」: center=({cx}, {cy}), conf={item['confidence']:.3f}")

    def test_camera_menu_detected_with_coordinate(self, sim_ocr_results):
        """「カメラ」メニュー項目が座標付きで検出されること"""
        item = find_best(sim_ocr_results, "カメラ", min_confidence=0.5)
        assert item is not None, (
            "「カメラ」が検出されませんでした。\n"
            f"  全テキスト: {[r['text'] for r in sim_ocr_results]}"
        )
        cx, cy = item["center"]
        print(f"\n[座標] 「カメラ」: center=({cx}, {cy}), conf={item['confidence']:.3f}")

    def test_general_is_above_camera(self, sim_ocr_results):
        """設定アプリのレイアウト: 「一般」は「カメラ」より上にあること"""
        general = find_best(sim_ocr_results, "一般")
        camera  = find_best(sim_ocr_results, "カメラ")
        if general is None or camera is None:
            pytest.skip("「一般」または「カメラ」が検出されませんでした")
        # y 座標が小さい = 画面上部
        assert general["center"][1] < camera["center"][1], (
            f"「一般」({general['center']}) が「カメラ」({camera['center']}) より下にあります"
        )

    def test_all_expected_items_detected(self, sim_ocr_results):
        """設定アプリの必須項目が全て検出されること"""
        all_texts = " ".join(r["text"] for r in sim_ocr_results)
        missing = [item for item in self.EXPECTED_ITEMS if item not in all_texts]
        assert not missing, (
            f"以下の項目が検出されませんでした: {missing}\n"
            f"  検出テキスト: {[r['text'] for r in sim_ocr_results]}"
        )

    def test_print_all_detected_items_with_coordinates(self, sim_ocr_results):
        """全検出テキストと中心座標を出力する（ユーザー確認・非 fail テスト）"""
        print("\n" + "=" * 62)
        print("  シミュレータ画面 — 検出テキストと中心座標")
        print("=" * 62)
        for r in sim_ocr_results:
            cx, cy = r["center"]
            print(
                f"  conf={r['confidence']:.3f}"
                f"  center=({cx:4d},{cy:4d})"
                f"  {r['text']!r}"
            )
        print("=" * 62)
        # このテストは常に PASS（出力確認用）
        assert True


# ============================================================
# center_of_box ユニットテスト（スクリーンショット不要）
# ============================================================

class TestCenterOfBox:
    """center_of_box() のユニットテスト（実機・OCR 不要）。"""

    def test_square_box_center(self):
        box = [[0, 0], [100, 0], [100, 100], [0, 100]]
        assert center_of_box(box) == [50, 50]

    def test_rectangle_box_center(self):
        box = [[10, 20], [110, 20], [110, 60], [10, 60]]
        assert center_of_box(box) == [60, 40]

    def test_tilted_box_approximation(self):
        """わずかに傾いたボックスの中心が近似値として正しいこと"""
        box = [[5, 0], [105, 2], [103, 52], [3, 50]]
        cx, cy = center_of_box(box)
        assert 50 <= cx <= 60
        assert 25 <= cy <= 30


# ============================================================
# CLI 実行（直接起動時）
# ============================================================

if __name__ == "__main__":
    """
    コマンドラインから直接実行:
      venv/bin/python tests/test_ocr_parsing.py [画像パス]
    """
    import argparse

    parser = argparse.ArgumentParser(description="シミュレータ OCR 解析ツール")
    parser.add_argument("image", nargs="?",
                        default=str(LATEST_SCREENSHOT) if LATEST_SCREENSHOT else "",
                        help="解析する画像のパス (デフォルト: 最新スクリーンショット)")
    parser.add_argument("--min-conf", type=float, default=0.0,
                        help="最低信頼スコア (デフォルト: 0.0 = 全て)")
    parser.add_argument("--find", metavar="KEYWORD",
                        help="特定キーワードの座標を検索")
    args = parser.parse_args()

    if not args.image or not Path(args.image).exists():
        print(f"ERROR: 画像ファイルが見つかりません: {args.image!r}")
        print("先に minimal_launch.py を実行してスクリーンショットを取得してください。")
        sys.exit(1)

    print(f"解析対象: {args.image}")
    results = run_ocr(args.image, min_confidence=args.min_conf)
    print(format_results(results))

    if args.find:
        matches = find_text(results, args.find)
        print(f"\n「{args.find}」の検索結果:")
        if not matches:
            print("  (見つかりませんでした)")
        for m in matches:
            print(f"  center={m['center']}  conf={m['confidence']:.3f}  text={m['text']!r}")
