================================================================
# まどドラ自律操縦レポート (auto_pilot.py)
停止理由: 周回 #1 完了
日時: 2026-03-30 17:57:12

## 修正/確認したファイルと定数
- `crawler/tools/auto_pilot.py`
  - WATCHDOG_DEADLOCK_THRESHOLD: 600.0 秒
  - WATCHDOG_MAX_TOTAL_RECOVERIES: 3
  - WATCHDOG_EXEMPT_ACTIONS: ADV_WAIT, BATTLE_WAIT, DOWNLOAD_WAIT, GOLD_SWIPE_DOWN, GOLD_SWIPE_LEFT, GOLD_SWIPE_RIGHT, GOLD_SWIPE_UP, GO_CHUI_AGREE, GO_CHUI_FALLBACK, GRIND_QUEST_NAV, LOADING_WAIT, MAIN_STORY_LOADING, MOVIE_WAIT, NOTICE_DISMISS
  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒
  - DOWNLOAD_WAIT: 10.0 秒/ループ (無限忍耐・Watchdog免除)
  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB

## 現在の画面状況
- 最終アクション : GOAL_HOME_REACHED
- 現在シーン    : MENU
- 最終OCRテキスト: MadoDora, LV., 5, i, MAGIA EXEDRA, 公式サイトはこちら
- ホーム到達    : True

## 実行統計
- 総ループ数           : 3
- 総タップ数           : 0
- OCR実行回数          : 3
- OCRスキップ          : 0
- 暗転スキップ         : 0
- SIGSEGV回避回数      : 0
- Watchdog復旧試行     : 0
- エビデンス保存数     : 1
- 平均判定速度         : 1096 ms/loop

## 主要検知成功率
- Dialog検知           : 1 回
- Finger検知           : 0 回
- GoldBtn検知          : 0 回

## テレメトリ
- 平均遷移時間         : 0.0s
- 最大遷移時間         : 0.0s
- 10秒超遷移回数       : 0
- 計測サンプル数       : 0

## 戦績サマリー
- ホーム到達           : ✓ CLEARED
- チュートリアル       : All Tutorials Cleared
- 周回モード           : ON
- 周回完了数           : 1
- 最終シーン           : MENU
- Rank                 : 1 / HOME REACHED
- 起動種別             : 途中再開

## フェーズタイムライン

| フェーズ | 到達時刻 | 区間時間 |
|---------|---------|---------|
| アプリ起動 | 0:05 | +0:05 |
| ホーム画面到達 | 0:10 | +0:05 |
| **合計** | **0:10** | |

## 最新コミット
- commit: c3c2412
- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/c3c2412
================================================================