"""Tests for FRONTEND_ORIGINS CORS origin parsing."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.main import _cors_allowed_origins


class TestCorsAllowedOrigins:
    def test_parses_comma_separated_origins(self) -> None:
        settings = MagicMock()
        settings.FRONTEND_ORIGINS = "https://a.example,https://b.example"
        with patch("app.main.get_settings", return_value=settings):
            assert _cors_allowed_origins() == [
                "https://a.example",
                "https://b.example",
            ]

    def test_strips_whitespace_and_drops_empty_entries(self) -> None:
        settings = MagicMock()
        settings.FRONTEND_ORIGINS = " http://localhost:3000 , ,, https://x.example ,"
        with patch("app.main.get_settings", return_value=settings):
            assert _cors_allowed_origins() == [
                "http://localhost:3000",
                "https://x.example",
            ]

    def test_empty_setting_yields_no_origins(self) -> None:
        settings = MagicMock()
        settings.FRONTEND_ORIGINS = ""
        with patch("app.main.get_settings", return_value=settings):
            assert _cors_allowed_origins() == []

    def test_settings_validation_failure_yields_no_origins(self) -> None:
        from pydantic import ValidationError

        def _raise() -> MagicMock:
            raise ValidationError.from_exception_data(
                "Settings",
                [{"type": "missing", "loc": ("SUPABASE_URL",), "input": {}}],
            )

        with patch("app.main.get_settings", side_effect=_raise):
            assert _cors_allowed_origins() == []
