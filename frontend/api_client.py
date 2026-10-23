"""HTTP client for the Self Search API, used by the Streamlit server process (Phase 9).

The frontend depends only on the documented API contract, never on backend
modules. Errors are normalised into ``ApiError`` with a ``kind`` the UI maps
to a user-facing message.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from urllib.parse import quote

import requests

from frontend.config import FrontendConfig

ErrorKind = Literal[
    "unreachable", "auth", "not_found", "conflict", "validation", "unavailable", "server", "other",
]
_KIND_BY_STATUS: dict[int, ErrorKind] = {
    401: "auth", 404: "not_found", 409: "conflict", 422: "validation", 503: "unavailable",
}
MAX_IMAGE_BYTES = 2_000_000
# Smaller "images" are blank placeholders or tracking pixels, not product photos.
MIN_IMAGE_BYTES = 1_500


class ApiError(Exception):
    """A failed API call."""

    def __init__(self, kind: ErrorKind, detail: str, status: Optional[int] = None) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.status = status


class ShelfSenseClient:
    """Thin wrapper over the Self Search REST API."""

    def __init__(self, config: FrontendConfig, session: Optional[requests.Session] = None) -> None:
        self.config = config
        self.http = session or requests.Session()

    def _request(self, method: str, path: str, json: Any = None) -> Any:
        headers = {"X-API-Key": self.config.api_key} if self.config.api_key else {}
        try:
            resp = self.http.request(
                method, f"{self.config.api_url}{path}", json=json, headers=headers,
                timeout=self.config.timeout_s,
            )
        except requests.ReadTimeout as e:  # connected, but no answer in time: the API is busy, not down
            raise ApiError("timeout", f"No response within {self.config.timeout_s:.0f} s") from e
        except (requests.ConnectionError, requests.Timeout) as e:
            raise ApiError("unreachable", f"Cannot reach the Self Search API at {self.config.api_url}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            if not isinstance(detail, str):
                detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
            kind = _KIND_BY_STATUS.get(resp.status_code, "server" if resp.status_code >= 500 else "other")
            raise ApiError(kind, detail, resp.status_code)
        return resp.json() if resp.content else None

    # -- chat ---------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def chat(
        self,
        message: str,
        session_id: Optional[str] = None,
        product_ids: Optional[list[int]] = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"message": message}
        if session_id:
            body["session_id"] = session_id
        if product_ids:
            body["product_ids"] = list(product_ids)
        return self._request("POST", "/chat", body)

    def summary(self, session_id: str, result_product_ids: Optional[list[int]] = None) -> dict[str, Any]:
        """Optional model summary of the session's latest search results (POST /chat/summary)."""
        body: dict[str, Any] = {"session_id": session_id}
        if result_product_ids is not None:
            body["result_product_ids"] = list(result_product_ids)
        return self._request("POST", "/chat/summary", body)

    def product(self, product_id: int) -> dict[str, Any]:
        return self._request("GET", f"/products/{int(product_id)}")

    def clear_filter(self, session_id: str, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/sessions/{quote(session_id, safe='')}/filters/{quote(name, safe='')}")

    def fetch_image(self, url: str) -> Optional[tuple[bytes, str]]:
        """Fetch a remote product image server-side. Returns None on any failure.

        Only called when remote images are enabled. Accepts http(s) URLs that
        return an ``image/*`` content type between 1.5 KB and 2 MB.
        """
        if not url.lower().startswith(("http://", "https://")):
            return None
        try:
            resp = self.http.get(url, timeout=5, stream=True)
            mime = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if resp.status_code != 200 or not mime.startswith("image/"):
                return None
            data = resp.raw.read(MAX_IMAGE_BYTES + 1, decode_content=True)
            if not data or len(data) < MIN_IMAGE_BYTES or len(data) > MAX_IMAGE_BYTES:
                return None
            return data, mime
        except Exception:
            return None
