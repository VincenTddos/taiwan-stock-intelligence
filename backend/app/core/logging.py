"""Structured logging.

Every log line is JSON on stdout, and carries whatever correlation ids are in
scope (`request_id` for HTTP, `job_run_id` / `task_name` for Celery). That is
what makes `docker logs api | jq 'select(.request_id=="...")'` a usable
debugging tool without an ELK stack.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import structlog

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_run_id_var: ContextVar[str | None] = ContextVar("job_run_id", default=None)


def _inject_context(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    if (rid := request_id_var.get()) is not None:
        event_dict.setdefault("request_id", rid)
    if (jid := job_run_id_var.get()) is not None:
        event_dict.setdefault("job_run_id", jid)
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

    # Uvicorn installs its own handlers; route them through structlog instead of
    # emitting two differently-shaped lines for the same event.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
