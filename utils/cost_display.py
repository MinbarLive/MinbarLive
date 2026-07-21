"""Pure formatting helpers for the cost-history viewer (no Tk).

Turns the raw session dicts produced by ``utils.cost_tracking`` into the small
value objects the Kosten tab renders: list rows, a breakdown text block and the
bar-chart series. Kept Tk-free so it can be unit-tested without a display.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from utils.cost_tracking import format_usd

# Roles are stored as pipeline ids; these are the human labels the breakdown
# shows. Unknown roles fall back to the raw id.
_ROLE_LABELS = {
    "translation": "translation",
    "transcription": "transcription",
    "embedding": "embedding",
    "summary": "summary",
}

# Proper-cased provider names (str.capitalize turns "openai" into "Openai").
_PROVIDER_LABELS = {
    "openai": "OpenAI",
    "gemini": "Gemini",
    "anthropic": "Anthropic",
    "deepgram": "Deepgram",
}


def provider_label(provider_id: str) -> str:
    """Display name for a provider id, falling back to a capitalized id."""
    pid = (provider_id or "").strip().lower()
    return _PROVIDER_LABELS.get(pid, (provider_id or "").capitalize())


@dataclass(frozen=True)
class CostRow:
    """One entry in the Kosten session list."""

    session_id: str
    title: str  # date + time range
    subtitle: str  # duration · providers · cost
    total_usd: str  # raw decimal string (for the chart)
    estimated: bool  # True when at least one request was not fully priced


@dataclass(frozen=True)
class CostBar:
    """One bar in the spend chart."""

    session_id: str
    label: str  # short date/time under the bar
    value: float  # USD as a float (chart scaling only)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def session_duration_seconds(session: Mapping[str, Any]) -> int:
    """Whole seconds between a session's start and its end/last-update."""
    start = _parse_iso(session.get("started_at"))
    end = _parse_iso(session.get("ended_at")) or _parse_iso(
        session.get("last_updated_at")
    )
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds()))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def session_providers(session: Mapping[str, Any]) -> list[str]:
    """Provider ids that actually billed a request, most-expensive first."""
    providers = session.get("providers", {})
    if not isinstance(providers, Mapping):
        return []
    named = [
        (pid, _decimal(row.get("cost_usd")))
        for pid, row in providers.items()
        if isinstance(row, Mapping) and int(row.get("requests", 0) or 0) > 0
    ]
    named.sort(key=lambda pair: pair[1], reverse=True)
    return [pid for pid, _cost in named]


def _cost_label(session: Mapping[str, Any]) -> str:
    """Formatted total, prefixed with ``~`` when any request was unpriced."""
    text = format_usd(session.get("total_cost_usd"))
    return text if session.get("fully_priced", True) else f"~{text}"


def cost_rows(
    sessions: list[Mapping[str, Any]],
    *,
    duration_fmt: str = "{minutes} min",
    seconds_fmt: str = "{seconds} s",
) -> list[CostRow]:
    """Build the Kosten list rows (already ordered newest-first by the caller)."""
    rows: list[CostRow] = []
    for session in sessions:
        start = _parse_iso(session.get("started_at"))
        if start is not None:
            end = _parse_iso(session.get("ended_at"))
            time_range = start.strftime("%Y-%m-%d  %H:%M")
            if end is not None:
                time_range += end.strftime(" – %H:%M")
        else:
            time_range = str(session.get("started_at", "?"))

        seconds = session_duration_seconds(session)
        if seconds < 60:
            duration = seconds_fmt.format(seconds=seconds)
        else:
            duration = duration_fmt.format(minutes=seconds // 60)

        providers = session_providers(session)
        provider_text = ", ".join(providers) if providers else "—"
        subtitle = f"{duration} · {provider_text} · {_cost_label(session)}"
        rows.append(
            CostRow(
                session_id=str(session.get("id", "")),
                title=time_range,
                subtitle=subtitle,
                total_usd=str(session.get("total_cost_usd", "0")),
                estimated=not session.get("fully_priced", True),
            )
        )
    return rows


@dataclass(frozen=True)
class CostWindowTotal:
    """Aggregate spend over a trailing time window."""

    total: str  # formatted, ``~``-prefixed when any session is an estimate
    sessions: int  # how many sessions fell inside the window


def cost_window_total(
    sessions: list[Mapping[str, Any]],
    *,
    days: int = 30,
    now: datetime | None = None,
) -> CostWindowTotal:
    """Sum the cost of sessions started within the last ``days``.

    ``now`` is injectable so the window is testable without wall-clock time.
    A session with no parseable start is counted (it is recent enough to still
    be in the store); the ``~`` prefix appears when any counted session was not
    fully priced.
    """
    reference = now or datetime.now(timezone.utc)
    window = timedelta(days=days)
    total = Decimal("0")
    count = 0
    estimated = False
    for session in sessions:
        start = _parse_iso(session.get("started_at"))
        if start is not None:
            # started_at may be tz-aware or naive; match the reference to it so
            # the subtraction never raises on mixed awareness.
            ref = reference if start.tzinfo else reference.replace(tzinfo=None)
            if (ref - start) > window:
                continue
        total += _decimal(session.get("total_cost_usd"))
        count += 1
        if not session.get("fully_priced", True):
            estimated = True
    text = format_usd(total)
    return CostWindowTotal(
        total=f"~{text}" if estimated else text, sessions=count
    )


@dataclass(frozen=True)
class CostProviderTotal:
    """One provider's aggregate spend over a trailing window."""

    provider: str
    total: str  # formatted, ``~``-prefixed when estimated
    amount: float  # raw USD (for ordering)


def cost_window_by_provider(
    sessions: list[Mapping[str, Any]],
    *,
    days: int = 30,
    now: datetime | None = None,
) -> list[CostProviderTotal]:
    """Per-provider spend over the last ``days``, most-expensive first.

    Only providers that billed a request contribute; the ``~`` prefix appears
    when any of that provider's contributions in the window were unpriced.
    """
    reference = now or datetime.now(timezone.utc)
    window = timedelta(days=days)
    totals: dict[str, Decimal] = {}
    estimated: dict[str, bool] = {}
    for session in sessions:
        start = _parse_iso(session.get("started_at"))
        if start is not None:
            ref = reference if start.tzinfo else reference.replace(tzinfo=None)
            if (ref - start) > window:
                continue
        providers = session.get("providers", {})
        if not isinstance(providers, Mapping):
            continue
        for pid, row in providers.items():
            if not isinstance(row, Mapping) or int(row.get("requests", 0) or 0) <= 0:
                continue
            totals[pid] = totals.get(pid, Decimal("0")) + _decimal(row.get("cost_usd"))
            if not row.get("fully_priced", True):
                estimated[pid] = True
    result = [
        CostProviderTotal(
            provider=pid,
            total=f"~{format_usd(amount)}"
            if estimated.get(pid)
            else format_usd(amount),
            amount=float(amount),
        )
        for pid, amount in totals.items()
    ]
    result.sort(key=lambda p: p.amount, reverse=True)
    return result


def cost_bars(sessions: list[Mapping[str, Any]], *, limit: int = 12) -> list[CostBar]:
    """The spend chart series, oldest→newest (left→right), capped at ``limit``.

    ``sessions`` arrives newest-first (as the list shows it); the chart reads
    naturally left-to-right in time, so the newest ``limit`` are reversed.
    """
    chosen = list(sessions[:limit])[::-1]
    bars: list[CostBar] = []
    for session in chosen:
        start = _parse_iso(session.get("started_at"))
        label = start.strftime("%m-%d\n%H:%M") if start else "?"
        try:
            value = float(_decimal(session.get("total_cost_usd")))
        except (ValueError, OverflowError):
            value = 0.0
        bars.append(
            CostBar(session_id=str(session.get("id", "")), label=label, value=value)
        )
    return bars


def cost_breakdown_lines(
    session: Mapping[str, Any],
    *,
    estimate_note: str = "Estimate — public list prices",
    unpriced_note: str = "unpriced",
    requests_label: str = "requests",
) -> str:
    """A per-provider / per-model breakdown block for the detail textbox."""
    lines: list[str] = []
    seconds = session_duration_seconds(session)
    minutes, secs = divmod(seconds, 60)
    duration = f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"
    lines.append(f"{_cost_label(session)}   ({duration})")
    version = session.get("pricing_version")
    note = estimate_note
    if version:
        note = f"{estimate_note} · {version}"
    lines.append(note)
    lines.append("")

    providers = session.get("providers", {})
    if not isinstance(providers, Mapping):
        return "\n".join(lines)
    ordered = sorted(
        (
            (pid, row)
            for pid, row in providers.items()
            if isinstance(row, Mapping) and int(row.get("requests", 0) or 0) > 0
        ),
        key=lambda pair: _decimal(pair[1].get("cost_usd")),
        reverse=True,
    )
    for pid, prow in ordered:
        pcost = _cost_label({"total_cost_usd": prow.get("cost_usd"),
                             "fully_priced": prow.get("fully_priced", True)})
        lines.append(f"{provider_label(pid)} — {pcost}")
        models = prow.get("models", {})
        if isinstance(models, Mapping):
            for mid, mrow in sorted(
                models.items(),
                key=lambda pair: _decimal(pair[1].get("cost_usd")),
                reverse=True,
            ):
                if not isinstance(mrow, Mapping):
                    continue
                roles = ", ".join(
                    _ROLE_LABELS.get(r, r) for r in mrow.get("roles", []) if r
                )
                requests = int(mrow.get("requests", 0) or 0)
                tag = "" if mrow.get("fully_priced", True) else f"  [{unpriced_note}]"
                role_text = f" ({roles})" if roles else ""
                lines.append(
                    f"    {mid}{role_text}: {requests} {requests_label}{tag}"
                )
        lines.append("")
    return "\n".join(lines).rstrip("\n")
