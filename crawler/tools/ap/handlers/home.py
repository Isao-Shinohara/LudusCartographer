"""
ap/handlers/home.py — ホーム画面検出 + チュートリアル完了判定

Phase 7: ホーム indicator マッチング、指/金枠/暗転によるチュートリアル判定、
吹き出しセリフ、周回モード対応。
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from tools.ap.context import DetectContext
from tools.ap.device import tap_device
from tools.ap.helpers import has_text, log_milestone
from tools.ap.image_proc import (
    ASSET_MANAGER,
    detect_tutorial_gold_button_tap,
    detect_tutorial_overlay,
    find_finger_blobs,
    find_gold_frame_near,
    roi_to_device,
)
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def handle_home(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """ホーム画面検出 + チュートリアル完了判定。"""
    ocr = ctx.ocr
    texts = ctx.texts
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── ホーム画面検出 ───
    # OCR が文字を途中で切る場合がある ("クエスト"→"クエス", "パーティ"→"パーテ")
    # → 短いプレフィックスで双方向部分一致: keyword in text OR text in keyword
    home_indicators = ["光の間", "ショップ", "ガシャ", "ガチャ", "パーティ",
                       "クエスト", "ミッション", "メニュー", "ホーム",
                       "お知らせ", "イベント", "フレンド", "マイページ", "編成",
                       "マップ", "レイヤー"]
    # 短縮プレフィックス (2文字以上一致で検出)
    _home_prefixes = ["光の", "ショッ", "ガシャ", "ガチャ", "パーテ",
                      "クエス", "ミッシ", "メニュ", "ホーム",
                      "お知ら", "イベン", "フレン", "マイペ", "編成",
                      "マッ", "レイヤ"]
    def _home_match(t: str) -> int:
        """home_indicators のうち t にマッチする数を返す (完全 or プレフィックス)"""
        count = 0
        for h, p in zip(home_indicators, _home_prefixes):
            if h in t or p in t or t in h:
                count += 1
        return count
    home_count = sum(min(1, _home_match(t)) for t in texts)
    # 重複排除: 同じ indicator に複数テキストがマッチしても1回
    _matched = set()
    for t in texts:
        for idx, (h, p) in enumerate(zip(home_indicators, _home_prefixes)):
            if idx not in _matched and (h in t or p in t or t in h):
                _matched.add(idx)
    home_count = len(_matched)
    if home_count < 3:
        return None

    state.home_reached = True
    # ── 指アイコン+金枠 → まだホームチュートリアル中 ──
    # 「ホーム画面かつ指+金枠がない」= チュートリアル終了
    # 回数制限なし: 指+金枠+暗転オーバーレイで判定 (カウンタ偽検出は廃止)
    _home_blobs = find_finger_blobs(analysis_path, home_mode=True) if analysis_path else []
    _home_gold = detect_tutorial_gold_button_tap(analysis_path, right_half_only=False) if analysis_path else None
    _home_dimmed = detect_tutorial_overlay(analysis_path) if analysis_path else False
    # tutorial_hand_pointer テンプレートも指の証拠として使用
    # (scrcpy 低解像度では find_finger_blobs の金枠検出が失敗することがある)
    _hand_match = ASSET_MANAGER.match_single("tutorial_hand_pointer", analysis_path) if analysis_path else None
    if _hand_match and _hand_match[2] >= 0.70 and not _home_blobs:
        # ハンドポインタ座標を指ブロブとして追加 (area=10000 ダミー)
        _hx, _hy = _hand_match[0], _hand_match[1]
        logger.info(">>> ホーム: tutorial_hand_pointer(%.2f) (%d,%d) → 指ブロブとして追加",
                    _hand_match[2], _hx, _hy)
        _home_blobs = [(_hx, _hy, 10000.0, _hx - 20, _hy - 20, 40, 40)]
    if _home_blobs or _home_gold:
        _tap_target = None
        if _home_blobs:
            _chosen_blob = max(_home_blobs, key=lambda b: b[2])  # area最大
            _bx, _by = _chosen_blob[0], _chosen_blob[1]
            # ── 優先1: OCR テキスト中心 (指の近傍でフッターナビ外) ──
            _ocr_target = None
            _HOME_NAV_KWS = {"光の間", "ショップ", "ガチャ", "ガシャ", "マップ", "レイヤ",
                             "マッチ", "ユニオン", "クエスト", "クエス", "パーティ", "育成",
                             "ころの器", "こころの器"}
            for _oe in ocr:
                _ot = _oe.get("text", "")
                _oc = _oe.get("center", (0, 0))
                # フッターナビ外のテキストで、指の近傍250px以内
                if (any(kw in _ot for kw in _HOME_NAV_KWS)
                        or len(_ot) < 2):
                    continue
                _odx = abs(_oc[0] - _bx)
                _ody = abs(_oc[1] - _by)
                if _odx < 250 and _ody < 250 and _oc[1] < H * 0.85:
                    _ocr_target = (_oc[0], _oc[1])
                    logger.info(">>> ホームチュートリアル: 指(%d,%d)→OCRテキスト '%s'(%d,%d) [%d回目]",
                                _bx, _by, _ot, _oc[0], _oc[1],
                                state.home_tutorial_tap_count + 1)
                    break
            if _ocr_target:
                _tap_target = _ocr_target
            else:
                # ── 優先2: HSV金枠検出 (フォールバック) ──
                _gf = find_gold_frame_near(analysis_path, _bx, _by, search_radius=250) if analysis_path else None
                if _gf and _gf[1] > H * 0.85:
                    logger.info(">>> ホーム: 金枠(%d,%d) がフッターナビ領域 → 除外", _gf[0], _gf[1])
                    _gf = None
                if _gf:
                    _tap_target = (_gf[0], _gf[1])
                    logger.info(">>> ホームチュートリアル: 指(%d,%d)→金枠(%d,%d) dimmed=%s [%d回目]",
                                _bx, _by, _gf[0], _gf[1], _home_dimmed, state.home_tutorial_tap_count + 1)
                elif _home_gold:
                    _tap_target = _home_gold
                    logger.info(">>> ホームチュートリアル: 指(%d,%d)→GoldBtn(%d,%d) [近傍外] [%d回目]",
                                _bx, _by, *_home_gold, state.home_tutorial_tap_count + 1)
                elif _home_dimmed:
                    _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                    _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
                    logger.info(">>> ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転あり]",
                                _bx, _by, *_tap_target)
                else:
                    if _bx > 150 and _by > 100 and _bx < W - 100 and _by < H - 80:
                        _tip_y = _chosen_blob[4] + int(_chosen_blob[6] * 0.1)
                        _tap_target = (_chosen_blob[3] + _chosen_blob[5] // 2, _tip_y)
                        logger.info(">>> ホームチュートリアル: 指(%d,%d)→指先(%d,%d) [金枠なし+暗転なし・中央付近]",
                                    _bx, _by, *_tap_target)
                    else:
                        logger.info(">>> ホーム指検出: 指(%d,%d) 金枠なし+暗転なし+画面端 → 偽検出疑い、スキップ",
                                    _bx, _by)
        elif _home_gold:
            # 指なし+金枠あり: dimmed でも非 dimmed でもチュートリアル金枠の可能性あり
            _tap_target = _home_gold
            if _home_dimmed:
                logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転あり]", *_home_gold)
            else:
                logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転なし・指なし]", *_home_gold)
        if _tap_target:
            state.home_tutorial_tap_count += 1
            # 指/金枠を検出 → チュートリアル未完了なので HOME_CLEAR_CHECK を常にリセット
            if hasattr(state, '_home_clear_count'):
                state._home_clear_count = 0
            tap_device(_tap_target[0], _tap_target[1], state, "HOME_TUTORIAL_TAP")
            return "HOME_TUTORIAL_TAP", 0.5
        # blob/gold検出あるがタップ対象なし → 指/金枠の存在自体がチュートリアル未完了の証拠
        if hasattr(state, '_home_clear_count') and state._home_clear_count > 0:
            logger.info(">>> 指/金枠検出あり(タップ対象なし) → HOME_CLEAR_COUNT リセット (%d→0)",
                        state._home_clear_count)
            state._home_clear_count = 0
        if state.blob_same_count >= 5:
            logger.info(">>> ホーム画面 + もやスタック → クエストへナビゲート")
            state.blob_same_count = 0
            state.home_nav_count += 1
            quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
            if quest_btn:
                cx, cy = quest_btn["center"]
                logger.info(">>> クエストボタン (%d,%d) タップ", cx, cy)
                tap_device(cx, cy, state, "QUEST_FROM_HOME")
                return "QUEST_FROM_HOME", 3.0
            _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
            tap_device(_qf_x, _qf_y, state, "QUEST_FIXED")
            return "QUEST_FROM_HOME", 3.0
        if state.home_nav_count > 0:
            logger.info(">>> ホーム画面 + 遷移試行 %d回目 → 画面変化待ち", state.home_nav_count)
            return "HOME_NAV_WAIT", 2.0
    # ── 右上吹き出しセリフチェック: まだチュートリアルガイダンス中 ──
    _BUBBLE_EXCLUDE_EXACT = {"AUTO", ">>", ">|", "D1", "×", "+", "■", "畄", "目", "SKIP"}
    _BUBBLE_EXCLUDE_SUBSTR = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora", "M.8", "M8X")
    _BUBBLE_NUM_RE = re.compile(r'^[\d,./:%+\-・\s]+$')
    _BUBBLE_ALPHANUM_NOISE_RE = re.compile(r'^[A-Za-z0-9.,\-+×★☆\s]{1,5}$')
    _bubble_texts = [r for r in ocr
                     if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                     and r["text"] not in _BUBBLE_EXCLUDE_EXACT
                     and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR)
                     and not _BUBBLE_NUM_RE.match(r["text"])
                     and not _BUBBLE_ALPHANUM_NOISE_RE.match(r["text"])
                     and len(r["text"]) > 2]
    if _bubble_texts:
        _bt = _bubble_texts[0]
        _btx, _bty = _bt["center"]
        logger.info(">>> ホーム画面 + 吹き出しセリフ '%s' → チュートリアル継続 (%d,%d)",
                    _bt["text"][:10], _btx, _bty)
        if hasattr(state, '_home_clear_count'):
            state._home_clear_count = 0
        tap_device(_btx, _bty, state, "BUBBLE_TAP")
        return "BUBBLE_TAP", 0.3
    # ── チュートリアル完了判定 ──
    # 条件: 暗転なし + 指なし + 金枠なし → 通常ホーム画面
    # 暗転中は指/金枠検出が失敗しているだけでチュートリアル中
    if not hasattr(state, '_home_clear_count'):
        state._home_clear_count = 0
        state._home_clear_last_phash = ""
    if _home_dimmed:
        # 暗転中 = チュートリアル中 → 完了判定リセット
        if state._home_clear_count > 0:
            logger.info(">>> ホーム画面 暗転あり → チュートリアル中 (HOME_CLEAR_COUNT %d→0)",
                        state._home_clear_count)
            state._home_clear_count = 0
        else:
            logger.info(">>> ホーム画面 暗転あり → チュートリアル中 (指/金枠未検出だが暗転)")
        return "HOME_CLEAR_CHECK", 0.5
    # 暗転なし + 指なし + 金枠なし → 通常ホームの可能性
    # 同一フレーム (phash同一) での重複カウントを防止
    _cur_phash = getattr(state, 'last_phash', "")
    if _cur_phash and _cur_phash == state._home_clear_last_phash:
        # 同一フレームでも暗転オーバーレイがあればチュートリアル中 → リセット
        if _home_dimmed and state._home_clear_count > 0:
            logger.info(">>> ホーム画面 同一フレームだが暗転あり → チュートリアル中 (HOME_CLEAR_COUNT %d→0)",
                        state._home_clear_count)
            state._home_clear_count = 0
            return "HOME_CLEAR_CHECK", 0.5
        logger.info(">>> ホーム画面 指/金枠/暗転なし (同一フレーム, %d/3) → スキップ",
                    state._home_clear_count)
        return "HOME_CLEAR_CHECK", 1.0
    state._home_clear_last_phash = _cur_phash
    state._home_clear_count += 1
    if state._home_clear_count < 3:
        logger.info(">>> ホーム画面 指/金枠/暗転なし (%d/3) → 確認待ち", state._home_clear_count)
        return "HOME_CLEAR_CHECK", 0.5
    logger.info(">>> ホーム画面 指/金枠/暗転なし 3フレーム連続 → チュートリアル完了!")
    # nav カウンタをリセット (チュートリアル指標消失)
    state.home_nav_count = 0
    state.blob_same_count = 0
    if state.grind_mode:
        # ── 周回モード: ホーム到達 → クエストへ自動ナビゲート ──
        state.grind_cycles_completed += 1
        logger.info("=" * 50)
        logger.info("  [GRIND] 周回 #%d 完了! → クエストへ自動ナビゲート",
                    state.grind_cycles_completed)
        logger.info("=" * 50)
        # 周回上限チェック
        if 0 < state.grind_max_cycles <= state.grind_cycles_completed:
            logger.info("[GRIND] 目標周回数 %d に到達 → 終了",
                        state.grind_max_cycles)
            return "GRIND_COMPLETE", 0
        # バトル関連カウンタをリセット
        state.battle_wait_count = 0
        state.auto_activated = False
        state.result_rapid_count = 0
        state.result_total_taps = 0
        state.result_subtype = ""
        state.home_nav_count = 0
        state.home_tutorial_tap_count = 0
        state.char_just_selected = False
        state.character_selected = False
        # クエストボタンをタップ
        quest_btn = has_text(ocr, "クエスト", min_conf=0.3)
        if quest_btn:
            cx, cy = quest_btn["center"]
            logger.info(">>> [GRIND] クエストボタン (%d,%d) タップ", cx, cy)
            tap_device(cx, cy, state, "GRIND_QUEST_NAV")
        else:
            _qf_x, _qf_y = roi_to_device(int(W * 0.88), int(H * 0.96), state.game_roi)
            logger.info(">>> [GRIND] クエスト固定位置 (%d,%d) タップ", _qf_x, _qf_y)
            tap_device(_qf_x, _qf_y, state, "GRIND_QUEST_NAV_FIXED")
        return "GRIND_QUEST_NAV", 3.0
    logger.info(">>> ホーム画面検出! (%d個) 指/金枠なし → チュートリアル完了!", home_count)
    log_milestone(state, "HOME_REACHED")
    return "HOME_REACHED", 0
