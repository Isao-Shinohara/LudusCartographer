"""
copy_to_clipboard.py — クリップボード連携ユーティリティ

【使い方】
  # テキストを直接コピー
  python tools/copy_to_clipboard.py "コピーしたいテキスト"

  # 標準入力からコピー
  echo "hello" | python tools/copy_to_clipboard.py

  # 最新のクロールログをコピー（末尾 50 行）
  python tools/copy_to_clipboard.py --last-log

  # 最新の discovery_report.md をコピー
  python tools/copy_to_clipboard.py --last-report

  # Gemini 報告用フォーマットを生成してコピー
  python tools/copy_to_clipboard.py --gemini

  # game_slug を指定して Gemini 報告
  python tools/copy_to_clipboard.py --gemini --slug madodora
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_CRAWLER_ROOT = Path(__file__).parent.parent


# ============================================================
# クリップボード操作
# ============================================================

def copy(text: str) -> None:
    """テキストを OS のクリップボードにコピーする。"""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
        else:
            # Linux: xclip を試してから xsel にフォールバック
            for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"), check=True)
                    break
                except FileNotFoundError:
                    continue
            else:
                raise RuntimeError("xclip / xsel が見つかりません。`sudo apt install xclip` で導入してください。")
        char_count = len(text)
        lines      = text.count("\n") + 1
        print(f"✅ クリップボードにコピーしました ({lines} 行 / {char_count} 文字)", file=sys.stderr)
    except Exception as e:
        print(f"❌ コピー失敗: {e}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# コンテンツ生成
# ============================================================

def _latest_session_dir() -> Path | None:
    """最新の evidence セッションディレクトリを返す。"""
    evidence = _CRAWLER_ROOT / "evidence"
    dirs = sorted(evidence.glob("*/"), key=lambda p: p.name)
    return dirs[-1] if dirs else None


def _load_last_log(lines: int = 50) -> str:
    """logs/crawler.log の末尾 N 行を返す。"""
    log_file = _CRAWLER_ROOT / "logs" / "crawler.log"
    if not log_file.exists():
        return "(ログファイルが見つかりません: logs/crawler.log)"
    all_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = all_lines[-lines:]
    header = f"=== logs/crawler.log (末尾 {len(tail)} 行 / 全 {len(all_lines)} 行) ===\n"
    return header + "\n".join(tail)


def _load_last_report() -> str:
    """最新セッションの discovery_report.md を返す。"""
    session = _latest_session_dir()
    if session is None:
        return "(evidence/ ディレクトリにセッションが見つかりません)"
    report = session / "discovery_report.md"
    if not report.exists():
        return f"(discovery_report.md が見つかりません: {session})"
    return report.read_text(encoding="utf-8")


def _build_gemini_report(slug: str | None = None) -> str:
    """Gemini への報告用テキストを生成する。"""
    now  = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = []

    # --- ヘッダー ---
    lines += [
        "## LudusCartographer — 探索レポート (Gemini 報告用)",
        "",
        f"**生成日時**: {now}",
    ]

    # --- ゲームプロファイル ---
    profiles_path = _CRAWLER_ROOT / "config" / "game_profiles.json"
    if profiles_path.exists():
        try:
            profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
            if slug:
                # slug から逆引き
                for title, p in profiles.items():
                    if p.get("slug") == slug:
                        lines.append(f"**ゲーム**: {title} (`{slug}`)")
                        break
            else:
                titles = list(profiles.keys())
                lines.append(f"**登録ゲーム**: {', '.join(titles)}")
        except Exception:
            pass

    # --- 最新セッション情報 ---
    session = _latest_session_dir()
    if session:
        lines += ["", f"**最新セッション**: `{session.name}`"]
        summary_path = session / "crawl_summary.json"
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
                screens = summary.get("screens", [])
                game    = summary.get("game_title", "-")
                lines += [
                    f"**ゲーム**: {game}",
                    f"**発見画面数**: {len(screens)} 画面",
                ]
                if screens:
                    lines += ["", "### 発見画面一覧", ""]
                    for i, s in enumerate(screens, 1):
                        fp    = s.get("fingerprint", "")[:8]
                        title = s.get("title", "unknown")
                        depth = s.get("depth", 0)
                        lines.append(f"{i}. `[{fp}]` depth={depth}  **{title}**")
            except Exception as e:
                lines.append(f"(crawl_summary.json 読み込みエラー: {e})")

    # --- knowledge/{slug}/ 行動ルール ---
    target_slug = slug
    if not target_slug and profiles_path.exists():
        try:
            all_profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
            if all_profiles:
                target_slug = next(iter(all_profiles.values())).get("slug")
        except Exception:
            pass

    if target_slug:
        rules_path = _CRAWLER_ROOT / "knowledge" / target_slug / "behavior_rules.json"
        if rules_path.exists():
            try:
                rules_data = json.loads(rules_path.read_text(encoding="utf-8"))
                rules = rules_data.get("rules", [])
                if rules:
                    lines += ["", "### 登録済み行動ルール", ""]
                    for r in rules:
                        lines.append(f"- **{r['id']}**: {r.get('description', '-')}")
                        lines.append(f"  - トリガー: `{json.dumps(r.get('trigger', {}), ensure_ascii=False)}`")
                        lines.append(f"  - 習得日: {r.get('learned_at', '-')}  成功回数: {r.get('success_count', 0)}")
            except Exception:
                pass

    # --- 最新ログ末尾 ---
    lines += ["", "### 最新ログ (末尾 20 行)", "```"]
    log_file = _CRAWLER_ROOT / "logs" / "crawler.log"
    if log_file.exists():
        tail = log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        lines.extend(tail)
    else:
        lines.append("(ログなし)")
    lines += ["```", "", "---", "_Generated by LudusCartographer copy_to_clipboard.py_"]

    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="クリップボード連携ユーティリティ",
    )
    parser.add_argument(
        "text",
        nargs="?",
        default=None,
        help="コピーするテキスト（省略時は標準入力から読む）",
    )
    parser.add_argument(
        "--last-log",
        action="store_true",
        help="logs/crawler.log の末尾 50 行をコピー",
    )
    parser.add_argument(
        "--log-lines",
        type=int,
        default=50,
        metavar="N",
        help="--last-log で取得する行数 (デフォルト: 50)",
    )
    parser.add_argument(
        "--last-report",
        action="store_true",
        help="最新セッションの discovery_report.md をコピー",
    )
    parser.add_argument(
        "--gemini",
        action="store_true",
        help="Gemini 報告用フォーマットを生成してコピー",
    )
    parser.add_argument(
        "--slug",
        default=None,
        metavar="SLUG",
        help="ゲームスラグ (例: madodora) — --gemini と組み合わせて使用",
    )
    args = parser.parse_args()

    if args.last_log:
        text = _load_last_log(args.log_lines)
    elif args.last_report:
        text = _load_last_report()
    elif args.gemini:
        text = _build_gemini_report(args.slug)
    elif args.text:
        text = args.text
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        parser.print_help()
        sys.exit(0)

    # 標準出力に内容を表示してからコピー
    print(text)
    print(file=sys.stderr)
    copy(text)


if __name__ == "__main__":
    main()
