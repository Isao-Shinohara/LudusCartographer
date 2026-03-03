"""
crawler.py — 再帰的自動巡回クローラー

DFS (深さ優先探索) で画面を自動探索し、各画面の情報を
MySQL の screens / ui_elements / crawl_sessions テーブルに蓄積する。

【アルゴリズム】
  1. 現在画面のスクリーンショット + OCR で画面指紋を生成
  2. 未訪問なら screens テーブルに保存
  3. タップ候補を抽出（iOS: 「>」シェブロン隣接テキスト）
  4. 各候補をタップ → 子画面を再帰探索 → back() で復帰
  5. 時間切れ or 最大深さ達したら停止

【タップ候補検出】
  iOS 設定アプリ（および標準的な iOS ナビゲーション）向け:
  - 「>」シェブロンが画面右側 (x > 800px) に存在し、
    同じ y レベル (±60px) にある左側テキストをタップ候補とする。
  - シェブロンがない場合は位置ヒューリスティックにフォールバック。
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .driver import AppiumDriver
from .ocr import run_ocr, find_best, format_results
from .utils import compute_phash, phash_distance

logger = logging.getLogger(__name__)

# タイトル抽出から除外するテキスト（ナビゲーションバーの制御要素など）
EXCLUDE_TEXTS: frozenset[str] = frozenset({
    '>', '›', '<', '‹', '7', 'L',
    '戻る', 'Done', '完了', 'Edit', '編集', 'キャンセル',
})

# phash ハミング距離の重複判定閾値（この値未満 = ほぼ同一画面）
PHASH_THRESHOLD = 8


# ============================================================
# 設定
# ============================================================

@dataclass
class CrawlerConfig:
    """クローラーの動作設定"""
    max_depth:        int   = 3      # 最大探索深さ
    max_duration_sec: float = 180.0  # 最大実行時間（秒）
    wait_after_tap:   float = 3.0    # タップ後の画面描画待機（秒）
    wait_after_back:  float = 1.5    # back() 後の待機（秒）
    min_confidence:   float = 0.6    # OCR 最低信頼スコア
    # DB 設定（省略時はメモリのみ）
    db_game_id:       int   = 1
    db_host:          str   = ""
    db_port:          int   = 3306
    db_name:          str   = "ludus_cartographer"
    db_user:          str   = "root"
    db_password:      str   = ""


# ============================================================
# 内部データ構造
# ============================================================

@dataclass
class ScreenRecord:
    """1 画面分のスナップショット情報"""
    fingerprint:    str                    # 画面指紋 (MD5)
    title:          str                    # 画面タイトル（OCR推定）
    screenshot_path: Path                  # スクリーンショットパス
    depth:          int                    # 探索深さ
    parent_fp:      Optional[str]          # 親画面の指紋
    ocr_results:    list[dict]             # run_ocr() の生結果
    tappable_items: list[dict]             # タップ候補
    db_screen_id:   Optional[int] = None   # screens.id（DB保存後に設定）
    phash:          Optional[str] = None   # 画像知覚ハッシュ (compute_phash)
    discovered_at:  str = field(
        default_factory=lambda: datetime.now().isoformat()
    )


@dataclass
class CrawlStats:
    """クロール実行統計"""
    screens_found:    int = 0
    screens_skipped:  int = 0   # 既訪問 skip 数
    taps_total:       int = 0
    db_saves:         int = 0
    db_errors:        int = 0
    elapsed_sec:      float = 0.0


# ============================================================
# メインクローラー
# ============================================================

class ScreenCrawler:
    """
    DFS 自動巡回クローラー。

    使用例:
        cfg = CrawlerConfig(max_duration_sec=180, max_depth=3)
        crawler = ScreenCrawler(driver, cfg)
        stats = crawler.crawl()
        print(f"{stats.screens_found} 画面を発見")
    """

    def __init__(self, driver: AppiumDriver, config: CrawlerConfig):
        self.driver  = driver
        self.config  = config
        self._visited:   dict[str, ScreenRecord] = {}  # fingerprint → ScreenRecord
        self._nav_stack: list[str] = []               # 現在の探索経路（指紋スタック）
        self._stats  = CrawlStats()
        self._start_time = time.time()

        # DB 接続（利用可能な場合のみ）
        self._db_conn        = None
        self._crawl_session_id: Optional[int] = None
        self._init_db()

    # ----------------------------------------------------------
    # パブリック API
    # ----------------------------------------------------------

    def crawl(self, depth: int = 0, parent_fp: Optional[str] = None) -> CrawlStats:
        """
        DFS クロールのエントリポイント。

        現在表示されている画面から探索を開始する。
        back() による復帰と再帰を繰り返し、時間 or 深さ制限に達するまで続ける。

        Returns:
            CrawlStats: 実行統計
        """
        self._crawl_impl(depth, parent_fp)
        self._stats.elapsed_sec = time.time() - self._start_time
        self._finalize_session()
        return self._stats

    def get_visited_screens(self) -> list[ScreenRecord]:
        """訪問済み画面の一覧を返す。"""
        return list(self._visited.values())

    # ----------------------------------------------------------
    # DFS 実装
    # ----------------------------------------------------------

    def _crawl_impl(self, depth: int, parent_fp: Optional[str]) -> None:
        """再帰 DFS の本体。"""

        # --- 停止条件 ---
        if self._is_time_up():
            logger.info("[CRAWL] ⏱ 時間制限に達しました")
            return
        if depth >= self.config.max_depth:
            logger.info(f"[CRAWL] 最大深さ {depth} に到達 — これ以上深く探索しません")
            return

        # --- 現在画面を取得・同定 ---
        record = self._snapshot_current_screen(depth, parent_fp)
        if record is None:
            return

        # --- phash ログ (診断用。iOS 設定の白系画面は phash 距離が 0-2 で差別不能のため
        #     重複判定には使用せず、テキスト指紋を正とする) ---
        if record.phash is not None and self._visited:
            min_dist = min(
                phash_distance(record.phash, prev.phash)
                for prev in self._visited.values()
                if prev.phash is not None
            )
            logger.debug(f"[PHASH] {record.title!r}  phash={record.phash}  min_dist={min_dist}")

        # --- 訪問済みチェック ---
        if record.fingerprint in self._visited:
            prev = self._visited[record.fingerprint]
            logger.info(
                f"[CRAWL] skip（既訪問）: {record.title!r}"
                f" (深さ{prev.depth} で既に訪問済み)"
            )
            self._stats.screens_skipped += 1
            return

        # --- 新規画面として登録・保存 ---
        self._visited[record.fingerprint] = record
        self._nav_stack.append(record.fingerprint)
        self._stats.screens_found += 1

        self._save_screen_to_db(record)
        self._save_ui_elements_to_db(record)

        logger.info(
            f"[CRAWL] ✅ 新規画面 #{self._stats.screens_found}"
            f"  深さ={depth}"
            f"  title={record.title!r}"
            f"  items={len(record.tappable_items)}件"
            f"  指紋={record.fingerprint[:8]}…"
        )

        # --- 各タップ候補を探索 ---
        for i, item in enumerate(record.tappable_items):
            if self._is_time_up():
                break

            text = item["text"]
            px, py = item["center"]
            logger.info(
                f"[CRAWL]  → [{i+1}/{len(record.tappable_items)}]"
                f" タップ: {text!r}  pixel=({px},{py})"
            )

            # タップ（ピクセル → ポイント 自動変換）
            self.driver.tap_ocr_coordinate(
                px, py,
                action_name=f"tap_{_safe_name(text)}",
                ocr_data={"ocr_boxes": [
                    {"text": text, "confidence": item["confidence"], "box": item["box"]}
                ]},
            )
            self._stats.taps_total += 1
            self.driver.wait(self.config.wait_after_tap)

            # 子画面を再帰探索
            self._crawl_impl(depth + 1, record.fingerprint)

            # 戻る
            if not self._is_time_up():
                self.driver.back()
                self.driver.wait(self.config.wait_after_back)
                logger.info(f"[CRAWL]  ← 戻る完了: {text!r}")

        self._nav_stack.pop()

    # ----------------------------------------------------------
    # 画面スナップショット
    # ----------------------------------------------------------

    def _snapshot_current_screen(
        self,
        depth: int,
        parent_fp: Optional[str],
    ) -> Optional[ScreenRecord]:
        """現在画面のスクリーンショット + OCR + 指紋生成を行い ScreenRecord を返す。"""
        try:
            shot_path = self.driver.screenshot(f"crawl_d{depth}")
        except Exception as e:
            logger.error(f"[CRAWL] スクリーンショット失敗: {e}")
            return None

        try:
            ocr_results = run_ocr(shot_path, min_confidence=0.0)
        except Exception as e:
            logger.error(f"[CRAWL] OCR 失敗: {e}")
            return None

        fingerprint = self._screen_fingerprint(ocr_results)
        title       = self._extract_title(ocr_results)
        tappable    = self._find_tappable_items(ocr_results)

        # phash 計算（エラー時はスキップしてテキスト指紋のみで継続）
        ph = None
        try:
            ph = compute_phash(shot_path)
        except Exception as e:
            logger.debug(f"[CRAWL] phash 計算スキップ: {e}")

        return ScreenRecord(
            fingerprint=fingerprint,
            title=title,
            screenshot_path=shot_path,
            depth=depth,
            parent_fp=parent_fp,
            ocr_results=ocr_results,
            tappable_items=tappable,
            phash=ph,
        )

    # ----------------------------------------------------------
    # 画面指紋
    # ----------------------------------------------------------

    def _screen_fingerprint(self, ocr_results: list[dict]) -> str:
        """
        高信頼度テキストのソート済み集合から MD5 指紋を生成する。

        除外するもの:
        - ステータスバー領域（y < 100px）: 時刻が毎回変わる
        - 信頼スコア < 0.75
        - 1 文字以下のテキスト
        """
        texts = sorted(
            r["text"]
            for r in ocr_results
            if r["confidence"] >= 0.75
            and r["center"][1] >= 100        # ステータスバー除外
            and len(r["text"]) > 1
        )
        fp_str = "|".join(texts)
        return hashlib.md5(fp_str.encode("utf-8")).hexdigest()[:16]

    # ----------------------------------------------------------
    # タイトル抽出
    # ----------------------------------------------------------

    def _extract_title(self, ocr_results: list[dict]) -> str:
        """
        画面タイトルを OCR 結果から推定する。2 ステップ方式:

        Step 1 — Large Title (iOS 大タイトル):
          左寄り (x < 400px)、y: 150〜750px、高信頼スコア
          → ナビバー制御要素 (戻る/Done/Edit) を除外

        Step 2 — Nav-bar center title (ナビゲーションバー中央タイトル):
          中央寄り (x: 300〜900px)、y: 100〜260px、高信頼スコア
          → Large Title が見つからなかった場合のフォールバック
        """
        # 共通フィルタ
        base_candidates = [
            r for r in ocr_results
            if r["confidence"] > 0.8
            and 1 < len(r["text"]) <= 20
            and r["text"].strip() not in EXCLUDE_TEXTS
        ]

        # Step 1: Large Title (y=300-750, x<800, len≤15)
        # y>=300 で nav-bar 戻るボタン (「< 設定」など) を除外
        # x<800 で iOS の Large Title が中央寄り (x≈587) でも取得できるよう範囲を拡大
        # len<=15 で説明文 (「ソフトウェアアップデート、デバイスの言語、」など) を除外
        large_title = [
            r for r in base_candidates
            if 300 <= r["center"][1] <= 750
            and r["center"][0] < 800
            and len(r["text"]) <= 15
        ]
        if large_title:
            return max(large_title, key=lambda r: r["confidence"])["text"]

        # Step 2: Nav-bar center title (中央, y=100-260)
        nav_candidates = [
            r for r in base_candidates
            if 100 <= r["center"][1] <= 260
            and 300 <= r["center"][0] <= 900
        ]
        if nav_candidates:
            return max(nav_candidates, key=lambda r: r["confidence"])["text"]

        return "unknown"

    # ----------------------------------------------------------
    # タップ候補抽出
    # ----------------------------------------------------------

    def _find_tappable_items(self, ocr_results: list[dict]) -> list[dict]:
        """
        OCR 結果からタップ可能な UI 項目を抽出する。

        iOS 標準設定アプリ（および一般的な iOS ナビゲーション）の検出ロジック:
        1. 「>」シェブロンが右端（x > 800px）に存在する y レベルを収集
        2. 各シェブロンと同じ y レベル（±60px）にある左側テキストをタップ候補とする
        3. シェブロンがなければ位置ヒューリスティック（左寄り、下部）でフォールバック

        除外条件:
        - ステータスバー: y < 100
        - ナビゲーションバー左端の戻るボタン: y < 250 かつ x < 300
        - 右端要素: x > 850（シェブロン, アイコン）
        - 中央揃えの説明文: x > 400 かつ len > 20
        - 低信頼スコア: conf < min_confidence
        - 1 文字以下
        """
        # --- シェブロン y 座標を収集 ---
        chevron_ys: list[int] = []
        for r in ocr_results:
            cx, cy = r["center"]
            text = r["text"].strip()
            if text in ('>', '›') and cx > 850:
                chevron_ys.append(cy)

        # --- 候補フィルタリング ---
        filtered: list[dict] = []
        for r in ocr_results:
            cx, cy = r["center"]
            text    = r["text"].strip()

            # 基本除外
            if r["confidence"] < self.config.min_confidence:
                continue
            if len(text) <= 1:
                continue
            if cy < 100:            # ステータスバー
                continue
            if cy < 250 and cx < 300:  # nav bar 戻るボタン
                continue
            if cx > 850:            # 右端（シェブロン等）
                continue
            if cx > 400 and len(text) > 20:  # 中央の説明文
                continue
            if cy < 900 and cx > 400:  # タイトル・説明文ゾーン（大タイトル・副題）
                continue
            # 特殊記号
            if text in ('>', '›', '<', '‹', 'Q', '…'):
                continue

            filtered.append(r)

        # --- シェブロン近傍チェック ---
        if chevron_ys:
            # シェブロンと y 座標が近い項目のみを採用
            tappable = [
                r for r in filtered
                if any(abs(r["center"][1] - chy) <= 60 for chy in chevron_ys)
            ]
            # 閾値 5: OCR がシェブロンを一部しか検出できない場合 (3-4 件)は
            # 位置ヒューリスティックに落とし、より多くの行を取得する
            if len(tappable) >= 5:
                logger.debug(
                    f"[TAPPABLE] シェブロン一致: {len(tappable)}件"
                    f" / 候補: {len(filtered)}件"
                )
                return tappable
            logger.debug(
                f"[TAPPABLE] シェブロン一致が {len(tappable)}件のみ → 位置フォールバックへ"
            )

        # --- フォールバック: 位置ヒューリスティック ---
        logger.debug(
            f"[TAPPABLE] シェブロン不一致 → 位置フォールバック ({len(filtered)}件)"
        )
        return [r for r in filtered if r["center"][1] > 400 and r["center"][0] < 600]

    # ----------------------------------------------------------
    # DB 保存
    # ----------------------------------------------------------

    def _init_db(self) -> None:
        """MySQL 接続を初期化し、クロールセッションを開始する。"""
        cfg = self.config
        host = cfg.db_host or os.environ.get("DB_HOST", "")
        if not host:
            logger.info("[DB] DB_HOST 未設定 → インメモリのみで動作")
            return

        try:
            import pymysql
            self._db_conn = pymysql.connect(
                host=host,
                port=cfg.db_port or int(os.environ.get("DB_PORT", 3306)),
                db=cfg.db_name or os.environ.get("DB_NAME", "ludus_cartographer"),
                user=cfg.db_user or os.environ.get("DB_USER", "root"),
                password=cfg.db_password or os.environ.get("DB_PASSWORD", ""),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
            # crawl_sessions に開始レコードを挿入
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO crawl_sessions
                       (game_id, device_id, status, screens_found)
                       VALUES (%s, %s, 'running', 0)""",
                    (cfg.db_game_id, self.driver.session_id),
                )
                self._crawl_session_id = cur.lastrowid
            self._db_conn.commit()
            logger.info(f"[DB] セッション開始: crawl_sessions.id={self._crawl_session_id}")

        except Exception as e:
            logger.warning(f"[DB] 接続スキップ: {e}")
            self._db_conn = None

    def _save_screen_to_db(self, record: ScreenRecord) -> None:
        """screens テーブルに画面情報を UPSERT する。"""
        if self._db_conn is None:
            return

        ocr_text = " / ".join(r["text"] for r in record.ocr_results if len(r["text"]) > 1)

        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO screens
                       (game_id, screen_hash, name, category,
                        screenshot_path, ocr_text)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                         visited_count = visited_count + 1,
                         last_seen_at  = NOW(),
                         name = IF(name IS NULL OR name = '', VALUES(name), name)""",
                    (
                        self.config.db_game_id,
                        record.fingerprint,
                        record.title,
                        "settings",
                        str(record.screenshot_path),
                        ocr_text,
                    ),
                )
                record.db_screen_id = cur.lastrowid or self._get_screen_id(record.fingerprint)
            self._db_conn.commit()
            self._stats.db_saves += 1

            # crawl_sessions.screens_found を更新
            if self._crawl_session_id:
                with self._db_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE crawl_sessions SET screens_found = %s WHERE id = %s",
                        (self._stats.screens_found, self._crawl_session_id),
                    )
                self._db_conn.commit()

        except Exception as e:
            logger.warning(f"[DB] screens 保存エラー: {e}")
            self._stats.db_errors += 1

    def _get_screen_id(self, fingerprint: str) -> Optional[int]:
        """指紋で screens.id を取得する。"""
        if not self._db_conn:
            return None
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM screens WHERE game_id=%s AND screen_hash=%s LIMIT 1",
                    (self.config.db_game_id, fingerprint),
                )
                row = cur.fetchone()
                return row["id"] if row else None
        except Exception:
            return None

    def _save_ui_elements_to_db(self, record: ScreenRecord) -> None:
        """ui_elements テーブルにタップ候補 UI 要素を保存する。"""
        if self._db_conn is None or record.db_screen_id is None:
            return
        if not record.tappable_items:
            return

        try:
            with self._db_conn.cursor() as cur:
                for item in record.tappable_items:
                    box = item["box"]
                    xs  = [p[0] for p in box]
                    ys  = [p[1] for p in box]
                    x1, y1 = min(xs), min(ys)
                    x2, y2 = max(xs), max(ys)

                    cur.execute(
                        """INSERT IGNORE INTO ui_elements
                           (screen_id, element_type, label,
                            bbox_x, bbox_y, bbox_w, bbox_h,
                            is_tappable, confidence)
                           VALUES (%s, 'menu', %s, %s, %s, %s, %s, 1, %s)""",
                        (
                            record.db_screen_id,
                            item["text"],
                            x1, y1, x2 - x1, y2 - y1,
                            item["confidence"],
                        ),
                    )
            self._db_conn.commit()
        except Exception as e:
            logger.warning(f"[DB] ui_elements 保存エラー: {e}")

    def save_summary_json(self, path: Path) -> None:
        """
        クロール結果を JSON ファイルに保存する。

        出力フィールド (各画面): fingerprint / title / depth / parent_fp /
          tappable_items / phash / screenshot_path / discovered_at

        Args:
            path: 保存先パス (例: evidence/20260303_160759/crawl_summary.json)
        """
        import json

        data = {
            "session_id": path.parent.name,
            "screens": [
                {
                    "fingerprint":     rec.fingerprint,
                    "title":           rec.title,
                    "depth":           rec.depth,
                    "parent_fp":       rec.parent_fp,
                    "tappable_items":  [
                        {"text": item["text"], "confidence": item["confidence"]}
                        for item in rec.tappable_items
                    ],
                    "phash":           rec.phash,
                    "screenshot_path": str(rec.screenshot_path),
                    "discovered_at":   rec.discovered_at,
                }
                for rec in self._visited.values()
            ],
            "stats": {
                "screens_found":   self._stats.screens_found,
                "screens_skipped": self._stats.screens_skipped,
                "taps_total":      self._stats.taps_total,
                "elapsed_sec":     self._stats.elapsed_sec,
            },
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"[CRAWL] サマリー保存: {path}")

    def _finalize_session(self) -> None:
        """クロール終了時にサマリー JSON を保存し、DB セッションを completed にする。"""
        # JSON サマリーを evidence ディレクトリに保存
        if self._visited:
            first_rec = next(iter(self._visited.values()))
            evidence_dir = first_rec.screenshot_path.parent
            summary_path = evidence_dir / "crawl_summary.json"
            try:
                self.save_summary_json(summary_path)
            except Exception as e:
                logger.warning(f"[CRAWL] サマリー保存失敗: {e}")

        # DB セッション終了処理
        if self._db_conn is None or self._crawl_session_id is None:
            return
        try:
            with self._db_conn.cursor() as cur:
                cur.execute(
                    """UPDATE crawl_sessions
                       SET status='completed', screens_found=%s, ended_at=NOW()
                       WHERE id=%s""",
                    (self._stats.screens_found, self._crawl_session_id),
                )
            self._db_conn.commit()
            self._db_conn.close()
            logger.info(f"[DB] セッション完了: id={self._crawl_session_id}")
        except Exception as e:
            logger.warning(f"[DB] セッション終了エラー: {e}")

    # ----------------------------------------------------------
    # ユーティリティ
    # ----------------------------------------------------------

    def _is_time_up(self) -> bool:
        return (time.time() - self._start_time) >= self.config.max_duration_sec

    def elapsed_sec(self) -> float:
        return time.time() - self._start_time

    def summary(self) -> str:
        """現在の探索状況をサマリー文字列で返す。"""
        lines = [
            "=" * 60,
            f"  クロールサマリー",
            "=" * 60,
            f"  経過時間    : {self.elapsed_sec():.1f}秒",
            f"  発見画面数  : {self._stats.screens_found} 件",
            f"  skip (既訪問): {self._stats.screens_skipped} 件",
            f"  タップ総数  : {self._stats.taps_total} 回",
            f"  DB 保存     : {self._stats.db_saves} 件",
        ]
        if self._visited:
            lines.append(f"\n  発見した画面一覧:")
            for i, rec in enumerate(self._visited.values(), 1):
                lines.append(
                    f"    [{i:02d}] 深さ={rec.depth}  {rec.title!r}"
                    f"  ({len(rec.tappable_items)}件タップ候補)"
                    f"  {rec.fingerprint[:8]}…"
                )
        lines.append("=" * 60)
        return "\n".join(lines)


# ============================================================
# ユーティリティ
# ============================================================

def _safe_name(text: str, max_len: int = 10) -> str:
    """ファイル名に使える安全な短縮文字列を生成する。"""
    safe = "".join(c for c in text if c.isalnum() or c in "_-")
    return safe[:max_len] or "item"
