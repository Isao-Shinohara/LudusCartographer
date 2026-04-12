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
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ─── 定数 ────────────────────────────────────────────
_SKIP_SCENES = frozenset({"LOADING"})
_MIN_CONFIDENCE = 0.3
_SORT_Y_BUCKET = 50         # Y座標のバケットサイズ (px)
_CASCADE_MIN_FACE = (40, 40)  # 顔検出の最小サイズ (px)
_CASCADE_XML = Path(__file__).parent.parent.parent / "assets" / "lbpcascade_animeface.xml"
_SCENE_CHANGE_PHASH_DIST = 20  # シーン切り替わり判定の phash 距離閾値
_MIN_BRIGHTNESS = 30            # シーン切り替わり記録の最低輝度 (暗転除外)

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

CREATE TABLE IF NOT EXISTS lc_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    from_screen_id  INTEGER NOT NULL,
    to_screen_id    INTEGER,
    from_fp         TEXT    NOT NULL,
    to_fp           TEXT,
    tap_x           INTEGER,
    tap_y           INTEGER,
    tap_label       TEXT,
    action_name     TEXT,
    discovered_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_trans_from ON lc_transitions(from_fp);
CREATE INDEX IF NOT EXISTS idx_trans_to ON lc_transitions(to_fp);
CREATE INDEX IF NOT EXISTS idx_trans_session ON lc_transitions(session_id);
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
        self._migrate()

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
        self._last_recorded_phash: str = ""
        self._last_inserted_id: Optional[int] = None

        # 遷移グラフ: タップ→次画面の非同期記録
        self._pending_transition: Optional[dict] = None
        # 前セッションの未完了遷移をクリーンアップ (クラッシュ復帰時のゴミ防止)
        try:
            self._conn.execute(
                "DELETE FROM lc_transitions"
                " WHERE to_screen_id IS NULL AND session_id != ?",
                (self._session_id,),
            )
            self._conn.commit()
        except Exception:
            pass  # lc_transitions テーブルが未作成の場合

        # 顔検出 (lbpcascade_animeface)
        self._cascade: Optional[cv2.CascadeClassifier] = None
        if _CASCADE_XML.exists():
            self._cascade = cv2.CascadeClassifier(str(_CASCADE_XML))
            logger.info("[ScreenRecorder] 顔検出カスケード読込: %s", _CASCADE_XML.name)
        else:
            logger.warning("[ScreenRecorder] カスケードファイル未検出: %s", _CASCADE_XML)

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
        force: bool = False,
    ) -> bool:
        """寛容撮影: 暗転以外は全部保存。間引きはバッチで行う。

        force=True: タップ直前の強制保存。暗転・シーンチェック・重複チェックを
        すべてスキップし、必ず保存する。
        """

        if not force:
            # 1. シーンスキップ
            if scene in _SKIP_SCENES:
                return False

            # 2. 暗転スキップ
            if analysis_path and Path(analysis_path).exists():
                _img = cv2.imread(str(analysis_path))
                if _img is not None:
                    _brightness = np.mean(cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY))
                    if _brightness <= _MIN_BRIGHTNESS:
                        return False

        # 3. fingerprint 生成
        if force:
            # force (タップ記録): phash 優先 (OCR は古い可能性がある)
            if phash:
                content_fp = f"ph_{phash[:14]}"
            else:
                content_fp = f"force_{time.time()}"
        else:
            # 通常記録: テキスト優先
            normalized = self._normalize_ocr(ocr_results) if ocr_results else ""
            if normalized:
                content_fp = self._content_fingerprint(normalized)
            elif phash:
                content_fp = f"ph_{phash[:14]}"
            else:
                return False

        # 4. 重複チェック
        # force: 直前と同じ phash fp ならスキップ (同画面連打の防止)
        if content_fp in self._seen_fps:
            if not force:
                return False
            if content_fp == self._last_recorded_fp:
                return False

        # 5. 新規画面 → 記録
        title = self._make_title(ocr_results) if ocr_results else f"({scene})"
        ocr_text = " ".join(
            item.get("text", "") for item in ocr_results
            if item.get("confidence", 0) >= _MIN_CONFIDENCE
        ) if ocr_results else ""

        # 画像保存
        screenshot_path, thumbnail_path = "", ""
        if analysis_path and Path(analysis_path).exists():
            screenshot_path, thumbnail_path = self._save_screenshot(
                Path(analysis_path), content_fp
            )

        # DB INSERT
        screen_id = self._insert_screen(
            fingerprint=content_fp,
            title=title,
            parent_fp=self._last_recorded_fp,
            phash=phash,
            screenshot_path=screenshot_path,
            thumbnail_path=thumbnail_path,
            ocr_text=ocr_text,
            scene=scene,
        )
        self._insert_tappable_items(screen_id, ocr_results)

        # 状態更新
        self._seen_fps.add(content_fp)
        self._last_recorded_fp = content_fp
        self._last_recorded_phash = phash
        self._last_record_time = time.time()
        self._recorded_count += 1

        # 遷移グラフ: pending があれば to を確定
        if self._pending_transition is not None:
            self._pending_transition["to_screen_id"] = screen_id
            self._pending_transition["to_fp"] = content_fp
            self._insert_transition(self._pending_transition)
            self._pending_transition = None

        logger.info(
            "[ScreenRecorder] 新規画面 #%d: fp=%s title='%s' scene=%s",
            self._recorded_count, content_fp[:8], title[:30], scene,
        )
        return True

    def record_tap(
        self,
        from_screen_id: int,
        from_fp: str,
        tap_x: int,
        tap_y: int,
        tap_label: str,
        action_name: str,
    ) -> None:
        """タップ操作を遷移として記録する。to は次の maybe_record() で確定する。"""
        # 既存の pending があれば to=NULL でフラッシュ (連続タップ対策)
        if self._pending_transition is not None:
            self._insert_transition(self._pending_transition)

        self._pending_transition = {
            "session_id": self._session_id,
            "from_screen_id": from_screen_id,
            "to_screen_id": None,
            "from_fp": from_fp,
            "to_fp": None,
            "tap_x": tap_x,
            "tap_y": tap_y,
            "tap_label": tap_label,
            "action_name": action_name,
            "discovered_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _resolve_tap_label(
        tap_x: int, tap_y: int, ocr_results: list[dict],
    ) -> str:
        """タップ座標から半径 50px 以内の最近傍 OCR テキストを返す。"""
        best_text = ""
        best_dist = 50.0  # 半径 50px
        for item in ocr_results:
            conf = item.get("confidence", 0)
            if conf < _MIN_CONFIDENCE:
                continue
            text = item.get("text", "").strip()
            if not text:
                continue
            cx, cy = item.get("center", [0, 0])
            dist = ((cx - tap_x) ** 2 + (cy - tap_y) ** 2) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best_text = text
        return best_text

    def close(self) -> None:
        """セッションを完了状態にして DB 接続を閉じる。"""
        try:
            # pending transition をフラッシュ (to=NULL)
            if self._pending_transition is not None:
                self._insert_transition(self._pending_transition)
                self._pending_transition = None
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

    # ─── マイグレーション ──────────────────────────────

    def _migrate(self) -> None:
        """既存 DB に不足カラムを追加する。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_screens)")}
        if "thumbnail_path" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN thumbnail_path TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: thumbnail_path カラム追加")
        if "scene" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN scene TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: scene カラム追加")

    # ─── 画像保存 ─────────────────────────────────────

    _WEBP_QUALITY = 80
    _THUMB_WIDTH = 320
    _THUMB_QUALITY = 60

    def _save_screenshot(
        self, analysis_path: Path, fingerprint: str
    ) -> tuple[str, str]:
        """WebP フルサイズ + サムネイルを保存し、(screenshot_path, thumbnail_path) を返す。"""
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        img = cv2.imread(str(analysis_path))
        if img is None:
            logger.warning("[ScreenRecorder] 画像読込失敗: %s", analysis_path)
            return "", ""

        # フルサイズ WebP
        full_path = self._storage_dir / f"{fingerprint}.webp"
        ok, buf = cv2.imencode(
            ".webp", img, [cv2.IMWRITE_WEBP_QUALITY, self._WEBP_QUALITY]
        )
        if ok:
            full_path.write_bytes(buf.tobytes())
        else:
            logger.warning("[ScreenRecorder] WebP エンコード失敗")
            return "", ""

        # サムネイル (幅 320px, アスペクト比維持)
        h, w = img.shape[:2]
        thumb_h = int(h * self._THUMB_WIDTH / w)
        thumb = cv2.resize(img, (self._THUMB_WIDTH, thumb_h), interpolation=cv2.INTER_AREA)
        thumb_path = self._storage_dir / f"{fingerprint}_thumb.webp"
        ok_t, buf_t = cv2.imencode(
            ".webp", thumb, [cv2.IMWRITE_WEBP_QUALITY, self._THUMB_QUALITY]
        )
        if ok_t:
            thumb_path.write_bytes(buf_t.tobytes())

        return str(full_path), str(thumb_path)

    # ─── 顔検出 ──────────────────────────────────────

    def _detect_face(self, img_path: Path) -> bool:
        """lbpcascade_animeface で顔を検出する。"""
        if self._cascade is None:
            return False
        try:
            img = cv2.imread(str(img_path))
            if img is None:
                return False
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = self._cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5,
                minSize=_CASCADE_MIN_FACE,
            )
            return len(faces) > 0
        except Exception:
            return False

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
        thumbnail_path: str,
        ocr_text: str,
        scene: str = "",
    ) -> int:
        """lc_screens に INSERT し、生成された ID を返す。"""
        cur = self._conn.execute(
            "INSERT INTO lc_screens"
            " (session_id, fingerprint, title, depth, parent_fp,"
            "  phash, screenshot_path, thumbnail_path, ocr_text, scene, discovered_at)"
            " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._session_id,
                fingerprint,
                title,
                parent_fp,
                phash,
                screenshot_path,
                thumbnail_path,
                ocr_text,
                scene,
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
        self._last_inserted_id = cur.lastrowid
        return cur.lastrowid  # type: ignore[return-value]

    def _insert_transition(self, t: dict) -> None:
        """lc_transitions に遷移レコードを INSERT する。"""
        try:
            self._conn.execute(
                "INSERT INTO lc_transitions"
                " (session_id, from_screen_id, to_screen_id, from_fp, to_fp,"
                "  tap_x, tap_y, tap_label, action_name, discovered_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    t["session_id"], t["from_screen_id"], t["to_screen_id"],
                    t["from_fp"], t["to_fp"],
                    t["tap_x"], t["tap_y"], t["tap_label"], t["action_name"],
                    t["discovered_at"],
                ),
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("[ScreenRecorder] transition INSERT エラー: %s", e)

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
