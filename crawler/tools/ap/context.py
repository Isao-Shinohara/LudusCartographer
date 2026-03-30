"""
ap/context.py — DetectContext: detect_and_act の共有コンテキスト
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.ap.constants import ANALYSIS_W, ANALYSIS_H
from tools.ap.image_proc import AdvSceneResult


@dataclass
class DetectContext:
    """detect_and_act の全ハンドラが共有する事前計算済みデータ。

    detect_and_act のエントリで一度だけ構築し、各ハンドラ関数に渡す。
    """
    # ─── OCR データ ───
    ocr: list
    texts: list[str]
    joined: str

    # ─── 解析空間サイズ ───
    W: int = ANALYSIS_W
    H: int = ANALYSIS_H

    # ─── 解析画像パス ───
    analysis_path: Optional[Path] = None

    # ─── 事前計算フラグ ───
    adv_result: AdvSceneResult = field(default_factory=AdvSceneResult)
    confirm_pos: Optional[dict] = None       # has_any(ocr, _CONFIRM_POS_KWS)
    confirm_neg: Optional[dict] = None       # has_any(ocr, _CONFIRM_NEG_KWS)
    is_notice: bool = False                   # お知らせポップアップ検出
    pre_dialog_finger: bool = False           # 指ブロブによるダイアログガード
    white_hand_pos: Optional[tuple] = None    # 白ハンドポインタ座標
    is_mini_conv: bool = False                # ミニ会話検出
    mini_conv_pos: Optional[tuple] = None    # ミニ会話座標 (cx, cy, side)
    is_result_screen_flag: bool = False       # Result画面テキスト検出
    is_adv_or_movie: bool = False             # ADV/MOVIEシーン判定
    in_battle_ctx: bool = False               # バトルコンテキスト (state.current_scene == "BATTLE")
    has_dialog_corners: Optional[bool] = None  # detect_dialog_corners 結果 (Phase 3 で計算、Phase 4 で再利用)
