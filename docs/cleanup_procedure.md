# DB・スクショのクリーンアップ手順

CLAUDE.md §14 から外出し。ユーザーが「クリーンアップして」と指示した場合、**セッション・画面データのみ**をクリアする。**OCR 修正ルール・学習パターンは絶対に削除しない（厳格）。**

## 削除対象（セッション関連データのみ）

1. **crawler/storage/ludus.db**: 以下のテーブルのみ DELETE → VACUUM（スキーマは保持）
   ```sql
   DELETE FROM lc_node_mappings;
   DELETE FROM lc_master_edges;
   DELETE FROM lc_master_nodes;
   DELETE FROM lc_session_graphs;
   DELETE FROM lc_scc_groups;
   DELETE FROM lc_transitions;
   DELETE FROM lc_tappable_items;
   DELETE FROM lc_screen_groups;
   DELETE FROM lc_screens;
   DELETE FROM lc_sessions;
   DELETE FROM lc_projects;
   DELETE FROM auto_pilot_state;
   VACUUM;
   ```
   ※ `crawler/ludus.db` は未使用。本体は `crawler/storage/ludus.db`
2. **crawler/storage/screenshots/**: 中身を全削除（ディレクトリは残す）
3. **crawler/storage/reinstall/**: 中身を全削除（ディレクトリは残す）
4. **crawler/storage/evidence/**: 中身を全削除（ディレクトリは残す）
5. **crawler/evidence/**: 中身を全削除（ディレクトリは残す）
6. **crawler/screenshots/**: 中身を全削除（ディレクトリは残す）
7. クリーンアップ後に行数とディスク使用量を確認して報告する

## 保護対象（絶対に削除しない）

| リソース | 内容 | 理由 |
|---------|------|------|
| `lc_ocr_corrections` テーブル | OCR 修正ルール (手動編集から自動抽出) | 周回を重ねて蓄積した学習資産 |
| `crawler/storage/ocr_learned_patterns.json` | OCR 学習パターン | 同上 |

- 「クリーンアップして」は上記の削除対象のみを実行する
- OCR ルールの削除が必要な場合は「OCR ルールも含めてクリーンアップして」等の**明示的な指示が必要**
- 迷った場合は確認してから実行する

## 起動コマンドの使い分け（厳格）

| ユーザー指示 | 操作 |
|-------------|------|
| **「再起動して」** | `-S -s` で起動（`-r` は付けない、クリーンアップしない） |
| **「クリーンアップして新規スタート」** | 本ドキュメントのクリーンアップ実行 → `-S -s -r` で起動 |

- `-r` は **ユーザーが「新規」「-r」「クリーンアップして新規スタート」と明示的に指示した場合のみ** 付与する
- 「再起動」「起動して」だけの場合は絶対に `-r` を付けない（CLAUDE.md §13「--fresh-install は指示直後の1回のみ」参照）
