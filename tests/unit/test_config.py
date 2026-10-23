"""Unit tests for the configuration and logging system."""

from __future__ import annotations

import os
import logging

import pytest
import structlog

from src.config.settings import Settings, get_settings, PROJECT_ROOT
from src.config.logging import setup_logging


class TestSettings:
    """Tests for the Settings configuration class."""

    def test_default_settings_load(self) -> None:
        """Settings class instantiates with sensible defaults without a .env file."""
        settings = Settings()
        assert settings.log_level == "INFO"
        assert settings.log_format == "console"
        assert settings.db_path == "data/processed/shelfsense.db"
        assert settings.retrieval_mode == "hybrid"
        assert settings.retrieval_top_k == 10
        assert settings.api_host == "127.0.0.1"
        assert settings.api_port == 8000
        assert settings.load_remote_images is True

    def test_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Environment variables override default settings."""
        monkeypatch.setenv("SHELFSENSE_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("SHELFSENSE_RETRIEVAL_TOP_K", "25")
        monkeypatch.setenv("SHELFSENSE_LOAD_REMOTE_IMAGES", "false")

        settings = Settings()
        assert settings.log_level == "DEBUG"
        assert settings.retrieval_top_k == 25
        assert settings.load_remote_images is False

    def test_resolve_path(self) -> None:
        """resolve_path returns an absolute path under PROJECT_ROOT."""
        settings = Settings()
        resolved = settings.resolve_path("data/processed/shelfsense.db")
        assert resolved.is_absolute()
        assert str(PROJECT_ROOT) in str(resolved)

    def test_resolved_db_path_is_absolute(self) -> None:
        """resolved_db_path property returns an absolute path."""
        settings = Settings()
        assert settings.resolved_db_path.is_absolute()

    def test_resolved_data_dirs(self) -> None:
        """Resolved data directory properties return absolute paths."""
        settings = Settings()
        assert settings.resolved_data_raw_dir.is_absolute()
        assert settings.resolved_data_processed_dir.is_absolute()
        assert settings.resolved_models_dir.is_absolute()

    def test_invalid_log_level_rejected(self) -> None:
        """Settings rejects invalid log level values."""
        with pytest.raises(Exception):
            Settings(log_level="INVALID")

    def test_invalid_retrieval_mode_rejected(self) -> None:
        """Settings rejects invalid retrieval mode values."""
        with pytest.raises(Exception):
            Settings(retrieval_mode="nonexistent")

    def test_get_settings_returns_same_instance(self) -> None:
        """get_settings() returns the same cached instance on repeated calls."""
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        get_settings.cache_clear()


class TestLogging:
    """Tests for the structured logging setup."""

    def test_logging_initialises_console(self) -> None:
        """setup_logging() with console format runs without error and returns a logger."""
        logger = setup_logging(level="DEBUG", log_format="console")
        assert logger is not None
        # Should be usable — no exception on logging
        logger.info("test_message", key="value")

    def test_logging_initialises_json(self) -> None:
        """setup_logging() with JSON format runs without error and returns a logger."""
        logger = setup_logging(level="INFO", log_format="json")
        assert logger is not None
        logger.info("test_json_message", number=42)

    def test_logging_level_respected(self) -> None:
        """Root logger level matches the configured level after setup."""
        setup_logging(level="WARNING", log_format="console")
        root = logging.getLogger()
        assert root.level == logging.WARNING


class TestProjectStructure:
    """Tests that the project structure is importable and well-formed."""

    def test_project_root_exists(self) -> None:
        """PROJECT_ROOT points to a directory containing pyproject.toml."""
        assert PROJECT_ROOT.exists()
        assert (PROJECT_ROOT / "pyproject.toml").exists()

    def test_src_importable(self) -> None:
        """The src package can be imported."""
        import src
        assert src is not None

    def test_config_importable(self) -> None:
        """The config package can be imported with its public API."""
        from src.config import Settings, get_settings, setup_logging
        assert Settings is not None
        assert callable(get_settings)
        assert callable(setup_logging)

    def test_all_subpackages_importable(self) -> None:
        """All src subpackages are importable (empty but valid)."""
        import src.data
        import src.db
        import src.reviews
        import src.embeddings
        import src.vector_store
        import src.retrieval
        import src.llm
        import src.evidence
        import src.agent
        import src.pricing
        import src.api
        import src.evaluation
