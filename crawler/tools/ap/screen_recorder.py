"""
スクリーン記録モジュール — auto_pilot のオプション機能。

auto_pilot の自律操縦中に通過するユニーク画面を SQLite に記録する。
設計書: docs/screen_recorder.md

使い方:
    auto_pilot.py -S   # スクリーン記録を有効化
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 定数 ────────────────────────────────────────────
_SKIP_SCENES = frozenset({"LOADING", "MOVIE"})
_MIN_CONFIDENCE = 0.3
_MIN_RECORD_INTERVAL = 5.0  # 秒 — バトル/ガチャ連写防止
_SORT_Y_BUCKET = 50         # Y座標のバケットサイズ (px)

# 日本語・英単語を含むトークンのみ採用
_HAS_TEXT_RE = re.compile(r"[\u3000-\u9fff\u30a0-\u30ffA-Za-z]")
# 純粋な数字 or 時刻パターン
_PURE_NUMBER_RE = re.compile(r"^[\d,.]+$")
_TIME_PATTERN_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")

# DB スキーマ (import_to_sqlite.py と同一)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS lc_projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_title TEXT    UNIQUE NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lc_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    UNIQUE NOT NULL,
    screens_found INTEGER DEFAULT 0,
    started_at    TEXT,
    status        TEXT    DEFAULT 'running',
    game_title    TEXT    DEFAULT 'Unknown Game',
    device_mode   TEXT    DEFAULT 'SIMULATOR',
    project_id    INTEGER
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


class ScreenRecorder:
    """auto_pilot 用スクリーン記録器。

    OCR テキストの正規化ハッシュで重複排除し、
    ユニーク画面のみ SQLite + ファイルに保存する。
    """

    def __init__(
        self,
        db_path: Path,
        storage_dir: Path,
        session_id: str,
        game_title: str = "Unknown Game",
    ) -> None:
        self._db_path = Path(db_path)
        self._storage_dir = Path(storage_dir) / session_id
        self._session_id = session_id
        self._game_title = game_title

        # ディレクトリ先行作成
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # SQLite 接続 (timeout=10 で BUSY 対策)
        self._conn = sqlite3.connect(str(self._db_path), timeout=10)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

        # セッション登録
        self._conn.execute(
            "INSERT OR IGNORE INTO lc_sessions"
            " (session_id, screens_found, started_at, status, game_title)"
            " VALUES (?, 0, ?, 'running', ?)",
            (self._session_id, datetime.now().isoformat(), self._game_title),
        )
        self._conn.commit()

        # 全セッション横断で既存 fingerprint をロード (蓄積型)
        rows = self._conn.execute(
            "SELECT fingerprint FROM lc_screens"
        ).fetchall()
        self._seen_fps: set[str] = {r[0] for r in rows}

        # 画面間リンク用
        self._last_recorded_fp: Optional[str] = None

        # インターバル制御
        self._last_record_time: float = 0.0

        # 統計
        self._recorded_count: int = 0

        logger.info(
            "[ScreenRecorder] 初期化: session=%s, 既存fp=%d件, storage=%s",
            session_id, len(self._seen_fps), self._storage_dir,
        )

    # ─── public API ───────────────────────────────────

    def maybe_record(
        self,
        analysis_path: Optional[Path],
        ocr_results: list[dict],
        scene: str,
        phash: str,
    ) -> bool:
        """ユニーク画面なら記録して True、スキップなら False を返す。"""

        # 1. シーンスキップ
        if scene in _SKIP_SCENES:
            return False

        # 2. OCR 空スキップ
        if not ocr_results:
            return False

        # 3. インターバル制御
        now = time.time()
        if now - self._last_record_time < _MIN_RECORD_INTERVAL:
            return False

        # 4. 正規化 + fingerprint
        normalized = self._normalize_ocr(ocr_results)
        if not normalized:
            return False
        content_fp = self._content_fingerprint(normalized)

        # 5. 重複チェック (全セッション横断)
        if content_fp in self._seen_fps:
            return False

        # 6. 新規画面 → 記録
        title = self._make_title(ocr_results)
        ocr_text = " ".join(
            item.get("text", "") for item in ocr_results
            if item.get("confidence", 0) >= _MIN_CONFIDENCE
        )

        # DB INSERT
        screen_id = self._insert_screen(
            fingerprint=content_fp,
            title=title,
            parent_fp=self._last_recorded_fp,
            phash=phash,
            screenshot_path="",  # Phase 2 で実装
            ocr_text=ocr_text,
        )
        self._insert_tappable_items(screen_id, ocr_results)

        # 状態更新
        self._seen_fps.add(content_fp)
        self._last_recorded_fp = content_fp
        self._last_record_time = now
        self._recorded_count += 1

        logger.info(
            "[ScreenRecorder] 新規画面 #%d: fp=%s title='%s' scene=%s",
            self._recorded_count, content_fp[:8], title[:30], scene,
        )
        return True

    def close(self) -> None:
        """セッションを完了状態にして DB 接続を閉じる。"""
        try:
            self._conn.execute(
                "UPDATE lc_sessions SET status = 'completed',"
                " screens_found = ? WHERE session_id = ?",
                (self._recorded_count, self._session_id),
            )
            self._conn.commit()
            logger.info(
                "[ScreenRecorder] セッション完了: %d 画面記録",
                self._recorded_count,
            )
        except Exception as e:
            logger.warning("[ScreenRecorder] close エラー: %s", e)
        finally:
            self._conn.close()

    # ─── 正規化・fingerprint ──────────────────────────

    @staticmethod
    def _normalize_ocr(ocr_results: list[dict]) -> str:
        """OCR 結果を正規化してハッシュ用文字列を生成する。

        - confidence >= 0.3 のみ
        - 日本語/英単語を含むトークンのみ
        - 純粋数字・時刻パターンを除外
        - (center_y // 50, center_x) でソート
        - テキストのみ | 結合 (座標はハッシュに含めない)
        """
        items: list[tuple[int, int, str]] = []
        for item in ocr_results:
            conf = item.get("confidence", 0)
            if conf < _MIN_CONFIDENCE:
                continue
            text = item.get("text", "").strip()
            if not text:
                continue
            # 日本語/英単語を含まないトークンは除外
            if not _HAS_TEXT_RE.search(text):
                continue
            # 純粋数字・時刻パターンは除外
            if _PURE_NUMBER_RE.match(text) or _TIME_PATTERN_RE.match(text):
                continue
            # テキスト内の数字を除去 (例: "Turn 3" → "Turn")
            text = re.sub(r"\d+", "", text).strip()
            if not text:
                continue
            # ソート用座標
            center = item.get("center", [0, 0])
            sort_y = int(center[1]) // _SORT_Y_BUCKET
            sort_x = int(center[0])
            items.append((sort_y, sort_x, text))

        items.sort()
        return "|".join(text for _, _, text in items)

    @staticmethod
    def _content_fingerprint(normalized: str) -> str:
        """正規化テキストから SHA-256 先頭16文字の fingerprint を生成。"""
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _make_title(ocr_results: list[dict]) -> str:
        """高信頼度テキスト上位3つからタイトルを生成。"""
        candidates = [
            (item.get("confidence", 0), item.get("text", "").strip())
            for item in ocr_results
            if item.get("confidence", 0) >= _MIN_CONFIDENCE
            and item.get("text", "").strip()
            and _HAS_TEXT_RE.search(item.get("text", ""))
            and not _PURE_NUMBER_RE.match(item.get("text", "").strip())
        ]
        candidates.sort(key=lambda x: -x[0])
        top_texts = [text for _, text in candidates[:3]]
        return " / ".join(top_texts) if top_texts else "Unknown"

    # ─── DB 操作 ──────────────────────────────────────

    def _insert_screen(
        self,
        fingerprint: str,
        title: str,
        parent_fp: Optional[str],
        phash: str,
        screenshot_path: str,
        ocr_text: str,
    ) -> int:
        """lc_screens に INSERT し、生成された ID を返す。"""
        cur = self._conn.execute(
            "INSERT INTO lc_screens"
            " (session_id, fingerprint, title, depth, parent_fp,"
            "  phash, screenshot_path, ocr_text, discovered_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                fingerprint,
                title,
                parent_fp,
                phash,
                screenshot_path,
                ocr_text,
                datetime.now().isoformat(),
            ),
        )
        self._conn.commit()

        # セッションの screens_found を更新
        self._conn.execute(
            "UPDATE lc_sessions SET screens_found = screens_found + 1"
            " WHERE session_id = ?",
            (self._session_id,),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def _insert_tappable_items(
        self, screen_id: int, ocr_results: list[dict]
    ) -> None:
        """OCR アイテムを lc_tappable_items に INSERT。"""
        rows = [
            (screen_id, item.get("text", "").strip(), item.get("confidence", 0))
            for item in ocr_results
            if item.get("confidence", 0) >= _MIN_CONFIDENCE
            and item.get("text", "").strip()
        ]
        if rows:
            self._conn.executemany(
                "INSERT INTO lc_tappable_items (screen_id, text, confidence)"
                " VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()
