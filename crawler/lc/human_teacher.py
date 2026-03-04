"""
human_teacher.py — 未知の画面で人間に操作を教えてもらうターミナル UI

入力コマンド一覧:
  540,1200            → tap (x=540, y=1200)
  tap 540,1200        → tap (同上)
  540,1200,3000       → tap (タップ後 3000ms 待機)
  tap 540,1200,3000   → tap (同上)
  swipe 300,600,300,200        → swipe (duration=300ms デフォルト)
  swipe 300,600,300,200,500    → swipe (duration=500ms 指定)
  back              → OS 戻る
  wait 2.0          → 2秒待機
  skip              → この画面をスキップして通常 DFS へ
  1〜5              → 直近タップ履歴を番号で再選択
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
        self._history: list[dict] = []  # 直近5件のタップ履歴 (MRU順)

    def ask_for_action(
        self,
        screenshot_path: Path,
        title: str,
        ocr_results: list[dict] = (),
        screen_size: Optional[tuple[int, int]] = None,  # (width, height) pixels
    ) -> list[dict]:
        """
        ターミナルに画面情報を表示してユーザーの操作入力を求める。

        Returns:
            list[dict] — 実行するアクションのリスト
            []         — skip が選ばれた場合 (通常 DFS へフォールバック)
        """
        self._print_prompt(screenshot_path, title, ocr_results, screen_size, self._history)
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

            # 番号入力 → 履歴再選択
            if line.isdigit():
                idx = int(line) - 1
                if self._history and 0 <= idx < len(self._history):
                    selected = self._history[idx]
                    print(f"  ✅ 履歴 #{idx+1} を選択: ({selected['x']},{selected['y']})")
                    return [dict(selected)]
                print(f"  ⚠️  履歴番号 {line!r} は範囲外です (1〜{len(self._history)})")
                continue

            actions = _parse_input(line)
            if actions is None:
                print(f"  ⚠️  認識できませんでした: {line!r}")
                print("  ヒント: '540,1200' / 'back' / 'skip' / 'swipe 300,600,300,200'")
                continue

            # タップ履歴を更新 (MRU: x,y が同一なら先頭に移動、最大5件)
            self._update_history(actions)
            return actions

    def _update_history(self, actions: list[dict]) -> None:
        """タップアクションを履歴先頭に追加する (MRU, 最大5件)。"""
        for action in reversed(actions):
            if action.get("type") != "tap":
                continue
            entry = {k: v for k, v in action.items() if k in ("type", "x", "y", "wait_ms")}
            entry = {k: v for k, v in entry.items() if v is not None}
            # 同一 x,y が既存なら除去してから先頭挿入
            self._history = [h for h in self._history
                             if not (h.get("x") == entry.get("x") and h.get("y") == entry.get("y"))]
            self._history.insert(0, entry)
        self._history = self._history[:5]

    @staticmethod
    def _print_prompt(
        screenshot_path: Path,
        title: str,
        ocr_results,
        screen_size: Optional[tuple[int, int]] = None,
        history: Optional[list] = None,
    ) -> None:
        ocr_lines = []
        for r in list(ocr_results)[:12]:
            cx, cy = r.get("center", [0, 0])
            ocr_lines.append(f"    [{cx:.0f},{cy:.0f}] {r['text']!r}")
        ocr_str = "\n".join(ocr_lines) or "    (OCR なし)"

        print(f"\n{DIVIDER}")
        print("🆕 [Teacher Mode] 未知の画面です。操作を教えてください")
        print(f"   画面タイトル : {title}")
        print(f"   スクリーンショット : {screenshot_path}")
        if screen_size is not None:
            print(f"   画面サイズ       : {screen_size[0]}×{screen_size[1]} px")
        print(f"   OCR 検出テキスト:\n{ocr_str}")
        if history:
            print("   直近の入力履歴:")
            for i, h in enumerate(history, 1):
                wait_part = f",{h['wait_ms']}" if "wait_ms" in h else ""
                print(f"     [{i}] tap({h['x']},{h['y']}{wait_part})")
            print("   番号入力で再選択できます")
        print(f"{DIVIDER}")
        print("  入力コマンド:")
        print("    x,y[,ms]         タップ（待機時間オプション）  例: 540,1200  または 540,1200,3000")
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

    # tap x,y[,wait_ms]  (または単に "x,y[,wait_ms]")
    m = re.match(r"^(?:tap\s+)?(-?\d+)[,\s]+(-?\d+)(?:[,\s]+(\d+))?$", t)
    if m:
        action: dict = {"type": "tap", "x": int(m.group(1)), "y": int(m.group(2))}
        if m.group(3):
            action["wait_ms"] = int(m.group(3))
        return [action]

    return None  # 認識不可 → 再入力
