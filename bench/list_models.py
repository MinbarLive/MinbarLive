"""Which models do the provider APIs list, compared with what the app offers?

Free to run: it calls only the providers' model-list endpoints, never a
completion. Run it now and then to spot two things (see the refresh-models
skill in .claude/skills/ for the whole procedure):

- **Offered but not listed** — a model a dropdown or fallback chain still
  names, which the provider no longer lists. Probe it and remove it.
- **Listed, not offered** — a candidate. Listed is NOT usable: in the
  2026-10-14 refresh, half the candidates were listed and then failed (a
  thinking setting they reject, an empty transcript, a turn-detection mode
  they refuse). Probe a candidate with ``bench/probe_models.py`` before adding.

Keys come from the app's keychain or the environment / a ``.env`` in the
current directory, the same way ``bench/run_bench.py`` finds them. A provider
without a key is skipped.

Usage (from the repo root):
    python bench/list_models.py          # the comparison
    python bench/list_models.py --all    # also every listed model, unfiltered
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A directly-run script puts bench/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Models the app has no use for. Hidden from the candidate list (never from
# --all) so the handful worth probing are not buried under a hundred others.
# Widen it when a new family turns up that can never be a candidate.
_IRRELEVANT = re.compile(
    # Not text or speech-to-text at all.
    r"image|tts|sora|veo|lyria|banana|moderation|search|diarize|^text-embedding"
    # Speech-to-speech voice models: the app transcribes, it never talks back.
    r"|^gpt-audio|^gpt-realtime(-mini|-\d[\d.]*(-mini)?)?$|^gpt-live-\d"
    # Coding, agent and research products.
    r"|codex|computer-use|robotics|deep-research|antigravity"
    # Premium tiers too slow for live subtitles, and aliases that move.
    r"|-pro$|-latest$"
    # Legacy generations.
    r"|^gpt-3\.5|^gpt-4(-|$)|^o\d|babbage|davinci|instruct|^aqa$"
    # Deepgram: domain-tuned variants, its hosted Whisper (no streaming), and
    # the generations before Nova-2.
    r"|automotive|drivethru|finance|meeting|phonecall|voicemail|medical|pharma"
    r"|-atc$|conversationalai|video$|phoneme|^whisper-|dQw4w9WgXcQ|polaris|-ea$"
    r"|^(enhanced-|nova-)?general$|^enhanced-"
)
# Candidates already probed and turned down, with why and when. Printed as
# their own group so the next check shows what is NEW instead of re-listing
# the same failures. Add to it whenever a probe rejects a model; re-probe one
# only when its reason may have changed (a price cut, a new snapshot).
ALREADY_REJECTED: dict[str, str] = {
    "gpt-5.6-sol": "as fast as gpt-6-sol at twice the price (2026-10-14)",
    "gpt-5.6-terra": "~2.9 s per sentence (2026-10-14)",
    "gpt-6-astra": 'doubles the honorific ("Allahs ﷻ ﷺ"), $10/$50 (2026-10-14)',
    "gpt-6.1-sol": 'doubles the honorific ("Allahs ﷻ ﷺ"), 2.8 s (2026-10-14)',
    "gpt-6-luna": "~3.4 s per sentence (2026-10-14)",
    "gpt-live-transcribe": "refuses server_vad turn detection (2026-10-14)",
    "gpt-realtime-whisper": "refuses server_vad turn detection (2026-10-14)",
    "gemini-2.5-flash": "404 for new accounts (2026-07-15)",
    "gemini-2.5-flash-lite": "404 for new accounts (2026-07-15)",
    "gemini-3.1-flash-live-preview": "no turn boundaries, one batched transcript (2026-07)",
    "gemini-3.5-live-translate-preview": "translates everything itself; out by design (2026-07)",
    "gemini-3.5-transcribe": "empty transcript; refuses every thinking setting (2026-10-14)",
    "gemini-3.5-transcribe-live": "no finals or turn boundaries (2026-10-14)",
    "gemini-3.7-flash": "refuses thinking 'minimal'; slower than the lite models (2026-10-14)",
    "gemini-3.8-flash": "refuses thinking 'minimal'; slower than the lite models (2026-10-14)",
    "gemini-4-argon": "refuses thinking 'minimal'; 2.5 s translation (2026-10-14)",
    "gemini-3.8-live": "refuses both AUDIO and TEXT replies (2026-10-14)",
}
# A dated snapshot of a model (gpt-4o-mini-transcribe-2025-12-15,
# claude-haiku-4-5-20251001), or Deepgram's "-general" form of an offered id.
_SUFFIX = r"(-\d{4}-\d{2}-\d{2}|-\d{8}|-general)?"


def api_key(provider: str) -> str | None:
    """The provider's stored key, as the app itself would find it."""
    import providers

    return providers.get_stored_api_key(provider)


def offered_models() -> dict[str, set[str]]:
    """Every model id the app can send: dropdowns, defaults and fallbacks."""
    import config
    import providers.anthropic as anthropic_models
    import providers.deepgram as deepgram_models
    import providers.gemini as gemini_models
    from providers.gemini import realtime as gemini_live
    from providers.openai import realtime as openai_realtime
    from utils import settings

    def ids(pairs):
        return {model_id for _, model_id in pairs}

    return {
        "openai": ids(settings.TRANSLATION_MODELS)
        | ids(settings.TRANSCRIPTION_MODELS)
        | set(settings.FALLBACK_TRANSLATION_MODELS)
        | set(settings.FALLBACK_TRANSCRIPTION_MODELS)
        | ids(openai_realtime.TRANSCRIPTION_MODELS)
        | {config.EMBEDDING_MODEL},
        "gemini": ids(gemini_models.TRANSLATION_MODELS)
        | ids(gemini_models.TRANSCRIPTION_MODELS)
        | set(gemini_models.FALLBACK_TRANSLATION_MODELS)
        | set(gemini_models.FALLBACK_TRANSCRIPTION_MODELS)
        | ids(gemini_live.TRANSCRIPTION_MODELS)
        | {config.GEMINI_EMBEDDING_MODEL},
        "anthropic": ids(anthropic_models.TRANSLATION_MODELS)
        | set(anthropic_models.FALLBACK_TRANSLATION_MODELS),
        "deepgram": ids(deepgram_models.TRANSCRIPTION_MODELS),
    }


def _list_openai(key: str) -> dict[str, str]:
    from openai import OpenAI

    # The clients are kept in a local: a client garbage-collected mid-listing
    # closes its connection under the pager (Gemini's does, measurably).
    client = OpenAI(api_key=key)
    return {m.id: "" for m in client.models.list()}


def _list_gemini(key: str) -> dict[str, str]:
    from google import genai

    # supported_actions tells the two Gemini paths apart: generateContent is
    # segmented STT / translation, bidiGenerateContent is a Live model.
    client = genai.Client(api_key=key)
    return {
        m.name.removeprefix("models/"): ", ".join(m.supported_actions or [])
        for m in client.models.list()
    }


def _list_anthropic(key: str) -> dict[str, str]:
    import anthropic

    client = anthropic.Anthropic(api_key=key)
    return {m.id: m.display_name for m in client.models.list()}


def _list_deepgram(key: str) -> dict[str, str]:
    import httpx

    resp = httpx.get(
        "https://api.deepgram.com/v1/models",
        headers={"Authorization": f"Token {key}"},
        timeout=30,
    )
    resp.raise_for_status()
    # One entry per version/language variant; merged by name. Speech-to-text
    # only — the app never uses Deepgram's voices.
    languages: dict[str, set[str]] = {}
    for m in resp.json().get("stt", []):
        languages.setdefault(m["canonical_name"], set()).update(
            m.get("languages") or []
        )
    return {name: f"{len(langs)} languages" for name, langs in languages.items()}


LISTERS = {
    "openai": _list_openai,
    "gemini": _list_gemini,
    "anthropic": _list_anthropic,
    "deepgram": _list_deepgram,
}


def _is_form_of(listed: str, offered: str) -> bool:
    return re.fullmatch(re.escape(offered) + _SUFFIX, listed) is not None


def compare(offered: set[str], listed: dict[str, str]) -> tuple[list, list, list, int]:
    """(offered but not listed, new candidates, already rejected, hidden count)."""
    missing = sorted(o for o in offered if not any(_is_form_of(m, o) for m in listed))
    candidates, rejected, hidden = [], [], 0
    for model in sorted(listed):
        if any(_is_form_of(model, o) for o in offered):
            continue
        # A dated snapshot of a model that is itself listed adds nothing.
        base = re.sub(r"(-\d{4}-\d{2}-\d{2}|-\d{8})$", "", model)
        if base != model and base in listed:
            continue
        if model in ALREADY_REJECTED:
            rejected.append(model)
        elif _IRRELEVANT.search(model):
            hidden += 1
        else:
            candidates.append(model)
    return missing, candidates, rejected, hidden


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="also print every listed model")
    args = ap.parse_args()

    from dotenv import load_dotenv

    load_dotenv()
    offered = offered_models()

    for provider, lister in LISTERS.items():
        key = api_key(provider)
        if not key:
            print(f"\n=== {provider}: skipped, no API key ===")
            continue
        try:
            listed = lister(key)
        except Exception as e:  # one provider failing shouldn't hide the others
            print(f"\n=== {provider}: ERROR {type(e).__name__}: {e} ===")
            continue
        missing, candidates, rejected, hidden = compare(offered[provider], listed)
        print(
            f"\n=== {provider}: {len(listed)} listed, "
            f"{len(offered[provider])} offered by the app ==="
        )
        print("Offered but NOT listed (probe, then remove):")
        for model in missing or ["(none)"]:
            print(f"  {model}")
        print("Listed, not offered (candidates; probe before adding):")
        for model in candidates or ["(none)"]:
            note = listed.get(model, "")
            print(f"  {model}" + (f"  [{note}]" if note else ""))
        if rejected:
            print("Already probed and rejected (re-probe only if the reason changed):")
            for model in rejected:
                print(f"  {model}: {ALREADY_REJECTED[model]}")
        if hidden:
            print(
                f"  ({hidden} more hidden: image/video/voice/search/coding models; --all)"
            )
        if args.all:
            print("Everything listed:")
            for model in sorted(listed):
                print(f"  {model}" + (f"  [{listed[model]}]" if listed[model] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
