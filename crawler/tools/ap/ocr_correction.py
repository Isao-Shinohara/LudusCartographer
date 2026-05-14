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
    """差分ペアを lc_ocr_corrections に記録 (WriteWorker 経由で直列化)。

    旧実装では timeout=2 秒の自前 conn でロック競合多発 (139 件/セッション)。
    WriteWorker に投入することで database lock 競合を完全排除。
    """
    db_path = Path(__file__).parent.parent.parent / "storage" / "ludus.db"
    if not db_path.exists():
        return
    if not diff_pairs:
        return
    try:
        from tools.ap.write_worker import get_write_worker
        worker = get_write_worker(db_path)
        for old, new in diff_pairs:
            worker.submit(
                "INSERT INTO lc_ocr_corrections "
                "(before_text, after_text, scope, source, frequency) "
                "VALUES (?, ?, 'global', ?, 1) "
                "ON CONFLICT(before_text, after_text, scope, scope_id) "
                "DO UPDATE SET frequency = frequency + 1",
                (old, new, source),
            )
        # 高頻度ルール自動昇格 (frequency >= 5 で promoted_to_regex に)
        worker.submit(
            "UPDATE lc_ocr_corrections SET promoted_to_regex = 1 "
            "WHERE frequency >= 5 AND promoted_to_regex = 0 AND source = ?",
            (source,),
        )
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
_GEMINI_RATE_LIMIT = 0    # 従量課金枠 (1000 RPM) → sleep 不要、並列数で制御
_GEMINI_PARALLEL_WORKERS = 8  # 1枚1リクエストの並列数（REST API直接呼び出しでSDK制約なし）
_GEMINI_BATCH_SIZE = 1    # 1リクエスト1画像（コンテキスト汚染防止で精度最優先）

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

# Implicit Cache の最大活用のため、動的値 (scene_hint, ocr_text) は SYSTEM 側に
# 含めない。固定指示を _GEMINI_SYSTEM_PROMPT として systemInstruction に渡し、
# 動的値は _GEMINI_USER_TEMPLATE 経由で contents 側に渡す。
# (1024+ tok の共通 prefix で Gemini が自動的に 75% 割引する仕組み)
_GEMINI_SYSTEM_PROMPT = '''あなたは「魔法少女まどか★マギカ Magia Exedra」のUI仕様と世界観に精通したデバッグエンジニアです。

ゲーム画面のスクリーンショットから、画面上のテキストを正確に読み取ってください。

## マスターリスト（参考）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、秋野かえで、深月フェリシア、二葉さな、水波レナ、御園かりん、梓みふゆ、十咎ももこ、志筑仁美、キュゥべえ、早乙女和子
- UI用語: パーティー、ホーム、ショップ、ガチャ、クエスト、バトル、スキル、通常攻撃、マギア、ドッペル、キオク、額縁、プレイヤー、推奨、報酬、限界突破、ATTACKER、BUFFER、DEFENDER、BREAK、SKIP、AUTO

## 出力形式（JSONのみ、他の説明不要）
{{
  "corrected_text": "画面上の全テキスト（主要テキストをスペース区切りで）",
  "corrections": [
    {{"before": "初期OCRの誤読", "after": "正しいテキスト"}}
  ],
  "is_artifact": false,
  "screen_type": "ADV",
  "noise_words": ["AUTO", "SKIP"]
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

## UIノイズ語の抽出
テキスト中に、画面の本来のコンテンツ（セリフ、メニュー名、説明文）ではなく、
UIの装飾・ボタン・ステータス表示として頻出する短い文字列を検出してください。
例: "AUTO", "SKIP", "WAVE", "Turn", "+", "×", "NEW", "Lv.", "HP", "MP", "MAX"
（該当なしなら空配列 []）

## is_artifact 判定ルール
以下の【判定ステップ】に沿って、上から順に評価し、最初に該当した条件で判定を確定してください。

**ステップ1: 演出エフェクトか？（該当 → is_artifact=true で確定）**
以下のいずれかに当てはまれば、他の要素の有無に関わらず直ちに true:
1. **バトル中のスキル・必殺技の演出**: 画面の大部分（50%以上）がビーム、光線、爆発、魔法陣、閃光などのエフェクトで覆われている。
   ★重要: これは「バトル画面（HPバー、キャラ顔アイコン列、コマンドボタン等のバトルUIが画面に存在する）」であることが前提です。バトルUIが一切ないストーリームービーの爆発シーンは例外としてステップ2へ進んでください。
   （※バトル中であれば、以下の要素が見えていても true）:
   - SKIPボタン
   - 画面下部の小さなキャラ顔アイコン列
   - ロール表示（ATTACKER, BUFFER, DEFENDER, BREAKER 等）
   - エフェクト越しに透けて見えるキャラクターのシルエット
2. **暗転・黒ベタ**: 画面がほぼ黒一色、またはロード中アイコンのみ
3. **ホワイトアウト・空白フレーム・白飛び**: 画面の大部分（70%以上）が白・灰白色などの均一色で覆われた状態。以下のいずれも該当:
   a) 演出としての白飛び（キャラ/UI が白い膜の下にうっすら見える）
   b) ロード/ダウンロード遷移中の空白フレーム（画面が空白で、進捗バーや小さな数字しか見えない）
4. **不完全なキャプチャ**: 画面が半分切れている、または著しいノイズで状況が不明

※ 注: 画像の左右や上下にある黒い余白は scrcpy の表示用レターボックスであり、ゲーム要素ではありません。判定材料に使わないでください。

**ステップ2: 残す画面か？（該当 → is_artifact=false で確定）**
ステップ1に該当しない場合、以下に該当すれば false:
- 人物（キャラクター）の本体が**エフェクトに遮られず明瞭に**視認できる画面（全身、顔のクローズアップ、手元・目元・口元、後ろ姿を含む。テキストが一切ないアニメ風カットも false）
  ※ 画面下部の「小さなキャラ顔アイコン」だけでは「キャラクターが見えている」と判断しない
  ※ エフェクト越しのシルエット・輪郭だけでは「明瞭に視認できる」と判断しない
- **ストーリームービーの1フレーム (MOVIE_CUT)**: バトルUIが存在せず、字幕（テキストボックス外の小さなテキスト）または明らかな映像演出（爆発、瓦礫、廃墟、背景描写など）を伴う固有のシーン。エフェクトや風景のみでキャラクター本体が不在でも false。
  - また、検出器が推定したシーンが MOVIE の場合、この画像は確実にムービーカットなので無条件で false (MOVIE_CUT) として扱ってください。
- メニュー、ホーム、編成、バトルコマンド選択（エフェクトで覆われていない安定した状態に限る）、ダイアログ、リザルト画面
- セリフ付きの会話シーン（ADV）

**判定の原則**: バトルUIが存在し、かつエフェクトが画面の半分以上を覆っていたら true（迷ったら true）。ただし、バトルUIが一切ないムービー風のカットシーンはストーリー上の固有フレームである可能性が高いため、安易に true にせず、字幕の有無や映像内容を主軸に false (MOVIE_CUT) と判定してください。

※テキスト抽出に関する警告:
corrected_text が空であることと is_artifact=true は全く無関係です。テキストが空でもキャラクター本体が明瞭に視認できれば必ず false。

【判定例】
- 例1: 画面の大半がレーザーと爆発で覆われているが、画面下に「WAVE 1/3」やキャラの顔アイコン列が並んでいる。 → バトル中の必殺技なので is_artifact=true, screen_type=ARTIFACT
- 例2: 画面の大半が瓦礫と煙の爆発で覆われておりキャラはいないが、バトルUIは一切なく、画面下に小さく「今度こそ…」と字幕がある。 → ムービーの1カットなので is_artifact=false, screen_type=MOVIE_CUT
- 例3: 画面が灰白色でほぼ空白、右下に小さな進捗バーと「5 MB」だけが見える。 → ダウンロード中の空白フレームなので is_artifact=true, screen_type=ARTIFACT

## screen_type 判定ルール
先に決定した is_artifact の値に基づいて判定してください。
- is_artifact=true の場合: 必ず "ARTIFACT"
- is_artifact=false の場合: 以下の優先順位で分類
  1. ストーリームービーの1フレーム（バトルUIなし + シネマスコープ/字幕） → "MOVIE_CUT"
  2. テキストボックスとキャラクター名がある → "ADV"
  3. バトルのUI（HPバー、コマンド）がある → "BATTLE_UI"
  4. 上記以外（メニュー、ホーム、カットシーン等） → "HOME"'''


# 動的値テンプレ (contents 側に渡る、リクエスト毎に異なる)
_GEMINI_USER_TEMPLATE = '''## 参考: 検出器が推定したシーン
{scene_hint}

## 参考: 初期 OCR 結果（誤読の可能性あり、参考程度に）
{ocr_text}

添付画像とこれらの参考情報を用い、システム指示の判定ルールに従って JSON を出力してください。'''


# 後方互換: 既存テストやデバッグツールが _GEMINI_PROMPT 全体を参照する。
# SYSTEM + USER を連結したものを公開する (実際の API call では分けて送る)。
_GEMINI_PROMPT = _GEMINI_SYSTEM_PROMPT + "\n\n" + _GEMINI_USER_TEMPLATE


_GEMINI_BATCH_SYSTEM_PROMPT = '''あなたは「魔法少女まどか★マギカ Magia Exedra」のUI仕様と世界観に精通したデバッグエンジニアです。

複数のゲーム画面のスクリーンショットから、各画面のテキストを正確に読み取ってください。
画像は順番に「画像1, 画像2, ...」として渡されます。

**重要: 各画像は互いに無関係です。他の画像と比較せず、1枚ずつ独立に判定してください。**

## マスターリスト（参考）
- キャラ名: 鹿目まどか、暁美ほむら、美樹さやか、巴マミ、佐倉杏子、由比鶴乃、七海やちよ、環いろは、秋野かえで、深月フェリシア、二葉さな、水波レナ、御園かりん、梓みふゆ、十咎ももこ、志筑仁美、キュゥべえ、早乙女和子
- UI用語: パーティー、ホーム、ショップ、ガチャ、クエスト、バトル、スキル、通常攻撃、マギア、ドッペル、キオク、額縁、プレイヤー、推奨、報酬、限界突破、ATTACKER、BUFFER、DEFENDER、BREAK、SKIP、AUTO

## 出力形式（JSONのみ、他の説明不要）
{{
  "results": [
    {{
      "index": 1,
      "corrected_text": "画像1のテキスト",
      "corrections": [{{"before": "誤読", "after": "正"}}],
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
以下の【判定ステップ】に沿って、上から順に評価し、最初に該当した条件で判定を確定してください。

**ステップ1: 演出エフェクトか？（該当 → is_artifact=true で確定）**
以下のいずれかに当てはまれば、他の要素の有無に関わらず直ちに true:
1. **バトル中のスキル・必殺技の演出**: 画面の大部分（50%以上）がビーム、光線、爆発、魔法陣、閃光などのエフェクトで覆われている。
   ★重要: これは「バトル画面（HPバー、キャラ顔アイコン列、コマンドボタン等のバトルUIが画面に存在する）」であることが前提です。バトルUIが一切ないストーリームービーの爆発シーンは例外としてステップ2へ進んでください。
   （※バトル中であれば、以下の要素が見えていても true）:
   - SKIPボタン
   - 画面下部の小さなキャラ顔アイコン列
   - ロール表示（ATTACKER, BUFFER, DEFENDER, BREAKER 等）
   - エフェクト越しに透けて見えるキャラクターのシルエット
2. **暗転・黒ベタ**: 画面がほぼ黒一色、またはロード中アイコンのみ
3. **ホワイトアウト・空白フレーム・白飛び**: 画面の大部分（70%以上）が白・灰白色などの均一色で覆われた状態。以下のいずれも該当:
   a) 演出としての白飛び（キャラ/UI が白い膜の下にうっすら見える）
   b) ロード/ダウンロード遷移中の空白フレーム（画面が空白で、進捗バーや小さな数字しか見えない）
4. **不完全なキャプチャ**: 画面が半分切れている、または著しいノイズで状況が不明

※ 注: 画像の左右や上下にある黒い余白は scrcpy の表示用レターボックスであり、ゲーム要素ではありません。判定材料に使わないでください。

**ステップ2: 残す画面か？（該当 → is_artifact=false で確定）**
ステップ1に該当しない場合、以下に該当すれば false:
- 人物（キャラクター）の本体が**エフェクトに遮られず明瞭に**視認できる画面（全身、顔のクローズアップ、手元・目元・口元、後ろ姿を含む。テキストが一切ないアニメ風カットも false）
  ※ 画面下部の「小さなキャラ顔アイコン」だけでは「キャラクターが見えている」と判断しない
  ※ エフェクト越しのシルエット・輪郭だけでは「明瞭に視認できる」と判断しない
- **ストーリームービーの1フレーム (MOVIE_CUT)**: バトルUIが存在せず、字幕（テキストボックス外の小さなテキスト）または明らかな映像演出（爆発、瓦礫、廃墟、背景描写など）を伴う固有のシーン。エフェクトや風景のみでキャラクター本体が不在でも false。
  - また、検出器が推定したシーンが MOVIE の場合、この画像は確実にムービーカットなので無条件で false (MOVIE_CUT) として扱ってください。
- メニュー、ホーム、編成、バトルコマンド選択（エフェクトで覆われていない安定した状態に限る）、ダイアログ、リザルト画面
- セリフ付きの会話シーン（ADV）

**判定の原則**: バトルUIが存在し、かつエフェクトが画面の半分以上を覆っていたら true（迷ったら true）。ただし、バトルUIが一切ないムービー風のカットシーンはストーリー上の固有フレームである可能性が高いため、安易に true にせず、字幕の有無や映像内容を主軸に false (MOVIE_CUT) と判定してください。

※テキスト抽出に関する警告:
corrected_text や noise_words が空であることと is_artifact=true は全く無関係です。テキストが空でもキャラクター本体が明瞭に視認できれば必ず false。

【判定例】
- 例1: 画面の大半がレーザーと爆発で覆われているが、画面下に「WAVE 1/3」やキャラの顔アイコン列が並んでいる。 → バトル中の必殺技なので is_artifact=true, screen_type=ARTIFACT
- 例2: 画面の大半が瓦礫と煙の爆発で覆われておりキャラはいないが、バトルUIは一切なく、画面下に小さく「今度こそ…」と字幕がある。 → ムービーの1カットなので is_artifact=false, screen_type=MOVIE_CUT
- 例3: 画面が灰白色でほぼ空白、右下に小さな進捗バーと「5 MB」だけが見える。 → ダウンロード中の空白フレームなので is_artifact=true, screen_type=ARTIFACT

## screen_type 判定ルール (string)
先に決定した is_artifact の値に基づいて判定してください。
- is_artifact=true の場合: 必ず "ARTIFACT"
- is_artifact=false の場合: 以下の優先順位で分類
  1. ストーリームービーの1フレーム（バトルUIなし + シネマスコープ/字幕） → "MOVIE_CUT"
  2. テキストボックスとキャラクター名がある → "ADV"
  3. バトルのUI（HPバー、コマンド）がある → "BATTLE_UI"
  4. 上記以外（メニュー、ホーム、カットシーン等） → "HOME"

## UIノイズ語の抽出
各画像のテキスト中に、画面の本来のコンテンツ（セリフ、メニュー名、説明文）ではなく、
UIの装飾・ボタン・ステータス表示として頻出する短い文字列を検出してください。
例: "AUTO", "SKIP", "WAVE", "Turn", "+", "×", "NEW", "Lv.", "HP", "MP", "MAX"
これらは画面の同一性判定では無視すべきノイズです。

result オブジェクトに以下を追加:
"noise_words": ["AUTO", "SKIP"]
（該当なしなら空配列 []）'''


# batch 版動的値テンプレ (各画像のシーン推定 + 初期 OCR を一括渡し)
_GEMINI_BATCH_USER_TEMPLATE = '''## 各画像の検出器シーン推定 + 初期 OCR 結果（誤読の可能性あり、参考程度に）
各行: `画像N: [scene=<検出器の推定>] <初期OCR>`
- scene=MOVIE はストーリームービーのカットシーンが確実に検出されたケース
- scene=UNKNOWN は検出器でも分類できなかったケース（必要なら画像から判断）
{ocr_block}

添付画像群とこの参考情報を用い、システム指示の判定ルールに従って JSON を出力してください。'''


# 後方互換: 既存テスト用に SYSTEM + USER を連結したものを公開
_GEMINI_BATCH_PROMPT = _GEMINI_BATCH_SYSTEM_PROMPT + "\n\n" + _GEMINI_BATCH_USER_TEMPLATE


_GEMINI_TIMEOUT = 60  # API リクエストタイムアウト (秒)
_GEMINI_JSON_RETRIES = 2  # JSON パース失敗時の追加リトライ回数 (truncated レスポンス対策、合計 1+N 回)


def _init_gemini_client():
    """Gemini クライアントを初期化（遅延ロード）。"""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None
    try:
        from google import genai
        return genai.Client(
            api_key=api_key,
            http_options=genai.types.HttpOptions(timeout=_GEMINI_TIMEOUT),
        )
    except Exception as e:
        logger.warning("[GEMINI] クライアント初期化失敗: %s", e)
        return None


def gemini_correct_single(
    screenshot_path: str,
    ocr_text: str,
    client=None,
    item_id: Optional[int] = None,
    scene: Optional[str] = None,
) -> Optional[dict]:
    """1枚の画像に対して Gemini REST API で OCR 補正を実行。

    google-genai SDK の画像送信にタイムアウトバグがあるため、
    urllib で REST API を直接呼び出す。

    scene: 検出器が推定したシーン (MOVIE/ADV/BATTLE/UNKNOWN 等)。
        プロンプトに渡され、特に MOVIE はムービーカット保護のヒントになる。

    Returns: {"id": int, "corrected_text": str, "corrections": list,
              "is_artifact": bool, "screen_type": str, "noise_words": list} or None
    """
    import base64
    import time as _time
    import random as _random
    import urllib.request

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return None

    try:
        # Jitter: SSLハンドシェイク衝突防止（Thundering Herd対策）
        _time.sleep(_random.uniform(0.1, 1.5))

        img_path = Path(screenshot_path)
        if not img_path.exists():
            return None

        # scrcpy 黒帯を除去してから送信する。Gemini のホワイトアウト/MOVIE_CUT
        # 判定が黒帯に影響されるため、純粋なゲーム描画領域だけを送る。
        # クロップ失敗時は元画像を送信 (フォールバック)。
        cropped_data: Optional[bytes] = None
        try:
            import cv2 as _cv2
            from tools.ap.image_proc import get_roi_cropped_image
            _bgr = _cv2.imread(str(img_path), _cv2.IMREAD_COLOR)
            if _bgr is not None:
                _cropped = get_roi_cropped_image(_bgr)
                # 元画像と shape が違えばクロップが効いている → エンコードして使う
                if _cropped is not None and _cropped.shape != _bgr.shape:
                    _ext = ".webp" if img_path.suffix == ".webp" else ".png"
                    _ok, _buf = _cv2.imencode(_ext, _cropped)
                    if _ok:
                        cropped_data = _buf.tobytes()
        except Exception:
            cropped_data = None

        if cropped_data is not None:
            img_data = cropped_data
        else:
            with open(img_path, "rb") as f:
                img_data = f.read()

        mime = "image/webp" if img_path.suffix == ".webp" else "image/png"
        img_b64 = base64.b64encode(img_data).decode()

        scene_hint = f"検出器の推定シーン: {scene}" if scene else "検出器の推定シーン: (情報なし)"
        # 動的値は USER 側のみに含める (SYSTEM は完全固定で Implicit Cache 対象)。
        user_prompt = _GEMINI_USER_TEMPLATE.format(
            ocr_text=ocr_text, scene_hint=scene_hint,
        )
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{_GEMINI_MODEL}:generateContent?key={api_key}"
        body = json.dumps({
            "systemInstruction": {
                "parts": [{"text": _GEMINI_SYSTEM_PROMPT}],
            },
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": mime, "data": img_b64}},
                {"text": user_prompt},
            ]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "maxOutputTokens": 16384,
                "temperature": 0.1,
            },
        }).encode()

        # JSON parse 失敗 (= truncated レスポンス) は同一リクエストでリトライ。
        # HTTP / その他の例外は永続的な可能性があるためリトライしない。
        last_json_err: Optional[json.JSONDecodeError] = None
        for attempt in range(_GEMINI_JSON_RETRIES + 1):
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=_GEMINI_TIMEOUT) as resp:
                raw = resp.read()
            try:
                resp_data = json.loads(raw)
            except json.JSONDecodeError as e:
                last_json_err = e
                if attempt < _GEMINI_JSON_RETRIES:
                    _time.sleep(0.3 + _random.uniform(0, 0.4) + attempt * 0.5)
                    continue
                logger.warning("[GEMINI] JSON パース失敗 (リトライ%d回後): %s",
                               _GEMINI_JSON_RETRIES, e)
                return None

            # API 使用量記録
            usage = resp_data.get("usageMetadata", {})
            in_tok = usage.get("promptTokenCount", 0)
            out_tok = usage.get("candidatesTokenCount", 0)
            from tools.ap.api_usage import record_api_usage
            record_api_usage(_GEMINI_MODEL, "hq_ocr", in_tok, out_tok)

            # レスポンス解析
            candidates = resp_data.get("candidates", [])
            if not candidates:
                logger.warning("[GEMINI] 応答なし (safety filter?)")
                return None
            # MAX_TOKENS truncation を早期検出: 同じプロンプト+画像で再試行しても
            # 結果は同じなのでリトライせず即諦める (API コスト削減)。
            finish_reason = candidates[0].get("finishReason", "")
            if finish_reason == "MAX_TOKENS":
                logger.warning("[GEMINI] MAX_TOKENS truncated → リトライせず即諦め (id=%s)",
                               item_id)
                return {"error": "truncated", "id": item_id}
            parts = candidates[0].get("content", {}).get("parts", [])
            if not parts:
                logger.warning("[GEMINI] 応答パーツなし")
                return None
            text = parts[0].get("text", "").strip()

            # ```json ... ``` を除去
            if text.startswith("```"):
                text = re.sub(r'^```\w*\n?', '', text)
                text = re.sub(r'\n?```$', '', text)

            try:
                result = json.loads(text)
            except json.JSONDecodeError as e:
                last_json_err = e
                if attempt < _GEMINI_JSON_RETRIES:
                    _time.sleep(0.3 + _random.uniform(0, 0.4) + attempt * 0.5)
                    continue
                # 永続的な失敗 (truncated 等): 後続バッチで再試行されないよう
                # marker 付きで返す。呼び出し元で sentinel ('') 化する。
                logger.warning("[GEMINI] 内側 JSON パース失敗 (リトライ%d回後): %s",
                               _GEMINI_JSON_RETRIES, e)
                return {"error": "truncated", "id": item_id}

            # パターン学習
            corrected = result.get("corrected_text", "")
            if corrected and corrected != ocr_text:
                for c in result.get("corrections", []):
                    if c.get("before") and c.get("after"):
                        learn_from_correction(c["before"], c["after"])

            if item_id is not None:
                result["id"] = item_id
            return result

        # ループ脱出は到達しないはず (return か continue のみ)
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
        items: [{"id": int, "screenshot_path": str, "ocr_text": str, "scene"?: str}, ...]
            scene は省略可。検出器の推定シーン (MOVIE/ADV/BATTLE/UNKNOWN 等)。
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
            _scene = item.get("scene") or "UNKNOWN"
            ocr_lines.append(f"画像{i}: [scene={_scene}] {item.get('ocr_text', '')}")
            valid_items.append((i, item))

        if not contents:
            return []

        # 動的値は USER 側のみに含める (SYSTEM は完全固定で Implicit Cache 対象)。
        user_prompt = _GEMINI_BATCH_USER_TEMPLATE.format(
            ocr_block="\n".join(ocr_lines),
        )
        contents.append(user_prompt)

        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=contents,
            config=_genai.types.GenerateContentConfig(
                system_instruction=_GEMINI_BATCH_SYSTEM_PROMPT,
                response_mime_type="application/json",
                max_output_tokens=8192,
                temperature=0.1,
            ),
        )

        # API 使用量記録
        from tools.ap.api_usage import record_api_usage, extract_usage_from_response
        in_tok, out_tok = extract_usage_from_response(response)
        record_api_usage(_GEMINI_MODEL, "hq_ocr", in_tok, out_tok)

        if response.text is None:
            logger.warning("[GEMINI] バッチ応答が空 (safety filter?)")
            return None
        text = response.text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)

        # response_mime_type="application/json" により構造化出力が保証されるが、
        # フォールバックとして制御文字除去も残す
        try:
            result = json.loads(text, strict=False)
        except json.JSONDecodeError:
            cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
            result = json.loads(cleaned, strict=False)
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
                "is_artifact": r.get("is_artifact", False),
                "screen_type": r.get("screen_type", ""),
                "noise_words": r.get("noise_words", []),
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
                # API 使用量記録
                from tools.ap.api_usage import record_api_usage, extract_usage_from_response
                in_tok, out_tok = extract_usage_from_response(response)
                record_api_usage(_GEMINI_MODEL, "hq_ocr", in_tok, out_tok)

                if response.text is None:
                    continue
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
