"""
test_auto_navigation.py — 自動タップ + 画面遷移テスト

iOS シミュレータで「設定」アプリの「一般」をタップし、
画面遷移後の OCR で「情報」「ソフトウェア・アップデート」等を検証する。

【前提条件】
  - Appium サーバーが起動済み (デフォルト: 127.0.0.1:4723)
  - iPhone 16 iOS 18.5 シミュレータが Booted 状態

【実行方法】
  # インフラを起動
  xcrun simctl boot BA7E719D-8EBA-4049-996C-AC51945A7AE4
  PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723 &

  # テスト実行
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \\
    venv/bin/python -m pytest tests/test_auto_navigation.py -v -s

【証拠ファイル】(CLAUDE.md §9)
  crawler/evidence/<session_id>/
    ├── <time>_tap_general/
    │   ├── before.png       # タップ前スクリーンショット
    │   ├── after.png        # タップ直後スクリーンショット (0.5 秒後)
    │   └── ocr_result.json  # タップ時の OCR データ
    └── <time>_after_transition.png  # 遷移完了後スクリーンショット

  crawler/screenshots/
    └── after_tap_general.png  # 遷移後スクリーンショット (クイックアクセス用)
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.capabilities import simulator_config_from_env
from lc.driver import ios_simulator_session
from lc.ocr import center_of_box, find_best, find_text, format_results, run_ocr

# ============================================================
# 定数
# ============================================================

SIMULATOR_UDID   = "BA7E719D-8EBA-4049-996C-AC51945A7AE4"  # iPhone 16 / iOS 18.5
BUNDLE_ID        = "com.apple.Preferences"
APPIUM_HOST      = os.environ.get("APPIUM_HOST", "127.0.0.1")
APPIUM_PORT      = int(os.environ.get("APPIUM_PORT", "4723"))
SCREENSHOTS_DIR  = Path(__file__).parent.parent / "screenshots"

# 「一般」タップ後に表示されるはずの項目 (一般設定の子項目)
GENERAL_SUBSCREEN_ITEMS = [
    "情報",
    "ソフトウェア・アップデート",
    "AirDrop",
    "iPhoneストレージ",
    "キーボード",
]

# OCR フォールバック座標 (Phase 2-1 で確認済み)
FALLBACK_GENERAL_COORD = (262, 1071)

# ============================================================
# インフラ確認ヘルパー
# ============================================================

def _is_appium_up(host: str = APPIUM_HOST, port: int = APPIUM_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _is_simulator_booted(udid: str = SIMULATOR_UDID) -> bool:
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if udid in line and "Booted" in line:
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


# ============================================================
# pytest スキップ条件
# ============================================================

requires_appium = pytest.mark.skipif(
    not _is_appium_up(),
    reason=(
        f"Appium サーバーが起動していません ({APPIUM_HOST}:{APPIUM_PORT})。\n"
        f"  起動方法: PATH=\"$HOME/.nodebrew/current/bin:$PATH\" "
        f"appium --port {APPIUM_PORT} &"
    ),
)

requires_simulator = pytest.mark.skipif(
    not _is_simulator_booted(),
    reason=(
        f"シミュレータ {SIMULATOR_UDID} が Booted 状態ではありません。\n"
        f"  起動方法: xcrun simctl boot {SIMULATOR_UDID}"
    ),
)

# ============================================================
# モジュールスコープ fixture: 画面遷移を1回だけ実行
# ============================================================

@pytest.fixture(scope="module")
def navigation_state():
    """
    Settings アプリを開き、「一般」をタップして遷移後の状態を返す。

    Returns: dict with keys:
        before_ocr       : list[dict]  - タップ前 OCR 結果
        after_ocr        : list[dict]  - 遷移後 OCR 結果
        general_coord    : (int, int)  - タップした「一般」の座標
        after_shot_path  : Path        - 遷移後スクリーンショットのパス
        evidence_session : str         - セッション ID
    """
    # 環境変数を設定（未設定の場合のデフォルト）
    os.environ.setdefault("IOS_BUNDLE_ID",       BUNDLE_ID)
    os.environ.setdefault("IOS_SIMULATOR_UDID",  SIMULATOR_UDID)
    os.environ.setdefault("IOS_USE_SIMULATOR",   "1")

    cfg = simulator_config_from_env()

    with ios_simulator_session(cfg) as d:
        # --------------------------------------------------
        # STEP 1: ルート画面に戻る（前回テストの残状態対策）
        # --------------------------------------------------
        print("\n[STEP 1] ルート画面への移動...")
        at_root = d.navigate_back_to_root(root_keyword="設定", root_max_y=500)
        if not at_root:
            # back で戻れなかった場合は少し待ってから続行
            print("[STEP 1] ルート画面に戻れませんでした。現在の画面から続行します。")
            d.wait(1)

        # --------------------------------------------------
        # STEP 2: 初期スクリーンショット + OCR
        # --------------------------------------------------
        print("[STEP 2] 初期スクリーンショット + OCR...")
        initial_shot = d.screenshot("initial_settings")
        before_ocr = run_ocr(initial_shot)
        print(format_results(before_ocr))

        # --------------------------------------------------
        # STEP 3: 「一般」の座標を動的検索
        # --------------------------------------------------
        print("[STEP 3] 「一般」の座標を OCR から動的検索...")
        general_entry = find_best(before_ocr, "一般", min_confidence=0.5)

        if general_entry:
            cx, cy = general_entry["center"]
            print(f"  [OCR] 「一般」を検出: center=({cx},{cy}), conf={general_entry['confidence']:.3f}")
        else:
            cx, cy = FALLBACK_GENERAL_COORD
            print(f"  [FALLBACK] 「一般」が OCR で見つかりませんでした。既知座標 ({cx},{cy}) を使用します")

        # --------------------------------------------------
        # STEP 4: タップ（OCRピクセル座標 → 論理ポイント自動変換）
        # --------------------------------------------------
        print(f"[STEP 4] 「一般」をタップ (pixel座標): ({cx}, {cy})...")
        ocr_evidence = {
            "ocr_boxes": [
                {
                    "text":       general_entry["text"] if general_entry else "一般",
                    "confidence": general_entry["confidence"] if general_entry else 0.0,
                    "box":        general_entry["box"] if general_entry else [],
                }
            ]
        } if general_entry else None

        # tap_ocr_coordinate: OCR ピクセル座標 → デバイス論理ポイントに自動変換してタップ
        d.tap_ocr_coordinate(cx, cy, action_name="tap_general", ocr_data=ocr_evidence)

        # --------------------------------------------------
        # STEP 5: 遷移待機（画面アニメーション完了まで）
        # --------------------------------------------------
        print("[STEP 5] 遷移待機 (4秒)...")
        d.wait(4)

        # --------------------------------------------------
        # STEP 6: 遷移後スクリーンショット
        # --------------------------------------------------
        print("[STEP 6] 遷移後スクリーンショット撮影...")
        after_shot = d.screenshot("after_transition")

        # screenshots/ ディレクトリにもコピー（クイックアクセス用）
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        dest = SCREENSHOTS_DIR / "after_tap_general.png"
        shutil.copy2(str(after_shot), str(dest))
        print(f"  [SAVE] {dest}")

        # --------------------------------------------------
        # STEP 7: 遷移後 OCR
        # --------------------------------------------------
        print("[STEP 7] 遷移後 OCR 解析...")
        after_ocr = run_ocr(after_shot)
        print(format_results(after_ocr))

        yield {
            "before_ocr":       before_ocr,
            "after_ocr":        after_ocr,
            "general_coord":    (cx, cy),
            "after_shot_path":  after_shot,
            "screenshots_copy": dest,
            "evidence_session": d.session_id,
        }


# ============================================================
# テストクラス
# ============================================================

@requires_appium
@requires_simulator
class TestNavigationPreconditions:
    """タップ前の初期画面が正しい状態であることを確認する。"""

    def test_initial_screen_shows_settings(self, navigation_state):
        """初期画面に「設定」タイトルが存在すること"""
        before_ocr = navigation_state["before_ocr"]
        match = find_best(before_ocr, "設定")
        assert match is not None, (
            "初期画面に「設定」が検出されませんでした。\n"
            f"  全テキスト: {[r['text'] for r in before_ocr]}"
        )

    def test_general_item_found_dynamically(self, navigation_state):
        """「一般」が OCR 動的検索で座標付きで取得できること"""
        before_ocr = navigation_state["before_ocr"]
        general = find_best(before_ocr, "一般")
        assert general is not None, (
            "「一般」が初期画面で OCR 検出されませんでした。\n"
            f"  全テキスト: {[r['text'] for r in before_ocr]}"
        )
        cx, cy = general["center"]
        print(f"\n  [座標] 「一般」: center=({cx},{cy}), conf={general['confidence']:.3f}")
        # 画面の左半分 (x < 600) かつ中段 (y: 800〜1400) にあるはず
        assert cx < 600,  f"「一般」x 座標が右すぎます: cx={cx}"
        assert 800 < cy < 1400, f"「一般」y 座標が予期範囲外です: cy={cy}"


@requires_appium
@requires_simulator
class TestTransitionResult:
    """「一般」タップ後の画面遷移結果を検証する。"""

    def test_after_screenshot_saved(self, navigation_state):
        """遷移後スクリーンショットが保存されていること"""
        after_shot = navigation_state["after_shot_path"]
        assert after_shot.exists(), f"スクリーンショットが存在しません: {after_shot}"

        screenshots_copy = navigation_state["screenshots_copy"]
        assert screenshots_copy.exists(), f"コピーが存在しません: {screenshots_copy}"
        print(f"\n  スクリーンショット: {screenshots_copy}")

    def test_after_screen_differs_from_before(self, navigation_state):
        """遷移後の画面内容がタップ前と異なること（テキストが変化していること）"""
        before_texts = {r["text"] for r in navigation_state["before_ocr"]}
        after_texts  = {r["text"] for r in navigation_state["after_ocr"]}
        # 遷移後に新しいテキストが1件以上現れること
        new_texts = after_texts - before_texts
        assert len(new_texts) >= 1, (
            "遷移前後でテキストに変化がありません。タップが効いていない可能性があります。\n"
            f"  before: {sorted(before_texts)}\n"
            f"  after : {sorted(after_texts)}"
        )
        print(f"\n  新たに検出されたテキスト ({len(new_texts)} 件): {sorted(new_texts)}")

    def test_general_subscreen_items_detected(self, navigation_state):
        """
        遷移後の画面に「一般」サブ画面固有の項目が含まれること。
        GENERAL_SUBSCREEN_ITEMS のうち1件以上が検出されれば OK。
        """
        after_ocr  = navigation_state["after_ocr"]
        after_texts = " ".join(r["text"] for r in after_ocr)

        found = [item for item in GENERAL_SUBSCREEN_ITEMS if item in after_texts]
        assert len(found) >= 1, (
            "「一般」サブ画面の項目が検出されませんでした。\n"
            f"  期待: {GENERAL_SUBSCREEN_ITEMS}\n"
            f"  検出テキスト: {[r['text'] for r in after_ocr]}"
        )
        print(f"\n  「一般」サブ画面で検出された項目: {found}")

    def test_general_title_on_after_screen(self, navigation_state):
        """遷移後の画面ナビゲーションバーに「一般」タイトルが表示されること"""
        after_ocr = navigation_state["after_ocr"]
        title = find_best(after_ocr, "一般")
        assert title is not None, (
            "遷移後の画面に「一般」タイトルが見つかりません。\n"
            f"  検出テキスト: {[r['text'] for r in after_ocr]}"
        )
        cx, cy = title["center"]
        print(f"\n  [座標] 遷移後「一般」タイトル: center=({cx},{cy}), conf={title['confidence']:.3f}")

    def test_print_after_screen_with_coordinates(self, navigation_state):
        """遷移後の全テキストと座標を出力する（確認用 / 常に PASS）"""
        after_ocr = navigation_state["after_ocr"]
        print("\n" + "=" * 62)
        print("  「一般」画面遷移後 — 検出テキストと中心座標")
        print("=" * 62)
        for r in after_ocr:
            cx, cy = r["center"]
            print(
                f"  conf={r['confidence']:.3f}"
                f"  center=({cx:4d},{cy:4d})"
                f"  {r['text']!r}"
            )
        print("=" * 62)
        assert True


@requires_appium
@requires_simulator
class TestEvidenceFiles:
    """CLAUDE.md §9 証拠ファイルが正しく保存されていることを確認する。"""

    def test_evidence_directory_exists(self, navigation_state):
        """evidence/<session_id>/ ディレクトリが作成されていること"""
        session_id = navigation_state["evidence_session"]
        evidence_dir = Path(__file__).parent.parent / "evidence" / session_id
        assert evidence_dir.exists(), f"証拠ディレクトリが存在しません: {evidence_dir}"

    def test_tap_evidence_files_exist(self, navigation_state):
        """tap_general アクションの before.png / after.png / ocr_result.json が存在すること"""
        session_id = navigation_state["evidence_session"]
        evidence_dir = Path(__file__).parent.parent / "evidence" / session_id

        tap_dirs = list(evidence_dir.glob("*tap_general*"))
        assert tap_dirs, f"tap_general の証拠ディレクトリが見つかりません: {evidence_dir}"

        tap_dir = tap_dirs[0]
        for fname in ("before.png", "after.png", "ocr_result.json"):
            fpath = tap_dir / fname
            assert fpath.exists(), f"証拠ファイルが存在しません: {fpath}"

    def test_ocr_result_json_is_valid(self, navigation_state):
        """ocr_result.json が正しい形式で保存されていること"""
        session_id = navigation_state["evidence_session"]
        evidence_dir = Path(__file__).parent.parent / "evidence" / session_id

        tap_dirs = list(evidence_dir.glob("*tap_general*"))
        if not tap_dirs:
            pytest.skip("tap_general 証拠ディレクトリが見つかりません")

        json_path = tap_dirs[0] / "ocr_result.json"
        data = json.loads(json_path.read_text(encoding="utf-8"))

        assert "action" in data
        assert "x" in data
        assert "y" in data
        assert "timestamp" in data
        print(f"\n  ocr_result.json: action={data['action']}, x={data['x']}, y={data['y']}")


# ============================================================
# CLI 実行（直接起動時）
# ============================================================

if __name__ == "__main__":
    """
    スタンドアロン実行（pytest なし）:
      IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \\
        venv/bin/python tests/test_auto_navigation.py
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if not _is_appium_up():
        print(f"ERROR: Appium が起動していません ({APPIUM_HOST}:{APPIUM_PORT})")
        print(f"  起動: PATH=\"$HOME/.nodebrew/current/bin:$PATH\" appium --port {APPIUM_PORT} &")
        sys.exit(1)
    if not _is_simulator_booted():
        print(f"ERROR: シミュレータ {SIMULATOR_UDID} が Booted 状態ではありません")
        print(f"  起動: xcrun simctl boot {SIMULATOR_UDID}")
        sys.exit(1)

    os.environ.setdefault("IOS_BUNDLE_ID",      BUNDLE_ID)
    os.environ.setdefault("IOS_SIMULATOR_UDID", SIMULATOR_UDID)
    os.environ.setdefault("IOS_USE_SIMULATOR",  "1")

    cfg = simulator_config_from_env()
    with ios_simulator_session(cfg) as d:
        print("\n[1] ルート画面へ移動...")
        d.navigate_back_to_root()

        print("[2] 初期 OCR...")
        shot = d.screenshot("initial")
        before_ocr = run_ocr(shot)
        print(format_results(before_ocr))

        print("[3] 「一般」を検索してタップ...")
        entry = find_best(before_ocr, "一般")
        if entry:
            cx, cy = entry["center"]
        else:
            cx, cy = FALLBACK_GENERAL_COORD
            print(f"  [FALLBACK] 座標 ({cx},{cy}) を使用")

        d.tap_ocr_coordinate(cx, cy, "tap_general")
        d.wait(4)

        print("[4] 遷移後 OCR...")
        after_shot = d.screenshot("after_transition")
        SCREENSHOTS_DIR.mkdir(exist_ok=True)
        shutil.copy2(str(after_shot), str(SCREENSHOTS_DIR / "after_tap_general.png"))
        after_ocr = run_ocr(after_shot)
        print(format_results(after_ocr))
