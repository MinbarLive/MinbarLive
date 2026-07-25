"""Translation-latency benchmark harness (REAL API calls — costs money).

Runs the true end-to-end translation path the app uses
(``translation.translator.translate_text``: RAG embedding + LLM completion) over
a fixed Arabic corpus, N times per utterance, and reports median/p90/min/max of
wall-clock latency per category and globally.

Not a CI unit test — it hits the configured provider's live API. Keep the corpus
small; reproducibility comes from N repeats + median/p90, and (where the provider
tolerates it) temperature=0.

Usage:
    python bench/run_bench.py --out baseline.json           # run + save
    python bench/run_bench.py --out streaming.json -n 5
    python bench/run_bench.py --compare baseline.json streaming.json   # diff two runs

Metrics per call:
    t_total = wall-clock of the whole translate_text() call (RAG + LLM)
    t_first = time-to-first-token. Baseline (blocking) == t_total by definition;
              the streaming arm (Hebel 1) overrides this with the real TTFT.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow ``python bench/run_bench.py`` from the repo root: a directly-run script
# puts its own dir (bench/) on sys.path, not the repo root, so ``import bench``
# / ``import providers`` would fail without this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS_DIR = Path(__file__).parent / "results"

# Fixed source/target for a representative Arabic -> German run, independent of
# whatever the GUI happens to have stored, so categories stay meaningful.
SOURCE_LANGUAGE = "Arabic"
TARGET_LANGUAGE = "German"

# Free-tier quotas throttle bursts (measured 2026-07-25: Gemini free tier caps
# gemini-3.1-flash-lite at 15 requests/min; over that the SDK silently waits
# ~12 s, which contaminates latency numbers with throttle time). Pace calls so
# we stay under the cap and measure pipeline latency, not quota waits. 4.5 s ->
# ~13/min, safely under 15. Set to 0 on a paid tier to run at full speed.
DEFAULT_MIN_INTERVAL_S = 4.5


# --------------------------------------------------------------------------
# Provider / key setup
# --------------------------------------------------------------------------
def _activate_stored_key(provider_id: str) -> bool:
    """Load the provider's stored key (keychain or .env) into its client."""
    import importlib

    import providers

    key = providers.get_stored_api_key(provider_id)
    if not key:
        return False
    client_mod = importlib.import_module(f"providers.{provider_id}.client")
    client_mod.set_api_key(key)
    return True


def _force_temperature_zero(provider_id: str) -> bool:
    """Pin temperature=0 on the active provider for reproducible output.

    Gemini and OpenAI are patched: both accept temperature=0 for their default
    translation models (OpenAI gpt-5.2 verified 2026-07-25). Anthropic Sonnet-5
    rejects non-default sampling (see the LATENZ review, point 1), so it is left
    unpinned. Returns True if pinned.
    """
    if provider_id == "gemini":
        from providers.gemini.translation import GeminiTranslationProvider as _P
    elif provider_id == "openai":
        from providers.openai.translation import OpenAITranslationProvider as _P
    else:
        return False

    _orig = _P.complete

    def _complete_temp0(self, **kwargs):
        kwargs.setdefault("temperature", 0.0)
        return _orig(self, **kwargs)

    _P.complete = _complete_temp0  # type: ignore[method-assign]
    return True


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------
def _translate_once(text: str, stream: bool) -> tuple[float, float, str]:
    """One real translate_text call. Returns (t_total, t_first, output).

    Blocking arm: t_first == t_total (the whole answer arrives at once).
    Streaming arm: on_delta records the wall-clock of the first text fragment,
    the real time-to-first-subtitle-word. Bypass paths (verified verse, Athan,
    same-language) emit no deltas, so t_first stays == t_total there — correct,
    since those never call the LLM and are already instant.
    """
    from translation.translator import translate_text

    first_at: list[float] = []
    start = time.perf_counter()

    on_delta = None
    if stream:
        def on_delta(_frag: str) -> None:
            if not first_at:
                first_at.append(time.perf_counter() - start)

    output = translate_text(
        text,
        source_language=SOURCE_LANGUAGE,
        target_language=TARGET_LANGUAGE,
        on_delta=on_delta,
    )
    t_total = time.perf_counter() - start
    t_first = first_at[0] if first_at else t_total
    return t_total, t_first, output


def _stats(samples: list[float]) -> dict[str, float]:
    if not samples:
        return {"median": 0.0, "p90": 0.0, "min": 0.0, "max": 0.0, "n": 0}
    s = sorted(samples)
    # Nearest-rank p90.
    rank = max(0, min(len(s) - 1, round(0.9 * (len(s) - 1))))
    return {
        "median": round(statistics.median(s), 4),
        "p90": round(s[rank], 4),
        "min": round(s[0], 4),
        "max": round(s[-1], 4),
        "n": len(s),
    }


def _paced(min_interval_s: float):
    """Yield after ensuring at least ``min_interval_s`` since the last yield."""
    last = [0.0]

    def wait() -> None:
        gap = time.monotonic() - last[0]
        if gap < min_interval_s:
            time.sleep(min_interval_s - gap)
        last[0] = time.monotonic()

    return wait


def run(n_repeats: int, out_name: str, min_interval_s: float, stream: bool) -> None:
    from bench.latency_corpus import CORPUS
    from providers import get_translation_model_chain
    from utils.settings import load_settings

    settings = load_settings()
    provider_id = settings.ai_provider
    if not _activate_stored_key(provider_id):
        raise SystemExit(
            f"No usable API key for provider '{provider_id}'.\n"
            f"Configure it in the app, or create a .env with the matching key "
            f"(e.g. GEMINI_API_KEY=... for gemini)."
        )
    temp_pinned = _force_temperature_zero(provider_id)
    model = get_translation_model_chain()[0]

    print(f"Provider={provider_id}  Model={model}  "
          f"{SOURCE_LANGUAGE}->{TARGET_LANGUAGE}  "
          f"N={n_repeats}  temperature={'0' if temp_pinned else 'default'}  "
          f"mode={'stream' if stream else 'blocking'}")
    print(f"Corpus: {len(CORPUS)} utterances -> "
          f"{len(CORPUS) * n_repeats} real API calls  "
          f"(pacing {min_interval_s:g}s/call)\n")

    pace = _paced(min_interval_s)
    per_entry = []
    cat_samples_total: dict[str, list[float]] = {}
    cat_samples_first: dict[str, list[float]] = {}
    all_total: list[float] = []
    all_first: list[float] = []

    for entry in CORPUS:
        t_totals, t_firsts = [], []
        output = ""
        bypassed = False
        for _ in range(n_repeats):
            pace()
            t_total, t_first, output = _translate_once(entry["arabisch"], stream)
            t_totals.append(t_total)
            t_firsts.append(t_first)
        # The verified-verse / same-language paths skip the LLM entirely; such
        # an entry does not exercise the translation stage (see corpus notes).
        if output.startswith("📖"):
            bypassed = True

        cat = entry["kategorie"]
        cat_samples_total.setdefault(cat, []).extend(t_totals)
        cat_samples_first.setdefault(cat, []).extend(t_firsts)
        all_total.extend(t_totals)
        all_first.extend(t_firsts)

        med = statistics.median(t_totals)
        flag = "  ⚠ LLM-BYPASS (swap this entry)" if bypassed else ""
        print(f"  {entry['id']:22s} {cat:16s} median {med:6.3f}s{flag}")
        per_entry.append({
            "id": entry["id"],
            "kategorie": cat,
            "t_total": [round(x, 4) for x in t_totals],
            "t_first": [round(x, 4) for x in t_firsts],
            "output": output,
            "bypassed": bypassed,
        })

    per_category = {
        cat: {
            "t_total": _stats(cat_samples_total[cat]),
            "t_first": _stats(cat_samples_first[cat]),
        }
        for cat in sorted(cat_samples_total)
    }
    result = {
        "meta": {
            "provider": provider_id,
            "model": model,
            "source_language": SOURCE_LANGUAGE,
            "target_language": TARGET_LANGUAGE,
            "temperature": "0" if temp_pinned else "default",
            "mode": "stream" if stream else "blocking",
            "n_repeats": n_repeats,
            "date": datetime.now(timezone.utc).isoformat(),
            "git_commit": _git_commit(),
        },
        "per_entry": per_entry,
        "per_category": per_category,
        "global": {"t_total": _stats(all_total), "t_first": _stats(all_first)},
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / out_name
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    _print_table(result)
    print(f"\nSaved -> {out_path}")


def _print_table(result: dict) -> None:
    print(f"\n{'Kategorie':18s} {'median t_total':>14s} {'p90':>8s} "
          f"{'median t_first':>15s} {'p90':>8s}")
    print("-" * 66)
    for cat, s in result["per_category"].items():
        tt, tf = s["t_total"], s["t_first"]
        print(f"{cat:18s} {tt['median']:>13.3f}s {tt['p90']:>7.3f}s "
              f"{tf['median']:>14.3f}s {tf['p90']:>7.3f}s")
    g = result["global"]
    print("-" * 66)
    print(f"{'GLOBAL':18s} {g['t_total']['median']:>13.3f}s "
          f"{g['t_total']['p90']:>7.3f}s {g['t_first']['median']:>14.3f}s "
          f"{g['t_first']['p90']:>7.3f}s")


# --------------------------------------------------------------------------
# Compare mode
# --------------------------------------------------------------------------
def _load(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        p = RESULTS_DIR / path
    return json.loads(p.read_text())


def _delta(old: float, new: float) -> str:
    if old == 0:
        return f"{new:+.3f}s"
    pct = (new - old) / old * 100
    return f"{new - old:+.3f}s ({pct:+.1f}%)"


def compare(baseline_name: str, new_name: str) -> None:
    base, new = _load(baseline_name), _load(new_name)
    bm, nm = base["meta"], new["meta"]
    print(f"BASELINE  {bm['provider']}/{bm['model']}  {bm['date']}")
    print(f"NEW       {nm['provider']}/{nm['model']}  {nm['date']}")
    if (bm["provider"], bm["model"]) != (nm["provider"], nm["model"]):
        print("  ⚠ DIFFERENT provider/model — latency comparison is INVALID.")

    print(f"\n{'Kategorie':18s} {'t_total median':>26s} {'t_first median':>26s}")
    print("-" * 74)
    for cat in base["per_category"]:
        if cat not in new["per_category"]:
            continue
        bt = base["per_category"][cat]
        nt = new["per_category"][cat]
        print(f"{cat:18s} "
              f"{_delta(bt['t_total']['median'], nt['t_total']['median']):>26s} "
              f"{_delta(bt['t_first']['median'], nt['t_first']['median']):>26s}")
    bg, ng = base["global"], new["global"]
    print("-" * 74)
    print(f"{'GLOBAL':18s} "
          f"{_delta(bg['t_total']['median'], ng['t_total']['median']):>26s} "
          f"{_delta(bg['t_first']['median'], ng['t_first']['median']):>26s}")

    # Quality: put the two output texts side by side per utterance.
    print("\n=== OUTPUT DIFF (quality check) ===")
    new_by_id = {e["id"]: e for e in new["per_entry"]}
    for be in base["per_entry"]:
        ne = new_by_id.get(be["id"])
        if not ne:
            continue
        same = "==" if be["output"] == ne["output"] else "!="
        print(f"\n[{be['id']}] {same}")
        print(f"  base: {be['output']}")
        print(f"  new : {ne['output']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="baseline.json",
                    help="result filename under bench/results/")
    ap.add_argument("-n", "--repeats", type=int, default=5)
    ap.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_S,
                    help="seconds between calls to stay under free-tier RPM "
                         "(0 = full speed, use on a paid tier)")
    ap.add_argument("--stream", action="store_true",
                    help="stream the translation (measures real t_first)")
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "NEW"),
                    help="diff two result files instead of running")
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    if args.compare:
        compare(*args.compare)
    else:
        run(args.repeats, args.out, args.min_interval, args.stream)


if __name__ == "__main__":
    main()
