import socket
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional
from fastapi import BackgroundTasks, HTTPException, status

from app.celery_app import celery_app
from app.config import get_settings


def _redis_kwargs_for_url(url: str, connect_timeout: float = 0.5, socket_timeout: float = 2.0) -> Dict[str, Any]:
    return {
        "socket_connect_timeout": connect_timeout,
        "socket_timeout": socket_timeout,
    }


def is_broker_reachable(broker_url: str, connect_timeout: float = 0.5, socket_timeout: float = 1.0) -> bool:
    """Fast broker reachability check that avoids multi-second Windows IPv6 localhost delays when local Redis is not running."""
    if not broker_url:
        return False
    try:
        parsed = urlparse(broker_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port or 6379
        if host in ("localhost", "127.0.0.1", "::1"):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.05)
                if sock.connect_ex(("127.0.0.1", port)) != 0:
                    return False
        import redis

        client = redis.Redis.from_url(
            broker_url,
            **_redis_kwargs_for_url(broker_url, connect_timeout, socket_timeout),
        )
        return bool(client.ping())
    except Exception:
        return False


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
        if not is_broker_reachable(
            current_settings.CELERY_BROKER_URL,
            current_settings.REDIS_SOCKET_CONNECT_TIMEOUT_SECONDS,
            current_settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        ):
            raise ConnectionError("Redis broker unreachable")
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
