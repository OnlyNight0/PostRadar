"""Tests for temporary startup network diagnostics."""

import asyncio
import logging
import socket
from unittest.mock import Mock

import pytest

from postradar.services.network_diagnostic import run_network_diagnostic


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_successful_tcp_probes_are_logged_and_connections_closed(caplog) -> None:
    connections = [FakeConnection() for _ in range(3)]
    factory = Mock(side_effect=connections)

    with caplog.at_level(logging.INFO, logger="postradar.network_diagnostic"):
        run_network_diagnostic(socket_factory=factory)

    assert "NETWORK TEST api.telegram.org:443 -> OK" in caplog.text
    assert "NETWORK TEST 149.154.175.60:443 -> OK" in caplog.text
    assert "NETWORK TEST 149.154.167.51:443 -> OK" in caplog.text
    assert all(connection.closed for connection in connections)
    assert all(call.kwargs["timeout"] <= 5 for call in factory.call_args_list)


def test_timeout_is_logged_and_remaining_targets_are_checked(caplog) -> None:
    first = FakeConnection()
    factory = Mock(side_effect=[TimeoutError("slow"), first, OSError("unavailable")])

    with caplog.at_level(logging.INFO, logger="postradar.network_diagnostic"):
        run_network_diagnostic(socket_factory=factory)

    assert "NETWORK TEST api.telegram.org:443 -> TIMEOUT" in caplog.text
    assert "NETWORK TEST 149.154.175.60:443 -> OK" in caplog.text
    assert "NETWORK TEST 149.154.167.51:443 -> FAILED (OSError)" in caplog.text
    assert factory.call_count == 3
    assert first.closed


def test_unexpected_probe_error_does_not_escape(caplog) -> None:
    factory = Mock(side_effect=RuntimeError("internal failure"))

    with caplog.at_level(logging.INFO, logger="postradar.network_diagnostic"):
        run_network_diagnostic(socket_factory=factory)

    assert "NETWORK DIAGNOSTIC ERROR (RuntimeError)" in caplog.text
    assert "NETWORK DIAGNOSTIC END" in caplog.text
    assert "internal failure" not in caplog.text


def test_diagnostic_logs_no_environment_or_session_values(caplog, monkeypatch) -> None:
    secret_values = ("test-bot-token", "test-session-string", "test-api-hash")
    for name, value in zip(
        ("BOT_TOKEN", "TELEGRAM_SESSION", "TELEGRAM_API_HASH"),
        secret_values,
    ):
        monkeypatch.setenv(name, value)
    factory = Mock(side_effect=[socket.gaierror("name resolution failed")] * 3)

    with caplog.at_level(logging.INFO, logger="postradar.network_diagnostic"):
        run_network_diagnostic(socket_factory=factory)

    for secret in secret_values:
        assert secret not in caplog.text
    assert "name resolution failed" not in caplog.text
    assert factory.call_count == 3


def test_startup_diagnostic_runs_before_source_monitor(monkeypatch) -> None:
    import main as app

    order: list[str] = []

    class Settings:
        log_level = "INFO"
        bot_token = "token"
        admin_user_id = 123
        telegram_api_id = 1
        telegram_api_hash = "hash"
        telegram_session = "session"
        database_url = "sqlite+aiosqlite:///:memory:"
        gemini_api_key = None
        gemini_primary_model = "model"
        gemini_fallback_model = "model"
        ai_edit_enabled = False
        media_dir = "media"

    class Monitor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def run(self) -> None:
            order.append("monitor")

        async def close(self) -> None:
            pass

    class Dispatcher:
        async def start_polling(self, *args, **kwargs) -> None:
            await asyncio.sleep(0)

    class Session:
        async def close(self) -> None:
            pass

    class Bind:
        async def dispose(self) -> None:
            pass

    class SessionFactory:
        kw = {"bind": Bind()}

    async def init_db(url: str) -> None:
        pass

    def create_session_factory(url: str) -> SessionFactory:
        return SessionFactory()

    def create_admin_bot(*args, **kwargs):
        return Mock(session=Session()), Dispatcher(), object()

    def run_diagnostic() -> None:
        order.append("diagnostic")

    class Editor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def close(self) -> None:
            pass

    async def review_worker(workflow) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(app, "Settings", Settings)
    monkeypatch.setattr(app, "configure_logging", lambda level: None)
    monkeypatch.setattr(app, "validate_admin_settings", lambda *args: None)
    monkeypatch.setattr(app, "validate_telegram_settings", lambda *args: None)
    monkeypatch.setattr(app, "init_db", init_db)
    monkeypatch.setattr(app, "create_session_factory", create_session_factory)
    monkeypatch.setattr(app, "AIEditor", Editor)
    monkeypatch.setattr(app, "SourceMonitor", Monitor)
    monkeypatch.setattr(app, "create_admin_bot", create_admin_bot)
    monkeypatch.setattr(app, "review_delivery_worker", review_worker)
    monkeypatch.setattr(app, "run_network_diagnostic", run_diagnostic)

    asyncio.run(app.main())

    assert order == ["diagnostic", "monitor"]
