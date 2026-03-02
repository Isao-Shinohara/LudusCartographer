"""
test_utils.py — get_device_udid() / diagnose_device_connection() ユニットテスト

実機なし・外部コマンドをモックして全パスを検証する。
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.utils import get_device_udid, _try_idevice_id, _try_ioreg, diagnose_device_connection

# ============================================================
# テスト用定数
# ============================================================

FAKE_UDID_ENV    = "00008120-000A1234ABCD1234"
FAKE_UDID_IDEV   = "00008030-001A2B3C4D5E6F78"
FAKE_SERIAL_IOREG = "0000814000061C16222B001C"  # 24文字16進 (実機で確認済みの値)

IOREG_SAMPLE_OUTPUT = """\
+-o Root  <class IORegistryEntry>
  +-o AppleT8112USBXHCI@00000000
    +-o iPhone@02100000  <class IOUSBHostDevice>
      | {
      |   "idVendor" = 1452
      |   "idProduct" = 4776
      |   "USB Serial Number" = "0000814000061C16222B001C"
      |   "USB Product Name" = "iPhone"
      | }
"""

IOREG_NO_IPHONE = """\
+-o Root  <class IORegistryEntry>
  +-o AppleT8112USBXHCI@00000000
    +-o USB2.0 Hub@01100000
"""


# ============================================================
# _try_idevice_id
# ============================================================

class TestTryIdeviceId:

    def _mock_run(self, stdout: str, returncode: int = 0):
        result = MagicMock()
        result.stdout = stdout
        result.returncode = returncode
        return result

    def test_returns_udid_when_found(self):
        with patch("subprocess.run", return_value=self._mock_run(FAKE_UDID_IDEV + "\n")):
            assert _try_idevice_id(5) == FAKE_UDID_IDEV

    def test_returns_none_when_empty(self):
        with patch("subprocess.run", return_value=self._mock_run("")):
            assert _try_idevice_id(5) is None

    def test_returns_none_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _try_idevice_id(5) is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("idevice_id", 5)):
            assert _try_idevice_id(5) is None

    def test_ignores_non_udid_lines(self):
        """ヘッダー行などの非UDIDテキストを無視すること"""
        output = "Devices:\n" + FAKE_UDID_IDEV + "\n"
        with patch("subprocess.run", return_value=self._mock_run(output)):
            assert _try_idevice_id(5) == FAKE_UDID_IDEV

    def test_accepts_24char_serial(self):
        """24文字シリアル形式の UDID を受け入れること"""
        with patch("subprocess.run", return_value=self._mock_run(FAKE_SERIAL_IOREG + "\n")):
            assert _try_idevice_id(5) == FAKE_SERIAL_IOREG


# ============================================================
# _try_ioreg
# ============================================================

class TestTryIoreg:

    def _mock_run(self, stdout: str):
        result = MagicMock()
        result.stdout = stdout
        return result

    def test_extracts_serial_from_ioreg(self):
        with patch("subprocess.run", return_value=self._mock_run(IOREG_SAMPLE_OUTPUT)):
            serial = _try_ioreg(5)
            assert serial == FAKE_SERIAL_IOREG

    def test_returns_none_when_no_iphone(self):
        with patch("subprocess.run", return_value=self._mock_run(IOREG_NO_IPHONE)):
            assert _try_ioreg(5) is None

    def test_returns_none_on_file_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _try_ioreg(5) is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ioreg", 5)):
            assert _try_ioreg(5) is None

    def test_non_apple_vendor_is_ignored(self):
        """Apple 以外の VendorID (≠1452) は無視されること"""
        non_apple = IOREG_SAMPLE_OUTPUT.replace('"idVendor" = 1452', '"idVendor" = 9999')
        with patch("subprocess.run", return_value=self._mock_run(non_apple)):
            assert _try_ioreg(5) is None

    def test_serial_uppercased(self):
        """抽出した Serial が大文字に正規化されること"""
        lower_output = IOREG_SAMPLE_OUTPUT.replace(
            FAKE_SERIAL_IOREG, FAKE_SERIAL_IOREG.lower()
        )
        with patch("subprocess.run", return_value=self._mock_run(lower_output)):
            result = _try_ioreg(5)
            assert result == FAKE_SERIAL_IOREG  # 大文字であること


# ============================================================
# get_device_udid — 優先順位テスト
# ============================================================

class TestGetDeviceUdid:

    def test_env_var_takes_priority(self, monkeypatch):
        """環境変数 IOS_UDID が最優先されること"""
        monkeypatch.setenv("IOS_UDID", FAKE_UDID_ENV)
        # idevice_id / ioreg は呼ばれないはず
        with patch("subprocess.run") as mock_run:
            result = get_device_udid()
            mock_run.assert_not_called()
        assert result == FAKE_UDID_ENV

    def test_idevice_id_used_when_no_env(self, monkeypatch):
        """環境変数なし → idevice_id を使うこと"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        idev_result = MagicMock()
        idev_result.stdout = FAKE_UDID_IDEV + "\n"

        with patch("subprocess.run", return_value=idev_result):
            result = get_device_udid()
        assert result == FAKE_UDID_IDEV

    def test_ioreg_fallback_when_idevice_empty(self, monkeypatch):
        """idevice_id が空 → ioreg にフォールバックすること"""
        monkeypatch.delenv("IOS_UDID", raising=False)

        call_count = [0]
        def side_effect(cmd, **kwargs):
            r = MagicMock()
            if "idevice_id" in cmd:
                r.stdout = ""          # idevice_id は空
            else:
                r.stdout = IOREG_SAMPLE_OUTPUT   # ioreg は成功
            call_count[0] += 1
            return r

        with patch("subprocess.run", side_effect=side_effect):
            result = get_device_udid()

        assert result == FAKE_SERIAL_IOREG
        assert call_count[0] == 2  # idevice_id + ioreg の2回呼ばれること

    def test_raises_when_all_methods_fail(self, monkeypatch):
        """全手段が失敗したとき RuntimeError を送出すること"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        r = MagicMock()
        r.stdout = ""

        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError, match="見つかりませんでした"):
                get_device_udid()

    def test_error_message_is_helpful(self, monkeypatch):
        """エラーメッセージに具体的な対処法が含まれること"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError) as exc_info:
                get_device_udid()
        msg = str(exc_info.value)
        assert "IOS_UDID" in msg     # 手動設定方法を案内
        assert "信頼" in msg         # ペアリング案内


# ============================================================
# diagnose_device_connection
# ============================================================

class TestDiagnoseDeviceConnection:

    def test_returns_dict_with_expected_keys(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            report = diagnose_device_connection()
        assert set(report.keys()) == {"env_udid", "idevice_id", "ioreg_serial", "usbmuxd_pid", "trusted"}

    def test_trusted_true_when_idevice_returns_udid(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        r = MagicMock()

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if "idevice_id" in cmd:
                m.stdout = FAKE_UDID_IDEV + "\n"
            else:
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=side_effect):
            report = diagnose_device_connection()
        assert report["trusted"] is True
        assert report["idevice_id"] == FAKE_UDID_IDEV

    def test_trusted_false_when_idevice_empty(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            report = diagnose_device_connection()
        assert report["trusted"] is False
