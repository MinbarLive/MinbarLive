"""Thread-safe provider usage metering and per-session cost history.

The provider APIs report usage, not invoice totals.  This module applies a
versioned snapshot of the public paid Standard USD list prices and therefore
always exposes an *estimate*.  Provider worker threads only update memory;
``flush_cost_history`` is called by the Tk thread so live subtitles never wait
for disk I/O.

Only counters, provider/model ids and timestamps are persisted.  Prompts,
transcripts, audio and credentials never enter the cost history.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from utils.app_paths import get_app_data_dir
from utils.logging import log

PRICING_VERSION = "2026-07-19"
SCHEMA_VERSION = 1
COST_HISTORY_DIR = get_app_data_dir() / "cost_history"

TOKEN_FIELDS = (
    "input_text_tokens",
    "cached_input_text_tokens",
    "input_audio_tokens",
    "cached_input_audio_tokens",
    "output_text_tokens",
    "output_audio_tokens",
    "input_unknown_tokens",
    "output_unknown_tokens",
)
USAGE_FIELDS = (*TOKEN_FIELDS, "duration_seconds")


@dataclass(frozen=True)
class ModelRates:
    """USD rates per one million tokens, plus an optional per-minute rate."""

    input_text: Decimal | None = None
    cached_input_text: Decimal | None = None
    input_audio: Decimal | None = None
    cached_input_audio: Decimal | None = None
    output_text: Decimal | None = None
    output_audio: Decimal | None = None
    duration_minute: Decimal | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            name: str(value) if value is not None else None
            for name, value in self.__dict__.items()
        }


def _d(value: str) -> Decimal:
    return Decimal(value)


# Paid Standard list-price snapshot.  Matching supports dated model snapshots
# (for example ``gpt-4o-mini-2024-07-18``); longest ids win.
MODEL_RATES: dict[str, ModelRates] = {
    "gpt-5.5": ModelRates(_d("5"), _d("0.5"), output_text=_d("30")),
    "gpt-5.4-mini": ModelRates(_d("0.75"), _d("0.075"), output_text=_d("4.5")),
    "gpt-5.4-nano": ModelRates(_d("0.20"), _d("0.02"), output_text=_d("1.25")),
    "gpt-5.4": ModelRates(_d("2.5"), _d("0.25"), output_text=_d("15")),
    "gpt-5.2": ModelRates(_d("1.75"), _d("0.175"), output_text=_d("14")),
    "gpt-5.1": ModelRates(_d("1.25"), _d("0.125"), output_text=_d("10")),
    "gpt-5-mini": ModelRates(_d("0.25"), _d("0.025"), output_text=_d("2")),
    "gpt-5-nano": ModelRates(_d("0.05"), _d("0.005"), output_text=_d("0.40")),
    "gpt-5": ModelRates(_d("1.25"), _d("0.125"), output_text=_d("10")),
    "gpt-4.1-mini": ModelRates(_d("0.40"), _d("0.10"), output_text=_d("1.60")),
    "gpt-4.1": ModelRates(_d("2"), _d("0.50"), output_text=_d("8")),
    "gpt-4o-mini": ModelRates(_d("0.15"), _d("0.075"), output_text=_d("0.60")),
    "gpt-4o": ModelRates(_d("2.5"), _d("1.25"), output_text=_d("10")),
    "gpt-4o-mini-transcribe": ModelRates(
        _d("1.25"), input_audio=_d("1.25"), output_text=_d("5")
    ),
    "gpt-4o-transcribe": ModelRates(
        _d("2.5"), input_audio=_d("2.5"), output_text=_d("10")
    ),
    "whisper-1": ModelRates(duration_minute=_d("0.006")),
    "text-embedding-3-large": ModelRates(input_text=_d("0.13")),
    "text-embedding-3-small": ModelRates(input_text=_d("0.02")),
    "gemini-3.5-flash": ModelRates(
        _d("1.5"), _d("0.15"), _d("1.5"), _d("0.15"), _d("9"), _d("9")
    ),
    "gemini-3.1-flash-lite": ModelRates(
        _d("0.25"), _d("0.025"), _d("0.50"), _d("0.05"), _d("1.5"), _d("1.5")
    ),
    "gemini-2.5-flash-native-audio-preview-12-2025": ModelRates(
        _d("0.50"), None, _d("3"), None, _d("2"), _d("12")
    ),
    # ``latest`` currently resolves to this family.  The UI already labels all
    # values as estimates, and the requested + resolved ids remain in history.
    "gemini-2.5-flash-native-audio-latest": ModelRates(
        _d("0.50"), None, _d("3"), None, _d("2"), _d("12")
    ),
    "gemini-embedding-001": ModelRates(input_text=_d("0.15")),
}

_RATE_FIELDS = {
    "input_text_tokens": "input_text",
    "cached_input_text_tokens": "cached_input_text",
    "input_audio_tokens": "input_audio",
    "cached_input_audio_tokens": "cached_input_audio",
    "output_text_tokens": "output_text",
    "output_audio_tokens": "output_audio",
}


def rates_for_model(model: str) -> ModelRates | None:
    model_id = (model or "").strip().lower()
    if not model_id:
        return None
    for known in sorted(MODEL_RATES, key=len, reverse=True):
        if model_id == known or model_id.startswith(f"{known}-"):
            return MODEL_RATES[known]
    return None


def estimate_usage_cost(
    model: str, usage: Mapping[str, int | float]
) -> tuple[Decimal, bool, ModelRates | None]:
    """Return ``(usd, fully_priced, applied_rates)`` for one usage delta."""

    rates = rates_for_model(model)
    used = any(Decimal(str(usage.get(field, 0) or 0)) > 0 for field in USAGE_FIELDS)
    if not used:
        return Decimal("0"), True, rates
    if rates is None:
        return Decimal("0"), False, None

    total = Decimal("0")
    fully_priced = True
    for usage_field, rate_field in _RATE_FIELDS.items():
        amount = Decimal(str(usage.get(usage_field, 0) or 0))
        if amount <= 0:
            continue
        rate = getattr(rates, rate_field)
        if rate is None:
            fully_priced = False
        else:
            total += amount * rate / Decimal("1000000")

    # Missing Gemini modality breakdowns are deliberately retained as unknown
    # instead of silently charging every mixed prompt at the cheaper text rate.
    if Decimal(str(usage.get("input_unknown_tokens", 0) or 0)) > 0:
        fully_priced = False
    if Decimal(str(usage.get("output_unknown_tokens", 0) or 0)) > 0:
        fully_priced = False

    seconds = Decimal(str(usage.get("duration_seconds", 0) or 0))
    if seconds > 0:
        if rates.duration_minute is None:
            fully_priced = False
        else:
            total += seconds * rates.duration_minute / Decimal("60")
    return total, fully_priced, rates


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty_usage() -> dict[str, int | float]:
    return {field: 0.0 if field == "duration_seconds" else 0 for field in USAGE_FIELDS}


def _safe_number(value: Any, *, as_float: bool = False) -> int | float:
    try:
        return max(0.0, float(value or 0)) if as_float else max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0.0 if as_float else 0


def _get(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


class CostTracker:
    """In-memory accumulator with atomic, one-file-per-session history."""

    def __init__(self, history_dir: str | Path = COST_HISTORY_DIR) -> None:
        self.history_dir = Path(history_dir)
        self._lock = threading.RLock()
        self._active: dict[str, Any] | None = None
        self._dirty = False
        self._revision = 0
        self._seen_event_ids: set[str] = set()
        self._live_snapshots: dict[str, dict[str, int | float]] = {}
        self._recover_stale_sessions()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def begin_session(self) -> str:
        with self._lock:
            if self._active is not None:
                self._finish_locked("interrupted")
            started = _now_iso()
            session_id = uuid.uuid4().hex
            self._active = {
                "schema_version": SCHEMA_VERSION,
                "id": session_id,
                "started_at": started,
                "ended_at": None,
                "last_updated_at": started,
                "status": "active",
                "pricing_version": PRICING_VERSION,
                "currency": "USD",
                "providers": {},
                "total_cost_usd": "0",
                "fully_priced": True,
            }
            self._dirty = False
            self._seen_event_ids.clear()
            self._live_snapshots.clear()
            self._revision += 1
            return session_id

    def cancel_session(self) -> None:
        """Discard a provisional/failed start without creating history."""

        with self._lock:
            if self._active is None:
                return
            path = self._session_path(self._active)
            self._active = None
            self._dirty = False
            self._seen_event_ids.clear()
            self._live_snapshots.clear()
            self._revision += 1
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            log(f"Cost history cleanup failed: {exc}", level="WARNING")

    def end_session(self, status: str = "completed") -> dict[str, Any] | None:
        with self._lock:
            return self._finish_locked(status)

    def _finish_locked(self, status: str) -> dict[str, Any] | None:
        if self._active is None:
            return None
        if not self._has_usage(self._active):
            result = copy.deepcopy(self._active)
            self._active = None
            self._dirty = False
            self._seen_event_ids.clear()
            self._live_snapshots.clear()
            self._revision += 1
            return result
        now = _now_iso()
        self._active["status"] = status
        self._active["ended_at"] = now
        self._active["last_updated_at"] = now
        result = copy.deepcopy(self._active)
        try:
            self._write_session(result)
        except OSError:
            # Cost metering is ancillary; a read-only/full profile directory
            # must never prevent the operator from stopping a live session.
            pass
        self._active = None
        self._dirty = False
        self._seen_event_ids.clear()
        self._live_snapshots.clear()
        self._revision += 1
        return result

    def record_usage(
        self,
        *,
        provider: str,
        role: str,
        model: str,
        usage: Mapping[str, int | float],
        event_id: str | None = None,
        force_unpriced: bool = False,
    ) -> None:
        with self._lock:
            if self._active is None:
                return
            if event_id:
                if event_id in self._seen_event_ids:
                    return
                self._seen_event_ids.add(event_id)
            clean = {
                field: _safe_number(
                    usage.get(field, 0), as_float=field == "duration_seconds"
                )
                for field in USAGE_FIELDS
            }
            if not any(clean.values()) and not force_unpriced:
                return
            cost, fully_priced, rates = estimate_usage_cost(model, clean)
            self._apply_usage_locked(
                provider=provider,
                role=role,
                model=model,
                usage=clean,
                cost=cost,
                fully_priced=fully_priced and not force_unpriced,
                rates=rates,
            )

    def record_live_snapshot(
        self,
        *,
        stream_id: str,
        provider: str,
        role: str,
        model: str,
        usage: Mapping[str, int | float],
    ) -> None:
        """Apply only the positive delta of a cumulative physical-stream total."""

        current = {
            field: _safe_number(
                usage.get(field, 0), as_float=field == "duration_seconds"
            )
            for field in USAGE_FIELDS
        }
        with self._lock:
            previous = self._live_snapshots.get(stream_id, _empty_usage())
            delta = {
                field: max(0, current[field] - previous.get(field, 0))
                for field in USAGE_FIELDS
            }
            self._live_snapshots[stream_id] = current
        self.record_usage(provider=provider, role=role, model=model, usage=delta)

    def _apply_usage_locked(
        self,
        *,
        provider: str,
        role: str,
        model: str,
        usage: Mapping[str, int | float],
        cost: Decimal,
        fully_priced: bool,
        rates: ModelRates | None,
    ) -> None:
        assert self._active is not None
        provider_id = provider.strip().lower()
        provider_row = self._active["providers"].setdefault(
            provider_id,
            {
                "cost_usd": "0",
                "fully_priced": True,
                "requests": 0,
                "usage": _empty_usage(),
                "models": {},
            },
        )
        model_id = model.strip() or "unknown"
        model_row = provider_row["models"].setdefault(
            model_id,
            {
                "cost_usd": "0",
                "fully_priced": True,
                "requests": 0,
                "roles": [],
                "usage": _empty_usage(),
                "rates_per_million_usd": rates.as_dict() if rates else None,
            },
        )
        if role and role not in model_row["roles"]:
            model_row["roles"].append(role)
        for field, amount in usage.items():
            provider_row["usage"][field] += amount
            model_row["usage"][field] += amount
        model_row["requests"] += 1
        provider_row["requests"] += 1
        model_row["cost_usd"] = str(Decimal(model_row["cost_usd"]) + cost)
        provider_row["cost_usd"] = str(Decimal(provider_row["cost_usd"]) + cost)
        model_row["fully_priced"] = model_row["fully_priced"] and fully_priced
        provider_row["fully_priced"] = provider_row["fully_priced"] and fully_priced
        self._active["total_cost_usd"] = str(
            Decimal(self._active["total_cost_usd"]) + cost
        )
        self._active["fully_priced"] = (
            self._active["fully_priced"] and fully_priced
        )
        self._active["last_updated_at"] = _now_iso()
        self._dirty = True
        self._revision += 1

    def snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return copy.deepcopy(self._active)

    def flush(self) -> None:
        with self._lock:
            if not self._dirty or self._active is None or not self._has_usage(self._active):
                return
            record = copy.deepcopy(self._active)
            self._dirty = False
        try:
            self._write_session(record)
        except OSError:
            with self._lock:
                self._dirty = True

    def list_sessions(self, *, include_active: bool = True) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        try:
            paths = list(self.history_dir.glob("*.json"))
        except OSError:
            paths = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("id"):
                    records[str(data["id"])] = data
            except (OSError, ValueError, TypeError):
                continue
        if include_active:
            active = self.snapshot()
            if active is not None and self._has_usage(active):
                records[str(active["id"])] = active
        return sorted(
            records.values(), key=lambda row: str(row.get("started_at", "")), reverse=True
        )

    def latest_session(self) -> dict[str, Any] | None:
        sessions = self.list_sessions(include_active=True)
        return sessions[0] if sessions else None

    def delete_session(self, session_id: str) -> bool:
        deleted = False
        try:
            for path in self.history_dir.glob(f"*_{session_id}.json"):
                path.unlink()
                deleted = True
        except OSError as exc:
            log(f"Cost history delete failed: {exc}", level="WARNING")
            return False
        return deleted

    @staticmethod
    def _has_usage(session: Mapping[str, Any]) -> bool:
        return any(
            int(provider.get("requests", 0) or 0) > 0
            for provider in session.get("providers", {}).values()
            if isinstance(provider, Mapping)
        )

    def _session_path(self, session: Mapping[str, Any]) -> Path:
        started = str(session.get("started_at", ""))
        stamp = started[:19].replace("-", "").replace(":", "").replace("T", "_")
        return self.history_dir / f"{stamp}_{session.get('id', 'unknown')}.json"

    def _write_session(self, session: Mapping[str, Any]) -> None:
        try:
            self.history_dir.mkdir(parents=True, exist_ok=True)
            target = self._session_path(session)
            temp = target.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp.write_text(
                json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(temp, target)
        except OSError as exc:
            log(f"Cost history write failed: {exc}", level="WARNING")
            raise

    def _recover_stale_sessions(self) -> None:
        try:
            paths = list(self.history_dir.glob("*.json"))
        except OSError:
            return
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("status") != "active":
                    continue
                data["status"] = "interrupted"
                data["ended_at"] = data.get("last_updated_at") or data.get("started_at")
                self._write_session(data)
            except (OSError, ValueError, TypeError):
                continue


_tracker = CostTracker()


def begin_cost_session() -> str:
    return _tracker.begin_session()


def cancel_cost_session() -> None:
    _tracker.cancel_session()


def end_cost_session(status: str = "completed") -> dict[str, Any] | None:
    return _tracker.end_session(status)


def flush_cost_history() -> None:
    _tracker.flush()


def active_cost_session() -> dict[str, Any] | None:
    return _tracker.snapshot()


def latest_cost_session() -> dict[str, Any] | None:
    return _tracker.latest_session()


def list_cost_sessions() -> list[dict[str, Any]]:
    return _tracker.list_sessions(include_active=True)


def delete_cost_session(session_id: str) -> bool:
    return _tracker.delete_session(session_id)


def cost_revision() -> int:
    return _tracker.revision


def format_usd(value: str | int | float | Decimal) -> str:
    """Compact USD formatting that never rounds a non-zero micro-cost to zero."""

    try:
        amount = max(Decimal("0"), Decimal(str(value or 0)))
    except Exception:
        amount = Decimal("0")
    if amount == 0:
        return "$0.0000"
    if amount < Decimal("0.0001"):
        return "< $0.0001"
    if amount < Decimal("1"):
        return f"${amount:.4f}"
    return f"${amount:.2f}"


def record_provider_usage(**kwargs: Any) -> None:
    """No-throw provider-thread entry point."""

    try:
        _tracker.record_usage(**kwargs)
    except Exception as exc:  # metering must never break subtitles
        log(f"Usage metering skipped: {type(exc).__name__}", level="DEBUG")


def record_live_usage_snapshot(**kwargs: Any) -> None:
    try:
        _tracker.record_live_snapshot(**kwargs)
    except Exception as exc:
        log(f"Live usage metering skipped: {type(exc).__name__}", level="DEBUG")


def _modality_counts(details: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for detail in details or []:
        modality = _get(detail, "modality", "")
        modality = getattr(modality, "value", modality)
        name = str(modality or "").lower().split(".")[-1]
        counts[name] = counts.get(name, 0) + _safe_number(_get(detail, "token_count", 0))
    return counts


def gemini_usage_values(metadata: Any, *, role: str, live: bool = False) -> dict[str, int | float]:
    usage = _empty_usage()
    prompt_total = _safe_number(_get(metadata, "prompt_token_count", 0))
    prompt_details = _modality_counts(_get(metadata, "prompt_tokens_details", None))
    cached_total = _safe_number(_get(metadata, "cached_content_token_count", 0))
    cached_details = _modality_counts(_get(metadata, "cache_tokens_details", None))

    if prompt_details:
        audio = prompt_details.get("audio", 0)
        text = sum(v for k, v in prompt_details.items() if k != "audio")
        # Preserve tokens not represented by details instead of dropping them.
        text += max(0, prompt_total - audio - text)
        cached_audio = cached_details.get("audio", 0)
        cached_text = max(0, cached_total - cached_audio)
        usage["input_audio_tokens"] = max(0, audio - cached_audio)
        usage["input_text_tokens"] = max(0, text - cached_text)
        usage["cached_input_audio_tokens"] = cached_audio
        usage["cached_input_text_tokens"] = cached_text
    elif role == "translation":
        usage["cached_input_text_tokens"] = cached_total
        usage["input_text_tokens"] = max(0, prompt_total - cached_total)
    else:
        # Gemini STT and Live prompts mix instructions/context with audio.
        usage["input_unknown_tokens"] = prompt_total

    tool_tokens = _safe_number(_get(metadata, "tool_use_prompt_token_count", 0))
    usage["input_text_tokens"] += tool_tokens
    output_name = "response_token_count" if live else "candidates_token_count"
    output_details_name = "response_tokens_details" if live else "candidates_tokens_details"
    output_total = _safe_number(_get(metadata, output_name, 0))
    output_details = _modality_counts(_get(metadata, output_details_name, None))
    if output_details:
        audio_out = output_details.get("audio", 0)
        text_out = sum(v for k, v in output_details.items() if k != "audio")
        text_out += max(0, output_total - audio_out - text_out)
        usage["output_audio_tokens"] = audio_out
        usage["output_text_tokens"] = text_out
    else:
        # Unary app operations always request text; Live can theoretically
        # produce audio even though MinbarLive instructs it to stay silent.
        if live and output_total:
            usage["output_unknown_tokens"] = output_total
        else:
            usage["output_text_tokens"] = output_total
    usage["output_text_tokens"] += _safe_number(_get(metadata, "thoughts_token_count", 0))
    return usage


def record_gemini_response(response: Any, *, model: str, role: str) -> None:
    metadata = _get(response, "usage_metadata", None)
    if metadata is None:
        return
    resolved_model = _get(response, "model_version", None) or model
    response_id = _get(response, "response_id", None)
    record_provider_usage(
        provider="gemini",
        role=role,
        model=str(resolved_model),
        usage=gemini_usage_values(metadata, role=role),
        event_id=str(response_id) if response_id else None,
    )


def openai_chat_usage_values(usage_obj: Any) -> dict[str, int | float]:
    usage = _empty_usage()
    prompt = _safe_number(_get(usage_obj, "prompt_tokens", 0))
    details = _get(usage_obj, "prompt_tokens_details", None)
    cached = _safe_number(_get(details, "cached_tokens", 0))
    usage["input_text_tokens"] = max(0, prompt - cached)
    usage["cached_input_text_tokens"] = cached
    # completion_tokens already includes reasoning/rejected-prediction tokens.
    usage["output_text_tokens"] = _safe_number(_get(usage_obj, "completion_tokens", 0))
    return usage


def record_openai_chat_response(response: Any, *, model: str, role: str = "translation") -> None:
    usage_obj = _get(response, "usage", None)
    if usage_obj is None:
        return
    record_provider_usage(
        provider="openai",
        role=role,
        model=str(_get(response, "model", None) or model),
        usage=openai_chat_usage_values(usage_obj),
        event_id=str(_get(response, "id", "")) or None,
    )


def openai_transcription_usage_values(usage_obj: Any) -> dict[str, int | float]:
    usage = _empty_usage()
    if str(_get(usage_obj, "type", "")).lower() == "duration":
        usage["duration_seconds"] = _safe_number(
            _get(usage_obj, "seconds", 0), as_float=True
        )
        return usage
    input_tokens = _safe_number(_get(usage_obj, "input_tokens", 0))
    details = _get(usage_obj, "input_token_details", None)
    audio_tokens = _safe_number(_get(details, "audio_tokens", 0))
    usage["input_audio_tokens"] = audio_tokens
    usage["input_text_tokens"] = max(0, input_tokens - audio_tokens)
    usage["output_text_tokens"] = _safe_number(_get(usage_obj, "output_tokens", 0))
    return usage


def record_openai_transcription_usage(
    usage_obj: Any, *, model: str, event_id: str | None = None
) -> None:
    if usage_obj is None:
        return
    record_provider_usage(
        provider="openai",
        role="transcription",
        model=model,
        usage=openai_transcription_usage_values(usage_obj),
        event_id=event_id,
    )


def record_openai_embedding_response(response: Any, *, model: str) -> None:
    usage_obj = _get(response, "usage", None)
    if usage_obj is None:
        return
    record_provider_usage(
        provider="openai",
        role="embedding",
        model=model,
        usage={"input_text_tokens": _safe_number(_get(usage_obj, "prompt_tokens", 0))},
        event_id=str(_get(response, "id", "")) or None,
    )


def record_unpriced_provider_request(*, provider: str, role: str, model: str) -> None:
    record_provider_usage(
        provider=provider,
        role=role,
        model=model,
        usage={},
        force_unpriced=True,
    )
