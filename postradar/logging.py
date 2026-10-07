"""Standard-library logging configuration."""

import logging


def configure_logging(level: str) -> None:
    """Configure root logging using a level name from application settings."""
    resolved_level = getattr(logging, level.upper(), None)
    if not isinstance(resolved_level, int):
        raise ValueError(f"Invalid logging level: {level!r}")

    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
