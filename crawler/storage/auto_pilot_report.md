================================================================
# まどドラ自律操縦レポート (auto_pilot.py)
停止理由: 手動停止 (Ctrl+C / SIGINT)
日時: 2026-03-06 18:35:46

## 修正/確認したファイルと定数
- `crawler/tools/auto_pilot.py`
  - WATCHDOG_DEADLOCK_THRESHOLD: 600.0 秒
  - WATCHDOG_MAX_TOTAL_RECOVERIES: 3
  - WATCHDOG_EXEMPT_ACTIONS: BATTLE_WAIT, DOWNLOAD_WAIT, GOLD_SWIPE_DOWN, GOLD_SWIPE_LEFT, GOLD_SWIPE_RIGHT, GOLD_SWIPE_UP, GO_CHUI_AGREE, GO_CHUI_FALLBACK, LOADING_WAIT, MAIN_STORY_LOADING, NOTICE_DISMISS
  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒
  - DOWNLOAD_WAIT: 10.0 秒/ループ (無限忍耐・Watchdog免除)
  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB

## 現在の画面状況
- 最終アクション : MOYA_TAP
- 現在シーン    : UNKNOWN
- 最終OCRテキスト: 个, まどドラ
- ホーム到達    : False

## 実行統計
- 総ループ数           : 4
- 総タップ数           : 1
- OCR実行回数          : 2
- OCRスキップ          : 3
- 暗転スキップ         : 0
- SIGSEGV回避回数      : 0
- Watchdog復旧試行     : 0
- エビデンス保存数     : 1
- 平均判定速度         : 2653 ms/loop

## 主要検知成功率
- Dialog検知           : 0 回
- Finger検知           : 1 回
- GoldBtn検知          : 0 回

## 戦績サマリー
- ホーム到達           : 未到達
- チュートリアル       : 進行中
- 最終シーン           : UNKNOWN
- Rank                 : In Progress

## 最新コミット
- commit: e6bf81b
- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/e6bf81b
================================================================