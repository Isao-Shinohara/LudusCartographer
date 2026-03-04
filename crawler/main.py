#!/usr/bin/env python3
"""
LudusCartographer — クローラー エントリポイント

環境変数を読み込み、iOS Simulator / 実機でアプリを自動探索する。

【使い方】
  # アプリ名を指定してシミュレータ実行
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences python main.py "iOS設定"

  # アプリ名省略 → モード別に自動命名 (TestRun_YYYYMMDD_HHMM)
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences python main.py

  # 実機ミラーリング (UxPlay 使用) — ワンコマンド
  python main.py --mirror --bundle com.example.mygame "MyGame"

  # 実機ミラーリング・アプリ名省略 → MirrorRun_YYYYMMDD_HHMM
  python main.py --mirror --bundle com.example.mygame

  # 探索パラメータ調整 (-d は --duration の短縮形)
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \
    python main.py "iOS設定" -d 600 --depth 4

【環境変数】
  IOS_BUNDLE_ID         ターゲットアプリの Bundle ID（必須）
  GAME_TITLE            ゲームタイトル（省略可 — 引数/env 未設定時は自動命名）
  DEVICE_MODE           "SIMULATOR" (デフォルト) または "MIRROR"
  IOS_USE_SIMULATOR     "1" でシミュレータモード
  IOS_SIMULATOR_UDID    シミュレータ UDID（省略可 — 自動選択）
  IOS_UDID              実機 UDID（省略可 — 自動検出）
  CRAWL_DURATION_SEC    クロール最大時間 秒 (デフォルト: 300)
  CRAWL_MAX_DEPTH       DFS 最大深さ (デフォルト: 3)
  DB_HOST               MySQL ホスト（省略可 — 未設定時は DB 保存スキップ）
  DB_NAME               MySQL DB 名 (デフォルト: ludus_cartographer)
  DB_USER               MySQL ユーザー (デフォルト: root)
  DB_PASSWORD           MySQL パスワード

  # MIRROR モード専用
  MIRROR_WINDOW_TITLE   キャプチャ対象ウィンドウタイトル (デフォルト: UxPlay を自動検索)
  MIRROR_DEVICE_WIDTH   デバイス論理幅 pt (デフォルト: 393)
  MIRROR_DEVICE_HEIGHT  デバイス論理高さ pt (デフォルト: 852)
  APPIUM_HOST           Appium ホスト (デフォルト: 127.0.0.1)
  APPIUM_PORT           Appium ポート (デフォルト: 4723)
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / ".env")

_MIRROR_SETUP_GUIDE = """\
  ┌─────────────────────────────────────────────────────┐
  │          📡  ミラーリング モード セットアップ         │
  └─────────────────────────────────────────────────────┘

  1. UxPlay をインストールして起動してください:
       brew install uxplay   (Homebrew)
       uxplay                # macOS デスクトップに iPhone 映像を表示

  2. iPhone の「コントロールセンター」→「画面ミラーリング」で
     UxPlay を選択してください。

  3. Appium サーバーを起動してください（別ターミナル）:
       PATH="$HOME/.nodebrew/current/bin:$PATH" appium --port 4723

  4. iPhone を Wi-Fi で Mac と同じネットワークに接続し、
     Appium が iPhone に Wi-Fi 経由で接続できることを確認してください。
     （USB 接続でも可）

  5. ウィンドウが見つからない場合は MIRROR_WINDOW_TITLE を設定:
       MIRROR_WINDOW_TITLE="UxPlay" python main.py --mirror --bundle ...

  詳細: https://github.com/Isao-Shinohara/LudusCartographer#mirror-mode
"""


def _resolve_game_title(
    app_name: Optional[str],
    title_opt: Optional[str],
    is_mirror: bool,
) -> str:
    """
    game_title を以下の優先順位で決定する:

      1. app_name 位置引数 (最優先)
      2. --title オプション
      3. GAME_TITLE 環境変数
      4. IOS_BUNDLE_ID 環境変数（設定されている場合）
      5. モード別自動命名:
           Simulator → TestRun_YYYYMMDD_HHMM
           Mirror    → MirrorRun_YYYYMMDD_HHMM
    """
    if app_name:
        return app_name
    if title_opt:
        return title_opt
    if os.environ.get("GAME_TITLE"):
        return os.environ["GAME_TITLE"]
    if os.environ.get("IOS_BUNDLE_ID"):
        return os.environ["IOS_BUNDLE_ID"]
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    return f"MirrorRun_{ts}" if is_mirror else f"TestRun_{ts}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LudusCartographer — モバイルアプリ自律クローラー",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # アプリ名を指定して実行 (Simulator)
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences python main.py "iOS設定"

  # アプリ名省略 → TestRun_YYYYMMDD_HHMM で自動命名
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences python main.py

  # 実機ミラーリング (UxPlay) — ワンコマンド
  python main.py --mirror --bundle com.example.mygame "MyGame"

  # ミラーリング・名前省略 → MirrorRun_YYYYMMDD_HHMM
  python main.py --mirror --bundle com.example.mygame

  # 探索パラメータ指定 (-d は --duration の短縮形)
  python main.py --mirror --bundle com.example.mygame "MyGame" -d 600 --depth 4
""",
    )
    parser.add_argument(
        "app_name",
        nargs="?",
        default=None,
        metavar="APP_NAME",
        help=(
            "アプリ/ゲーム名（省略可）。"
            " 省略時は IOS_BUNDLE_ID または TestRun_YYYYMMDD_HHMM / MirrorRun_YYYYMMDD_HHMM で自動命名。"
        ),
    )
    parser.add_argument(
        "--mirror",
        action="store_true",
        help=(
            "実機ミラーリングモード (UxPlay/scrcpy) で起動。"
            " DEVICE_MODE=MIRROR / IOS_USE_SIMULATOR=0 を自動設定。"
        ),
    )
    parser.add_argument(
        "--bundle",
        metavar="BUNDLE_ID",
        help="ターゲットアプリの Bundle ID (IOS_BUNDLE_ID 環境変数より優先)",
    )
    parser.add_argument(
        "--title",
        metavar="GAME_TITLE",
        help="ゲームタイトル (app_name 位置引数より低優先、後方互換用)",
    )
    parser.add_argument(
        "--duration", "-d",
        type=int,
        metavar="SEC",
        default=None,
        help="クロール最大時間 秒 (デフォルト: 300 = 5分)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        metavar="N",
        help="DFS 最大深さ (デフォルト: 3)",
    )
    # 未知の引数（環境変数由来のフラグ等）を無視して続行
    args, _ = parser.parse_known_args()
    return args


def main() -> None:
    args = _parse_args()

    # --mirror フラグ: 環境変数を上書きして MIRROR モードに切り替え
    if args.mirror:
        os.environ["DEVICE_MODE"]        = "MIRROR"
        os.environ["IOS_USE_SIMULATOR"]  = "0"
        print(_MIRROR_SETUP_GUIDE)

    # CLI 引数が環境変数より優先される
    if args.bundle:
        os.environ["IOS_BUNDLE_ID"] = args.bundle
    if args.duration is not None:
        os.environ["CRAWL_DURATION_SEC"] = str(args.duration)
    if args.depth:
        os.environ["CRAWL_MAX_DEPTH"] = str(args.depth)

    bundle_id = os.environ.get("IOS_BUNDLE_ID")
    if not bundle_id:
        print("ERROR: IOS_BUNDLE_ID が設定されていません。")
        if args.mirror:
            print("  例: python main.py --mirror --bundle com.example.mygame")
        else:
            print("  例: IOS_BUNDLE_ID=com.example.mygame python main.py")
        sys.exit(1)

    # is_mirror を先に確定してからゲームタイトルを決定する
    is_mirror = os.environ.get("DEVICE_MODE", "").upper() == "MIRROR"

    game_title = _resolve_game_title(
        app_name  = args.app_name,
        title_opt = args.title,
        is_mirror = is_mirror,
    )

    duration  = int(os.environ.get("CRAWL_DURATION_SEC", "300"))  # デフォルト 5 分
    max_depth = int(os.environ.get("CRAWL_MAX_DEPTH",    "3"))
    db_host   = os.environ.get("DB_HOST", "")

    # DEVICE_MODE 解決（driver_factory と同じロジック）
    from driver_factory import _resolve_device_mode
    device_mode = _resolve_device_mode()

    print("=" * 60)
    print("  LudusCartographer — 自律クロール開始")
    print("=" * 60)
    print(f"  ターゲット : {bundle_id}")
    print(f"  ゲーム名  : {game_title}")
    print(f"  モード    : {device_mode}")
    print(f"  最大時間  : {duration} 秒")
    print(f"  最大深さ  : {max_depth}")
    print(f"  DB 保存   : {'有効 (' + db_host + ')' if db_host else '無効 (DB_HOST 未設定)'}")
    print("=" * 60)

    # --- Driver セッション開始 ---
    from driver_adapter import BaseDriver
    from driver_factory import create_driver_session
    from lc.crawler import CrawlerConfig, ScreenCrawler
    from lc.ocr import find_best, run_ocr

    crawler_cfg = CrawlerConfig(
        game_title       = game_title,
        device_mode      = device_mode,
        max_duration_sec = duration,
        max_depth        = max_depth,
        db_host          = db_host,
        db_name          = os.environ.get("DB_NAME",     "ludus_cartographer"),
        db_user          = os.environ.get("DB_USER",     "root"),
        db_password      = os.environ.get("DB_PASSWORD", ""),
    )

    crawler: "ScreenCrawler | None" = None  # WindowNotFoundError 時の参照用

    with create_driver_session() as driver:
        try:
            # モーダルダイアログを解除（起動直後のアラート等）
            for _ in range(3):
                if not driver.dismiss_any_modal():
                    break
                driver.wait(1.0)

            # クローラー実行
            crawler = ScreenCrawler(driver, crawler_cfg)
            stats   = crawler.crawl(depth=0, parent_fp=None)

        except BaseDriver.WindowNotFoundError as exc:
            # ---- ウィンドウ喪失: 現時点のデータを保存して安全終了 ----
            print(f"\n[LC] ⚠ ウィンドウ消失を検知しました: {exc}")
            print("[LC] 現時点のクロールデータを保存して終了します...")
            if crawler is not None:
                evidence_dir = Path(__file__).parent / "evidence"
                # crawler が生成したセッションディレクトリに保存
                session_dirs = sorted(evidence_dir.glob("*/"))
                if session_dirs:
                    interrupted_path = session_dirs[-1] / "crawl_summary.json"
                    crawler.save_summary_json(interrupted_path)
                    print(f"[LC] 中断サマリーを保存: {interrupted_path}")
                    stats = crawler._stats  # type: ignore[attr-defined]
                    print(
                        f"[LC] 発見済み: {stats.screens_found} 画面"
                        f" / {stats.taps_total} 回タップ"
                    )
            return  # メイン処理を安全終了

        # サマリー表示
        print(crawler.summary())
        print(
            f"\n[LC] 完了: {stats.screens_found} 画面発見"
            f" / {stats.taps_total} 回タップ"
            f" / {stats.elapsed_sec:.1f} 秒"
        )

        # 遷移マップ出力
        evidence_dir = Path(__file__).parent / "evidence"
        latest_sessions = sorted(evidence_dir.glob("*/crawl_summary.json"))
        if latest_sessions:
            from tools.visualize_map import load_summary, build_graph, render_tree, analyze_gaps
            summary_path = latest_sessions[-1]
            screens = load_summary(summary_path.parent)
            graph   = build_graph(screens)
            print("\n" + render_tree(graph))
            gaps = analyze_gaps(screens)
            if gaps["unknown_screens"] or gaps["suspicious_titles"]:
                print("\n[LC] ⚠ 要調査:")
                for s in gaps["unknown_screens"]:
                    print(f"  unknown: depth={s.depth}  fp={s.fingerprint[:8]}")


if __name__ == "__main__":
    main()
