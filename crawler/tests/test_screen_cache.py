"""
test_screen_cache.py — ScreenCache ユニットテスト

Appium・実機・PaddleOCR 不要。
cv2 で合成した PNG と tmp_path を使い、ファイルシステム動作を検証する。

【実行方法】
  cd crawler
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True venv/bin/python -m pytest tests/test_screen_cache.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

os_env_patch = {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}

from lc.screen_cache import ScreenCache, CachedSolution


# ============================================================
# テスト用ヘルパー
# ============================================================

def _make_png(path: Path, seed: int = 42, size: tuple = (200, 200)) -> Path:
    """
    再現可能な合成 PNG を生成する。
    seed が異なれば pHash が異なる（統計的に高確率）。
    """
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (*size, 3), dtype=np.uint8)
    cv2.imwrite(str(path), img)
    return path


def _make_solid_png(path: Path, color: int = 0) -> Path:
    """単色 PNG (pHash が安定して一定値になる)。"""
    img = np.full((200, 200, 3), color, dtype=np.uint8)
    cv2.imwrite(str(path), img)
    return path


# ============================================================
# TestScreenCacheInit
# ============================================================

class TestScreenCacheInit:

    def test_creates_knowledge_dir(self, tmp_path: Path) -> None:
        """ScreenCache 初期化時に knowledge_dir が作成されること"""
        kd = tmp_path / "games" / "testgame" / "knowledge"
        assert not kd.exists()
        ScreenCache(kd)
        assert kd.is_dir()

    def test_creates_screenshots_subdir(self, tmp_path: Path) -> None:
        """screenshots/ サブディレクトリが作成されること"""
        kd = tmp_path / "knowledge"
        ScreenCache(kd)
        assert (kd / "screenshots").is_dir()

    def test_loads_existing_index(self, tmp_path: Path) -> None:
        """既存 JSON が起動時にインデックスへ読み込まれること"""
        kd = tmp_path / "knowledge"
        kd.mkdir(parents=True)
        for h in ["aabbccdd11223344", "11223344aabbccdd"]:
            (kd / f"{h}.json").write_text(
                json.dumps({
                    "hash": h, "actions": [], "title": "t",
                    "created_at": "", "hit_count": 0,
                    "platform": "ios", "success": True,
                    "screenshot_path": "",
                }),
                encoding="utf-8",
            )
        cache = ScreenCache(kd)
        assert len(cache._index) == 2

    def test_empty_index_on_fresh_dir(self, tmp_path: Path) -> None:
        """新規ディレクトリでは _index が空であること"""
        kd = tmp_path / "knowledge"
        cache = ScreenCache(kd)
        assert len(cache._index) == 0


# ============================================================
# TestScreenCacheSave
# ============================================================

class TestScreenCacheSave:

    def test_save_creates_json(self, tmp_path: Path) -> None:
        """save() が {hash}.json を作成すること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=1)
        cache = ScreenCache(kd)
        h = cache.save(png, title="ショップ", actions=[{"type": "tap", "x": 100, "y": 200}])
        assert (kd / f"{h}.json").exists()

    def test_save_json_contains_required_fields(self, tmp_path: Path) -> None:
        """save() が作成する JSON に必要なフィールドが含まれること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=2)
        cache = ScreenCache(kd)
        actions = [{"type": "tap", "x": 50, "y": 80}]
        h = cache.save(png, title="ホーム", actions=actions, success=True)
        data = json.loads((kd / f"{h}.json").read_text())
        assert data["hash"] == h
        assert data["title"] == "ホーム"
        assert data["actions"] == actions
        assert data["success"] is True
        assert "screenshot_path" in data
        assert "created_at" in data
        assert "hit_count" in data
        assert "platform" in data

    def test_save_copies_reference_screenshot(self, tmp_path: Path) -> None:
        """save() が screenshots/{hash}.png に参照スクリーンショットをコピーすること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=3)
        cache = ScreenCache(kd)
        h = cache.save(png, title="バトル", actions=[])
        assert (kd / "screenshots" / f"{h}.png").exists()

    def test_save_returns_16char_hex(self, tmp_path: Path) -> None:
        """save() が 16 文字の hex 文字列を返すこと"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=4)
        cache = ScreenCache(kd)
        h = cache.save(png, title="X", actions=[])
        assert len(h) == 16
        int(h, 16)  # hex として有効であること

    def test_save_updates_index(self, tmp_path: Path) -> None:
        """save() 後に _index にエントリが追加されること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=5)
        cache = ScreenCache(kd)
        assert len(cache._index) == 0
        cache.save(png, title="T", actions=[])
        assert len(cache._index) == 1

    def test_save_preserves_hit_count_on_overwrite(self, tmp_path: Path) -> None:
        """同じ pHash で 2 回 save() した場合、hit_count が引き継がれること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=6)
        cache = ScreenCache(kd)
        h = cache.save(png, title="T", actions=[])
        # hit_count を手動で 7 に書き換え
        jp = kd / f"{h}.json"
        data = json.loads(jp.read_text())
        data["hit_count"] = 7
        jp.write_text(json.dumps(data))
        # 2 回目 save → hit_count が 7 のまま引き継がれること
        cache.save(png, title="T2", actions=[])
        data2 = json.loads(jp.read_text())
        assert data2["hit_count"] == 7


# ============================================================
# TestScreenCacheLookup
# ============================================================

class TestScreenCacheLookup:

    def test_lookup_returns_none_when_empty(self, tmp_path: Path) -> None:
        """知識ベースが空のとき lookup() は None を返すこと"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "q.png", seed=10)
        cache = ScreenCache(kd)
        assert cache.lookup(png) is None

    def test_lookup_returns_cached_solution_on_exact_hit(self, tmp_path: Path) -> None:
        """登録済みスクリーンショットと同一画像で lookup() が CachedSolution を返すこと"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "ref.png", seed=11)
        cache = ScreenCache(kd)
        cache.save(png, title="バトル", actions=[{"type": "tap", "x": 50, "y": 80}])
        result = cache.lookup(png)
        assert result is not None
        assert isinstance(result, CachedSolution)
        assert result.title == "バトル"
        assert result.distance == 0  # 完全一致

    def test_lookup_returns_none_when_above_threshold(self, tmp_path: Path) -> None:
        """ハミング距離が閾値以上のとき None を返すこと"""
        kd  = tmp_path / "knowledge"
        # 200x200 ランダム画像は 32x32 ダウンスケール時に平均化で DCT 差異が小さくなるため
        # 32x32 画像 (ダウンスケールなし) を使用。異なるシード間の距離は実測 13+
        img_ref   = _make_png(tmp_path / "ref.png",   seed=1,  size=(32, 32))
        img_query = _make_png(tmp_path / "query.png", seed=2,  size=(32, 32))
        cache = ScreenCache(kd, hash_threshold=10)
        cache.save(img_ref, title="ref", actions=[])
        assert cache.lookup(img_query) is None

    def test_lookup_sets_distance_field(self, tmp_path: Path) -> None:
        """lookup() が返す CachedSolution の distance が 0 以上であること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "ref.png", seed=12)
        cache = ScreenCache(kd)
        cache.save(png, title="X", actions=[])
        sol = cache.lookup(png)
        assert sol is not None
        assert sol.distance >= 0

    def test_lookup_selects_best_match(self, tmp_path: Path) -> None:
        """複数エントリがある場合、最も近いものが返されること"""
        kd  = tmp_path / "knowledge"
        ref = _make_png(tmp_path / "ref.png", seed=20)
        # ref と同一 → distance=0
        cache = ScreenCache(kd)
        cache.save(ref, title="exact", actions=[])
        # 別エントリも追加
        other = _make_png(tmp_path / "other.png", seed=99)
        cache.save(other, title="other", actions=[])
        sol = cache.lookup(ref)
        assert sol is not None
        assert sol.title == "exact"
        assert sol.distance == 0


# ============================================================
# TestScreenCacheRecordHit
# ============================================================

class TestScreenCacheRecordHit:

    def test_record_hit_increments_hit_count(self, tmp_path: Path) -> None:
        """record_hit() が hit_count を +1 してディスクに書き込むこと"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=30)
        cache = ScreenCache(kd)
        h = cache.save(png, title="T", actions=[])
        cache.record_hit(h)
        data = json.loads((kd / f"{h}.json").read_text())
        assert data["hit_count"] == 1

    def test_record_hit_multiple_times(self, tmp_path: Path) -> None:
        """record_hit() を複数回呼ぶと hit_count が累積されること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=31)
        cache = ScreenCache(kd)
        h = cache.save(png, title="T", actions=[])
        for _ in range(5):
            cache.record_hit(h)
        data = json.loads((kd / f"{h}.json").read_text())
        assert data["hit_count"] == 5

    def test_record_hit_unknown_hash_does_not_crash(self, tmp_path: Path) -> None:
        """存在しない hash で record_hit() を呼んでもクラッシュしないこと"""
        kd = tmp_path / "knowledge"
        cache = ScreenCache(kd)
        cache.record_hit("0000000000000000")  # 例外なし


# ============================================================
# TestScreenCacheActions
# ============================================================

class TestScreenCacheActions:

    def test_save_swipe_action(self, tmp_path: Path) -> None:
        """swipe アクションが正しく保存・読み込みされること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=40)
        cache = ScreenCache(kd)
        swipe = {"type": "swipe", "x1": 760, "y1": 500, "x2": 760, "y2": 100, "duration": 250}
        h = cache.save(png, title="チュートリアル", actions=[swipe] * 60)
        sol = cache.lookup(png)
        assert sol is not None
        assert len(sol.actions) == 60
        assert sol.actions[0]["type"] == "swipe"

    def test_save_mixed_actions(self, tmp_path: Path) -> None:
        """tap/swipe/back/wait の混合アクションが保存されること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=41)
        cache = ScreenCache(kd)
        actions = [
            {"type": "swipe", "x1": 760, "y1": 500, "x2": 760, "y2": 100, "duration": 250},
            {"type": "tap",   "x": 923, "y": 626, "label": "ok"},
            {"type": "wait",  "duration": 2.0},
            {"type": "back"},
        ]
        h = cache.save(png, title="ダイアログ+スワイプ", actions=actions)
        sol = cache.lookup(png)
        assert sol is not None
        types = [a["type"] for a in sol.actions]
        assert types == ["swipe", "tap", "wait", "back"]

    def test_index_persists_across_instances(self, tmp_path: Path) -> None:
        """別の ScreenCache インスタンスでも保存したエントリが参照できること"""
        kd  = tmp_path / "knowledge"
        png = _make_png(tmp_path / "shot.png", seed=50)
        cache1 = ScreenCache(kd)
        h = cache1.save(png, title="永続化テスト", actions=[])
        # 新しいインスタンスで読み込む
        cache2 = ScreenCache(kd)
        sol = cache2.lookup(png)
        assert sol is not None
        assert sol.title == "永続化テスト"
        assert sol.hash == h
