"""Celery application.

Queue topology (ARCHITECTURE.md §17 Q8) is declared here in Phase 1 even though
only `q_maint` is used yet — routing decisions are much harder to retrofit once
tasks exist, and an idle queue costs nothing.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import setup_logging, worker_ready, worker_shutting_down

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
log = get_logger(__name__)

celery_app = Celery(
    "twquant",
    broker=settings.redis_broker_url,
    backend=settings.redis_result_url,
    include=["app.workers.tasks.health"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.TIMEZONE,
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,  # redeliver if a worker dies mid-task
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # long tasks must not hog a prefetch buffer
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_default_queue="q_maint",
    task_queues_definitions=None,
    task_routes={
        "ingest.*": {"queue": "q_ingest"},
        "compute.*": {"queue": "q_compute"},
        "nlp.*": {"queue": "q_nlp"},
        "user.*": {"queue": "q_user"},
        "maint.*": {"queue": "q_maint"},
    },
    beat_schedule={
        "worker-heartbeat": {
            "task": "maint.heartbeat",
            "schedule": 30.0,
            "options": {"queue": "q_maint", "expires": 60},
        },
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    configure_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)


@worker_ready.connect
def _on_ready(**_: object) -> None:
    from app.workers.tasks.health import write_heartbeat

    write_heartbeat()
    log.info("celery_worker_ready", queues=list(celery_app.conf.task_routes or {}))


@worker_shutting_down.connect
def _on_shutdown(**_: object) -> None:
    log.info("celery_worker_shutting_down")
