"""Wall-clock deadlines shared by the UI and isolated provider adapters."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import threading
import time

NETWORK_TIMEOUT_SEC = 15.0
_deadline = ContextVar('network_deadline', default=None)
_progress = ContextVar('analysis_progress', default=None)
_lock = threading.Lock()
_pending = {}
_slots = threading.BoundedSemaphore(16)


def remaining_timeout(timeout=NETWORK_TIMEOUT_SEC):
    deadline = _deadline.get()
    remaining = timeout if deadline is None else min(timeout, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError('网络超时：本次分析的联网等待已达15秒')
    return remaining


@contextmanager
def network_budget(seconds=NETWORK_TIMEOUT_SEC):
    current = _deadline.get()
    deadline = time.monotonic() + seconds
    token = _deadline.set(min(current, deadline) if current is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(token)


def network_limited(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with network_budget():
            return function(*args, **kwargs)
    return wrapped


@contextmanager
def analysis_progress(callback):
    token = _progress.set(callback)
    try:
        yield
    finally:
        _progress.reset(token)


def report_progress(message):
    callback = _progress.get()
    if callback:
        callback(message)


def bounded_call(function, *, timeout=None, key=None):
    """Wait on a daemon, reuse in-flight work, and cap abandoned workers.

    A Python thread cannot be killed. Provider subprocesses additionally have
    their own kill timeout; remaining daemon requests never hold up shutdown.
    """
    wait = remaining_timeout(NETWORK_TIMEOUT_SEC if timeout is None else timeout)
    key = key if key is not None else object()
    with _lock:
        task = _pending.get(key)
        if task is None:
            if not _slots.acquire(blocking=False):
                raise TimeoutError('网络超时：后台请求繁忙，请稍后重试')
            task = {'event': threading.Event()}
            _pending[key] = task
            context = copy_context()

            def run():
                try:
                    task['value'] = context.run(function)
                except BaseException as exc:
                    task['error'] = exc
                finally:
                    with _lock:
                        _pending.pop(key, None)
                    _slots.release()
                    task['event'].set()

            threading.Thread(target=run, daemon=True, name='bounded-provider').start()
    if not task['event'].wait(wait):
        raise TimeoutError(f'网络超时：请求等待超过{wait:g}秒')
    if 'error' in task:
        raise task['error']
    return task['value']
