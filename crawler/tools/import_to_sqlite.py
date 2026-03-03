"""
crawl_summary.json を SQLite データベースに取り込む。

使い方:
  venv/bin/python tools/import_to_sqlite.py
  venv/bin/python tools/import_to_sqlite.py --evidence evidence --db storage/ludus.db
  venv/bin/python tools/import_to_sqlite.py --game-title "マイゲーム"
  venv/bin/python tools/import_to_sqlite.py --seed        # テストデータを投入
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lc_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    UNIQUE NOT NULL,
    screens_found INTEGER DEFAULT 0,
    started_at    TEXT,
    status        TEXT    DEFAULT 'completed',
    game_title    TEXT    DEFAULT 'Unknown Game'
);

CREATE TABLE IF NOT EXISTS lc_screens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    fingerprint     TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    depth           INTEGER DEFAULT 0,
    parent_fp       TEXT,
    phash           TEXT,
    screenshot_path TEXT,
    ocr_text        TEXT,
    discovered_at   TEXT
);

CREATE TABLE IF NOT EXISTS lc_tappable_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id  INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    confidence REAL    DEFAULT 0
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    """既存 DB に不足カラムを追加するマイグレーション。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(lc_sessions)")]
    if "game_title" not in cols:
        conn.execute("ALTER TABLE lc_sessions ADD COLUMN game_title TEXT DEFAULT 'Unknown Game'")
        conn.execute(
            "UPDATE lc_sessions SET game_title = 'iOS設定'"
            " WHERE game_title IS NULL OR game_title = 'Unknown Game'"
        )
        conn.commit()
        print("  [migrate] game_title カラムを追加 → 既存セッションを 'iOS設定' に設定")


def seed_test_games(conn: sqlite3.Connection) -> None:
    """異なる game_title を持つテストデータを投入する（重複スキップ）。"""
    cur = conn.cursor()

    # --- カレンダーアプリ ---
    cal_sid = "test_calendar_001"
    cur.execute(
        "INSERT OR IGNORE INTO lc_sessions"
        " (session_id, screens_found, started_at, status, game_title)"
        " VALUES (?,?,?,?,?)",
        (cal_sid, 3, "2026-03-04T10:00:00", "completed", "カレンダー"),
    )
    for fp, title, depth, parent, ocr in [
        ("cal_fp_001", "カレンダー",    0, None,        "予定 今日 明日 週表示 月表示"),
        ("cal_fp_002", "イベント詳細", 1, "cal_fp_001", "タイトル 日時 場所 メモ 削除"),
        ("cal_fp_003", "イベント追加", 1, "cal_fp_001", "新規イベント タイトル 開始 終了 繰り返し 保存"),
    ]:
        cur.execute(
            "SELECT id FROM lc_screens WHERE session_id=? AND fingerprint=?", (cal_sid, fp)
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO lc_screens"
                " (session_id, fingerprint, title, depth, parent_fp, ocr_text, discovered_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (cal_sid, fp, title, depth, parent, ocr, "2026-03-04T10:00:00"),
            )

    # --- マップアプリ ---
    map_sid = "test_maps_001"
    cur.execute(
        "INSERT OR IGNORE INTO lc_sessions"
        " (session_id, screens_found, started_at, status, game_title)"
        " VALUES (?,?,?,?,?)",
        (map_sid, 2, "2026-03-04T11:00:00", "completed", "マップ"),
    )
    for fp, title, depth, parent, ocr in [
        ("map_fp_001", "マップ",    0, None,        "現在地 検索 経路 お気に入り ルート"),
        ("map_fp_002", "経路案内", 1, "map_fp_001", "出発地 目的地 徒歩 車 電車 開始"),
    ]:
        cur.execute(
            "SELECT id FROM lc_screens WHERE session_id=? AND fingerprint=?", (map_sid, fp)
        )
        if not cur.fetchone():
            cur.execute(
                "INSERT INTO lc_screens"
                " (session_id, fingerprint, title, depth, parent_fp, ocr_text, discovered_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (map_sid, fp, title, depth, parent, ocr, "2026-03-04T11:00:00"),
            )

    conn.commit()
    print("  [seed] カレンダー (3 screens) + マップ (2 screens) を投入")


def import_session(conn: sqlite3.Connection, summary_path: Path, game_title: str = "") -> int:
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    session_id = data["session_id"]
    screens = data.get("screens", [])

    # game_title の優先順位: 引数 > JSON > "Unknown Game"
    if not game_title:
        game_title = data.get("game_title", "Unknown Game")

    cur = conn.cursor()
    started_at = screens[0]["discovered_at"] if screens else None

    cur.execute(
        "INSERT OR IGNORE INTO lc_sessions (session_id, screens_found, started_at, game_title)"
        " VALUES (?,?,?,?)",
        (session_id, len(screens), started_at, game_title),
    )

    imported = 0
    for screen in screens:
        items = screen.get("tappable_items", [])
        ocr_text = " ".join(item["text"] for item in items)

        cur.execute(
            "SELECT id FROM lc_screens WHERE session_id=? AND fingerprint=?",
            (session_id, screen["fingerprint"]),
        )
        if cur.fetchone():
            continue

        cur.execute(
            """INSERT INTO lc_screens
               (session_id, fingerprint, title, depth, parent_fp, phash,
                screenshot_path, ocr_text, discovered_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                screen["fingerprint"],
                screen["title"],
                screen.get("depth", 0),
                screen.get("parent_fp"),
                screen.get("phash"),
                screen.get("screenshot_path"),
                ocr_text,
                screen.get("discovered_at"),
            ),
        )
        screen_id = cur.lastrowid
        for item in items:
            cur.execute(
                "INSERT INTO lc_tappable_items (screen_id, text, confidence)"
                " VALUES (?,?,?)",
                (screen_id, item["text"], item.get("confidence", 0.0)),
            )
        imported += 1

    conn.commit()
    return imported


def main() -> None:
    parser = argparse.ArgumentParser(description="crawl_summary.json → SQLite importer")
    default_evidence = str(Path(__file__).parent.parent / "evidence")
    default_db       = str(Path(__file__).parent.parent / "storage" / "ludus.db")
    parser.add_argument("--evidence",   default=default_evidence)
    parser.add_argument("--db",         default=default_db)
    parser.add_argument("--game-title", default="",
                        help="クロール対象ゲームのタイトル (例: 'iOS設定')。省略時は crawl_summary.json から自動取得")
    parser.add_argument("--seed",       action="store_true",
                        help="テスト用ゲームデータを投入する")
    args = parser.parse_args()

    evidence_dir = Path(args.evidence)
    db_path      = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)

    # マイグレーション（既存 DB への game_title 追加）
    migrate(conn)

    # crawl_summary.json を取り込む
    total = 0
    for summary_path in sorted(evidence_dir.glob("*/crawl_summary.json")):
        n = import_session(conn, summary_path, args.game_title)
        print(f"  [{summary_path.parent.name}] {n} screens imported")
        total += n

    # テストデータの投入
    if args.seed:
        seed_test_games(conn)

    conn.close()
    print(f"\nDone: {total} screens total → {db_path}")


if __name__ == "__main__":
    main()
