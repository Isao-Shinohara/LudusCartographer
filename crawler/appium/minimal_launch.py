"""
minimal_launch.py — 最小疎通確認スクリプト（Vertex AI + MySQL 統合版）

【使い方】
  # IOS_UDID は省略可能 — 接続中の iPhone を自動検出する
  # (検出順: 環境変数 → idevice_id -l → ioreg USB Serial)
  export IOS_BUNDLE_ID="com.example.mygame"

  # Vertex AI 解析（任意 — 未設定時はスキップ）
  # 認証は ADC を自動使用 (gcloud auth application-default login 済みであること)
  export GCP_PROJECT_ID="my-gcp-project"

  # MySQL 保存（任意 — 未設定時はスキップ）
  export DB_HOST=localhost
  export DB_NAME=ludus_cartographer
  export DB_USER=root
  export DB_PASSWORD=secret

  # Appiumサーバー起動後に実行
  appium --port 4723 &
  cd crawler && venv/bin/python appium/minimal_launch.py

【フロー】(CLAUDE.md §7 最小単位検証)
  1. アプリを起動
  2. 3秒待機（描画完了まで）
  3. スクリーンショットを1枚撮影 → crawler/evidence/<session>/launch.png
  4. [任意] Vertex AI Gemini で画面解析 → 画面名・ボタン一覧をログ出力
  5. [任意] MySQL screens テーブルに保存
  6. セッションを終了

【次のステップ】
  スクリーンショットを確認後、以下で OCR 解析:
    venv/bin/python tests/test_ocr.py <スクリーンショットパス>
"""
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# プロジェクトルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.capabilities import ios_config_from_env
from lc.driver import ios_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ============================================================
# Appium サーバー自動ライフサイクル管理
# ============================================================

def ensure_appium_running(
    host: str = "127.0.0.1",
    port: int = 4723,
    wait_sec: int = 30,
) -> None:
    """
    Appium サーバーが起動していない場合、バックグラウンドで自動起動して待機する。

    Args:
        host:     Appium ホスト (デフォルト: 127.0.0.1)
        port:     Appium ポート (デフォルト: 4723)
        wait_sec: 起動完了まで待機する最大秒数 (デフォルト: 30)

    Raises:
        RuntimeError: 待機タイムアウトした場合
    """
    def _is_up() -> bool:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    if _is_up():
        logger.info(f"[APPIUM] 起動中を確認: http://{host}:{port}")
        return

    logger.info(f"[APPIUM] 未起動 → バックグラウンドで起動: appium --port {port}")
    proc = subprocess.Popen(
        ["appium", "--port", str(port), "--log", "/tmp/appium.log", "--log-timestamp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(f"[APPIUM] PID {proc.pid} で起動中 ... (最大 {wait_sec}秒待機)")

    start = time.time()
    while time.time() - start < wait_sec:
        if _is_up():
            elapsed = time.time() - start
            logger.info(f"[APPIUM] 起動完了 (PID {proc.pid}, {elapsed:.1f}秒)")
            return
        time.sleep(1)

    raise RuntimeError(
        f"Appium が {wait_sec}秒以内に起動しませんでした。\n"
        f"  ログ確認: cat /tmp/appium.log\n"
        f"  Node.js v18 が有効か確認: node --version\n"
        f"  (nodebrew use v18.20.8 で切り替え)"
    )


# ============================================================
# MySQL 保存
# ============================================================

def save_screen_to_db(
    game_id:       int,
    screen_hash:   str,
    screen_name:   str,
    category:      str,
    screenshot_path: str,
    ocr_text:      str = "",
) -> bool:
    """
    screens テーブルに画面情報を保存する（重複ハッシュは visited_count をインクリメント）。
    MySQL が未接続の場合は False を返してスキップする。
    """
    try:
        import pymysql

        cfg = {
            "host":     os.environ.get("DB_HOST", "localhost"),
            "port":     int(os.environ.get("DB_PORT", 3306)),
            "db":       os.environ.get("DB_NAME", "ludus_cartographer"),
            "user":     os.environ.get("DB_USER", "root"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "charset":  "utf8mb4",
            "cursorclass": pymysql.cursors.DictCursor,
        }
        conn = pymysql.connect(**cfg)
        try:
            with conn.cursor() as cur:
                # UPSERT: 同じハッシュがあれば visited_count++ と last_seen_at 更新
                sql = """
                    INSERT INTO screens
                        (game_id, screen_hash, name, category, screenshot_path, ocr_text)
                    VALUES
                        (%s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        visited_count = visited_count + 1,
                        last_seen_at  = NOW(),
                        name          = IF(name IS NULL OR name = '', VALUES(name), name)
                """
                cur.execute(sql, (game_id, screen_hash, screen_name, category, screenshot_path, ocr_text))
            conn.commit()
            logger.info(f"[DB] screens に保存: hash={screen_hash[:8]}… name={screen_name!r}")
            return True
        finally:
            conn.close()

    except Exception as e:
        logger.warning(f"[DB] 保存スキップ（MySQL未接続または設定なし）: {e}")
        return False


# ============================================================
# 画像ハッシュ生成
# ============================================================

def compute_image_hash(image_path: Path) -> str:
    """スクリーンショットの SHA-256 ハッシュを返す（重複検出用）。"""
    sha256 = hashlib.sha256()
    with open(image_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()[:32]  # 32文字に短縮


# ============================================================
# Vertex AI 解析
# ============================================================

def analyze_with_vertex_ai(screenshot_path: Path) -> dict:
    """
    Vertex AI GameAnalyzer でスクリーンショットを解析する。
    GCP_PROJECT_ID が未設定の場合はスキップして空の結果を返す。
    """
    project_id = os.environ.get("GCP_PROJECT_ID", "")
    if not project_id:
        logger.info("[AI] GCP_PROJECT_ID 未設定 → Vertex AI 解析をスキップ")
        return {"screen_type": "", "buttons": [], "confidence": 0.0, "skipped": True}

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from ai_analyzer import analyzer_from_env

        analyzer = analyzer_from_env()
        logger.info(f"[AI] Vertex AI 解析中: {screenshot_path.name} ...")
        result = analyzer.analyze(screenshot_path)

        if result.is_ok:
            logger.info(f"[AI] ┌─ 画面種別  : {result.screen_type} (信頼度: {result.confidence:.0%})")
            for i, btn in enumerate(result.buttons[:5], 1):
                logger.info(f"[AI] │  ボタン{i:02d} : [{btn.position}] {btn.name!r} — {btn.description}")
            if not result.buttons:
                logger.info("[AI] └─ ボタン    : (検出なし)")
            else:
                logger.info("[AI] └─ (以上)")
        else:
            logger.warning(f"[AI] 解析エラー: {result.error}")

        return result.to_dict()

    except Exception as e:
        logger.warning(f"[AI] Vertex AI 解析失敗: {e}")
        return {"screen_type": "", "buttons": [], "confidence": 0.0, "error": str(e)}


# ============================================================
# メイン
# ============================================================

def main() -> None:
    # --- .env ロード（存在する場合）---
    env_path = Path(__file__).parent.parent / "config" / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)

    # --- Appium サーバー自動起動 ---
    appium_host = os.environ.get("APPIUM_HOST", "127.0.0.1")
    appium_port = int(os.environ.get("APPIUM_PORT", "4723"))
    try:
        ensure_appium_running(host=appium_host, port=appium_port)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # --- デバイス設定（UDID は自動検出） ---
    try:
        cfg = ios_config_from_env()
    except (ValueError, RuntimeError) as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("LudusCartographer — 最小疎通確認 (Vertex AI + MySQL 統合版)")
    logger.info(f"  UDID     : {cfg.udid}")
    logger.info(f"  Bundle ID: {cfg.bundle_id}")
    logger.info(f"  Appium   : http://{cfg.appium_host}:{cfg.appium_port}")
    logger.info(f"  GCP      : {os.environ.get('GCP_PROJECT_ID', '(未設定 — AI解析スキップ)')} [ADC認証]")
    logger.info(f"  MySQL    : {os.environ.get('DB_HOST', 'localhost')}/{os.environ.get('DB_NAME', 'ludus_cartographer')}")
    logger.info("=" * 60)

    with ios_session(cfg) as d:
        # --------------------------------------------------
        # STEP 1: 起動確認
        # --------------------------------------------------
        logger.info("[STEP 1/5] アプリ起動完了")

        # --------------------------------------------------
        # STEP 2: 描画待ち
        # --------------------------------------------------
        logger.info("[STEP 2/5] 3秒待機（描画完了まで）...")
        d.wait(3)

        # --------------------------------------------------
        # STEP 3: スクリーンショット撮影
        # --------------------------------------------------
        logger.info("[STEP 3/5] スクリーンショット撮影...")
        shot_path = d.screenshot("launch")
        logger.info(f"[STEP 3/5] 保存: {shot_path}")

        # --------------------------------------------------
        # STEP 4: Vertex AI 解析
        # --------------------------------------------------
        logger.info("[STEP 4/5] Vertex AI による画面解析...")
        ai_result = analyze_with_vertex_ai(shot_path)

        # --------------------------------------------------
        # STEP 5: MySQL に保存
        # --------------------------------------------------
        logger.info("[STEP 5/5] MySQL への保存...")

        screen_hash = compute_image_hash(shot_path)
        screen_name = ai_result.get("screen_type") or "未分類"
        # screen_type から category を推定
        category_map = {
            "タイトル": "title", "ホーム": "home", "クエスト": "quest",
            "バトル": "battle", "ショップ": "shop", "ガチャ": "gacha",
            "ランキング": "ranking", "設定": "settings", "ログイン": "login",
            "イベント": "event",
        }
        category = next(
            (v for k, v in category_map.items() if k in screen_name),
            "unknown",
        )

        saved = save_screen_to_db(
            game_id=         1,  # games テーブルのサンプルデータ ID
            screen_hash=     screen_hash,
            screen_name=     screen_name,
            category=        category,
            screenshot_path= str(shot_path),
            ocr_text=        "",  # OCRは別ステップで実施
        )

        # --------------------------------------------------
        # 完了サマリー
        # --------------------------------------------------
        logger.info("")
        logger.info("=" * 60)
        logger.info("  疎通確認 完了サマリー")
        logger.info("=" * 60)
        logger.info(f"  スクリーンショット : {shot_path}")
        logger.info(f"  画面種別 (AI判定)  : {ai_result.get('screen_type', '(スキップ)')}")
        logger.info(f"  検出ボタン数       : {len(ai_result.get('buttons', []))} 件")
        logger.info(f"  スクリーンハッシュ : {screen_hash}")
        logger.info(f"  MySQL 保存         : {'✅ 成功' if saved else '⏭  スキップ（DB未接続）'}")
        logger.info("=" * 60)
        logger.info("")
        logger.info("  ユーザー確認をお願いします:")
        logger.info(f"  1. 上記スクリーンショットを目視確認")
        logger.info(f"  2. AI判定が正しいか確認")
        logger.info(f"  3. 問題なければ次のステップへ (OCR解析):")
        logger.info(f"     venv/bin/python tests/test_ocr.py {shot_path}")
        logger.info("")


if __name__ == "__main__":
    main()
