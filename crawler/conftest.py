"""
pytest の共通フィクスチャと設定。
DB接続テストは実際のMySQLが不要な場合モックに切り替わる。
"""
import os
import pytest
from pathlib import Path

# .env ファイルを自動ロード（存在する場合）
def pytest_configure(config):
    env_path = Path(__file__).parent / "config" / ".env"
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path)
