# 遷移グラフ実装計画

## 目的

auto_pilot が操作した画面遷移を有向グラフとして記録し、ゲームUIの「地図」を構築する。
BFS 階層 + SCC 機能ユニット分類で、意味のあるグルーピングを $0 で実現する。

## 設計方針（確定事項）

| 項目 | 方針 | 根拠 |
|------|------|------|
| 記録方式 | B案: セッションごとに別エッジ、集計は SQL GROUP BY | 漏れなく記録 → 不要と判断したら表示側で絞る |
| ポップアップ | A案: 背景+ポップアップの組み合わせで1ノード | 遷移の正確な再現を優先 |
| 戻るエッジ判定 | BFS depth 比較を主軸 + action_name 補助 | depth_to < depth_from でショートカットも一括除外 |
| 可視化 | Cytoscape.js + dagre + Compound Nodes | SCC クラスタ表示、階層レイアウト、サムネ表示が標準サポート |
| 初期表示 | 全展開 → ユーザーが SCC を任意で折りたたみ | 認知負荷の軽減（パフォーマンスは問題なし） |

---

## フェーズ構成

| Phase | 内容 | 変更対象 | 依存 |
|-------|------|----------|------|
| **1** | 遷移エッジ記録 | screen_recorder.py, device.py, batch_processor.py | なし |
| **2** | phash 名寄せ + LOADING バイパス + BFS + SCC | batch_processor.py (新Phase) | Phase 1 |
| **3** | API エンドポイント | api/search.php | Phase 2 |
| **4** | Cytoscape.js 地図表示 | graph.html.twig, graph.php | Phase 3 |
| **5** | OCR ラベル自動生成 + エッジ重み | batch_processor.py | Phase 2 |

各フェーズは独立してテスト・コミット可能。

---

## Phase 1: 遷移エッジ記録

### 目標
タップ操作ごとに `from_screen → to_screen` の遷移を lc_transitions テーブルに記録する。

### スキーマ

```sql
CREATE TABLE IF NOT EXISTS lc_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    from_screen_id  INTEGER NOT NULL,
    to_screen_id    INTEGER,            -- タップ後に新画面が記録されるまで NULL
    from_fp         TEXT NOT NULL,       -- 検索・集計用（screen_id は session 固有）
    to_fp           TEXT,
    tap_x           INTEGER,            -- analysis 座標系 (1440x720)
    tap_y           INTEGER,
    tap_label       TEXT,                -- タップ座標近傍の OCR テキスト
    action_name     TEXT,
    discovered_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_trans_from ON lc_transitions(from_fp);
CREATE INDEX IF NOT EXISTS idx_trans_to ON lc_transitions(to_fp);
CREATE INDEX IF NOT EXISTS idx_trans_session ON lc_transitions(session_id);
```

### 実装詳細

**screen_recorder.py への変更:**

1. `__init__` に `_pending_transition: Optional[dict]` を追加
   - タップ時に from 情報を一時保持し、次の画面記録時に to を確定する

2. `__init__` でセッション開始時に前セッションの未完了遷移をクリーンアップ
   ```sql
   DELETE FROM lc_transitions WHERE to_screen_id IS NULL AND session_id != ?
   ```
   - auto_pilot クラッシュ時のゴミデータ防止

3. `record_tap(from_screen_id, from_fp, tap_x, tap_y, tap_label, action_name)` メソッド追加
   - `_pending_transition` に from 情報をセット
   - まだ to は不明（次の maybe_record で確定する）

4. `maybe_record()` 内の `_insert_screen()` 成功後に:
   - `_pending_transition` が存在すれば、to_screen_id と to_fp を確定して INSERT
   - `_pending_transition = None` にリセット

5. `_resolve_tap_label(tap_x, tap_y, ocr_results)` メソッド追加
   - タップ座標から半径 50px 以内の最も近い OCR テキストを返す
   - `lc_tappable_items` は使わず、maybe_record に渡された ocr_results から直接取得

6. `_last_inserted_id` プロパティ追加
   - `_insert_screen()` の戻り値（lastrowid）を保持
   - `record_tap()` で from_screen_id として使用

**device.py tap_device() への変更:**

```python
# 既存の force=True maybe_record 呼び出しの直後に追加
_rec = getattr(state, "recorder", None)
if _rec is not None:
    _last_id = getattr(_rec, "_last_inserted_id", None)
    _last_fp = getattr(_rec, "_last_recorded_fp", None)
    if _last_id and _last_fp:
        _tap_label = _rec._resolve_tap_label(x, y, getattr(state, "last_ocr_results", []))
        _rec.record_tap(
            from_screen_id=_last_id,
            from_fp=_last_fp,
            tap_x=x, tap_y=y,
            tap_label=_tap_label,
            action_name=desc,
        )
```

**batch_processor.py への変更:**
- `_migrate()` に lc_transitions テーブルの CREATE TABLE IF NOT EXISTS を追加

### テスト

- 単体テスト: record_tap → maybe_record の順序で transition が正しく記録されるか
- to_screen_id が NULL のまま残る遷移（画面が変わらなかったタップ）のテスト
- _resolve_tap_label の座標→テキスト紐付けテスト
- セッション開始時の未完了遷移クリーンアップテスト
- close() 時に _pending_transition のフラッシュ確認

### 成果物
- lc_transitions にセッション中の全タップ遷移が記録される
- 「画面が変わらなかったタップ」は to_screen_id=NULL として記録（デバッグデータとして有用）
- タップしていない画面遷移（自動遷移、ポップアップ自動消滅等）は parent_fp で追跡可能（既存）

---

## Phase 2: phash 名寄せ + LOADING バイパス + BFS + SCC

### 目標
バッチ処理の新 Phase として、lc_transitions からグラフを構築し、BFS depth と SCC グループを付与する。
グラフ構築前に fingerprint の断片化を名寄せし、LOADING ノードをバイパスする。

### スキーマ拡張

```sql
-- lc_screens に追加
ALTER TABLE lc_screens ADD COLUMN bfs_depth INTEGER;      -- HOME からの最短距離
ALTER TABLE lc_screens ADD COLUMN scc_id INTEGER;          -- SCC グループ ID
ALTER TABLE lc_screens ADD COLUMN scc_label TEXT;          -- SCC グループ名

-- SCC グループ管理
CREATE TABLE IF NOT EXISTS lc_scc_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    label        TEXT,                  -- 自動生成ラベル
    screen_count INTEGER DEFAULT 0,
    root_fp      TEXT                   -- グループ内で最も depth が浅い画面
);
```

### 実装詳細

**batch_processor.py に `build_graph()` メソッド追加:**

```
処理フロー:

Step 0: phash 名寄せ
  - 全 lc_screens の fingerprint + phash を取得
  - phash 距離 < 8 の fingerprint 同士を「代表 fp」にマッピング
  - 一時変換テーブル（dict）を構築
  - 以降の集約は代表 fp で GROUP BY

Step 1: グラフ構築
  - lc_transitions から全エッジを読み込み
  - from_fp, to_fp を代表 fp に変換
  - GROUP BY 代表from_fp, 代表to_fp で集約（count, action_names）
  - networkx.DiGraph を構築

Step 2: LOADING ノードバイパス
  - scene='LOADING' のノードを検出
  - 前後のエッジを直結 (A → LOADING → B を A → B に)
  - LOADING ノードをグラフから除去

Step 3: BFS depth 付与
  - HOME 画面を特定 (scene='MENU' かつ GOAL_HOME_REACHED の画面)
  - BFS で全ノードに depth を付与
  - HOME が見つからない場合: 最も多くの出次数を持つノードを仮 ROOT に

Step 4: 戻るエッジ除外 + SCC 計算
  - 戻るエッジを除外:
    - depth_to < depth_from（主軸: ショートカットも含め一括除外）
    - action_name に 'BACK' を含むエッジ（補助）
  - 順方向エッジのみで SCC を計算 (nx.strongly_connected_components)

Step 5: DB 更新
  - 各 SCC に lc_scc_groups レコードを作成
  - lc_screens.bfs_depth, scc_id を UPDATE
```

**依存ライブラリ:**
- `networkx`: グラフ構築、BFS、SCC（pip install 済みか確認要）

### テスト

- phash 名寄せ: 距離 7 → 同一代表fp、距離 9 → 別fp
- LOADING バイパス: A→LOADING→B が A→B になること
- 小規模グラフ（10ノード程度）で BFS depth が正しいか
- 戻るエッジ除外後の SCC が期待通りのグループになるか
- HOME が見つからない場合のフォールバック

---

## Phase 3: API エンドポイント

### 目標
Cytoscape.js が消費する JSON データを返す API を追加する。

### エンドポイント

**`api/search.php?action=get_graph&game={title}`**

```json
{
  "nodes": [
    {
      "id": "fp_abc123",
      "fingerprint": "abc123def456",
      "title": "ホーム画面",
      "scene": "MENU",
      "thumbnail": "/img.php?path=...",
      "bfs_depth": 0,
      "scc_id": 1,
      "scc_label": "ホーム"
    }
  ],
  "edges": [
    {
      "source": "fp_abc123",
      "target": "fp_def789",
      "tap_label": "クエスト",
      "action_name": "STORY_TAP",
      "count": 5,
      "is_back": false
    }
  ],
  "scc_groups": [
    {
      "id": 1,
      "label": "ホーム",
      "screen_count": 3
    }
  ]
}
```

### SQL クエリ

```sql
-- ノード: 代表画像のみ（is_representative=1）
SELECT fingerprint, title, scene, thumbnail_path, bfs_depth, scc_id
FROM lc_screens
WHERE is_representative = 1

-- エッジ: fingerprint ベースで集約
SELECT from_fp, to_fp, tap_label, action_name,
       COUNT(*) as count,
       MAX(CASE WHEN action_name LIKE '%BACK%' THEN 1 ELSE 0 END) as is_back
FROM lc_transitions
WHERE to_fp IS NOT NULL
GROUP BY from_fp, to_fp

-- SCC グループ
SELECT * FROM lc_scc_groups
```

### テスト
- 空 DB でエラーにならないことを確認
- ノード数が 0 の場合に空配列を返す

---

## Phase 4: Cytoscape.js 地図表示

### 目標
ダッシュボードに「Map」タブを追加し、遷移グラフをインタラクティブに表示する。

### ファイル構成

```
web/
├── public/
│   └── graph.php                 # エントリポイント（Twig レンダー）
└── templates/
    └── graph.html.twig           # Cytoscape.js 地図テンプレート
```

### 外部ライブラリ（CDN）

```html
<script src="https://unpkg.com/cytoscape@3/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/dagre@0.8/dist/dagre.min.js"></script>
<script src="https://unpkg.com/cytoscape-dagre@2/cytoscape-dagre.js"></script>
```

### UI 仕様

1. **レイアウト**: dagre（上→下の階層表示、BFS depth に基づく）
2. **ノード表示**:
   - サムネイル画像を背景に表示 (background-image)
   - ラベル: title の先頭 20 文字
   - 色: scene ごとに色分け（BATTLE=赤、ADV=青、MENU=緑 等）
3. **エッジ表示**:
   - 太さ: count に比例（1〜5px）
   - ラベル: tap_label（ボタン名）
   - 戻るエッジ: 破線 + 薄い色
4. **SCC クラスタ**: Compound Nodes で囲み表示、初期は全展開、ユーザーが任意で折りたたみ
5. **インタラクション**:
   - ズーム・パン
   - ノードクリック → 右パネルに詳細（スクショ拡大、OCR テキスト、遷移先一覧）
   - SCC グループクリック → 展開/折りたたみ
6. **ダッシュボードとの連携**:
   - layout.html.twig のナビに「Map」リンクを追加
   - ゲームセレクタと連動

### テスト
- 0 ノードで空表示（エラーなし）
- 100 ノード程度でのレンダリングパフォーマンス確認
- モバイルブラウザでのタッチ操作確認

---

## Phase 5: OCR ラベル自動生成 + エッジ重み

### 目標
SCC グループと BFS 階層に意味のあるラベルを自動付与する。エッジの重み（遷移頻度）を活用した可視化の最適化。

### SCC ラベル自動生成ロジック

```
1. SCC 内の全画面の ocr_text_hq を収集
2. テキストを形態素解析（mecab）or 単純な頻度カウント
3. 最頻出の名詞を SCC ラベルに採用
   例: 「クエスト」「バトル」「ガチャ」「パーティ」
4. scene ラベルとの組み合わせ:
   SCC 内の最頻 scene + 最頻テキスト → 「バトル:ブレイクチュートリアル」
```

### エッジ重み活用

```
- count >= 5: 太線（メインルート）
- count 2-4: 通常線
- count == 1: 細線（レアルート）
- is_back == true: 破線
```

### テスト
- ラベル生成が空文字にならないことの確認
- 日本語テキストの頻度カウントが正しいか

---

## 実装順序とマイルストーン

| 順序 | Phase | 完了条件 | 想定規模 |
|------|-------|----------|----------|
| 1st | **Phase 1** | lc_transitions に遷移が記録され、テスト通過 | screen_recorder +70行, device +10行, テスト +100行 |
| 2nd | **Phase 2** | バッチ実行で bfs_depth, scc_id が全画面に付与される | batch_processor +200行, テスト +120行 |
| 3rd | **Phase 3** | API が正しい JSON を返す | search.php +60行, テスト +40行 |
| 4th | **Phase 4** | ブラウザで地図が表示され、操作できる | graph.php +20行, graph.html.twig +300行 |
| 5th | **Phase 5** | SCC に自動ラベルが付き、エッジ太さが反映される | batch_processor +80行, graph.html.twig +30行 |

### 制約事項

- **Phase 1 は今回の auto_pilot 実行に間に合わせたい** — 次の周回から遷移データが溜まる
- Phase 2-5 はデータが溜まってから段階的に実装可能
- **networkx** が未インストールの場合は Phase 2 開始前に `pip install networkx` が必要
- Cytoscape.js は CDN 利用（$0）、オフライン用にローカルコピーも検討

---

## Gemini レビュー結果（2026-04-13）

### 採用した指摘

| 指摘 | 反映先 | 内容 |
|------|--------|------|
| 未完了遷移のクリーンアップ | Phase 1 | セッション開始時に前セッションの to_screen_id IS NULL をクリーンアップ |
| phash 名寄せ | Phase 2 Step 0 | グラフ構築前に phash 距離 < 8 の fp を代表 fp にマッピング |
| LOADING ノードバイパス | Phase 2 Step 2 | A→LOADING→B を A→B に直結、LOADING ノードをグラフから除去 |
| BFS depth を戻るエッジ判定の主軸に | Phase 2 Step 4 | depth_to < depth_from で一括除外（action_name は補助） |
| Cytoscape.js 初期表示は全展開 | Phase 4 | パフォーマンス問題なし、折りたたみはユーザー任意 |

### 採用しなかった指摘

| 指摘 | 理由 |
|------|------|
| CLIP 属性タグ付け | 遷移グラフが先。地図の骨格ができてから |
| Gemini クラスタ命名 | OCR ベース自動命名で十分。精度不足なら後から追加 |
| Gemini 未知ダイアログ判断 | リアルタイムループに API 呼び出しはレイテンシ上不可 |
| SSIM 一次フィルター | phash が同じ役割を果たしており十分 |

---

*最終更新: 2026-04-13*
*作成: Claude Code (Opus 4.6)*
