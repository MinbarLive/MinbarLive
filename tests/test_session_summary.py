"""Tests for utils.session_summary (session transcript summarization)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.session_summary as ss
from utils.history import HistoryEntry


def _pairs():
    return [
        (HistoryEntry("10:00:00", "AR", "السلام عليكم"), HistoryEntry("10:00:00", "GE", "Friede sei mit euch")),
        (HistoryEntry("10:00:12", "AR", "الحمد لله"), HistoryEntry("10:00:12", "GE", "Lob sei Gott")),
    ]


class TestBuildPrompts:
    def test_includes_target_language_and_both_texts(self):
        system, user = ss._build_prompts(_pairs(), "German", "2026-07-07 · AR → GE")
        assert "German" in system
        # Ask for prose under section headings, not bullet points (either mode).
        assert "prose" in system.lower()
        assert "heading" in system.lower()
        # both original and translation appear, with their language tags
        assert "[AR] السلام عليكم" in user
        assert "[GE] Friede sei mit euch" in user
        assert "[AR] الحمد لله" in user
        assert "2026-07-07 · AR → GE" in user

    def test_handles_unpaired_trailing_entry(self):
        pairs = [(HistoryEntry("10:00:00", "AR", "فقط"), None)]
        _system, user = ss._build_prompts(pairs, "English", None)
        assert "[AR] فقط" in user
        assert "[GE]" not in user  # no translation line for the unpaired entry

    def test_identical_pair_collapsed_to_single_line(self):
        """Same-language records store transcription == translation — the
        duplicate must not be sent to the model twice."""
        pairs = [
            (
                HistoryEntry("10:00:00", "AR", "الحمد لله"),
                HistoryEntry("10:00:00", "AR", "الحمد لله"),
            ),
        ]
        _system, user = ss._build_prompts(pairs, "German", None)
        assert user.count("الحمد لله") == 1

    def test_islamic_mode_on_uses_islamic_framing(self, monkeypatch):
        monkeypatch.setattr(
            ss, "load_settings", lambda *a, **k: SimpleNamespace(islamic_mode=True)
        )
        system, _user = ss._build_prompts(_pairs(), "German", None)
        low = system.lower()
        assert "khutbah" in low or "islamic" in low
        assert "quran" in low or "hadith" in low

    def test_islamic_mode_off_uses_neutral_framing(self, monkeypatch):
        monkeypatch.setattr(
            ss, "load_settings", lambda *a, **k: SimpleNamespace(islamic_mode=False)
        )
        system, _user = ss._build_prompts(_pairs(), "German", None)
        low = system.lower()
        for word in ("islamic", "khutbah", "sermon", "quran", "hadith"):
            assert word not in low
        assert "german" in low  # target language still honored


class TestSummarizeSessionFile:
    def _write(self, tmp_path):
        p = tmp_path / "2026-07-07.txt"
        p.write_text(
            "[10:00:00] AR: السلام عليكم\n"
            "[10:00:00] GE: Friede sei mit euch\n"
            "\n",
            encoding="utf-8",
        )
        return str(p)

    def test_calls_provider_and_returns_text(self, tmp_path, monkeypatch):
        captured = {}

        class FakeProvider:
            def complete(self, *, model, user_prompt, system_prompt=None,
                         max_output_tokens=None, temperature=None):
                captured.update(
                    model=model, user_prompt=user_prompt,
                    system_prompt=system_prompt,
                    max_output_tokens=max_output_tokens,
                )
                return "  A concise summary.  "

        monkeypatch.setattr(ss, "get_translation_provider_for", lambda pid: FakeProvider())
        monkeypatch.setattr(ss, "get_default_model", lambda pid, cap: "fake-model")

        out = ss.summarize_session_file(
            self._write(tmp_path),
            target_language="German",
            provider_id="openai",
            session_label="2026-07-07 · AR → GE",
        )
        # complete() promises stripped output; our module returns it as-is
        assert out.strip() == "A concise summary."
        assert captured["model"] == "fake-model"
        assert "German" in captured["system_prompt"]
        assert captured["max_output_tokens"] == ss._SUMMARY_MAX_OUTPUT_TOKENS
        assert "السلام عليكم" in captured["user_prompt"]

    def test_uses_provided_model_override(self, tmp_path, monkeypatch):
        seen = {}

        class FakeProvider:
            def complete(self, *, model, **kw):
                seen["model"] = model
                return "x"

        monkeypatch.setattr(ss, "get_translation_provider_for", lambda pid: FakeProvider())
        monkeypatch.setattr(ss, "get_default_model", lambda pid, cap: "default-model")
        ss.summarize_session_file(
            self._write(tmp_path), target_language="English",
            provider_id="openai", model="my-model",
        )
        assert seen["model"] == "my-model"

    def test_empty_session_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "2026-07-07.txt"
        p.write_text("\n[Processing time]: 1.0s\n\n", encoding="utf-8")
        monkeypatch.setattr(ss, "get_translation_provider_for", lambda pid: None)
        with pytest.raises(ValueError):
            ss.summarize_session_file(
                str(p), target_language="German", provider_id="openai"
            )
