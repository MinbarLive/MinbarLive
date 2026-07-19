"""Tests for the AI provider abstraction layer."""

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import providers
from providers.anthropic import AnthropicTranslationProvider
from providers.anthropic import client as anthropic_client
from providers.anthropic import translation as anthropic_translation
from providers.base import (
    EmbeddingProvider,
    StreamHandle,
    StreamingTranscriptionProvider,
    TranscriptionProvider,
    TranslationProvider,
)
from providers.deepgram import DeepgramTranscriptionProvider
from providers.deepgram import client as deepgram_client
from providers.deepgram import transcription as deepgram_transcription
from providers.gemini import (
    GeminiLiveTranscriptionProvider,
    GeminiTranscriptionProvider,
    GeminiTranslationProvider,
)
from providers.gemini import client as gemini_client
from providers.gemini import realtime as gemini_realtime
from providers.gemini import transcription as gemini_transcription
from providers.gemini import translation as gemini_translation
from providers.openai import (
    OpenAIEmbeddingProvider,
    OpenAIRealtimeTranscriptionProvider,
    OpenAITranscriptionProvider,
    OpenAITranslationProvider,
)
from providers.openai import client as openai_client
from providers.openai import embeddings as openai_embeddings
from providers.openai import realtime as openai_realtime
from providers.openai import transcription as openai_transcription
from providers.openai import translation as openai_translation


class TestProtocolConformance:
    """Implementations must structurally satisfy the base Protocols."""

    def test_transcription_provider(self):
        assert isinstance(OpenAITranscriptionProvider(), TranscriptionProvider)
        assert isinstance(GeminiTranscriptionProvider(), TranscriptionProvider)

    def test_streaming_transcription_provider(self):
        assert isinstance(
            DeepgramTranscriptionProvider(), StreamingTranscriptionProvider
        )
        assert isinstance(
            OpenAIRealtimeTranscriptionProvider(), StreamingTranscriptionProvider
        )
        assert isinstance(
            GeminiLiveTranscriptionProvider(), StreamingTranscriptionProvider
        )

    def test_translation_provider(self):
        assert isinstance(OpenAITranslationProvider(), TranslationProvider)
        assert isinstance(GeminiTranslationProvider(), TranslationProvider)
        assert isinstance(AnthropicTranslationProvider(), TranslationProvider)

    def test_embedding_provider(self):
        assert isinstance(OpenAIEmbeddingProvider(), EmbeddingProvider)


class TestFactories:
    """Provider selection from the ai_provider setting."""

    @pytest.fixture(autouse=True)
    def _no_keys(self, monkeypatch):
        # Fallback paths are key-aware; pin to "no keys" so the ranked
        # fallback deterministically lands on the highest-ranked provider
        # (Gemini) regardless of which keys exist on this machine.
        monkeypatch.setattr(providers, "has_usable_key", lambda p: False)
        monkeypatch.setattr(providers, "_fallback_cache", {})
        monkeypatch.setattr(providers, "_warned_fallbacks", set())

    def _set_provider(self, monkeypatch, name, transcription_provider=None):
        # Transcription defaults to the same provider unless overridden — this
        # mirrors the pre-split behavior the existing assertions expect.
        tp = transcription_provider if transcription_provider is not None else name
        monkeypatch.setattr(
            providers,
            "load_settings",
            lambda: SimpleNamespace(ai_provider=name, transcription_provider=tp),
        )

    def test_openai_selected(self, monkeypatch):
        self._set_provider(monkeypatch, "openai")
        assert isinstance(
            providers.get_transcription_provider(), OpenAITranscriptionProvider
        )
        assert isinstance(
            providers.get_translation_provider(), OpenAITranslationProvider
        )

    def test_unknown_provider_falls_back_to_gemini(self, monkeypatch):
        self._set_provider(monkeypatch, "not-a-provider")
        assert isinstance(
            providers.get_translation_provider(), GeminiTranslationProvider
        )

    def test_empty_provider_falls_back_to_gemini(self, monkeypatch):
        self._set_provider(monkeypatch, "")
        assert isinstance(
            providers.get_translation_provider(), GeminiTranslationProvider
        )

    def test_gemini_selected(self, monkeypatch):
        self._set_provider(monkeypatch, "gemini")
        assert isinstance(
            providers.get_transcription_provider(), GeminiTranscriptionProvider
        )
        assert isinstance(
            providers.get_translation_provider(), GeminiTranslationProvider
        )

    def test_embedding_space_openai_without_gemini_matrix(self, monkeypatch):
        """Query embeddings must match the precomputed verse embedding space:
        without a Gemini verse matrix on disk, even a Gemini ai_provider
        stays in the OpenAI space."""
        self._set_provider(monkeypatch, "gemini")
        monkeypatch.setattr(
            providers, "QURAN_EMBEDDINGS_GEMINI_NPZ_PATH", "does/not/exist.npz"
        )
        assert providers.get_embedding_space() == "openai"
        assert isinstance(providers.get_embedding_provider(), OpenAIEmbeddingProvider)

    def test_embedding_space_gemini_with_matrix(self, monkeypatch, tmp_path):
        """With ai_provider=gemini AND its verse matrix built, RAG switches
        to the Gemini space (provider + model together)."""
        from config import EMBEDDING_MODEL, GEMINI_EMBEDDING_MODEL
        from providers.gemini import GeminiEmbeddingProvider

        npz = tmp_path / "quran_embeddings_gemini.npz"
        npz.write_bytes(b"placeholder")
        self._set_provider(monkeypatch, "gemini")
        monkeypatch.setattr(providers, "QURAN_EMBEDDINGS_GEMINI_NPZ_PATH", str(npz))
        assert providers.get_embedding_space() == "gemini"
        assert isinstance(providers.get_embedding_provider(), GeminiEmbeddingProvider)
        assert providers.get_embedding_model() == GEMINI_EMBEDDING_MODEL

        # OpenAI users are untouched by the file's existence
        self._set_provider(monkeypatch, "openai")
        assert providers.get_embedding_space() == "openai"
        assert providers.get_embedding_model() == EMBEDDING_MODEL

    def test_anthropic_selected(self, monkeypatch):
        self._set_provider(monkeypatch, "anthropic")
        assert isinstance(
            providers.get_translation_provider(), AnthropicTranslationProvider
        )

    def test_anthropic_transcription_falls_back_to_gemini(self, monkeypatch):
        """Anthropic has no STT API — the registry falls back to the
        highest-ranked provider (Gemini since 2026-07-14)."""
        self._set_provider(monkeypatch, "anthropic")
        assert isinstance(
            providers.get_transcription_provider(), GeminiTranscriptionProvider
        )

    def test_transcription_independent_of_translation(self, monkeypatch):
        """Translation and transcription providers are chosen separately:
        Claude for translation, OpenAI for the speech-to-text."""
        self._set_provider(monkeypatch, "anthropic", transcription_provider="openai")
        assert isinstance(
            providers.get_translation_provider(), AnthropicTranslationProvider
        )
        assert isinstance(
            providers.get_transcription_provider(), OpenAITranscriptionProvider
        )

    def test_streaming_engine_follows_transcription_provider(self, monkeypatch):
        """The streaming engine is resolved from transcription_provider —
        OpenAI Realtime, Deepgram or Gemini Live."""
        self._set_provider(monkeypatch, "gemini", transcription_provider="deepgram")
        assert isinstance(
            providers.get_streaming_transcription_provider(),
            DeepgramTranscriptionProvider,
        )
        self._set_provider(
            monkeypatch, "gemini", transcription_provider="openai_realtime"
        )
        assert isinstance(
            providers.get_streaming_transcription_provider(),
            OpenAIRealtimeTranscriptionProvider,
        )
        self._set_provider(
            monkeypatch, "openai", transcription_provider="gemini_realtime"
        )
        assert isinstance(
            providers.get_streaming_transcription_provider(),
            GeminiLiveTranscriptionProvider,
        )

    def test_streaming_engine_falls_back_to_default(self, monkeypatch):
        """A non-streaming transcription_provider (stale settings) must not
        break streaming start — it falls back to the default engine
        (Gemini Live since 2026-07-14)."""
        self._set_provider(monkeypatch, "gemini")
        assert isinstance(
            providers.get_streaming_transcription_provider(),
            GeminiLiveTranscriptionProvider,
        )

    def test_fallback_warning_logged_once(self, monkeypatch):
        """_resolve runs every audio segment; the fallback warning must not
        repeat every few seconds."""
        self._set_provider(monkeypatch, "anthropic")
        monkeypatch.setattr(providers, "_warned_fallbacks", set())
        warnings = []
        monkeypatch.setattr(
            providers,
            "log",
            lambda msg, level="INFO": warnings.append(msg)
            if level == "WARNING"
            else None,
        )
        providers.get_transcription_provider()
        providers.get_transcription_provider()
        assert len(warnings) == 1


class TestResolveProviderByKeys:
    """Key-decided provider ("Standard" semantics, onboarding + startup
    repair): the default provider wins whenever its key exists or no key
    exists at all; otherwise the highest-ranked keyed provider."""

    def _keys(self, monkeypatch, keyed):
        monkeypatch.setattr(providers, "has_usable_key", lambda p: p in keyed)

    def test_no_keys_at_all_is_default(self, monkeypatch):
        self._keys(monkeypatch, set())
        assert providers.resolve_provider_by_keys() == "gemini"

    def test_default_key_wins_over_all_others(self, monkeypatch):
        self._keys(monkeypatch, {"gemini", "openai", "anthropic"})
        assert providers.resolve_provider_by_keys() == "gemini"

    def test_only_openai_key_selects_openai(self, monkeypatch):
        self._keys(monkeypatch, {"openai"})
        assert providers.resolve_provider_by_keys() == "openai"

    def test_only_anthropic_key_selects_anthropic(self, monkeypatch):
        self._keys(monkeypatch, {"anthropic"})
        assert providers.resolve_provider_by_keys() == "anthropic"

    def test_ranking_decides_between_non_default_keys(self, monkeypatch):
        self._keys(monkeypatch, {"openai", "anthropic"})
        assert providers.resolve_provider_by_keys() == "openai"

    def test_session_typed_key_counts(self, monkeypatch):
        self._keys(monkeypatch, set())
        assert (
            providers.resolve_provider_by_keys({"anthropic": "sk-ant-x"}) == "anthropic"
        )

    def test_session_typed_default_key_wins(self, monkeypatch):
        self._keys(monkeypatch, {"openai"})
        assert providers.resolve_provider_by_keys({"gemini": "AIza-x"}) == "gemini"

    def test_blank_session_key_is_ignored(self, monkeypatch):
        self._keys(monkeypatch, set())
        assert providers.resolve_provider_by_keys({"anthropic": "   "}) == "gemini"


class TestKeyAwareFallback:
    """Fallback paths pick the highest-ranked provider WITH a usable key
    instead of hardcoded OpenAI. Explicit user choices are never overridden
    — the ranking only applies where a capability is missing."""

    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch):
        # Fresh module state per test; monkeypatch restores the real cache
        # afterwards so other tests never see a ranking left behind here.
        monkeypatch.setattr(providers, "_fallback_cache", {})
        monkeypatch.setattr(providers, "_warned_fallbacks", set())

    def _keys(self, monkeypatch, keyed):
        monkeypatch.setattr(providers, "has_usable_key", lambda p: p in keyed)

    def test_rank_order_wins_when_all_keyed(self, monkeypatch):
        self._keys(monkeypatch, {"openai", "gemini", "anthropic"})
        assert providers.ranked_keyed_provider(["openai", "gemini"]) == "openai"

    def test_keyless_providers_are_skipped(self, monkeypatch):
        self._keys(monkeypatch, {"gemini"})
        assert providers.ranked_keyed_provider(["openai", "gemini"]) == "gemini"

    def test_no_keys_takes_first_candidate(self, monkeypatch):
        self._keys(monkeypatch, set())
        assert providers.ranked_keyed_provider(["openai", "gemini"]) == "openai"

    def test_anthropic_stt_falls_back_to_keyed_gemini(self, monkeypatch):
        """An Anthropic user with only a Gemini key must not 'fall back' to
        OpenAI, which they cannot authenticate with."""
        self._keys(monkeypatch, {"gemini", "anthropic"})
        name, instance = providers._resolve(
            providers._TRANSCRIPTION_PROVIDERS, "transcription", "anthropic"
        )
        assert name == "gemini"
        assert isinstance(instance, GeminiTranscriptionProvider)

    def test_fallback_resolution_is_cached(self, monkeypatch):
        """has_usable_key can hit the OS keyring and _resolve runs per audio
        segment — the ranking must only be computed once."""
        calls = []

        def counting(p):
            calls.append(p)
            return p == "openai"

        monkeypatch.setattr(providers, "has_usable_key", counting)
        providers._resolve(
            providers._TRANSCRIPTION_PROVIDERS, "transcription", "anthropic"
        )
        first = len(calls)
        assert first > 0
        providers._resolve(
            providers._TRANSCRIPTION_PROVIDERS, "transcription", "anthropic"
        )
        assert len(calls) == first

    def test_model_choices_follow_keyed_fallback(self, monkeypatch):
        """The GUI shows the models of the STT engine that will actually run
        under Anthropic — not a hardcoded OpenAI list."""
        self._keys(monkeypatch, {"gemini"})
        assert providers.get_model_choices("anthropic", "transcription") == (
            providers.get_model_choices("gemini", "transcription")
        )
        assert providers.get_default_model("anthropic", "transcription") == (
            providers.get_default_model("gemini", "transcription")
        )


class TestModelChains:
    """Provider-aware model chain resolution."""

    def _set(
        self,
        monkeypatch,
        ai_provider,
        translation_model="",
        transcription_model="",
        transcription_provider=None,
    ):
        tp = (
            transcription_provider
            if transcription_provider is not None
            else ai_provider
        )
        monkeypatch.setattr(
            providers,
            "load_settings",
            lambda: SimpleNamespace(
                ai_provider=ai_provider,
                transcription_provider=tp,
                translation_model=translation_model,
                transcription_model=transcription_model,
            ),
        )

    def test_valid_openai_setting_leads_chain(self, monkeypatch):
        self._set(monkeypatch, "openai", translation_model="gpt-4o-mini")
        chain = providers.get_translation_model_chain()
        assert chain[0] == "gpt-4o-mini"
        assert len(chain) == len(set(chain))  # deduplicated

    def test_unknown_setting_uses_provider_default(self, monkeypatch):
        self._set(monkeypatch, "openai", translation_model="totally-made-up")
        chain = providers.get_translation_model_chain()
        assert chain[0] == providers._MODEL_CHAINS["openai"]["translation"][0]

    def test_openai_model_not_sent_to_gemini(self, monkeypatch):
        """The critical cross-provider case: switching to Gemini with a stale
        OpenAI model id in settings must yield Gemini models only."""
        self._set(monkeypatch, "gemini", translation_model="gpt-5.2")
        chain = providers.get_translation_model_chain()
        assert all(m.startswith("gemini") for m in chain)
        assert chain[0] == providers._MODEL_CHAINS["gemini"]["translation"][0]

    def test_gemini_transcription_chain(self, monkeypatch):
        self._set(monkeypatch, "gemini", transcription_model="gpt-4o-transcribe")
        chain = providers.get_transcription_model_chain()
        assert all(m.startswith("gemini") for m in chain)

    def test_valid_gemini_setting_leads_chain(self, monkeypatch):
        self._set(monkeypatch, "gemini", translation_model="gemini-3.5-flash")
        chain = providers.get_translation_model_chain()
        assert chain[0] == "gemini-3.5-flash"

    def test_openai_model_not_sent_to_anthropic(self, monkeypatch):
        self._set(monkeypatch, "anthropic", translation_model="gpt-5.2")
        chain = providers.get_translation_model_chain()
        assert all(m.startswith("claude") for m in chain)
        assert chain[0] == providers._MODEL_CHAINS["anthropic"]["translation"][0]

    def test_valid_anthropic_setting_leads_chain(self, monkeypatch):
        self._set(monkeypatch, "anthropic", translation_model="claude-haiku-4-5")
        chain = providers.get_translation_model_chain()
        assert chain[0] == "claude-haiku-4-5"

    def test_anthropic_gui_only_model_honored(self, monkeypatch):
        """Opus is offered in the GUI dropdown but not in the fallback chain;
        selecting it must still lead the chain."""
        self._set(monkeypatch, "anthropic", translation_model="claude-opus-4-8")
        chain = providers.get_translation_model_chain()
        assert chain[0] == "claude-opus-4-8"

    def test_anthropic_transcription_chain_uses_ranked_fallback(self, monkeypatch):
        """No Anthropic STT — the chain comes from the ranked key-aware
        fallback (Gemini when no key decides otherwise)."""
        self._set(monkeypatch, "anthropic", transcription_model="")
        monkeypatch.setattr(providers, "has_usable_key", lambda p: False)
        monkeypatch.setattr(providers, "_fallback_cache", {})
        chain = providers.get_transcription_model_chain()
        assert chain[0] == providers._MODEL_CHAINS["gemini"]["transcription"][0]


class TestGeminiTranslationProvider:
    """Request construction for Gemini generate_content."""

    def _client_mock(self, monkeypatch, text=" out "):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=text)
        monkeypatch.setattr(gemini_translation, "get_client", lambda: client)
        return client

    def test_system_prompt_and_options(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        out = GeminiTranslationProvider().complete(
            model="gemini-2.5-flash",
            system_prompt="sys",
            user_prompt="usr",
            max_output_tokens=40,
            temperature=0.2,
        )
        assert out == "out"
        kwargs = client.models.generate_content.call_args.kwargs
        assert kwargs["model"] == "gemini-2.5-flash"
        assert kwargs["contents"] == "usr"
        assert kwargs["config"].system_instruction == "sys"
        assert kwargs["config"].max_output_tokens == 40
        assert kwargs["config"].temperature == 0.2
        assert kwargs["config"].thinking_config.thinking_budget == 0

    def test_user_only_defaults_to_thinking_off(self, monkeypatch):
        # Even a bare call sends a config: Gemini 3.x models think by
        # default, which live subtitles can't afford (probed 2026-07-15).
        client = self._client_mock(monkeypatch)
        GeminiTranslationProvider().complete(model="m", user_prompt="usr")
        cfg = client.models.generate_content.call_args.kwargs["config"]
        assert cfg.system_instruction is None
        assert cfg.thinking_config.thinking_budget == 0

    def test_none_text_returns_empty_string(self, monkeypatch):
        self._client_mock(monkeypatch, text=None)
        out = GeminiTranslationProvider().complete(model="m", user_prompt="usr")
        assert out == ""

    def test_usage_is_recorded_before_text_is_returned(self, monkeypatch):
        client = MagicMock()
        response = SimpleNamespace(text="out", usage_metadata=SimpleNamespace())
        client.models.generate_content.return_value = response
        monkeypatch.setattr(gemini_translation, "get_client", lambda: client)
        captured = []
        monkeypatch.setattr(
            gemini_translation,
            "record_gemini_response",
            lambda resp, **kwargs: captured.append((resp, kwargs)),
        )
        assert GeminiTranslationProvider().complete(model="m", user_prompt="u") == "out"
        assert captured == [(response, {"model": "m", "role": "translation"})]


class TestGeminiTranscriptionProvider:
    def _client_mock(self, monkeypatch, text="transcript"):
        client = MagicMock()
        client.models.generate_content.return_value = SimpleNamespace(text=text)
        monkeypatch.setattr(gemini_transcription, "get_client", lambda: client)
        return client

    def test_audio_part_and_language_hint(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        out = GeminiTranscriptionProvider().transcribe(
            b"wav-bytes", model="gemini-2.5-flash", language="ar"
        )
        assert out == "transcript"
        contents = client.models.generate_content.call_args.kwargs["contents"]
        part, instruction = contents
        assert part.inline_data.mime_type == "audio/wav"
        assert part.inline_data.data == b"wav-bytes"
        assert "verbatim" in instruction
        assert "'ar'" in instruction
        cfg = client.models.generate_content.call_args.kwargs["config"]
        assert cfg.thinking_config.thinking_budget == 0

    def test_auto_detect_has_no_language_hint(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        GeminiTranscriptionProvider().transcribe(b"wav", model="m")
        _, instruction = client.models.generate_content.call_args.kwargs["contents"]
        assert "ISO 639-1" not in instruction

    def test_prompt_added_to_instruction(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        GeminiTranscriptionProvider().transcribe(
            b"wav", model="m", prompt="der letzte Satz"
        )
        _, instruction = client.models.generate_content.call_args.kwargs["contents"]
        assert "der letzte Satz" in instruction

    def test_usage_is_recorded(self, monkeypatch):
        client = MagicMock()
        response = SimpleNamespace(text="spoken", usage_metadata=SimpleNamespace())
        client.models.generate_content.return_value = response
        monkeypatch.setattr(gemini_transcription, "get_client", lambda: client)
        captured = []
        monkeypatch.setattr(
            gemini_transcription,
            "record_gemini_response",
            lambda resp, **kwargs: captured.append((resp, kwargs)),
        )
        assert GeminiTranscriptionProvider().transcribe(b"wav", model="m") == "spoken"
        assert captured == [(response, {"model": "m", "role": "transcription"})]


class TestAnthropicTranslationProvider:
    """Request construction for the Anthropic Messages API."""

    def _client_mock(self, monkeypatch, content=None):
        if content is None:
            content = [SimpleNamespace(type="text", text=" out ")]
        client = MagicMock()
        client.messages.create.return_value = SimpleNamespace(content=content)
        monkeypatch.setattr(anthropic_translation, "get_client", lambda: client)
        return client

    def test_system_prompt_and_options(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        out = AnthropicTranslationProvider().complete(
            model="claude-sonnet-5",
            system_prompt="sys",
            user_prompt="usr",
            max_output_tokens=40,
        )
        assert out == "out"
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["model"] == "claude-sonnet-5"
        assert kwargs["system"] == "sys"
        assert kwargs["max_tokens"] == 40
        assert kwargs["messages"] == [{"role": "user", "content": "usr"}]
        assert kwargs["thinking"] == {"type": "disabled"}

    def test_default_max_tokens(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        AnthropicTranslationProvider().complete(model="m", user_prompt="usr")
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["max_tokens"] == anthropic_translation._DEFAULT_MAX_OUTPUT_TOKENS

    def test_temperature_not_forwarded(self, monkeypatch):
        """Claude Sonnet 5 rejects non-default sampling params (400)."""
        client = self._client_mock(monkeypatch)
        AnthropicTranslationProvider().complete(
            model="m", user_prompt="usr", temperature=0.2
        )
        assert "temperature" not in client.messages.create.call_args.kwargs

    def test_user_only_omits_system(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        AnthropicTranslationProvider().complete(model="m", user_prompt="usr")
        assert "system" not in client.messages.create.call_args.kwargs

    def test_joins_text_blocks_only(self, monkeypatch):
        self._client_mock(
            monkeypatch,
            content=[
                SimpleNamespace(type="text", text="a "),
                SimpleNamespace(type="thinking", thinking="ignored"),
                SimpleNamespace(type="text", text="b"),
            ],
        )
        out = AnthropicTranslationProvider().complete(model="m", user_prompt="usr")
        assert out == "a b"

    def test_empty_content_returns_empty_string(self, monkeypatch):
        self._client_mock(monkeypatch, content=[])
        out = AnthropicTranslationProvider().complete(model="m", user_prompt="usr")
        assert out == ""


class TestAnthropicClientKeyLoading:
    def test_set_and_clear_key(self):
        anthropic_client.set_api_key("sk-ant-test")
        assert anthropic_client.has_api_key()
        anthropic_client.set_api_key(None)
        assert not anthropic_client.has_api_key()

    def test_stored_key_prefers_keyring_over_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: "from-keyring",
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
        assert anthropic_client._load_stored_key() == "from-keyring"

    def test_stored_key_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
        assert anthropic_client._load_stored_key() == "from-env"

    def test_no_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        anthropic_client.set_api_key(None)
        with pytest.raises(RuntimeError):
            anthropic_client.get_client()


class TestProviderChoiceHelpers:
    def test_provider_choices_are_registered(self):
        for _name, provider_id in providers.PROVIDER_CHOICES:
            assert provider_id in providers._TRANSLATION_PROVIDERS

    def test_model_choices_per_provider(self):
        openai_ids = [
            m for _n, m in providers.get_model_choices("openai", "translation")
        ]
        assert "gpt-5.2" in openai_ids
        gemini_ids = [
            m for _n, m in providers.get_model_choices("gemini", "translation")
        ]
        assert all(m.startswith("gemini") for m in gemini_ids)

    def test_unknown_provider_falls_back_to_gemini_choices(self):
        assert providers.get_model_choices(
            "nope", "translation"
        ) == providers.get_model_choices("gemini", "translation")

    def test_default_model(self):
        assert (
            providers.get_default_model("gemini", "translation")
            == "gemini-3.1-flash-lite"
        )
        assert providers.get_default_model("nope", "translation") == (
            providers.get_default_model("gemini", "translation")
        )

    def test_anthropic_choices(self, monkeypatch):
        monkeypatch.setattr(providers, "has_usable_key", lambda p: False)
        monkeypatch.setattr(providers, "_fallback_cache", {})
        translation_ids = [
            m for _n, m in providers.get_model_choices("anthropic", "translation")
        ]
        assert all(m.startswith("claude") for m in translation_ids)
        assert providers.get_default_model("anthropic", "translation") == (
            "claude-sonnet-5"
        )
        # Transcription surfaces the ranked fallback engine's models (Gemini)
        assert providers.get_model_choices("anthropic", "transcription") == (
            providers.get_model_choices("gemini", "transcription")
        )
        assert providers.get_default_model("anthropic", "transcription") == (
            providers.get_default_model("gemini", "transcription")
        )


class TestKeyHelpers:
    def test_stored_key_openai_env_fallback(self, monkeypatch):
        monkeypatch.setattr("utils.settings.get_saved_api_key", lambda: None)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
        assert providers.get_stored_api_key("openai") == "sk-env"

    def test_stored_key_gemini(self, monkeypatch):
        monkeypatch.setattr("providers.gemini.client._load_stored_key", lambda: "g-key")
        assert providers.get_stored_api_key("gemini") == "g-key"

    def test_stored_key_unknown_provider(self):
        assert providers.get_stored_api_key("nope") is None

    def test_save_and_clear_gemini_key(self, monkeypatch):
        calls = {}

        def fake_set(key, provider):
            calls["set"] = (key, provider)
            return True

        def fake_delete(provider):
            calls["delete"] = provider
            return True

        monkeypatch.setattr("utils.keyring_storage.set_api_key_in_keyring", fake_set)
        monkeypatch.setattr(
            "utils.keyring_storage.delete_api_key_from_keyring", fake_delete
        )
        assert providers.save_api_key("gemini", "g-key") is True
        assert calls["set"] == ("g-key", "gemini")
        assert gemini_client.has_api_key()

        providers.clear_api_key("gemini")
        assert calls["delete"] == "gemini"
        assert not gemini_client.has_api_key()

    def test_stored_key_anthropic(self, monkeypatch):
        monkeypatch.setattr(
            "providers.anthropic.client._load_stored_key", lambda: "a-key"
        )
        assert providers.get_stored_api_key("anthropic") == "a-key"

    def test_save_and_clear_anthropic_key(self, monkeypatch):
        calls = {}

        def fake_set(key, provider):
            calls["set"] = (key, provider)
            return True

        def fake_delete(provider):
            calls["delete"] = provider
            return True

        monkeypatch.setattr("utils.keyring_storage.set_api_key_in_keyring", fake_set)
        monkeypatch.setattr(
            "utils.keyring_storage.delete_api_key_from_keyring", fake_delete
        )
        assert providers.save_api_key("anthropic", "sk-ant-key") is True
        assert calls["set"] == ("sk-ant-key", "anthropic")
        assert anthropic_client.has_api_key()

        providers.clear_api_key("anthropic")
        assert calls["delete"] == "anthropic"
        assert not anthropic_client.has_api_key()

    def test_empty_key_not_saved(self):
        assert providers.save_api_key("gemini", "   ") is False

    def test_has_usable_key_unknown_provider(self):
        assert providers.has_usable_key("nope") is False

    def test_stored_key_deepgram(self, monkeypatch):
        monkeypatch.setattr(
            "providers.deepgram.client._load_stored_key", lambda: "d-key"
        )
        assert providers.get_stored_api_key("deepgram") == "d-key"

    def test_save_and_clear_deepgram_key(self, monkeypatch):
        calls = {}

        def fake_set(key, provider):
            calls["set"] = (key, provider)
            return True

        def fake_delete(provider):
            calls["delete"] = provider
            return True

        monkeypatch.setattr("utils.keyring_storage.set_api_key_in_keyring", fake_set)
        monkeypatch.setattr(
            "utils.keyring_storage.delete_api_key_from_keyring", fake_delete
        )
        assert providers.save_api_key("deepgram", "d-key") is True
        assert calls["set"] == ("d-key", "deepgram")
        assert deepgram_client.has_api_key()

        providers.clear_api_key("deepgram")
        assert calls["delete"] == "deepgram"
        assert not deepgram_client.has_api_key()


class TestGeminiClientKeyLoading:
    def test_set_and_clear_key(self):
        gemini_client.set_api_key("g-test")
        assert gemini_client.has_api_key()
        gemini_client.set_api_key(None)
        assert not gemini_client.has_api_key()

    def test_stored_key_prefers_keyring_over_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: "from-keyring",
        )
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        assert gemini_client._load_stored_key() == "from-keyring"

    def test_stored_key_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        assert gemini_client._load_stored_key() == "from-env"

    def test_no_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        gemini_client.set_api_key(None)
        with pytest.raises(RuntimeError):
            gemini_client.get_client()


class TestOpenAITranslationProvider:
    """Message building and response handling."""

    def _capture(self, monkeypatch, content="  out  "):
        captured = {}

        def fake_create(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

        monkeypatch.setattr(openai_translation, "create_chat_completion", fake_create)
        return captured

    def test_system_and_user_messages(self, monkeypatch):
        captured = self._capture(monkeypatch)
        out = OpenAITranslationProvider().complete(
            model="m", system_prompt="sys", user_prompt="usr"
        )
        assert out == "out"
        assert captured["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "usr"},
        ]

    def test_user_only_message_with_options(self, monkeypatch):
        captured = self._capture(monkeypatch)
        OpenAITranslationProvider().complete(
            model="m", user_prompt="usr", max_output_tokens=40, temperature=0.2
        )
        assert captured["messages"] == [{"role": "user", "content": "usr"}]
        assert captured["max_output_tokens"] == 40
        assert captured["temperature"] == 0.2

    def test_none_content_returns_empty_string(self, monkeypatch):
        self._capture(monkeypatch, content=None)
        out = OpenAITranslationProvider().complete(model="m", user_prompt="usr")
        assert out == ""

    def test_truncated_completion_logged(self, monkeypatch):
        """finish_reason 'length' (max_output_tokens hit, e.g. reasoning
        tokens eating the budget) must not pass silently."""

        def fake_create(**kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content="cut off"),
                        finish_reason="length",
                    )
                ]
            )

        monkeypatch.setattr(openai_translation, "create_chat_completion", fake_create)
        logged = []
        monkeypatch.setattr(
            openai_translation, "log", lambda msg, level="INFO": logged.append(level)
        )
        out = OpenAITranslationProvider().complete(model="m", user_prompt="usr")
        assert out == "cut off"
        assert "WARNING" in logged

    def test_usage_response_is_forwarded_to_meter(self, monkeypatch):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="out"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        monkeypatch.setattr(
            openai_translation, "create_chat_completion", lambda **_kwargs: response
        )
        captured = []
        monkeypatch.setattr(
            openai_translation,
            "record_openai_chat_response",
            lambda resp, **kwargs: captured.append((resp, kwargs)),
        )
        assert OpenAITranslationProvider().complete(model="m", user_prompt="u") == "out"
        assert captured == [(response, {"model": "m"})]


class TestOpenAITranscriptionProvider:
    """Request construction for the transcription API."""

    def _client_mock(self, monkeypatch, result="text"):
        client = MagicMock()
        client.audio.transcriptions.create.return_value = result
        monkeypatch.setattr(openai_transcription, "get_client", lambda: client)
        return client

    def test_language_hint_passed(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        out = OpenAITranscriptionProvider().transcribe(
            b"wav-bytes", model="m", language="ar"
        )
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        assert kwargs["model"] == "m"
        assert kwargs["language"] == "ar"
        assert kwargs["file"] == ("audio.wav", b"wav-bytes")
        assert kwargs["response_format"] == "json"
        assert out == "text"

    def test_auto_detect_omits_language(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        OpenAITranscriptionProvider().transcribe(b"wav-bytes", model="m")
        assert "language" not in client.audio.transcriptions.create.call_args.kwargs

    def test_prompt_passed_for_continuity(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        OpenAITranscriptionProvider().transcribe(
            b"wav-bytes", model="m", language="ar", prompt="previous tail"
        )
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        assert kwargs["prompt"] == "previous tail"

    def test_no_prompt_omits_parameter(self, monkeypatch):
        client = self._client_mock(monkeypatch)
        OpenAITranscriptionProvider().transcribe(b"wav-bytes", model="m")
        assert "prompt" not in client.audio.transcriptions.create.call_args.kwargs

    def test_json_response_returns_text_and_records_usage(self, monkeypatch):
        usage = SimpleNamespace(type="tokens", input_tokens=10, output_tokens=2)
        result = SimpleNamespace(text="spoken", usage=usage)
        self._client_mock(monkeypatch, result=result)
        captured = []
        monkeypatch.setattr(
            openai_transcription,
            "record_openai_transcription_usage",
            lambda value, **kwargs: captured.append((value, kwargs)),
        )
        assert OpenAITranscriptionProvider().transcribe(b"wav", model="m") == "spoken"
        assert captured == [(usage, {"model": "m"})]


class TestOpenAIEmbeddingProvider:
    def test_embed_returns_vector(self, monkeypatch):
        client = MagicMock()
        client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1, 0.2])]
        )
        monkeypatch.setattr(openai_embeddings, "get_client", lambda: client)
        out = OpenAIEmbeddingProvider().embed("txt", model="e")
        assert out == [0.1, 0.2]
        client.embeddings.create.assert_called_once_with(model="e", input=["txt"])


class TestOpenAIClientKeyManagement:
    def test_set_and_clear_key(self):
        openai_client.set_api_key("sk-test")
        assert openai_client.has_api_key()
        openai_client.set_api_key(None)
        assert not openai_client.has_api_key()
        with pytest.raises(RuntimeError):
            openai_client.get_client()


class TestPerProviderKeyEntries:
    def test_openai_entry_name_unchanged(self):
        """Existing stored keys must keep working without migration."""
        from utils.keyring_storage import _username_for

        assert _username_for("openai") == "openai_api_key"

    def test_other_providers_get_distinct_entries(self):
        from utils.keyring_storage import _username_for

        assert _username_for("gemini") == "gemini_api_key"
        assert _username_for("anthropic") == "anthropic_api_key"
        assert _username_for("deepgram") == "deepgram_api_key"


class TestDeepgramClientKeyLoading:
    def test_set_and_clear_key(self):
        deepgram_client.set_api_key("d-test")
        assert deepgram_client.has_api_key()
        deepgram_client.set_api_key(None)
        assert not deepgram_client.has_api_key()

    def test_stored_key_prefers_keyring_over_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: "from-keyring",
        )
        monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env")
        assert deepgram_client._load_stored_key() == "from-keyring"

    def test_stored_key_falls_back_to_env(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.setenv("DEEPGRAM_API_KEY", "from-env")
        assert deepgram_client._load_stored_key() == "from-env"

    def test_no_key_raises(self, monkeypatch):
        monkeypatch.setattr(
            "utils.keyring_storage.get_api_key_from_keyring",
            lambda provider=None: None,
        )
        monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
        deepgram_client.set_api_key(None)
        with pytest.raises(RuntimeError):
            deepgram_client.get_client()


class _FakeDeepgramConnection:
    """Stands in for the SDK's V1SocketClient in tests."""

    def __init__(self, messages):
        self._messages = messages
        self._handlers = {}
        self.sent_media = []
        self.closed = False

    def on(self, event_type, callback):
        self._handlers[event_type] = callback

    def start_listening(self):
        from deepgram.core.events import EventType

        cb = self._handlers.get(EventType.MESSAGE)
        if cb:
            for msg in self._messages:
                cb(msg)

    def send_media(self, data):
        self.sent_media.append(data)

    def send_close_stream(self):
        self.closed = True


class _BlockingDeepgramConnection(_FakeDeepgramConnection):
    """Keeps start_listening() blocked until close — lets tests exercise the
    deliberate-shutdown path deterministically (a drained fake would race
    the test's close() call)."""

    def start_listening(self):
        while not self.closed:
            time.sleep(0.005)


def _fail_on_real_error(e):
    """on_error for tests whose scripted fake ends its event stream: the
    providers report that end as "stream ended by server" (the reconnect
    signal) — only OTHER errors are test failures."""
    if "stream ended by server" not in str(e):
        pytest.fail(f"unexpected error: {e}")


def _wait_until(predicate, timeout=5.0, interval=0.01):
    # Generous timeout: the streaming providers spin up a thread (the Gemini
    # one a whole asyncio loop) before the first callback can fire, which
    # can exceed 1s under full-suite load. Passing tests return immediately.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class TestDeepgramTranscriptionProvider:
    """open_stream() runs the Deepgram socket in a background thread —
    these tests fake the SDK's connect() context manager and drive it
    synchronously to verify the message-handling/flush logic."""

    def _make_result(self, transcript, is_final, speech_final=False):
        from deepgram.listen.v1.types.listen_v1results import ListenV1Results

        return ListenV1Results.model_construct(
            type="Results",
            is_final=is_final,
            speech_final=speech_final,
            channel={"alternatives": [{"transcript": transcript}]},
        )

    def _make_utterance_end(self):
        from deepgram.listen.v1.types.listen_v1utterance_end import (
            ListenV1UtteranceEnd,
        )

        return ListenV1UtteranceEnd.model_construct(
            type="UtteranceEnd", channel=[0], last_word_end=1.0
        )

    def _fake_client(self, monkeypatch, messages, connect_error=None, conn=None):
        conn = conn if conn is not None else _FakeDeepgramConnection(messages)
        captured = {}

        @contextmanager
        def fake_connect(**kwargs):
            captured["kwargs"] = kwargs
            if connect_error is not None:
                raise connect_error
            yield conn

        client = SimpleNamespace(
            listen=SimpleNamespace(v1=SimpleNamespace(connect=fake_connect))
        )
        monkeypatch.setattr(deepgram_transcription, "get_client", lambda: client)
        return conn, captured

    def test_open_stream_returns_stream_handle(self, monkeypatch):
        conn, _ = self._fake_client(monkeypatch, [])
        handle = DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="ar",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: None,
        )
        assert isinstance(handle, StreamHandle)

    def test_final_transcript_and_speech_final_flush(self, monkeypatch):
        messages = [
            self._make_result("hello", is_final=False),
            self._make_result("hello world", is_final=True, speech_final=True),
        ]
        self._fake_client(monkeypatch, messages)
        transcripts = []
        utterance_ends = []
        DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda text, is_final: transcripts.append((text, is_final)),
            on_utterance_end=lambda: utterance_ends.append(True),
            on_error=_fail_on_real_error,
        )
        assert _wait_until(lambda: len(utterance_ends) == 1)
        assert transcripts == [("hello", False), ("hello world", True)]

    def test_utterance_end_message_also_flushes(self, monkeypatch):
        messages = [
            self._make_result("hi", is_final=True, speech_final=False),
            self._make_utterance_end(),
        ]
        self._fake_client(monkeypatch, messages)
        utterance_ends = []
        DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: utterance_ends.append(True),
            on_error=_fail_on_real_error,
        )
        assert _wait_until(lambda: len(utterance_ends) == 1)

    def test_feed_sends_media_once_connected(self, monkeypatch):
        conn, _ = self._fake_client(monkeypatch, [])
        handle = DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: None,
        )
        handle.feed(b"pcm-bytes")
        assert _wait_until(lambda: conn.sent_media == [b"pcm-bytes"])

    def test_close_sends_close_stream(self, monkeypatch):
        conn, _ = self._fake_client(monkeypatch, [])
        handle = DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: None,
        )
        assert _wait_until(lambda: conn._handlers != {})
        handle.close()
        assert _wait_until(lambda: conn.closed is True)

    def test_connect_kwargs(self, monkeypatch):
        _, captured = self._fake_client(monkeypatch, [])
        DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="ar",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: None,
        )
        assert _wait_until(lambda: "kwargs" in captured)
        kwargs = captured["kwargs"]
        assert kwargs["model"] == "nova-3"
        assert kwargs["encoding"] == "linear16"
        assert kwargs["language"] == "ar"
        assert kwargs["channels"] == 1

    def test_connection_error_calls_on_error(self, monkeypatch):
        self._fake_client(monkeypatch, [], connect_error=RuntimeError("boom"))
        errors = []
        DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: errors.append(e),
        )
        assert _wait_until(lambda: len(errors) == 1)
        assert isinstance(errors[0], RuntimeError)

    def test_server_end_calls_on_error(self, monkeypatch):
        """The server closing the stream without a close() from our side
        (session cap, idle policy) must be reported — it used to die
        silently and subtitles just stopped."""
        self._fake_client(monkeypatch, [])
        errors = []
        DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: errors.append(e),
        )
        assert _wait_until(lambda: len(errors) == 1)
        assert "stream ended by server" in str(errors[0])

    def test_deliberate_close_suppresses_on_error(self, monkeypatch):
        conn = _BlockingDeepgramConnection([])
        self._fake_client(monkeypatch, [], conn=conn)
        errors = []
        handle = DeepgramTranscriptionProvider().open_stream(
            model="nova-3",
            language="en",
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=lambda e: errors.append(e),
        )
        assert _wait_until(lambda: conn._handlers != {})
        handle.close()
        assert _wait_until(lambda: conn.closed is True)
        time.sleep(0.1)  # give the receive thread time to unwind
        assert errors == []


class TestStreamingEngineHelpers:
    """Per-engine streaming model resolution, key mapping and capture rate."""

    def test_resolve_model_deepgram(self):
        assert (
            providers.resolve_streaming_transcription_model("deepgram", "nova-2")
            == "nova-2"
        )
        # A model id left over from a segmented provider must not be sent
        # to the Deepgram socket.
        assert (
            providers.resolve_streaming_transcription_model(
                "deepgram", "gpt-4o-transcribe"
            )
            == "nova-3"
        )

    def test_resolve_model_openai_realtime(self):
        assert (
            providers.resolve_streaming_transcription_model(
                "openai_realtime", "gpt-4o-mini-transcribe"
            )
            == "gpt-4o-mini-transcribe"
        )
        assert (
            providers.resolve_streaming_transcription_model("openai_realtime", "nova-3")
            == "gpt-4o-transcribe"
        )

    def test_resolve_model_gemini_realtime(self):
        """Only the empirically-verified native-audio family is accepted —
        other Live models (3.1 batched, 3.5 translate) resolve to the default."""
        assert (
            providers.resolve_streaming_transcription_model(
                "gemini_realtime", "gemini-2.5-flash-native-audio-preview-12-2025"
            )
            == "gemini-2.5-flash-native-audio-preview-12-2025"
        )
        for rejected in ("gemini-3.1-flash-live-preview", "gemini-2.5-flash", "nova-3"):
            assert (
                providers.resolve_streaming_transcription_model(
                    "gemini_realtime", rejected
                )
                == "gemini-2.5-flash-native-audio-latest"
            )

    def test_resolve_model_unknown_engine_uses_default_engine(self):
        # Default streaming engine is Gemini Live (2026-07-14 decision).
        assert (
            providers.resolve_streaming_transcription_model("bogus", "whatever")
            == "gemini-2.5-flash-native-audio-latest"
        )

    def test_key_provider_mapping(self):
        """The realtime engines authenticate with their provider's regular
        key; every other id (streaming or not) maps to itself."""
        assert providers.get_streaming_key_provider("openai_realtime") == "openai"
        assert providers.get_streaming_key_provider("gemini_realtime") == "gemini"
        assert providers.get_streaming_key_provider("deepgram") == "deepgram"
        assert providers.get_streaming_key_provider("gemini") == "gemini"

    def test_capture_sample_rate(self):
        from config import FS

        assert providers.get_streaming_capture_sample_rate("deepgram") == FS
        assert providers.get_streaming_capture_sample_rate("gemini_realtime") == FS
        # The OpenAI Realtime API only accepts 24 kHz PCM.
        assert providers.get_streaming_capture_sample_rate("openai_realtime") == 24000
        assert providers.get_streaming_capture_sample_rate("bogus") == FS

    def test_default_model_and_choices(self):
        assert (
            providers.get_default_model("openai_realtime", "transcription")
            == "gpt-4o-transcribe"
        )
        assert (
            providers.get_default_model("gemini_realtime", "transcription")
            == "gemini-2.5-flash-native-audio-latest"
        )
        model_ids = [
            mid
            for _n, mid in providers.get_model_choices(
                "openai_realtime", "transcription"
            )
        ]
        assert model_ids == ["gpt-4o-transcribe", "gpt-4o-mini-transcribe"]


class _FakeRealtimeConnection:
    """Stands in for the SDK's RealtimeConnection in tests."""

    def __init__(self, events):
        self._events = events
        self.session_updates = []
        self.appended_audio = []
        self.closed = False
        # The real connection exposes these as instance-attribute resources
        self.session = SimpleNamespace(
            update=lambda **kw: self.session_updates.append(kw)
        )
        self.input_audio_buffer = SimpleNamespace(
            append=lambda **kw: self.appended_audio.append(kw)
        )

    def __iter__(self):
        yield from self._events

    def close(self):
        self.closed = True


class _BlockingRealtimeConnection(_FakeRealtimeConnection):
    """Yields nothing until close(), then dies like a torn-down socket —
    lets tests exercise the deliberate-shutdown path deterministically."""

    def __iter__(self):
        yield from self._events
        while not self.closed:
            time.sleep(0.005)
        raise RuntimeError("socket torn down")


def _rt_event(etype, **fields):
    return SimpleNamespace(type=etype, **fields)


class TestOpenAIRealtimeTranscriptionProvider:
    """open_stream() runs the Realtime WebSocket in a background thread —
    these tests fake the SDK's realtime.connect() context manager and drive
    the event loop synchronously, mirroring the Deepgram tests above."""

    DELTA = "conversation.item.input_audio_transcription.delta"
    COMPLETED = "conversation.item.input_audio_transcription.completed"
    FAILED = "conversation.item.input_audio_transcription.failed"

    def _fake_client(
        self,
        monkeypatch,
        events,
        connect_error=None,
        conn=None,
        confirmation="session.updated",
        connect_delay=0.0,
    ):
        scripted_events = list(events)
        if confirmation:
            scripted_events.insert(0, _rt_event(confirmation))
        if conn is None:
            conn = _FakeRealtimeConnection(scripted_events)
        else:
            conn._events = scripted_events
        captured = {}

        @contextmanager
        def fake_connect(**kwargs):
            captured["kwargs"] = kwargs
            if connect_delay:
                time.sleep(connect_delay)
            if connect_error is not None:
                raise connect_error
            yield conn

        client = SimpleNamespace(realtime=SimpleNamespace(connect=fake_connect))
        monkeypatch.setattr(openai_realtime, "get_client", lambda: client)
        return conn, captured

    def _open(self, transcripts=None, utterance_ends=None, errors=None):
        return OpenAIRealtimeTranscriptionProvider().open_stream(
            model="gpt-4o-transcribe",
            language="ar",
            on_transcript=(
                (lambda text, is_final: transcripts.append((text, is_final)))
                if transcripts is not None
                else lambda *a: None
            ),
            on_utterance_end=(
                (lambda: utterance_ends.append(True))
                if utterance_ends is not None
                else lambda: None
            ),
            on_error=(
                (lambda e: errors.append(e))
                if errors is not None
                else _fail_on_real_error
            ),
        )

    def test_open_stream_returns_stream_handle(self, monkeypatch):
        self._fake_client(monkeypatch, [])
        handle = self._open()
        assert isinstance(handle, StreamHandle)
        assert handle._ready.is_set()

    def test_session_created_also_confirms_startup(self, monkeypatch):
        self._fake_client(monkeypatch, [], confirmation="session.created")
        handle = self._open()
        assert handle._ready.is_set()

    @pytest.mark.parametrize(
        "confirmation",
        ("transcription_session.created", "transcription_session.updated"),
    )
    def test_legacy_transcription_session_events_confirm_startup(
        self, monkeypatch, confirmation
    ):
        """Accept the lifecycle names emitted by legacy/Beta sessions.

        The GA API uses the unified ``session.*`` names, while the legacy/Beta
        transcription-only path used ``transcription_session.*``. Either is a
        positive server-side confirmation, not a startup timeout.
        """
        self._fake_client(monkeypatch, [], confirmation=confirmation)
        handle = self._open()
        assert handle._ready.is_set()

    def test_deltas_accumulate_and_completed_flushes(self, monkeypatch):
        """Deltas are append-only fragments (unlike Deepgram's replace-the-
        hypothesis interims); completed carries the full utterance text and
        doubles as the utterance-end signal."""
        events = [
            _rt_event(self.DELTA, item_id="i1", delta="As"),
            _rt_event(self.DELTA, item_id="i1", delta="salamu"),
            _rt_event(self.COMPLETED, item_id="i1", transcript="Assalamu alaikum"),
        ]
        self._fake_client(monkeypatch, events)
        transcripts, utterance_ends = [], []
        self._open(transcripts=transcripts, utterance_ends=utterance_ends)
        assert _wait_until(lambda: len(utterance_ends) == 1)
        assert transcripts == [
            ("As", False),
            ("Assalamu", False),
            ("Assalamu alaikum", True),
        ]

    def test_completed_event_records_usage_once(self, monkeypatch):
        usage = SimpleNamespace(type="tokens", input_tokens=10, output_tokens=2)
        events = [
            _rt_event(
                self.COMPLETED,
                item_id="i1",
                event_id="evt-1",
                transcript="hello",
                usage=usage,
            )
        ]
        self._fake_client(monkeypatch, events)
        captured = []
        monkeypatch.setattr(
            openai_realtime,
            "record_openai_transcription_usage",
            lambda value, **kwargs: captured.append((value, kwargs)),
        )
        ends = []
        self._open(utterance_ends=ends)
        assert _wait_until(lambda: len(ends) == 1)
        assert captured == [
            (usage, {"model": "gpt-4o-transcribe", "event_id": "evt-1"})
        ]

    def test_failed_transcription_still_flushes(self, monkeypatch):
        """A failed utterance must clear the pending interim (empty flush)
        instead of leaving it on screen forever."""
        events = [
            _rt_event(self.DELTA, item_id="i1", delta="doomed"),
            _rt_event(self.FAILED, item_id="i1", error=SimpleNamespace(message="x")),
        ]
        self._fake_client(monkeypatch, events)
        transcripts, utterance_ends = [], []
        self._open(transcripts=transcripts, utterance_ends=utterance_ends)
        assert _wait_until(lambda: len(utterance_ends) == 1)
        assert transcripts == [("doomed", False)]  # no final for the failure

    def test_error_event_calls_on_error(self, monkeypatch):
        events = [_rt_event("error", error=SimpleNamespace(message="rate limited"))]
        self._fake_client(monkeypatch, events)
        errors = []
        self._open(errors=errors)
        # The scripted events then run out, which now also reports a
        # server-side stream end — the error event must come first.
        assert _wait_until(lambda: len(errors) >= 1)
        assert "rate limited" in str(errors[0])

    def test_feed_appends_base64_audio(self, monkeypatch):
        import base64

        conn, _ = self._fake_client(monkeypatch, [])
        handle = self._open()
        handle.feed(b"pcm-bytes")
        assert _wait_until(lambda: len(conn.appended_audio) == 1)
        assert conn.appended_audio[0]["audio"] == base64.b64encode(b"pcm-bytes").decode(
            "ascii"
        )

    def test_session_configured_for_transcription(self, monkeypatch):
        conn, captured = self._fake_client(monkeypatch, [])
        self._open()
        assert _wait_until(lambda: len(conn.session_updates) == 1)
        assert captured["kwargs"]["extra_query"] == {"intent": "transcription"}
        transport_options = captured["kwargs"]["websocket_connection_options"]
        assert transport_options["open_timeout"] == (
            openai_realtime.WEBSOCKET_OPEN_TIMEOUT_SECONDS
        )
        assert transport_options["close_timeout"] == (
            openai_realtime.WEBSOCKET_CLOSE_TIMEOUT_SECONDS
        )
        assert (
            openai_realtime.STARTUP_TIMEOUT_SECONDS
            > transport_options["open_timeout"]
        )
        session = conn.session_updates[0]["session"]
        assert session["type"] == "transcription"
        audio_input = session["audio"]["input"]
        assert audio_input["format"] == {"type": "audio/pcm", "rate": 24000}
        assert audio_input["transcription"] == {
            "model": "gpt-4o-transcribe",
            "language": "ar",
        }
        assert audio_input["turn_detection"] == {"type": "server_vad"}

    def test_language_omitted_when_none(self, monkeypatch):
        conn, _ = self._fake_client(monkeypatch, [])
        OpenAIRealtimeTranscriptionProvider().open_stream(
            model="gpt-4o-transcribe",
            language=None,
            on_transcript=lambda *a: None,
            on_utterance_end=lambda: None,
            on_error=_fail_on_real_error,
        )
        assert _wait_until(lambda: len(conn.session_updates) == 1)
        transcription = conn.session_updates[0]["session"]["audio"]["input"][
            "transcription"
        ]
        assert transcription == {"model": "gpt-4o-transcribe"}

    def test_connect_error_raises_synchronously(self, monkeypatch):
        self._fake_client(monkeypatch, [], connect_error=RuntimeError("boom"))
        errors = []
        with pytest.raises(RuntimeError, match="boom"):
            self._open(errors=errors)
        assert errors == []

    def test_error_before_session_confirmation_raises_synchronously(self, monkeypatch):
        self._fake_client(
            monkeypatch,
            [_rt_event("error", error=SimpleNamespace(message="invalid_api_key"))],
            confirmation=None,
        )
        errors = []
        with pytest.raises(RuntimeError, match="invalid_api_key"):
            self._open(errors=errors)
        assert errors == []

    def test_startup_confirmation_wait_is_bounded(self, monkeypatch):
        conn = _BlockingRealtimeConnection([])
        self._fake_client(monkeypatch, [], conn=conn, confirmation=None)
        monkeypatch.setattr(openai_realtime, "STARTUP_TIMEOUT_SECONDS", 0.03)

        with pytest.raises(TimeoutError, match="session confirmation"):
            self._open(errors=[])

        assert conn.closed is True

    def test_slow_websocket_handshake_can_still_confirm(self, monkeypatch):
        """A connection that is merely slow must not lose a timeout race."""
        self._fake_client(monkeypatch, [], connect_delay=0.04)
        monkeypatch.setattr(openai_realtime, "STARTUP_TIMEOUT_SECONDS", 0.1)

        handle = self._open(errors=[])

        assert handle._ready.is_set()

    def test_deliberate_close_suppresses_on_error(self, monkeypatch):
        """close() tears the socket down mid-recv; that expected shutdown
        must not surface as a connection-error subtitle."""
        conn = _BlockingRealtimeConnection([])
        self._fake_client(monkeypatch, [], conn=conn)
        errors = []
        handle = self._open(errors=errors)
        assert _wait_until(lambda: len(conn.session_updates) == 1)
        handle.close()
        assert conn.closed is True
        time.sleep(0.1)  # give the receive thread time to hit the raise
        assert errors == []

    def test_server_end_calls_on_error(self, monkeypatch):
        """The server closing the session without a close() from our side
        must be reported (the SDK swallows ConnectionClosedOK, so this used
        to die silently)."""
        self._fake_client(monkeypatch, [])
        errors = []
        self._open(errors=errors)
        assert _wait_until(lambda: len(errors) == 1)
        assert "stream ended by server" in str(errors[0])


class _FakeLiveSession:
    """Stands in for google-genai's AsyncSession in tests.

    ``turns`` is a list of message-lists — each inner list is what one
    ``receive()`` pass yields (the SDK's iterator ends per turn). With
    ``stay_open`` the session blocks after the scripted turns until close()
    is called, then yields nothing (the provider's drained-socket exit)."""

    def __init__(self, turns, stay_open=False):
        self._turns = list(turns)
        self._stay_open = stay_open
        self._close_event = None
        self.sent = []
        self.close_called = False

    async def send_realtime_input(self, **kwargs):
        self.sent.append(kwargs)

    async def close(self):
        self.close_called = True
        if self._close_event is not None:
            self._close_event.set()

    def receive(self):
        async def gen():
            import asyncio

            if self._turns:
                for message in self._turns.pop(0):
                    yield message
                return
            # close_called check: a close() that lands before this generator
            # created the event must not leave the session awaiting forever.
            if self._stay_open and not self.close_called:
                if self._close_event is None:
                    self._close_event = asyncio.Event()
                await self._close_event.wait()
            # yields nothing -> provider treats the socket as drained

        return gen()


def _live_msg(text=None, turn_complete=False):
    return SimpleNamespace(
        server_content=SimpleNamespace(
            input_transcription=(
                SimpleNamespace(text=text, finished=None) if text else None
            ),
            turn_complete=turn_complete,
        )
    )


class TestGeminiLiveTranscriptionProvider:
    """open_stream() runs a private asyncio loop in a background thread —
    these tests fake the SDK's aio.live.connect() context manager and drive
    the receive loop synchronously, mirroring the other streaming engines."""

    @pytest.fixture(autouse=True)
    def _join_receive_threads(self, monkeypatch):
        """Close and join every stream opened by the test. A leaked receive
        thread that starts late otherwise grabs the NEXT test's monkeypatched
        fake client and silently consumes its scripted turns (the
        load-order-dependent flake this class used to have). Depending on
        monkeypatch keeps this test's fakes alive during the join."""
        self._handles = []
        yield
        for handle in self._handles:
            handle.close()
            if handle._thread is not None:
                handle._thread.join(timeout=5)

    def _fake_client(self, monkeypatch, session, connect_error=None):
        from contextlib import asynccontextmanager

        captured = {}

        @asynccontextmanager
        async def fake_connect(**kwargs):
            captured["kwargs"] = kwargs
            if connect_error is not None:
                raise connect_error
            yield session

        client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=fake_connect))
        )
        monkeypatch.setattr(gemini_realtime, "get_live_client", lambda: client)
        return captured

    def _open(self, transcripts=None, utterance_ends=None, errors=None):
        handle = GeminiLiveTranscriptionProvider().open_stream(
            model="gemini-2.5-flash-native-audio-latest",
            language="ar",
            on_transcript=(
                (lambda text, is_final: transcripts.append((text, is_final)))
                if transcripts is not None
                else lambda *a: None
            ),
            on_utterance_end=(
                (lambda: utterance_ends.append(True))
                if utterance_ends is not None
                else lambda: None
            ),
            on_error=(
                (lambda e: errors.append(e))
                if errors is not None
                else _fail_on_real_error
            ),
        )
        self._handles.append(handle)
        return handle

    def test_open_stream_returns_stream_handle(self, monkeypatch):
        self._fake_client(monkeypatch, _FakeLiveSession([]))
        handle = self._open()
        assert isinstance(handle, StreamHandle)

    def test_fragments_accumulate_and_turn_complete_flushes(self, monkeypatch):
        """Input-transcription fragments are append-only pieces accumulated
        per turn; turn_complete is the utterance boundary."""
        session = _FakeLiveSession(
            [
                [
                    _live_msg(text="As"),
                    _live_msg(text="salamu"),
                    _live_msg(turn_complete=True),
                ]
            ]
        )
        self._fake_client(monkeypatch, session)
        transcripts, utterance_ends = [], []
        self._open(transcripts=transcripts, utterance_ends=utterance_ends)
        assert _wait_until(lambda: len(utterance_ends) == 1)
        assert transcripts == [
            ("As", False),
            ("Assalamu", False),
            ("Assalamu", True),
        ]

    def test_accumulation_resets_between_turns(self, monkeypatch):
        session = _FakeLiveSession(
            [
                [_live_msg(text="one"), _live_msg(turn_complete=True)],
                [_live_msg(text="two"), _live_msg(turn_complete=True)],
            ]
        )
        self._fake_client(monkeypatch, session)
        transcripts, utterance_ends = [], []
        self._open(transcripts=transcripts, utterance_ends=utterance_ends)
        assert _wait_until(lambda: len(utterance_ends) == 2)
        assert ("two", True) in transcripts
        assert ("onetwo", False) not in transcripts

    def test_empty_turn_still_signals_utterance_end(self, monkeypatch):
        """A turn without any transcription must flush empty so a pending
        interim can't linger on the subtitle window."""
        session = _FakeLiveSession([[_live_msg(turn_complete=True)]])
        self._fake_client(monkeypatch, session)
        transcripts, utterance_ends = [], []
        self._open(transcripts=transcripts, utterance_ends=utterance_ends)
        assert _wait_until(lambda: len(utterance_ends) == 1)
        assert transcripts == []

    def test_usage_only_messages_forward_cumulative_snapshot(self, monkeypatch):
        def metadata(total):
            return SimpleNamespace(
                prompt_token_count=total,
                prompt_tokens_details=[
                    SimpleNamespace(modality="AUDIO", token_count=total)
                ],
                cached_content_token_count=0,
                cache_tokens_details=None,
                response_token_count=0,
                response_tokens_details=None,
                thoughts_token_count=0,
                tool_use_prompt_token_count=0,
            )

        session = _FakeLiveSession(
            [[
                SimpleNamespace(server_content=None, usage_metadata=metadata(100)),
                SimpleNamespace(server_content=None, usage_metadata=metadata(150)),
                _live_msg(turn_complete=True),
            ]]
        )
        self._fake_client(monkeypatch, session)
        captured = []
        monkeypatch.setattr(
            gemini_realtime,
            "record_live_usage_snapshot",
            lambda **kwargs: captured.append(kwargs),
        )
        ends = []
        self._open(utterance_ends=ends)
        assert _wait_until(lambda: len(ends) == 1)
        assert [row["usage"]["input_audio_tokens"] for row in captured] == [100, 150]
        assert len({row["stream_id"] for row in captured}) == 1

    def test_feed_sends_pcm_with_mime(self, monkeypatch):
        from config import FS

        session = _FakeLiveSession([], stay_open=True)
        self._fake_client(monkeypatch, session)
        handle = self._open()
        handle.feed(b"pcm-bytes")
        assert _wait_until(lambda: len(session.sent) == 1)
        assert session.sent[0]["audio"] == {
            "data": b"pcm-bytes",
            "mime_type": f"audio/pcm;rate={FS}",
        }
        handle.close()

    def test_session_config(self, monkeypatch):
        from config import STREAMING_GEMINI_SILENCE_MS

        session = _FakeLiveSession([])
        captured = self._fake_client(monkeypatch, session)
        self._open()
        assert _wait_until(lambda: "kwargs" in captured)
        assert captured["kwargs"]["model"] == "gemini-2.5-flash-native-audio-latest"
        config = captured["kwargs"]["config"]
        assert config["response_modalities"] == ["AUDIO"]
        assert config["proactivity"] == {"proactive_audio": True}
        assert config["input_audio_transcription"] == {}
        assert (
            config["realtime_input_config"]["automatic_activity_detection"][
                "silence_duration_ms"
            ]
            == STREAMING_GEMINI_SILENCE_MS
        )

    def test_connect_error_calls_on_error(self, monkeypatch):
        self._fake_client(
            monkeypatch, _FakeLiveSession([]), connect_error=RuntimeError("boom")
        )
        errors = []
        self._open(errors=errors)
        assert _wait_until(lambda: len(errors) == 1)
        assert isinstance(errors[0], RuntimeError)

    def test_deliberate_close_suppresses_on_error(self, monkeypatch):
        session = _FakeLiveSession([], stay_open=True)
        self._fake_client(monkeypatch, session)
        errors = []
        handle = self._open(errors=errors)
        assert _wait_until(lambda: handle._ready.is_set())
        handle.close()
        assert _wait_until(lambda: session.close_called)
        time.sleep(0.1)  # give the receive thread time to unwind
        assert errors == []

    def test_server_end_calls_on_error(self, monkeypatch):
        """A drained receive loop without a close() from our side means the
        server ended the session — must be reported, not die silently."""
        self._fake_client(monkeypatch, _FakeLiveSession([]))
        errors = []
        self._open(errors=errors)
        assert _wait_until(lambda: len(errors) == 1)
        assert "stream ended by server" in str(errors[0])


class TestInsecureKeyFallback:
    """save_api_key() returns False both when a key lands in the plaintext
    settings file (OpenAI's legacy fallback) and when it is not persisted at
    all (every other provider). The GUI must tell those apart, or it reports
    "saved" for a key that is gone after the next restart.
    """

    def test_only_openai_persists_without_a_keychain(self):
        assert providers.has_insecure_key_fallback("openai") is True
        for provider in ("gemini", "anthropic", "deepgram"):
            assert providers.has_insecure_key_fallback(provider) is False

    def test_unknown_provider_has_no_fallback(self):
        assert providers.has_insecure_key_fallback("nonexistent") is False

    def test_save_reports_not_secure_without_a_keychain(self, monkeypatch):
        """Guards the premise: with no keyring backend every provider reports
        an insecure save, which is why has_insecure_key_fallback is needed to
        pick the right warning."""
        monkeypatch.setattr(
            "utils.keyring_storage._check_keyring_available", lambda: False
        )
        monkeypatch.setattr(providers, "_client_module", lambda p: MagicMock())
        for provider in ("gemini", "anthropic", "deepgram"):
            assert providers.save_api_key(provider, "k-123") is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
