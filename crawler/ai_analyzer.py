"""
ai_analyzer.py — Vertex AI (Gemini) によるゲーム画面解析

GameAnalyzer クラスは画像を受け取り、以下を返す:
  - 画面の種類 (screen_type)
  - クリック可能な重要ボタンの一覧 (buttons)

認証: ADC (Application Default Credentials) を使用する。
      事前に以下を実行しておくこと:
        gcloud auth application-default login
      JSONキーファイルは不要。ライブラリが ADC を自動参照する。

使用例:
    from crawler.ai_analyzer import GameAnalyzer, AnalysisResult

    analyzer = GameAnalyzer(
        project_id="my-gcp-project",
        location="asia-northeast1",
    )
    result = analyzer.analyze("path/to/screenshot.png")
    print(result.screen_type)   # "ホーム画面"
    print(result.buttons)       # [{"name": "クエスト", "position": "下部タブ"}, ...]
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# データクラス
# ============================================================

@dataclass
class ButtonInfo:
    """クリック可能なボタン情報。"""
    name:        str           # ボタンのラベル (例: "クエストへ")
    position:    str           # 大まかな位置 (例: "画面下部タブ中央")
    priority:    int  = 1      # 重要度 1(高)〜3(低)
    description: str  = ""     # 補足説明


@dataclass
class AnalysisResult:
    """Vertex AI による画面解析結果。"""
    screen_type:   str                    # 画面種別 (例: "ホーム画面")
    buttons:       list[ButtonInfo]       # クリック可能なボタン一覧
    confidence:    float         = 1.0    # 判定信頼度 (0.0〜1.0)
    raw_response:  str           = ""     # Gemini の生レスポンス (デバッグ用)
    error:         Optional[str] = None   # エラーがあれば格納

    @property
    def is_ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "screen_type":  self.screen_type,
            "buttons":      [b.__dict__ for b in self.buttons],
            "confidence":   self.confidence,
            "error":        self.error,
        }


# ============================================================
# GameAnalyzer
# ============================================================

# Gemini に渡すプロンプト（日本語ゲームUI向け）
_ANALYSIS_PROMPT = """\
あなたはモバイルゲームのUI解析AIです。
添付のスクリーンショット画像を分析し、以下の JSON 形式で回答してください。
コードブロックや説明文は不要です。JSONのみ出力してください。

{
  "screen_type": "<画面の種類。例: タイトル画面 / ホーム画面 / クエスト選択画面 / バトル画面 / ガチャ画面 / ショップ画面 / ログイン画面 / イベント画面 / 設定画面 / ランキング画面 / その他>",
  "confidence": <0.0〜1.0 の判定信頼度>,
  "buttons": [
    {
      "name": "<ボタンのラベルテキスト>",
      "position": "<画面上の大まかな位置。例: 画面中央下部 / 左上 / 右下タブ / 画面中央>",
      "priority": <1=最重要(メイン導線) / 2=重要 / 3=補助>,
      "description": "<このボタンの推定機能>"
    }
  ]
}

注意:
- buttons は重要度の高い順に最大10件まで列挙してください
- テキストが日本語の場合はそのまま日本語で返してください
- 画像が不鮮明でも最善の判定を行ってください
"""


class GameAnalyzer:
    """
    Vertex AI Gemini を使ったゲーム画面解析クラス。

    Args:
        project_id: Google Cloud プロジェクト ID
        location:   Vertex AI リージョン (デフォルト: asia-northeast1)
        model:      使用モデル (デフォルト: gemini-1.5-flash-002)
    """

    DEFAULT_MODEL = "gemini-1.5-flash-002"

    def __init__(
        self,
        project_id:  str = "",
        location:    str = "asia-northeast1",
        model:       str = "",
    ) -> None:
        self.project_id = project_id or os.environ.get("GCP_PROJECT_ID", "")
        self.location   = location
        self.model      = model or self.DEFAULT_MODEL
        self._client    = None  # 遅延初期化

        if not self.project_id:
            raise ValueError(
                "GCP プロジェクト ID が設定されていません。\n"
                "引数 project_id か環境変数 GCP_PROJECT_ID を設定してください。"
            )

    # ----------------------------------------------------------
    # 公開 API
    # ----------------------------------------------------------

    def analyze(self, image_path: "str | Path") -> AnalysisResult:
        """
        画像ファイルを Vertex AI Gemini で解析する。

        Args:
            image_path: スクリーンショットのファイルパス (PNG/JPEG)

        Returns:
            AnalysisResult: 画面種別とボタン一覧を含む解析結果
        """
        path = Path(image_path)
        if not path.exists():
            return AnalysisResult(
                screen_type="不明",
                buttons=[],
                error=f"画像ファイルが見つかりません: {path}",
            )

        try:
            client = self._get_client()
            response_text = self._call_gemini(client, path)
            return self._parse_response(response_text)

        except Exception as e:
            logger.error(f"[GameAnalyzer] Vertex AI 呼び出し失敗: {e}")
            return AnalysisResult(
                screen_type="不明",
                buttons=[],
                error=str(e),
            )

    def analyze_bytes(self, image_bytes: bytes, mime_type: str = "image/png") -> AnalysisResult:
        """
        画像バイト列を直接渡して解析する（Appium スクリーンショット直送用）。

        Args:
            image_bytes: 画像のバイナリデータ
            mime_type:   "image/png" または "image/jpeg"

        Returns:
            AnalysisResult
        """
        try:
            client = self._get_client()
            response_text = self._call_gemini_bytes(client, image_bytes, mime_type)
            return self._parse_response(response_text)
        except Exception as e:
            logger.error(f"[GameAnalyzer] Vertex AI 呼び出し失敗: {e}")
            return AnalysisResult(
                screen_type="不明",
                buttons=[],
                error=str(e),
            )

    # ----------------------------------------------------------
    # 内部実装
    # ----------------------------------------------------------

    def _get_client(self):
        """Vertex AI クライアントを遅延初期化して返す。"""
        if self._client is None:
            import vertexai
            from vertexai.generative_models import GenerativeModel

            # 認証: ADC を自動参照 (gcloud auth application-default login 済みであること)
            # credentials 引数を渡さないことで google-auth ライブラリが ADC を自動検出する
            vertexai.init(project=self.project_id, location=self.location)
            self._client = GenerativeModel(self.model)
            logger.info(
                f"[GameAnalyzer] 初期化完了 | "
                f"project={self.project_id} | location={self.location} | model={self.model}"
            )
        return self._client

    def _call_gemini(self, client, image_path: Path) -> str:
        """ファイルパスから画像を読み込んで Gemini に送信する。"""
        from vertexai.generative_models import Part

        with open(image_path, "rb") as f:
            image_bytes = f.read()

        mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        return self._call_gemini_bytes(client, image_bytes, mime_type)

    def _call_gemini_bytes(self, client, image_bytes: bytes, mime_type: str) -> str:
        """バイト列で画像を Gemini に送信してテキストレスポンスを返す。"""
        from vertexai.generative_models import Part

        image_part = Part.from_data(data=image_bytes, mime_type=mime_type)
        response = client.generate_content(
            [image_part, _ANALYSIS_PROMPT],
            generation_config={
                "temperature":     0.1,   # 再現性重視
                "max_output_tokens": 1024,
                "response_mime_type": "application/json",
            },
        )
        return response.text

    def _parse_response(self, response_text: str) -> AnalysisResult:
        """Gemini のレスポンステキストを AnalysisResult に変換する。"""
        # JSON ブロックの抽出（```json ... ``` に囲まれている場合も対応）
        cleaned = re.sub(r"```(?:json)?\s*", "", response_text).strip().rstrip("`").strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"[GameAnalyzer] JSON パース失敗: {e}\nレスポンス: {response_text[:200]}")
            return AnalysisResult(
                screen_type="不明",
                buttons=[],
                raw_response=response_text,
                error=f"JSON パース失敗: {e}",
            )

        buttons = [
            ButtonInfo(
                name=        b.get("name", ""),
                position=    b.get("position", ""),
                priority=    int(b.get("priority", 2)),
                description= b.get("description", ""),
            )
            for b in data.get("buttons", [])
            if b.get("name")  # 名前なしは除外
        ]

        return AnalysisResult(
            screen_type=  data.get("screen_type", "不明"),
            buttons=      buttons,
            confidence=   float(data.get("confidence", 1.0)),
            raw_response= response_text,
        )


# ============================================================
# ユーティリティ: 環境変数から GameAnalyzer を構築
# ============================================================

def analyzer_from_env() -> GameAnalyzer:
    """
    環境変数から GameAnalyzer を構築する。

    必須環境変数:
        GCP_PROJECT_ID : Google Cloud プロジェクト ID

    任意環境変数:
        GCP_LOCATION   : Vertex AI リージョン (デフォルト: asia-northeast1)
        VERTEX_AI_MODEL: 使用モデル (デフォルト: gemini-1.5-flash-002)

    認証は ADC を自動使用 (gcloud auth application-default login 済みであること)。
    """
    return GameAnalyzer(
        project_id= os.environ.get("GCP_PROJECT_ID", ""),
        location=   os.environ.get("GCP_LOCATION", "asia-northeast1"),
        model=      os.environ.get("VERTEX_AI_MODEL", GameAnalyzer.DEFAULT_MODEL),
    )
