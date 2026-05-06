"""縮退 phash 同士の誤統合防止テスト。

phash 同士が「Hamming 距離 0」でも、両方が縮退 phash (set bit < 8 or > 56) の
場合は内容と無関係に近くなるだけ。クラスタ判定ロジックでは別物として扱う。

修正対象:
  1. lc/cluster_decision.py:classify_empty_text_pair — phash_near 判定除外
  2. tools/ap/background_worker.py:merge_to_prev_empty 判定除外

これらが改修されないと、MOVIE 暗転 (phash=8000...) と マギカストーン獲得
(phash=8000...) のような視覚的に全く違う画像が同一クラスタに合流する。
"""
from __future__ import annotations

import pytest


class TestIsDegeneratePhash:
    """縮退判定関数の単体テスト。"""

    def test_all_zeros_is_degenerate(self):
        from lc.utils import is_degenerate_phash
        assert is_degenerate_phash("0000000000000000") is True  # bits=0

    def test_single_bit_is_degenerate(self):
        from lc.utils import is_degenerate_phash
        assert is_degenerate_phash("8000000000000000") is True  # bits=1

    def test_all_ones_is_degenerate(self):
        from lc.utils import is_degenerate_phash
        assert is_degenerate_phash("ffffffffffffffff") is True  # bits=64

    def test_threshold_8_is_not_degenerate(self):
        """set bit = 8 は閾値ぎりぎりだが縮退ではない (< 8 のみ)。"""
        from lc.utils import is_degenerate_phash
        # 8 ビット立つ phash
        ph = "ff00000000000000"  # bits=8
        assert is_degenerate_phash(ph) is False

    def test_threshold_56_is_not_degenerate(self):
        """set bit = 56 は閾値ぎりぎりだが縮退ではない (> 56 のみ)。"""
        from lc.utils import is_degenerate_phash
        ph = "00ffffffffffffff"  # bits=56
        assert is_degenerate_phash(ph) is False

    def test_normal_phash_is_not_degenerate(self):
        from lc.utils import is_degenerate_phash
        # 通常のリッチな phash (12 bits)
        assert is_degenerate_phash("8088061201400881") is False

    def test_invalid_phash_treated_as_degenerate(self):
        """不正な hex 文字列は安全側 (信用しない) として縮退扱い。"""
        from lc.utils import is_degenerate_phash
        assert is_degenerate_phash("not_a_hex") is True

    def test_empty_or_none_treated_as_degenerate(self):
        from lc.utils import is_degenerate_phash
        assert is_degenerate_phash("") is True
        assert is_degenerate_phash(None) is True


class TestClusterDecisionRejectsDegeneratePair:
    """cluster_decision.classify_empty_text_pair の縮退 phash 除外。"""

    def test_degenerate_pair_distance_0_not_phash_near(self, tmp_path):
        """縮退 phash 同士で d=0 でも phash_near 判定を返さない。

        例: MOVIE 暗転 (phash=8000...) + マギカストーン獲得 (phash=8000...)
        が誤って phash_near として同クラスタにされる問題への対策。
        """
        from lc.cluster_decision import classify_empty_text_pair_with_metrics

        # ダミー画像パス (実画像不要、prev_phash/curr_phash で判定するため)
        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        for p in (p1, p2):
            import cv2
            import numpy as np
            cv2.imwrite(str(p), np.zeros((100, 100, 3), dtype=np.uint8))

        r = classify_empty_text_pair_with_metrics(
            prev_path=p1, curr_path=p2,
            phash_distance=0,
            dhash_distance=0,
            near_threshold=8, far_threshold=40, fallback_threshold=40,
            prev_phash="8000000000000000",  # 縮退
            curr_phash="8000000000000000",  # 縮退
        )
        assert r.is_same is False, "縮退 phash 同士は別物扱いとなるべき"
        assert r.method == "phash_degenerate", \
            f"phash_degenerate と判定されるべき: 実際 {r.method}"

    def test_normal_pair_distance_0_is_phash_near(self, tmp_path):
        """通常 phash 同士で d=0 なら phash_near (同) 判定。"""
        from lc.cluster_decision import classify_empty_text_pair_with_metrics

        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        for p in (p1, p2):
            import cv2
            import numpy as np
            cv2.imwrite(str(p), np.zeros((100, 100, 3), dtype=np.uint8))

        r = classify_empty_text_pair_with_metrics(
            prev_path=p1, curr_path=p2,
            phash_distance=0,
            dhash_distance=0,
            near_threshold=8, far_threshold=40, fallback_threshold=40,
            prev_phash="8088061201400881",  # 通常 (12 bits)
            curr_phash="8088061201400881",  # 通常
        )
        assert r.is_same is True
        assert r.method == "phash_near"

    def test_one_degenerate_one_normal_treated_as_degenerate(self, tmp_path):
        """片方が縮退でも判定対象外。

        通常画面が偶然 phash 距離が小さくても、相手が縮退なら別物確定。
        """
        from lc.cluster_decision import classify_empty_text_pair_with_metrics

        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        for p in (p1, p2):
            import cv2
            import numpy as np
            cv2.imwrite(str(p), np.zeros((100, 100, 3), dtype=np.uint8))

        r = classify_empty_text_pair_with_metrics(
            prev_path=p1, curr_path=p2,
            phash_distance=3,
            dhash_distance=0,
            near_threshold=8, far_threshold=40, fallback_threshold=40,
            prev_phash="8000000000000000",  # 縮退
            curr_phash="8088061201400881",  # 通常
        )
        assert r.is_same is False
        assert r.method == "phash_degenerate"

    def test_no_phash_args_falls_back_to_old_behavior(self, tmp_path):
        """prev_phash/curr_phash 未指定時は従来通り (後方互換)。"""
        from lc.cluster_decision import classify_empty_text_pair_with_metrics

        p1 = tmp_path / "a.png"
        p2 = tmp_path / "b.png"
        for p in (p1, p2):
            import cv2
            import numpy as np
            cv2.imwrite(str(p), np.zeros((100, 100, 3), dtype=np.uint8))

        r = classify_empty_text_pair_with_metrics(
            prev_path=p1, curr_path=p2,
            phash_distance=0,
            dhash_distance=0,
            near_threshold=8, far_threshold=40, fallback_threshold=40,
            # prev_phash/curr_phash 未指定
        )
        # 従来通り phash_near 判定
        assert r.is_same is True
        assert r.method == "phash_near"
