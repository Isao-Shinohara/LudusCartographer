"""
crawl_summary.json を SQLite データベースに取り込む。

使い方:
  venv/bin/python tools/import_to_sqlite.py
  venv/bin/python tools/import_to_sqlite.py --evidence evidence --db storage/ludus.db
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS lc_sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT    UNIQUE NOT NULL,
    screens_found INTEGER DEFAULT 0,
    started_at   TEXT,
    status       TEXT    DEFAULT 'completed'
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


def import_session(conn: sqlite3.Connection, summary_path: Path) -> int:
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    session_id = data["session_id"]
    screens = data.get("screens", [])

    cur = conn.cursor()
    started_at = screens[0]["discovered_at"] if screens else None

    cur.execute(
        "INSERT OR IGNORE INTO lc_sessions (session_id, screens_found, started_at)"
        " VALUES (?,?,?)",
        (session_id, len(screens), started_at),
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
    parser.add_argument("--evidence", default=default_evidence)
    parser.add_argument("--db",       default=default_db)
    args = parser.parse_args()

    evidence_dir = Path(args.evidence)
    db_path      = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)

    total = 0
    for summary_path in sorted(evidence_dir.glob("*/crawl_summary.json")):
        n = import_session(conn, summary_path)
        print(f"  [{summary_path.parent.name}] {n} screens imported")
        total += n

    conn.close()
    print(f"\nDone: {total} screens total → {db_path}")


if __name__ == "__main__":
    main()
