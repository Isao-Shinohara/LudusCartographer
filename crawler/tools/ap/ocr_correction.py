"""
OCR テキスト自動修正パイプライン。

3段階で修正:
  1. 正規表現: 定型的な OCR 誤認パターンを高速置換
  2. 辞書マッチ: キャラ名・ゲーム用語のマスターリストと編集距離で修正
  3. Gemini Flash: 文脈依存の修正 (API キー未設定ならスキップ)
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 段階1: 正規表現置換 ─────────────────────────────────

_REGEX_REPLACEMENTS = [
    # 記号の誤認
    (r'＋(\d)', r'★\1'),       # ＋0 → ★0 (レアリティ)
    (r'＋\s', r'★ '),
    (r'(?<!\w)臼(?!\w)', ''),   # ゴミ文字「臼」
    (r'(?<!\w)米(?!\w)', ''),   # ゴミ文字「米」
    (r'(?<!\w)図(?!\w)', ''),   # ゴミ文字「図」
    (r'^2ヨ\s*/?', ''),         # ゴミ文字「2ヨ」
    (r'(?<!\w)日(?!\w)', ''),   # ゴミ文字「日」(単独)
    # MAGIA EXEDRA の誤認パターン
    (r'NIAGIA', 'MAGIA'),
    (r'IVIAGIA', 'MAGIA'),
    (r'IIAGIA', 'MAGIA'),
    (r'IMAGIA', 'MAGIA'),
    (r'NAGIA', 'MAGIA'),
    # ゲーム用語の誤認
    (r'TAREAKER', 'ATTACKER'),
    (r'FTACKER', 'ATTACKER'),
    (r'TACKER', 'ATTACKER'),
    (r'UFFEI', 'BUFFER'),
    (r'IDEFENDERN', 'DEFENDER'),
    (r'ADEBOAI', 'DEBONAIR'),
    (r'CRUFFER', 'BUFFER'),
    # 日本語の誤認
    (r'まどか、マギカ', 'まどか★マギカ'),
    (r'まどか、ギカ', 'まどか★マギカ'),
    (r'まどかマギカ', 'まどか★マギカ'),
    (r'動画配乍設定', '動画配信設定'),
    (r'動画配合設定', '動画配信設定'),
    # 連続スペースの正規化
    (r'\s{2,}', ' '),
]

_COMPILED_REGEX = [(re.compile(pat), repl) for pat, repl in _REGEX_REPLACEMENTS]


def _stage1_regex(text: str) -> str:
    """段階1: 正規表現で定型的な OCR 誤認を修正。"""
    for pat, repl in _COMPILED_REGEX:
        text = pat.sub(repl, text)
    return text.strip()


# ─── 段階2: 辞書マッチ (編集距離) ────────────────────────

# まどか★マギカ Magia Exedra のキャラ名・用語マスターリスト
_GAME_DICTIONARY = [
    # キャラ名
    "鹿目まどか", "暁美ほむら", "美樹さやか", "巴マミ", "佐倉杏子",
    "由比鶴乃", "七海やちよ", "環いろは", "秋野かえで", "深月フェリシア",
    "二葉さな", "水波レナ", "御園かりん", "梓みふゆ", "十咎ももこ",
    "志筑仁美", "キュゥべえ",
    # UI 用語
    "パーティ", "ホーム", "ショップ", "ガチャ", "クエスト",
    "バトル", "スキル", "通常攻撃", "マギア", "ドッペル",
    "キオク", "額縁", "魔法少女", "魔女", "使い魔",
    "プレイヤー", "ダウンロード", "アセット",
    "薔薇園の魔女", "記憶の光", "光の間",
    # ゲームシステム
    "ブレイク", "ブレイクゲージ", "ブレイクボーナス",
    "TOTAL DAMAGE", "ATTACKER", "BUFFER", "DEFENDER",
    "BREAK", "Result", "SKIP", "AUTO",
    "推奨", "報酬", "初回報酬", "限界突破",
]


def _stage2_dictionary(text: str, dictionary: list[str] = _GAME_DICTIONARY,
                       max_distance: int = 2) -> str:
    """段階2: 辞書マッチで固有名詞の誤認を修正。"""
    words = text.split()
    corrected = []
    for word in words:
        if len(word) < 2:
            corrected.append(word)
            continue
        # 辞書内で近い語を検索
        matches = difflib.get_close_matches(word, dictionary, n=1, cutoff=0.6)
        if matches and matches[0] != word:
            # 編集距離を確認
            ratio = difflib.SequenceMatcher(None, word, matches[0]).ratio()
            if ratio >= 0.6 and len(word) >= 3:
                corrected.append(matches[0])
                continue
        corrected.append(word)
    return " ".join(corrected)


# ─── 段階3: Gemini Flash ─────────────────────────────────

_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
_GEMINI_MODEL = "gemini-2.0-flash-lite"
_GEMINI_RATE_LIMIT = 4.0  # 1リクエストあたりの最小間隔 (秒) — 15rpm 制限対策


def _stage3_gemini_batch(texts: list[dict[int, str]]) -> dict[int, str]:
    """段階3: Gemini Flash で OCR テキストをバッチ修正。

    Args:
        texts: [{id: ocr_text}, ...] の辞書リスト

    Returns:
        {id: corrected_text} の辞書。修正不要なら元テキストを返す。
    """
    api_key = _GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}

    try:
        import urllib.request
        import urllib.error

        # バッチプロンプト生成
        items = []
        for item in texts:
            for sid, text in item.items():
                items.append({"id": sid, "text": text})

        prompt = f"""以下はゲーム「まどか★マギカ Magia Exedra」の画面から OCR で抽出したテキストです。
OCR の誤認を修正してください。

ルール:
- 明らかな誤字のみ修正（意味が通る場合はそのまま）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、志筑仁美、キュゥべえ
- ゴミ文字（「2ヨ」「臼」「米」「図」等の単独文字）は削除
- 修正不要ならそのまま返す

入力 (JSON 配列):
{json.dumps(items, ensure_ascii=False)}

出力 (JSON 配列、同じ id で修正後テキストを返す):
"""

        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }).encode()

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent?key={api_key}"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})

        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())

        # レスポンス解析
        text_resp = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        corrected_items = json.loads(text_resp)

        result_map = {}
        if isinstance(corrected_items, list):
            for item in corrected_items:
                if isinstance(item, dict) and "id" in item and "text" in item:
                    result_map[item["id"]] = item["text"]

        return result_map

    except Exception as e:
        logger.warning("[OCR_CORRECT] Gemini API エラー: %s", e)
        return {}


# ─── パイプライン実行 ─────────────────────────────────────

def correct_ocr_text(text: str) -> str:
    """段階1+2 でテキストを修正 (ローカルのみ、高速)。"""
    text = _stage1_regex(text)
    text = _stage2_dictionary(text)
    return text


def correct_batch_gemini(texts: list[dict[int, str]]) -> dict[int, str]:
    """段階3: Gemini でバッチ修正。API キー未設定なら空辞書を返す。"""
    api_key = _GEMINI_API_KEY or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.debug("[OCR_CORRECT] GEMINI_API_KEY 未設定 → スキップ")
        return {}
    # レート制限
    time.sleep(_GEMINI_RATE_LIMIT)
    return _stage3_gemini_batch(texts)
