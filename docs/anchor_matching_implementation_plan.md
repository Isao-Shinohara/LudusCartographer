# アンカーマッチング改善 — 実装計画

## 前提ドキュメント

- 設計書: `docs/anchor_matching_design.md`
- sort_order 戦略: `docs/merge_sort_algorithm.md`
- CLAUDE.md §16: クラスタリングルール + アンカー方針

---

## 現状の問題

| 項目 | 現状 | 目標 |
|------|------|------|
| アンカー候補 | 最大 11 件 (home + convergence + static_ui) | 全 tap+テキストあり ノード |
| マッチ率 | 523 件中 120 件 (23%) | 大幅向上（安全性を維持しつつ） |
| マッチ方式 | アンカー起点 → k-hop → transition_similarity | Phase 段階的決定 |
| 安全装置 | スコア閾値のみ | 時系列整合性 + 矛盾検出 + 曖昧時破棄 |

---

## ファイル構成

### 新規作成

| ファイル | 内容 |
|---------|------|
| `crawler/tools/anchor_matcher.py` | 段階的アンカーマッチング (新モジュール) |
| `crawler/tests/test_anchor_matcher.py` | ユニットテスト |

### 修正

| ファイル | 変更内容 |
|---------|---------|
| `crawler/tools/cross_session_merger.py` | `_compute_matches` を `AnchorMatcher` に委譲 |

### 変更なし (そのまま活用)

| ファイル | 理由 |
|---------|------|
| `crawler/tools/merge_sort_strategy.py` | SafeInsertStrategy はそのまま |
| `crawler/tools/ap/background_worker.py` | 合成エッジ生成はそのまま |

---

## anchor_matcher.py の設計

### クラス構成

```python
@dataclass
class AnchorMatch:
    """確定したアンカーマッチ。"""
    session_fp: str      # セッション側の fingerprint
    master_fp: str       # マスター側の fingerprint
    master_sort: int     # マスター側の sort_order
    method: str          # "phase1_tap_text" / "phase2_auto_text" / "phase3_tap_phash"
    score: float         # マッチスコア
    phase: int           # 1, 2, 3

class AnchorMatcher:
    """段階的アンカーマッチング。差し替え可能な設計。"""

    def compute_matches(
        self,
        conn: sqlite3.Connection,
        session_id: str,
    ) -> tuple[dict[str, tuple[str, str, float]], list[str]]:
        """全 Phase を実行し、マッチ結果を返す。

        Returns:
            (node_mapping, skipped_fps)
            node_mapping: session_fp → (master_fp, method, score)
            skipped_fps: マッチしなかった session_fp のリスト
        """
```

### 内部メソッド

```
compute_matches()
  ├── _prepare_data()           # DB からデータ取得、ノード分類
  ├── _phase1_tap_text()        # tap + テキストあり
  ├── _verify_consistency()     # 時系列整合性チェック
  ├── _phase2_auto_text()       # auto + テキストあり
  ├── _verify_consistency()     # 時系列整合性チェック
  ├── _phase3_tap_phash()       # tap + テキスト空
  ├── _verify_consistency()     # 時系列整合性チェック
  └── return
```

---

## 実装ステップ (各ステップでテスト → コミット)

### Step 1: データ準備とノード分類

**`_prepare_data()`**

セッションとマスターのノードを4カテゴリに分類:

```python
@dataclass
class NodeInfo:
    fp: str
    text: str           # normalize 済みテキスト
    phash: str
    scene: str
    edge_type: str      # "tap" / "auto" / "none" (遷移なし)
    has_text: bool       # len(text) > 0
    session_time_rank: int  # セッション内の時系列順位
```

セッション側:
- lc_screens (is_representative=1) を discovered_at 順に取得
- 各ノードの edge_type を lc_transitions から取得
  - tap エッジの from_fp または to_fp に含まれる → "tap"
  - auto エッジのみ → "auto"
  - どちらにもない → "none"
- session_time_rank を 0, 1, 2... で付与

マスター側:
- lc_master_nodes を sort_order 順に取得
- テキストは `COALESCE(ocr_text_manual, ocr_text_gemini, ocr_text)` を使用

**テストケース:**
- ノード分類が正しいか
- edge_type の判定が正しいか

---

### Step 2: Phase 1 — tap + テキストあり

**`_phase1_tap_text()`**

1. セッション側: edge_type="tap" かつ has_text=True のノードを抽出
2. マスター側: テキストがあるノードを全件取得
3. マッチング:
   - テキスト完全一致 → 候補リスト作成
   - テキスト前方一致 (5文字以上) → 候補リスト作成
4. 候補フィルタリング:
   - 候補が 1 件 → phash 類似度チェック (閾値以上なら確定)
   - 候補が 2 件以上 → **全て破棄** (曖昧)
   - 候補が 0 件 → スキップ
5. phash 二重確認:
   - phash_distance < 30 → 確定
   - phash_distance >= 30 → 破棄 (テキスト一致でも画面が異なる可能性)

**マッチ条件 (全て満たす):**
- テキスト完全一致 or 前方一致
- phash_distance < 30
- 候補が一意
- テキスト完全一致の候補が単一の場合のみ確定

**テストケース:**
- テキスト完全一致 + phash 近い → マッチ
- テキスト完全一致 + phash 遠い → 破棄
- テキスト完全一致で候補 2 件 → 破棄
- テキスト前方一致 + phash 近い → マッチ
- テキストなし → スキップ (Phase 1 対象外)

---

### Step 3: 時系列整合性チェック

**`_verify_consistency(anchors: list[AnchorMatch]) -> list[AnchorMatch]`**

1. 確定済みアンカーを session_time_rank 順にソート
2. 対応する master_sort も同じ方向に単調増加しているか確認
3. 単調増加に違反するペアを検出:
   ```
   anchor[i].session_time_rank < anchor[j].session_time_rank
   かつ
   anchor[i].master_sort > anchor[j].master_sort
   → 矛盾: 両方を破棄
   ```
4. 矛盾するアンカーを除去して返す

**具体例:**
```
確定アンカー (session_time_rank → master_sort):
  A: rank=0 → sort=2  ✓
  B: rank=3 → sort=8  ✓ (2 < 8)
  C: rank=5 → sort=5  ✗ (8 > 5 → B と C が矛盾)
  D: rank=7 → sort=12 ✓

→ B と C を破棄、A と D のみ残す
```

**矛盾解消アルゴリズム:**
- 最長増加部分列 (LIS) を求め、LIS に含まれないアンカーを破棄
- これにより最大数のアンカーを保持しつつ矛盾をゼロにできる

**テストケース:**
- 全て単調増加 → 全件保持
- 1 件の矛盾 → その 1 件を破棄
- 複数矛盾 → LIS で最大保持
- アンカー 0 件 → 空リスト返却

---

### Step 4: Phase 2 — auto + テキストあり

**`_phase2_auto_text()`**

1. セッション側: edge_type="auto" かつ has_text=True のノードを抽出
2. Phase 1 で確定したアンカーを基準点にして候補を絞る:
   - S2 時系列で前後の Phase 1 アンカーを特定
   - マスター側の候補を、前後アンカーの sort_order 範囲に限定
3. 範囲内でテキスト完全一致 + phash 二重確認
4. 候補が一意 → 確定、複数 → 破棄

**候補範囲の限定:**
```
Phase 1 確定: A(rank=0, sort=2), C(rank=10, sort=20)
S2 ノード X: rank=5 (A と C の間)
→ マスター側の候補を sort 2〜20 に限定
→ この範囲でテキスト一致するノードを検索
```

Phase 1 アンカーがない区間（先頭〜最初のアンカー、最後のアンカー〜末尾）:
- 先頭区間: sort 0〜最初のアンカーの sort に限定
- 末尾区間: 最後のアンカーの sort〜max_sort に限定
- アンカーが 0 件: 全範囲（Phase 1 と同じ条件）

**テストケース:**
- Phase 1 アンカー間のノードがマッチ
- 範囲外のテキスト一致 → マッチしない (範囲制限が効いている)
- Phase 1 アンカーなし → 全範囲検索

---

### Step 5: Phase 3 — tap + テキスト空

**`_phase3_tap_phash()`**

1. セッション側: edge_type="tap" かつ has_text=False のノードを抽出
2. Phase 1+2 で確定したアンカーを基準点にして候補を絞る
3. 範囲内で phash_distance < 15 (高閾値) のノードを検索
4. **追加条件**: 前後のアンカーが確定していなければマッチしない
   - 「前のアンカー」と「後のアンカー」の両方がある区間のみ対象
   - 先頭/末尾の片側アンカーのみの区間は対象外
5. 候補が一意 → 確定、複数 → 破棄

**テストケース:**
- 前後アンカーあり + phash 近い + 候補一意 → マッチ
- 前後アンカーあり + phash 近い + 候補複数 → 破棄
- 片側アンカーのみ → マッチしない
- テキストあり → スキップ (Phase 3 対象外)

---

### Step 6: CrossSessionMerger への統合

**`cross_session_merger.py` の修正:**

```python
class CrossSessionMerger:
    def __init__(self, db_path, sort_strategy=None, anchor_matcher=None):
        from tools.merge_sort_strategy import SafeInsertStrategy
        from tools.anchor_matcher import AnchorMatcher
        self._sort_strategy = sort_strategy or SafeInsertStrategy()
        self._anchor_matcher = anchor_matcher or AnchorMatcher()
```

`_compute_matches` を書き換え:
```python
def _compute_matches(self, session_id):
    # seed チェック (変更なし)
    if master_count == 0:
        return {}, session_reps, True

    # 新しいアンカーマッチャーに委譲
    node_mapping, skipped = self._anchor_matcher.compute_matches(
        self._conn, session_id
    )
    return node_mapping, session_reps, False
```

旧メソッド削除:
- `find_anchors()` → AnchorMatcher に移行
- `find_master_anchors()` → AnchorMatcher に移行
- `match_score()` → AnchorMatcher 内部
- `node_match_score()` → AnchorMatcher 内部
- `k_hop_match()` → 廃止 (Phase 段階的決定で代替)
- `transition_similarity()` → 廃止 (同上)

**ただし旧メソッドは一旦残し、新旧を切り替え可能にする。**
検証完了後に旧メソッドを削除。

---

### Step 7: デバッグモード強化

**UI 変更:**

| 要素 | 表示 |
|------|------|
| Phase 1 アンカー | 水色枠 (cyan) + "P1" ラベル |
| Phase 2 アンカー | 水色枠 (cyan) + "P2" ラベル |
| Phase 3 アンカー | 水色枠 (cyan) + "P3" ラベル |
| 安全挿入ノード | 緑枠 (lime) |
| 選択中アンカー | オレンジ枠 (orange) |

**フッター統計:**
```
P1: 45 | P2: 30 | P3: 5 | 隣接: 28 | 挿入: 52
```

**API 変更:**
- `anchor_info` にフェーズ番号を含める: `"phase1_tap_text:ap_20260417_204613"`
- `last_match_method` に phase を含める: `"phase1_tap_text"` / `"phase2_auto_text"` / `"phase3_tap_phash"` / `"new"`

---

### Step 8: 実データ検証

1. クリーンアップ → seed → merge の手順で再構築
2. 検証項目:
   - seed_inversions = 0 (seed 順序保持)
   - マッチ数の変化 (旧: 120 → 目標: 大幅増)
   - 各 Phase のマッチ数と破棄数
   - 矛盾検出で破棄されたアンカー数
   - SafeInsert の挿入数と隣接ペア数
3. Final タブでデバッグモードによる目視確認
4. 問題があれば閾値調整

---

## テスト一覧

| テスト | 対象 Step | ケース数 |
|--------|----------|---------|
| test_prepare_data | Step 1 | 3 |
| test_phase1_tap_text | Step 2 | 5 |
| test_verify_consistency | Step 3 | 4 |
| test_phase2_auto_text | Step 4 | 3 |
| test_phase3_tap_phash | Step 5 | 4 |
| test_integration | Step 6 | 3 |
| **合計** | | **22** |

---

## リスク管理

| リスク | 対策 |
|--------|------|
| 新ロジックでマッチ数が減る | 旧ロジックと並行稼働可能な設計 |
| Phase 1 で誤マッチ | テキスト完全一致 + phash + 候補一意の三重条件 |
| 矛盾検出で大量破棄 | LIS で最大保持、ログで破棄理由を記録 |
| パフォーマンス低下 | テキスト一致は DB インデックスで高速化可能 |
| auto ノードの edge_type 誤判定 | 合成エッジは明示的に edge_type='auto' で記録済み |

---

## 作業見積もり

| Step | 内容 | 規模 |
|------|------|------|
| 1 | データ準備 + ノード分類 | 小 |
| 2 | Phase 1 (tap + テキスト) | 中 |
| 3 | 時系列整合性チェック (LIS) | 中 |
| 4 | Phase 2 (auto + テキスト) | 中 |
| 5 | Phase 3 (tap + phash) | 小 |
| 6 | CrossSessionMerger 統合 | 中 |
| 7 | デバッグモード強化 | 小 |
| 8 | 実データ検証 | 中 |
