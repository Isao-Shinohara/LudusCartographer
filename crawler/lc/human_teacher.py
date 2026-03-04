"""
human_teacher.py — 未知の画面で人間に操作を教えてもらうターミナル UI

入力コマンド一覧:
  540,1200          → tap (x=540, y=1200)
  tap 540,1200      → tap (同上)
  swipe 300,600,300,200        → swipe (duration=300ms デフォルト)
  swipe 300,600,300,200,500    → swipe (duration=500ms 指定)
  back              → OS 戻る
  wait 2.0          → 2秒待機
  skip              → この画面をスキップして通常 DFS へ
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

DIVIDER = "=" * 60


class HumanTeacher:
    """未知の画面でターミナルを通じてユーザーに操作を教えてもらうクラス。"""

    def __init__(self, auto_open_screenshot: bool = True):
        self.auto_open_screenshot = auto_open_screenshot  # macOS: open コマンドで Preview 起動

    def ask_for_action(
        self,
        screenshot_path: Path,
        title: str,
        ocr_results: list[dict] = (),
    ) -> list[dict]:
        """
        ターミナルに画面情報を表示してユーザーの操作入力を求める。

        Returns:
            list[dict] — 実行するアクションのリスト
            []         — skip が選ばれた場合 (通常 DFS へフォールバック)
        """
        self._print_prompt(screenshot_path, title, ocr_results)
        if self.auto_open_screenshot and screenshot_path.exists():
            try:
                subprocess.run(["open", str(screenshot_path)], check=False, timeout=3)
            except Exception:
                pass

        while True:
            try:
                line = input("操作を入力 > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n[Teacher Mode] 入力をキャンセルしました")
                return []

            if not line:
                continue
            actions = _parse_input(line)
            if actions is None:
                print(f"  ⚠️  認識できませんでした: {line!r}")
                print("  ヒント: '540,1200' / 'back' / 'skip' / 'swipe 300,600,300,200'")
                continue
            return actions

    @staticmethod
    def _print_prompt(screenshot_path: Path, title: str, ocr_results) -> None:
        ocr_lines = []
        for r in list(ocr_results)[:12]:
            cx, cy = r.get("center", [0, 0])
            ocr_lines.append(f"    [{cx:.0f},{cy:.0f}] {r['text']!r}")
        ocr_str = "\n".join(ocr_lines) or "    (OCR なし)"

        print(f"\n{DIVIDER}")
        print("🆕 [Teacher Mode] 未知の画面です。操作を教えてください")
        print(f"   画面タイトル : {title}")
        print(f"   スクリーンショット : {screenshot_path}")
        print(f"   OCR 検出テキスト:\n{ocr_str}")
        print(f"{DIVIDER}")
        print("  入力コマンド:")
        print("    x,y              タップ  例: 540,1200")
        print("    swipe x1,y1,x2,y2[,ms]  スワイプ  例: swipe 300,600,300,200,300")
        print("    back             OS 戻る")
        print("    wait 秒数        待機  例: wait 2.0")
        print("    skip             スキップして通常探索へ")
        print(f"{DIVIDER}")


def _parse_input(text: str) -> Optional[list[dict]]:
    """
    ユーザー入力文字列をアクションリストに変換する。

    Returns:
        list[dict] — パース成功したアクションリスト（skip の場合は []）
        None       — 認識できない入力（再入力を促す）
    """
    t = text.strip().lower()

    # skip
    if t in ("skip", "s"):
        return []

    # back
    if t in ("back", "b"):
        return [{"type": "back"}]

    # wait N
    m = re.match(r"^wait\s+([\d.]+)$", t)
    if m:
        return [{"type": "wait", "duration": float(m.group(1))}]

    # swipe x1,y1,x2,y2[,duration]
    m = re.match(
        r"^(?:swipe\s+)?(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)[,\s]+(-?\d+)(?:[,\s]+([\d]+))?$",
        t,
    )
    if m:
        x1, y1, x2, y2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        duration = int(m.group(5)) if m.group(5) else 300
        return [{"type": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "duration": duration}]

    # tap x,y  (または単に "x,y")
    m = re.match(r"^(?:tap\s+)?(-?\d+)[,\s]+(-?\d+)$", t)
    if m:
        return [{"type": "tap", "x": int(m.group(1)), "y": int(m.group(2))}]

    return None  # 認識不可 → 再入力
