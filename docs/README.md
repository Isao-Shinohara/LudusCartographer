# docs/ インデックス

LudusCartographer のドキュメント一覧。CLAUDE.md から外出しされた詳細仕様 + リファレンス文書。

## CLAUDE.md から直接リンクされる詳細仕様 (CORE)

該当する CLAUDE.md セクションを編集する際に参照する。

| ファイル | CLAUDE.md 該当 | 内容 |
|---------|---------------|------|
| `tutorial_autopilot.md` | §12 | チュートリアル自律操縦の詳細ルール |
| `scene_detection_rules.md` | §13 / §15-5 | シーン検出・矩形テンプレマッチ実装規約 |
| `troubleshooting.md` | §8 / §10 | リトライ実装例・OCR フォールバック・ADB 復旧 |
| `evidence_recording.md` | §9 | 証拠記録ディレクトリ構造・JSON スキーマ |
| `cleanup_procedure.md` | §14 | クリーンアップ削除 SQL 全文・保護対象 |
| `screen_recorder.md` | §16 | スクリーン記録 startup_phase 制御・Fingerprint 設計 |
| `anchor_matching_design.md` | §17 | アンカーマッチング詳細 (類似度計算・ノイズ除去) |
| `merge_sort_algorithm.md` | §19 | SafeInsert 詳細アルゴリズム |
| `gemini_prompt_design.md` | §22 | Gemini REST/SDK 送信テンプレ・効果測定 |
| `design/master_node_tags.md` | §21 | タグ機能設計 (シーン/詳細タグ管理・モーダル) |

## リファレンス文書 (KEEP, CLAUDE.md 非リンク)

ピンポイントで参照する補助文書。CLAUDE.md には載せないが現役。

| ファイル | 内容 |
|---------|------|
| `image_recognition.md` | auto_pilot の画像認識手法の網羅的記述 |
| `setup.md` | 全体セットアップガイド & トラブルシューティング |
| `auto_pilot_setup.md` | Auto Pilot 詳細セットアップ |
| `UxPlay_setup.md` | iPhone ミラーリング (macOS) セットアップ |
| `ocr_improvement_plan.md` | OCR 改善計画 (Phase 1-4)、MEMORY.md で参照 |

## ディレクトリ

| パス | 内容 |
|------|------|
| `adr/` | Architecture Decision Records (重要な設計判断の履歴) |
| `schema/` | DB スキーマ定義 |
| `design/` | 機能設計書 (`master_node_tags.md` 等) |
| `history/` | セッション履歴 (`YYYY-MM-DD_HH.md` 形式) |
| `_archive/` | 過去の計画書・相談録 (実装完了・代替策確立で運用外) |

## アーカイブされたファイル (`_archive/`)

過去の計画書・技術相談録。実装完了または運用ルール (CLAUDE.md) で代替済み。
歴史的経緯の参照用に保存、新規参照はしない。

| ファイル | アーカイブ理由 |
|---------|---------------|
| `PROMPT_CONTEXT.md` | 旧 auto_pilot 実装コンテキスト (現在は CLAUDE.md §12 で運用) |
| `gemini_consultation.md` | グルーピング手法相談録 (CLAUDE.md §16/§22 で運用ルール確立済み) |
| `fingerprint_redesign_plan.md` | Fingerprint 再設計計画 (実装完了) |
| `cross_session_merge.md` | クロスセッションマージ設計 (CLAUDE.md §16/§17 で運用済み) |
| `anchor_matching_implementation_plan.md` | アンカーマッチング実装計画 (§17 PHASE_DEFS に統合済) |
| `ROADMAP.md` | 2026-03-03 当時のロードマップ (Phase 4-G/5-B 時代、現状と乖離) |
