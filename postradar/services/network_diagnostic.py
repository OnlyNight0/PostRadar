"""One-shot outbound TCP checks for staging diagnostics."""

import logging
import socket
from collections.abc import Callable

logger = logging.getLogger("postradar.network_diagnostic")

_TARGETS = (
    ("api.telegram.org", 443),
    ("149.154.175.60", 443),
    ("149.154.167.51", 443),
)
_TIMEOUT_SECONDS = 5.0


def run_network_diagnostic(
    *,
    socket_factory: Callable[..., socket.socket] = socket.create_connection,
) -> None:
    """Log the TCP reachability of Telegram's Bot API and MTProto endpoints."""
    logger.info("NETWORK DIAGNOSTIC START")
    try:
        for host, port in _TARGETS:
            _probe_target(host, port, socket_factory)
    except Exception as error:
        # Keep unexpected instrumentation problems from interrupting startup.
        logger.warning(
            "NETWORK DIAGNOSTIC ERROR (%s)",
            type(error).__name__,
        )
    finally:
        logger.info("NETWORK DIAGNOSTIC END")


def _probe_target(
    host: str,
    port: int,
    socket_factory: Callable[..., socket.socket],
) -> None:
    """Probe one endpoint and contain expected network errors per target."""
    try:
        connection = socket_factory((host, port), timeout=_TIMEOUT_SECONDS)
    except TimeoutError:
        logger.warning("NETWORK TEST %s:%s -> TIMEOUT", host, port)
    except OSError as error:
        logger.warning(
            "NETWORK TEST %s:%s -> FAILED (%s)",
            host,
            port,
            type(error).__name__,
        )
    else:
        try:
            logger.info("NETWORK TEST %s:%s -> OK", host, port)
        finally:
            connection.close()
