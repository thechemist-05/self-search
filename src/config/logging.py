"""
Structured logging setup for Self Search.

Provides JSON or human-readable console logging via structlog.
All log entries include timestamps, log level, and the logger name.
"""

from __future__ import annotations

import logging
import sys

import structlog


def make_console_output_safe() -> None:
    """Never let a log line crash a request because the console can't encode it.

    On Windows, stdout/stderr (a console or a redirected log file) use a legacy code
    page such as cp1252, which cannot encode e.g. "\u20b9". Logging a user's message
    containing it raised UnicodeEncodeError inside the request (HTTP 500). Unencodable
    characters are written as backslash escapes instead; nothing else changes.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # closed or non-reconfigurable stream
                pass


def setup_logging(
    level: str = "INFO",
    log_format: str = "console",
) -> structlog.stdlib.BoundLogger:
    """Initialise structured logging for the application.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_format: Output format — 'json' for machine-readable,
                    'console' for human-readable coloured output.

    Returns:
        A configured structlog BoundLogger ready for use.
    """
    # Shared processors for both formats
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if log_format == "json":
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the standard library root logger so structlog output appears
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    logger: structlog.stdlib.BoundLogger = structlog.get_logger("shelfsense")
    logger.info("logging_initialised", level=level, format=log_format)
    return logger
