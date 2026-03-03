"""
driver_adapter.py — 画面取得・タップ操作の抽象化層

シミュレータ (Appium) とミラーリング (UxPlay / scrcpy) の
両方に対応する Driver インターフェースを定義する。

【クラス構成】
  BaseDriver     — 抽象基底クラス
  SimulatorDriver — iOS シミュレータ用 (AppiumDriver ラッパー)
  MirroringDriver — 実機ミラーリング用 (ウィンドウキャプチャ + Appium Wi-Fi)

【使い方】
  from driver_factory import create_driver_session
  from lc.crawler import ScreenCrawler, CrawlerConfig

  with create_driver_session() as driver:
      print(driver.is_simulator())          # True / False
      img = driver.get_screenshot()         # numpy.ndarray (BGR)
      driver.tap(100, 200)                  # 論理座標タップ
      crawler = ScreenCrawler(driver, CrawlerConfig())
      crawler.crawl()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from lc.driver import AppiumDriver


# ============================================================
# 抽象基底クラス
# ============================================================

class BaseDriver(ABC):
    """
    Driver の抽象基底クラス。

    具象クラス:
      SimulatorDriver  — Appium (iOS シミュレータ) ラッパー
      MirroringDriver  — UxPlay / scrcpy ウィンドウキャプチャ + Appium (Wi-Fi)

    ScreenCrawler はこの型を受け取り、各メソッドを呼び出す。
    get_screenshot / tap / is_simulator の 3 メソッドが最小契約。
    その他の AppiumDriver メソッドは具象クラスの __getattr__ 委譲で利用可能。
    """

    @abstractmethod
    def get_screenshot(self) -> np.ndarray:
        """
        現在の画面を OpenCV 形式 (BGR, HxWx3) の numpy.ndarray で返す。

        - SimulatorDriver: Appium スクリーンショットを PNG → ndarray 変換
        - MirroringDriver: ミラーリングウィンドウをキャプチャして返す
        """

    @abstractmethod
    def tap(self, x: int, y: int) -> None:
        """
        デバイス論理座標 (x, y) をタップする。

        座標系はデバイスの論理ピクセル (ポイント) を使用すること。
        iPhone 16 の場合: 横 0–392、縦 0–851 (pt 単位)

        注意: OCR が返すピクセル座標を渡す場合は
              tap_ocr_coordinate() を使うこと (@3x → pt に自動変換される)。
        """

    @abstractmethod
    def is_simulator(self) -> bool:
        """
        シミュレータ環境の場合 True、実機ミラーリングの場合 False を返す。
        """


# ============================================================
# SimulatorDriver — iOS シミュレータ向け
# ============================================================

class SimulatorDriver(BaseDriver):
    """
    iOS シミュレータ向け Driver。

    AppiumDriver を内部に保持し、BaseDriver インターフェースを実装する。
    定義されていないメソッド・属性は __getattr__ で AppiumDriver へ
    透過的に委譲するため、既存の ScreenCrawler コードは変更なしで動作する。

    委譲される主なメソッド:
      screenshot()           — evidence/ に PNG 保存して Path を返す
      tap_ocr_coordinate()   — OCR ピクセル座標 → 論理座標変換タップ
      tap_coordinate()       — 論理座標タップ (tap() の委譲先)
      wait_until_stable()    — phash 監視による画面静止待機
      wait()                 — スリープ
      back()                 — OS 戻る操作
      dismiss_any_modal()    — OCR によるモーダル解除
      session_id             — セッション ID 文字列
      driver                 — 生 Appium WebDriver (save_screenshot 等)
      _evidence_dir          — evidence ディレクトリ Path
    """

    def __init__(self, appium_driver: "AppiumDriver") -> None:
        # __setattr__ を避けて直接セット (後の __getattr__ 再帰防止)
        object.__setattr__(self, "_appium", appium_driver)

    # ----------------------------------------------------------
    # BaseDriver 抽象メソッド実装
    # ----------------------------------------------------------

    def get_screenshot(self) -> np.ndarray:
        """
        Appium のスクリーンショットを numpy.ndarray (BGR) で返す。

        AppiumDriver.screenshot() でファイル保存し、
        cv2.imread() で読み込んで返す。
        """
        import cv2
        path = self._appium.screenshot("_adapter")
        img = cv2.imread(str(path))
        if img is None:
            raise RuntimeError(
                f"スクリーンショットの読み込みに失敗しました: {path}"
            )
        return img

    def tap(self, x: int, y: int) -> None:
        """論理座標 (x, y) をタップする (AppiumDriver.tap_coordinate に委譲)。"""
        self._appium.tap_coordinate(x, y)

    def is_simulator(self) -> bool:
        return True

    # ----------------------------------------------------------
    # 後方互換: AppiumDriver の全メソッド・属性を透過委譲
    # ----------------------------------------------------------

    def __getattr__(self, name: str):
        """
        定義されていない属性・メソッドを AppiumDriver へ委譲する。

        これにより ScreenCrawler が呼び出す以下のメソッドがすべて動作する:
          driver.screenshot(), driver.tap_ocr_coordinate(),
          driver.wait_until_stable(), driver.back(), driver.session_id,
          driver.driver (生 Appium WebDriver), driver._evidence_dir など。
        """
        appium = object.__getattribute__(self, "_appium")
        return getattr(appium, name)


# ============================================================
# MirroringDriver — 実機ミラーリング向け
# ============================================================

class MirroringDriver(BaseDriver):
    """
    実機ミラーリング向け Driver。

    画面取得: UxPlay (iOS) または scrcpy (Android) のウィンドウを
              OpenCV でキャプチャして返す。
    操作    : Appium (Network / Wi-Fi 経由) でタップ。
              ウィンドウ座標からデバイス論理座標への変換を自動実施。

    環境変数:
      MIRROR_WINDOW_TITLE   : キャプチャ対象ウィンドウのタイトル (部分一致)
                              例: "UxPlay" / "iPhone" / "scrcpy"
                              省略時は既定候補を順番に検索
      MIRROR_DEVICE_WIDTH   : デバイス論理幅 pt (デフォルト 393 — iPhone 16)
      MIRROR_DEVICE_HEIGHT  : デバイス論理高さ pt (デフォルト 852 — iPhone 16)
    """

    # キャプチャ対象ウィンドウのタイトル検索候補 (優先順)
    _WINDOW_TITLE_CANDIDATES: tuple[str, ...] = ("UxPlay", "iPhone", "scrcpy")

    def __init__(
        self,
        appium_driver: "AppiumDriver",
        window_title: str = "",
        device_logical_width: int = 393,
        device_logical_height: int = 852,
    ) -> None:
        self._appium         = appium_driver
        self._window_title   = window_title
        self._device_width   = device_logical_width
        self._device_height  = device_logical_height
        # (x, y, width, height) — 初回 get_screenshot() 時に確定
        self._window_rect: Optional[tuple[int, int, int, int]] = None

    # ----------------------------------------------------------
    # BaseDriver 抽象メソッド実装
    # ----------------------------------------------------------

    def get_screenshot(self) -> np.ndarray:
        """
        UxPlay / scrcpy ウィンドウをキャプチャして BGR numpy.ndarray を返す。

        初回呼び出しでウィンドウを検索し、以降はキャッシュした領域を使用する。
        ウィンドウが見つからない場合は RuntimeError を送出する。
        """
        from tools.window_manager import capture_region, find_mirroring_window

        if self._window_rect is None:
            candidates = (
                [self._window_title] if self._window_title
                else list(self._WINDOW_TITLE_CANDIDATES)
            )
            self._window_rect = find_mirroring_window(candidates)
            if self._window_rect is None:
                raise RuntimeError(
                    "ミラーリングウィンドウが見つかりません。"
                    " UxPlay または scrcpy が起動しているか確認してください。"
                    f" 検索タイトル: {candidates}"
                )

        return capture_region(self._window_rect)

    def tap(self, x: int, y: int) -> None:
        """
        ウィンドウ座標 (x, y) をデバイス論理座標に変換してタップする。

        Args:
            x, y: ウィンドウキャプチャ画像上のピクセル座標
        """
        if self._window_rect is None:
            self.get_screenshot()  # ウィンドウ検索を兼ねる

        _, _, win_w, win_h = self._window_rect  # type: ignore[misc]
        pt_x = int(x * self._device_width  / win_w)
        pt_y = int(y * self._device_height / win_h)
        self._appium.tap_coordinate(pt_x, pt_y)

    def is_simulator(self) -> bool:
        return False

    # ----------------------------------------------------------
    # 後方互換: AppiumDriver の全メソッド・属性を透過委譲
    # ----------------------------------------------------------

    def __getattr__(self, name: str):
        """AppiumDriver への透過委譲。"""
        return getattr(self._appium, name)
