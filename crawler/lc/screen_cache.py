"""
screen_cache.py — pHash ベースの画面-アクションキャッシュ (知識ベース)

探索セッション間で「既知の画面→実行すべき操作」を記憶・再利用する。
類似画面（ハミング距離 < hash_threshold）が知識ベースにある場合は
キャッシュ済みアクションを即座に返す（CACHE_HIT）ことで、AI/OCR の
問い合わせをスキップして探索を高速化する。

【ファイル構成】
    knowledge_dir/
        {hash}.json            ← CachedSolution の永続化
        screenshots/{hash}.png ← 参照スクリーンショット

【JSON フォーマット】
    {
        "hash":            "a3f0c2e1b4d59876",
        "screenshot_path": "games/madodra/knowledge/screenshots/a3f0c2e1b4d59876.png",
        "actions": [
            {"type": "tap",   "x": 760, "y": 360, "label": "ok_button"},
            {"type": "swipe", "x1": 760, "y1": 500, "x2": 760, "y2": 100, "duration": 250},
            {"type": "back"},
            {"type": "wait",  "duration": 1.5}
        ],
        "success":    true,
        "title":      "チュートリアル歩行",
        "created_at": "2026-03-04T12:00:00",
        "hit_count":  5,
        "platform":   "android"
    }
"""
from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from .utils import compute_phash, phash_distance

logger = logging.getLogger(__name__)


# ============================================================
# CachedSolution — キャッシュエントリのデータモデル
# ============================================================

@dataclass
class CachedSolution:
    """
    知識ベースから取り出した画面-アクション解決策。

    `distance` は lookup() がクエリとの距離を計算して代入するフィールドであり、
    ディスク上の JSON には保存されない。
    """
    hash:            str            # pHash hex (16 文字)
    screenshot_path: str            # 参照スクリーンショットの絶対パス文字列
    actions:         list[dict]     # [{type, x, y, ...}, ...]
    success:         bool           # 最後の実行が成功したか
    title:           str            # 推定画面タイトル
    created_at:      str            # ISO8601
    hit_count:       int            # キャッシュヒット回数（record_hit で更新）
    platform:        str            # "ios" | "android" | "unknown"
    distance:        int = field(default=0, compare=False)  # lookup 時に設定（非永続）


# ============================================================
# ScreenCache — 知識ベース管理クラス
# ============================================================

class ScreenCache:
    """
    pHash ベースのスクリーン→アクション知識ベース。

    起動時に knowledge_dir/*.json を走査してインメモリインデックスを構築し、
    lookup() では O(n) ハミング距離スキャンで最近傍エントリを返す。
    save() は即座に _index を更新するためスレッドセーフではないが、
    シングルプロセスのクローラーでは問題ない。

    使用例:
        cache = ScreenCache(Path("games/madodra/knowledge"), platform="android")
        sol = cache.lookup(Path("evidence/session_id/before.png"))
        if sol:
            # CACHE_HIT: sol.actions を実行
            cache.record_hit(sol.hash)
        else:
            # CACHE_MISS: 通常探索 → 遷移成功後に記録
            cache.save(shot_path, title="ホーム", actions=[...])
    """

    # デフォルトハミング距離閾値
    # クローラー内の PHASH_THRESHOLD=8 (同一性判定) より少し緩くして
    # HUD 更新・ローディング進捗表示などの微細差異を吸収する
    DEFAULT_HASH_THRESHOLD: int = 10

    def __init__(
        self,
        knowledge_dir: Path,
        hash_threshold: int = DEFAULT_HASH_THRESHOLD,
        platform: str = "unknown",
    ) -> None:
        self.knowledge_dir   = knowledge_dir
        self.hash_threshold  = hash_threshold
        self.platform        = platform
        self._screenshots_dir = knowledge_dir / "screenshots"

        # ディレクトリを保証
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self._screenshots_dir.mkdir(parents=True, exist_ok=True)

        # {hash_str: json_path} インメモリインデックス
        self._index: dict[str, Path] = {}
        self._load_index()

    # ----------------------------------------------------------
    # インデックス構築
    # ----------------------------------------------------------

    def _load_index(self) -> None:
        """knowledge_dir/*.json を走査して _index を構築する。human_solved/ サブディレクトリも対象。"""
        # root の human-curated エントリ
        for p in self.knowledge_dir.glob("*.json"):
            self._index[p.stem] = p
        # human_solved エントリ (同一ハッシュがあれば root 優先)
        hs_dir = self.knowledge_dir / "human_solved"
        if hs_dir.exists():
            for p in hs_dir.glob("*.json"):
                if p.stem not in self._index:
                    self._index[p.stem] = p
        logger.info(
            "[CACHE] インデックス構築: %d 件  dir=%s",
            len(self._index),
            self.knowledge_dir,
        )

    # ----------------------------------------------------------
    # lookup — キャッシュ照合
    # ----------------------------------------------------------

    def lookup(self, screenshot_path: Path) -> Optional[CachedSolution]:
        """
        スクリーンショットの pHash を計算し、知識ベース内の最近傍を返す。

        ハミング距離が hash_threshold 未満のエントリが存在する場合に
        CachedSolution を返す（最小距離のものを選択）。

        Returns:
            CachedSolution — CACHE_HIT (distance フィールドに実測距離を設定済み)
            None           — CACHE_MISS (エントリなし or 距離 ≥ threshold)
        """
        if not self._index:
            return None

        try:
            query_hash = compute_phash(screenshot_path)
        except Exception as e:
            logger.debug("[CACHE] pHash 計算失敗: %s", e)
            return None

        best_hash: Optional[str] = None
        best_dist = self.hash_threshold  # 閾値未満のみ採用（等値は除外）

        for known_hash in self._index:
            dist = phash_distance(query_hash, known_hash)
            if dist < best_dist:
                best_dist = dist
                best_hash = known_hash

        if best_hash is None:
            return None

        sol = self._load_solution(best_hash)
        if sol is None:
            return None

        sol.distance = best_dist
        logger.info(
            "[CACHE_HIT] title=%r  dist=%d  hash=%s…  actions=%d件",
            sol.title, best_dist, best_hash[:8], len(sol.actions),
        )
        return sol

    # ----------------------------------------------------------
    # save — 新規エントリ登録 / 更新
    # ----------------------------------------------------------

    def save(
        self,
        screenshot_path: Path,
        title: str,
        actions: list[dict],
        success: bool = True,
        source: str = "auto",   # "auto" | "human_solved"
    ) -> str:
        """
        スクリーンショットと対応するアクション列を知識ベースに保存する。

        同一 pHash が既に存在する場合は上書き更新し、hit_count を引き継ぐ。
        参照スクリーンショットは screenshots/{hash}.png にコピーされる。

        Args:
            source: "auto" → knowledge_dir 直下に保存。
                    "human_solved" → knowledge_dir/human_solved/ サブディレクトリに保存。

        Returns:
            保存した pHash hex 文字列 (16 文字)
        """
        try:
            hash_str = compute_phash(screenshot_path)
        except Exception as e:
            logger.warning("[CACHE] save 失敗 — pHash 計算エラー: %s", e)
            raise

        # 参照スクリーンショットをコピー (タイムスタンプも保持)
        ref_png = self._screenshots_dir / f"{hash_str}.png"
        try:
            shutil.copy2(str(screenshot_path), str(ref_png))
        except Exception as e:
            logger.debug("[CACHE] 参照スクリーンショットコピー失敗: %s", e)

        # 既存エントリの hit_count を引き継ぐ
        existing = self._load_solution(hash_str)
        hit_count = existing.hit_count if existing else 0

        payload = {
            "hash":            hash_str,
            "screenshot_path": str(ref_png),
            "actions":         actions,
            "success":         success,
            "title":           title,
            "created_at":      datetime.now().isoformat(),
            "hit_count":       hit_count,
            "platform":        self.platform,
            "source":          source,
        }

        # human_solved は専用サブディレクトリへ保存
        save_dir = self.knowledge_dir
        if source == "human_solved":
            save_dir = self.knowledge_dir / "human_solved"
            save_dir.mkdir(exist_ok=True)

        json_path = save_dir / f"{hash_str}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._index[hash_str] = json_path

        logger.info(
            "[CACHE] 保存: title=%r  hash=%s…  actions=%d件  source=%s",
            title, hash_str[:8], len(actions), source,
        )
        return hash_str

    # ----------------------------------------------------------
    # record_hit — ヒットカウンタ更新
    # ----------------------------------------------------------

    def record_hit(self, hash_str: str) -> None:
        """
        hit_count を +1 してディスクに書き戻す。
        lookup() → _execute_cached_actions() 成功後に呼び出す。
        """
        json_path = self._index.get(hash_str)
        if json_path is None or not json_path.exists():
            logger.debug("[CACHE] record_hit スキップ: hash=%s 未登録", hash_str[:8])
            return

        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            data["hit_count"] = data.get("hit_count", 0) + 1
            json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug(
                "[CACHE] hit_count=%d  hash=%s…", data["hit_count"], hash_str[:8]
            )
        except Exception as e:
            logger.warning("[CACHE] record_hit 失敗: %s", e)

    # ----------------------------------------------------------
    # 内部ヘルパー
    # ----------------------------------------------------------

    def _load_solution(self, hash_str: str) -> Optional[CachedSolution]:
        """hash_str に対応する JSON を読み込み CachedSolution を返す。失敗時は None。"""
        json_path = self._index.get(hash_str)
        if json_path is None or not json_path.exists():
            return None
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return CachedSolution(
                hash            = data["hash"],
                screenshot_path = data.get("screenshot_path", ""),
                actions         = data.get("actions", []),
                success         = data.get("success", True),
                title           = data.get("title", "unknown"),
                created_at      = data.get("created_at", ""),
                hit_count       = data.get("hit_count", 0),
                platform        = data.get("platform", "unknown"),
            )
        except Exception as e:
            logger.warning("[CACHE] JSON 読み込み失敗: %s  %s", json_path, e)
            return None
