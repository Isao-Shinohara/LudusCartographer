"""
minimal_launch.py — 最小疎通確認スクリプト

【使い方】
iPhoneを繋いでUDIDとBundle IDを確認したら:

  export IOS_UDID="00008120-000A1234ABCD1234"
  export IOS_BUNDLE_ID="com.example.mygame"
  export IOS_DEVICE_NAME="iPhone 15 Pro"   # 任意
  export IOS_PLATFORM_VERSION="17.4"        # 任意

  # Appiumサーバーを別ターミナルで起動してから:
  appium --port 4723 &

  # このスクリプトを実行:
  cd crawler
  venv/bin/python appium/minimal_launch.py

【動作内容】(CLAUDE.md §7 最小単位検証)
  1. アプリを起動
  2. 3秒待機（描画完了まで）
  3. スクリーンショットを1枚撮影 → crawler/evidence/<session>/launch.png
  4. セッションを終了

【出力】
  - スクリーンショットのパスをターミナルに表示
  - OCR解析は次のステップ (test_ocr.py) で実行
"""
import logging
import os
import sys
from pathlib import Path

# プロジェクトルートを sys.path に追加
sys.path.insert(0, str(Path(__file__).parent.parent))

from appium_client.capabilities import ios_config_from_env  # noqa
from appium_client.driver import ios_session                 # noqa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    # --- 環境変数チェック ---
    try:
        cfg = ios_config_from_env()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    logger.info("=" * 50)
    logger.info("LudusCartographer — 最小疎通確認")
    logger.info(f"  UDID     : {cfg.udid}")
    logger.info(f"  Bundle ID: {cfg.bundle_id}")
    logger.info(f"  Appium   : http://{cfg.appium_host}:{cfg.appium_port}")
    logger.info("=" * 50)

    with ios_session(cfg) as d:
        logger.info("[STEP 1/3] アプリ起動完了")

        logger.info("[STEP 2/3] 3秒待機（描画完了まで）...")
        d.wait(3)

        logger.info("[STEP 3/3] スクリーンショット撮影...")
        shot_path = d.screenshot("launch")
        logger.info(f"[DONE] スクリーンショット保存: {shot_path}")
        logger.info("")
        logger.info("次のステップ: OCR解析")
        logger.info(f"  venv/bin/python appium/test_ocr_on_screenshot.py {shot_path}")


if __name__ == "__main__":
    main()
