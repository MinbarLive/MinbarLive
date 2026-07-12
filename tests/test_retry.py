"""Tests for retry utilities."""

import pytest
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.retry import (
    retry_with_backoff,
    _is_retryable_exception,
)


class MockRateLimitError(Exception):
    """Mock OpenAI RateLimitError."""

    pass


class MockAPIConnectionError(Exception):
    """Mock OpenAI APIConnectionError."""

    pass


class MockAuthenticationError(Exception):
    """Mock OpenAI AuthenticationError (non-retryable)."""

    pass


# Monkey-patch the class names to match what retry logic checks
MockRateLimitError.__name__ = "RateLimitError"
MockAPIConnectionError.__name__ = "APIConnectionError"
MockAuthenticationError.__name__ = "AuthenticationError"


class TestIsRetryableException:
    """Tests for exception classification."""

    def test_rate_limit_is_retryable(self):
        """RateLimitError should be retryable."""
        exc = MockRateLimitError("rate limited")
        assert _is_retryable_exception(exc) is True

    def test_connection_error_is_retryable(self):
        """APIConnectionError should be retryable."""
        exc = MockAPIConnectionError("connection failed")
        assert _is_retryable_exception(exc) is True

    def test_auth_error_not_retryable(self):
        """AuthenticationError should not be retryable."""
        exc = MockAuthenticationError("invalid key")
        assert _is_retryable_exception(exc) is False

    def test_generic_exception_not_retryable(self):
        """Generic exceptions should not be retryable."""
        exc = ValueError("some error")
        assert _is_retryable_exception(exc) is False


class TestRetryWithBackoff:
    """Tests for retry_with_backoff function."""

    def test_success_on_first_try(self):
        """Should return result immediately on success."""
        func = MagicMock(return_value="success")

        result = retry_with_backoff(func, max_retries=3, operation_name="Test")

        assert result == "success"
        assert func.call_count == 1

    def test_success_after_retry(self):
        """Should succeed after transient failure."""
        func = MagicMock(side_effect=[MockRateLimitError("rate limited"), "success"])

        with patch("utils.retry.time.sleep"):  # Skip actual delays
            result = retry_with_backoff(
                func, max_retries=3, base_delay=0.01, operation_name="Test"
            )

        assert result == "success"
        assert func.call_count == 2

    def test_exhausts_retries(self):
        """Should raise after exhausting all retries."""
        func = MagicMock(side_effect=MockRateLimitError("rate limited"))

        with patch("utils.retry.time.sleep"):  # Skip actual delays
            with pytest.raises(MockRateLimitError):
                retry_with_backoff(
                    func, max_retries=2, base_delay=0.01, operation_name="Test"
                )

        # Initial attempt + 2 retries = 3 total calls
        assert func.call_count == 3

    def test_non_retryable_fails_immediately(self):
        """Should not retry non-retryable errors."""
        func = MagicMock(side_effect=MockAuthenticationError("invalid key"))

        with pytest.raises(MockAuthenticationError):
            retry_with_backoff(func, max_retries=3, operation_name="Test")

        # Should fail on first attempt without retry
        assert func.call_count == 1

    def test_passes_args_and_kwargs(self):
        """Should pass arguments to the wrapped function."""
        func = MagicMock(return_value="result")

        result = retry_with_backoff(
            func, "arg1", "arg2", kwarg1="value1", max_retries=3, operation_name="Test"
        )

        func.assert_called_once_with("arg1", "arg2", kwarg1="value1")
        assert result == "result"


class TestRetryTiming:
    """Tests for retry timing behavior."""

    def test_exponential_backoff(self):
        """Delays should increase exponentially."""
        delays = []

        def mock_sleep(duration: float) -> None:
            delays.append(duration)

        func = MagicMock(
            side_effect=[
                MockRateLimitError("1"),
                MockRateLimitError("2"),
                MockRateLimitError("3"),
            ]
        )

        with patch("utils.retry.time.sleep", side_effect=mock_sleep):
            with patch("utils.retry.random.random", return_value=0.5):  # No jitter
                with pytest.raises(MockRateLimitError):
                    retry_with_backoff(
                        func,
                        max_retries=2,
                        base_delay=1.0,
                        max_delay=30.0,
                        operation_name="Test",
                    )

        # Should have 2 delays (after 1st and 2nd failures)
        assert len(delays) == 2
        # First delay should be ~1.0s (base_delay * 2^0)
        # Second delay should be ~2.0s (base_delay * 2^1)
        # With jitter at 0.5, jitter factor is 0, so delays are exact
        assert 0.9 <= delays[0] <= 1.1
        assert 1.9 <= delays[1] <= 2.1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
