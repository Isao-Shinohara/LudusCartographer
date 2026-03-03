#!/usr/bin/env python3
"""
LudusCartographer — クローラー エントリポイント

環境変数を読み込み、iOS Simulator / 実機でアプリを自動探索する。

【使い方】
  # iOS Simulator
  IOS_USE_SIMULATOR=1 IOS_BUNDLE_ID=com.example.mygame python main.py

  # 実機 (UDID 自動検出)
  IOS_BUNDLE_ID=com.example.mygame python main.py

  # 探索パラメータ調整
  CRAWL_DURATION_SEC=300 CRAWL_MAX_DEPTH=3 IOS_USE_SIMULATOR=1 \\
    IOS_BUNDLE_ID=com.apple.Preferences python main.py

【環境変数】
  IOS_BUNDLE_ID         ターゲットアプリの Bundle ID（必須）
  GAME_TITLE            ゲームタイトル（省略可 — 未設定時は IOS_BUNDLE_ID を使用）
  IOS_USE_SIMULATOR     "1" でシミュレータモード
  IOS_SIMULATOR_UDID    シミュレータ UDID（省略可 — 自動選択）
  IOS_UDID              実機 UDID（省略可 — 自動検出）
  CRAWL_DURATION_SEC    クロール最大時間 秒 (デフォルト: 180)
  CRAWL_MAX_DEPTH       DFS 最大深さ (デフォルト: 3)
  DB_HOST               MySQL ホスト（省略可 — 未設定時は DB 保存スキップ）
  DB_NAME               MySQL DB 名 (デフォルト: ludus_cartographer)
  DB_USER               MySQL ユーザー (デフォルト: root)
  DB_PASSWORD           MySQL パスワード
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / "config" / ".env")


def main() -> None:
    bundle_id = os.environ.get("IOS_BUNDLE_ID")
    if not bundle_id:
        print("ERROR: IOS_BUNDLE_ID が設定されていません。")
        print("  例: IOS_BUNDLE_ID=com.example.mygame python main.py")
        sys.exit(1)

    game_title = os.environ.get("GAME_TITLE", bundle_id)  # 未設定時は bundle_id をタイトルとして使う
    duration   = int(os.environ.get("CRAWL_DURATION_SEC", "180"))
    max_depth  = int(os.environ.get("CRAWL_MAX_DEPTH",    "3"))
    db_host    = os.environ.get("DB_HOST", "")

    print("=" * 60)
    print("  LudusCartographer — 自律クロール開始")
    print("=" * 60)
    print(f"  ターゲット : {bundle_id}")
    print(f"  ゲーム名  : {game_title}")
    print(f"  最大時間  : {duration} 秒")
    print(f"  最大深さ  : {max_depth}")
    print(f"  DB 保存   : {'有効 (' + db_host + ')' if db_host else '無効 (DB_HOST 未設定)'}")
    print("=" * 60)

    # --- Appium セッション開始 ---
    from lc.capabilities import simulator_config_from_env
    from lc.crawler import CrawlerConfig, ScreenCrawler
    from lc.driver import ios_simulator_session
    from lc.ocr import find_best, run_ocr

    crawler_cfg = CrawlerConfig(
        game_title       = game_title,
        max_duration_sec = duration,
        max_depth        = max_depth,
        db_host          = db_host,
        db_name          = os.environ.get("DB_NAME",     "ludus_cartographer"),
        db_user          = os.environ.get("DB_USER",     "root"),
        db_password      = os.environ.get("DB_PASSWORD", ""),
    )

    sim_cfg = simulator_config_from_env()

    with ios_simulator_session(sim_cfg) as driver:
        # モーダルダイアログを解除（起動直後のアラート等）
        for _ in range(3):
            if not driver.dismiss_any_modal():
                break
            driver.wait(1.0)

        # クローラー実行
        crawler = ScreenCrawler(driver, crawler_cfg)
        stats   = crawler.crawl(depth=0, parent_fp=None)

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
