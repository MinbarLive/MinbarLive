"""Credential-redaction contracts for application logs."""

from utils import logging as app_logging


def test_redact_sensitive_text_covers_provider_and_authorization_shapes():
    openai_key = "sk-proj-secret-runtime-value-123456"
    google_key = "AIzaSecretRuntimeValue1234567890"
    bearer = "unprefixedDeepgramToken123456789"

    result = app_logging.redact_sensitive_text(
        f"{openai_key} {google_key} Authorization: Bearer {bearer}"
    )

    assert openai_key not in result
    assert google_key not in result
    assert bearer not in result
    assert result.count("[REDACTED]") == 3


def test_new_format_google_keys_are_redacted():
    # Google issues "AQ.Ab..." keys alongside the legacy "AIza..." ones, and
    # only the latter used to be covered — so a current Gemini key reached the
    # log in the clear unless it followed an "api_key="-style label.
    key = "AQ.Ab8RN6Jsecret_runtime-value.1234567890"

    result = app_logging.redact_sensitive_text(f"gemini rejected {key}")

    assert key not in result
    assert "[REDACTED]" in result


def test_redaction_leaves_ordinary_text_alone():
    # The shape rules must not eat real log content: a commit SHA looks exactly
    # like a Deepgram key, which is why those are matched only when labelled.
    plain = "merged 6388884abc1234567890deadbeefcafe12345678 into main"

    assert app_logging.redact_sensitive_text(plain) == plain


def test_log_redacts_before_queue_and_disk(monkeypatch):
    written = []
    monkeypatch.setattr(app_logging, "_write_to_file", written.append)
    while not app_logging.log_queue.empty():
        app_logging.log_queue.get_nowait()
    secret = "sk-ant-secret-runtime-value-123456"

    app_logging.log(f"provider rejected {secret}", level="ERROR")

    queued = app_logging.log_queue.get_nowait()
    assert secret not in queued
    assert "[REDACTED]" in queued
    assert written == [queued]


def test_server_masked_openai_key_is_redacted_too():
    masked = "sk-proj-****WXYZ"

    result = app_logging.redact_sensitive_text(f"OpenAI rejected {masked}")

    assert masked not in result
    assert "[REDACTED]" in result
