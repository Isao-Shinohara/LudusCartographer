# クロスセッションマージ設計書

## 概要

LudusCartographer は周回ごとにセッションを生成し、各セッション内でUI画面を記録・クラスタリングする。
周回を重ねることでUI地図（遷移グラフ）が成長していく。

本機能は、複数セッションの遷移グラフを正確にマージし、ゲーム全体のUI地図を構築する。

## 設計思想

```
セッション内: 時系列ベースのクラスタリング（直前クラスタとの比較）
  ↓
セッション終了時: アンカーポイント + 遷移グラフ構造でマスターグラフに統合
  ↓
マスターグラフ: 全セッションの統合済みUI地図
```

### なぜセッション分離が必要か

異なるセッションの異なる場面（例: 別ステージのバトル）が共通テキスト（例: "AUTO 通常攻撃"）で
誤マージされる問題を防ぐ。セッション内は時系列の文脈があるため誤マージが起きにくいが、
セッションを横断すると文脈が失われる。

## 実装フェーズ

### Phase A: セッション内クラスタリング分離（実装済み）

`background_worker.py` の `_run_incremental_clustering()` と `_merge_clusters_by_phash()` に
`session_id` フィルタを追加。各セッションの画像は自セッション内でのみクラスタリングされる。

- `cluster_id` はグローバルに一意（セッション間で重複しない）
- `rep_map`（代表画像マップ）はセッション内の代表のみ
- 直前クラスタ追跡もセッション内に限定

### Phase D: マスターグラフテーブル

| テーブル | 用途 |
|---------|------|
| `lc_master_nodes` | マスターグラフのノード（セッション横断の統合済み画面） |
| `lc_master_edges` | マスターグラフのエッジ（統合済み遷移） |
| `lc_node_mappings` | セッション→マスターのノード対応関係 |
| `lc_session_graphs` | セッション別グラフの構築状態 |

#### lc_master_nodes

| カラム | 型 | 説明 |
|-------|-----|------|
| master_fp | TEXT PK | マスターノードの fingerprint |
| representative_screen_id | INTEGER | 最も品質の高いスクショの screen id |
| title | TEXT | 画面タイトル |
| scene | TEXT | シーン分類 (BATTLE, ADV, MENU 等) |
| phash | TEXT | 代表画像の phash |
| ocr_text | TEXT | OCR テキスト |
| bfs_depth | INTEGER | ホームからの BFS 深さ |
| scc_id | INTEGER | SCC グループ ID |
| scc_label | TEXT | SCC ラベル |
| visit_count | INTEGER | 何セッションで観測されたか |
| first_seen_at | TEXT | 初回観測日時 |
| last_seen_at | TEXT | 最終観測日時 |

#### lc_master_edges

| カラム | 型 | 説明 |
|-------|-----|------|
| from_master_fp | TEXT | 遷移元ノード |
| to_master_fp | TEXT | 遷移先ノード |
| tap_label | TEXT | タップしたUI要素名 |
| action_name | TEXT | アクション名 |
| count | INTEGER | 遷移回数 |
| avg_duration | REAL | 平均遷移時間（秒） |
| min_duration | REAL | 最短遷移時間（秒） |

#### lc_node_mappings

| カラム | 型 | 説明 |
|-------|-----|------|
| session_id | TEXT | セッション ID |
| session_fp | TEXT | セッション内の fingerprint |
| master_fp | TEXT | マスターノードの fingerprint |
| match_method | TEXT | マッチ手法 (anchor, k_hop, transition, new) |
| match_score | REAL | マッチスコア (0.0〜1.0) |

### Phase B: セッション別グラフ構築

`build_graph()` に `session_id` パラメータを追加し、セッションごとに独立した遷移グラフを構築。
構築結果を `lc_session_graphs` に保存。

### Phase C: クロスセッションマージ（核心）

#### アルゴリズム概要

```
1. マスターが空 → 最初のセッションをそのままコピー
2. アンカーポイント検出
   - ホーム画面 (GOAL_HOME_REACHED の遷移先)
   - 収束点 (入次数上位のノード)
   - 静的UI (テキスト量が多い MENU シーン)
3. アンカー同士をマッチング
   - phash 距離 (重み 0.3)
   - テキスト Jaccard 類似度 (重み 0.5)
   - scene 一致ボーナス (重み 0.2)
   - 合計スコア >= 0.6 でマッチ
4. マッチしたアンカーから k=2 hop で拡張マッチング
5. 残りのノードは transition_similarity で追加マッチング
6. マッチしたノード → visit_count++ / lc_node_mappings に記録
7. マッチしないノード → マスターに新規追加
8. エッジをマージ（既存は count 加算、新規は追加）
9. BFS depth + SCC 再計算
```

#### 成長シナリオ

```
1周目: A → B → C → D（ホーム）
  ↓ マスターに投入
マスター: A → B → C → D

2周目: A → B → X → Y → C → D
  ↓ A,B,C,D はマッチ、X,Y は新規追加
マスター: A → B → C → D
               ↘       ↗
                X → Y
```

### Phase E: API・可視化

- `get_graph` API を `lc_master_nodes` + `lc_master_edges` ベースに切り替え
- `visit_count` でノードサイズを変える
- セッション別表示モード追加

### Phase F: BackgroundWorker 統合

- セッショングラフ構築完了後に自動マージ（300秒間隔）
- 未マージセッションを検出して順次処理

## 使い方

### CLI

```bash
# セッション別クラスタリング（自動）
# -S: スクリーン記録有効（UI地図構築に必須）
# -s: ホーム画面到達で停止
auto_pilot.py -S -s   # BackgroundWorker が session_id 付きで動作

# マスターグラフ再構築（手動）
python -m tools.cross_session_merger rebuild

# 特定セッションをマージ
python -m tools.cross_session_merger merge --session ap_20260414_115829
```

### API

```
GET /api/search.php?action=get_graph                    → マスターグラフ
GET /api/search.php?action=get_graph&session_id=ap_...  → セッション別グラフ
```
