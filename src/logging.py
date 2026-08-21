"""
src/logging.py — Structured JSON logging configuration.

Every module gets a logger via:

    from src.logging import get_logger
    log = get_logger(__name__)

Log records include the standard structlog fields (timestamp, level, event)
plus any key=value pairs bound by the caller:

    log.info("issue triaged", repo_id="owner/repo", issue_number=42)

In Phase 3, these JSON logs will be correlated by delivery_id, repo_id, and
issue_number in whatever log aggregation stack is in use (e.g. Loki).
"""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    """
    Call once at process startup (e.g. in __main__) to configure structlog.

    Uses JSON rendering in production-like contexts and pretty console output
    when the output is a TTY (i.e. your terminal during development).
    """
    is_tty = sys.stderr.isatty()

    structlog.configure(
        processors=[
            # Add log level to every event dict
            structlog.stdlib.add_log_level,
            # Add ISO-8601 timestamp
            structlog.processors.TimeStamper(fmt="iso"),
            # Render as pretty colored output in a terminal, JSON otherwise
            structlog.dev.ConsoleRenderer() if is_tty else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a bound structlog logger for the given module name."""
    return structlog.get_logger(name)
