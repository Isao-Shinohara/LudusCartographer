================================================================
# まどドラ自律操縦レポート (auto_pilot.py)
停止理由: 手動停止 (Ctrl+C / SIGINT)
日時: 2026-03-12 12:28:15

## 修正/確認したファイルと定数
- `crawler/tools/auto_pilot.py`
  - WATCHDOG_DEADLOCK_THRESHOLD: 600.0 秒
  - WATCHDOG_MAX_TOTAL_RECOVERIES: 3
  - WATCHDOG_EXEMPT_ACTIONS: ADV_WAIT, BATTLE_WAIT, DOWNLOAD_WAIT, GOLD_SWIPE_DOWN, GOLD_SWIPE_LEFT, GOLD_SWIPE_RIGHT, GOLD_SWIPE_UP, GO_CHUI_AGREE, GO_CHUI_FALLBACK, GRIND_QUEST_NAV, LOADING_WAIT, MAIN_STORY_LOADING, MOVIE_WAIT, NOTICE_DISMISS
  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒
  - DOWNLOAD_WAIT: 10.0 秒/ループ (無限忍耐・Watchdog免除)
  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB

## 現在の画面状況
- 最終アクション : CAND_STORY_TAP
- 現在シーン    : UNKNOWN
- 最終OCRテキスト: 挑戦, 中, 1-2, 福成, テートリアル, -y
- ホーム到達    : False

## 実行統計
- 総ループ数           : 47
- 総タップ数           : 50
- OCR実行回数          : 24
- OCRスキップ          : 45
- 暗転スキップ         : 0
- SIGSEGV回避回数      : 0
- Watchdog復旧試行     : 0
- エビデンス保存数     : 3
- 平均判定速度         : 4905 ms/loop

## 主要検知成功率
- Dialog検知           : 0 回
- Finger検知           : 0 回
- GoldBtn検知          : 0 回

## テレメトリ
- 平均遷移時間         : 0.3s
- 最大遷移時間         : 0.3s
- 10秒超遷移回数       : 0
- 計測サンプル数       : 1

## 戦績サマリー
- ホーム到達           : 未到達
- チュートリアル       : 進行中
- 周回モード           : OFF
- 周回完了数           : 0
- 最終シーン           : UNKNOWN
- Rank                 : In Progress

## 最新コミット
- commit: unknown
- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/unknown
================================================================