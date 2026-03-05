# CLAUDE.md — LudusCartographer 運用憲法

このファイルはプロジェクト全体の運用ルールを定める憲法です。
Claude Code はこれらのルールを厳守して作業を行います。

---

## 0. チュートリアル自律操縦マニュアル

※このルールは、ホーム画面到達までの「誘導」がある間のみ適用する。
ホーム画面到達後は解除し、状況に応じた論理的判断に戻す。

### 優先度チェックリスト（上から順に評価）

| 優先度 | 条件 | アクション |
|--------|------|-----------|
| **1. 指差し・ハイライト** | 画面上に指差しアイコン・黄色い枠・ゴールドハイライトが存在する | **即タップ**（OCR テキスト解析より優先。ガイドが指す対象の座標を叩く） |
| **2. ダイアログ・ポップアップ** | X ボタン・OK・確認・閉じる・はい・次へ・完了・決定 が表示されている | **該当ボタンをタップ** |
| **3. スキップ・ストーリー進行** | 「スキップ」「SKIP」ボタンが見える / 下部に会話ウィンドウがある | **スキップボタン優先タップ** → なければ画面中央タップでセリフ送り |
| **4. バトル AUTO + 待機** | バトル UI（AUTO, 通常攻撃, Turn, WAVE 等）が表示されている | **AUTO ボタンをタップしてオン** → バトル終了まで wait（リザルト・勝利・クリアが見えたらタップで進む） |
| **5. ダウンロード待機** | 「追加データ」「ダウンロード中」「Loading」が表示されている | **一切タップせず wait を繰り返す**（完了まで粘り強く待つ） |

### 共通ルール
- **高精度タップ:** タップ前は 0.5 秒静止し、必ず座標の「中心点」を狙う
- **ガイド対象の座標を叩く:** OCR で「ここをタップ！」を検出した場合、そのテキストではなくガイドが指し示す対象ボタンの座標をタップする

---

## 1. プロジェクト概要

**LudusCartographer（ルードゥス・カルトグラファー）**
AIにモバイルゲームを自律実行させ、すべてのUIを「地図を作るように」記録・検索可能にするシステム。

| 項目 | 内容 |
|------|------|
| 動作環境 | M2 Mac (Local), 実機 (iOS/Android), MySQL, GCS, PHP 8.x |
| 技術スタック | Appium, PaddleOCR, Twig, Tailwind CSS, Playwright |
| テストフレームワーク | Pytest (Mobile/Crawler), Playwright (Web E2E) |

---

## 2. 自動コミットルール

- **変更が正常に動作した**、または**テストをパスした**タイミングで即座に `git commit` を実行すること
- コミットメッセージは以下の形式に従う（Conventional Commits）：
  ```
  <type>: <subject>

  <body（任意）>

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  ```
- type の例: `feat`, `fix`, `test`, `chore`, `docs`, `refactor`

---

## 3. テストファーストルール

- 主要機能の実装前に、必ずテストを先に作成すること
  - Crawler / Mobile: **Pytest** でテストを作成
  - Web (PHP): **Playwright** で E2E テストを作成
- テストが失敗した状態でコードをコミットしてはならない

---

## 4. 自己修復ルール

テストが失敗した場合、以下の手順を守ること：

1. 失敗ログを完全に読み込む
2. 原因を特定し、修正案をユーザーに提示する
3. ユーザーの承認を得た上で（または明示的な自律モードの場合）修正を実行する
4. 修正後、テストを再実行して通過を確認する
5. 通過後に即座にコミットする

---

## 5. 継続的記録ルール

各セッション終了前に必ず以下を実行すること：

- `STATUS.md` を最新の状態に更新する
- 対話の要約を `docs/history/YYYY-MM-DD_HH.md` 形式で保存する

---

## 6. ディレクトリ構造

```
LudusCartographer/
├── crawler/            # Python: Appium + PaddleOCR クローラー
│   ├── tests/          # Pytest テスト
│   ├── config/         # 設定ファイル（.gitignore対象）
│   └── venv/           # Python 仮想環境（.gitignore対象）
├── web/                # PHP: Twig + Tailwind 検索 UI
│   ├── src/            # PHP ソース
│   ├── templates/      # Twig テンプレート
│   ├── public/         # ドキュメントルート
│   └── vendor/         # Composer 依存（.gitignore対象）
├── tests/              # Playwright E2E テスト
├── docs/
│   ├── history/        # セッション要約ログ
│   └── schema/         # MySQL スキーマ定義
├── STATUS.md           # 進捗管理
└── CLAUDE.md           # 本ファイル（運用憲法）
```

---

## 7. イテレーティブ開発ルール

実機検証・クローラー開発は必ず最小単位で進めること：

1. **最小単位で実機確認:** 一気に完成させず、「アプリ起動のみ」「1タップのみ」などの
   最小単位で実機動作を確認し、ユーザーの OK を得てから次のステップへ進む
2. **ステップ間のコミット:** 各最小単位の検証が成功した時点で即座にコミットする
3. **ユーザー確認ゲート:** 実機の画面状態・OCR結果・スクリーンショットを提示し、
   進行可否をユーザーに確認してから次の操作を実行する

---

## 8. ゲーム解析堅牢化ルール

Appium によるゲーム操作では以下を標準実装すること：

### リトライ戦略
- XML要素検索には **最大3回のリトライ（1秒間隔）** を標準実装する
- 実装パターン（Python）:
  ```python
  import time
  def find_element_with_retry(driver, by, value, retries=3, interval=1.0):
      for i in range(retries):
          try:
              return driver.find_element(by, value)
          except Exception:
              if i < retries - 1:
                  time.sleep(interval)
      return None
  ```

### OCRフォールバック
- XML要素が取得できない場合、PaddleOCR の座標データを用いた
  **「座標指定タップ」** へフォールバックする
- フォールバック時はログに `[FALLBACK_OCR_TAP]` プレフィックスを付けて記録する

---

## 9. 証拠記録ルール

クローラーが行うすべてのアクションについて、以下をセットで保存すること：

```
crawler/evidence/<session_id>/<timestamp>_<action>/
├── before.png          # アクション前のスクリーンショット
├── after.png           # アクション後のスクリーンショット
└── ocr_result.json     # PaddleOCR解析結果（テキスト・座標・信頼スコア）
```

- `ocr_result.json` の形式:
  ```json
  {
    "timestamp": "2026-03-03T00:00:00",
    "action": "tap",
    "target": "ショップボタン",
    "ocr_boxes": [
      {"text": "ショップ", "confidence": 0.98, "box": [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]}
    ]
  }
  ```
- これにより「なぜその判断をしたか」を後から追跡可能にする

---

## 10. ADB 接続・復旧マニュアル

### デバイス自動検出
`get_android_serial()` が以下の優先順位で Android デバイスを検出する:
1. 環境変数 `ANDROID_UDID`（最優先）
2. 環境変数 `ANDROID_SERIAL`
3. `adb devices` から最初のオンラインデバイス（USB / Wi-Fi 両対応）

### USB 接続
```bash
# デバイス確認
adb devices
# 出力例: f6b8cef7	device
```

### Wi-Fi 接続
```bash
# 1. USB 接続状態で TCP/IP モードに切り替え
adb tcpip 5555

# 2. USB を外して Wi-Fi 接続
adb connect 192.168.10.118:5555

# 3. 接続確認
adb devices
# 出力例: 192.168.10.118:5555	device
```

### 接続が切れた場合の復旧
```bash
# Wi-Fi 再接続
adb connect 192.168.10.118:5555

# それでもダメなら USB 再接続 → tcpip 再設定
adb tcpip 5555
adb connect 192.168.10.118:5555
```

### 環境変数設定例
```bash
# Wi-Fi 接続のデバイスを固定指定
export ANDROID_UDID=192.168.10.118:5555

# USB 接続のデバイスを固定指定
export ANDROID_SERIAL=f6b8cef7
```

---

## 11. 禁止事項

- テスト未通過のコードをコミットすること
- `.env` や認証情報ファイルをコミットすること
- セッション終了時に `STATUS.md` を更新しないこと
- ユーザーの確認なしに実機で連続操作を実行すること
