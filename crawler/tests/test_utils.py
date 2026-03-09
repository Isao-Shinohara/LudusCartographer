"""
test_utils.py — デバイス接続ユーティリティのユニットテスト

実機なし・外部コマンドをモックして全パスを検証する。
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import subprocess

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from lc.utils import (
    get_device_udid,
    get_android_serial,
    detect_connected_device,
    ensure_adb_connection,
    _try_idevice_id,
    _try_ioreg,
    _try_adb,
    _find_usb_device,
    _find_wifi_device,
    _switch_to_tcpip,
    _adb_connect,
    _adb_pair,
    _format_ios_udid,
    diagnose_device_connection,
)

# ============================================================
# テスト用定数
# ============================================================

FAKE_UDID_ENV    = "00008120-000A1234ABCD1234"
FAKE_UDID_IDEV   = "00008030-001A2B3C4D5E6F78"
# ioreg が返す raw 24文字シリアル → _try_ioreg() は XXXXXXXX-XXXXXXXXXXXXXXXX に変換する
FAKE_SERIAL_RAW   = "0000814000061C16222B001C"
FAKE_SERIAL_IOREG = "00008140-00061C16222B001C"   # 変換後の期待値
FAKE_ANDROID_SERIAL = "emulator-5554"

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

ADB_DEVICES_OUTPUT = """\
List of devices attached
emulator-5554\tdevice
"""

ADB_DEVICES_OFFLINE = """\
List of devices attached
emulator-5554\toffline
"""


# ============================================================
# _format_ios_udid
# ============================================================

class TestFormatIosUdid:

    def test_formats_24char_raw_serial(self):
        assert _format_ios_udid("0000814000061C16222B001C") == "00008140-00061C16222B001C"

    def test_passthrough_when_already_dashed(self):
        assert _format_ios_udid("00008140-00061C16222B001C") == "00008140-00061C16222B001C"

    def test_uppercases_result(self):
        assert _format_ios_udid("0000814000061c16222b001c") == "00008140-00061C16222B001C"

    def test_passthrough_long_udid(self):
        """40文字旧形式はそのまま返す"""
        long_udid = "A" * 40
        assert _format_ios_udid(long_udid) == long_udid


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

    def test_accepts_dashed_udid(self):
        """XXXXXXXX-XXXXXXXXXXXXXXXX 形式の UDID を受け入れること"""
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
        """ioreg 出力から UDID を XXXXXXXX-XXXXXXXXXXXXXXXX 形式で返すこと"""
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

    def test_serial_uppercased_and_dashed(self):
        """抽出した Serial が大文字・ダッシュ付きに正規化されること"""
        lower_output = IOREG_SAMPLE_OUTPUT.replace(
            FAKE_SERIAL_RAW, FAKE_SERIAL_RAW.lower()
        )
        with patch("subprocess.run", return_value=self._mock_run(lower_output)):
            result = _try_ioreg(5)
            assert result == FAKE_SERIAL_IOREG


# ============================================================
# _try_adb
# ============================================================

class TestTryAdb:

    def _mock_run(self, stdout: str):
        result = MagicMock()
        result.stdout = stdout
        return result

    def test_returns_serial_when_device_online(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_OUTPUT)):
            assert _try_adb(5) == FAKE_ANDROID_SERIAL

    def test_returns_none_when_device_offline(self):
        """offline 状態のデバイスは無視すること"""
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_OFFLINE)):
            assert _try_adb(5) is None

    def test_returns_none_when_no_devices(self):
        with patch("subprocess.run", return_value=self._mock_run("List of devices attached\n")):
            assert _try_adb(5) is None

    def test_returns_none_on_file_not_found(self):
        """adb が未インストールの場合は None を返すこと"""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _try_adb(5) is None

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 5)):
            assert _try_adb(5) is None


# ============================================================
# get_device_udid — 優先順位テスト (後方互換 iOS 専用 API)
# ============================================================

class TestGetDeviceUdid:

    def test_env_var_takes_priority(self, monkeypatch):
        """環境変数 IOS_UDID が最優先されること"""
        monkeypatch.setenv("IOS_UDID", FAKE_UDID_ENV)
        with patch("subprocess.run") as mock_run:
            result = get_device_udid()
            mock_run.assert_not_called()
        assert result == FAKE_UDID_ENV

    def test_idevice_id_used_when_no_env(self, monkeypatch):
        """環境変数なし → idevice_id を使うこと"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        idev_result = MagicMock()
        idev_result.stdout = FAKE_UDID_IDEV + "\n"

        with patch("subprocess.run", return_value=idev_result):
            result = get_device_udid()
        assert result == FAKE_UDID_IDEV

    def test_ioreg_fallback_when_idevice_empty(self, monkeypatch):
        """idevice_id が空 → ioreg にフォールバックすること (UDID は変換済み形式)"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)

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
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock()
        r.stdout = ""

        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError, match="見つかりませんでした"):
                get_device_udid()

    def test_error_message_is_helpful(self, monkeypatch):
        """エラーメッセージに具体的な対処法が含まれること"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError) as exc_info:
                get_device_udid()
        msg = str(exc_info.value)
        assert "IOS_UDID" in msg     # 手動設定方法を案内
        assert "信頼" in msg         # ペアリング案内


# ============================================================
# detect_connected_device — iOS/Android 統合検出
# ============================================================

class TestDetectConnectedDevice:

    def test_ios_env_takes_priority(self, monkeypatch):
        """IOS_UDID 環境変数が最優先されること"""
        monkeypatch.setenv("IOS_UDID", FAKE_UDID_ENV)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        udid, platform = detect_connected_device()
        assert udid == FAKE_UDID_ENV
        assert platform == "ios"

    def test_android_env_used_when_no_ios(self, monkeypatch):
        """IOS_UDID 未設定 → ANDROID_SERIAL が使われること"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.setenv("ANDROID_SERIAL", FAKE_ANDROID_SERIAL)
        with patch("subprocess.run") as mock_run:
            udid, platform = detect_connected_device()
            mock_run.assert_not_called()
        assert udid == FAKE_ANDROID_SERIAL
        assert platform == "android"

    def test_adb_detected_as_android(self, monkeypatch):
        """adb でデバイスが見つかれば Android として検出されること"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)

        def side_effect(cmd, **kwargs):
            r = MagicMock()
            if "adb" in cmd:
                r.stdout = ADB_DEVICES_OUTPUT
            else:
                r.stdout = ""  # idevice_id / ioreg は空
            return r

        with patch("subprocess.run", side_effect=side_effect):
            udid, platform = detect_connected_device()

        assert udid == FAKE_ANDROID_SERIAL
        assert platform == "android"

    def test_ios_only_skips_android(self, monkeypatch):
        """ios_only=True の場合、adb は呼ばれないこと"""
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.setenv("ANDROID_SERIAL", FAKE_ANDROID_SERIAL)

        idev_result = MagicMock()
        idev_result.stdout = FAKE_UDID_IDEV + "\n"

        with patch("subprocess.run", return_value=idev_result):
            udid, platform = detect_connected_device(ios_only=True)

        # ANDROID_SERIAL は無視され idevice_id の結果が使われる
        assert platform == "ios"
        assert udid == FAKE_UDID_IDEV


# ============================================================
# diagnose_device_connection
# ============================================================

class TestDiagnoseDeviceConnection:

    _EXPECTED_KEYS = {
        "env_udid", "env_android", "idevice_id", "ioreg_serial",
        "adb_serial", "usbmuxd_pid", "trusted", "platform",
    }

    def test_returns_dict_with_expected_keys(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            report = diagnose_device_connection()
        assert set(report.keys()) == self._EXPECTED_KEYS

    def test_trusted_true_when_idevice_returns_udid(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)

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
        assert report["platform"] == "ios"

    def test_trusted_false_when_idevice_empty(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock(); r.stdout = ""
        with patch("subprocess.run", return_value=r):
            report = diagnose_device_connection()
        assert report["trusted"] is False

    def test_platform_android_when_adb_found(self, monkeypatch):
        monkeypatch.delenv("IOS_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)

        def side_effect(cmd, **kwargs):
            m = MagicMock()
            if "adb" in cmd:
                m.stdout = ADB_DEVICES_OUTPUT
            else:
                m.stdout = ""
            return m

        with patch("subprocess.run", side_effect=side_effect):
            report = diagnose_device_connection()
        assert report["adb_serial"] == FAKE_ANDROID_SERIAL
        assert report["platform"] == "android"


# ============================================================
# get_android_serial — Android 専用自動検出 API
# ============================================================

FAKE_WIFI_SERIAL = "192.168.10.118:5555"

ADB_DEVICES_WIFI = """\
List of devices attached
192.168.10.118:5555\tdevice
"""

ADB_DEVICES_MULTI = """\
List of devices attached
f6b8cef7\tdevice
192.168.10.118:5555\tdevice
"""


class TestGetAndroidSerial:

    def test_android_udid_env_takes_priority(self, monkeypatch):
        """ANDROID_UDID 環境変数が最優先されること"""
        monkeypatch.setenv("ANDROID_UDID", FAKE_WIFI_SERIAL)
        monkeypatch.setenv("ANDROID_SERIAL", FAKE_ANDROID_SERIAL)
        with patch("subprocess.run") as mock_run:
            result = get_android_serial()
            mock_run.assert_not_called()
        assert result == FAKE_WIFI_SERIAL

    def test_android_serial_env_fallback(self, monkeypatch):
        """ANDROID_UDID 未設定 → ANDROID_SERIAL にフォールバックすること"""
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.setenv("ANDROID_SERIAL", FAKE_ANDROID_SERIAL)
        with patch("subprocess.run") as mock_run:
            result = get_android_serial()
            mock_run.assert_not_called()
        assert result == FAKE_ANDROID_SERIAL

    def test_adb_usb_auto_detect(self, monkeypatch):
        """環境変数未設定 → adb devices から USB デバイスを自動検出すること"""
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock()
        r.stdout = ADB_DEVICES_OUTPUT
        with patch("subprocess.run", return_value=r):
            result = get_android_serial()
        assert result == FAKE_ANDROID_SERIAL

    def test_adb_wifi_auto_detect(self, monkeypatch):
        """環境変数未設定 → adb devices から Wi-Fi デバイスを自動検出すること"""
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock()
        r.stdout = ADB_DEVICES_WIFI
        with patch("subprocess.run", return_value=r):
            result = get_android_serial()
        assert result == FAKE_WIFI_SERIAL

    def test_raises_when_no_device(self, monkeypatch):
        """デバイスなし → RuntimeError を送出すること"""
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock()
        r.stdout = "List of devices attached\n"
        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError, match="Android デバイスが見つかりませんでした"):
                get_android_serial()

    def test_error_message_includes_wifi_instructions(self, monkeypatch):
        """エラーメッセージに Wi-Fi 接続手順が含まれること"""
        monkeypatch.delenv("ANDROID_UDID", raising=False)
        monkeypatch.delenv("ANDROID_SERIAL", raising=False)
        r = MagicMock()
        r.stdout = ""
        with patch("subprocess.run", return_value=r):
            with pytest.raises(RuntimeError) as exc_info:
                get_android_serial()
        msg = str(exc_info.value)
        assert "Wi-Fi" in msg
        assert "adb connect" in msg
        assert "ANDROID_UDID" in msg


# ============================================================
# _find_usb_device / _find_wifi_device ヘルパーテスト
# ============================================================

ADB_DEVICES_USB_ONLY = """\
List of devices attached
f6b8cef7\tdevice
"""

ADB_DEVICES_WIFI_ONLY = """\
List of devices attached
192.168.10.118:5555\tdevice
"""

ADB_DEVICES_USB_AND_WIFI = """\
List of devices attached
f6b8cef7\tdevice
192.168.10.118:5555\tdevice
"""


class TestFindUsbDevice:

    def _mock_run(self, stdout):
        r = MagicMock(); r.stdout = stdout; return r

    def test_returns_usb_serial(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_USB_ONLY)):
            assert _find_usb_device() == "f6b8cef7"

    def test_ignores_wifi_device(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_WIFI_ONLY)):
            assert _find_usb_device() is None

    def test_returns_first_usb_when_mixed(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_USB_AND_WIFI)):
            assert _find_usb_device() == "f6b8cef7"

    def test_returns_none_on_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _find_usb_device() is None


class TestFindWifiDevice:

    def _mock_run(self, stdout):
        r = MagicMock(); r.stdout = stdout; return r

    def test_returns_matching_wifi(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_WIFI_ONLY)):
            assert _find_wifi_device("192.168.10.118:5555") == "192.168.10.118:5555"

    def test_returns_none_when_no_match(self):
        with patch("subprocess.run", return_value=self._mock_run(ADB_DEVICES_USB_ONLY)):
            assert _find_wifi_device("192.168.10.118:5555") is None

    def test_returns_none_on_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _find_wifi_device("192.168.10.118:5555") is None


# ============================================================
# _switch_to_tcpip / _adb_connect / _adb_pair
# ============================================================

class TestSwitchToTcpip:

    def test_success(self):
        r = MagicMock(); r.stdout = "restarting in TCP mode port: 5555"; r.returncode = 0
        with patch("subprocess.run", return_value=r):
            assert _switch_to_tcpip("f6b8cef7") is True

    def test_failure_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 10)):
            assert _switch_to_tcpip("f6b8cef7") is False


class TestAdbConnect:

    def test_success(self):
        r = MagicMock(); r.stdout = "connected to 192.168.10.118:5555"
        with patch("subprocess.run", return_value=r):
            assert _adb_connect("192.168.10.118:5555") is True

    def test_cannot_connect(self):
        r = MagicMock(); r.stdout = "cannot connect to 192.168.10.118:5555"
        with patch("subprocess.run", return_value=r):
            assert _adb_connect("192.168.10.118:5555") is False

    def test_failure_on_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert _adb_connect("192.168.10.118:5555") is False


class TestAdbPair:

    def test_success(self):
        r = MagicMock(); r.stdout = "Successfully paired to 192.168.10.118:37000"; r.returncode = 0
        with patch("subprocess.run", return_value=r):
            assert _adb_pair("192.168.10.118", 37000, "123456") is True

    def test_failure(self):
        r = MagicMock(); r.stdout = "Failed: "; r.stderr = "error"; r.returncode = 1
        with patch("subprocess.run", return_value=r):
            assert _adb_pair("192.168.10.118", 37000, "123456") is False


# ============================================================
# ensure_adb_connection — 統合フロー
# ============================================================

class TestEnsureAdbConnection:
    """ensure_adb_connection() の各パスを検証する。"""

    WIFI_ADDR = "192.168.10.118:5555"

    def test_wifi_already_connected(self):
        """Wi-Fi デバイスが既にオンライン → そのまま返す"""
        with patch("lc.utils._find_wifi_device", return_value=self.WIFI_ADDR):
            result = ensure_adb_connection(wifi_addr=self.WIFI_ADDR)
        assert result == self.WIFI_ADDR

    def test_usb_to_wifi_switch(self):
        """USB デバイス検出 → tcpip 切替 → Wi-Fi 接続成功"""
        with patch("lc.utils._find_wifi_device", side_effect=[None, self.WIFI_ADDR]), \
             patch("lc.utils._try_adb", return_value="f6b8cef7"), \
             patch("lc.utils._switch_to_tcpip", return_value=True), \
             patch("lc.utils._adb_connect", return_value=True), \
             patch("time.sleep"):
            result = ensure_adb_connection(wifi_addr=self.WIFI_ADDR)
        assert result == self.WIFI_ADDR

    def test_usb_fallback_when_tcpip_fails(self):
        """USB デバイスあり + tcpip 切替失敗 → USB シリアルを返す"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value="f6b8cef7"), \
             patch("lc.utils._switch_to_tcpip", return_value=False):
            result = ensure_adb_connection(wifi_addr=self.WIFI_ADDR)
        assert result == "f6b8cef7"

    def test_direct_wifi_connect(self):
        """デバイスなし → Wi-Fi 直接接続成功"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value=None), \
             patch("lc.utils._adb_connect", return_value=True):
            result = ensure_adb_connection(wifi_addr=self.WIFI_ADDR)
        assert result == self.WIFI_ADDR

    def test_adb_pair_flow(self):
        """直接接続失敗 + pairing_code あり → adb pair → connect 成功"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value=None), \
             patch("lc.utils._adb_connect", side_effect=[False, True]), \
             patch("lc.utils._adb_pair", return_value=True), \
             patch("time.sleep"):
            result = ensure_adb_connection(
                wifi_addr=self.WIFI_ADDR,
                pairing_code="123456",
                pairing_port=37000,
            )
        assert result == self.WIFI_ADDR

    def test_adb_pair_failure_raises(self):
        """adb pair 失敗 → RuntimeError"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value=None), \
             patch("lc.utils._adb_connect", return_value=False), \
             patch("lc.utils._adb_pair", return_value=False):
            with pytest.raises(RuntimeError, match="pair 失敗"):
                ensure_adb_connection(
                    wifi_addr=self.WIFI_ADDR,
                    pairing_code="123456",
                    pairing_port=37000,
                )

    def test_no_device_no_pairing_raises(self):
        """デバイスなし + pairing_code なし → RuntimeError"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value=None), \
             patch("lc.utils._adb_connect", return_value=False):
            with pytest.raises(RuntimeError, match="見つかりません"):
                ensure_adb_connection(wifi_addr=self.WIFI_ADDR)

    def test_backward_compatible_no_args(self):
        """引数なし呼び出し (後方互換) — wifi_addr 空 → _try_adb フォールバック"""
        with patch("lc.utils._try_adb", return_value="f6b8cef7"):
            result = ensure_adb_connection()
        assert result == "f6b8cef7"

    def test_other_wifi_device_returned(self):
        """Wi-Fi アドレス不一致だが何らかのデバイスが接続中 → そのまま返す"""
        with patch("lc.utils._find_wifi_device", return_value=None), \
             patch("lc.utils._try_adb", return_value="10.0.0.5:5555"):
            result = ensure_adb_connection(wifi_addr=self.WIFI_ADDR)
        assert result == "10.0.0.5:5555"
