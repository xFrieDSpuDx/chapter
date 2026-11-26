"""Keep Wi-Fi and Bluetooth checks off the dashboard request path."""

from .device_status import DeviceStatusCache
from .wifi import WifiStatus


class WifiStatusCache(DeviceStatusCache[WifiStatus | None]):
    def __init__(self, *, ttl: float = 3) -> None:
        super().__init__(None, WifiStatus(False, message="Wi-Fi status unavailable"),
                         ttl=ttl, name="chapter-web-wifi")


class BluetoothStatusCache(DeviceStatusCache[tuple[bool, str]]):
    def __init__(self, *, ttl: float = 15) -> None:
        super().__init__((False, "Checking Bluetooth…"), (False, "Bluetooth status unavailable"),
                         ttl=ttl, name="chapter-web-bluetooth")
