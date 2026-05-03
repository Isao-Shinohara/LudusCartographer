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
_SKIP_SCENES = frozenset({"LOADING", "STARTUP"})
_MIN_CONFIDENCE = 0.3
_SORT_Y_BUCKET = 50         # Y座標のバケットサイズ (px)
_CASCADE_MIN_FACE = (40, 40)  # 顔検出の最小サイズ (px)
_CASCADE_XML = Path(__file__).parent.parent.parent / "assets" / "lbpcascade_animeface.xml"
_SCENE_CHANGE_PHASH_DIST = 20  # シーン切り替わり判定の phash 距離閾値
_DARK_MAX = 70       # 最も明るいピクセル(max)≦この値 → 全ピクセル暗い → 除外
_BRIGHT_MIN = 180    # 白画面判定の輝度閾値
_BRIGHT_RATIO = 0.999  # この割合以上のピクセルが _BRIGHT_MIN 以上なら白画面

def _is_too_dark_or_bright(gray: "np.ndarray") -> bool:
    """全ピクセル暗い/明るいかを判定。
    暗画面: max <= _DARK_MAX
    白画面: 以下のいずれかに該当
      1. 全ピクセルの99.9%以上が _BRIGHT_MIN 以上（従来ロジック）
      2. 黒ピクセル(<15)を除外した残りの最小値が _BRIGHT_MIN 以上（黒帯対応）
    """
    if int(np.max(gray)) <= _DARK_MAX:
        return True
    # 白画面判定1: 全体の割合ベース
    bright_ratio = np.count_nonzero(gray >= _BRIGHT_MIN) / gray.size
    if bright_ratio >= _BRIGHT_RATIO:
        return True
    # 白画面判定2: 黒ピクセル除外後の最小値ベース（黒帯があっても検出可能）
    non_black = gray[gray >= 15]
    if len(non_black) > 0 and int(np.min(non_black)) >= _BRIGHT_MIN:
        return True
    return False

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

CREATE TABLE IF NOT EXISTS lc_versions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    UNIQUE NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    is_active  INTEGER DEFAULT 0,
    is_deleted INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lc_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    UNIQUE NOT NULL,
    screens_found   INTEGER DEFAULT 0,
    started_at      TEXT,
    status          TEXT    DEFAULT 'running',
    completion_type TEXT,   -- NULL/goal_reached/manual_stop/orphaned
    game_title      TEXT    DEFAULT 'Unknown Game',
    device_mode     TEXT    DEFAULT 'SIMULATOR',
    project_id      INTEGER,
    version_id      INTEGER
);

CREATE TABLE IF NOT EXISTS lc_screens (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL,
    fingerprint       TEXT    NOT NULL,
    title             TEXT    NOT NULL,
    depth             INTEGER DEFAULT 0,
    parent_fp         TEXT,
    phash             TEXT,
    screenshot_path   TEXT,
    ocr_text          TEXT,
    discovered_at     TEXT,
    is_representative INTEGER DEFAULT 0
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
CREATE INDEX IF NOT EXISTS idx_trans_from_session ON lc_transitions(from_fp, session_id);
CREATE INDEX IF NOT EXISTS idx_trans_to_session ON lc_transitions(to_fp, session_id);
CREATE INDEX IF NOT EXISTS idx_screens_fp_session ON lc_screens(fingerprint, session_id);
CREATE INDEX IF NOT EXISTS idx_screens_session_rep ON lc_screens(session_id, is_representative);
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
        version_id: int | None = None,
        operation_code_key: str | None = None,
        operation_tag_id: int | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._storage_dir = Path(storage_dir) / session_id
        self._session_id = session_id
        self._game_title = game_title
        self._explicit_version_id = version_id
        self._operation_code_key = operation_code_key
        self._operation_tag_id = operation_tag_id

        # ディレクトリ先行作成
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # SQLite 接続 (timeout=30 で BUSY 対策)
        self._conn = sqlite3.connect(str(self._db_path), timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()

        # version_id: 明示指定 > active version > デフォルト 1
        if self._explicit_version_id is not None:
            # 指定バージョンが存在しなければ新規作成
            exists = self._conn.execute(
                "SELECT id FROM lc_versions WHERE id = ?",
                (self._explicit_version_id,),
            ).fetchone()
            if not exists:
                self._conn.execute(
                    "INSERT INTO lc_versions (id, name, is_active, created_at)"
                    " VALUES (?, ?, 0, datetime('now'))",
                    (self._explicit_version_id, f"Ver{self._explicit_version_id}"),
                )
                self._conn.commit()
                logger.info("[ScreenRecorder] バージョン %d を新規作成", self._explicit_version_id)
            self._version_id = self._explicit_version_id
        else:
            v_row = self._conn.execute(
                "SELECT id FROM lc_versions WHERE is_active = 1"
            ).fetchone()
            self._version_id = v_row[0] if v_row else 1

        # セッション登録 (新規) または継続再開時の status 更新
        self._conn.execute(
            "INSERT OR IGNORE INTO lc_sessions"
            " (session_id, screens_found, started_at, status, game_title, version_id,"
            "  operation_code_key, operation_tag_id)"
            " VALUES (?, 0, ?, 'running', ?, ?, ?, ?)",
            (
                self._session_id, datetime.now().isoformat(), self._game_title,
                self._version_id, self._operation_code_key, self._operation_tag_id,
            ),
        )
        # 継続再開時は status を 'running' に戻し、completion_type をクリア
        # 操縦カテゴリ未設定の既存セッションには今回の値を上書き保存する
        self._conn.execute(
            "UPDATE lc_sessions SET status = 'running', completion_type = NULL,"
            " operation_code_key = COALESCE(operation_code_key, ?),"
            " operation_tag_id = COALESCE(operation_tag_id, ?)"
            " WHERE session_id = ?",
            (self._operation_code_key, self._operation_tag_id, self._session_id),
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
        # タップ後の保存抑制 (phash 追跡は継続)
        self._last_tap_time: float = 0.0
        self._TAP_COOLDOWN: float = 2.0  # タップ後この秒数は通常記録をスキップ

        # 統計
        self._recorded_count: int = 0

        # 起動シーン記録用
        self._startup_last_phash: str = ""
        self._startup_last_brightness: float = 0.0

        logger.info(
            "[ScreenRecorder] 初期化: session=%s, 既存fp=%d件, storage=%s",
            session_id, len(self._seen_fps), self._storage_dir,
        )

    # ─── public API ───────────────────────────────────

    def check_discarded(self) -> bool:
        """セッションが外部から discarded にされたか確認する。"""
        try:
            row = self._conn.execute(
                "SELECT status FROM lc_sessions WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
            if row and row[0] == "discarded":
                logger.info(
                    "[ScreenRecorder] セッション %s が discarded — 新セッション作成が必要",
                    self._session_id,
                )
                return True
        except Exception as e:
            logger.warning("[ScreenRecorder] discarded チェックエラー: %s", e)
        return False

    def record_startup(
        self,
        img_path: Optional[Path],
        phash: str,
        ocr_results: Optional[list[dict]] = None,
    ) -> bool:
        """起動シーン専用記録: 暗転→ロゴ等の変化を検出して記録する。

        完全暗転 (brightness <= _MIN_BRIGHTNESS) はスキップしつつ基準値として保持。
        phash または brightness に変化があった場合のみ記録する。
        """
        if not img_path or not Path(img_path).exists():
            return False

        _img = cv2.imread(str(img_path))
        if _img is None:
            return False

        _brightness = float(np.mean(cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY)))

        # startup_phase では暗画面スキップを無効化（ロゴ等の暗い画面を残すため）
        # maybe_record 側の暗画面スキップはそのまま残す

        # 変化判定: ハッシュ または brightness がわずかでも変わったら保存
        _changed = False
        if phash and self._startup_last_phash:
            from lc.image_comparator import phash_distance
            _dist = phash_distance(self._startup_last_phash, phash)
            _changed = _dist >= 3  # 微小な変化でも検出
        elif not self._startup_last_phash:
            _changed = True  # 初回の非暗転フレーム

        if not _changed and abs(_brightness - self._startup_last_brightness) > 5:
            _changed = True  # brightness の微小変化でも検出

        if not _changed:
            return False

        # 基準値を更新
        self._startup_last_phash = phash if phash else self._startup_last_phash
        self._startup_last_brightness = _brightness

        # fingerprint 生成
        _ts_suffix = f"_{int(time.time() * 1000) % 1000000}"
        if phash:
            content_fp = f"startup_ph_{phash[:14]}{_ts_suffix}"
        else:
            content_fp = f"startup_{time.time()}"

        # 保存
        title = "(STARTUP)"
        screenshot_path, thumbnail_path = self._save_screenshot(
            Path(img_path), content_fp
        )
        if not screenshot_path:
            return False

        # OCR テキスト: Vision OCR で検出されたテキストを保存（クラスタリングの精度向上）
        ocr_text = ""
        if ocr_results:
            ocr_text = " ".join(
                item.get("text", "").strip() for item in ocr_results
                if item.get("text", "").strip()
            )
        screen_id = self._insert_screen(
            fingerprint=content_fp,
            title=title,
            parent_fp=self._last_recorded_fp,
            phash=phash,
            screenshot_path=screenshot_path,
            thumbnail_path=thumbnail_path,
            ocr_text=ocr_text,
            scene="STARTUP",
        )
        if screen_id:
            self._seen_fps.add(content_fp)
            self._last_recorded_fp = content_fp
            self._last_recorded_phash = phash
            self._last_inserted_id = screen_id
            self._recorded_count += 1
            logger.info(
                "[ScreenRecorder] STARTUP記録 #%d: brightness=%.1f, phash=%s",
                self._recorded_count, _brightness, phash[:8] if phash else "N/A",
            )
            return True
        return False

    def maybe_record(
        self,
        analysis_path: Optional[Path],
        ocr_results: list[dict],
        scene: str,
        phash: str,
        force: bool = False,
    ) -> bool:
        """寛容撮影: 暗転以外は全部保存。クラスタリングはバッチで行う。

        force=True: タップ直前の強制保存。暗転・シーンチェック・重複チェックを
        すべてスキップし、必ず保存する。
        """

        # Android システムダイアログはゲーム画面ではないので常にスキップ (force含む)
        if ocr_results and any("応答していません" in r.get("text", "") for r in ocr_results):
            return False

        if not force:
            # 0. タップ後クールダウン: 保存のみスキップ (phash 追跡は呼び出し元で継続)
            if self._last_tap_time > 0 and (time.time() - self._last_tap_time) < self._TAP_COOLDOWN:
                return False

            # 1. シーンスキップ
            if scene in _SKIP_SCENES:
                return False

            # 2. 全ピクセル暗め / 全ピクセル明るめ → スキップ (テキスト有無問わず)
            if analysis_path and Path(analysis_path).exists():
                _img = cv2.imread(str(analysis_path))
                if _img is not None:
                    if _is_too_dark_or_bright(cv2.cvtColor(_img, cv2.COLOR_BGR2GRAY)):
                        return False

        # 3. fingerprint 生成 (タイムスタンプ付きでファイル名衝突を防止)
        _ts_suffix = f"_{int(time.time() * 1000) % 1000000}"
        if force:
            if phash:
                content_fp = f"ph_{phash[:14]}{_ts_suffix}"
            else:
                content_fp = f"force_{time.time()}"
        else:
            normalized = self._normalize_ocr(ocr_results) if ocr_results else ""
            if normalized:
                content_fp = self._content_fingerprint(normalized)
            elif phash:
                content_fp = f"ph_{phash[:14]}{_ts_suffix}"
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

        # 画像保存 (analysis_path を直接保存: OCR と画像の一致を保証)
        screenshot_path, thumbnail_path = "", ""
        if analysis_path and Path(analysis_path).exists():
            screenshot_path, thumbnail_path = self._save_screenshot(
                Path(analysis_path), content_fp
            )

        # 画像保存失敗なら記録しない
        if not screenshot_path:
            return False

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

    def close(self, goal_reached: bool = False) -> None:
        """セッションを停止して DB 接続を閉じる。

        Args:
            goal_reached: True なら 'completed' + 'goal_reached' (ホーム到達/周回完了, マージ可能),
                          False なら 'paused' + 'manual_stop' (Ctrl+C 等、resume 可能、マージ前にユーザーの確定が必要)
        """
        if goal_reached:
            new_status = 'completed'
            completion_type = 'goal_reached'
        else:
            new_status = 'paused'
            completion_type = 'manual_stop'
        try:
            # pending transition をフラッシュ (to=NULL)
            if self._pending_transition is not None:
                self._insert_transition(self._pending_transition)
                self._pending_transition = None
            self._conn.execute(
                "UPDATE lc_sessions SET status = ?,"
                " completion_type = ?,"
                " screens_found = ? WHERE session_id = ?",
                (new_status, completion_type, self._recorded_count, self._session_id),
            )
            self._conn.commit()
            logger.info(
                "[ScreenRecorder] セッション %s (%s): %d 画面記録",
                new_status, completion_type, self._recorded_count,
            )
        except Exception as e:
            logger.warning("[ScreenRecorder] close エラー: %s", e)
        finally:
            self._conn.close()

    def start_new_session(self, new_session_id: str) -> None:
        """周回完了等で現在のセッションを完了し、新セッションに切り替える。

        DB 接続は維持したまま、前セッションを 'completed' にし、
        新しい session_id でレコードを追加する。
        """
        # 前セッション完了処理
        try:
            if self._pending_transition is not None:
                self._insert_transition(self._pending_transition)
                self._pending_transition = None
            # discarded は外部操作なので上書きしない
            cur_status = self._conn.execute(
                "SELECT status FROM lc_sessions WHERE session_id = ?",
                (self._session_id,),
            ).fetchone()
            if cur_status and cur_status[0] != "discarded":
                self._conn.execute(
                    "UPDATE lc_sessions SET status = 'completed',"
                    " completion_type = 'goal_reached',"
                    " screens_found = ? WHERE session_id = ?",
                    (self._recorded_count, self._session_id),
                )
            self._conn.commit()
        except Exception as e:
            logger.warning("[ScreenRecorder] 前セッション完了エラー: %s", e)

        prev_session = self._session_id

        # 新セッションに切替
        self._session_id = new_session_id
        self._storage_dir = self._storage_dir.parent / new_session_id
        self._storage_dir.mkdir(parents=True, exist_ok=True)

        # 新セッション登録
        self._conn.execute(
            "INSERT OR IGNORE INTO lc_sessions"
            " (session_id, screens_found, started_at, status, game_title, version_id)"
            " VALUES (?, 0, ?, 'running', ?, ?)",
            (self._session_id, datetime.now().isoformat(), self._game_title, self._version_id),
        )
        self._conn.commit()

        # 状態リセット
        self._recorded_count = 0
        self._last_recorded_fp = None
        self._last_recorded_phash = ""
        self._last_inserted_id = None
        self._last_record_time = 0.0
        self._last_tap_time = 0.0
        self._startup_last_phash = ""
        self._startup_last_brightness = 0.0
        self._seen_fps = set()  # 周回で同じセリフを記録するためリセット必須

        logger.info(
            "[ScreenRecorder] 新セッション開始: %s → %s (storage=%s)",
            prev_session, new_session_id, self._storage_dir,
        )

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
        if "is_artifact" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN is_artifact INTEGER DEFAULT 0"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: is_artifact カラム追加")
        if "dhash" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN dhash TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: dhash カラム追加")
        if "cluster_id_phash_only" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN cluster_id_phash_only INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: cluster_id_phash_only カラム追加")
        # 旧カラム cluster_id_dhash は廃止 (Q3=B)
        if "cluster_id_dhash" in cols:
            try:
                self._conn.execute("ALTER TABLE lc_screens DROP COLUMN cluster_id_dhash")
                self._conn.commit()
                logger.info("[ScreenRecorder] migrate: cluster_id_dhash カラム削除")
            except sqlite3.OperationalError as e:
                logger.warning("[ScreenRecorder] cluster_id_dhash DROP 失敗: %s", e)
        if "cluster_id_hybrid" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN cluster_id_hybrid INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: cluster_id_hybrid カラム追加")
        if "cluster_decision_method" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN cluster_decision_method TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: cluster_decision_method カラム追加")
        if "avg_brightness" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN avg_brightness REAL"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: avg_brightness カラム追加")
        if "phash_dist_to_prev_rep" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN phash_dist_to_prev_rep INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: phash_dist_to_prev_rep カラム追加")
        if "dhash_dist_to_prev_rep" not in cols:
            self._conn.execute(
                "ALTER TABLE lc_screens ADD COLUMN dhash_dist_to_prev_rep INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: dhash_dist_to_prev_rep カラム追加")
        # 旧カラム hist_dist_to_prev_rep は廃止 (Q3=B、ヒスト判定撤廃のため)
        if "hist_dist_to_prev_rep" in cols:
            try:
                self._conn.execute("ALTER TABLE lc_screens DROP COLUMN hist_dist_to_prev_rep")
                self._conn.commit()
                logger.info("[ScreenRecorder] migrate: hist_dist_to_prev_rep カラム削除")
            except sqlite3.OperationalError as e:
                logger.warning("[ScreenRecorder] hist_dist_to_prev_rep DROP 失敗: %s", e)
        # lc_master_nodes に dhash カラム
        try:
            mn_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_master_nodes)")}
            if "dhash" not in mn_cols:
                self._conn.execute(
                    "ALTER TABLE lc_master_nodes ADD COLUMN dhash TEXT"
                )
                self._conn.commit()
                logger.info("[ScreenRecorder] migrate: lc_master_nodes.dhash カラム追加")
        except Exception:
            pass  # テーブル未作成時
        # lc_transitions に edge_type カラム
        trans_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_transitions)")}
        if "edge_type" not in trans_cols:
            self._conn.execute(
                "ALTER TABLE lc_transitions ADD COLUMN edge_type TEXT DEFAULT 'tap'"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: lc_transitions.edge_type カラム追加")
        # lc_sessions に completion_type カラム
        sess_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_sessions)")}
        if "completion_type" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE lc_sessions ADD COLUMN completion_type TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: completion_type カラム追加")
        # version_id マイグレーション
        if "version_id" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE lc_sessions ADD COLUMN version_id INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: lc_sessions.version_id カラム追加")
        # 操縦カテゴリ (Phase 2)
        if "operation_code_key" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE lc_sessions ADD COLUMN operation_code_key TEXT"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: lc_sessions.operation_code_key カラム追加")
        if "operation_tag_id" not in sess_cols:
            self._conn.execute(
                "ALTER TABLE lc_sessions ADD COLUMN operation_tag_id INTEGER"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: lc_sessions.operation_tag_id カラム追加")
        # lc_versions テーブル + 初期バージョン
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS lc_versions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT    UNIQUE NOT NULL,
                created_at TEXT    NOT NULL DEFAULT (datetime('now')),
                is_active  INTEGER DEFAULT 0,
                is_deleted INTEGER DEFAULT 0
            )
        """)
        ver_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lc_versions)")}
        if "is_deleted" not in ver_cols:
            self._conn.execute("ALTER TABLE lc_versions ADD COLUMN is_deleted INTEGER DEFAULT 0")
            self._conn.commit()
        if not self._conn.execute("SELECT 1 FROM lc_versions LIMIT 1").fetchone():
            self._conn.execute(
                "INSERT INTO lc_versions (name, is_active) VALUES ('v1.0.0', 1)"
            )
            self._conn.execute(
                "UPDATE lc_sessions SET version_id = 1 WHERE version_id IS NULL"
            )
            self._conn.commit()
            logger.info("[ScreenRecorder] migrate: v1.0.0 初期バージョン作成")

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

    @staticmethod
    def _crop_os_bars(img: np.ndarray) -> np.ndarray:
        """ステータスバー/ナビバーの黒帯を除去してゲーム領域のみ返す。

        上端・下端から行平均輝度が低い（< 15）帯を検出してクロップ。
        scrcpy キャプチャでは通常不要だが、ADB screencap 時の OS UI を除去する。
        """
        h, w = img.shape[:2]
        if h < 100:
            return img

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        row_means = np.mean(gray, axis=1)

        # 上端: 輝度 < 15 の連続行をスキップ (最大 h の 10%)
        top = 0
        max_bar = int(h * 0.1)
        for i in range(min(max_bar, h)):
            if row_means[i] < 15:
                top = i + 1
            else:
                break

        # 下端: 同様
        bottom = h
        for i in range(h - 1, max(h - max_bar, 0), -1):
            if row_means[i] < 15:
                bottom = i
            else:
                break

        # 左端・右端も同様 (ナビバーが横に出る場合)
        col_means = np.mean(gray, axis=0)
        left = 0
        max_side = int(w * 0.1)
        for i in range(min(max_side, w)):
            if col_means[i] < 15:
                left = i + 1
            else:
                break

        right = w
        for i in range(w - 1, max(w - max_side, 0), -1):
            if col_means[i] < 15:
                right = i
            else:
                break

        # クロップが意味ある場合のみ適用 (少なくとも元の80%は残す)
        cropped_h = bottom - top
        cropped_w = right - left
        if cropped_h >= h * 0.8 and cropped_w >= w * 0.8 and (top > 0 or bottom < h or left > 0 or right < w):
            return img[top:bottom, left:right]
        return img

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
        - 数字混じりトークン (例: 'Download 1083 MB', 'Ver.3.4.0', 'Turn 5')
          は数字を**保持**してそのままハッシュに含める。これにより本来別画面の
          進捗・バージョン違いを別 fingerprint として正しく区別する。
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

    _DB_RETRY_MAX = 5
    _DB_RETRY_INTERVAL = 2.0

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
        """lc_screens に INSERT し、生成された ID を返す。DB locked 時はリトライ。"""
        import time
        import sqlite3
        # dhash を計算（screenshot_path から）
        dhash_val = None
        if screenshot_path and Path(screenshot_path).exists():
            try:
                from lc.utils import compute_dhash
                dhash_val = compute_dhash(Path(screenshot_path))
            except Exception:
                pass
        for attempt in range(self._DB_RETRY_MAX):
            try:
                cur = self._conn.execute(
                    "INSERT INTO lc_screens"
                    " (session_id, fingerprint, title, depth, parent_fp,"
                    "  phash, dhash, screenshot_path, thumbnail_path, ocr_text, scene, discovered_at)"
                    " VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._session_id,
                        fingerprint,
                        title,
                        parent_fp,
                        phash,
                        dhash_val,
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
            except sqlite3.OperationalError as e:
                if "locked" in str(e) and attempt < self._DB_RETRY_MAX - 1:
                    logger.warning("[ScreenRecorder] DB locked, リトライ %d/%d (%.1fs後)",
                                   attempt + 1, self._DB_RETRY_MAX, self._DB_RETRY_INTERVAL)
                    time.sleep(self._DB_RETRY_INTERVAL)
                else:
                    raise

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
