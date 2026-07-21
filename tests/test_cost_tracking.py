"""Provider usage pricing, logical-session history and recovery tests."""

from __future__ import annotations

import json
import threading
from decimal import Decimal
from types import SimpleNamespace

from utils.cost_tracking import (
    CostTracker,
    estimate_usage_cost,
    format_usd,
    gemini_usage_values,
    openai_chat_usage_values,
    openai_transcription_usage_values,
)


def test_openai_chat_cached_tokens_use_the_cached_rate():
    cost, complete, _rates = estimate_usage_cost(
        "gpt-4o-mini",
        {
            "input_text_tokens": 900,
            "cached_input_text_tokens": 100,
            "output_text_tokens": 200,
        },
    )
    expected = (
        Decimal(900) * Decimal("0.15")
        + Decimal(100) * Decimal("0.075")
        + Decimal(200) * Decimal("0.60")
    ) / Decimal(1_000_000)
    assert cost == expected
    assert complete is True


def test_unknown_model_retains_usage_but_is_not_priced():
    cost, complete, rates = estimate_usage_cost(
        "future-model", {"input_text_tokens": 10}
    )
    assert cost == 0
    assert complete is False
    assert rates is None


def test_true_start_stop_sessions_are_separate_and_persisted(tmp_path):
    tracker = CostTracker(tmp_path)
    first = tracker.begin_session()
    tracker.record_usage(
        provider="openai",
        role="translation",
        model="gpt-4o-mini",
        usage={"input_text_tokens": 100, "output_text_tokens": 20},
    )
    tracker.end_session()
    second = tracker.begin_session()
    tracker.record_usage(
        provider="gemini",
        role="transcription",
        model="gemini-3.1-flash-lite",
        usage={"input_audio_tokens": 200, "output_text_tokens": 30},
    )
    tracker.end_session()

    records = tracker.list_sessions()
    assert {record["id"] for record in records} == {first, second}
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert records[0]["providers"] or records[1]["providers"]


def test_failed_or_empty_session_creates_no_history(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()
    tracker.end_session()
    assert tracker.list_sessions() == []
    assert list(tmp_path.glob("*.json")) == []


def test_live_snapshots_replace_physical_stream_totals(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()
    tracker.record_live_snapshot(
        stream_id="socket-1",
        provider="gemini",
        role="transcription",
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        usage={"input_audio_tokens": 100, "output_text_tokens": 10},
    )
    tracker.record_live_snapshot(
        stream_id="socket-1",
        provider="gemini",
        role="transcription",
        model="gemini-2.5-flash-native-audio-preview-12-2025",
        usage={"input_audio_tokens": 150, "output_text_tokens": 15},
    )
    usage = tracker.snapshot()["providers"]["gemini"]["usage"]
    assert usage["input_audio_tokens"] == 150
    assert usage["output_text_tokens"] == 15


def test_reconnect_streams_are_summed_in_one_logical_session(tmp_path):
    tracker = CostTracker(tmp_path)
    session_id = tracker.begin_session()
    for stream_id in ("one", "two"):
        tracker.record_live_snapshot(
            stream_id=stream_id,
            provider="gemini",
            role="transcription",
            model="gemini-2.5-flash-native-audio-preview-12-2025",
            usage={"input_audio_tokens": 100},
        )
    record = tracker.end_session()
    assert record["id"] == session_id
    assert record["providers"]["gemini"]["usage"]["input_audio_tokens"] == 200


def test_active_checkpoint_recovers_as_interrupted(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()
    tracker.record_usage(
        provider="openai",
        role="translation",
        model="gpt-4o-mini",
        usage={"input_text_tokens": 1},
    )
    tracker.flush()

    recovered = CostTracker(tmp_path).list_sessions(include_active=False)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "interrupted"
    assert recovered[0]["ended_at"]


def test_recording_is_thread_safe(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()

    def worker():
        for _ in range(100):
            tracker.record_usage(
                provider="openai",
                role="translation",
                model="gpt-4o-mini",
                usage={"input_text_tokens": 1},
            )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    provider = tracker.snapshot()["providers"]["openai"]
    assert provider["requests"] == 400
    assert provider["usage"]["input_text_tokens"] == 400


def test_openai_normalizers_do_not_double_count_details():
    chat = openai_chat_usage_values(
        SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            prompt_tokens_details=SimpleNamespace(cached_tokens=30),
        )
    )
    assert chat["input_text_tokens"] == 70
    assert chat["cached_input_text_tokens"] == 30
    assert chat["output_text_tokens"] == 20

    stt = openai_transcription_usage_values(
        SimpleNamespace(
            type="tokens",
            input_tokens=120,
            output_tokens=12,
            input_token_details=SimpleNamespace(audio_tokens=100, text_tokens=20),
        )
    )
    assert stt["input_audio_tokens"] == 100
    assert stt["input_text_tokens"] == 20
    assert stt["output_text_tokens"] == 12


def test_duration_billed_transcription_usage():
    usage = openai_transcription_usage_values(
        SimpleNamespace(type="duration", seconds=90)
    )
    cost, complete, _rates = estimate_usage_cost("whisper-1", usage)
    assert usage["duration_seconds"] == 90
    assert cost == Decimal("0.009")
    assert complete is True


def test_gemini_modality_breakdown_and_thoughts():
    metadata = SimpleNamespace(
        prompt_token_count=140,
        prompt_tokens_details=[
            SimpleNamespace(modality="TEXT", token_count=40),
            SimpleNamespace(modality="AUDIO", token_count=100),
        ],
        cached_content_token_count=10,
        cache_tokens_details=[SimpleNamespace(modality="TEXT", token_count=10)],
        candidates_token_count=20,
        candidates_tokens_details=[SimpleNamespace(modality="TEXT", token_count=20)],
        thoughts_token_count=5,
        tool_use_prompt_token_count=0,
    )
    usage = gemini_usage_values(metadata, role="transcription")
    assert usage["input_text_tokens"] == 30
    assert usage["cached_input_text_tokens"] == 10
    assert usage["input_audio_tokens"] == 100
    assert usage["output_text_tokens"] == 25


def test_missing_gemini_mixed_modality_is_visible_as_unpriced(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()
    metadata = SimpleNamespace(
        prompt_token_count=100,
        prompt_tokens_details=None,
        cached_content_token_count=0,
        cache_tokens_details=None,
        candidates_token_count=10,
        candidates_tokens_details=None,
        thoughts_token_count=0,
        tool_use_prompt_token_count=0,
    )
    usage = gemini_usage_values(metadata, role="transcription")
    tracker.record_usage(
        provider="gemini",
        role="transcription",
        model="gemini-3.1-flash-lite",
        usage=usage,
    )
    provider = tracker.snapshot()["providers"]["gemini"]
    assert provider["usage"]["input_unknown_tokens"] == 100
    assert provider["fully_priced"] is False


def test_history_contains_no_content_or_credentials(tmp_path):
    tracker = CostTracker(tmp_path)
    tracker.begin_session()
    tracker.record_usage(
        provider="openai",
        role="translation",
        model="gpt-4o-mini",
        usage={"input_text_tokens": 5},
    )
    tracker.end_session()
    raw = next(tmp_path.glob("*.json")).read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert "prompt" not in raw.lower()
    assert "transcript" not in raw.lower()
    assert "api_key" not in raw.lower()
    assert parsed["pricing_version"]


def test_usd_formatter_keeps_micro_cost_visible():
    assert format_usd("0") == "$0.0000"
    assert format_usd("0.000001") == "< $0.0001"
    assert format_usd("0.123456") == "$0.1235"
