# STATUS.md — LudusCartographer 進捗管理

最終更新: 2026-03-19 (HSV→テンプレートマッチ移行 & 堅牢化)

---

## 現在のフェーズ: HSV→テンプレートマッチ移行 & 解像度非依存化

### 2026-03-19 HSV→テンプレートマッチ移行 & 堅牢化 (36コミット)

#### テンプレートマッチ移行結果
- `detect_tutorial_gold_swipe()`: HSV Phase 2 削除 → テンプレのみ
- `smart_tap_button()`: HSV → gold_frame_small テンプレ
- `find_gold_frame_near()`: HSV → 複数コーナー中心推定 + direction パラメータ
- `detect_tutorial_gold_button_tap()`: **HSV 維持が妥当** (テンプレは偽陽性過多)

#### scrcpy / 解像度非依存化
- scrcpy max-size: 720 → 1520 (テンプレマッチ精度向上、ウィンドウは720維持)
- 真っ黒キャプチャ検出 → ADB フォールバック
- prepare_analysis_image: 画像実サイズで判定 (デバイス解像度非依存)

#### detect_scene_early 判定改善
- TUTORIAL_WALK → BATTLE → ADV → MOVIE の優先順序を整理
- MOVIE 中 dist 3-15 でバトル/ガチャ/ADV テンプレチェック
- MOVIE 長期滞留脱出 (15回で UNKNOWN)
- battle_special 単独 BATTLE 判定に UI 二重確認

#### 未解決課題
- ADV ↓ボタン検出漏れ: キャプチャタイミングで↓が見えない瞬間がある
- scrcpy Quartz キャプチャの不安定さ (ADB フォールバックで対処)

---

## 過去のフェーズ: ホームチュートリアル突破 → お知らせポップアップ対応

### 2026-03-18 Auto Pilot 安定化 & ドキュメント整備

#### 完了項目

**シーン判定の修正**
- MOVIE 無条件早期リターンを削除し ADV/BATTLE 判定を優先
- detect_scene_early に analysis 画像（1520x720）を渡すよう修正（生画像のスケール不一致解消）
- ホーム画面チュートリアル判定に指/金枠の存在チェックを追加（暗転のみの判定を改善）

**盲目的タップの排除**
- UNKNOWN 時の SCENE_TAP 中央タップを 2 箇所削除
- 盲目的フォールバックタップを 8 箇所削除（STALL_CORNER, WFC_CENTER_ESCAPE 等）
- MOYA_TAP 直前に phash 比較を追加（シーン遷移中のタップ防止）

**動画シーンの安定化**
- MOVIE 初回遷移時の直前タップによる一時停止を即時解除

**ダイアログ・ポップアップ**
- DIALOG_BLUR_GUARD でページドット+▷/×があれば PAGING 続行
- お知らせポップアップのページ送り+×閉じを確実に実行
- お知らせ一覧画面を検出して×で閉じる
- ご注意ハンドラを「今日は表示しない」検出時にスキップ

**チュートリアル操作精度**
- TAP_HIGHLIGHTED_NAV でテンプレートマッチを OCR より優先（戻るボタン等のアイコン対応）

**ドキュメント**
- README をクイックスタート中心に整理（Mac+Android 4 ステップ）
- docs/auto_pilot_setup.md を詳細ガイドとして新規作成
- 対応状況表（Mac/Windows × Android/iOS/Steam）を追加

#### テスト状況
```
Pytest: 550 passed, 26 skipped
```

#### 残課題

| 優先度 | 課題 |
|-------|------|
| 高 | ポップアップ偽陽性（ページドット+背景ぼかし）がバトル画面で発生 |
| 高 | BATTLE テンプレ初回検出が金枠オーバーレイで失敗する場合がある |
| 中 | お知らせポップアップのページ送りで「ダイアログ消失」と誤判定されるケース |
| 中 | BROWSER_ESCAPE がお知らせテキスト内の「WEB SHOP」で誤発火 |
| 低 | ご注意ハンドラのテキスト判定をテンプレートマッチに置き換え |

---

## GitHub

https://github.com/Isao-Shinohara/LudusCartographer
