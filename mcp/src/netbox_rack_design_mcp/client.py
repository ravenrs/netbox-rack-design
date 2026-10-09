"""Thin NetBox REST client. The token is only ever put in the Authorization header."""

from __future__ import annotations

import os

import httpx

API_ROOT = "/api/plugins/rack-design"


class NetBoxError(RuntimeError):
    """A NetBox call failed. The message never contains the token."""


def auth_header(token: str) -> str:
    # NetBox 4.5+ v2 tokens look like "nbt_<key>.<secret>" and use Bearer;
    # older (v1) tokens use the "Token" scheme.
    return f"Bearer {token}" if token.startswith("nbt_") else f"Token {token}"


class NetBoxClient:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 verify: bool = True, timeout: float = 60.0):
        self._http = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": auth_header(token), "Accept": "application/json"},
            transport=transport,
            verify=verify,
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> NetBoxClient:
        url = os.environ.get("NETBOX_URL", "").strip()
        token = os.environ.get("NETBOX_TOKEN", "").strip()
        if not url or not token:
            raise NetBoxError("Set NETBOX_URL and NETBOX_TOKEN in the MCP server environment.")
        verify = os.environ.get("NETBOX_VERIFY_SSL", "true").strip().lower() not in ("0", "false", "no")
        return cls(url, token, verify=verify)

    def request(self, method: str, path: str, *, params=None, json=None):
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            resp = self._http.request(method, f"{API_ROOT}/{path}", params=clean, json=json)
        except httpx.HTTPError as exc:
            raise NetBoxError(f"NetBox request failed: {type(exc).__name__}") from None
        if resp.status_code >= 400:
            raise NetBoxError(f"NetBox answered {resp.status_code} for {method} {path}: {resp.text[:500]}")
        if "json" in resp.headers.get("content-type", ""):
            return resp.json()
        return resp.text

    def get(self, path: str, **params):
        return self.request("GET", path, params=params)

    def post(self, path: str, body):
        return self.request("POST", path, json=body)
