"""Unit tests for auto.config — environment variable parsing and _int_env helper."""

import importlib
import sys

import pytest


def reload_config(monkeypatch, env_overrides):
    """Reload auto.config with the given environment variable overrides applied."""
    for key, value in env_overrides.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)

    # Force a fresh import so module-level constants are re-evaluated.
    import auto.config as cfg_module
    importlib.reload(cfg_module)
    return cfg_module


class TestIntEnvHelper:
    """Tests for the _int_env helper function directly."""

    def test_valid_int_returns_integer(self, monkeypatch):
        monkeypatch.setenv("AUTO_TEST_VAR", "42")
        from auto.config import _int_env
        assert _int_env("AUTO_TEST_VAR", "10") == 42

    def test_missing_env_uses_default(self, monkeypatch):
        monkeypatch.delenv("AUTO_TEST_VAR", raising=False)
        from auto.config import _int_env
        assert _int_env("AUTO_TEST_VAR", "10") == 10

    def test_invalid_env_falls_back_to_default(self, monkeypatch, capsys):
        monkeypatch.setenv("AUTO_TEST_VAR", "abc")
        from auto.config import _int_env
        result = _int_env("AUTO_TEST_VAR", "10")
        assert result == 10

    def test_invalid_env_prints_warning_to_stderr(self, monkeypatch, capsys):
        monkeypatch.setenv("AUTO_TEST_VAR", "not_a_number")
        from auto.config import _int_env
        _int_env("AUTO_TEST_VAR", "7")
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "AUTO_TEST_VAR" in captured.err
        assert "not_a_number" in captured.err
        assert "7" in captured.err

    def test_warning_not_printed_for_valid_value(self, monkeypatch, capsys):
        monkeypatch.setenv("AUTO_TEST_VAR", "5")
        from auto.config import _int_env
        _int_env("AUTO_TEST_VAR", "5")
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_warning_not_printed_for_missing_value(self, monkeypatch, capsys):
        monkeypatch.delenv("AUTO_TEST_VAR", raising=False)
        from auto.config import _int_env
        _int_env("AUTO_TEST_VAR", "5")
        captured = capsys.readouterr()
        assert captured.err == ""

    def test_return_type_is_int(self, monkeypatch):
        monkeypatch.setenv("AUTO_TEST_VAR", "99")
        from auto.config import _int_env
        result = _int_env("AUTO_TEST_VAR", "1")
        assert isinstance(result, int)

    def test_fallback_return_type_is_int(self, monkeypatch):
        monkeypatch.setenv("AUTO_TEST_VAR", "bad")
        from auto.config import _int_env
        result = _int_env("AUTO_TEST_VAR", "1")
        assert isinstance(result, int)


class TestDirectorInterval:
    """Tests for the DIRECTOR_INTERVAL config constant."""

    def test_valid_value_is_used(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_DIRECTOR_INTERVAL": "10"})
        assert cfg.DIRECTOR_INTERVAL == 10

    def test_missing_uses_default(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_DIRECTOR_INTERVAL": None})
        assert cfg.DIRECTOR_INTERVAL == 5

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        cfg = reload_config(monkeypatch, {"AUTO_DIRECTOR_INTERVAL": "abc"})
        assert cfg.DIRECTOR_INTERVAL == 5
        captured = capsys.readouterr()
        assert "AUTO_DIRECTOR_INTERVAL" in captured.err


class TestIdeasPerBatch:
    """Tests for the IDEAS_PER_BATCH config constant."""

    def test_valid_value_is_used(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_IDEAS_PER_BATCH": "7"})
        assert cfg.IDEAS_PER_BATCH == 7

    def test_missing_uses_default(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_IDEAS_PER_BATCH": None})
        assert cfg.IDEAS_PER_BATCH == 3

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        cfg = reload_config(monkeypatch, {"AUTO_IDEAS_PER_BATCH": "xyz"})
        assert cfg.IDEAS_PER_BATCH == 3
        captured = capsys.readouterr()
        assert "AUTO_IDEAS_PER_BATCH" in captured.err


class TestDefaultTimeBudget:
    """Tests for the DEFAULT_TIME_BUDGET config constant."""

    def test_valid_value_is_used(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_TIME_BUDGET": "600"})
        assert cfg.DEFAULT_TIME_BUDGET == 600

    def test_missing_uses_default(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_TIME_BUDGET": None})
        assert cfg.DEFAULT_TIME_BUDGET == 300

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        cfg = reload_config(monkeypatch, {"AUTO_TIME_BUDGET": "not_an_int"})
        assert cfg.DEFAULT_TIME_BUDGET == 300
        captured = capsys.readouterr()
        assert "AUTO_TIME_BUDGET" in captured.err


class TestDefaultMaxExperiments:
    """Tests for the DEFAULT_MAX_EXPERIMENTS config constant."""

    def test_valid_value_is_used(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_MAX_EXPERIMENTS": "50"})
        assert cfg.DEFAULT_MAX_EXPERIMENTS == 50

    def test_missing_uses_default(self, monkeypatch):
        cfg = reload_config(monkeypatch, {"AUTO_MAX_EXPERIMENTS": None})
        assert cfg.DEFAULT_MAX_EXPERIMENTS == 0

    def test_invalid_falls_back_to_default(self, monkeypatch, capsys):
        cfg = reload_config(monkeypatch, {"AUTO_MAX_EXPERIMENTS": "!!!"})
        assert cfg.DEFAULT_MAX_EXPERIMENTS == 0
        captured = capsys.readouterr()
        assert "AUTO_MAX_EXPERIMENTS" in captured.err
