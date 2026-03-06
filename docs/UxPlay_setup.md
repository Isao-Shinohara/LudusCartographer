# UxPlay セットアップガイド — macOS + iPhone ミラーリング

> **動作確認済み環境:**
> macOS (Apple Silicon / Intel)、Homebrew、iOS 18 以降の iPhone。
> 本ドキュメントは実際のミラーリング成功時のログ・エラー解決手順をもとに作成しています。

---

## 目次

1. [UxPlay とは](#1-uxplay-とは)
2. [依存ライブラリのインストール](#2-依存ライブラリのインストール)
3. [UxPlay のビルドとインストール](#3-uxplay-のビルドとインストール)
4. [【重要】起動エラーの解決（GStreamer リンクエラー）](#4-重要起動エラーの解決gstreamer-リンクエラー)
5. [起動と主なオプション](#5-起動と主なオプション)
6. [iPhone 側の接続操作（iOS 18 以降）](#6-iphone-側の接続操作ios-18-以降)
7. [動作確認と終了方法](#7-動作確認と終了方法)
8. [トラブルシューティング早見表](#8-トラブルシューティング早見表)

---

## 1. UxPlay とは

**リポジトリ:** [https://github.com/FDH2/UxPlay](https://github.com/FDH2/UxPlay)

UxPlay は macOS / Linux 上で動作するオープンソースの AirPlay サーバーです。
iPhone / iPad の画面ミラーリング映像を Mac のウィンドウに表示します。

**このプロジェクトで採用した理由:**

| 項目 | 内容 |
|------|------|
| USB 不要 | Wi-Fi 経由の AirPlay — iPhone を物理接続しない |
| 軽量 | GStreamer を利用したシンプルな実装 |
| macOS 実績 | Apple Silicon / Intel 双方で動作報告が豊富 |
| オープンソース | MIT ライセンス、継続的にメンテナンス中 |

---

## 2. 依存ライブラリのインストール

Homebrew で必要なライブラリを一括インストールします。

```bash
brew install \
  cmake \
  pkg-config \
  libplist \
  openssl@3 \
  gstreamer \
  gst-plugins-base \
  gst-plugins-good \
  gst-plugins-bad \
  gst-libav \
  gobject-introspection
```

> **所要時間の目安:** 初回は GStreamer のビルドが含まれるため 5〜15 分程度かかります。

---

## 3. UxPlay のビルドとインストール

```bash
git clone https://github.com/FDH2/UxPlay.git
cd UxPlay
```

```bash
mkdir build && cd build
```

```bash
cmake ..
```

```bash
make
```

```bash
sudo make install
```

インストール後、以下のコマンドでパスが通っていることを確認します。

```bash
which uxplay
# 出力例: /usr/local/bin/uxplay
```

---

## 4. 【重要】起動エラーの解決（GStreamer リンクエラー）

### 発生する現象

`uxplay` を実行すると、以下のいずれかのエラーが出て起動しない場合があります。

```
dyld: Library not loaded: libgobject-2.0.0.dylib
```

```
gi.repository.Gst が見つかりません
```

```
GStreamer plugin scanner or gst-plugin-scanner not found
```

### 原因

Homebrew でインストールした GStreamer 関連ライブラリのパスが、macOS のダイナミックリンカーとランタイムに伝わっていないためです。

### 解決策：シェル設定ファイルに環境変数を追加

シェルに合わせてどちらかを編集してください。

**zsh（macOS デフォルト）の場合:**

```bash
echo '# UxPlay & GStreamer Settings' >> ~/.zshrc
echo 'export DYLD_LIBRARY_PATH=$(brew --prefix)/lib:$DYLD_LIBRARY_PATH' >> ~/.zshrc
echo 'export GI_TYPELIB_PATH=$(brew --prefix)/lib/girepository-1.0' >> ~/.zshrc
```

**bash の場合:**

```bash
echo '# UxPlay & GStreamer Settings' >> ~/.bashrc
echo 'export DYLD_LIBRARY_PATH=$(brew --prefix)/lib:$DYLD_LIBRARY_PATH' >> ~/.bashrc
echo 'export GI_TYPELIB_PATH=$(brew --prefix)/lib/girepository-1.0' >> ~/.bashrc
```

### 設定の反映

```bash
# zsh の場合
source ~/.zshrc

# bash の場合
source ~/.bashrc
```

### 追記内容の確認

```bash
# 正しいパスが展開されているか確認
echo $DYLD_LIBRARY_PATH
# 出力例: /opt/homebrew/lib:  （Apple Silicon）
#         /usr/local/lib:     （Intel Mac）

echo $GI_TYPELIB_PATH
# 出力例: /opt/homebrew/lib/girepository-1.0
```

> **ポイント:** `brew --prefix` は Apple Silicon では `/opt/homebrew`、Intel Mac では `/usr/local` を返します。
> `echo` で展開値が正しいことを必ず確認してください。

---

## 5. 起動と主なオプション

### 基本起動

```bash
uxplay
```

起動に成功すると、ターミナルに以下のようなメッセージが表示され、iPhone からの接続待ち状態になります。

```
AirPlay server started, listening on port 7000
mDNS service registered: UxPlay@G-PC-01239960
```

### 主なオプション

| オプション | 説明 |
|------------|------|
| `uxplay -fs` | フルスクリーン表示で起動 |
| `uxplay -avdec` | H.264 ハードウェアデコードを強制（動作が重い場合に有効） |
| `uxplay -n MyMirror` | AirPlay 一覧に表示される名前を任意に変更 |
| `uxplay -p 7000` | 使用ポートを明示指定（デフォルト: 7000） |
| `uxplay -vs 0` | 映像のみ（音声なし）で起動 |

### 使用例

```bash
# 表示名を変更して起動（同一 Wi-Fi に複数台ある場合に便利）
uxplay -n iPhone_Mirror

# 動作が重い場合 — ハードウェアデコード強制
uxplay -avdec

# フルスクリーン + カスタム名
uxplay -fs -n MyMirror
```

---

## 6. iPhone 側の接続操作（iOS 18 以降）

### 事前準備

- Mac と iPhone を**同一の Wi-Fi ネットワーク**に接続してください。
- Mac 側で `uxplay` が起動済みであることを確認してください。

---

### コントロールセンターへの「画面ミラーリング」追加（初回のみ）

iOS 18 では、コントロールセンターのボタン配置をカスタマイズする手順が変わっています。

**手順:**

1. iPhone の**コントロールセンターを開く**
2. 画面の**空白部分（ボタンのない背景）を長押し**する
3. 画面が「編集モード」になる（ボタンが揺れ始める）
4. 画面下部の **「コントロールを追加」** をタップ
5. 一覧から **「画面ミラーリング」**（二重の四角アイコン）を選択して追加

> **iOS 17 以前との違い:** iOS 18 では「設定 → コントロールセンター」から追加する方法と、コントロールセンター内での長押し編集の両方が使えます。本手順はより直感的な後者の方法です。

---

### ミラーリングの接続

1. iPhone の**コントロールセンターを開く**
2. 追加した**画面ミラーリングアイコンをタップ**
3. 表示される AirPlay デバイス一覧から **`UxPlay@[ホスト名]`** を選択

   > 例: `UxPlay@G-PC-01239960`（ホスト名は Mac の設定により異なります）

4. 接続が成功すると:
   - **iPhone 側:** ステータスバーに青いミラーリングアイコンが表示される
   - **Mac 側:** `OpenGL renderer` ウィンドウが開き、iPhone の画面が映し出される

---

## 7. 動作確認と終了方法

### 動作確認

接続成功後、Mac 側のターミナルに以下のようなログが出力されます。

```
New client connected: [iPhone の名前]
Video stream started
OpenGL renderer initialized: 393x852
```

ウィンドウに iPhone の画面がリアルタイムで表示されていれば**セットアップ完了**です。

### 終了方法

```bash
# ターミナルで Ctrl + C を押す
^C
# AirPlay server stopped.
```

iPhone 側のコントロールセンターで「ミラーリングを停止」をタップして切断することもできます。

---

## 8. トラブルシューティング早見表

| 症状 | 原因 | 解決策 |
|------|------|--------|
| `libgobject-2.0.0.dylib` が見つからない | ライブラリパス未設定 | [`DYLD_LIBRARY_PATH` を設定](#4-重要起動エラーの解決gstreamer-リンクエラー) |
| `gi.repository.Gst` エラー | GI typelib パス未設定 | [`GI_TYPELIB_PATH` を設定](#4-重要起動エラーの解決gstreamer-リンクエラー) |
| iPhone の AirPlay 一覧に UxPlay が出ない | Wi-Fi が異なる / mDNS ブロック | Mac と iPhone を同一 Wi-Fi に接続。Mac のファイアウォール設定を確認 |
| 映像が映らない / 黒画面 | デコーダーの問題 | `uxplay -avdec` で再試行 |
| 映像が遅延・カクつく | Wi-Fi 品質の問題 | 5GHz 帯を使用 / Mac と iPhone を近づける |
| `cmake ..` が失敗する | 依存ライブラリ不足 | `brew install` コマンドをすべて再実行 |
| 接続後すぐ切断される | タイムアウト設定 | `uxplay -t 30` でタイムアウトを延長 |
| ポート 7000 が使用中 | 別プロセスが占有 | `uxplay -p 7001` で別ポートを使用 |

---

## 参考リンク

- **UxPlay リポジトリ:** [https://github.com/FDH2/UxPlay](https://github.com/FDH2/UxPlay)
- **GStreamer 公式:** [https://gstreamer.freedesktop.org](https://gstreamer.freedesktop.org)
- **Homebrew:** [https://brew.sh](https://brew.sh)
