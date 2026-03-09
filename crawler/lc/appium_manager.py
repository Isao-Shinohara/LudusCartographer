"""
appium_manager.py — Appium サーバーの自動検出・起動・インストール案内

Usage:
    from lc.appium_manager import ensure_appium_server, AppiumNotInstalledError

    try:
        proc = ensure_appium_server("127.0.0.1", 4723)
    except AppiumNotInstalledError:
        print("Appium がインストールされていません")
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)


class AppiumNotInstalledError(Exception):
    """Appium バイナリが見つからない場合に送出される。"""
    pass


_managed_proc: Optional[subprocess.Popen] = None


def _cleanup_appium() -> None:
    """atexit ハンドラ: 自動起動した Appium プロセスを終了する。"""
    global _managed_proc
    if _managed_proc is not None and _managed_proc.poll() is None:
        logger.info("[APPIUM] 自動起動した Appium サーバーを終了します (PID=%d)", _managed_proc.pid)
        _managed_proc.terminate()
        try:
            _managed_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _managed_proc.kill()
        _managed_proc = None


atexit.register(_cleanup_appium)


def find_appium_binary() -> Optional[str]:
    """
    Appium バイナリのパスを探索する。

    探索順:
      1. PATH 上の appium
      2. nodebrew 環境 (~/.nodebrew/current/bin/appium)
      3. nvm 環境 (~/.nvm/versions/node/*/bin/appium)
      4. npm global (npm root -g)/appium
    """
    # 1. PATH
    found = shutil.which("appium")
    if found:
        return found

    # 2. nodebrew
    home = os.path.expanduser("~")
    nodebrew_appium = os.path.join(home, ".nodebrew", "current", "bin", "appium")
    if os.path.isfile(nodebrew_appium) and os.access(nodebrew_appium, os.X_OK):
        return nodebrew_appium

    # 3. nvm
    nvm_dir = os.path.join(home, ".nvm", "versions", "node")
    if os.path.isdir(nvm_dir):
        for node_ver in sorted(os.listdir(nvm_dir), reverse=True):
            nvm_appium = os.path.join(nvm_dir, node_ver, "bin", "appium")
            if os.path.isfile(nvm_appium) and os.access(nvm_appium, os.X_OK):
                return nvm_appium

    return None


def _is_server_running(host: str, port: int) -> bool:
    """Appium サーバーが指定アドレスで応答するか確認する。"""
    import urllib.request
    try:
        url = f"http://{host}:{port}/status"
        req = urllib.request.Request(url, method="GET")
        resp = urllib.request.urlopen(req, timeout=3)
        return resp.status == 200
    except Exception:
        return False


def ensure_appium_server(
    host: str = "127.0.0.1",
    port: int = 4723,
    startup_timeout: float = 15.0,
) -> Optional[subprocess.Popen]:
    """
    Appium サーバーが起動中でなければ自動起動する。

    Args:
        host: Appium ホスト (デフォルト 127.0.0.1)
        port: Appium ポート (デフォルト 4723)
        startup_timeout: 起動待機タイムアウト秒

    Returns:
        起動した Popen オブジェクト (既に起動中の場合は None)

    Raises:
        AppiumNotInstalledError: Appium バイナリが見つからない場合
    """
    global _managed_proc

    # 既に起動中なら何もしない
    if _is_server_running(host, port):
        logger.info("[APPIUM] サーバーは既に起動中 (%s:%d)", host, port)
        return None

    # 自動起動済みプロセスが生きているか確認
    if _managed_proc is not None and _managed_proc.poll() is None:
        logger.info("[APPIUM] 自動起動済みプロセス PID=%d が存在 — 応答待ち", _managed_proc.pid)
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            if _is_server_running(host, port):
                return _managed_proc
            time.sleep(1.0)
        logger.warning("[APPIUM] 自動起動済みプロセスが応答しません")

    # バイナリ探索
    appium_bin = find_appium_binary()
    if appium_bin is None:
        interactive_install_guide()
        raise AppiumNotInstalledError(
            "Appium がインストールされていません。"
            " `npm install -g appium` でインストールしてください。"
        )

    logger.info("[APPIUM] サーバーを自動起動します: %s --address %s --port %d", appium_bin, host, port)

    # appium のディレクトリから PATH に node を追加
    appium_dir = os.path.dirname(appium_bin)
    env = os.environ.copy()
    if appium_dir not in env.get("PATH", ""):
        env["PATH"] = appium_dir + os.pathsep + env.get("PATH", "")

    proc = subprocess.Popen(
        [appium_bin, "--address", host, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    _managed_proc = proc

    # 起動待ち
    deadline = time.time() + startup_timeout
    while time.time() < deadline:
        if _is_server_running(host, port):
            logger.info("[APPIUM] サーバー起動確認 PID=%d (%s:%d)", proc.pid, host, port)
            return proc
        if proc.poll() is not None:
            logger.error("[APPIUM] サーバープロセスが異常終了 (rc=%d)", proc.returncode)
            _managed_proc = None
            raise AppiumNotInstalledError(
                f"Appium サーバーが起動直後に終了しました (exit code: {proc.returncode})。"
                " `appium` を手動実行してエラーを確認してください。"
            )
        time.sleep(1.0)

    logger.error("[APPIUM] 起動タイムアウト (%.0f秒)", startup_timeout)
    proc.terminate()
    _managed_proc = None
    raise AppiumNotInstalledError(
        f"Appium サーバーが {startup_timeout:.0f} 秒以内に応答しませんでした。"
    )


def interactive_install_guide() -> None:
    """Appium 未インストール時のインストール案内を表示する。"""
    guide = """\
  ┌──────────────────────────────────────────────────────────┐
  │           Appium がインストールされていません              │
  └──────────────────────────────────────────────────────────┘

  Appium は Node.js パッケージとしてインストールします:

    1. Node.js (v18 LTS 推奨) をインストール:
         brew install node@18
       または nodebrew / nvm を使用

    2. Appium をグローバルインストール:
         npm install -g appium

    3. Android ドライバーをインストール:
         appium driver install uiautomator2

    4. (iOS の場合) iOS ドライバーをインストール:
         appium driver install xcuitest

  インストール後、再度このコマンドを実行してください。
"""
    print(guide, file=sys.stderr)

    # TTY なら自動インストールを提案
    if sys.stdin.isatty():
        try:
            answer = input("  npm install -g appium を実行しますか? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                print("  Appium をインストール中...")
                result = subprocess.run(
                    ["npm", "install", "-g", "appium"],
                    timeout=120,
                )
                if result.returncode == 0:
                    print("  Appium のインストールが完了しました。")
                    print("  ドライバーもインストールしてください:")
                    print("    appium driver install uiautomator2")
                    print("    appium driver install xcuitest")
                else:
                    print("  インストールに失敗しました。手動でインストールしてください。",
                          file=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            pass
