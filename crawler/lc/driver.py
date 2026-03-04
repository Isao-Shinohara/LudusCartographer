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

    @property
    def screenshot_scale(self) -> tuple:
        """
        OCR ピクセル座標 → Appium 論理座標（ポイント）への変換スケール係数を返す。

        iOS @2x / @3x スクリーンショットを論理座標に変換するために使用する。
        スケールは初回アクセス時に計算してキャッシュする。

        Returns:
            (scale_x, scale_y): pixel / point の比率 (例: iPhone 16 = (3.0, 3.0))
        """
        if not hasattr(self, "_screenshot_scale"):
            import io
            from PIL import Image
            window = self._driver.get_window_size()
            png = self._driver.get_screenshot_as_png()
            img = Image.open(io.BytesIO(png))
            px_w, px_h = img.size
            sx = px_w / window["width"]
            sy = px_h / window["height"]
            self._screenshot_scale = (sx, sy)
            logger.info(f"[SCALE] screenshot={px_w}x{px_h}  window={window['width']}x{window['height']}"
                        f"  scale=({sx:.2f}, {sy:.2f})")
        return self._screenshot_scale

    def tap_coordinate(
        self,
        x: int,
        y: int,
        action_name: str = "ocr_tap",
        ocr_data: Optional[Dict] = None,
    ) -> None:
        """
        論理座標（ポイント）を直接タップする。
        CLAUDE.md §8 — XML要素取得不可時のフォールバック。

        Note: OCR で取得したピクセル座標をタップする場合は
              tap_ocr_coordinate() を使うこと（自動スケール変換）。
        """
        action_dir = self._evidence_dir / f"{datetime.now().strftime('%H%M%S')}_{action_name}"
        action_dir.mkdir(exist_ok=True)

        before_path = action_dir / "before.png"
        self._driver.save_screenshot(str(before_path))

        logger.info(f"[FALLBACK_OCR_TAP] tapping coordinate ({x}, {y})")

        # Android: adb input tap の方が Unity ゲームで確実に動作する
        if os.environ.get("DEVICE_MODE", "").upper() == "ANDROID":
            import subprocess as _sp
            udid = os.environ.get("ANDROID_UDID", "")
            cmd = (["adb", "-s", udid, "input", "tap", str(x), str(y)] if udid
                   else ["adb", "input", "tap", str(x), str(y)])
            _sp.run(cmd, check=False, timeout=10)
            logger.info(f"[ADB_TAP] adb input tap {x} {y}")
        else:
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

    def tap_ocr_coordinate(
        self,
        pixel_x: int,
        pixel_y: int,
        action_name: str = "ocr_tap",
        ocr_data: Optional[Dict] = None,
    ) -> None:
        """
        OCR が返すピクセル座標をデバイス論理座標（ポイント）に自動変換してタップする。

        iOS @2x/@3x のスクリーンショットから得た座標をそのまま渡せる。
        スケール係数は screenshot_scale プロパティで自動計算する。

        Args:
            pixel_x    : OCR バウンディングボックスのピクセル x 座標
            pixel_y    : OCR バウンディングボックスのピクセル y 座標
            action_name: 証拠ディレクトリ名に使用するアクション名
            ocr_data   : 証拠 JSON に記録する OCR データ
        """
        sx, sy = self.screenshot_scale
        pt_x = int(pixel_x / sx)
        pt_y = int(pixel_y / sy)
        logger.info(
            f"[OCR_TAP] pixel=({pixel_x},{pixel_y})"
            f" → point=({pt_x},{pt_y})"
            f" (scale={sx:.2f}×{sy:.2f})"
        )
        self.tap_coordinate(pt_x, pt_y, action_name, ocr_data)

    # ----------------------------------------------------------
    # ヘルパー
    # ----------------------------------------------------------

    def wait(self, seconds: float) -> None:
        """指定秒数待機する（ゲームの描画待ち用）。"""
        logger.debug(f"[WAIT] sleeping {seconds}s")
        time.sleep(seconds)

    def wait_until_stable(
        self,
        interval: float = 0.5,
        threshold: int = 5,
        timeout: float = 3.0,
    ) -> bool:
        """
        連続フレームの phash ハミング距離を監視し、画面が静止するまで待機する。

        `interval` 秒ごとにスクリーンショットを取得し、前フレームとの
        phash ハミング距離を計算する。距離が `threshold` 以下になった時点で
        「静止した」とみなして即座に return する。
        `timeout` 秒を超えた場合は静止未確認のまま強制 return して解析を続行する。

        ゲームの Live2D アニメーション・画面遷移エフェクト・ローディング演出など
        動的な UI が混在する環境で、無駄な固定待機を避けつつ安定後のスクリーンショットを
        保証するために使用する。

        Args:
            interval  : フレーム間のポーリング間隔 (秒, デフォルト 0.5)
            threshold : 静止とみなすハミング距離の上限 (デフォルト 5)
            timeout   : 最大待機時間 (秒, デフォルト 3.0)

        Returns:
            True  = 静止確認 (ハミング距離 ≤ threshold)
            False = タイムアウト強制終了
        """
        import cv2
        import numpy as np

        def _phash_from_png(png_bytes: bytes, hash_size: int = 8) -> str:
            arr = np.frombuffer(png_bytes, np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
            if img is None:
                raise ValueError("cv2.imdecode failed")
            img = cv2.resize(img, (hash_size * 4, hash_size * 4))
            dct = cv2.dct(np.float32(img))
            top = dct[:hash_size, :hash_size]
            avg = top.mean()
            bits = top.flatten() > avg
            return format(int("".join("1" if b else "0" for b in bits), 2), "016x")

        def _hamming(h1: str, h2: str) -> int:
            return bin(int(h1, 16) ^ int(h2, 16)).count("1")

        deadline = time.time() + timeout
        prev_hash: Optional[str] = None

        while True:
            try:
                png = self._driver.get_screenshot_as_png()
                cur_hash = _phash_from_png(png)
            except Exception as e:
                logger.debug(f"[STABLE] phash 計算失敗: {e}")
                cur_hash = None

            if prev_hash is not None and cur_hash is not None:
                dist = _hamming(prev_hash, cur_hash)
                logger.debug(f"[STABLE] phash_dist={dist}")
                if dist <= threshold:
                    logger.debug(f"[STABLE] 静止確認 (dist={dist} ≤ {threshold})")
                    return True

            prev_hash = cur_hash

            remaining = deadline - time.time()
            if remaining <= 0:
                logger.debug(f"[STABLE] タイムアウト ({timeout}s) — 強制続行")
                return False

            time.sleep(min(interval, remaining))

    def back(self) -> None:
        """OS の「戻る」操作を実行する（XCUITest: NavigationBar 戻るボタン相当）。"""
        try:
            self._driver.back()
            logger.info("[BACK] 戻る操作を実行")
        except Exception as e:
            logger.warning(f"[BACK] 戻る操作に失敗: {e}")

    def dismiss_any_modal(
        self,
        dismiss_keywords: Optional[list] = None,
        min_confidence: float = 0.6,
    ) -> bool:
        """
        OCR でモーダル/ダイアログの解除ボタンを検出してタップする。

        iOS では音声入力・通知許可・App Store アップデートなどの
        ダイアログが突然現れることがある。このメソッドは一般的な
        「今はしない」「キャンセル」などをタップして閉じる。

        Returns:
            True = 何かタップした, False = 解除対象なし
        """
        if dismiss_keywords is None:
            dismiss_keywords = ["今はしない", "キャンセル", "スキップ", "閉じる", "OK", "後で"]

        from .ocr import run_ocr, find_best
        shot = self.screenshot("modal_check")
        results = run_ocr(shot)
        for kw in dismiss_keywords:
            btn = find_best(results, kw, min_confidence=min_confidence)
            if btn:
                cx, cy = btn["center"]
                logger.info(f"[MODAL] 「{kw}」を検出 → タップして解除 pixel=({cx},{cy})")
                self.tap_ocr_coordinate(cx, cy, action_name="dismiss_modal")
                import time as _time
                _time.sleep(1.0)
                return True
        logger.debug("[MODAL] 解除対象のモーダルなし")
        return False

    def navigate_back_to_root(
        self,
        root_keyword: str = "設定",
        root_min_y: int = 200,
        root_max_y: int = 500,
        max_attempts: int = 5,
    ) -> bool:
        """
        root_keyword が「大タイトル領域」(root_min_y ≤ y < root_max_y) に現れるまで
        back() を繰り返す。

        iOS のナビゲーション構造:
          - ナビゲーションバー戻るボタン: y < 200px（小さい、画面最上部）
          - 大タイトル (Large Title): y ≥ 200px（大きい、コンテンツ直上）
        root_min_y=200 を指定することで、戻るボタンの「設定」を
        ルート画面の「設定」タイトルと誤判定しなくなる。

        Args:
            root_keyword : ルート画面を示すキーワード (例: "設定")
            root_min_y   : ルートタイトルの y 座標の下限 (nav bar 除外)
            root_max_y   : ルートタイトルの y 座標の上限
            max_attempts : 最大戻り回数

        Returns:
            True = ルート画面に到達, False = 到達できなかった
        """
        from .ocr import run_ocr, find_best

        for attempt in range(max_attempts):
            shot = self.screenshot(f"back_check_{attempt}")
            results = run_ocr(shot)
            title = find_best(results, root_keyword)
            if title and root_min_y <= title["center"][1] < root_max_y:
                logger.info(f"[NAVIGATE] ルート画面を確認 ({attempt} 回 back) y={title['center'][1]}")
                return True
            y_info = f"y={title['center'][1]}" if title else "未検出"
            logger.info(f"[NAVIGATE] back #{attempt + 1}: 「{root_keyword}」{y_info} → 戻る")
            self.back()
            time.sleep(1.0)

        logger.warning(f"[NAVIGATE] {max_attempts} 回戻っても「{root_keyword}」が見つかりませんでした")
        return False

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
