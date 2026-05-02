# Fingerprint 設計見直し + マージ処理修正 計画書

作成日: 2026-04-30
対象ブランチ: `feature/screen-recorder`
推定作業時間: **8〜15 時間** (慎重実装 + 検証込み)

## 🟢 実施済み (2026-04-30)

ユーザー方針「既存データの修復はなくてよい」に従い、Phase 0/1/2 を完了。

- **Phase 0** (事前検証): A 案 (数字保持) で OK と判断
- **Phase 1** (コード修正): コミット `322b2b0` で実施
  - `screen_recorder._normalize_ocr` から数字除去を削除
  - `cross_session_merger.merge_to_master` / `_add_all_as_new` に直接 fp 一致チェック追加
  - `PHASE_DEFS` に `direct_fp_match` 追加
  - 新規テスト 6 件、全 109 件 PASS
- **Phase 2** (シミュレーション): 合成 3 セッションで動的検証 — master 削除 0 件、`direct_fp_match` 6 件記録 ✓
- **Phase 3** (ドキュメント): STATUS.md / docs/history/2026-04-30.md / CLAUDE.md §16 更新 (本コミット)

既存データのマイグレーションは省略 (Big Bang 再構築なし)。新規セッションから新ロジック適用。

## 1. 背景と目的

### 観測された不具合
マスターのソート順が時系列に合わない。例: sort 92 (3202 MB) の直後に sort 93 (1575 MB) が来てダウンロードゲージが巻き戻る。

### 根本原因 (3 つの絡み合い)

| # | 場所 | 何が起きてるか |
|---|---|---|
| ❶ | `screen_recorder.py:892` `_normalize_ocr` | `re.sub(r"\d+", "")` で OCR の数字を全削除 → 「Download 2046 MB」「Download 1083 MB」が同 fp に |
| ❷ | `cross_session_merger.py:398-422` `merge_to_master` else 分岐 | 既存 master_fp との直接一致チェックなし → 同 fp なのに「new」と記録 |
| ❸ | `merge_sort_strategy.py` SafeInsert + L444 DELETE | 配置不能な「new」を `result.skipped` → マスターから削除 |

3 段がチェーンして **seed の 30 ノードが消失**。穴を後続セッションのノードが埋め、時系列が崩れる。

### 影響範囲データ
- fp 衝突 (5 セッション以上) **14 件**
- 衝突のうち「**正しい衝突**」(同一 dialog/UI): **59 fp** (3 セッション以上)
- 衝突のうち「**バグ衝突**」(進捗違い等): **25 fp**
- 影響テーブル: lc_screens (16,305行), lc_master_nodes (1,034), lc_master_edges (12,984), lc_transitions (147,908), lc_node_mappings (5,695), lc_anchor_judgments (2,614)
- コード参照箇所 **597 箇所**

## 2. 設計方針の選択

### 「正しい衝突」と「バグ衝突」の区別

| 種類 | 例 | 現挙動 | 期待 |
|---|---|---|---|
| 正しい衝突 | 「それだけは覚えていたし、それだけしか覚えていない」 (5 セッションで全く同じセリフ) | 同 fp (1 master node) | ✅ そのまま |
| バグ衝突 | 「Download 2046 MB」「Download 1083 MB」 (異なる進捗) | 同 fp (1 master node に重なる) | ❌ 別 fp / 別 master node にしたい |

→ **数字を含むテキストは数字を維持、それ以外は変更なし**が理想。

### 設計案 (3 つの選択肢)

#### A 案: 全数字保持 (最もシンプル)
```python
# OLD
text = re.sub(r"\d+", "", text).strip()
# NEW
# (数字をそのまま保持)
```
- 利点: 単純、確実
- 欠点: 「Turn 1」「Turn 2」「WAVE 1」「WAVE 2」も別 fp に → バトル中の冗長記録増加。クラスタリングで吸収できれば問題ないが要検証

#### B 案: ノイズ語数字パターンのみ除去
```python
# 「Turn N」「WAVE N」「Lv.N」等の特定パターンは除去 (= 数字含めて全体を消す)
# それ以外の数字は保持
NUM_NOISE_PATTERNS = [
    r'\b(Turn|WAVE|wave|Lv\.?|HP|MP|MAX|NEXT|STEP)\s*\d+\b',
    # ... 必要に応じて追加
]
for pat in NUM_NOISE_PATTERNS:
    text = re.sub(pat, '', text)
# 残りの数字は保持
```
- 利点: 細かく制御可能、副作用小
- 欠点: パターンメンテが必要、ゲーム固有

#### C 案: phash ベース fingerprint
```python
# fingerprint = phash の先頭 16 hex
# 視覚的に異なれば別 fp
```
- 利点: OCR ノイズに強い、自然
- 欠点: テキスト一致による fp 一致が消える (例: 「私は、魔法少女だった」セリフが微妙な phash 違いで別 fp に)
- AnchorMatcher のテキストベース判定と二重で動くので OK?
- しかし master_fp の「意味」が変わる (テキスト ID → 視覚 ID)。既存ロジックに広く影響

### 推奨: **A 案 + クラスタリング強化**

理由:
- B 案はメンテ負担、ゲーム固有パターン依存
- C 案は設計大変更、副作用予測困難
- A 案は単純で予測可能。デメリット (バトル冗長) は既存クラスタリングで吸収できれば OK
- 既存クラスタリング (Step A: phash + Step B: dHash + Jaccard) は同一テンプレ別数字を「同クラスタ」にまとめる能力あり (CLAUDE.md §16 「テキスト一致 → 同クラスタ」)

**A 案の前提条件**: クラスタリングが「同テンプレ別数字」を 1 クラスタにまとめられる必要あり。事前検証で確認する。

## 3. 段階的実装プラン

### Phase 0: 事前検証 (1 時間)

**目的**: A 案を実装したとき、クラスタリングが「Turn 1」「Turn 2」を 1 クラスタにまとめてくれるか検証。

**手順**:
1. テストスクリプト作成: 既存セッションから同一バトルの「Turn 1」「Turn 2」「Turn 3」フレームを抽出
2. 数字を保持した状態で fingerprint を計算
3. 同じクラスタリングロジック (`background_worker._run_incremental_clustering`) で評価
4. 期待: phash 距離が小さく Step A で同クラスタ化される

**判定**:
- 通れば → A 案で進める
- 通らなければ → B 案に切替検討

### Phase 1: 短期修正 — データ損失停止 (2 時間)

**目的**: 既存の master ノード削除を即座に停止 (= ❷ 修正)。fingerprint 設計はまだ変えない。

**変更点**:
1. `cross_session_merger.py:398` else 分岐に直接 fp 一致チェック追加
   ```python
   else:
       existing = self._conn.execute(
           "SELECT 1 FROM lc_master_nodes WHERE master_fp = ? AND version_id = ?",
           (s_fp, self._version_id)
       ).fetchone()
       if existing:
           # 直接 fp 一致 → アンカー扱い
           UPDATE master visit_count++, last_seen_at
           INSERT mapping method='direct_fp_match'
       else:
           # 既存ロジック (新規挿入)
           INSERT OR IGNORE master
           INSERT mapping method='new'
   ```

2. `anchor_matcher.PHASE_DEFS` に `direct_fp_match` を追加 (UI 表示用)

3. テスト追加:
   - `test_merge_to_master_direct_fp_match`: 同 fp が既存に存在する場合 'direct_fp_match' で記録、'new' にはならない
   - `test_seed_node_not_deleted_on_collision`: 同 fp 衝突で seed が削除されない

4. **既存マージのリビルド**: 削除済み master nodes を回復するため `rebuild_master()` を 1 回実行 (UI のリビルドボタン経由)

**期待**:
- master ノード数 1,034 → ~1,070+ (削除されてた 30 が復活)
- 削除されてた 19ca870ff371d6d4 等が復活
- ただしまだ「同 fp に複数進捗が重なる」状態は残る (= 巻き戻りは緩和されるが完全には直らない)

### Phase 2: Fingerprint 再設計 — A 案 (3-5 時間)

**目的**: 数字保持で「Download 1365 MB」と「Download 2046 MB」を別 fp にする。

**変更点**:

1. `screen_recorder.py:_normalize_ocr` の数字除去削除
   ```python
   # 削除する行 (line 891-892):
   #   text = re.sub(r"\d+", "", text).strip()
   #   if not text: continue
   ```

2. **マイグレーション戦略**:
   - **Option α (Big Bang)**: 全 lc_screens.fingerprint を再計算 → 既存 master/mapping/transition リセット → 全セッションを再マージ
     - 利点: クリーンな状態、整合性 100%
     - 欠点: 大規模 DB 変更、検証コスト高
   - **Option β (混在許容)**: 新規キャプチャから新 fp、既存はそのまま → 当面マージは古いセッション中心
     - 利点: 影響小
     - 欠点: 永続的な状態混在、整合性が読みにくい
   - **Option γ (旧 fp 保持 + 新カラム追加)**: `lc_screens.fingerprint_v2` を追加、新規だけ v2 で記録
     - 利点: ロールバック容易
     - 欠点: スキーマ複雑化、永続的な負債

   **推奨: Option α**。整合性最優先、テスト DB あるしクリーンスタートのが安全。

3. **マイグレーションスクリプト** (`crawler/tools/migrate_fingerprints.py` 新規):
   - 全 lc_screens の OCR を新ロジックで再 fingerprint
   - 同セッション内の重複 fp を統合 (= 同一 OCR は同 fp、これは正しい衝突)
   - lc_node_mappings, lc_transitions, lc_master_nodes, lc_master_edges, lc_anchor_judgments を更新 or 削除
   - 全セッションを再マージ (rebuild_master)

4. テスト追加:
   - `test_fingerprint_no_digit_strip`: 「Download 1365 MB」と「Download 2046 MB」が別 fp
   - `test_fingerprint_same_dialog_same_fp`: 同一 dialog (数字なし) は同 fp
   - 既存 `test_screen_recorder.py` の fingerprint テストを再走 (壊れないこと確認)

5. **クラスタリング閾値の再調整**:
   - 数字が違う「Turn 1」「Turn 2」が phash 近傍で同クラスタになるか実測
   - 必要なら `LC_TEXT_SEPARATION` フラグ等の閾値を調整

### Phase 3: 検証 + チューニング (2-4 時間)

1. ユーザー報告ケースの再検証:
   - sort 92 → 93 で巻き戻りが解消されているか
   - 全 master のソート順をスクリーンショットと付き合わせて目視確認

2. 副作用チェック:
   - master ノード数の変化 (1,034 → どう変わったか)
   - クラスタ数の変化
   - アンカー数の変化 (P1〜P6 の比率)

3. パフォーマンス測定:
   - マージ時間 (Phase 0 では 5 分台が標準)
   - キャッシュヒット率 (`lc_anchor_judgments`)

4. 必要なら閾値調整

### Phase 4: ドキュメント更新 (1 時間)

1. `STATUS.md` 更新
2. `docs/history/2026-04-XX.md` セッション要約
3. `CLAUDE.md` の fingerprint 関連記述更新 (§16 等)
4. 設計書 `docs/fingerprint_design.md` 新規作成

## 4. リスクと緩和策

| リスク | 緩和策 |
|---|---|
| マイグレーション失敗で DB 損傷 | バックアップ → 検証後にバックアップ削除 |
| バトル画面が冗長化 (Turn 毎に別 fp) | Phase 0 で事前検証、クラスタリング閾値で吸収 |
| AnchorMatcher のキャッシュが無効化 (master_fp 変わる) | 受け入れる (累計 2,614 → 0)。再マージで再構築 |
| 想定外の依存箇所 (597 参照箇所) | コードレビュー + grep で全箇所確認 |

## 5. 各フェーズの開始判定

```
Phase 0 (検証) → ユーザー承認 → Phase 1 (短期修正) →  実機検証 →
Phase 2 (fingerprint 再設計) → 実機検証 → Phase 3 (チューニング) → Phase 4 (ドキュメント)
```

各 Phase 終了時にユーザーに状態を報告し、次の Phase に進むかどうか判断を仰ぐ。

## 6. 成功基準

- ✅ 巻き戻り解消: ダウンロード進捗が master のソート順で単調増加
- ✅ データ損失なし: seed 由来の master ノードが削除されない
- ✅ テスト 100% PASS (既存 + 新規)
- ✅ マージ時間が許容範囲 (Phase 0 比較で +20% 以内)
- ✅ ドキュメント更新完了
