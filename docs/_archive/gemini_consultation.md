# LudusCartographer — グルーピング手法に関する技術相談

## 本ドキュメントの目的

LudusCartographer プロジェクトにおける「スクリーンショットのグルーピング（タグ付け）」の最適な手法について、第三者の視点から評価・助言をいただきたい。
現在の実装状況、試行錯誤の経緯、検討中の方針を共有した上で、見落としている手法やアプローチの欠陥がないかレビューを依頼する。

---

## 1. プロジェクト概要

### 1.1 ミッション

**LudusCartographer（ルードゥス・カルトグラファー）** は、AIにモバイルゲームを自律実行させ、すべてのUI画面を「地図を作るように」記録・検索可能にするシステムである。

具体的には：
- ゲームを新規インストールからチュートリアル完了まで自動操縦する
- 操縦中に遭遇するすべてのUI画面をスクリーンショットとして記録する
- 記録した画面群を整理・分類し、ゲームのUI構造を「地図」として可視化する
- 最終的に、任意の画面をテキスト検索で発見できるようにする

### 1.2 技術スタック

| 項目 | 技術 |
|------|------|
| 自動操縦 | Python + ADB (Android Debug Bridge) |
| 画面キャプチャ | scrcpy (Quartz Window Capture) |
| 画面認識 | テンプレートマッチング (OpenCV) + OCR (macOS Vision / PaddleOCR) |
| データ保存 | SQLite (ローカル) |
| 画像形式 | WebP (Q80 フル / Q60 サムネイル) |
| ダッシュボード | PHP + Twig + Tailwind CSS |
| 対象ゲーム | 魔法少女まどかマギカ Magia Exedra (Android) |

### 1.3 動作フロー概要

```
[自動操縦ループ] ─ 約2-4秒/回転
│
├─ 1. スクリーンショット取得 (scrcpy ウィンドウキャプチャ, 1440x720)
├─ 2. phash（知覚ハッシュ）計算 → 前フレームとの差分検出
├─ 3. 画面変化あり → OCR 実行 + シーン判定
│     (BATTLE / ADV / MOVIE / MENU / GACHA / LOADING / UNKNOWN 等)
├─ 4. シーンに応じたアクション決定・タップ実行
├─ 5. タップ直前にスクリーンショットを強制保存（force=True）
└─ 6. 次ループへ
```

---

## 2. 現在のスクリーン記録システム

### 2.1 リアルタイム記録（auto_pilot 実行中）

**記録タイミング：**
- 通常: OCR分析後、画面が新しいと判断された場合
- 強制: タップ実行の直前（操作した全画面を漏れなく記録）

**重複排除：**
- OCRテキストがある場合: テキストを正規化 → SHA-256ハッシュの先頭16文字 = fingerprint
- テキストがない場合: phash（知覚ハッシュ）をfingerprintとして使用
- 全セッションのfingerprintをメモリ上のSetで管理し、O(1)で重複チェック

**OCRテキスト正規化ルール：**
- 信頼度 0.3 以上のテキストのみ採用
- 純粋な数字、時刻パターン（HH:MM）を除外
- 日本語 or 英語を含むテキストのみ
- 画面上の位置順にソート（上→下、左→右）
- `|` 区切りで結合 → SHA-256

**記録データ（1画面あたり）：**

```
lc_screens テーブル:
  - fingerprint: コンテンツ識別子
  - title: OCR上位3テキスト（表示用）
  - parent_fp: 直前に記録した画面のfingerprint（遷移リンク）
  - phash: 知覚ハッシュ（類似度クラスタリング用）
  - screenshot_path: フルサイズWebPのパス
  - thumbnail_path: 320px幅サムネイルのパス
  - ocr_text: 全OCRテキスト（検索用）
  - scene: auto_pilotのシーン判定ラベル
  - discovered_at: タイムスタンプ
```

**1周（チュートリアル新規〜ホーム到達）の記録量：**
- 所要時間: 約90分（うちDL待ち30-40分）
- 総ループ数: 約200回
- 記録スクリーンショット数: 100〜200枚
- ストレージ: 約30-50MB

### 2.2 バッチ処理（自動操縦完了後）

自動操縦が完了すると、記録済みスクリーンショットに対して以下のバッチ処理を実行する。

**Phase 1: グルーピング（時系列+シーンラベル）**
- 同一シーンラベルが連続する画面を1グループにまとめる
- 60秒以上の時間ギャップがあれば別グループに分離
- 自動ラベル: 「バトル#1」「ストーリー#2」等（シーン名+連番）
- 結果例: 697枚 → 81グループ

**Phase 2: 重複排除（phashクラスタリング）**
- phash距離 < 8 の画像をクラスタにまとめる
- 各クラスタの中央の画像を代表画像として選出（is_representative=1）
- 非代表画像は `thinned/` サブディレクトリに移動（削除はしない）
- 結果例: 697枚 → 605枚（代表画像）

**Phase 3: 高品質OCR再処理**
- 代表画像のみに対してPaddleOCRをフル解像度で実行
- `ocr_text_hq` カラムに高品質テキストを保存

**Phase 4: セッション統合**
- 代表画像を `final/` ディレクトリにコピー（fingerprint名で重複排除）
- 複数セッション・複数周回の結果を統合

**Phase 5: 意味的分類（未実装）**
- 当初の構想: CLIP ONNX によるローカル画像分類
- 本相談の対象

### 2.3 現在のDBスキーマ（全テーブル）

```sql
-- セッション管理
CREATE TABLE lc_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT UNIQUE NOT NULL,
    screens_found INTEGER DEFAULT 0,
    started_at    TEXT,
    status        TEXT DEFAULT 'completed',
    game_title    TEXT DEFAULT 'Unknown Game',
    device_mode   TEXT DEFAULT 'SIMULATOR'
);

-- 画面記録（中核テーブル）
CREATE TABLE lc_screens (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    fingerprint       TEXT NOT NULL,
    title             TEXT NOT NULL,
    depth             INTEGER DEFAULT 0,
    parent_fp         TEXT,          -- 直前画面のfingerprint
    phash             TEXT,
    screenshot_path   TEXT,
    ocr_text          TEXT,
    discovered_at     TEXT,
    thumbnail_path    TEXT,
    scene             TEXT,          -- BATTLE, ADV, MENU, MOVIE, etc.
    group_id          INTEGER,       -- Phase 1 で付与
    is_representative BOOLEAN DEFAULT 0,  -- Phase 2 で付与
    cluster_id        INTEGER,       -- Phase 2 で付与
    ocr_text_hq       TEXT           -- Phase 3 で付与
);

-- タップ可能要素
CREATE TABLE lc_tappable_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    screen_id  INTEGER NOT NULL,     -- FK → lc_screens.id
    text       TEXT NOT NULL,
    confidence REAL DEFAULT 0
);

-- 画面グループ
CREATE TABLE lc_screen_groups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    label        TEXT NOT NULL,       -- 「バトル#1」等
    scene        TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    screen_count INTEGER DEFAULT 0
);

-- 自動操縦状態の永続化
CREATE TABLE auto_pilot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- プロジェクト管理
CREATE TABLE lc_projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    game_title TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## 3. 現在のグルーピングの問題点

### 3.1 Phase 1（時系列+シーンラベル）の限界

現在の Phase 1 グルーピングは「シーンラベルの変化」と「60秒の時間ギャップ」のみでグループを分割している。この手法には以下の根本的な問題がある。

**問題1: シーンラベルが粗すぎる**
- auto_pilot のシーンラベルは操縦判断用に設計されており、UI分類用ではない
- 例えば「MENU」ラベルには、ホーム画面・パーティ編成・ガチャ結果・設定画面が全て含まれる
- 「BATTLE」にもチュートリアルバトル・通常バトル・ボス戦が全て含まれる

**問題2: 時間ギャップでの分割が不正確**
- 同じシーンが60秒以上続く場合（長いバトル等）でも分割されない
- 逆に、短い動画カットインを挟んだだけで別グループになる

**問題3: 画面の「意味」を捉えていない**
- 「チュートリアル第1章のバトル」と「ストーリー第2章のバトル」の区別がつかない
- メニュー階層（ホーム → ショップ → アイテム一覧）の深度が分からない

### 3.2 Phase 2（phashクラスタリング）の限界

**問題4: 視覚的類似 ≠ 意味的類似**
- 異なるキャラのバトル画面はUIレイアウトが同じなのでphashが近い → 同一クラスタになる
- 同じメニューでもタブ切り替えでOCRが変わると別クラスタになる

**問題5: セッション間の対応付けが弱い**
- fingerprintが完全一致でなければ別画面扱い
- OCRの微妙な読み取り差異で同じ画面が別fingerprintになることがある

---

## 4. 検討した手法と評価

### 4.1 CLIP ONNX によるローカル分類（当初の Phase 5 構想）

```
画像 → CLIP エンコーダ → 512次元ベクトル → クラスタリング（DBSCAN等）
```

| 項目 | 評価 |
|------|------|
| コスト | $0（ローカル実行） |
| 処理速度 | ~50ms/枚 |
| 利点 | 視覚的類似性を高精度で捉える。phashより賢い |
| 欠点 | ゲーム固有の「意味」は理解できない。「1章のバトル」と「2章のバトル」は同じクラスタになる可能性が高い |
| 評価 | phash の上位互換としては有用だが、**グルーピングの根本問題は解決しない** |

### 4.2 Gemini Flash による AI 分類

```
画像 → Gemini 2.5 Flash API → ラベル・タグ
```

| 項目 | 評価 |
|------|------|
| コスト | ~$0.7/90分（動画ネイティブ入力の場合） |
| 利点 | ゲーム文脈を理解した意味のあるラベルを生成できる |
| 欠点 | フレーム単位の分類は冗長。全フレームに聞くのは非効率 |
| 評価 | ラベル付けには強いが、**全フレームに適用するにはコスト過大** |

### 4.3 ハイブリッド（CLIP クラスタリング + Gemini ラベリング）

```
Step 1: CLIP でクラスタリング → 30-50グループ（$0）
Step 2: 各グループの代表画像のみ Gemini に送信 → ラベル付け（~$0.05/周）
```

| 項目 | 評価 |
|------|------|
| コスト | ~$0.05/周 |
| 利点 | CLIPの視覚グルーピング + Geminiの意味理解 |
| 欠点 | CLIPが作るクラスタが意味的に正しいとは限らない。結果としてラベルも不正確になる |
| 評価 | 費用対効果は良いが、**グルーピングの質がCLIPの限界に律速される** |

### 4.4 遷移グラフベース（新提案・現在の推奨案）

```
Step 1: 画面Aでタップ → 画面Bに遷移 という事実データをエッジとして記録
Step 2: グラフの連結成分・コミュニティ検出で自動グルーピング
Step 3: 各グループの代表画面のOCRテキストからラベル自動生成
Step 4: (任意) 自動ラベルが不十分なグループのみ Gemini に問い合わせ
```

| 項目 | 評価 |
|------|------|
| コスト | Step 1-3: $0、Step 4: ~$0.01/周 |
| 利点 | AIの推測ではなく事実データ（操作記録）に基づく。画面間の関係性が正確に記録される |
| 欠点 | 1周で通らなかった遷移は記録されない（周回で蓄積される設計） |
| 評価 | **グルーピングの根本問題を構造的に解決する。プロジェクトのミッション「地図を作る」に最も合致** |

---

## 5. 遷移グラフ方式の詳細設計（推奨案）

### 5.1 既存データの活用

現在の実装には既に遷移の「種」が存在する。

**既にあるもの：**
- `lc_screens.parent_fp`: 直前に記録した画面のfingerprint
  - 現状は「記録順の直前画面」を指すだけで、「タップによる遷移」とは限らない
- `tap_device()`: タップ前に force=True でスクリーンショットを保存している
  - つまり「タップ前の画面」と「タップ座標」の情報は既に記録の流れに乗っている
- `lc_tappable_items`: 各画面のOCRテキスト+座標+信頼度

**不足しているもの：**
- タップの「結果」としてどの画面に遷移したか（from → to の明示的な対応）
- タップ座標とタップ対象テキストの紐付け
- 遷移エッジ専用のテーブル

### 5.2 提案するスキーマ拡張

```sql
-- 画面間の遷移記録
CREATE TABLE lc_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    from_screen_id  INTEGER NOT NULL,     -- FK → lc_screens.id
    to_screen_id    INTEGER NOT NULL,     -- FK → lc_screens.id
    tap_x           INTEGER,              -- タップ座標 (analysis座標系 1440x720)
    tap_y           INTEGER,
    tap_label       TEXT,                 -- タップ対象のOCRテキスト（あれば）
    action_name     TEXT,                 -- auto_pilotのアクション名
    transition_time REAL,                 -- 遷移にかかった秒数
    discovered_at   TEXT
);

-- インデックス（グラフ探索用）
CREATE INDEX idx_transitions_from ON lc_transitions(from_screen_id);
CREATE INDEX idx_transitions_to ON lc_transitions(to_screen_id);
CREATE INDEX idx_transitions_session ON lc_transitions(session_id);
```

### 5.3 記録フロー（既存コードへの最小変更）

```
tap_device(x, y, state, desc) の中で:
  1. タップ前: force=True でスクショ保存 → from_screen_id を取得（既存）
  2. ADB タップ実行（既存）
  3. タップ後: 次ループで新画面が記録された時点で → to_screen_id を取得
  4. lc_transitions に (from_screen_id, to_screen_id, x, y, desc) を INSERT（新規）
```

変更量は screen_recorder.py に数十行程度の追加で済む見込み。

### 5.4 グラフベースのグルーピング

遷移グラフが構築されれば、以下の手法でグルーピングが可能：

**5.4.1 連結成分（Connected Components）**
- 遷移で到達可能な画面群を1グループにする
- 最も基本的だが、ゲーム全体が1つの連結成分になる可能性が高い

**5.4.2 コミュニティ検出（Louvain法等）**
- 密に相互遷移する画面群を自動的にコミュニティとして検出
- 例: バトル画面群（通常攻撃→スキル→リザルト）が自然に1コミュニティになる
- ライブラリ: networkx (Python) で数行で実装可能

**5.4.3 階層的グルーピング**
- ホーム画面を起点として、遷移の深さ（BFS）で階層を付与
- 深さ0: ホーム画面
- 深さ1: ホームから1タップで到達できる画面（ショップ、パーティ等）
- 深さ2: 2タップで到達できる画面
- UI設計の「サイトマップ」に相当する最も直感的な地図表現

### 5.5 自動ラベリング

各グループの代表画面のOCRテキストから自動命名：
- 最頻出テキストをグループ名に採用（例: 「ショップ」が多いグループ → 「ショップ」）
- シーンラベルとの組み合わせ（例: BATTLE + 「第1章」テキスト → 「第1章バトル」）
- AI不要で実装可能

---

## 6. 費用比較まとめ

チュートリアル1周（約90分）あたりの費用：

| 手法 | 費用/周 | 10周 | グルーピング精度 |
|------|---------|------|-----------------|
| 現状（phash + シーンラベル） | $0 | $0 | 低（シーンラベルが粗い） |
| CLIP ONNX ローカル | $0 | $0 | 中（視覚的類似のみ） |
| Gemini Flash 全フレーム | ~$0.7 | ~$7 | 高（意味理解あり）但し冗長 |
| ハイブリッド (CLIP + Gemini) | ~$0.05 | ~$0.5 | 中〜高 |
| **遷移グラフ（推奨）** | **$0** | **$0** | **高（事実データに基づく）** |
| 遷移グラフ + Gemini補正 | ~$0.01 | ~$0.1 | 最高 |

---

## 7. 相談事項

以下の点について意見を求めたい。

### 7.1 遷移グラフ方式の妥当性
- この方式で「ゲームUIの地図」として十分な品質のグルーピングが実現できるか？
- 見落としている欠点や、実装上のリスクはあるか？

### 7.2 グラフアルゴリズムの選択
- Louvain コミュニティ検出 vs BFS 階層化 vs その他
- ゲームUIのグルーピングに最適な手法はどれか？

### 7.3 CLIP の位置づけ
- 遷移グラフ方式を採用した場合、CLIP の役割はあるか？
- 例えば「遷移グラフでグルーピング → CLIP で視覚的サブ分類」のような併用は有効か？

### 7.4 Gemini の活用場面
- 遷移グラフ + ローカル処理で大部分が解決する前提で、Gemini を使うとしたらどの場面が最もコスパが良いか？
- 例: グループ名の洗練、異常検出、テスト仕様書の自動生成 等

### 7.5 他に検討すべきアプローチ
- 上記で検討していない手法で、このユースケースに適したものはあるか？
- 特にローカル処理（$0）で実現可能な手法を重視する

---

## 8. 補足情報

### 8.1 プロジェクトの制約
- **費用最小化を重視**: ローカル処理を優先し、API費用は最小限に抑えたい
- **Python 3.9.6 環境**: macOS 上で動作
- **SQLite**: 大規模データは想定しない（1ゲームあたり数千画面程度）

### 8.2 ゲームの特性
- モバイルRPG（魔法少女まどかマギカ Magia Exedra）
- チュートリアルは約60分（DL除く）
- 画面遷移パターン: ホーム → メニュー → バトル準備 → バトル → リザルト → ホーム のループ
- ダイアログ・ポップアップが頻出（お知らせ、チュートリアルガイド等）
- 動画カットイン、ADV（アドベンチャー）シーンあり

### 8.3 GitHub リポジトリ
https://github.com/Isao-Shinohara/LudusCartographer

---

*本ドキュメントは Claude Code (Opus 4.6) によって作成されました。*
*最終更新: 2026-04-13*
