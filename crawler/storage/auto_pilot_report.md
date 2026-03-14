================================================================
# まどドラ自律操縦レポート (auto_pilot.py)
停止理由: ホーム画面到達 (チュートリアル完了)
日時: 2026-03-14 08:58:36

## 修正/確認したファイルと定数
- `crawler/tools/auto_pilot.py`
  - WATCHDOG_DEADLOCK_THRESHOLD: 600.0 秒
  - WATCHDOG_MAX_TOTAL_RECOVERIES: 3
  - WATCHDOG_EXEMPT_ACTIONS: ADV_WAIT, BATTLE_WAIT, DOWNLOAD_WAIT, GOLD_SWIPE_DOWN, GOLD_SWIPE_LEFT, GOLD_SWIPE_RIGHT, GOLD_SWIPE_UP, GO_CHUI_AGREE, GO_CHUI_FALLBACK, GRIND_QUEST_NAV, LOADING_WAIT, MAIN_STORY_LOADING, MOVIE_WAIT, NOTICE_DISMISS
  - NOTICE_DISMISS (ご注意後Unity初期化待ち): 120 秒
  - DOWNLOAD_WAIT: 10.0 秒/ループ (無限忍耐・Watchdog免除)
  - ダウンロード検出キーワード: ダウンロード/Download/Downloading/%/MB/GB

## 現在の画面状況
- 最終アクション : HOME_REACHED
- 現在シーン    : MENU
- 最終OCRテキスト: MadoDora, Lv., 5, Max, 3,160, 自
- ホーム到達    : True

## 実行統計
- 総ループ数           : 25
- 総タップ数           : 21
- OCR実行回数          : 14
- OCRスキップ          : 13
- 暗転スキップ         : 0
- SIGSEGV回避回数      : 0
- Watchdog復旧試行     : 0
- エビデンス保存数     : 3
- 平均判定速度         : 1360 ms/loop

## 主要検知成功率
- Dialog検知           : 0 回
- Finger検知           : 0 回
- GoldBtn検知          : 0 回

## テレメトリ
- 平均遷移時間         : 1.3s
- 最大遷移時間         : 2.1s
- 10秒超遷移回数       : 0
- 計測サンプル数       : 11

## 戦績サマリー
- ホーム到達           : ✓ CLEARED
- チュートリアル       : All Tutorials Cleared
- 周回モード           : OFF
- 周回完了数           : 0
- 最終シーン           : MENU
- Rank                 : 1 / HOME REACHED
- 起動種別             : 途中再開
- ホーム画面到達 (チュートリアル完了): 0h01m00s

## 最新コミット
- commit: 5338be7
- GitHub: https://github.com/Isao-Shinohara/LudusCartographer/commit/5338be7
================================================================