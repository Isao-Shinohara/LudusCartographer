"""
driver_adapter.py — 画面取得・タップ操作の抽象化層

シミュレータ (Appium) とミラーリング (UxPlay / scrcpy) の
両方に対応する Driver インターフェースを定義する。

【クラス構成】
  BaseDriver.WindowNotFoundError — ウィンドウ消失例外
  BaseDriver       — 抽象基底クラス
  SimulatorDriver  — iOS シミュレータ用 (AppiumDriver ラッパー)
  MirroringDriver  — 実機ミラーリング用 (ウィンドウキャプチャ + Appium Wi-Fi)

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

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path
    from lc.driver import AppiumDriver

logger = logging.getLogger(__name__)


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

    class WindowNotFoundError(RuntimeError):
        """
        ミラーリングウィンドウが見失われた、または閉じられた場合に送出される。

        main.py はこの例外を捕捉し、クロール中断 + 現時点のサマリーを保存する。
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

    解像度チェック:
      キャプチャ結果が MIN_CAPTURE_WIDTH × MIN_CAPTURE_HEIGHT px 未満の場合、
      警告ログを出力してデバイス論理解像度にアップスケールする。
      (UxPlay ウィンドウが小さすぎると OCR 精度が低下するため)

    ウィンドウ消失検知:
      screenshot() 呼び出し VERIFY_INTERVAL 回に 1 回、ウィンドウ存在を確認する。
      ウィンドウが消失していた場合は WindowNotFoundError を送出する。

    環境変数:
      MIRROR_WINDOW_TITLE   : キャプチャ対象ウィンドウのタイトル (部分一致)
                              例: "UxPlay" / "iPhone" / "scrcpy"
                              省略時は既定候補を順番に検索
      MIRROR_DEVICE_WIDTH   : デバイス論理幅 pt (デフォルト 393 — iPhone 16)
      MIRROR_DEVICE_HEIGHT  : デバイス論理高さ pt (デフォルト 852 — iPhone 16)
    """

    # キャプチャ対象ウィンドウのタイトル検索候補 (優先順)
    _WINDOW_TITLE_CANDIDATES: tuple[str, ...] = ("UxPlay", "iPhone", "scrcpy")

    # 解像度チェック閾値 — これ未満は OCR 精度が著しく低下する
    MIN_CAPTURE_WIDTH:  int = 300
    MIN_CAPTURE_HEIGHT: int = 600

    # ウィンドウ存在確認の頻度 (screenshot() 呼び出し N 回に 1 回)
    VERIFY_INTERVAL: int = 5

    def __init__(
        self,
        appium_driver: "AppiumDriver",
        window_title: str = "",
        device_logical_width: int = 393,
        device_logical_height: int = 852,
    ) -> None:
        self._appium          = appium_driver
        self._window_title    = window_title
        self._device_width    = device_logical_width
        self._device_height   = device_logical_height
        # (x, y, width, height) — 初回 get_screenshot() 時に確定
        self._window_rect: Optional[tuple[int, int, int, int]] = None
        # screenshot() 呼び出し回数カウンタ（ウィンドウ検証用）
        self._screenshot_count: int = 0

    # ----------------------------------------------------------
    # BaseDriver 抽象メソッド実装
    # ----------------------------------------------------------

    def get_screenshot(self) -> np.ndarray:
        """
        UxPlay / scrcpy ウィンドウをキャプチャして BGR numpy.ndarray を返す。

        初回呼び出しでウィンドウを検索・前面表示し、以降はキャッシュを使用する。
        キャプチャ解像度が低い場合はログ警告 + デバイス論理サイズにリサイズする。

        Raises:
            WindowNotFoundError: ウィンドウが見つからない / 消失した場合
        """
        import cv2
        from tools.window_manager import bring_window_to_front, capture_region, find_mirroring_window

        candidates = (
            [self._window_title] if self._window_title
            else list(self._WINDOW_TITLE_CANDIDATES)
        )

        # 初回: ウィンドウ検索 + 前面表示
        if self._window_rect is None:
            self._window_rect = find_mirroring_window(candidates)
            if self._window_rect is None:
                raise BaseDriver.WindowNotFoundError(
                    "ミラーリングウィンドウが見つかりません。"
                    " UxPlay または scrcpy が起動しているか確認してください。"
                    f" 検索タイトル: {candidates}"
                )
            brought = bring_window_to_front(candidates)
            logger.info(
                f"[MIRROR] ウィンドウ発見 rect={self._window_rect}"
                f" 前面表示={'成功' if brought else 'スキップ'}"
            )

        # キャプチャ (失敗時は再検索して1回リトライ)
        try:
            img = capture_region(self._window_rect)
        except Exception as e:
            logger.warning(f"[MIRROR] キャプチャ失敗、ウィンドウを再検索します: {e}")
            self._window_rect = find_mirroring_window(candidates)
            if self._window_rect is None:
                raise BaseDriver.WindowNotFoundError(
                    "ミラーリングウィンドウが消失しました。"
                    " UxPlay または scrcpy が終了した可能性があります。"
                ) from e
            bring_window_to_front(candidates)
            img = capture_region(self._window_rect)

        # 解像度チェック: 低すぎる場合は警告 + リサイズ
        h, w = img.shape[:2]
        if w < self.MIN_CAPTURE_WIDTH or h < self.MIN_CAPTURE_HEIGHT:
            logger.warning(
                f"[MIRROR] キャプチャ解像度が低すぎます: {w}×{h}px"
                f" (推奨: {self.MIN_CAPTURE_WIDTH}×{self.MIN_CAPTURE_HEIGHT}px 以上)。"
                " OCR 精度が低下する可能性があります。"
                " UxPlay ウィンドウを大きくすることを推奨します。"
                f" デバイス論理サイズ ({self._device_width}×{self._device_height}px)"
                " にアップスケールします。"
            )
            img = cv2.resize(
                img,
                (self._device_width, self._device_height),
                interpolation=cv2.INTER_LANCZOS4,
            )

        return img

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
    # screenshot() オーバーライド — ウィンドウ存在確認を追加
    # ----------------------------------------------------------

    def screenshot(self, name: str = "") -> "Path":
        """
        AppiumDriver.screenshot() の前にウィンドウ存在を定期確認する。

        VERIFY_INTERVAL 回に 1 回 find_mirroring_window() を実行し、
        ウィンドウが消失していた場合は WindowNotFoundError を送出する。

        Args:
            name: スクリーンショットのファイル名サフィックス

        Returns:
            保存された PNG ファイルの Path

        Raises:
            WindowNotFoundError: ウィンドウが消失した場合
        """
        self._screenshot_count += 1
        if self._screenshot_count % self.VERIFY_INTERVAL == 1:
            # 1, 6, 11, ... 回目に確認
            self._verify_window_exists()
        return self._appium.screenshot(name)

    # ----------------------------------------------------------
    # 内部メソッド
    # ----------------------------------------------------------

    def _verify_window_exists(self) -> None:
        """
        ウィンドウが引き続き存在することを確認する。

        ウィンドウが見つからない場合は _window_rect キャッシュを無効化し、
        WindowNotFoundError を送出する。

        Raises:
            WindowNotFoundError: ウィンドウが消失した場合
        """
        from tools.window_manager import find_mirroring_window

        candidates = (
            [self._window_title] if self._window_title
            else list(self._WINDOW_TITLE_CANDIDATES)
        )
        rect = find_mirroring_window(candidates)
        if rect is None:
            self._window_rect = None  # キャッシュを無効化
            raise BaseDriver.WindowNotFoundError(
                "ミラーリングウィンドウが消失しました。"
                " UxPlay または scrcpy が終了した可能性があります。"
                " クロールを中断してデータを保存します。"
            )
        # ウィンドウが移動した場合のために位置を更新
        if self._window_rect != rect:
            logger.info(f"[MIRROR] ウィンドウ位置更新: {self._window_rect} → {rect}")
            self._window_rect = rect

    # ----------------------------------------------------------
    # 後方互換: AppiumDriver の全メソッド・属性を透過委譲
    # ----------------------------------------------------------

    def __getattr__(self, name: str):
        """AppiumDriver への透過委譲。"""
        return getattr(self._appium, name)
