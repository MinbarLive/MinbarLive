"""Retry utilities with exponential backoff for API calls.

Handles transient API failures with configurable retry behavior.
"""

from __future__ import annotations

import time
import random
from typing import TypeVar, Callable, Any

from utils.logging import log

# Default retry configuration
DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 30.0  # seconds
DEFAULT_EXPONENTIAL_BASE = 2

# Exceptions that should trigger a retry (transient errors)
# These are imported lazily to avoid circular imports
RETRYABLE_EXCEPTIONS = (
    "RateLimitError",
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "ServiceUnavailableError",
)

T = TypeVar("T")


def _is_retryable_exception(exc: Exception) -> bool:
    """Check if an exception is retryable based on its class name."""
    exc_name = type(exc).__name__
    return exc_name in RETRYABLE_EXCEPTIONS


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    operation_name: str = "API call",
    **kwargs: Any,
) -> T:
    """
    Execute a function with exponential backoff retry logic.

    Args:
        func: The function to execute.
        *args: Positional arguments to pass to the function.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay between retries in seconds.
        max_delay: Maximum delay between retries in seconds.
        operation_name: Name of the operation for logging.
        **kwargs: Keyword arguments to pass to the function.

    Returns:
        The return value of the function.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exception = e

            if not _is_retryable_exception(e):
                # Non-retryable error, raise immediately
                log(
                    f"{operation_name} failed with non-retryable error: {e}",
                    level="ERROR",
                )
                raise

            if attempt >= max_retries:
                # Exhausted all retries
                log(
                    f"{operation_name} failed after {max_retries + 1} attempts: {e}",
                    level="ERROR",
                )
                raise

            # Calculate delay with exponential backoff and jitter
            delay = min(
                base_delay * (DEFAULT_EXPONENTIAL_BASE**attempt),
                max_delay,
            )
            # Add jitter (±25%) to prevent thundering herd
            jitter = delay * 0.25 * (2 * random.random() - 1)
            delay = max(0.1, delay + jitter)

            log(
                f"{operation_name} failed (attempt {attempt + 1}/{max_retries + 1}): {e}. "
                f"Retrying in {delay:.1f}s...",
                level="WARNING",
            )
            time.sleep(delay)

    # Should never reach here, but satisfy type checker
    if last_exception:
        raise last_exception
    raise RuntimeError(f"{operation_name} failed unexpectedly")
