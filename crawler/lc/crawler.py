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
import re
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

# タップ候補から除外するシンボルと後退ナビゲーション語
# (クローラー自身が back() で後退処理するため「戻る」系は除外)
_TAPPABLE_SKIP: frozenset[str] = frozenset({
    '>', '›', '<', '‹', '…', 'Q',
    '戻る', 'Back',
})

# テキストにこれらを含む要素は位置に関わらずタップ候補とする（操作系キーワード）
_ACTION_KEYWORDS: frozenset[str] = frozenset({
    # 日本語アクション
    'OK', '完了', '次へ', '保存', '閉じる', '削除', '確認', '開く',
    'リセット', '転送', '続ける', '許可', '追加', '選択', '設定する',
    'キャンセル', '取り消し', '送信', '適用',
    # 英語アクション
    'Done', 'Save', 'Next', 'Close', 'Delete', 'Apply',
    'Confirm', 'Cancel', 'Submit', 'Continue', 'Allow',
})

# 単一文字でもタップ候補とするアイコン類似文字
_ICON_CHARS: frozenset[str] = frozenset({
    '⚙', '⚙️', '☰', '＋', '+', '⋮', '✕', '×',
})

# 指紋生成時に除去する数字パターン（時刻・所持金・レベル等の可変数値を無視するため）
_DIGIT_RE = re.compile(r'\d+')


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
        self._visited:   dict[str, ScreenRecord] = {}  # "{title}@{fingerprint}" → ScreenRecord
        self._nav_stack: list[str] = []               # 現在の探索経路（指紋スタック）
        self._stats  = CrawlStats()
        self._start_time = time.time()

        # evidence ディレクトリ（ドライバーのセッションディレクトリと共有）
        # AppiumDriver が既に作成しているが、ここでも保証する
        self._evidence_dir: Path = driver._evidence_dir
        self._evidence_dir.mkdir(parents=True, exist_ok=True)

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
        _vkey = f"{record.title}@{record.fingerprint}"
        if _vkey in self._visited:
            prev = self._visited[_vkey]
            logger.info(
                f"[CRAWL] skip（既訪問）: {record.title!r}"
                f" (深さ{prev.depth} で既に訪問済み)"
            )
            self._stats.screens_skipped += 1
            return

        # --- 新規画面として登録・保存 ---
        self._visited[_vkey] = record
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

        # --- タップ候補 0 件 = スタック検知 ---
        if not record.tappable_items:
            logger.warning(f"[CRAWL] ⚠ タップ候補なし: {record.title!r} — エビデンス保存")
            self._save_evidence("no_tappable_items", record.ocr_results, record.title)

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
            # 固定待機の代わりに phash で画面静止を検知
            # Live2D アニメーション等がある場合も最大 3s でタイムアウトして続行
            settled = self.driver.wait_until_stable()
            if not settled:
                # タイムアウト = 3s 経過しても画面が静止しなかった（ループアニメ等）
                logger.warning(f"[CRAWL] ⚠ Settling タイムアウト: tap={text!r} — エビデンス保存")
                self._save_evidence("settling_timeout", record.ocr_results, record.title)

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

        fingerprint = self._generate_fingerprint(ocr_results)
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

    def _generate_fingerprint(self, ocr_results: list[dict]) -> str:
        """
        数字を除外したテキスト群から MD5 指紋を生成する。

        `_screen_fingerprint` の上位互換。時刻・所持金・残弾数・レベル等の
        可変数値をハッシュ前に _DIGIT_RE で除去することで、「同じ画面構造でも
        数値が変わるたびに別画面と誤認識される」副作用を防ぐ。

        判定方式:
          1. 画面サイズを OCR bounding-box 座標から動的に推定（絶対値ではなく相対比率）
          2. ステータスバー (y < screen_h × 5%) を除外
          3. 信頼スコア < 0.75 を除外
          4. 各テキストから数字列を除去し、残りが 2 文字以上のものだけを採用
          5. 採用テキストをソートして結合し、MD5 hex 先頭 16 文字を返す

        Returns:
            16 文字の hex 文字列 (例: "a3f1c2d9e8b70451")
        """
        all_ys = [p[1] for r in ocr_results for p in r["box"]]
        screen_h = int(max(all_ys) * 1.05) if all_ys else 2000

        texts: list[str] = []
        for r in ocr_results:
            if r["confidence"] < 0.75:
                continue
            if r["center"][1] < screen_h * 0.05:   # ステータスバー除外
                continue
            stripped = _DIGIT_RE.sub("", r["text"]).strip()
            if len(stripped) > 1:
                texts.append(stripped)

        fp_str = "|".join(sorted(texts))
        return hashlib.md5(fp_str.encode("utf-8")).hexdigest()[:16]

    # ----------------------------------------------------------
    # タイトル抽出
    # ----------------------------------------------------------

    def _extract_title(self, ocr_results: list[dict]) -> str:
        """
        画面タイトルを OCR 結果から推定する。座標絶対値に依存しない汎用ロジック。

        【アルゴリズム概要】
        画面サイズを OCR の bounding-box 座標群から推定し、すべての判定を
        「画面高さ・幅に対する相対比率」で行う。ハードコードされたピクセル値は使用しない。

        Step 1 — Large Title:
          画面上部 (y < 30%) かつ bounding-box 高さが画面高さの 3% 以上の候補の中で
          最もフォントが大きいテキストをタイトルと推論する。
          → iOS Large Title (~34pt @3x ≈ 3.3%)、ゲームのタイトルバーなど。
          → nav-bar 戻るボタン (~17pt @3x ≈ 2.5%) は高さ不足で自動除外される。

        Step 2 — Nav-bar center title (fallback):
          画面上部 (y < 20%) かつ水平中央寄り (x: 20%〜80%) の候補の中で
          信頼スコアが最も高いテキストをタイトルと推論する。
          → iOS ナビゲーションバーの中央タイトル (例: 「情報」「言語と地域」)。

        EXCLUDE_TEXTS (戻る/Done/Edit/キャンセル 等) は共通フィルタで除外する。
        """

        def _box_height(r: dict) -> float:
            """bounding-box の高さ（フォントサイズの代理指標）を返す。"""
            ys = [p[1] for p in r["box"]]
            return max(ys) - min(ys)

        # 画面サイズを OCR bounding-box 座標群から推定
        # (画面下端 / 右端テキストの座標 + 5% のマージン)
        all_ys = [p[1] for r in ocr_results for p in r["box"]]
        all_xs = [p[0] for r in ocr_results for p in r["box"]]
        screen_h = int(max(all_ys) * 1.05) if all_ys else 2000
        screen_w = int(max(all_xs) * 1.05) if all_xs else 1000

        # 共通フィルタ: 信頼スコア / テキスト長 / ナビゲーション除外ワード / ステータスバー
        base = [
            r for r in ocr_results
            if r["confidence"] > 0.8
            and 1 < len(r["text"]) <= 20
            and r["text"].strip() not in EXCLUDE_TEXTS
            and r["center"][1] > screen_h * 0.05      # ステータスバー (上端 5%) 除外
        ]

        # Step 1: Large Title — 上部 30% かつ文字高さ 3% 以上
        large = [
            r for r in base
            if r["center"][1] < screen_h * 0.30
            and _box_height(r) >= screen_h * 0.03
        ]
        if large:
            return max(large, key=_box_height)["text"]

        # Step 2: Nav-bar center title — 上部 20% かつ水平中央 20%〜80%
        nav = [
            r for r in base
            if r["center"][1] < screen_h * 0.20
            and screen_w * 0.20 <= r["center"][0] <= screen_w * 0.80
        ]
        if nav:
            return max(nav, key=lambda r: r["confidence"])["text"]

        return "unknown"

    # ----------------------------------------------------------
    # タップ候補抽出
    # ----------------------------------------------------------

    def _find_tappable_items(self, ocr_results: list[dict]) -> list[dict]:
        """
        OCR 結果からタップ可能な UI 項目を自律的に抽出する。

        ハードコードされたピクセル値を使わず、画面サイズに対する相対比率と
        テキストの意味的・視覚的特徴で判定する。4 つの検出パスを持つ:

        Path A — キーワード優先:
          _ACTION_KEYWORDS に含まれるテキスト (「完了」「リセット」「転送」等) を
          画面上の位置に関わらずタップ候補とする。

        Path B — フッターボタン:
          画面下部 (y > 80%) にある要素を「アクションボタン」として優先取得する。
          iOS 設定の「転送またはiPhoneをリセット」などが該当する。

        Path C — シェブロン行:
          右端 (x > 85%) で検出された「>」「›」と同じ水平バンド (±4%) にある
          左側テキストをリスト行のラベルとして取得する。

        Path D — コンテンツエリア (フォールバック):
          シェブロンが疎な場合 (< 5 件) に発動。ナビバー以下かつフッター以上の
          コンテンツ領域で、Large Title でも説明文でもない左寄りテキストを取得する。

        共通除外:
          - ステータスバー上端 (y < 5%)
          - 右端シェブロン・アイコン (x > 85%)
          - _TAPPABLE_SKIP (「戻る」等)
          - 低信頼スコア / 空テキスト
        """

        def _box_h(r: dict) -> float:
            ys = [p[1] for p in r["box"]]
            return max(ys) - min(ys)

        # 画面サイズを bounding-box 座標群から動的推定
        all_ys = [p[1] for r in ocr_results for p in r["box"]]
        all_xs = [p[0] for r in ocr_results for p in r["box"]]
        screen_h = int(max(all_ys) * 1.05) if all_ys else 2000
        screen_w = int(max(all_xs) * 1.05) if all_xs else 1000

        # 相対境界値
        STATUS_TOP  = screen_h * 0.05   # ステータスバー下端
        NAV_BOTTOM  = screen_h * 0.15   # ナビバー下端
        FOOTER_TOP  = screen_h * 0.80   # フッターエリア上端
        RIGHT_EDGE  = screen_w * 0.85   # 右端 (シェブロン・アイコン帯)
        LEFT_MAX    = screen_w * 0.80   # 左寄りテキストの右限
        LARGE_FONT  = screen_h * 0.03   # Large Title の最小フォント高さ
        CHEVRON_R   = screen_h * 0.04   # シェブロン行の近傍半径 (±4%)
        DESC_X_MIN  = screen_w * 0.40   # 説明文の中央寄り判定閾値 (x > 40%)

        # シェブロン (>) の y 座標を収集
        chevron_ys: list[int] = [
            r["center"][1]
            for r in ocr_results
            if r["text"].strip() in ('>', '›') and r["center"][0] > RIGHT_EDGE
        ]

        seen: set[str] = set()
        result: list[dict] = []

        def _emit(r: dict) -> None:
            t = r["text"].strip()
            if t and t not in seen:
                seen.add(t)
                result.append(r)

        for r in ocr_results:
            text = r["text"].strip()
            cx, cy = r["center"]
            conf   = r["confidence"]

            # ── 共通ゲート ──────────────────────────────────────────────
            if conf < self.config.min_confidence:
                continue
            # 空 / 極端に短い (アイコン文字は例外)
            if not text or (len(text) <= 1 and text not in _ICON_CHARS):
                continue
            if text in _TAPPABLE_SKIP:
                continue
            if cy < STATUS_TOP:         # ステータスバー内
                continue
            if cx > RIGHT_EDGE:         # 右端 (シェブロン帯)
                continue

            # ── Path A: キーワード ───────────────────────────────────────
            if any(kw in text for kw in _ACTION_KEYWORDS):
                _emit(r)
                continue

            # ── Path B: フッターボタン ────────────────────────────────────
            if cy > FOOTER_TOP:
                _emit(r)
                continue

            # ── Path C: シェブロン行 ──────────────────────────────────────
            if chevron_ys and any(abs(cy - chy) <= CHEVRON_R for chy in chevron_ys):
                if _box_h(r) < LARGE_FONT:   # Large Title は除外
                    _emit(r)
                continue

            # ── Path D: コンテンツエリア (フォールバック) ─────────────────
            if len(chevron_ys) < 5:
                if (NAV_BOTTOM <= cy <= FOOTER_TOP
                        and cx < LEFT_MAX
                        and 1 < len(text) <= 20
                        and _box_h(r) < LARGE_FONT          # Large Title 除外
                        # 中央寄り説明文 (x>40% かつ len>12) を除外
                        and not (cx > DESC_X_MIN and len(text) > 12)):
                    _emit(r)

        logger.debug(
            f"[TAPPABLE] {len(result)}件  "
            f"chevrons={len(chevron_ys)}  screen={screen_w}×{screen_h}"
        )
        return result

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

    # ----------------------------------------------------------
    # 異常系エビデンス保存
    # ----------------------------------------------------------

    def _save_evidence(
        self,
        reason: str,
        ocr_results: Optional[list] = None,
        current_title: str = "unknown",
    ) -> None:
        """
        スタック・異常検知が発生した瞬間のスクリーンショットと OCR コンテキストを保存する。

        保存パス:
            evidence/{session_id}/{YYYYMMDD_HHMMSS}_{reason}.png
            evidence/{session_id}/{YYYYMMDD_HHMMSS}_{reason}.json

        JSON スキーマ:
            {
              "timestamp":   "ISO8601",
              "reason":      "no_tappable_items" | "settling_timeout" | ...,
              "title":       "推定画面タイトル",
              "ocr_results": [{"text", "confidence", "center"}, ...]
            }

        Args:
            reason       : 保存理由 (ファイル名に使用)
            ocr_results  : 保存時点の OCR 結果。省略時は空リストとして記録する。
            current_title: 保存時点の推定画面タイトル
        """
        import json as _json

        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = "".join(c for c in reason if c.isalnum() or c in "_-")[:24]
        stem = f"{ts}_{safe}"

        png_path  = self._evidence_dir / f"{stem}.png"
        json_path = self._evidence_dir / f"{stem}.json"

        # スクリーンショット保存
        try:
            self.driver.driver.save_screenshot(str(png_path))
        except Exception as e:
            logger.warning(f"[EVIDENCE] スクリーンショット保存失敗: {e}")
            return

        # OCR コンテキスト保存（軽量フィールドのみ）
        payload = {
            "timestamp":   datetime.now().isoformat(),
            "reason":      reason,
            "title":       current_title,
            "ocr_results": [
                {
                    "text":       r["text"],
                    "confidence": r["confidence"],
                    "center":     r["center"],
                }
                for r in (ocr_results or [])
            ],
        }
        try:
            json_path.write_text(
                _json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"[EVIDENCE] JSON 保存失敗: {e}")
            return

        logger.warning(
            f"[EVIDENCE] 異常検知エビデンス保存:"
            f" {png_path.name}  reason={reason!r}  title={current_title!r}"
        )

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
