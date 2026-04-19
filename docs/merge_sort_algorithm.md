# マージ時の sort_order 決定アルゴリズム

## 概要

複数セッションのスクリーン記録をマスターグラフに統合する際、
新規ノードの sort_order（表示順序）を決定するアルゴリズム。

**設計方針**: 順序の正確性を最優先とし、不確実な配置は行わない。
周回を重ねることで徐々にマップが完成していく。

---

## 用語定義

| 用語 | 定義 |
|------|------|
| **マスターノード** | 全セッション統合後の重複排除済み画面 (`lc_master_nodes`) |
| **sort_order** | マスターノードの表示順序。整数値、0始まり、一意 |
| **seed** | 最初にマスターに投入されるセッション。sort_order = `first_seen_at` 順 |
| **アンカー** | merge 時に既存マスターノードにマッチしたセッション画面 |
| **新規ノード** | merge 時にマスターに存在しない画面 (`match_method = 'new'`) |
| **隣接アンカー** | マスターの sort_order が連続している（間にノードがない）アンカーのペア |

---

## アルゴリズム: SafeInsert (安全挿入方式)

### 原則

1. **挿入されたノードの順序は 100% 正しい**
2. 不確実な位置には挿入しない（挿入しないことで安全性を保証）
3. 一度配置されたノードの sort_order は変更しない
4. 周回を重ねてアンカーが密になれば、挿入可能な位置が増える

### seed セッション

- sort_order = `first_seen_at` 順（時系列）
- 位相ソートは使わない
- 一本道のチュートリアルでは時系列 = ゲーム進行順

### merge セッション

#### Step 1: アンカー列の構築

セッションの画面を `discovered_at` 順に走査し、マスターにマッチした画面をアンカーとして抽出。

```
セッション時系列: A' → F → B' → G → H → C' → I → D'
アンカー列:       A'(sort=0)  B'(sort=5)  C'(sort=6)  D'(sort=10)
```

#### Step 2: 挿入可能判定

新規ノードごとに、前後のアンカーを特定し、以下の条件で挿入可否を判定:

| 条件 | 挿入位置 | 安全性 |
|------|---------|--------|
| 後のアンカーがマスター先頭 (sort=0) | 先頭に挿入 | 100% |
| 前のアンカーがマスター末尾 (sort=max) | 末尾に追加 | 100% |
| 前後のアンカーが sort_order で隣同士 (差=1) | 間に挿入 | 100% |
| 上記いずれにも該当しない | **挿入しない** | - |

#### Step 3: 挿入順序

同じ位置に複数の新規ノードが挿入される場合、セッションの `discovered_at` 順（時系列順）。

```
マスター: ... → E(sort=8)
セッション: ... → E' → F → G → H (F,G,H は新規)
結果: ... → E(8) → F(9) → G(10) → H(11)
```

#### Step 4: 再番号付け

挿入後、全ノードの sort_order を 0 から連番で振り直す（既存ノードの相対順序は不変）。

### 挿入しなかったノード

- マスターに追加しない
- `lc_node_mappings` にも記録しない
- 破棄とする（次の周回での再挿入は期待しない）

---

## インターフェース

```python
class MergeSortStrategy(ABC):
    """マージ時の sort_order 決定戦略の抽象基底クラス。"""

    @abstractmethod
    def compute_sort_order(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        node_mapping: dict[str, tuple[str, str, float]],
    ) -> MergeSortResult:
        """新規ノードの sort_order を計算する。

        Args:
            conn: DB 接続
            session_id: マージ対象のセッション ID
            node_mapping: session_fp → (master_fp, method, score)

        Returns:
            MergeSortResult: 挿入するノードと sort_order のリスト、
                            挿入しなかったノードのリスト
        """
        ...

@dataclass
class MergeSortResult:
    """sort_order 計算結果。"""
    inserts: list[tuple[str, float]]   # [(master_fp, sort_position), ...]
    skipped: list[str]                  # 挿入しなかった master_fp のリスト
```

---

## 差し替え方法

1. `MergeSortStrategy` を継承した新しいクラスを作成
2. `CrossSessionMerger.__init__` で使用する戦略を切り替え
3. 既存のコードは `self._sort_strategy.compute_sort_order(...)` を呼ぶだけ
