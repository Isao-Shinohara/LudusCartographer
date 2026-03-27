"""
ap/handlers/scene.py — シーン固有ハンドラ

Phase 8: ダウンロード二次チェック、クエストマップ/ステージ選択、
バトル画面 (チュートリアル/AUTO/停滞)、バトル結果、ADVシーン。
"""
from __future__ import annotations

import logging
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H, BATTLE_WAIT, DOWNLOAD_WAIT
from tools.ap.context import DetectContext
from tools.ap.device import tap_device
from tools.ap.helpers import has_any, has_text
from tools.ap.image_proc import detect_dialog, detect_dialog_corners, roi_to_device
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def handle_scene_specific(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """シーン固有ハンドラ: DL二次, クエストマップ, バトル, バトル結果, ADV。"""
    ocr = ctx.ocr
    texts = ctx.texts
    joined = ctx.joined
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── ダウンロード/ロード中 (セカンダリチェック) ───
    # ※ メインの厳格判定は関数冒頭の【絶対最優先 #-3】で実施済み。
    # ここではフォールバックとして「ダウンロード」(日本語) + 進捗テキスト の組み合わせのみ検出。
    # 通信速度やネットワーク状態による推測は一切行わない。
    _dl_jp = has_any(ocr, ["ダウンロード", "追加データ"])
    _dl_progress = any("MB" in t or "GB" in t for t in texts)
    if _dl_jp and _dl_progress:
        # ダウンロード確認ダイアログ（「開始しますか？」）はOKタップが先
        if any("開始しますか" in t for t in texts):
            # ConfirmDialog と同じロジッックで OK/キャンセルを探す
            _ok_btn = has_any(ocr, ["OK"])
            if not _ok_btn:
                # OCR が OK を ●K 等に誤読するケースに対応 → 右側のボタン位置を固定推定
                _ok_x, _ok_y = roi_to_device(int(W * 0.69), int(H * 0.82), state.game_roi)
                logger.info(">>> [DOWNLOAD_CONFIRM] ダウンロード確認 → OK 固定位置 (%d,%d) タップ", _ok_x, _ok_y)
                tap_device(_ok_x, _ok_y, state, "DOWNLOAD_CONFIRM_OK")
            else:
                logger.info(">>> [DOWNLOAD_CONFIRM] ダウンロード確認 → OK (%d,%d) タップ",
                            _ok_btn["center"][0], _ok_btn["center"][1])
                tap_device(_ok_btn["center"][0], _ok_btn["center"][1], state, "DOWNLOAD_CONFIRM_OK")
            return "DOWNLOAD_CONFIRM_OK", 2.0
        logger.info(">>> [DOWNLOAD_STRICT_JP] %s + 進捗あり — 待機", _dl_jp["text"])
        state.download_active = True
        return "DOWNLOAD_WAIT", DOWNLOAD_WAIT

    # ─── クエストマップ/ステージ選択 ───
    stage_num = has_any(ocr, ["1-1", "1-2", "1-3", "2-1", "2-2", "2-3",
                               "3-1", "3-2", "4-1", "4-2", "Main"])
    sentu_btn = None
    for _skw in ["戦闘", "出撃", "挑戦"]:
        _sb = has_text(ocr, _skw)
        if _sb and _sb["center"][1] > H * 0.5:
            sentu_btn = _sb
            break
    # 「挑戦」がヘッダー位置(y<H*0.5)でのみ検出された場合:
    # 装飾フォントのボタンテキストをOCRが読めていない → 固定位置タップ
    if not sentu_btn and has_text(ocr, "挑戦") and stage_num:
        _chal_fx, _chal_fy = roi_to_device(int(W * 0.82), int(H * 0.91), state.game_roi)
        logger.info(">>> クエストマップ — 「挑戦」ボタン固定位置 (%d,%d)", _chal_fx, _chal_fy)
        tap_device(_chal_fx, _chal_fy, state, "QUEST_START_CHALLENGE_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0
    if not sentu_btn:
        expl = has_text(ocr, "探索")
        if expl and expl["center"][1] > H * 0.6:
            sentu_btn = expl
    if stage_num and sentu_btn:
        cx, cy = sentu_btn["center"]
        # ROI クランプ: ゲーム領域外 (黒帯) をタップしない
        if state.game_roi:
            _roi_max_y = state.game_roi[1] + state.game_roi[3] - 5
            cy = min(cy, _roi_max_y)
        logger.info("[QuestStart] '%s'(%d,%d) タップ", sentu_btn["text"], cx, cy)
        tap_device(cx, cy, state, f"QUEST_START {sentu_btn['text']}")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0
    elif stage_num:
        fx, fy = roi_to_device(int(W * 0.74), int(H * 0.91), state.game_roi)
        logger.info(">>> クエストマップ(固定) (%d,%d)", fx, fy)
        tap_device(fx, fy, state, "QUEST_START_FIXED")
        state.battle_wait_count = 0
        return "QUEST_START", 2.0

    # ─── バトル画面 ───
    # 「AUTO」「HP」「戦闘」はストーリー画面にも出るため除外、戦闘固有キーワードで判定
    battle_keywords = ["通常攻撃", "单体攻撃", "単体攻撃", "全体攻撃",
                       "隣接攻撃", "必殺技", "巫殺技",  # チュートリアルバトルキーワード追加
                       "BREAK", "Turn", "WAVE"]
    battle = has_any(ocr, battle_keywords)
    if battle:
        state.battle_wait_count += 1

        # バトルチュートリアル: バフ効果
        buff_tut = has_any(ocr, ["バフ効果を発生", "支援するバフ", "CRTアップ", "バフ効果"])
        if buff_tut and has_text(ocr, "ことができます"):
            bx, by = roi_to_device(int(W * 0.888), int(H * 0.667), state.game_roi)
            logger.info(">>> バフチュートリアル (%d,%d)", bx, by)
            tap_device(bx, by, state, "BUFF_TUTORIAL")
            return "BATTLE_TUTORIAL", 0.5

        # バトルチュートリアル: スキル使用
        skill_tut = has_any(ocr, ["スキルを使ってみましょう", "スキを使ってみ",
                                   "戦闘スキルを使", "戦闘スキを使",
                                   "スキルを使用してみ", "使ってみましょう"])
        if skill_tut:
            sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
            logger.info(">>> スキルチュートリアル (%d,%d)", sx, sy)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL", rapid=True)
            tap_device(sx, sy, state, "SKILL_CARD_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.0

        # バトルチュートリアル: 必殺技
        hissatsu_tut = has_any(ocr, ["CTDアップ", "必殺技", "巫殺技"])
        if hissatsu_tut:
            hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
            logger.info(">>> 必殺技チュートリアル (%d,%d)", hx, hy)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL", rapid=True)
            tap_device(hx, hy, state, "HISSATSU_TUTORIAL confirm")
            return "BATTLE_TUTORIAL", 0.0

        # バトルチュートリアル: 攻撃対象変更
        if has_any(ocr, ["攻撃対象を変更", "対象を変更"]):
            ex, ey = roi_to_device(int(W * 0.651), int(H * 0.361), state.game_roi)
            logger.info(">>> 攻撃対象チュートリアル (%d,%d)", ex, ey)
            tap_device(ex, ey, state, "ATTACK_TARGET_TUTORIAL")
            return "BATTLE_TUTORIAL", 0.5

        # バトルチュートリアル: 一般ポップアップ
        tutorial_popup = has_any(ocr, [
            "タイムライン", "表示されている", "行動してい",
            "ここをタップ", "タップしてください",
            "ことができます", "することができ",
            "スキルを使用", "スキルを選択", "カードを選択",
            "ましょう", "みましょう", "てみよう",
            "一番上に", "順番に行動", "DEFENDER",
            # ロール説明ポップアップ
            "ロールについて", "ATTACKER", "BREAKER", "BUFFER", "DEBUFFER", "HEALER",
            # バトル説明ポップアップ
            "STEP1", "STEP2", "バトルシステム", "ブレイクし",
        ])
        # バトル速度ツールチップは速度ボタン本体 (1409,19) をタップして消す
        speed_tip = has_any(ocr, ["このボタンでバトル", "進行速度を変更"])
        if speed_tip:
            _sp_x, _sp_y = roi_to_device(int(W * 0.927), int(H * 0.026), state.game_roi)
            logger.info(">>> 速度ツールチップ → 速度ボタン (%d,%d) タップ", _sp_x, _sp_y)
            tap_device(_sp_x, _sp_y, state, "SPEED_BUTTON_TAP")
            return "BATTLE_TUTORIAL", 0.5
        if tutorial_popup:
            # 四隅テンプレで本物のダイアログか確認
            if analysis_path and not detect_dialog_corners(analysis_path):
                tutorial_popup = None
        if tutorial_popup:
            # ── テンプレートマッチングで ▷/× を優先検出 ──
            _btl_nav = detect_dialog(analysis_path, W, H) if analysis_path else None
            if _btl_nav:
                _btn, _bx, _by = _btl_nav
                logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) [template]",
                            tutorial_popup["text"][:10], "×" if _btn == "close" else "▷", _bx, _by)
                tap_device(_bx, _by, state, "BATTLE_TUTORIAL_POPUP")
                return "BATTLE_TUTORIAL", 0.5
            # フォールバック: ▷ 矢印 → × ボタンのシーケンス
            state.pre_popup_tap_count += 1
            _arr_b = roi_to_device(int(W * 0.91), int(H * 0.49), state.game_roi)
            _cls_b = roi_to_device(int(W * 0.98), int(H * 0.056), state.game_roi)
            _btl_candidates = [_arr_b, _arr_b, _arr_b, _arr_b, _cls_b, _cls_b]
            _bidx = min(state.pre_popup_tap_count - 1, len(_btl_candidates) - 1)
            cx, cy = _btl_candidates[_bidx]
            _blabel = "×" if (cx, cy) == _cls_b else "▷"
            logger.info(">>> バトルチュートリアル popup '%s' %s→(%d,%d) (試行%d回目)",
                        tutorial_popup["text"][:10], _blabel, cx, cy, state.pre_popup_tap_count)
            tap_device(cx, cy, state, "BATTLE_TUTORIAL_POPUP")
            return "BATTLE_TUTORIAL", 0.5

        # バトル停滞時: ハイライト候補を順番にタップ試行
        if state.battle_wait_count > 8:
            stall_phase = (state.battle_wait_count - 8) % 12
            if stall_phase == 0:
                sx, sy = roi_to_device(int(W * 0.947), int(H * 0.722), state.game_roi)
                logger.info(">>> バトル停滞 — スキルタップ (%d,%d)", sx, sy)
                tap_device(sx, sy, state, "STALL_SKILL", rapid=True)
                tap_device(sx, sy, state, "STALL_SKILL confirm")
                return "BATTLE_STALL", 0.0
            elif stall_phase == 4:
                hx, hy = roi_to_device(int(W * 0.862), int(H * 0.778), state.game_roi)
                logger.info(">>> バトル停滞 — 必殺技タップ (%d,%d)", hx, hy)
                tap_device(hx, hy, state, "STALL_HISSATSU", rapid=True)
                tap_device(hx, hy, state, "STALL_HISSATSU confirm")
                return "BATTLE_STALL", 0.0
            elif stall_phase == 8:
                # 探索バトル: 左パネルのキャラカードを再タップ (char_just_selected リセット)
                state.char_just_selected = False
                lx, ly = roi_to_device(int(W * 0.141), int(H * 0.875), state.game_roi)
                logger.info(">>> バトル停滞 — 左カード再タップ (%d,%d)", lx, ly)
                tap_device(lx, ly, state, "STALL_LEFT_CARD")
                return "BATTLE_STALL", 1.0

        # 高回数停滞: auto_activated リセットで再検出
        if state.battle_wait_count > 30:
            logger.warning(">>> バトル長期停滞 (count=%d) — auto_activated/char_justSelected リセット",
                           state.battle_wait_count)
            state.auto_activated = False
            state.char_just_selected = False
            state.battle_wait_count = 0

        logger.info(">>> バトル中 — 待機 (count=%d, auto=%s)",
                    state.battle_wait_count, state.auto_activated)
        return "BATTLE_WAIT", BATTLE_WAIT

    # バトル終了検出
    if state.battle_wait_count > 0:
        logger.info(">>> バトル終了検出 (wait_count was %d)", state.battle_wait_count)
        state.battle_wait_count = 0
        state.auto_activated = False

    # ─── バトル結果/リザルト ───
    result_match = has_any(ocr, ["リザルト", "Result", "RESULT", "勝利", "Victory",
                                  "クリア", "CLEAR", "EXP", "経験値", "ランクアップ"])
    if result_match:
        # まず「次へ」ボタンを優先タップ (右下エリアのみ: y>H*0.6 & x>W*0.5)
        _nxt_r = None
        for _ocr_item_r in ocr:
            _txt_r = _ocr_item_r.get("text", "")
            if "次へ" in _txt_r or "NEXT" in _txt_r:
                _rx_c, _ry_c = _ocr_item_r["center"]
                if _ry_c > H * 0.6 and _rx_c > W * 0.5:
                    _nxt_r = _ocr_item_r
                    break
        if _nxt_r:
            _rx, _ry = _nxt_r["center"]
            logger.info(">>> バトル結果【次へ優先】 (%d,%d) タップ", _rx, _ry)
            tap_device(_rx, _ry, state, "RESULT_NEXT")
            return "RESULT_TAP", 1.0
        cx, cy = result_match["center"]
        text = result_match["text"]
        logger.info(">>> バトル結果 '%s' (%d,%d)", text, cx, cy)
        tap_device(cx, cy, state, "RESULT_TAP")
        return "RESULT_TAP", 1.0

    # ─── ADVシーン: ↓ボタンのみタップ、上部アイコンは無視 ───
    if ctx.adv_result.is_adv:
        if ctx.adv_result.next_btn_pos:
            cx, cy = ctx.adv_result.next_btn_pos
            logger.info(">>> ADV ↓ボタンタップ (%d,%d)", cx, cy)
            tap_device(cx, cy, state, "ADV_NEXT_TAP")
            return "ADV_NEXT_TAP", 0.3
        # ↓ ボタンなし → SKIP ボタンを探す (キャラ紹介/ガチャ演出等)
        _skip_match = has_any(ocr, ["SKIP", "スキップ"])
        if _skip_match:
            sx, sy = _skip_match["center"]
            logger.info(">>> ADV ↓未検出 → SKIP '%s' (%d,%d)", _skip_match["text"], sx, sy)
            tap_device(sx, sy, state, "ADV_SKIP_TAP")
            return "ADV_SKIP_TAP", 1.0
        # ↓テンプレ不一致 + SKIP なし → 盲タップせず None を返して OCR パスへ
        logger.info(">>> ADV ↓未検出 + SKIP なし → フォールスルー")

    return None
