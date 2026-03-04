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

from typing import TYPE_CHECKING

from .core import AppHealthMonitor, StuckDetector, FrontierTracker
from .driver import AppiumDriver
from .ocr import run_ocr, find_best, format_results
from .utils import compute_phash, phash_distance

if TYPE_CHECKING:
    from driver_adapter import BaseDriver

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


def _iou(a: tuple, b: tuple) -> float:
    """
    2 つの XYWH 矩形の IoU (Intersection over Union) を返す。

    NMS (Non-Maximum Suppression) での重複判定に使用する。

    Args:
        a: (x, y, w, h) タプル — 左上座標 + 幅・高さ
        b: (x, y, w, h) タプル — 左上座標 + 幅・高さ

    Returns:
        0.0〜1.0 の IoU 値。重複なしのとき 0.0、完全一致のとき 1.0。
    """
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix  = max(ax, bx)
    iy  = max(ay, by)
    ix2 = min(ax + aw, bx + bw)
    iy2 = min(ay + ah, by + bh)
    inter = max(0, ix2 - ix) * max(0, iy2 - iy)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


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
    # --- OCR / 検出しきい値 ---
    # min_confidence: 0.5 はゲーム UI（テクスチャ混在・斜体フォント）向け。
    #   システム UI の精度を優先する場合は 0.6 に戻すこと。
    min_confidence:   float = 0.5    # OCR 最低信頼スコア (ゲーム UI 対応で 0.6→0.5)
    # icon_threshold: 0.75 はゲームアイコン（アート調ボタン）向け。
    #   誤検出が増える場合は 0.80 に戻すこと。
    icon_threshold:   float = 0.75   # テンプレートマッチング検出閾値 (0.80→0.75)
    # ゲームタイトル（SQLite保存時に使用）
    game_title:       str   = "Unknown Game"
    # 動作モード（crawl_summary.json に記録、管理画面で実機/シミュ判別に使用）
    device_mode:      str   = "SIMULATOR"  # "SIMULATOR" または "MIRROR"
    # SQLite DB パス（グローバル pHash 重複排除・増分探索用）
    sqlite_db_path:   Optional[str] = None
    # 自己修復パラメーター
    max_heal_retries:     int  = 2      # アプリ復帰最大試行回数
    # anti_stuck_threshold: ゲームのローディング画面を考慮して 3 を推奨。
    #   同一画面で N 回 dead-end になった後にスワイプ/長押しを試みる。
    anti_stuck_threshold: int  = 3      # スタック検知閾値 (ゲームロード考慮で 2→3)
    smart_backtrack:      bool = True   # フロンティアへのスマートバックトラック有効
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
        logger.info("%d 画面を発見", stats.screens_found)
    """

    def __init__(self, driver: "AppiumDriver | BaseDriver", config: CrawlerConfig):
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

        # アイコンテンプレート: {stem: np.ndarray(グレースケール)}
        # assets/templates/*.png を起動時に一括読み込み
        self._icon_templates: dict[str, object] = {}
        self._load_icon_templates()

        # グローバル pHash セット（同一ゲームの過去セッション画面 — 増分探索用）
        self._known_phashes: set[str] = set()
        self._load_known_phashes()

        # 自己修復コンポーネント
        _bundle_id = os.environ.get("IOS_BUNDLE_ID", "")
        self._health_monitor = AppHealthMonitor(
            driver, _bundle_id, max_retries=config.max_heal_retries
        )
        self._stuck_detector  = StuckDetector(threshold=config.anti_stuck_threshold)
        self._frontier_tracker = FrontierTracker()
        # _crawl_impl → 親の tap_map 記録用サイドチャネル
        # save/restore により深い再帰でも正しい直近の子 fp を取得できる
        self._pending_child_fp: Optional[str] = None

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
        主 DFS 終了後、時間が残っていればフロンティアへスマートバックトラックを試みる。

        Returns:
            CrawlStats: 実行統計
        """
        self._crawl_impl(depth, parent_fp)

        # スマートバックトラック: フロンティア（max_depth で打ち切られた画面）を再探索
        if self.config.smart_backtrack and not self._is_time_up():
            self._smart_backtrack_loop()

        self._stats.elapsed_sec = time.time() - self._start_time
        self._finalize_session()
        return self._stats

    def get_visited_screens(self) -> list[ScreenRecord]:
        """訪問済み画面の一覧を返す。"""
        return list(self._visited.values())

    # ----------------------------------------------------------
    # DFS 実装
    # ----------------------------------------------------------

    def _crawl_impl(self, depth: int, parent_fp: Optional[str]) -> bool:
        """
        再帰 DFS の本体。

        Returns:
            True  — 画面遷移が発生した（新規 or 既訪問の別画面へ移動）
                     → 呼び出し元は back() で元の画面に戻る必要がある
            False — 画面遷移なし（同一画面のまま）
                     → 呼び出し元は back() 不要（呼ぶと1階層多く戻ってしまう）
        """

        # --- 停止条件 ---
        if self._is_time_up():
            logger.info("[CRAWL] ⏱ 時間制限に達しました")
            return False
        if depth >= self.config.max_depth:
            logger.info(f"[CRAWL] 最大深さ {depth} に到達 — これ以上深く探索しません")
            return False

        # --- アプリ生存確認（クラッシュ/バックグラウンド落ちを自動復帰）---
        if not self._health_monitor.check_and_heal():
            logger.error("[CRAWL] ❌ アプリ不応答 — このブランチをスキップ")
            return False

        # --- 現在画面を取得・同定 ---
        record = self._snapshot_current_screen(depth, parent_fp)
        if record is None:
            return False

        # サイドチャネル: 親の tap_map 記録用に自分の fingerprint をセット
        self._pending_child_fp = record.fingerprint

        # --- グローバル pHash 重複チェック（増分探索: 前回セッションで既知の画面をスキップ）---
        if record.phash and self._known_phashes:
            min_dist = min(
                (phash_distance(record.phash, known) for known in self._known_phashes),
                default=999,
            )
            if min_dist < PHASH_THRESHOLD:
                logger.info(
                    f"[PHASH_DUP] 前回セッション既知画面 (dist={min_dist}): {record.title!r} — スキップ"
                )
                self._stats.screens_skipped += 1
                return record.fingerprint != parent_fp

        # --- phash ログ (診断用。セッション内での同一性チェック) ---
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
            return record.fingerprint != parent_fp

        # --- 新規画面として登録・保存 ---
        self._visited[_vkey] = record
        self._nav_stack.append(record.fingerprint)
        self._stats.screens_found += 1

        # フロンティアトラッカーに親子関係を記録（スマートバックトラック用）
        self._frontier_tracker.record_nav(record.fingerprint, parent_fp)

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
            count = self._stuck_detector.record(record.fingerprint)
            logger.info(f"[STUCK] スタックカウント: {count}  fp={record.fingerprint[:8]}")

            # スワイプ・長押しで突破を試みる
            if self._try_unstuck_gestures(record):
                # ジェスチャー後に再スナップショット
                new_record = self._snapshot_current_screen(depth, parent_fp)
                if new_record and new_record.tappable_items:
                    logger.info(
                        f"[UNSTUCK] ✅ ジェスチャー後タップ候補出現: {len(new_record.tappable_items)} 件"
                    )
                    record = new_record  # 更新された record で継続
                    self._stuck_detector.reset(record.fingerprint)

            # root (depth=0) かつタップ候補なし → デッドエンド脱出
            if depth == 0 and not record.tappable_items:
                self._escape_dead_end()

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

            # タップ前スクリーンショットにオーバーレイ（DEBUG_DRAW_OPS=1 時）
            self._annotate_screenshot(record.screenshot_path, px, py, text)

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
            settled = self.driver.wait_until_stable()
            if not settled:
                logger.warning(f"[CRAWL] ⚠ Settling タイムアウト: tap={text!r} — エビデンス保存")
                self._save_evidence("settling_timeout", record.ocr_results, record.title)

            # 子画面を再帰探索（サイドチャネル save/restore で直近の子 fp を取得）
            _saved_child_fp = self._pending_child_fp
            self._pending_child_fp = None

            _did_navigate = self._crawl_impl(depth + 1, record.fingerprint)

            # 子が報告した fingerprint を tap_map に記録
            child_fp = self._pending_child_fp
            self._pending_child_fp = _saved_child_fp  # 親へ向けて restore

            if child_fp:
                self._frontier_tracker.record_tap(record.fingerprint, text, child_fp)

            if _did_navigate:
                # 遷移先から元の画面に戻る
                if not self._is_time_up():
                    self.driver.back()
                    self.driver.wait(self.config.wait_after_back)
                    logger.info(f"[CRAWL]  ← 戻る完了: {text!r}")
            else:
                # 遷移なし: back() せずに次のタップ候補へ
                logger.info(f"[CRAWL]  🔄 [NO_NAV] 非遷移タップ: {text!r} — back() スキップ")

        self._nav_stack.pop()
        return True

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

        # アイコン検出（テンプレートマッチング）
        # ocr_results は純粋に OCR のみを保持し、指紋・タイトルに影響させない。
        # テンプレート一致 = タップ対象確定なので _find_tappable_items を経由せず直接追加。
        # 位置フィルタ（RIGHT_EDGE 等）はアイコンに適さないためバイパスする。
        icon_results = self._detect_icons(shot_path)
        for icon in icon_results:
            tappable.append(icon)

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
    # グローバル pHash ロード（増分探索）
    # ----------------------------------------------------------

    def _load_known_phashes(self) -> None:
        """SQLite DB から同一ゲームの既知 pHash をロードする（増分探索用）。"""
        db_path = self.config.sqlite_db_path
        if not db_path or not Path(db_path).exists():
            return
        try:
            import sqlite3 as _sqlite3
            from tools.import_to_sqlite import get_project_phashes
            conn = _sqlite3.connect(db_path)
            self._known_phashes = get_project_phashes(conn, self.config.game_title)
            conn.close()
            logger.info(
                f"[PHASH] 既知 pHash ロード: {len(self._known_phashes)} 件"
                f"  game_title={self.config.game_title!r}"
            )
        except Exception as e:
            logger.warning(f"[PHASH] pHash ロード失敗 (スキップ): {e}")

    # ----------------------------------------------------------
    # Anti-Stuck: スワイプ・長押しによる UI スタック突破
    # ----------------------------------------------------------

    def _try_unstuck_gestures(self, record: "ScreenRecord") -> bool:
        """
        同一画面でスタックが繰り返された場合、スワイプ・長押しで突破を試みる。

        【ジェスチャー戦略】
          threshold 回目   → スワイプ（画面を下から上に 60% 幅でスクロール）
          threshold×2 回目 → 長押し（画面中央付近をランダムにずらして長押し）
          threshold×3 回以上 → 諦め（ジェスチャー打ち切り）

        Returns:
            True  — ジェスチャー実行（画面が変わった可能性あり）
            False — 閾値未到達 or 全ジェスチャー失敗
        """
        import random

        fp = record.fingerprint
        if not self._stuck_detector.should_swipe(fp):
            return False

        if self._stuck_detector.is_hopeless(fp):
            logger.warning(f"[UNSTUCK] 打ち切り — ジェスチャーが {self._stuck_detector.get_count(fp)} 回効かず: {fp[:8]}")
            return False

        # デバイス論理サイズの取得 (MirroringDriver → _device_width, それ以外 → Appium window/size)
        w = int(getattr(self.driver, '_device_width',  None) or 0)
        h = int(getattr(self.driver, '_device_height', None) or 0)
        if not w or not h:
            try:
                size = self.driver.driver.get_window_size()
                w, h = size["width"], size["height"]
            except Exception:
                w, h = 393, 852

        try:
            # スワイプ: 画面下 65% → 上 25%（リスト更新・隠れた要素を出す）
            sx  = w // 2 + random.randint(-40, 40)
            sy1 = int(h * 0.65) + random.randint(-20, 20)
            sy2 = int(h * 0.25) + random.randint(-20, 20)
            self.driver.driver.swipe(sx, sy1, sx, sy2, 400)
            self.driver.wait(1.5)
            logger.info(f"[UNSTUCK] スワイプ: ({sx},{sy1})→({sx},{sy2})")

            # 長押し: stuck_count >= threshold×2 のみ
            if self._stuck_detector.should_long_press(fp):
                cx = w // 2 + random.randint(-60, 60)
                cy = h // 2 + random.randint(-80, 80)
                if os.environ.get("DEVICE_MODE", "").upper() == "ANDROID":
                    # Android: mobile: longClick (UiAutomator2)
                    try:
                        self.driver.driver.execute_script(
                            "mobile: longClick",
                            {"x": cx, "y": cy, "duration": 1500},
                        )
                    except Exception:
                        pass
                else:
                    # iOS: mobile: touchAndHold (XCUITest)
                    self.driver.driver.execute_script(
                        "mobile: touchAndHold",
                        {"x": cx, "y": cy, "duration": 1.5},
                    )
                self.driver.wait(1.5)
                logger.info(f"[UNSTUCK] 長押し: ({cx},{cy})")

            return True
        except Exception as e:
            logger.debug(f"[UNSTUCK] ジェスチャー失敗: {e}")
            return False

    # ----------------------------------------------------------
    # スマートバックトラック: フロンティアへの再探索
    # ----------------------------------------------------------

    def _smart_backtrack_loop(self) -> None:
        """
        主 DFS 終了後にフロンティア（max_depth で打ち切られた画面）を再探索する。

        【フロンティアの定義】
          depth == config.max_depth - 1 かつタップ候補あり の画面。
          これらは max_depth の制限で子画面が未探索のまま残っている。

        【再探索手順】
          1. フロンティアを depth 昇順にソート
          2. 各フロンティアへのナビゲーションレシピを構築
          3. アプリを activate_app で再起動
          4. レシピ通りにタップして遷移 → _crawl_impl() を 1 段深く呼ぶ
        """
        if self._is_time_up():
            return

        frontier = [
            rec for rec in self._visited.values()
            if rec.depth >= self.config.max_depth - 1
            and rec.tappable_items
        ]

        if not frontier:
            logger.debug("[BACKTRACK] フロンティアなし — スマートバックトラックをスキップ")
            return

        logger.info(
            f"[BACKTRACK] フロンティア発見: {len(frontier)} 画面"
            f" — 最大深さ {self.config.max_depth} を延長して再探索"
        )

        # フロンティアを最浅順に処理（上位層から探索する方が効率的）
        for target in sorted(frontier, key=lambda r: r.depth):
            if self._is_time_up():
                break

            path = self._frontier_tracker.build_path_to(target.fingerprint)
            if not path:
                logger.warning(f"[BACKTRACK] パス再構築失敗: {target.title!r}")
                continue

            recipe = self._frontier_tracker.get_nav_recipe(path, self._visited)
            if not recipe and len(path) > 1:
                logger.warning(f"[BACKTRACK] ナビレシピ構築失敗: {target.title!r}")
                continue

            logger.info(
                f"[BACKTRACK] → {target.title!r}  depth={target.depth}"
                f"  path={len(path)} ステップ"
            )

            if self._navigate_to_frontier(recipe):
                # フロンティアから 1 段深く探索（max_depth を一時的に +1）
                _orig_max = self.config.max_depth
                try:
                    object.__setattr__(self.config, 'max_depth', _orig_max + 1)
                    self._crawl_impl(target.depth + 1, target.fingerprint)
                except AttributeError:
                    # dataclass が frozen の場合はそのまま呼ぶ
                    self._crawl_impl(target.depth + 1, target.fingerprint)
                finally:
                    try:
                        object.__setattr__(self.config, 'max_depth', _orig_max)
                    except AttributeError:
                        pass

                # バックトラック先からルートへ戻る
                if not self._is_time_up():
                    for _ in range(target.depth + 1):
                        self.driver.back()
                        self.driver.wait(self.config.wait_after_back)

    def _navigate_to_frontier(
        self,
        recipe: list[tuple[str, dict]],
    ) -> bool:
        """
        フロンティアへのナビゲーションを再生する。

        1. activate_app でアプリをルート状態に戻す
        2. recipe に従って順にタップして遷移する

        Args:
            recipe: [(item_text, item_dict), ...] — FrontierTracker.get_nav_recipe() の結果

        Returns:
            True  — ナビゲーション成功（フロンティアに到達）
            False — 失敗（bundle_id 未設定 or activate_app 失敗 or タップ失敗）
        """
        bundle_id = os.environ.get("IOS_BUNDLE_ID", "")
        if not bundle_id:
            logger.warning("[BACKTRACK] IOS_BUNDLE_ID 未設定 — ナビゲーション不可")
            return False

        # アプリをルートに戻す
        try:
            self.driver.driver.activate_app(bundle_id)
            self.driver.wait(2.0)
            logger.info(f"[BACKTRACK] アプリ再起動: {bundle_id!r}")
        except Exception as e:
            logger.warning(f"[BACKTRACK] activate_app 失敗: {e}")
            return False

        # recipe が空 = root が target → ナビゲーション不要
        if not recipe:
            return True

        # 各ステップをタップして遷移
        for text, item in recipe:
            if self._is_time_up():
                return False
            px, py = item["center"]
            logger.info(f"[BACKTRACK] → タップ: {text!r}  pixel=({px},{py})")
            try:
                self.driver.tap_ocr_coordinate(
                    px, py,
                    action_name=f"backtrack_{_safe_name(text)}",
                    ocr_data={"ocr_boxes": [{"text": text, "confidence": 1.0, "box": item["box"]}]},
                )
                self._stats.taps_total += 1
                self.driver.wait(self.config.wait_after_tap)
            except Exception as e:
                logger.warning(f"[BACKTRACK] タップ失敗: {e}")
                return False

        logger.info("[BACKTRACK] ✅ フロンティアへのナビゲーション完了")
        return True

    # ----------------------------------------------------------
    # DEBUG_DRAW_OPS: タップ座標オーバーレイ
    # ----------------------------------------------------------

    def _annotate_screenshot(
        self,
        shot_path: Path,
        px: int,
        py: int,
        action: str = "tap",
    ) -> None:
        """
        DEBUG_DRAW_OPS=1 のとき、スクリーンショットにタップ座標を赤円でオーバーレイする。

        タップ前の before.png に「ここをタップした」を記録する用途。元ファイルを上書き保存する。
        """
        if not os.environ.get("DEBUG_DRAW_OPS"):
            return
        try:
            import cv2 as _cv2
            img = _cv2.imread(str(shot_path))
            if img is None:
                return

            # --- プロフェッショナル品質タップマーカー ---
            # 1. ドロップシャドウ（黒・太め）
            _cv2.circle(img, (px + 2, py + 2), 24, (0, 0, 0), 5)
            # 2. 白背景リング（視認性確保）
            _cv2.circle(img, (px, py), 24, (255, 255, 255), 7)
            # 3. 赤リング（メインマーカー）
            _cv2.circle(img, (px, py), 24, (40, 40, 220), 3)
            # 4. 中心ドット: 白インナー + 赤コア
            _cv2.circle(img, (px, py), 7, (255, 255, 255), -1)
            _cv2.circle(img, (px, py), 4, (40, 40, 220), -1)
            # 5. クロスヘア（精密位置表示）
            _cv2.line(img, (px - 10, py), (px + 10, py), (40, 40, 220), 1)
            _cv2.line(img, (px, py - 10), (px, py + 10), (40, 40, 220), 1)
            # 6. テキスト: 黒アウトライン + 白本体（どんな背景でも読める）
            label = action[:20]
            tx, ty = px + 30, py + 6
            _cv2.putText(img, label, (tx, ty), _cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
            _cv2.putText(img, label, (tx, ty), _cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

            _cv2.imwrite(str(shot_path), img)
            logger.debug(f"[DEBUG_DRAW_OPS] タップ描画: ({px},{py}) {action!r}")
        except Exception as e:
            logger.debug(f"[DEBUG_DRAW_OPS] 描画スキップ: {e}")

    # ----------------------------------------------------------
    # デッドエンド脱出（スタック時リカバリ）
    # ----------------------------------------------------------

    def _escape_dead_end(self) -> bool:
        """
        root (depth=0) でタップ候補がない場合に脱出を試みる。

        1. activate_app() でアプリをフォアグラウンドに戻す
        2. iOS ホームボタン (XCUITest 'mobile: pressButton') を押す
        3. いずれも失敗したら False を返す

        Returns:
            True  — 脱出成功
            False — 脱出失敗
        """
        logger.warning("[DEADEND] root でタップ候補なし — 脱出を試みます")
        is_android = os.environ.get("DEVICE_MODE", "").upper() == "ANDROID"
        bundle_id = os.environ.get("ANDROID_APP_PACKAGE" if is_android else "IOS_BUNDLE_ID", "")

        # 1. activate_app (iOS) / launch_app (Android)
        if bundle_id:
            try:
                if is_android:
                    self.driver.driver.activate_app(bundle_id)
                else:
                    self.driver.driver.activate_app(bundle_id)
                self.driver.wait(2.0)
                logger.info(f"[DEADEND] activate_app 成功: {bundle_id!r}")
                return True
            except Exception as e1:
                logger.debug(f"[DEADEND] activate_app 失敗: {e1}")

        if is_android:
            # 2a. Android: ホームキー (keycode 3) → アプリ再起動
            try:
                self.driver.driver.press_keycode(3)  # KEYCODE_HOME
                self.driver.wait(1.0)
                if bundle_id:
                    self.driver.driver.activate_app(bundle_id)
                    self.driver.wait(2.0)
                logger.info("[DEADEND] Android ホームキー + アプリ復帰 成功")
                return True
            except Exception as e2:
                logger.debug(f"[DEADEND] Android ホームキー失敗: {e2}")
        else:
            # 2b. iOS ホームボタン (XCUITest)
            try:
                self.driver.driver.execute_script("mobile: pressButton", {"name": "home"})
                self.driver.wait(2.0)
                logger.info("[DEADEND] ホームボタン 押下成功")
                return True
            except Exception as e2:
                logger.debug(f"[DEADEND] ホームボタン失敗: {e2}")

        logger.warning("[DEADEND] 脱出失敗 — クロールを継続します")
        return False

    # ----------------------------------------------------------
    # アイコン検出（テンプレートマッチング）
    # ----------------------------------------------------------

    def _load_icon_templates(self) -> None:
        """
        assets/templates/*.png をグレースケールで読み込み、self._icon_templates に格納する。

        ファイル名（拡張子なし）がテンプレート識別子になる。
        ディレクトリが存在しない場合や読み込み失敗時はスキップして続行する。
        """
        try:
            import cv2 as _cv2
        except ImportError:
            logger.debug("[ICON] opencv-python 未インストール → テンプレートマッチング無効")
            return

        templates_dir = Path(__file__).parent.parent / "assets" / "templates"
        if not templates_dir.is_dir():
            logger.debug(f"[ICON] テンプレートディレクトリなし: {templates_dir}")
            return

        for png in sorted(templates_dir.glob("*.png")):
            img = _cv2.imread(str(png), _cv2.IMREAD_GRAYSCALE)
            if img is None:
                logger.warning(f"[ICON] 読み込み失敗: {png.name}")
                continue
            self._icon_templates[png.stem] = img
            logger.info(f"[ICON] テンプレート登録: {png.name}  size={img.shape[1]}×{img.shape[0]}")

        logger.info(f"[ICON] {len(self._icon_templates)} 件のテンプレートを読み込みました")

    def _detect_icons(self, screenshot_path: Path) -> list[dict]:
        """
        テンプレートマッチングでアイコンボタンを検出し、OCR 互換形式で返す。

        【信頼度 (confidence) の設計方針】
        cv2.matchTemplate(TM_CCOEFF_NORMED) の出力スコアは -1.0〜+1.0 だが、
        実用的には閾値 (icon_threshold=0.80) 以上の範囲 [0.80, 1.00] のみを採用する。
        このスコアを OCR の confidence フィールドと同じ名前でそのまま保存することで、
        OCR 結果とアイコン検出結果を統一フォーマットで扱える。

        【NMS (Non-Maximum Suppression) の方針】
        同一テンプレートが近接位置に複数マッチする場合（圧縮アーティファクト等）、
        IoU >= 0.4 の後発候補をスコア降順で順次除去する。
        これにより「本物の複数アイコン（別位置）」を保持しながら
        「同一アイコンの偽複数検出」を除去する。

        Returns:
            list[dict]: 各要素は OCR 結果と同形式。
            {
              "text":       "icon:{template_stem}",  # 例: "icon:close_btn"
              "confidence": 0.923,                   # TM_CCOEFF_NORMED スコア
              "box":        [[x,y],[x+w,y],[x+w,y+h],[x,y+h]],
              "center":     [cx, cy],
            }
        """
        if not self._icon_templates:
            return []

        try:
            import cv2 as _cv2
            import numpy as _np
        except ImportError:
            return []

        img = _cv2.imread(str(screenshot_path), _cv2.IMREAD_GRAYSCALE)
        if img is None:
            logger.warning(f"[ICON] 画像読み込み失敗: {screenshot_path}")
            return []

        all_detections: list[dict] = []
        threshold = self.config.icon_threshold

        for name, tmpl in self._icon_templates.items():
            th, tw = tmpl.shape[:2]

            # テンプレートがスクリーンより大きい場合はスキップ
            if th > img.shape[0] or tw > img.shape[1]:
                logger.debug(f"[ICON] {name!r} はスクリーンより大きいためスキップ")
                continue

            # ── テンプレートマッチング ──────────────────────────────────
            # TM_CCOEFF_NORMED: 照明変動に強い正規化相互相関。
            # score_map の各セル = その位置を左上角としたときの一致スコア
            score_map = _cv2.matchTemplate(img, tmpl, _cv2.TM_CCOEFF_NORMED)

            # 閾値以上の全候補点 [(score, x, y), ...] を収集
            ys, xs = _np.where(score_map >= threshold)
            if len(xs) == 0:
                continue

            candidates: list[tuple[float, int, int]] = sorted(
                ((float(score_map[y, x]), int(x), int(y)) for y, x in zip(ys, xs)),
                key=lambda c: c[0],
                reverse=True,
            )

            # ── NMS: スコア降順でイテレートし IoU >= 0.4 の後発候補を除去 ──
            accepted: list[tuple[float, int, int]] = []
            for score, x, y in candidates:
                if all(_iou((x, y, tw, th), (ax, ay, tw, th)) < 0.4
                       for _, ax, ay in accepted):
                    accepted.append((score, x, y))

            # ── OCR 互換形式に変換して結果リストに追加 ────────────────
            for score, x, y in accepted:
                cx  = x + tw // 2
                cy  = y + th // 2
                box = [[x, y], [x + tw, y], [x + tw, y + th], [x, y + th]]
                all_detections.append({
                    "text":       f"icon:{name}",
                    "confidence": round(score, 4),
                    "box":        box,
                    "center":     [cx, cy],
                })
                logger.debug(
                    f"[ICON] 検出: {name!r}  score={score:.3f}"
                    f"  pos=({x},{y})  size={tw}×{th}"
                )

        if all_detections:
            logger.info(
                f"[ICON] {len(all_detections)} 件検出"
                f" (テンプレート {len(self._icon_templates)} 種)"
            )
        return all_detections

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

                    # アイコン検出結果は element_type='icon'、OCR要素は 'menu'
                    etype = "icon" if item["text"].startswith("icon:") else "menu"

                    cur.execute(
                        """INSERT IGNORE INTO ui_elements
                           (screen_id, element_type, label,
                            bbox_x, bbox_y, bbox_w, bbox_h,
                            is_tappable, confidence)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s)""",
                        (
                            record.db_screen_id,
                            etype,
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
            "session_id":  path.parent.name,
            "game_title":  self.config.game_title,
            "device_mode": self.config.device_mode,
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
