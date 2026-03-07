from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlparse

from fastapi import Request


def env_truthy(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_csv_list(value: Optional[str], fallback: list[str]) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return list(fallback)
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return items or list(fallback)


def merge_allowed_hosts(configured_hosts: list[str]) -> list[str]:
    baseline = ["localhost", "127.0.0.1", "::1", "testserver"]
    merged: list[str] = []
    seen: set[str] = set()
    for host in [*configured_hosts, *baseline]:
        key = str(host).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(str(host).strip())
    return merged


def _is_viable_lan_ip(candidate: str) -> bool:
    try:
        parsed = ipaddress.ip_address(str(candidate).strip())
    except ValueError:
        return False
    if parsed.version != 4:
        return False
    if parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast:
        return False
    return True


def _discover_preferred_lan_ip() -> Optional[str]:
    candidates: list[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe_socket:
            probe_socket.connect(("8.8.8.8", 80))
            discovered = str(probe_socket.getsockname()[0]).strip()
            if discovered:
                candidates.append(discovered)
    except OSError:
        pass

    try:
        for addr_info in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            discovered = str(addr_info[4][0]).strip()
            if discovered:
                candidates.append(discovered)
    except OSError:
        pass

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_viable_lan_ip(candidate):
            return candidate
    return None


def _extract_host_port(raw_host: str) -> tuple[str, Optional[str]]:
    parsed = urlparse(f"//{raw_host}")
    hostname = str(parsed.hostname or raw_host).strip().strip("[]")
    port_value = str(parsed.port) if parsed.port else None
    return hostname, port_value


def get_configured_lan_host(fallback_port: str = "8443") -> tuple[str, str]:
    configured_value = str(os.getenv("LOCAL_IP", "")).strip() or str(os.getenv("LAN_HOST", "")).strip()
    if not configured_value:
        return "", fallback_port
    host_value, host_port = _extract_host_port(configured_value)
    normalized_host = str(host_value).strip().strip("[]")
    normalized_port = str(host_port or fallback_port).strip() or fallback_port
    return normalized_host, normalized_port


def resolve_mobile_entry_url(request: Request) -> str:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    forwarded_host = str(request.headers.get("x-forwarded-host") or "").split(",", 1)[0].strip()
    host = forwarded_host or str(request.headers.get("host") or "").split(",", 1)[0].strip()

    if host:
        extracted_host, extracted_port = _extract_host_port(host)
        normalized_host = str(extracted_host).strip().lower()
        loopback_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        fallback_port = extracted_port or "8443"
        configured_lan_host_value, configured_lan_port = get_configured_lan_host(fallback_port)

        discovered_lan_ip = _discover_preferred_lan_ip()
        if normalized_host in loopback_hosts:
            if configured_lan_host_value:
                return f"https://{configured_lan_host_value}:{configured_lan_port}/"
            if discovered_lan_ip:
                return f"https://{discovered_lan_ip}:{fallback_port}/"

        try:
            host_ip = ipaddress.ip_address(normalized_host)
            if (
                (configured_lan_host_value or discovered_lan_ip)
                and host_ip.version == 4
                and (host_ip.is_private or host_ip.is_link_local)
                and (
                    (configured_lan_host_value and str(host_ip) != configured_lan_host_value)
                    or (discovered_lan_ip and str(host_ip) != discovered_lan_ip)
                )
            ):
                if configured_lan_host_value:
                    return f"https://{configured_lan_host_value}:{configured_lan_port}/"
                if discovered_lan_ip:
                    return f"https://{discovered_lan_ip}:{fallback_port}/"
        except ValueError:
            pass

        scheme = forwarded_proto or str(request.url.scheme or "").strip().lower() or "https"
        return f"{scheme}://{host.rstrip('/')}/"

    configured_public_base_url = str(os.getenv("PUBLIC_BASE_URL", "")).strip()
    base_url = configured_public_base_url or str(request.base_url).strip()
    if not base_url:
        return "/"
    return f"{base_url.rstrip('/')}/"
