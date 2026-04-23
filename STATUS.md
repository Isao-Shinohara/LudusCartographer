# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-04-23

## 現在のブランチ
- `feature/screen-recorder` (main 未マージ)

## 最終セッション (2026-04-23)
- 主な作業: セッション物理削除・CycleState分離・Live不採用フィルタ分離・クラスタバリデーション・scrcpy自動復旧
- コミット: 未コミット

## 現在の状態
- **DB**: クリーンアップ済み（セッション0件、OCR修正ルール15,205件保護）
- **マスター**: 空（クリーンアップ済み）
- **自動マージ**: 廃止済み

## 未コミットの変更

### 大きな変更

#### PilotState / CycleState 分離
- `state.py`: PilotState（6フィールド、周回引継ぎ）と CycleState（~78フィールド、周回リセット）に分離
- 全ファイル ~960箇所を `state.xxx` → `state.cycle.xxx` に書き換え
- `reset_for_new_cycle()` → `CycleState()` 再作成に簡素化
- 互換レイヤー実装→全書き換え後に削除済み
- CLAUDE.md §13 にルール追記

#### セッション物理削除機能
- `EvidenceRepository.php`: `deleteSession()` 書き換え — running→discarded、未マージ→全削除、マージ済み→非代表のみ削除
- `search.php`: セッションディレクトリ自動クリーンアップ
- `screen_recorder.py`: `check_discarded()` 追加、`start_new_session()` で discarded 上書き防止
- `background_worker.py`: `session_id` プロパティ追加
- `auto_pilot.py`: discarded 検出→自動新セッション作成
- `dashboard.html.twig`: 全状態に削除ボタン、確認ダイアログを状態別に表示

#### is_artifact 値分離（Gemini/ユーザー区別）
- `is_artifact`: 0=通常、1=Gemini不採用、2=ユーザー不採用
- `EvidenceRepository.php`: toggleScreenArtifact で 0↔2 トグル、レスポンス/フィールドを(int)に
- `dashboard.html.twig`: タイトル[Gemini不採用]/[不採用]/[非代表]、ドット色分け（赤/オレンジ/灰）

#### Liveタブ フィルタ再構成
- 採用 / 不採用 / 非代表 / 未処理 / すべて の5フィルタに分離
- Shift+クリック: 採用フィルタ→不採用ボタン、不採用フィルタ→採用ボタン
- 0件時のメッセージをフィルタ別に表示
- 暗い表示（opacity-40）は「すべて」フィルタ時のみ

#### Liveタブ 同一クラスタパネル
- モーダルに同一クラスタ表示（Finalと共通）
- 代表変更機能（master_fp不要でも動作）
- 表示中画像: 暗めオレンジ枠、代表: ★バッジ、選択中: 緑枠
- 初期表示時に選択画像までスクロール
- 非代表画像では採用/不採用ボタン非表示

#### クラスタ内バリデーション
- `background_worker.py`: `_validate_clusters()` — テキスト空メンバーが代表とphash距離>=12なら新クラスタに分離
- dedup 後に毎回実行

#### artifact 判定後の代表入れ替え
- `background_worker.py`: Gemini artifact 判定で代表降格時、同クラスタ非artifactメンバーから新代表を選出

#### scrcpy 自動復旧
- `auto_pilot.py`: scrcpy ウィンドウ未生成時に adb kill-server → start-server → scrcpy 再起動（最大3回リトライ）

#### tap_device スクショスキップログ
- `device.py`: 暗転スキップ、キャプチャ失敗、maybe_record失敗、ゲーム非フォアグラウンド、例外のログ追加

#### 2周目スクショ消失バグ修正
- `auto_pilot.py`: 周回リセット後に `state.cycle.recorder = recorder` を再設定（`reset_for_new_cycle` で None にリセットされていた）

#### 未処理フィルタ修正
- `dashboard.html.twig`: Gemini OCR完了も「処理済み」として扱う（`!has_hq_ocr && !has_gemini`）

### 前回セッションからの継続変更
- startup_phase制御改善
- アニメーションループ救済カウンタ
- 自動マージ廃止
- gemini remerge 1件ごとcommit
- toggle_screen_artifact / check_screen_master / adopt_and_rebuild API
- cross_session_merger: is_artifactフィルタ

## 未解決の課題
1. ~~ループ救済カウンタのリセット問題~~ → CycleState分離で解決
2. **DL完了ダイアログのOK押下失敗**: 動画ループの根本原因（未調査）
3. **anchor_matcher Phase 1の候補複数問題**: 同一テキストのマスターノードが複数ある場合にスキップされる
4. **Gemini がキャラカットシーンを artifact と誤判定**: プロンプト改善が必要
5. **2周目ADVスクショ不足の根本原因**: `state.recorder` リセット漏れは修正済み。次回周回で検証

## 設計ドキュメント
- `docs/merge_sort_algorithm.md` — SafeInsert 仕様
- `docs/anchor_matching_design.md` — 段階的 Phase 設計
