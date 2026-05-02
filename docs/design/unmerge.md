# 未マージ（Unmerge）機能 設計書

## 概要

マージ済みセッションを「未マージ」に戻し、残りのセッションからマスターグラフを再構築する機能。

## 動機

- マージ結果が期待と異なる場合にやり直したい
- 問題のあるセッションをマスターグラフから除外したい

## 方式

**全削除 → 再構築（Rebuild） → 復元（Restore）**

リビジョン管理やインクリメンタル引き算は採用しない。理由:
- リビジョン管理: テーブル間依存が複雑でスナップショットの整合性担保が困難
- 引き算方式: BFS depth / SCC の再計算が必須で、ロジック複雑さだけが増す

## 前提条件

- `master_fp` は `lc_screens.fingerprint` を直接使用（決定論的・不変）
- OCR 再処理はマージ前に完了済み → マージ後に fingerprint は変わらない
- 削除済みセッション（transitions/screens が物理削除済み）は遡れない
- タイトルや OCR テキストの手動編集は `master_fp` に影響しない

## 処理フロー

```
1. can_unmerge() — 可否チェック
   - lc_session_graphs に対象セッションが存在するか
   - lc_transitions に対象セッションのデータがあるか
   - lc_screens に対象セッションの代表画像があるか
   - 他にマージ済みセッションが残るか（全セッション unmerge は rebuild_master で空になるだけ）

2. バックアップ — 手動変更の退避
   - lc_master_nodes から全ノードの以下カラムを TEMP TABLE にコピー:
     master_fp, user_excluded, manual_group_id, is_group_representative, title

3. 全削除
   - DELETE FROM lc_master_nodes
   - DELETE FROM lc_master_edges
   - DELETE FROM lc_node_mappings

4. rebuild_master() — 対象セッションを除外して再構築
   - lc_session_graphs から対象セッション以外を built_at 順に取得
   - 各セッションに対して merge_to_master() を順次実行

5. 復元 — 手動変更の再適用
   - master_fp キーでバックアップから user_excluded, manual_group_id,
     is_group_representative, title を UPDATE
   - 再構築後に存在しない master_fp へのバックアップは無視（対象セッション固有ノード）

6. 後処理
   - orphan チェック: representative_screen_id が lc_screens に存在するか確認
     - 存在しない場合、同 fingerprint の別スクリーンで再割当て
   - lc_session_graphs の対象セッションの built_at をクリア（未マージ状態に戻す）
```

## 排他制御

`auto_pilot_state` テーブルに `is_rebuilding` フラグを追加。

- unmerge 開始時: `is_rebuilding = 1` に設定
- unmerge 完了時: `is_rebuilding = 0` に設定
- auto_pilot 側: メインループの先頭で `is_rebuilding` をチェックし、1 なら待機

## テーブル依存マップ

```
変更対象（unmerge で操作）:
  lc_master_nodes  — 全削除 → 再構築 → 手動変更復元
  lc_master_edges  — 全削除 → 再構築
  lc_node_mappings — 全削除 → 再構築
  lc_session_graphs — 対象セッションの built_at クリア

影響なし（セッションローカル）:
  lc_screens       — cluster_id, is_representative は不変
  lc_transitions   — 不変（再構築のソース）
  lc_tappable_items — screen_id 参照、master 非依存
  lc_screen_groups — 不変
  lc_scc_groups    — _recalculate_master_graph() で再計算
```

## 手動変更バックアップ対象

| カラム | 理由 |
|--------|------|
| `user_excluded` | 不採用フラグ |
| `manual_group_id` | 手動グループ統合 |
| `is_group_representative` | グループ代表 |
| `title` | 手動編集されている可能性がある |

全ノードの title を一括バックアップする（title_edited フラグは追加しない）。
数千行程度なら SQLite の TEMP TABLE コストは無視できる。

## API

| アクション | パラメータ | 説明 |
|-----------|-----------|------|
| `can_unmerge` | `session_id` | 可否チェック。`{ok: bool, reason?: string}` |
| `execute_unmerge` | `session_id` | 実行。`{ok: bool, master_nodes: int, master_edges: int}` |

## UI（Merge タブ）

- マージ済みセッション行に「未マージ」ボタンを追加
- `can_unmerge` で不可能な場合はボタンを disabled + ツールチップで理由表示
- クリック時に確認ダイアログ（影響範囲を表示）
- 実行中はスピナー表示

## 制約・注意事項

- 削除済みセッション（lc_transitions がない）は unmerge 不可
- unmerge で消えたノードに対する手動変更は復元不可（許容範囲）
- auto_pilot 実行中は is_rebuilding フラグで待機させる
- 将来的に手動エッジ編集（tap_label）を実装する場合、バックアップ対象に追加が必要
