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
    また DB の lc_ocr_corrections テーブルにも記録 (Phase 2/4)。
    """
    if not original or not corrected or original == corrected:
        return

    # 単語レベルで差分抽出
    orig_words = original.split()
    corr_words = corrected.split()
    sm = difflib.SequenceMatcher(None, orig_words, corr_words)

    data = _load_learned_patterns()
    updated = False
    diff_pairs: list[tuple[str, str]] = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace":
            old = " ".join(orig_words[i1:i2])
            new = " ".join(corr_words[j1:j2])
            if old and new and old != new and len(old) >= 2:
                diff_pairs.append((old, new))
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

    # DB の lc_ocr_corrections にも記録 (Phase 2/4)
    if diff_pairs:
        _record_corrections_to_db(diff_pairs, source="gemini")


def _record_corrections_to_db(diff_pairs: list, source: str = "gemini") -> None:
    """差分ペアを lc_ocr_corrections に記録。"""
    db_path = Path(__file__).parent.parent.parent / "storage" / "ludus.db"
    if not db_path.exists():
        return
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            for old, new in diff_pairs:
                conn.execute(
                    "INSERT INTO lc_ocr_corrections "
                    "(before_text, after_text, scope, source, frequency) "
                    "VALUES (?, ?, 'global', ?, 1) "
                    "ON CONFLICT(before_text, after_text, scope, scope_id) "
                    "DO UPDATE SET frequency = frequency + 1",
                    (old, new, source),
                )
            # 高頻度ルール自動昇格 (frequency >= 5 で promoted_to_regex に)
            conn.execute(
                "UPDATE lc_ocr_corrections SET promoted_to_regex = 1 "
                "WHERE frequency >= 5 AND promoted_to_regex = 0 AND source = ?",
                (source,),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[OCR_LEARN] DB 記録失敗: %s", e)


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

_GEMINI_MODEL = "gemini-2.5-flash-lite"
_GEMINI_RATE_LIMIT = 1.5  # 40 RPM (有料枠) → 1.5秒間隔で安全
_GEMINI_BATCH_SIZE = 8    # 1リクエストあたりの画像枚数（観察しながら調整）

# Gemini が誤って返す「テキストなし」系の説明文パターン (空文字に変換)
_NO_TEXT_PATTERNS = [
    r'^テキスト(が|は)?(なし|ありません|ない).*',
    r'^文字(が|は)?(なし|ありません|ない).*',
    r'.*画像(には|に)?(テキスト|文字)(が|は)?(ない|ありません|存在しない).*',
    r'^(背景|イベントシーン|暗転|空白|黒画面|白画面)(の一部|のみ)?$',
]
_NO_TEXT_RE = re.compile('|'.join(_NO_TEXT_PATTERNS))


def _clean_gemini_output(text: str) -> str:
    """Gemini が説明文を返した場合は空文字に変換。"""
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    # 先頭60文字で判定（長文はそのまま返す = 実際のテキスト）
    head = stripped[:60]
    if _NO_TEXT_RE.match(head):
        return ""
    return text

_GEMINI_PROMPT = '''あなたは「魔法少女まどか★マギカ Magia Exedra」のUI仕様と世界観に精通したデバッグエンジニアです。

ゲーム画面のスクリーンショットから、画面上のテキストを正確に読み取ってください。

## マスターリスト（参考）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、秋野かえで、深月フェリシア、二葉さな、水波レナ、御園かりん、梓みふゆ、十咎ももこ、志筑仁美、キュゥべえ、早乙女和子
- UI用語: パーティー、ホーム、ショップ、ガチャ、クエスト、バトル、スキル、通常攻撃、マギア、ドッペル、キオク、額縁、プレイヤー、推奨、報酬、限界突破、ATTACKER、BUFFER、DEFENDER、BREAK、SKIP、AUTO

## 参考: 初期 OCR 結果（誤読の可能性あり、参考程度に）
{ocr_text}

## 出力形式（JSONのみ、他の説明不要）
{{
  "corrected_text": "画面上の全テキスト（主要テキストをスペース区切りで）",
  "corrections": [
    {{"before": "初期OCRの誤読", "after": "正しいテキスト", "reason": "理由"}}
  ],
  "is_artifact": false,
  "screen_type": "ADV"
}}

## 重要な制約
- 画像にテキストが「全く存在しない」場合（イベントシーン・背景・暗転等）は corrected_text を **空文字 ""** にする
- 画像の説明や解釈（「テキストなし」「背景の一部」等の文）を corrected_text に書かない
- 画面に表示されている文字のみ抽出する。説明文や注釈は不要
- 初期OCR が空または無関係な場合も画像から読み取ってください
- corrections は誤読を検出した場合のみ

## 文字の忠実性（厳守）
- 画面に表示されている文字を「そのまま」抽出する
- ひらがな→漢字、カタカナ→ひらがな等の **文字種変換は禁止**
  - 例: 「つかいま」→「使い魔」に変えない
- 修正対象は OCR 由来の **誤認識のみ** (例: 「明美」→「暁美」、「fu.」→「Lv.」)

## is_artifact 判定ルール
以下のいずれかに該当する場合 true:
1. 演出・エフェクト中（派手な光、スキル演出が大部分を占める）
2. 非情報フレーム（暗転、真っ白、テキストも操作UIもない）
3. 獲得・リザルトポップアップ（「OK」のみの単純な通知）
4. 不完全なキャプチャ（半分切れ、ノイズ）

## screen_type: "HOME" / "ADV" / "BATTLE_UI" / "ARTIFACT"'''


_GEMINI_BATCH_PROMPT = '''あなたは「魔法少女まどか★マギカ Magia Exedra」のUI仕様と世界観に精通したデバッグエンジニアです。

複数のゲーム画面のスクリーンショットから、各画面のテキストを正確に読み取ってください。
画像は順番に「画像1, 画像2, ...」として渡されます。

## マスターリスト（参考）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、秋野かえで、深月フェリシア、二葉さな、水波レナ、御園かりん、梓みふゆ、十咎ももこ、志筑仁美、キュゥべえ、早乙女和子
- UI用語: パーティー、ホーム、ショップ、ガチャ、クエスト、バトル、スキル、通常攻撃、マギア、ドッペル、キオク、額縁、プレイヤー、推奨、報酬、限界突破、ATTACKER、BUFFER、DEFENDER、BREAK、SKIP、AUTO

## 各画像の初期 OCR 結果（誤読の可能性あり、参考程度に）
{ocr_block}

## 出力形式（JSONのみ、他の説明不要）
{{
  "results": [
    {{
      "index": 1,
      "corrected_text": "画像1のテキスト",
      "corrections": [{{"before": "誤読", "after": "正", "reason": "..."}}],
      "is_artifact": false,
      "screen_type": "ADV"
    }},
    {{
      "index": 2,
      "corrected_text": "",
      "corrections": [],
      "is_artifact": true,
      "screen_type": "ARTIFACT"
    }}
  ]
}}

## 重要な制約
- 各画像について必ず1つのオブジェクトを返してください
- 画像にテキストが「全く存在しない」場合（イベントシーン・背景・暗転等）は corrected_text を **空文字 ""** にする
- 画像の説明や解釈（「テキストなし」「背景の一部」等の文）を corrected_text に書かない
- 画面に表示されている文字のみ抽出する。説明文や注釈は不要
- corrections は誤読を検出した場合のみ

## 文字の忠実性（厳守）
- 画面に表示されている文字を「そのまま」抽出する
- ひらがな→漢字、カタカナ→ひらがな等の **文字種変換は禁止**
  - 例: 「つかいま」→「使い魔」に変えない
  - 例: 「ひかりのま」→「光の間」に変えない
- 省略記号「…」「・・・」もそのまま
- スペース・改行も画面表示通り
- 修正対象は OCR 由来の **誤認識のみ** (例: 「明美」→「暁美」、「fu.」→「Lv.」)
  - 文字種そのものが画面と一致しているなら触らない

## is_artifact 判定ルール (boolean)
以下のいずれかに該当する場合、true（記録不要）と判定してください。

1. **演出・エフェクト中**:
   - 派手な光、攻撃エフェクト、スキル演出が画面の大部分を占めている。
   - キャラクターのカットインのみで、操作UI（コマンドボタン等）がない。
2. **非情報フレーム**:
   - 暗転、真っ白、または著しくボケている画面。
   - 動画再生中などで、テキストや操作UIが一切存在しない。
3. **獲得・リザルトポップアップ**:
   - アイテム獲得、報酬表示などの一時的な通知（「OK」ボタンのみの単純なもの）。
   - 地図としての情報（場所、メニュー、会話）が含まれていない。
4. **不完全なキャプチャ**:
   - 画面が半分切れている、または読み取り不能なノイズ。

is_artifact=false とすべきもの（残す画面）:
- メニュー画面、ホーム画面、編成画面
- セリフ付きの会話シーン（ADV）
- バトルのコマンド選択画面（演出中ではないUI状態）
- ダイアログ・ポップアップ（操作を伴うもの）

## screen_type 判定ルール (string)
判定の根拠として、以下のいずれかを割り当ててください。
- "HOME": ホーム、メニュー、編成画面
- "ADV": セリフや名前が表示されている会話シーン
- "BATTLE_UI": コマンド選択可能なバトル画面
- "ARTIFACT": 上記の除外対象（演出、暗転、ポップアップ等）

各 result オブジェクトに以下を追加:
"is_artifact": false,
"screen_type": "HOME"'''


def _init_gemini_client():
    """Gemini クライアントを初期化（遅延ロード）。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=api_key)
    except Exception as e:
        logger.warning("[GEMINI] クライアント初期化失敗: %s", e)
        return None


def gemini_correct_single(
    screenshot_path: str,
    ocr_text: str,
    client=None,
) -> Optional[dict]:
    """1枚の画像に対して Gemini で OCR 補正を実行。

    Returns: {"corrected_text": str, "corrections": list} or None
    """
    if client is None:
        client = _init_gemini_client()
    if client is None:
        return None

    from google import genai as _genai

    try:
        img_path = Path(screenshot_path)
        if not img_path.exists():
            return None

        with open(img_path, "rb") as f:
            img_data = f.read()

        mime = "image/webp" if img_path.suffix == ".webp" else "image/png"

        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[
                _genai.types.Part.from_bytes(data=img_data, mime_type=mime),
                _GEMINI_PROMPT.format(ocr_text=ocr_text),
            ],
        )

        text = response.text.strip()
        # ```json ... ``` を除去
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)

        result = json.loads(text)

        # パターン学習
        corrected = result.get("corrected_text", "")
        if corrected and corrected != ocr_text:
            for c in result.get("corrections", []):
                if c.get("before") and c.get("after"):
                    learn_from_correction(c["before"], c["after"])

        return result

    except json.JSONDecodeError as e:
        logger.warning("[GEMINI] JSON パース失敗: %s", e)
        return None
    except Exception as e:
        logger.warning("[GEMINI] API エラー: %s", e)
        return None


def gemini_correct_multi(
    items: list[dict],
    client=None,
) -> Optional[list[dict]]:
    """複数画像を1リクエストでバッチ補正。

    Args:
        items: [{"id": int, "screenshot_path": str, "ocr_text": str}, ...]
        client: Gemini クライアント (None なら自動作成)

    Returns:
        [{"id": int, "corrected_text": str, "corrections": list}, ...] or None
    """
    if not items:
        return []
    if client is None:
        client = _init_gemini_client()
    if client is None:
        return None

    from google import genai as _genai

    try:
        # 画像読み込み + 検証
        contents: list = []
        ocr_lines = []
        valid_items = []
        for i, item in enumerate(items, 1):
            img_path = Path(item["screenshot_path"])
            if not img_path.exists():
                continue
            with open(img_path, "rb") as f:
                img_data = f.read()
            mime = "image/webp" if img_path.suffix == ".webp" else "image/png"
            contents.append(_genai.types.Part.from_bytes(data=img_data, mime_type=mime))
            ocr_lines.append(f"画像{i}: {item.get('ocr_text', '')}")
            valid_items.append((i, item))

        if not contents:
            return []

        prompt = _GEMINI_BATCH_PROMPT.format(ocr_block="\n".join(ocr_lines))
        contents.append(prompt)

        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=contents,
        )

        text = response.text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)

        result = json.loads(text)
        results = result.get("results", [])

        # index → 元の id にマッピング
        index_to_id = {idx: item["id"] for idx, item in valid_items}
        index_to_orig = {idx: item.get("ocr_text", "") for idx, item in valid_items}

        output = []
        for r in results:
            idx = r.get("index")
            if idx not in index_to_id:
                continue
            corrected = r.get("corrected_text", "")
            output.append({
                "id": index_to_id[idx],
                "corrected_text": corrected,
                "corrections": r.get("corrections", []),
            })
            # パターン学習
            orig = index_to_orig[idx]
            if corrected and corrected != orig:
                for c in r.get("corrections", []):
                    if c.get("before") and c.get("after"):
                        learn_from_correction(c["before"], c["after"])

        return output

    except json.JSONDecodeError as e:
        logger.warning("[GEMINI] バッチ JSON パース失敗: %s", e)
        return None
    except Exception as e:
        logger.warning("[GEMINI] バッチ API エラー: %s", e)
        return None


def _stage3_gemini_batch(texts: list[dict[int, str]]) -> dict[int, str]:
    """段階3: Gemini Flash でテキストのみバッチ修正（画像なし、フォールバック用）。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {}

    client = _init_gemini_client()
    if client is None:
        return {}

    result_map = {}
    for item_dict in texts:
        for sid, text in item_dict.items():
            try:
                response = client.models.generate_content(
                    model=_GEMINI_MODEL,
                    contents=f"以下のOCRテキストの誤読を修正してください。"
                             f"修正後のテキストのみ返してください。\n\n{text}",
                )
                corrected = response.text.strip()
                if corrected:
                    result_map[sid] = corrected
                    if corrected != text:
                        learn_from_correction(text, corrected)
            except Exception as e:
                logger.warning("[GEMINI] batch 修正失敗 id=%d: %s", sid, e)
            time.sleep(_GEMINI_RATE_LIMIT)

    return result_map


# ─── パイプライン実行 ─────────────────────────────────────

def correct_ocr_text(text: str) -> str:
    """段階1+2 でテキストを修正 (ローカルのみ、高速)。"""
    text = _stage1_regex(text)
    text = _stage2_dictionary(text)
    return text


# ─── Phase 4: DB ルール適用 ──────────────────────────────

def apply_db_corrections(text: str, db_path: Optional[Path] = None) -> str:
    """DB の lc_ocr_corrections から global ルールを適用 (周回開始時用)。

    promoted_to_regex = 1 のルールは正規表現として適用。
    その他は単純な文字列置換。
    """
    if not text:
        return text
    if db_path is None:
        db_path = Path(__file__).parent.parent.parent / "storage" / "ludus.db"
    if not db_path.exists():
        return text
    try:
        import sqlite3
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rules = conn.execute(
                "SELECT before_text, after_text, promoted_to_regex"
                " FROM lc_ocr_corrections"
                " WHERE scope = 'global'"
                " ORDER BY frequency DESC LIMIT 200"
            ).fetchall()
            for rule in rules:
                if rule["promoted_to_regex"]:
                    try:
                        text = re.sub(rule["before_text"], rule["after_text"], text)
                    except Exception:
                        pass
                else:
                    if rule["before_text"] in text:
                        text = text.replace(rule["before_text"], rule["after_text"])
            return text
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[OCR_CORRECT] DB ルール適用失敗: %s", e)
        return text


def correct_ocr_full(text: str, db_path: Optional[Path] = None) -> str:
    """フルパイプライン: 組み込み regex + 辞書 + DB ルール (Phase 4)。"""
    text = correct_ocr_text(text)
    text = apply_db_corrections(text, db_path)
    return text


def correct_batch_gemini(texts: list[dict[int, str]]) -> dict[int, str]:
    """段階3: Gemini でバッチ修正。API キー未設定なら空辞書を返す。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        logger.debug("[OCR_CORRECT] GEMINI_API_KEY 未設定 → スキップ")
        return {}
    time.sleep(_GEMINI_RATE_LIMIT)
    return _stage3_gemini_batch(texts)
