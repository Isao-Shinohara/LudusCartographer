"""
OCR テキスト自動修正パイプライン。

3段階で修正:
  1. 正規表現 + 学習パターン: 定型的な OCR 誤認パターンを高速置換
  2. 辞書マッチ: キャラ名・ゲーム用語のマスターリストと編集距離で修正
  3. Gemini Flash: 文脈依存の修正 (API キー未設定ならスキップ)

学習パターン:
  - Gemini 修正結果やユーザー指摘から差分を自動抽出
  - 同じ修正が LEARN_THRESHOLD 回以上出現したら確定パターンに昇格
  - crawler/storage/ocr_learned_patterns.json に保存
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ─── 学習パターンファイル ─────────────────────────────────

_STORAGE_DIR = Path(__file__).parent.parent.parent / "storage"
_LEARNED_PATTERNS_PATH = _STORAGE_DIR / "ocr_learned_patterns.json"
_LEARN_THRESHOLD = 3  # 同じ修正が N 回出現したら確定パターンに昇格


def _load_learned_patterns() -> dict:
    """学習パターンを JSON から読み込む。"""
    if _LEARNED_PATTERNS_PATH.exists():
        try:
            with open(_LEARNED_PATTERNS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"confirmed": {}, "candidates": {}}


def _save_learned_patterns(data: dict) -> None:
    """学習パターンを JSON に保存。"""
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_LEARNED_PATTERNS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def learn_from_correction(original: str, corrected: str) -> None:
    """修正前後の差分からパターンを学習。

    Gemini 修正結果またはユーザー指摘から呼ばれる。
    同じ修正が LEARN_THRESHOLD 回以上出現したら確定パターンに昇格。
    """
    if not original or not corrected or original == corrected:
        return

    # 単語レベルで差分抽出
    orig_words = original.split()
    corr_words = corrected.split()
    sm = difflib.SequenceMatcher(None, orig_words, corr_words)

    data = _load_learned_patterns()
    updated = False

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace":
            old = " ".join(orig_words[i1:i2])
            new = " ".join(corr_words[j1:j2])
            if old and new and old != new and len(old) >= 2:
                key = f"{old} → {new}"
                if key in data["confirmed"]:
                    continue  # 既に確定済み
                count = data["candidates"].get(key, 0) + 1
                data["candidates"][key] = count
                if count >= _LEARN_THRESHOLD:
                    # 確定パターンに昇格
                    data["confirmed"][old] = new
                    del data["candidates"][key]
                    logger.info("[OCR_LEARN] パターン確定: '%s' → '%s' (%d回)", old, new, count)
                updated = True

    if updated:
        _save_learned_patterns(data)


def add_user_correction(wrong: str, correct: str) -> None:
    """ユーザーからの指摘で即座に確定パターンに追加。"""
    if not wrong or not correct or wrong == correct:
        return
    data = _load_learned_patterns()
    data["confirmed"][wrong] = correct
    # candidates からも削除
    key = f"{wrong} → {correct}"
    data["candidates"].pop(key, None)
    _save_learned_patterns(data)
    logger.info("[OCR_LEARN] ユーザー指摘でパターン追加: '%s' → '%s'", wrong, correct)


# ─── 段階1: 正規表現 + 学習パターン置換 ──────────────────

_BUILTIN_REPLACEMENTS = [
    # 記号の誤認
    (r'＋(\d)', r'★\1'),
    (r'＋\s', r'★ '),
    (r'(?<!\w)臼(?!\w)', ''),
    (r'(?<!\w)米(?!\w)', ''),
    (r'(?<!\w)図(?!\w)', ''),
    (r'^2ヨ\s*/?', ''),
    (r'(?<!\w)日(?!\w)', ''),
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

_COMPILED_BUILTIN = [(re.compile(pat), repl) for pat, repl in _BUILTIN_REPLACEMENTS]


def _stage1_regex(text: str) -> str:
    """段階1: 組み込み正規表現 + 学習パターンで修正。"""
    # 組み込みパターン
    for pat, repl in _COMPILED_BUILTIN:
        text = pat.sub(repl, text)
    # 学習パターン (確定済みのみ、単純文字列置換)
    learned = _load_learned_patterns()
    for wrong, correct in learned.get("confirmed", {}).items():
        text = text.replace(wrong, correct)
    return text.strip()


# ─── 段階2: 辞書マッチ (編集距離) ────────────────────────

_GAME_DICTIONARY = [
    # キャラ名
    "鹿目まどか", "暁美ほむら", "美樹さやか", "巴マミ", "佐倉杏子",
    "由比鶴乃", "七海やちよ", "環いろは", "秋野かえで", "深月フェリシア",
    "二葉さな", "水波レナ", "御園かりん", "梓みふゆ", "十咎ももこ",
    "志筑仁美", "キュゥべえ", "早乙女和子",
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


def _stage2_dictionary(text: str, dictionary: list[str] = _GAME_DICTIONARY) -> str:
    """段階2: 辞書マッチで固有名詞の誤認を修正。"""
    words = text.split()
    corrected = []
    for word in words:
        if len(word) < 2:
            corrected.append(word)
            continue
        matches = difflib.get_close_matches(word, dictionary, n=1, cutoff=0.6)
        if matches and matches[0] != word:
            ratio = difflib.SequenceMatcher(None, word, matches[0]).ratio()
            if ratio >= 0.6 and len(word) >= 3:
                corrected.append(matches[0])
                continue
        corrected.append(word)
    return " ".join(corrected)


# ─── 段階3: Gemini Flash ─────────────────────────────────

_GEMINI_MODEL = "gemini-2.0-flash-lite"
_GEMINI_RATE_LIMIT = 4.0


def _stage3_gemini_batch(texts: list[dict[int, str]]) -> dict[int, str]:
    """段階3: Gemini Flash で OCR テキストをバッチ修正。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}

    try:
        import urllib.request

        items = []
        for item in texts:
            for sid, text in item.items():
                items.append({"id": sid, "text": text})

        prompt = f"""以下はゲーム「まどか★マギカ Magia Exedra」の画面から OCR で抽出したテキストです。
OCR の誤認を修正してください。

ルール:
- 明らかな誤字のみ修正（意味が通る場合はそのまま）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、志筑仁美、キュゥべえ、早乙女和子
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

        text_resp = result.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
        corrected_items = json.loads(text_resp)

        result_map = {}
        if isinstance(corrected_items, list):
            for item in corrected_items:
                if isinstance(item, dict) and "id" in item and "text" in item:
                    result_map[item["id"]] = item["text"]

        # Gemini 修正結果からパターンを学習
        for item_dict in texts:
            for sid, orig_text in item_dict.items():
                corrected = result_map.get(sid)
                if corrected and corrected != orig_text:
                    learn_from_correction(orig_text, corrected)

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
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.debug("[OCR_CORRECT] GEMINI_API_KEY 未設定 → スキップ")
        return {}
    time.sleep(_GEMINI_RATE_LIMIT)
    return _stage3_gemini_batch(texts)
