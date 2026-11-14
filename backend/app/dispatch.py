from typing import Any, Callable, List, Optional
from fastapi import BackgroundTasks, HTTPException, status

from app.config import get_settings


def dispatch_celery_or_fallback(
    background_tasks: BackgroundTasks,
    celery_task: Any,
    args: List[Any],
    sync_fn: Callable[..., Any],
    on_dispatched: Optional[Callable[[], None]] = None,
    on_fallback: Optional[Callable[[], None]] = None,
    on_failed: Optional[Callable[[], None]] = None,
    resource_id: str = "",
) -> str:
    """
    Dispatch work to the durable Celery queue with bounded Redis timeouts.
    Falls back to FastAPI BackgroundTasks ONLY when APP_ENV != 'production'
    and ALLOW_BACKGROUND_TASK_FALLBACK is explicitly enabled.
    """
    current_settings = get_settings()
    allow_fallback = (
        current_settings.APP_ENV != "production"
        and current_settings.ALLOW_BACKGROUND_TASK_FALLBACK
    )
    try:
        import redis

        redis_client = redis.Redis.from_url(
            current_settings.CELERY_BROKER_URL,
            socket_connect_timeout=current_settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=current_settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        redis_client.ping()
        celery_task.apply_async(args=args, retry=False)
        if on_dispatched:
            on_dispatched()
        return "dispatched"
    except Exception:
        if allow_fallback:
            if on_fallback:
                on_fallback()
            background_tasks.add_task(sync_fn, *args)
            return "fallback"

        if on_failed:
            on_failed()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "QUEUE_UNAVAILABLE",
                "retryable": True,
                "resource_id": resource_id,
                "job_id": resource_id,
                "message": "Durable job queue is unavailable. Please retry shortly.",
            },
            headers={"Retry-After": "5"},
        )
