"""
Application settings loaded from environment variables and .env file.

Uses pydantic-settings for type-safe configuration with validation.
All settings have sensible defaults so the app can start without a .env file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains pyproject.toml)."""
    current = Path(__file__).resolve().parent
    for parent in [current] + list(current.parents):
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: two levels up from src/config/
    return Path(__file__).resolve().parent.parent.parent


PROJECT_ROOT = _find_project_root()


class Settings(BaseSettings):
    """Self Search application settings.

    Values are loaded in order of priority:
    1. Environment variables (highest)
    2. .env file
    3. Defaults defined here (lowest)
    """

    model_config = SettingsConfigDict(
        env_prefix="SHELFSENSE_",
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---
    db_path: str = "data/processed/shelfsense.db"
    data_raw_dir: str = "data/raw"
    data_processed_dir: str = "data/processed"
    models_dir: str = "models"
    # Directory holding products.index / reviews.index / faiss.index and manifest.json.
    # Relative paths resolve against the working directory, as they always have.
    vector_store_dir: str = "data/vector_store"

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "console"

    # --- LLM (Phase 5+) ---
    ollama_base_url: str = "http://localhost:11434"
    # Must equal a name from `ollama list`. llama3.2:3b is a 3B instruct model (spec: 3B-8B).
    ollama_model: str = "llama3.2:3b"
    # Model-time budget per chat message: LLM query parsing, the answer and any JSON
    # retry all share it (src/llm/deadline.py). When it runs out the answer uses the safe
    # evidence-based fallback. 110 s keeps every reply inside the UI's 120 s request
    # timeout (frontend/config.py), so the UI gets an answer instead of an error.
    ollama_timeout: int = 110
    # Upper bound on generated tokens per call (a brief answer needs about 300-450).
    ollama_num_predict: int = 700
    # Keep the model loaded between requests, so a pause does not add a reload.
    ollama_keep_alive: str = "30m"
    # Query understanding. "rules_first": the deterministic parser handles every message
    # it can safely handle and the LLM is asked only for the rest (brand cues), which
    # saves a model call per turn. "llm_first": always ask the LLM, rules as fallback.
    query_parser_mode: Literal["rules_first", "llm_first"] = "rules_first"

    # --- Embeddings (Phase 3+) ---
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # --- Retrieval (Phase 4+) ---
    retrieval_mode: Literal["semantic", "keyword", "hybrid", "reranked_hybrid"] = "hybrid"
    retrieval_top_k: int = 10

    # --- Server (Phase 7+) ---
    api_host: str = "127.0.0.1"
    api_port: int = 8000

    # --- Privacy ---
    load_remote_images: bool = True

    # --- Synthetic INR price estimates (experimental; see src/pricing) ---
    # Off by default. When off, estimates are never shown or used for budgets.
    estimated_prices_enabled: bool = False
    price_estimate_config: str = "config/price_estimate_ranges.toml"

    # --- Documented exchange rates (2026-10-23; see src/pricing/fx.py) ---
    # Every price is shown in INR. A non-INR source price is converted only with a rate
    # from this file; without one it is shown as "Price unavailable", never in its own currency.
    fx_rate_config: str = "config/exchange_rates.toml"

    def resolve_path(self, relative_path: str) -> Path:
        """Resolve a relative path against the project root.

        Args:
            relative_path: Path relative to the project root.

        Returns:
            Absolute path resolved against PROJECT_ROOT.
        """
        return PROJECT_ROOT / relative_path

    @property
    def resolved_db_path(self) -> Path:
        """Absolute path to the SQLite database."""
        return self.resolve_path(self.db_path)

    @property
    def resolved_data_raw_dir(self) -> Path:
        """Absolute path to the raw data directory."""
        return self.resolve_path(self.data_raw_dir)

    @property
    def resolved_data_processed_dir(self) -> Path:
        """Absolute path to the processed data directory."""
        return self.resolve_path(self.data_processed_dir)

    @property
    def resolved_models_dir(self) -> Path:
        """Absolute path to the models directory."""
        return self.resolve_path(self.models_dir)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Call this instead of constructing Settings() directly to avoid
    re-reading the .env file on every access.
    """
    return Settings()
