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
    find_gold_button,
    detect_tutorial_overlay,
    find_gold_frame_near,
    roi_to_device,
)
from tools.ap.state import PilotState

logger = logging.getLogger("auto_pilot")


def _handle_grind_nav(ctx: DetectContext, state: PilotState) -> tuple[str, float]:
    """周回モード: ホーム到達 → クエストへ自動ナビゲート。"""
    state.grind_cycles_completed += 1
    logger.info("=" * 50)
    logger.info("  [GRIND] 周回 #%d 完了! → クエストへ自動ナビゲート",
                state.grind_cycles_completed)
    logger.info("=" * 50)
    if 0 < state.grind_max_cycles <= state.grind_cycles_completed:
        logger.info("[GRIND] 目標周回数 %d に到達 → 終了", state.grind_max_cycles)
        return "GOAL_GRIND_COMPLETE", 0
    state.battle_wait_count = 0
    state.auto_activated = False
    state.result_rapid_count = 0
    state.result_total_taps = 0
    state.result_subtype = ""
    state.home_nav_count = 0
    state.home_tutorial_tap_count = 0
    state.char_just_selected = False
    state.character_selected = False
    quest_btn = has_text(ctx.ocr, "クエスト", min_conf=0.3)
    if quest_btn:
        cx, cy = quest_btn["center"]
        logger.info(">>> [GRIND] クエストボタン (%d,%d) タップ", cx, cy)
        tap_device(cx, cy, state, "GRIND_QUEST_NAV")
    else:
        _qf_x, _qf_y = roi_to_device(int(ctx.W * 0.88), int(ctx.H * 0.96), state.game_roi)
        logger.info(">>> [GRIND] クエスト固定位置 (%d,%d) タップ", _qf_x, _qf_y)
        tap_device(_qf_x, _qf_y, state, "GRIND_QUEST_NAV_FIXED")
    return "GRIND_QUEST_NAV", 3.0


def handle_home(ctx: DetectContext, state: PilotState) -> Optional[tuple[str, float]]:
    """ホーム画面検出 + チュートリアル完了判定。"""
    ocr = ctx.ocr
    texts = ctx.texts
    W = ctx.W
    H = ctx.H
    analysis_path = ctx.analysis_path

    # ─── ホーム画面検出 ───
    # ホームナビバーのボタン名のみでカウント (編成/メニュー等のサブ画面と区別)
    # フッター領域 (y > 80%) の OCR テキストのみ対象
    # OCR が文字を途中で切る場合がある ("クエスト"→"クエス", "パーティ"→"パーテ")
    # → 短いプレフィックスで双方向部分一致: keyword in text OR text in keyword
    from tools.ap.image_proc import count_home_nav_templates
    home_count = count_home_nav_templates(analysis_path) if analysis_path else 0
    if home_count < 3:
        return None

    state.home_reached = True

    # 前段ハンドラ (tutorial.py) で指テンプレが処理済みの場合、
    # チュートリアル証拠なしカウンタをリセット (誤完了判定防止)
    if ctx.pre_dialog_finger:
        state._home_no_evidence_count = 0

    # ── チュートリアル判定: 全方向の指テンプレート + 金枠 ──
    # チュートリアル中: 指アイコン(上/下/左/右) が常に1つ + 金枠が1つ表示される
    # チュートリアル完了後: 指アイコンなし + 金枠なし
    _hand_match = None
    _has_hand = False
    _ft_name_found = ""
    if analysis_path:
        _ft_rot = ASSET_MANAGER.match_finger_rotated(analysis_path)
        if _ft_rot:
            _hand_match = _ft_rot  # (cx, cy, score, direction)
            _has_hand = True
            _ft_name_found = f"finger_{_ft_rot[3]}"
            logger.info(">>> ホーム: %s(%.2f) (%d,%d) 検出 → チュートリアル中",
                        _ft_name_found, _ft_rot[2], _ft_rot[0], _ft_rot[1])

    # 金枠検出 — HSV で検出
    _home_gold = find_gold_button(analysis_path, right_half_only=False) if analysis_path else None
    _home_gold_tmpl = _home_gold  # チュートリアル証拠として使用

    if _has_hand:
        _hx, _hy = _hand_match[0], _hand_match[1]
        _home_blobs = [(_hx, _hy, 10000.0, _hx - 20, _hy - 20, 40, 40)]
    else:
        _home_blobs = []

    # チュートリアル証拠: 指テンプレ or HSV金枠
    # 追加ガード: チュートリアル証拠があっても暗転オーバーレイがなければ偽陽性
    # (チュートリアル中は対象以外が暗くなる。完了後は暗転しない)
    _has_overlay = detect_tutorial_overlay(analysis_path) if analysis_path else False
    if not _has_overlay:
        if _has_hand:
            logger.info(">>> ホーム: 指テンプレ検出だが暗転なし → 偽陽性として無視")
            _has_hand = False
            _home_blobs = []
        if _home_gold_tmpl is not None:
            logger.info(">>> ホーム: 金枠検出だが暗転なし → 偽陽性として無視")
            _home_gold_tmpl = None
    _has_tutorial_evidence = _has_hand or (_home_gold_tmpl is not None)

    if not _has_tutorial_evidence:
        # 指アイコンなし + 金枠なし = チュートリアル完了候補
        # 1フレームの遷移瞬間で誤判定しないよう、3回連続で確認
        _no_evidence_count = getattr(state, "_home_no_evidence_count", 0) + 1
        state._home_no_evidence_count = _no_evidence_count
        if not state.tutorial_cleared and _no_evidence_count < 3:
            logger.info(">>> ホーム画面 チュートリアル証拠なし (%d/3) → 次フレームで再確認",
                        _no_evidence_count)
            return "HOME_TUTORIAL_RECHECK", 0.5
        if not state.tutorial_cleared:
            logger.info(">>> ホーム画面 指テンプレなし+金枠なし (3回連続) → チュートリアル完了")
            state.tutorial_cleared = True
            log_milestone(state, "HOME_REACHED")
        logger.info(">>> ホーム画面検出 (%d個) — チュートリアル完了済み", home_count)
        return "GOAL_HOME_REACHED", 0

    # ── チュートリアル中: 指/金枠を検出してタップ ──
    # チュートリアル証拠ありなので連続確認カウンタをリセット
    state._home_no_evidence_count = 0
    logger.info(">>> ホーム画面 チュートリアル中 (指=%s 金枠=%s)",
                _has_hand, _home_gold is not None)
    if _home_blobs or _home_gold:
        _tap_target = None
        if _home_blobs:
            _chosen_blob = max(_home_blobs, key=lambda b: b[2])  # area最大
            _bx, _by = _chosen_blob[0], _chosen_blob[1]
            # 指テンプレート名から方向を取得
            _finger_dir = ""
            for _dn, _dd in [("finger_down", "down"), ("finger_up", "up"),
                             ("finger_left", "left"), ("finger_right", "right"),
                             ("hand_pointer", "up")]:
                if _dn in _ft_name_found:
                    _finger_dir = _dd
                    break
            # ── 優先1: OCR テキスト中心 (指の近傍) ──
            # 指が下向きの場合、ナビバーボタンを指している可能性があるため
            # ── 優先1: 金枠検出 (方向付きテンプレートマッチ) ──
            # OCR でテキストが取れなくても金枠中央をタップすれば正しく動作する
            _gf = find_gold_frame_near(
                analysis_path, _bx, _by, search_radius=250,
                direction=_finger_dir) if analysis_path else None
            if _gf and _gf[1] > H * 0.85:
                logger.info(">>> ホーム: 金枠(%d,%d) がフッターナビ領域 → 除外", _gf[0], _gf[1])
                _gf = None
            if _gf:
                _tap_target = (_gf[0], _gf[1])
                logger.info(">>> ホームチュートリアル: 指(%d,%d,dir=%s)→金枠(%d,%d) [%d回目]",
                            _bx, _by, _finger_dir, _gf[0], _gf[1], state.home_tutorial_tap_count + 1)
            else:
                # ── 優先2: OCR テキスト (金枠が検出できない場合のフォールバック) ──
                _ocr_target = None
                _ocr_target_label = ""
                _ocr_best_dist = 999
                _HOME_NAV_KWS = {"光の間", "ショップ", "ガチャ", "ガシャ", "マップ", "レイヤ",
                                 "マッチ", "ユニオン", "クエスト", "クエス", "パーティ", "育成",
                                 "ころの器", "こころの器"}
                _skip_nav_filter = (_finger_dir == "down")
                for _oe in ocr:
                    _ot = _oe.get("text", "")
                    _oc = _oe.get("center", (0, 0))
                    if len(_ot) < 2:
                        continue
                    if not _skip_nav_filter and any(kw in _ot for kw in _HOME_NAV_KWS):
                        continue
                    _odx = abs(_oc[0] - _bx)
                    _ody = abs(_oc[1] - _by)
                    _dir_ok = True
                    if _finger_dir == "down" and _oc[1] < _by:
                        _dir_ok = False
                    elif _finger_dir == "up" and _oc[1] > _by:
                        _dir_ok = False
                    _dist = (_odx ** 2 + _ody ** 2) ** 0.5
                    if _dir_ok and _odx < 250 and _ody < 250 and _dist < _ocr_best_dist:
                        _ocr_best_dist = _dist
                        _ocr_target = (_oc[0], _oc[1])
                        _ocr_target_label = _ot
                if _ocr_target:
                    logger.info(">>> ホームチュートリアル: 指(%d,%d,dir=%s)→OCRテキスト '%s'(%d,%d) [%d回目]",
                                _bx, _by, _finger_dir, _ocr_target_label, _ocr_target[0], _ocr_target[1],
                                state.home_tutorial_tap_count + 1)
                if _ocr_target:
                    _tap_target = _ocr_target
                elif _home_gold:
                    _tap_target = _home_gold
                    logger.info(">>> ホームチュートリアル: 指(%d,%d)→GoldBtn(%d,%d) [近傍外] [%d回目]",
                                _bx, _by, *_home_gold, state.home_tutorial_tap_count + 1)
                elif False:
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
            if False:
                logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転あり]", *_home_gold)
            else:
                logger.info(">>> ホームチュートリアル: 金ボタン(%d,%d) [暗転なし・指なし]", *_home_gold)
        if _tap_target:
            state.home_tutorial_tap_count += 1
            tap_device(_tap_target[0], _tap_target[1], state, "HOME_TUTORIAL_TAP")
            return "HOME_TUTORIAL_TAP", 0.5
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
    _BUBBLE_EXCLUDE_SUBSTR = ("Max", "Lv", "Lx", "Rank", "LV", "MadoDora", "M.8", "M8X",
                              "AUTO", "UTO", "UT0", "AUT")
    _BUBBLE_NUM_RE = re.compile(r'^[\d,./:%+\-・\s]+$')
    _BUBBLE_ALPHANUM_NOISE_RE = re.compile(r'^[A-Za-z0-9.,\-+×★☆\s]{1,5}$')
    # AUTO ボタン位置を検出 → その近傍 50px 以内のテキストも除外
    _auto_pos_h = None
    if analysis_path:
        _auto_mh = ASSET_MANAGER.match_single("icon_auto", analysis_path)
        if _auto_mh and _auto_mh[2] >= 0.60:
            _auto_pos_h = (_auto_mh[0], _auto_mh[1])
    _bubble_texts = [r for r in ocr
                     if r["center"][0] > W * 0.55 and r["center"][1] < H * 0.35
                     and r["text"] not in _BUBBLE_EXCLUDE_EXACT
                     and not any(s in r["text"] for s in _BUBBLE_EXCLUDE_SUBSTR)
                     and not _BUBBLE_NUM_RE.match(r["text"])
                     and not _BUBBLE_ALPHANUM_NOISE_RE.match(r["text"])
                     and len(r["text"]) > 2
                     and not (_auto_pos_h and abs(r["center"][0] - _auto_pos_h[0]) < 50
                              and abs(r["center"][1] - _auto_pos_h[1]) < 50)]
    if _bubble_texts:
        _bt = _bubble_texts[0]
        _btx, _bty = _bt["center"]
        logger.info(">>> ホーム画面 + 吹き出しセリフ '%s' → チュートリアル継続 (%d,%d)",
                    _bt["text"][:10], _btx, _bty)
        tap_device(_btx, _bty, state, "BUBBLE_TAP")
        return "BUBBLE_TAP", 0.3
    # ── 暗転あるが指/金枠/吹き出しなし → 待機 ──
    logger.info(">>> ホーム画面 暗転あり + 指/金枠/吹き出しなし → 待機")
    return "HOME_CLEAR_CHECK", 0.5
