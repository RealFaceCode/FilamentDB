import unittest

from fastapi import Request

import app.main as main_module


class MobileEntryUrlResolverTests(unittest.TestCase):
    def test_loopback_host_prefers_discovered_lan_ip(self):
        original_discover = main_module._discover_preferred_lan_ip
        original_local_ip = main_module.os.environ.get("LOCAL_IP")
        original_lan_host = main_module.os.environ.get("LAN_HOST")
        try:
            main_module._discover_preferred_lan_ip = lambda: "192.168.128.23"
            main_module.os.environ.pop("LOCAL_IP", None)
            main_module.os.environ.pop("LAN_HOST", None)

            request = Request(
                {
                    "type": "http",
                    "scheme": "https",
                    "method": "GET",
                    "path": "/dashboard",
                    "headers": [(b"host", b"127.0.0.1:8443")],
                    "query_string": b"",
                    "client": ("127.0.0.1", 50000),
                    "server": ("127.0.0.1", 8443),
                }
            )

            resolved = main_module._resolve_mobile_entry_url(request)
            self.assertEqual(resolved, "https://192.168.128.23:8443/")
        finally:
            main_module._discover_preferred_lan_ip = original_discover
            if original_local_ip is None:
                main_module.os.environ.pop("LOCAL_IP", None)
            else:
                main_module.os.environ["LOCAL_IP"] = original_local_ip
            if original_lan_host is None:
                main_module.os.environ.pop("LAN_HOST", None)
            else:
                main_module.os.environ["LAN_HOST"] = original_lan_host

    def test_stale_private_host_is_replaced_with_current_lan_ip(self):
        original_discover = main_module._discover_preferred_lan_ip
        original_local_ip = main_module.os.environ.get("LOCAL_IP")
        original_lan_host = main_module.os.environ.get("LAN_HOST")
        try:
            main_module._discover_preferred_lan_ip = lambda: "172.18.0.3"
            main_module.os.environ.pop("LOCAL_IP", None)
            main_module.os.environ["LAN_HOST"] = "192.168.128.23"

            request = Request(
                {
                    "type": "http",
                    "scheme": "https",
                    "method": "GET",
                    "path": "/dashboard",
                    "headers": [(b"host", b"192.168.127.78:8443")],
                    "query_string": b"",
                    "client": ("127.0.0.1", 50000),
                    "server": ("127.0.0.1", 8443),
                }
            )

            resolved = main_module._resolve_mobile_entry_url(request)
            self.assertEqual(resolved, "https://192.168.128.23:8443/")
        finally:
            main_module._discover_preferred_lan_ip = original_discover
            if original_local_ip is None:
                main_module.os.environ.pop("LOCAL_IP", None)
            else:
                main_module.os.environ["LOCAL_IP"] = original_local_ip
            if original_lan_host is None:
                main_module.os.environ.pop("LAN_HOST", None)
            else:
                main_module.os.environ["LAN_HOST"] = original_lan_host

    def test_local_ip_overrides_legacy_lan_host(self):
        original_discover = main_module._discover_preferred_lan_ip
        original_local_ip = main_module.os.environ.get("LOCAL_IP")
        original_lan_host = main_module.os.environ.get("LAN_HOST")
        try:
            main_module._discover_preferred_lan_ip = lambda: None
            main_module.os.environ["LOCAL_IP"] = "192.168.127.78"
            main_module.os.environ["LAN_HOST"] = "192.168.128.23"

            request = Request(
                {
                    "type": "http",
                    "scheme": "https",
                    "method": "GET",
                    "path": "/dashboard",
                    "headers": [(b"host", b"127.0.0.1:8443")],
                    "query_string": b"",
                    "client": ("127.0.0.1", 50000),
                    "server": ("127.0.0.1", 8443),
                }
            )

            resolved = main_module._resolve_mobile_entry_url(request)
            self.assertEqual(resolved, "https://192.168.127.78:8443/")
        finally:
            main_module._discover_preferred_lan_ip = original_discover
            if original_local_ip is None:
                main_module.os.environ.pop("LOCAL_IP", None)
            else:
                main_module.os.environ["LOCAL_IP"] = original_local_ip
            if original_lan_host is None:
                main_module.os.environ.pop("LAN_HOST", None)
            else:
                main_module.os.environ["LAN_HOST"] = original_lan_host


if __name__ == "__main__":
    unittest.main()
