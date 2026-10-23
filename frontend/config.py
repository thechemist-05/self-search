"""Frontend configuration, read from environment variables (Phase 9).

The API key is used only by the Streamlit server process when it calls the
Self Search API. It is never rendered into the page or sent to the browser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class FrontendConfig:
    """Settings for the Streamlit UI."""

    api_url: str = "http://127.0.0.1:8000"
    api_key: Optional[str] = None
    load_remote_images: bool = False
    timeout_s: float = 120.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "FrontendConfig":
        """Build config from ``SHELFSENSE_API_URL``, ``SHELFSENSE_API_KEY`` (or ``API_KEY``)
        and ``SHELFSENSE_LOAD_REMOTE_IMAGES`` (remote images only when explicitly true)."""
        return cls(
            api_url=env.get("SHELFSENSE_API_URL", cls.api_url).rstrip("/"),
            api_key=env.get("SHELFSENSE_API_KEY") or env.get("API_KEY") or None,
            load_remote_images=env.get("SHELFSENSE_LOAD_REMOTE_IMAGES", "").strip().lower() in _TRUE,
        )
