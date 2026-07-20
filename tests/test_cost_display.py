"""Tests for the pure cost-history display helpers (no Tk)."""

from __future__ import annotations

from datetime import datetime, timezone

from utils.cost_display import (
    cost_bars,
    cost_breakdown_lines,
    cost_rows,
    cost_window_by_provider,
    cost_window_total,
    session_duration_seconds,
    session_providers,
)


def _session(**over):
    base = {
        "id": "abc123",
        "started_at": "2026-07-20T14:00:00+00:00",
        "ended_at": "2026-07-20T14:18:00+00:00",
        "total_cost_usd": "0.2100",
        "fully_priced": True,
        "pricing_version": "2026-07-19",
        "providers": {
            "gemini": {
                "cost_usd": "0.2100",
                "fully_priced": True,
                "requests": 40,
                "models": {
                    "gemini-3.5-flash": {
                        "cost_usd": "0.2100",
                        "fully_priced": True,
                        "requests": 40,
                        "roles": ["transcription", "translation"],
                    }
                },
            }
        },
    }
    base.update(over)
    return base


class TestDuration:
    def test_uses_start_and_end(self):
        assert session_duration_seconds(_session()) == 18 * 60

    def test_falls_back_to_last_updated_when_active(self):
        s = _session(ended_at=None, last_updated_at="2026-07-20T14:05:00+00:00")
        assert session_duration_seconds(s) == 5 * 60

    def test_missing_timestamps_is_zero(self):
        assert session_duration_seconds({"started_at": None}) == 0

    def test_unparseable_is_zero(self):
        assert session_duration_seconds(_session(started_at="not-a-date")) == 0


class TestProviders:
    def test_only_providers_with_requests(self):
        s = _session()
        s["providers"]["openai"] = {"cost_usd": "0", "requests": 0}
        assert session_providers(s) == ["gemini"]

    def test_ordered_by_cost_desc(self):
        s = _session()
        s["providers"]["openai"] = {"cost_usd": "0.9", "requests": 5}
        assert session_providers(s) == ["openai", "gemini"]


class TestRows:
    def test_row_fields(self):
        (row,) = cost_rows([_session()])
        assert row.session_id == "abc123"
        assert "2026-07-20" in row.title and "14:00" in row.title
        assert "18 min" in row.subtitle
        assert "gemini" in row.subtitle
        assert "$0.21" in row.subtitle
        assert row.estimated is False

    def test_unpriced_session_prefixes_tilde_and_flags_estimated(self):
        (row,) = cost_rows([_session(fully_priced=False)])
        assert row.subtitle.count("~$") == 1
        assert row.estimated is True

    def test_custom_duration_format_applied(self):
        (row,) = cost_rows(
            [_session()], duration_fmt="{minutes} Min.", seconds_fmt="{seconds} Sek."
        )
        assert "18 Min." in row.subtitle

    def test_short_session_uses_seconds(self):
        s = _session(ended_at="2026-07-20T14:00:30+00:00")
        (row,) = cost_rows([s])
        assert "30 s" in row.subtitle


class TestBars:
    def test_oldest_to_newest_and_capped(self):
        sessions = [
            _session(id=str(i), started_at=f"2026-07-20T{10 + i:02d}:00:00+00:00")
            for i in range(15)
        ]  # newest-first as the list provides
        bars = cost_bars(sessions, limit=5)
        assert len(bars) == 5
        # Reversed to time order: the 5 most-recent, oldest on the left.
        assert [b.session_id for b in bars] == ["4", "3", "2", "1", "0"]
        assert all(isinstance(b.value, float) for b in bars)

    def test_bad_total_is_zero_value(self):
        (bar,) = cost_bars([_session(total_cost_usd="oops")])
        assert bar.value == 0.0


class TestWindowTotal:
    _NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def test_sums_sessions_inside_window(self):
        sessions = [
            _session(id="a", started_at="2026-07-19T10:00:00+00:00",
                     total_cost_usd="0.50"),
            _session(id="b", started_at="2026-07-01T10:00:00+00:00",
                     total_cost_usd="0.30"),
        ]
        result = cost_window_total(sessions, days=30, now=self._NOW)
        assert result.sessions == 2
        assert result.total == "$0.8000"

    def test_excludes_sessions_older_than_window(self):
        sessions = [
            _session(id="recent", started_at="2026-07-19T10:00:00+00:00",
                     total_cost_usd="0.50"),
            _session(id="old", started_at="2026-04-24T10:00:00+00:00",
                     total_cost_usd="9.00"),
        ]
        result = cost_window_total(sessions, days=30, now=self._NOW)
        assert result.sessions == 1
        assert result.total == "$0.5000"

    def test_tilde_when_any_session_estimated(self):
        sessions = [
            _session(started_at="2026-07-19T10:00:00+00:00",
                     total_cost_usd="0.50", fully_priced=False),
        ]
        result = cost_window_total(sessions, days=30, now=self._NOW)
        assert result.total.startswith("~$")

    def test_empty_is_zero(self):
        result = cost_window_total([], days=30, now=self._NOW)
        assert result.sessions == 0
        assert result.total == "$0.0000"


class TestWindowByProvider:
    _NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)

    def _two_provider_session(self, sid, start, gem, oai, gem_priced=True):
        s = _session(id=sid, started_at=start)
        s["providers"] = {
            "gemini": {"cost_usd": gem, "fully_priced": gem_priced, "requests": 5,
                       "models": {}},
            "openai": {"cost_usd": oai, "fully_priced": True, "requests": 5,
                       "models": {}},
        }
        return s

    def test_sums_per_provider_ordered_desc(self):
        sessions = [
            self._two_provider_session(
                "a", "2026-07-19T10:00:00+00:00", "0.10", "0.40"
            ),
            self._two_provider_session(
                "b", "2026-07-18T10:00:00+00:00", "0.05", "0.20"
            ),
        ]
        result = cost_window_by_provider(sessions, days=30, now=self._NOW)
        assert [p.provider for p in result] == ["openai", "gemini"]
        assert result[0].total == "$0.6000"  # openai 0.40 + 0.20
        assert result[1].total == "$0.1500"  # gemini 0.10 + 0.05

    def test_excludes_out_of_window(self):
        sessions = [
            self._two_provider_session(
                "recent", "2026-07-19T10:00:00+00:00", "0.10", "0.40"
            ),
            self._two_provider_session(
                "old", "2026-01-01T10:00:00+00:00", "9.00", "9.00"
            ),
        ]
        result = cost_window_by_provider(sessions, days=30, now=self._NOW)
        totals = {p.provider: p.total for p in result}
        assert totals == {"openai": "$0.4000", "gemini": "$0.1000"}

    def test_tilde_when_provider_has_unpriced(self):
        sessions = [
            self._two_provider_session(
                "a", "2026-07-19T10:00:00+00:00", "0.10", "0.40", gem_priced=False
            )
        ]
        result = cost_window_by_provider(sessions, days=30, now=self._NOW)
        totals = {p.provider: p.total for p in result}
        assert totals["gemini"].startswith("~$")
        assert not totals["openai"].startswith("~")

    def test_skips_zero_request_providers(self):
        s = _session()
        s["providers"]["openai"] = {"cost_usd": "0", "requests": 0, "models": {}}
        result = cost_window_by_provider([s], days=30, now=self._NOW)
        assert [p.provider for p in result] == ["gemini"]


class TestBreakdown:
    def test_provider_label_proper_cases_known_ids(self):
        from utils.cost_display import provider_label

        assert provider_label("openai") == "OpenAI"
        assert provider_label("gemini") == "Gemini"
        assert provider_label("mystery") == "Mystery"

    def test_contains_provider_model_roles_and_note(self):
        text = cost_breakdown_lines(_session())
        assert "Gemini —" in text
        assert "gemini-3.5-flash" in text
        assert "transcription" in text and "translation" in text
        assert "40 requests" in text
        assert "2026-07-19" in text

    def test_unpriced_model_tagged(self):
        s = _session(fully_priced=False)
        s["providers"]["gemini"]["fully_priced"] = False
        s["providers"]["gemini"]["models"]["gemini-embedding-001"] = {
            "cost_usd": "0",
            "fully_priced": False,
            "requests": 3,
            "roles": ["embedding"],
        }
        text = cost_breakdown_lines(s, unpriced_note="unpriced")
        assert "[unpriced]" in text
        assert text.lstrip().startswith("~$")
