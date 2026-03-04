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

  # ミラーリング・名前省略 → MirrorRun_YYYYMMDD_HHMM
  python main.py --mirror --bundle com.example.mygame

  # 探索パラメータ調整 (-d は --duration の短縮形)
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.apple.Preferences \\
    python main.py "iOS設定" -d 600 --depth 4

  # 探索後にブラウザで管理画面を自動表示
  python main.py --mirror --bundle com.example.mygame "MyGame" --open-web

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
import logging
import os
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / ".env")

logger = logging.getLogger(__name__)

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


def _ensure_directories() -> None:
    """実行に必要なディレクトリ構造を自動生成する。"""
    base = Path(__file__).parent
    for d in ["storage", "evidence", "logs", "assets/templates"]:
        (base / d).mkdir(parents=True, exist_ok=True)


def _configure_logging() -> None:
    """logging 設定（コンソール + logs/crawler.log）。"""
    log_dir  = Path(__file__).parent / "logs"
    log_file = log_dir / "crawler.log"

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt = "%H:%M:%S",
        handlers = [
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ],
    )
    # Appium / urllib3 の大量ログを抑制
    for noisy in ("urllib3", "appium", "selenium", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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


def _print_session_summary(
    stats,
    game_title: str,
    sqlite_db: Path,
    known_before: int,
) -> None:
    """
    探索完了後のセッションサマリーをコンソール表示する。

    表示内容:
      - 今回の新規発見画面数
      - プロジェクト累計ユニーク画面数（SQLite から取得）
      - 概算網羅率（前回既知 / 今回発見の割合）
    """
    sep = "=" * 62

    # SQLite からプロジェクト累計を取得
    total_unique = stats.screens_found  # フォールバック
    if sqlite_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(sqlite_db))
            row = conn.execute(
                """SELECT COUNT(DISTINCT s.fingerprint) AS cnt
                   FROM lc_screens s
                   JOIN lc_sessions sess ON sess.session_id = s.session_id
                   WHERE sess.game_title = ?""",
                (game_title,),
            ).fetchone()
            conn.close()
            if row:
                total_unique = int(row[0])
        except Exception:
            pass

    # 今セッションで新たに追加された画面数
    newly_found = stats.screens_found

    # 概算: 「既知 pHash でスキップされた数」÷「全接触画面」でどれだけ既知か
    skipped   = stats.screens_skipped
    contacted = newly_found + skipped
    coverage_pct = (
        f"{skipped / contacted * 100:.0f}% 既知" if contacted > 0 else "初回探索"
    )

    print(sep)
    print("  LudusCartographer — 探索完了")
    print(sep)
    print(f"  プロジェクト         : {game_title}")
    print(f"  今回の新規発見画面   : {newly_found} 画面")
    print(f"  累計ユニーク画面     : {total_unique} 画面")
    print(f"  既知画面スキップ     : {skipped} 画面  ({coverage_pct})")
    print(f"  タップ操作数         : {stats.taps_total} 回")
    print(f"  経過時間             : {stats.elapsed_sec:.1f} 秒")
    print(sep)


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

  # 探索後にブラウザで管理画面を自動表示
  python main.py --mirror --bundle com.example.mygame "MyGame" --open-web
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
        "--android",
        action="store_true",
        help=(
            "Android 実機モード (UiAutomator2) で起動。"
            " DEVICE_MODE=ANDROID を自動設定。--package と組み合わせて使用。"
        ),
    )
    parser.add_argument(
        "--package",
        metavar="PACKAGE_NAME",
        help="Android アプリのパッケージ名 (ANDROID_APP_PACKAGE 環境変数より優先)",
    )
    parser.add_argument(
        "--activity",
        metavar="ACTIVITY_NAME",
        help=(
            "Android アプリの起動アクティビティ名"
            " (デフォルト: com.google.firebase.MessagingUnityPlayerActivity)"
        ),
    )
    parser.add_argument(
        "--android-udid",
        metavar="UDID",
        dest="android_udid",
        help="Android デバイスシリアル (adb devices で確認、省略時は最初の接続端末)",
    )
    parser.add_argument(
        "--bundle",
        metavar="BUNDLE_ID",
        help="ターゲットアプリの Bundle ID (iOS 用、IOS_BUNDLE_ID 環境変数より優先)",
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
    parser.add_argument(
        "--tap-wait",
        type=float,
        metavar="SEC",
        default=None,
        help=(
            "タップ後の描画待機時間 秒 (デフォルト: 4.0 [MIRROR] / 3.0 [SIMULATOR])。"
            " ゲームのローディング画面が長い場合は 5.0〜6.0 を推奨。"
        ),
    )
    parser.add_argument(
        "--stuck-threshold",
        type=int,
        metavar="N",
        default=None,
        help=(
            "同一画面で N 回 dead-end になった後にスワイプを試みる閾値"
            " (デフォルト: 3 [MIRROR] / 3 [SIMULATOR])。"
        ),
    )
    parser.add_argument(
        "--knowledge-dir",
        metavar="PATH",
        default=None,
        help=(
            "知識ベースのベースディレクトリ (デフォルト: games/)。"
            " {PATH}/{game_title}/knowledge/ にキャッシュが保存される。"
            " 環境変数 KNOWLEDGE_BASE_DIR でも設定可能。"
        ),
    )
    parser.add_argument(
        "--open-web",
        action="store_true",
        help=(
            "探索完了後にブラウザで管理画面 (http://localhost:8080) を自動表示する。"
            " PHP ビルトインサーバーが起動していない場合は自動起動する。"
        ),
    )
    parser.add_argument(
        "--teacher-mode",
        action="store_true",
        dest="teacher_mode",
        help="未知の画面で人間に操作を教えてもらう Teacher Mode を有効化",
    )
    # 未知の引数（環境変数由来のフラグ等）を無視して続行
    args, _ = parser.parse_known_args()
    return args


def _open_web_dashboard(game_title: str) -> None:
    """
    管理画面 (http://localhost:8080) をブラウザで開く。

    PHP ビルトインサーバーが起動していない場合は起動してから開く。
    """
    import subprocess
    import time
    import urllib.request

    base_url = "http://localhost:8080"
    url = f"{base_url}/?game={game_title}"

    # サーバーが応答するか確認
    server_up = False
    try:
        urllib.request.urlopen(base_url, timeout=1)
        server_up = True
    except Exception:
        pass

    if not server_up:
        # PHP ビルトインサーバーを起動
        web_dir = Path(__file__).parent.parent / "web" / "public"
        if web_dir.exists():
            logger.info("[WEB] PHP ビルトインサーバーを起動します: %s", web_dir)
            subprocess.Popen(
                ["php", "-S", "localhost:8080", "-t", str(web_dir)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)  # 起動待機
        else:
            logger.warning("[WEB] web/public ディレクトリが見つかりません: %s", web_dir)

    logger.info("[WEB] ブラウザで管理画面を開きます: %s", url)
    webbrowser.open(url)


def main() -> None:
    # 起動直後: ディレクトリ保証 → logging 設定
    _ensure_directories()
    _configure_logging()

    args = _parse_args()

    # --mirror フラグ: 環境変数を上書きして MIRROR モードに切り替え
    if args.mirror:
        os.environ["DEVICE_MODE"]        = "MIRROR"
        os.environ["IOS_USE_SIMULATOR"]  = "0"
        logger.info(_MIRROR_SETUP_GUIDE)

    # --android フラグ: Android 実機モードに切り替え
    if args.android:
        os.environ["DEVICE_MODE"] = "ANDROID"
        # UDID: CLI引数 → adb devices の先頭デバイス
        udid = getattr(args, "android_udid", None) or os.environ.get("ANDROID_UDID", "")
        if not udid:
            import subprocess as _sp
            try:
                lines = _sp.check_output(["adb", "devices"], text=True).splitlines()
                for line in lines[1:]:
                    if "\tdevice" in line:
                        udid = line.split("\t")[0].strip()
                        break
            except Exception:
                pass
        if udid:
            os.environ["ANDROID_UDID"] = udid
        if args.package:
            os.environ["ANDROID_APP_PACKAGE"] = args.package
        if args.activity:
            os.environ["ANDROID_APP_ACTIVITY"] = args.activity

    # CLI 引数が環境変数より優先される
    if args.bundle:
        os.environ["IOS_BUNDLE_ID"] = args.bundle
    if args.duration is not None:
        os.environ["CRAWL_DURATION_SEC"] = str(args.duration)
    if args.depth:
        os.environ["CRAWL_MAX_DEPTH"] = str(args.depth)

    # モード判定
    is_android = os.environ.get("DEVICE_MODE", "").upper() == "ANDROID"

    if is_android:
        # Android モード: ANDROID_APP_PACKAGE が必須
        pkg = os.environ.get("ANDROID_APP_PACKAGE", "")
        if not pkg:
            logger.error("ANDROID_APP_PACKAGE が設定されていません。")
            logger.error("  例: python main.py --android --package com.aniplex.magia.exedra.jp")
            sys.exit(1)
        # iOS 用 bundle_id のダミーをセット (driver_factory が IOS_BUNDLE_ID を参照する場合の保険)
        os.environ.setdefault("IOS_BUNDLE_ID", pkg)
        bundle_id = pkg
    else:
        bundle_id = os.environ.get("IOS_BUNDLE_ID")
        if not bundle_id:
            logger.error("IOS_BUNDLE_ID が設定されていません。")
            if args.mirror:
                logger.error("  例: python main.py --mirror --bundle com.example.mygame")
            else:
                logger.error("  例: IOS_BUNDLE_ID=com.example.mygame python main.py")
            sys.exit(1)

    # is_mirror を先に確定してからゲームタイトルを決定する
    is_mirror = os.environ.get("DEVICE_MODE", "").upper() == "MIRROR"
    # is_android は上で既に確定済み

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

    sep = "=" * 62
    logger.info(sep)
    logger.info("  LudusCartographer — 自律クロール開始")
    logger.info(sep)
    logger.info("  ターゲット : %s", bundle_id)
    logger.info("  ゲーム名  : %s", game_title)
    logger.info("  モード    : %s", device_mode)
    logger.info("  最大時間  : %s 秒", duration)
    logger.info("  最大深さ  : %s", max_depth)
    logger.info("  DB 保存   : %s", f"有効 ({db_host})" if db_host else "無効 (DB_HOST 未設定)")
    logger.info(sep)

    # --- Driver セッション開始 ---
    from driver_adapter import BaseDriver
    from driver_factory import create_driver_session
    from lc.crawler import CrawlerConfig, ScreenCrawler

    _sqlite_db = Path(__file__).parent / "storage" / "ludus.db"

    # SQLite から今セッション前の既知 pHash 数を記録（サマリー用）
    known_before = 0
    if _sqlite_db.exists():
        try:
            import sqlite3 as _sl3
            _c = _sl3.connect(str(_sqlite_db))
            row = _c.execute(
                "SELECT COUNT(DISTINCT s.fingerprint) FROM lc_screens s"
                " JOIN lc_sessions sess ON sess.session_id = s.session_id"
                " WHERE sess.game_title = ?",
                (game_title,),
            ).fetchone()
            _c.close()
            known_before = int(row[0]) if row else 0
        except Exception:
            pass

    # ミラーモード向けデフォルト: ゲームのローディング時間を考慮して待機を長めに設定
    _default_tap_wait    = 4.0 if is_mirror else 3.0
    _default_stuck_thr   = 3   # ゲームロード考慮で統一デフォルト
    tap_wait         = args.tap_wait        if args.tap_wait        is not None else _default_tap_wait
    stuck_threshold  = args.stuck_threshold if args.stuck_threshold is not None else _default_stuck_thr

    knowledge_base_dir = (
        getattr(args, "knowledge_dir", None)
        or os.environ.get("KNOWLEDGE_BASE_DIR", "games")
    )

    teacher_mode = getattr(args, "teacher_mode", False) or os.environ.get("TEACHER_MODE") == "1"

    crawler_cfg = CrawlerConfig(
        game_title           = game_title,
        device_mode          = device_mode,
        max_duration_sec     = duration,
        max_depth            = max_depth,
        wait_after_tap       = tap_wait,
        anti_stuck_threshold = stuck_threshold,
        sqlite_db_path       = str(_sqlite_db) if _sqlite_db.exists() else None,
        db_host              = db_host,
        db_name              = os.environ.get("DB_NAME",     "ludus_cartographer"),
        db_user              = os.environ.get("DB_USER",     "root"),
        db_password          = os.environ.get("DB_PASSWORD", ""),
        knowledge_base_dir   = knowledge_base_dir,
        teacher_mode_enabled = teacher_mode,
    )
    logger.info(
        "  タップ待機: %.1f 秒  スタック閾値: %d 回",
        tap_wait, stuck_threshold,
    )
    logger.info("  知識ベース : %s", knowledge_base_dir)
    logger.info("  Teacher Mode: %s", "有効" if teacher_mode else "無効")

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
            logger.warning("ウィンドウ消失を検知しました: %s", exc)
            logger.warning("現時点のクロールデータを保存して終了します...")
            if crawler is not None:
                evidence_dir = Path(__file__).parent / "evidence"
                session_dirs = sorted(evidence_dir.glob("*/"))
                if session_dirs:
                    interrupted_path = session_dirs[-1] / "crawl_summary.json"
                    crawler.save_summary_json(interrupted_path)
                    logger.info("中断サマリーを保存: %s", interrupted_path)
                    stats = crawler._stats  # type: ignore[attr-defined]
                    logger.info(
                        "発見済み: %d 画面 / %d 回タップ",
                        stats.screens_found, stats.taps_total,
                    )
            return  # メイン処理を安全終了

        # セッションサマリー表示
        logger.info(crawler.summary())
        _print_session_summary(stats, game_title, _sqlite_db, known_before)

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
                logger.warning("要調査画面あり:")
                for s in gaps["unknown_screens"]:
                    logger.warning("  unknown: depth=%d  fp=%s", s.depth, s.fingerprint[:8])
            # Discovery Tree: JSON 保存 + ASCII 表示
            if crawler is not None:
                tree_json_path = summary_path.parent / "discovery_tree.json"
                crawler.save_discovery_tree(tree_json_path)
                print(crawler.render_discovery_tree())

        # --open-web: ブラウザで管理画面を自動表示
        if args.open_web:
            _open_web_dashboard(game_title)


if __name__ == "__main__":
    main()
