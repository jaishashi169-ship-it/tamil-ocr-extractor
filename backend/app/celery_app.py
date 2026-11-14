from celery import Celery
from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "tamil_doc_studio",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_time_limit=settings.JOB_TIMEOUT_SECONDS,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Allow Celery workers to reconnect cleanly after Redis startup delays or outages;
    # API publish paths use bounded timeouts and retry=False explicitly.
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=None,
    broker_transport_options={
        "socket_connect_timeout": settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
        "socket_timeout": settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        "retry_on_timeout": False,
        "visibility_timeout": max(3600, settings.JOB_TIMEOUT_SECONDS + settings.JOB_LEASE_SECONDS),
    },
    redis_socket_connect_timeout=settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
    redis_socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
)
