"""Probe candidate models through the app's own providers. REAL, PAID API calls.

The second half of a model refresh; ``bench/list_models.py`` is the first.
A listed model is not a usable one, so every check here goes through the same
provider code and prompts the app uses: "works" means it works in MinbarLive,
not merely against the raw API.

--translation provider:model ...
    Four Arabic sentences x --rounds, Arabic -> German with the Islamic-mode
    prompts, no RAG hint. Reports median/max latency and flags the faults that
    have actually decided past refreshes: a missing or doubled honorific, a
    German passage not kept as-is, Arabic script leaking into the German.

--stt engine:model ...
    ``engine`` is an app transcription-provider id. ``openai`` / ``gemini`` are
    segmented: a ~15 s clip, --rounds calls, latency and character error rate.
    ``openai_realtime`` / ``gemini_realtime`` / ``deepgram`` stream a ~52 s clip
    at real-time pace: lag after the speech ends, accuracy, turn ends, errors.

The speech is OpenAI TTS of a known Arabic sermon text (so an OpenAI key is
needed), cached in bench/results/. It is far cleaner than mosque audio: it
shows whether a model works and how fast, not which copes best with a reverberant
hall. Judge that in a real session before making anything a default.

Usage (from the repo root):
    python bench/probe_models.py --translation openai:gpt-6-sol gemini:gemini-3.7-flash
    python bench/probe_models.py --stt openai:gpt-transcribe openai_realtime:gpt-transcribe

Cost: cents — about 8 short completions per translation model, and roughly a
minute of billed audio per streaming model.
"""

from __future__ import annotations

import argparse
import importlib
import io
import re
import statistics
import sys
import threading
import time
import wave
from pathlib import Path

# A directly-run script puts bench/ on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS_DIR = Path(__file__).parent / "results"

SENTENCES = {
    "medium": "إن هذه الدنيا دار ممر لا دار مقر، فاعملوا لآخرتكم قبل أن يأتي يوم لا ينفع فيه مال ولا بنون.",
    "codeswitch": "نحن اليوم في مدينة München نتحدث عن أهمية الصلاة في حياة المسلم.",
    "long": (
        "اعلموا رحمكم الله أن الإنسان في هذه الحياة الدنيا مبتلى بالخير والشر والغنى "
        "والفقر والصحة والمرض ليظهر صدق إيمانه وحسن توكله على ربه، فمن صبر عند البلاء "
        "وشكر عند الرخاء فقد فاز فوزا عظيما."
    ),
    "prophet": "قال رسول الله صلى الله عليه وسلم: إنما الأعمال بالنيات، وإنما لكل امرئ ما نوى.",
}

SPEECH_SHORT = (
    "الحمد لله نحمده ونستعينه ونستغفره، ونعوذ بالله من شرور أنفسنا ومن سيئات "
    "أعمالنا، من يهده الله فلا مضل له، ومن يضلل فلا هادي له."
)
SPEECH_LONG = (
    "أما بعد، فيا أيها الناس اتقوا الله حق تقواه، واعلموا أن هذه الدنيا دار ممر "
    "لا دار مقر. فاعملوا لآخرتكم قبل أن يأتي يوم لا ينفع فيه مال ولا بنون إلا من "
    "أتى الله بقلب سليم. واعلموا رحمكم الله أن الإنسان في هذه الحياة مبتلى بالخير "
    "والشر، والغنى والفقر، والصحة والمرض، ليظهر صدق إيمانه وحسن توكله على ربه. "
    "فمن صبر عند البلاء وشكر عند الرخاء فقد فاز فوزا عظيما. وقد قال رسول الله صلى "
    "الله عليه وسلم: عجبا لأمر المؤمن إن أمره كله خير. فحافظوا على الصلوات الخمس "
    "في أوقاتها، وصلوا أرحامكم، وأحسنوا إلى جيرانكم، وكفوا أذاكم عن الناس."
)
TTS_RATE = 24000  # gpt-4o-mini-tts "pcm" output: 24 kHz mono int16
STREAMING_ENGINES = ("openai_realtime", "gemini_realtime", "deepgram")


def _parse(pairs: list[str]) -> list[tuple[str, str]]:
    out = []
    for pair in pairs:
        provider, sep, model = pair.partition(":")
        if not sep or not model:
            raise SystemExit(f"expected provider:model, got {pair!r}")
        out.append((provider, model))
    return out


def _activate_key(provider_id: str) -> None:
    import providers

    key_provider = providers.get_streaming_key_provider(provider_id)
    key = providers.get_stored_api_key(key_provider)
    if not key:
        raise SystemExit(f"No API key for {key_provider} (keychain or .env)")
    importlib.import_module(f"providers.{key_provider}.client").set_api_key(key)


# --------------------------------------------------------------------------
# Translation
# --------------------------------------------------------------------------
def probe_translation(provider_id: str, model: str, rounds: int) -> None:
    from providers import get_translation_provider_for
    from translation.translator import _build_system_prompt, _build_user_prompt

    provider = get_translation_provider_for(provider_id)
    system_prompt = _build_system_prompt("Arabic", "German")
    times, outputs = [], {}
    for _ in range(rounds):
        for name, text in SENTENCES.items():
            user_prompt = _build_user_prompt(text, "", "", "Arabic", "German")
            started = time.perf_counter()
            try:
                out = provider.complete(
                    model=model, system_prompt=system_prompt, user_prompt=user_prompt
                )
            except Exception as e:
                print(f"{provider_id}:{model}  FAILED  {type(e).__name__}: {e}")
                return
            times.append(time.perf_counter() - started)
            outputs.setdefault(name, out)

    flags = []
    prophet = outputs["prophet"]
    if "ﷺ" not in prophet:
        flags.append("no ﷺ for the Prophet")
    if "ﷻ ﷺ" in prophet or "ﷺ ﷺ" in prophet:
        flags.append("doubled honorific")
    if "München" not in outputs["codeswitch"]:
        flags.append("did not keep 'München'")
    leaked = [
        word
        for out in outputs.values()
        for word in re.findall(r"\S*[؀-ۿ]\S*", out)
        if word not in ("ﷺ", "ﷻ")
    ]
    if leaked:
        flags.append(f"Arabic left in the German: {leaked[:3]}")
    print(
        f"{provider_id}:{model}  median {statistics.median(times):.2f}s  "
        f"max {max(times):.2f}s  {'; '.join(flags) or 'no faults flagged'}"
    )
    for name in ("prophet", "long"):
        print(f"    [{name}] {outputs[name]}")


# --------------------------------------------------------------------------
# Speech
# --------------------------------------------------------------------------
def _speech(name: str, text: str) -> bytes:
    """24 kHz PCM of ``text`` spoken by OpenAI TTS, cached per text."""
    import hashlib

    from providers.openai.client import get_client

    RESULTS_DIR.mkdir(exist_ok=True)
    digest = hashlib.sha1(text.encode()).hexdigest()[:8]
    path = RESULTS_DIR / f"probe-speech-{name}-{digest}.pcm"
    if not path.exists():
        resp = get_client().audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="onyx",
            input=text,
            instructions="Read calmly like an imam giving a Friday sermon, in Modern Standard Arabic.",
            response_format="pcm",
        )
        path.write_bytes(resp.read())
    return path.read_bytes()


def _resample(pcm: bytes, rate: int) -> bytes:
    import numpy as np
    from scipy.signal import resample_poly

    if rate == TTS_RATE:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    y = resample_poly(x, rate, TTS_RATE)
    return np.clip(y, -32768, 32767).astype(np.int16).tobytes()


def _wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


_DIACRITICS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")


def _normalise(text: str) -> str:
    text = _DIACRITICS.sub("", text)
    text = re.sub("[أإآٱ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


def cer(reference: str, hypothesis: str) -> float:
    """Character error rate after normalising Arabic spelling variants."""
    a, b = _normalise(reference), _normalise(hypothesis)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1] / max(1, len(a))


def probe_segmented(engine: str, model: str, rounds: int) -> None:
    from config import FS
    from providers import get_transcription_provider_for

    audio = _wav(_resample(_speech("short", SPEECH_SHORT), FS), FS)
    provider = get_transcription_provider_for(engine)
    times, text = [], ""
    for _ in range(rounds):
        started = time.perf_counter()
        try:
            text = provider.transcribe(audio, model=model, language="ar")
        except Exception as e:
            print(f"{engine}:{model}  FAILED  {type(e).__name__}: {e}")
            return
        times.append(time.perf_counter() - started)
    print(
        f"{engine}:{model}  median {statistics.median(times):.2f}s  "
        f"CER {cer(SPEECH_SHORT, text):.3f}"
        + ("  EMPTY TRANSCRIPT" if not text.strip() else "")
    )


def probe_streaming(engine: str, model: str) -> None:
    import providers

    rate = providers.get_streaming_capture_sample_rate(engine)
    speech = _resample(_speech("long", SPEECH_LONG), rate)
    speech_seconds = len(speech) / 2 / rate
    events: list[tuple[float, str]] = []
    finals: list[str] = []
    errors: list[str] = []

    def on_transcript(text, is_final, **_):
        events.append((time.monotonic(), "final" if is_final else "interim"))
        if is_final:
            finals.append(text)

    try:
        handle = providers._STREAMING_TRANSCRIPTION_PROVIDERS[engine].open_stream(
            model=model,
            language="ar",
            on_transcript=on_transcript,
            on_utterance_end=lambda **_: events.append((time.monotonic(), "end")),
            on_error=lambda e: errors.append(f"{type(e).__name__}: {e}"),
        )
    except Exception as e:
        print(f"{engine}:{model}  FAILED to open  {type(e).__name__}: {e}")
        return

    # 100 ms chunks on a fixed schedule, then 4 s of silence so server-side
    # endpointing can close the last turn.
    data = speech + b"\x00" * (rate * 2 * 4)
    chunk = int(rate * 0.1) * 2
    start = time.monotonic()
    for n, offset in enumerate(range(0, len(data), chunk)):
        delay = start + n * 0.1 - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        handle.feed(data[offset : offset + chunk])
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        last = max((t for t, kind in events if kind == "final"), default=0)
        if last and time.monotonic() - last > 6:
            break
        time.sleep(0.2)
    handle.close()

    final_times = [t for t, kind in events if kind == "final"]
    lag = (
        f"{max(final_times) - (start + speech_seconds):.2f}s" if final_times else "n/a"
    )
    print(
        f"{engine}:{model}  {speech_seconds:.0f}s streamed  "
        f"last final {lag} after the speech ended  "
        f"CER {cer(SPEECH_LONG, ' '.join(finals)) if finals else float('nan'):.3f}  "
        f"interims {sum(kind == 'interim' for _, kind in events)}  "
        f"finals {len(final_times)}  turn ends {sum(kind == 'end' for _, kind in events)}"
        + (f"  ERRORS {errors[:2]}" if errors else "")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--translation", nargs="+", default=[], metavar="PROVIDER:MODEL")
    ap.add_argument("--stt", nargs="+", default=[], metavar="ENGINE:MODEL")
    ap.add_argument(
        "--rounds", type=int, default=2, help="repeats per model (default 2)"
    )
    args = ap.parse_args()
    if not args.translation and not args.stt:
        ap.error("nothing to probe: pass --translation and/or --stt")

    from dotenv import load_dotenv

    load_dotenv()
    translation, stt = _parse(args.translation), _parse(args.stt)
    for provider_id in {p for p, _ in translation + stt} | (
        {"openai"} if stt else set()
    ):
        _activate_key(provider_id)

    for provider_id, model in translation:
        probe_translation(provider_id, model, args.rounds)
    for engine, model in stt:
        if engine not in STREAMING_ENGINES:
            probe_segmented(engine, model, args.rounds)
    # Streaming probes run side by side: each takes a minute of wall clock.
    threads = [
        threading.Thread(target=probe_streaming, args=(engine, model))
        for engine, model in stt
        if engine in STREAMING_ENGINES
    ]
    if threads:
        # Once, up front: threads racing to fill the cache each got their own
        # TTS rendition, of different lengths, and the results stopped being
        # comparable.
        _speech("long", SPEECH_LONG)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
