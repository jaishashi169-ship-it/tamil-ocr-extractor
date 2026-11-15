from typing import Any, Callable, List, Optional
from fastapi import BackgroundTasks, HTTPException, status

from app.celery_app import celery_app
from app.config import get_settings


def dispatch_celery_or_fallback(
    background_tasks: BackgroundTasks,
    celery_task: Any = None,
    args: Optional[List[Any]] = None,
    sync_fn: Optional[Callable[..., Any]] = None,
    on_dispatched: Optional[Callable[[], None]] = None,
    on_fallback: Optional[Callable[[], None]] = None,
    on_failed: Optional[Callable[[], None]] = None,
    resource_id: str = "",
    task_name: Optional[str] = None,
    sync_fn_name: Optional[str] = None,
) -> str:
    """
    Dispatch work to the durable Celery queue with bounded Redis timeouts.
    Falls back to FastAPI BackgroundTasks ONLY when APP_ENV != 'production'
    and ALLOW_BACKGROUND_TASK_FALLBACK is explicitly enabled.
    Supports task_name/sync_fn_name so lightweight API services do not import app.tasks.
    """
    call_args = args or []
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
        if celery_task is not None:
            celery_task.apply_async(args=call_args, retry=False)
        elif task_name:
            celery_app.send_task(task_name, args=call_args, retry=False)
        else:
            raise ValueError("Either celery_task or task_name must be provided")
        if on_dispatched:
            on_dispatched()
        return "dispatched"
    except Exception:
        if allow_fallback:
            resolved_sync_fn = sync_fn
            if resolved_sync_fn is None and sync_fn_name:
                from app import tasks as _tasks

                resolved_sync_fn = getattr(_tasks, sync_fn_name)
            if resolved_sync_fn is not None:
                if on_fallback:
                    on_fallback()
                background_tasks.add_task(resolved_sync_fn, *call_args)
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
