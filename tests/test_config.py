"""Tests for config.py Settings loading and validation."""

import os
import pytest
from config import Settings


class TestSettingsFromEnv:
    """Test Settings.from_env() loads and validates environment variables."""

    def test_loads_bot_token_from_env(self, monkeypatch):
        """Settings.from_env() reads BOT_TOKEN from environment."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        settings = Settings.from_env()
        assert settings.BOT_TOKEN == "test-bot-token-123"

    def test_loads_twogis_api_key_from_env(self, monkeypatch):
        """Settings.from_env() reads TWOGIS_API_KEY from environment."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        settings = Settings.from_env()
        assert settings.TWOGIS_API_KEY == "test-api-key-456"

    def test_raises_valueerror_when_bot_token_missing(self, monkeypatch):
        """Settings.from_env() raises ValueError with clear message if BOT_TOKEN is missing."""
        monkeypatch.delenv("BOT_TOKEN", raising=False)
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        with pytest.raises(ValueError, match="BOT_TOKEN"):
            Settings.from_env()

    def test_raises_valueerror_when_twogis_api_key_missing(self, monkeypatch):
        """Settings.from_env() raises ValueError with clear message if TWOGIS_API_KEY is missing."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.delenv("TWOGIS_API_KEY", raising=False)
        with pytest.raises(ValueError, match="TWOGIS_API_KEY"):
            Settings.from_env()

    def test_has_default_twogis_base_url(self, monkeypatch):
        """Settings has a default TWOGIS_BASE_URL."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        settings = Settings.from_env()
        assert settings.TWOGIS_BASE_URL == "https://catalog.api.2gis.com/3.0/items"

    def test_has_default_page_size(self, monkeypatch):
        """Settings has a default PAGE_SIZE of 50."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        settings = Settings.from_env()
        assert settings.PAGE_SIZE == 50

    def test_has_default_request_delay(self, monkeypatch):
        """Settings has a default REQUEST_DELAY of 0.3."""
        monkeypatch.setenv("BOT_TOKEN", "test-bot-token-123")
        monkeypatch.setenv("TWOGIS_API_KEY", "test-api-key-456")
        settings = Settings.from_env()
        assert settings.REQUEST_DELAY == 0.3
