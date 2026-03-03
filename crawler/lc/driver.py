"""
driver.py — Appium WebDriver セッション管理

実機検証フェーズで使う「堅牢化された」Appiumドライバーラッパー。
CLAUDE.md §8 のリトライ戦略・OCRフォールバックを実装する。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, Optional

from appium import webdriver
from appium.webdriver.common.appiumby import AppiumBy
from selenium.common.exceptions import NoSuchElementException, WebDriverException

from .capabilities import (
    iOSDeviceConfig, AndroidDeviceConfig,
    iOSSimulatorConfig,
    build_ios_capabilities, build_android_capabilities, build_ios_simulator_capabilities,
)

logger = logging.getLogger(__name__)

# 証拠保存のルートディレクトリ
EVIDENCE_ROOT = Path(__file__).parent.parent / "evidence"


class AppiumDriver:
    """
    Appium ドライバーラッパー。

    主な機能:
    - リトライ付き要素検索 (最大3回、1秒間隔)
    - アクション前後のスクリーンショット保存
    - PaddleOCR フォールバックタップ (OCR実装後に接続)
    - 証拠ログ (before.png / after.png / ocr_result.json)
    """

    def __init__(self, driver: webdriver.Remote, session_id: str = ""):
        self._driver = driver
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
        self._evidence_dir = EVIDENCE_ROOT / self.session_id
        self._evidence_dir.mkdir(parents=True, exist_ok=True)

    @property
    def driver(self) -> webdriver.Remote:
        return self._driver

    # ----------------------------------------------------------
    # スクリーンショット
    # ----------------------------------------------------------

    def screenshot(self, name: str = "") -> Path:
        """スクリーンショットを撮って evidence/ 以下に保存し、パスを返す。"""
        ts = datetime.now().strftime("%H%M%S_%f")
        filename = f"{ts}_{name}.png" if name else f"{ts}.png"
        path = self._evidence_dir / filename
        self._driver.save_screenshot(str(path))
        logger.info(f"[SCREENSHOT] saved: {path}")
        return path

    # ----------------------------------------------------------
    # リトライ付き要素検索 (CLAUDE.md §8)
    # ----------------------------------------------------------

    def find_element(
        self,
        by: str,
        value: str,
        retries: int = 3,
        interval: float = 1.0,
    ):
        """
        最大 `retries` 回リトライしながら要素を検索する。
        見つからない場合は None を返す（例外を投げない）。
        """
        for attempt in range(retries):
            try:
                el = self._driver.find_element(by, value)
                logger.debug(f"[FIND_ELEMENT] found '{value}' on attempt {attempt + 1}")
                return el
            except (NoSuchElementException, WebDriverException):
                if attempt < retries - 1:
                    logger.debug(
                        f"[FIND_ELEMENT] '{value}' not found (attempt {attempt + 1}/{retries}), "
                        f"retrying in {interval}s..."
                    )
                    time.sleep(interval)
        logger.warning(f"[FIND_ELEMENT] '{value}' not found after {retries} attempts")
        return None

    # ----------------------------------------------------------
    # タップ（証拠記録付き）
    # ----------------------------------------------------------

    def tap_element(self, by: str, value: str, action_name: str = "") -> bool:
        """
        要素を検索してタップする。
        アクション前後のスクリーンショットと証拠JSONを保存する。
        """
        action_dir = self._evidence_dir / f"{datetime.now().strftime('%H%M%S')}_{action_name or 'tap'}"
        action_dir.mkdir(exist_ok=True)

        # Before スクリーンショット
        before_path = action_dir / "before.png"
        self._driver.save_screenshot(str(before_path))

        el = self.find_element(by, value)
        if el is None:
            logger.warning(f"[TAP_ELEMENT] element not found, consider OCR fallback: '{value}'")
            self._save_evidence_json(action_dir, {
                "action": "tap",
                "target": value,
                "result": "element_not_found",
                "fallback": "ocr_tap_required",
            })
            return False

        try:
            el.click()
            time.sleep(0.5)  # タップ後の描画待ち

            # After スクリーンショット
            after_path = action_dir / "after.png"
            self._driver.save_screenshot(str(after_path))

            self._save_evidence_json(action_dir, {
                "action": "tap",
                "target": value,
                "result": "success",
            })
            logger.info(f"[TAP_ELEMENT] tapped '{value}'")
            return True

        except WebDriverException as e:
            logger.error(f"[TAP_ELEMENT] error tapping '{value}': {e}")
            return False

    def tap_coordinate(
        self,
        x: int,
        y: int,
        action_name: str = "ocr_tap",
        ocr_data: Optional[Dict] = None,
    ) -> None:
        """
        OCR フォールバック: 座標を直接タップする。
        CLAUDE.md §8 — XML要素取得不可時のフォールバック。
        """
        action_dir = self._evidence_dir / f"{datetime.now().strftime('%H%M%S')}_{action_name}"
        action_dir.mkdir(exist_ok=True)

        before_path = action_dir / "before.png"
        self._driver.save_screenshot(str(before_path))

        logger.info(f"[FALLBACK_OCR_TAP] tapping coordinate ({x}, {y})")
        self._driver.tap([(x, y)])
        time.sleep(0.5)

        after_path = action_dir / "after.png"
        self._driver.save_screenshot(str(after_path))

        evidence = {
            "action": "coordinate_tap",
            "x": x,
            "y": y,
            "result": "success",
        }
        if ocr_data:
            evidence["ocr_boxes"] = ocr_data.get("ocr_boxes", [])

        self._save_evidence_json(action_dir, evidence)

    # ----------------------------------------------------------
    # ヘルパー
    # ----------------------------------------------------------

    def wait(self, seconds: float) -> None:
        """指定秒数待機する（ゲームの描画待ち用）。"""
        logger.debug(f"[WAIT] sleeping {seconds}s")
        time.sleep(seconds)

    def _save_evidence_json(self, directory: Path, data: dict) -> None:
        """証拠 JSON を保存する。"""
        data["timestamp"] = datetime.now().isoformat()
        path = directory / "ocr_result.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def quit(self) -> None:
        """セッションを終了する。"""
        try:
            self._driver.quit()
            logger.info(f"[SESSION] quit: {self.session_id}")
        except Exception:
            pass


# ============================================================
# コンテキストマネージャ: セッション管理
# ============================================================

@contextmanager
def ios_session(cfg: iOSDeviceConfig) -> Generator[AppiumDriver, None, None]:
    """
    iOS Appium セッションをコンテキストマネージャとして提供する。

    使用例:
        with ios_session(cfg) as d:
            d.screenshot("launch")
            d.wait(3)
    """
    appium_url = f"http://{cfg.appium_host}:{cfg.appium_port}"
    caps = build_ios_capabilities(cfg)

    logger.info(f"[SESSION] connecting to {appium_url} | UDID: {cfg.udid}")
    raw_driver = webdriver.Remote(appium_url, options=_make_options(caps))
    driver = AppiumDriver(raw_driver)
    try:
        yield driver
    finally:
        driver.quit()


@contextmanager
def ios_simulator_session(cfg: iOSSimulatorConfig) -> Generator[AppiumDriver, None, None]:
    """
    iOS シミュレータ Appium セッションをコンテキストマネージャとして提供する。

    使用例:
        with ios_simulator_session(cfg) as d:
            d.screenshot("launch")
            d.wait(3)
    """
    appium_url = f"http://{cfg.appium_host}:{cfg.appium_port}"
    caps = build_ios_simulator_capabilities(cfg)

    logger.info(f"[SESSION] connecting to {appium_url} | Simulator UDID: {cfg.udid}")
    raw_driver = webdriver.Remote(appium_url, options=_make_options(caps))
    driver = AppiumDriver(raw_driver)
    try:
        yield driver
    finally:
        driver.quit()


@contextmanager
def android_session(cfg: AndroidDeviceConfig) -> Generator[AppiumDriver, None, None]:
    """Android Appium セッションをコンテキストマネージャとして提供する。"""
    appium_url = f"http://{cfg.appium_host}:{cfg.appium_port}"
    caps = build_android_capabilities(cfg)

    logger.info(f"[SESSION] connecting to {appium_url} | UDID: {cfg.udid}")
    raw_driver = webdriver.Remote(appium_url, options=_make_options(caps))
    driver = AppiumDriver(raw_driver)
    try:
        yield driver
    finally:
        driver.quit()


def _make_options(caps: dict):
    """Capabilities dict を Appium Options オブジェクトに変換する。"""
    from appium.options.common import AppiumOptions
    options = AppiumOptions()
    for k, v in caps.items():
        options.set_capability(k, v)
    return options
