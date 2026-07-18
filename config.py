"""Static configuration constants and paths.

This module contains IMMUTABLE technical constants (audio params, model names,
file paths, thresholds). These are not user-configurable.

For user-configurable runtime settings (API key, language preferences, etc.),
see utils/settings.py instead.
"""

from __future__ import annotations

import os
import sys

from utils.app_paths import get_app_data_dir

# -------------------------
# AUDIO PARAMETERS
# -------------------------
# Translation mode (different languages)
DURATION = 12  # Length (in seconds) of each saved segment
OVERLAP = 3  # Overlap (in seconds) between segments
STEP = DURATION - OVERLAP  # Interval at which a new segment is captured

# Same-language mode (faster feedback, small overlap to prevent gaps)
SAME_LANG_DURATION = 5  # Shorter segments for faster feedback
SAME_LANG_OVERLAP = 1  # Small overlap to prevent missed speech
SAME_LANG_STEP = SAME_LANG_DURATION - SAME_LANG_OVERLAP  # 4 second intervals

FS = 16000  # Sample rate

# -------------------------
# MODEL CONFIGURATION
# -------------------------
# Embedding models must match the pre-built verse matrices in
# data/embeddings/ — query and verse embeddings live in the same vector
# space per provider (see providers.get_embedding_space()).
EMBEDDING_MODEL = "text-embedding-3-large"  # OpenAI space (default)
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"  # Gemini space (optional)

# -------------------------
# PATHS
# -------------------------
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _get_resource_dir() -> str:
    """Directory where bundled read-only resources (like data/) live."""
    if IS_FROZEN and hasattr(sys, "_MEIPASS"):
        return str(sys._MEIPASS)  # type: ignore[attr-defined]
    return os.path.dirname(os.path.abspath(__file__))


RESOURCE_DIR = _get_resource_dir()

# Bundled/static resources
DATA_DIR = os.path.join(RESOURCE_DIR, "data")
ICON_PATH = os.path.join(RESOURCE_DIR, "public", "MinbarLive.ico")
ICON_PATH_PNG = os.path.join(RESOURCE_DIR, "public", "MinbarLive1.png")

# Writable runtime data (works for EXEs, avoids Program Files permissions)
APP_DATA_DIR = str(get_app_data_dir())
AUDIO_DIR = os.path.join(APP_DATA_DIR, "recordings")
HISTORY_DIR = os.path.join(APP_DATA_DIR, "history")
LOGS_DIR = os.path.join(APP_DATA_DIR, "logs")
BATCH_DIR = os.path.join(APP_DATA_DIR, "batch")  # per-run batch transcripts

# Translation data directories (new structure)
TRANSLATIONS_DIR = os.path.join(DATA_DIR, "translations")
QURAN_TRANSLATIONS_DIR = os.path.join(TRANSLATIONS_DIR, "quran")
ATHAN_TRANSLATIONS_DIR = os.path.join(TRANSLATIONS_DIR, "athan")
FOOTER_TRANSLATIONS_PATH = os.path.join(TRANSLATIONS_DIR, "footer_translations.json")
STATUS_MESSAGES_PATH = os.path.join(TRANSLATIONS_DIR, "status_messages.json")
GUI_TRANSLATIONS_DIR = os.path.join(TRANSLATIONS_DIR, "gui")
EMBEDDINGS_DIR = os.path.join(DATA_DIR, "embeddings")

# Embeddings paths (language-agnostic, based on Arabic text).
# The .npz is what the app loads (compact, fast); the .json is the raw notebook
# output kept as source of truth. Rebuild the .npz via notebooks/build_embeddings_npz.py.
QURAN_EMBEDDINGS_PATH = os.path.join(EMBEDDINGS_DIR, "quran_embeddings.json")
QURAN_EMBEDDINGS_OPENAI_NPZ_PATH = os.path.join(
    EMBEDDINGS_DIR, "quran_embeddings_openai.npz"
)
# Optional Gemini-space verse matrix (built by build_embeddings_npz.py with
# PROVIDER="gemini"); when present AND ai_provider is gemini, RAG runs fully
# on Gemini (no OpenAI key needed for verse matching).
QURAN_EMBEDDINGS_GEMINI_NPZ_PATH = os.path.join(
    EMBEDDINGS_DIR, "quran_embeddings_gemini.npz"
)


def ensure_directories() -> None:
    """Create necessary writable directories. Call this at app startup."""
    os.makedirs(AUDIO_DIR, exist_ok=True)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(BATCH_DIR, exist_ok=True)
    # In source runs, ensure data/ exists too.
    if not IS_FROZEN:
        os.makedirs(DATA_DIR, exist_ok=True)


# -------------------------
# AUDIO BUFFER SETTINGS
# -------------------------
RING_CAPACITY = int(2 * DURATION * FS)  # Keep 2x duration in ring buffer

# -------------------------
# SEGMENT OVERLAP DEDUP (live segmented mode)
# -------------------------
# Consecutive live segments overlap by OVERLAP seconds, so each one is
# transcribed with the tail of the previous one repeated at its head — a
# visible duplicate on every boundary. After transcription the repeated
# prefix is stripped (see translation.stt.strip_overlap_prefix). The repeated
# span is ~OVERLAP/DURATION of the segment; this caps how many words may be
# stripped so a genuine repetition can never be mistaken for whole-segment
# overlap. Batch mode is unaffected (it uses non-overlapping segments).
SEGMENT_OVERLAP_MAX_WORDS = 10

# -------------------------
# FRAGMENT GATE (shared: live segmented + streaming)
# -------------------------
# A transcription with fewer alphabetic characters than this is a fragment
# (e.g. a bare "م" that renders as "h", or a near-silent "Um"): never worth a
# GPT call. In streaming it is first folded into the coalescing buffer; only a
# still-tiny residual is dropped. In segmented mode the segment is skipped.
MIN_TRANSLATABLE_LETTERS = 3

# -------------------------
# SEMANTIC BUFFER SETTINGS
# -------------------------
SEMANTIC_MAX_CHUNKS = 3
SEMANTIC_MAX_SECONDS = 10
# A flushed buffer longer than this is split into multiple subtitles at
# sentence ends (never mid-sentence) so a dense multi-sentence flush doesn't
# scroll away before the viewer can read it.
SEMANTIC_MAX_WORDS = 28

# -------------------------
# SILENCE DETECTION
# -------------------------
SILENCE_THRESHOLD = 0.001
SILENCE_RATIO = 0.8

# -------------------------
# NOISE FILTER (webrtcvad voice-activity gate, settings.noise_filter)
# -------------------------
# The RMS silence gate above is loudness-only: static hiss or hum from a muted
# mixer channel sits above SILENCE_THRESHOLD, reaches the STT models and comes
# back as hallucinated sentences. webrtcvad classifies 30 ms frames by
# spectral shape instead, so stationary noise is rejected even when loud.
VAD_AGGRESSIVENESS = 2  # 0-3; higher = stricter about calling a frame speech
# Segmented/batch: a non-silent segment is skipped when fewer than this
# fraction of its frames are speech. Measured: real speech ≈ 0.8+, static and
# hum ≈ 0.01 — and the RMS gate already guarantees ≥ (1-SILENCE_RATIO) loud
# frames, so genuine speech cannot land under this threshold.
VAD_MIN_SPEECH_RATIO = 0.05
# Streaming: sustained non-speech longer than this is replaced with digital
# silence — the connection stays alive (and billed) but the engine's server
# VAD hears true silence instead of static. Short speech pauses never gate.
VAD_STREAM_HANGOVER_SECONDS = 2.0
# Streaming open/close decision: fraction of speech frames over a rolling
# window, so a single false-positive frame on hiss cannot reopen the gate.
VAD_STREAM_WINDOW_SECONDS = 1
VAD_STREAM_OPEN_RATIO = 0.1
# webrtcvad's classifier is energy-sensitive: real speech quieter than about
# -46 dBFS peak (e.g. a low mic-gain audio interface) stops being detected,
# so the gate starved a quiet mic (user-confirmed 2026-07-15: filter off let
# sentences through). The copy used for the DECISION is boosted toward this
# peak — the audio actually fed onward is never touched. Measured (SAPI
# speech + synthetic noise, aggressiveness 2): speech ratio ~0.73 at -30
# dBFS peak; hiss reads as fake speech only from -14 dBFS, 50 Hz hum+hiss
# from -20, 60 Hz hum+hiss from ~-25 → the -30.5 dBFS target keeps ≥4 dB
# margin below every flip, so no noise floor can be boosted into a false
# open. The cap bounds how deep a floor is raised (x16 = +24 dB rescues
# speech down to ~-60 dBFS peak). 100 Hz hum reads as speech at EVERY level
# — a pre-existing webrtcvad limit the boost cannot worsen.
VAD_DECISION_TARGET_PEAK = 0.03  # ≈ -30.5 dBFS
VAD_DECISION_MAX_BOOST = 16.0  # +24 dB

# -------------------------
# BATCH (file → SRT) SEGMENTATION
# -------------------------
# Batch mode splits a file on its own pauses (VAD-style) instead of on a blind
# clock, so segment boundaries fall in silence — no words cut mid-boundary and
# no short utterance lost inside a mostly-silent block.
BATCH_MAX_SEGMENT_SECONDS = 15.0  # cap for one transcription chunk (unbroken speech)
BATCH_MIN_SILENCE_GAP_SECONDS = 0.4  # micro-pauses shorter than this stay inside a block
BATCH_MIN_SEGMENT_SECONDS = 0.3  # drop speech runs shorter than this (transients)
# Segments are contiguous: a pause up to this length is absorbed into the
# surrounding segments (split at its centre) rather than dropped, so quiet
# speech trailing/leading a phrase is never lost. Longer pure-silence gaps are
# trimmed to half this on each side (cost).
BATCH_MAX_SILENCE_KEEP_SECONDS = 2.0
# A speech block shorter than this is not transcribed standalone (too little
# acoustic context — hallucination-prone, e.g. "giggle" from a 1.4s snippet);
# it is absorbed into a neighboring block when the pause between them is at
# most BATCH_MAX_SILENCE_KEEP_SECONDS. Genuinely isolated short utterances
# still get their own segment.
BATCH_MIN_STANDALONE_SECONDS = 2.0

# -------------------------
# RAG SETTINGS
# -------------------------
RAG_MIN_SIMILARITY = 0.60
RAG_TOP_K = 5

# Hard-verified verse bypass: when the top RAG match is at least this
# confident, skip GPT entirely and output the exact dictionary translation
# (no paraphrasing of Quran text, one API call saved).
RAG_HARD_MATCH_THRESHOLD = 0.85
# The bypass replaces the WHOLE segment output, so it must only fire when the
# segment is essentially the verse alone. Two guards, both must pass:
# - word-count ratio (segment/verse) inside the band — binding for short verses
# - absolute word-count difference at most MAX_WORD_DIFF — binding for long
#   verses, where the ratio alone would still admit whole dropped sentences
# Outside either → fall back to the normal GPT-with-hint path, otherwise
# surrounding sermon speech would be silently dropped (segment too long) or a
# partially recited verse would be over-completed (segment too short).
RAG_HARD_MATCH_MIN_LENGTH_RATIO = 0.75
RAG_HARD_MATCH_MAX_LENGTH_RATIO = 1.25
RAG_HARD_MATCH_MAX_WORD_DIFF = 6
# Multi-verse verified bypass: when one segment contains several complete
# ayat recited back-to-back, no single verse can pass the guards above (the
# blended embedding lowers every verse's score and the length guard rejects
# any one verse). Candidates that are consecutive ayat of the same surah are
# instead verified as a run by exact text comparison: the concatenated
# dictionary verses must fuzzy-match the whole normalized segment at least
# this well. Embeddings only nominate candidates — the text match certifies.
RAG_MULTI_VERSE_TEXT_SIMILARITY = 0.80
# Prefix shown on subtitles for verified verses (kept as one constant so the
# GUI indicator can be restyled in one place)
QURAN_VERIFIED_MARKER = "📖"

# -------------------------
# DICTIONARY MATCHING
# -------------------------
ATHAN_MATCH_THRESHOLD = 0.75  # Minimum fuzzy match score for Athan detection

# -------------------------
# FILE RETENTION
# -------------------------
LOGS_RETENTION_DAYS = 30
HISTORY_RETENTION_DAYS = 90
BATCH_RETENTION_DAYS = 90

# -------------------------
# STREAMING (real-time transcription engines, P7 pipeline_mode="streaming")
# -------------------------
STREAMING_CHUNK_MS = 50  # PCM chunk size fed to the streaming connection
# WASAPI capture buffer for loopback recording, as a duration — deliberately
# NOT derived from STREAMING_CHUNK_MS. soundcard turns the recorder blocksize
# straight into the WASAPI buffer duration, so the old `chunk_frames * 4`
# shrank the buffer from 800 ms to 200 ms when STREAMING_CHUNK_MS went 200 ->
# 50, and any stall past that overran it (WASAPI raised DATA_DISCONTINUITY and
# dropped samples — audible as clipped speech). Costs no latency: the loop
# still reads STREAMING_CHUNK_MS at a time, this is only headroom.
LOOPBACK_CAPTURE_BUFFER_SECONDS = 0.5
# Reconnect-with-backoff when the streaming connection dies mid-session
# (network blip, server-side session end). Retries continue until Stop with
# exponential backoff between attempts; the first transcript after a
# successful reconnect resets the backoff.
STREAMING_RECONNECT_BASE_SECONDS = 1.0
STREAMING_RECONNECT_MAX_SECONDS = 30.0
# Watchdog for a connection that stays open with no error but quietly stops
# producing transcripts (observed live: 15-26s of total silence — no interim,
# no final, no error callback — during continuous speech the reconnect logic
# above never sees). Unlike that reconnect-with-backoff, a stall reconnect
# never shows an audience-facing error message (a genuine long pause in
# speech looks identical from here, and reconnecting during real silence has
# no visible cost) and never backs off — each check is independent.
STREAMING_STALL_TIMEOUT_SECONDS = 15.0
STREAMING_ENDPOINTING_MS = 300  # Deepgram endpointing sensitivity (silence -> final)
STREAMING_UTTERANCE_END_MS = 1000  # Deepgram UtteranceEnd silence threshold (ms)
# Gemini Live VAD: silence before the turn (utterance) is considered ended.
STREAMING_GEMINI_SILENCE_MS = 800
# Max age of accumulated text before a forced flush. Continuous speech resets
# neither speech_final nor UtteranceEnd, so without this cap an unbroken
# recitation would accumulate unbounded; DURATION keeps worst-case latency no
# worse than segmented mode's.
STREAMING_MAX_UTTERANCE_SECONDS = DURATION
STREAMING_MODEL = "nova-3"

# Auto-stop: end a running session after this long without any transcription
# arriving (mic muted, khutbah over, forgotten session) — saves API cost,
# especially streaming's per-audio-minute billing which includes silence.
# Toggled by the "auto_stop_inactivity" checkbox (Advanced, default on).
AUTO_STOP_INACTIVITY_SECONDS = 600

# Realtime feed: continuous speech flushes up to STREAMING_MAX_UTTERANCE_
# SECONDS of speech as ONE utterance, whose translation renders as a wall of
# text. Settled translations longer than this are split at sentence
# boundaries into separate feed blocks (display-only — no extra API calls).
REALTIME_MAX_BLOCK_CHARS = 220
# The live (in-progress) transcript line renders only its last N wrapped
# rows — a long interim otherwise wraps to several rows and shoves the
# settled history up by that much at once (and the feed never scrolls back
# down). The full text still arrives as the settled block.
REALTIME_LIVE_MAX_ROWS = 1
# Feed spacing between blocks. Wider than the intra-pair gap so a bilingual
# (source above translation) pair reads as one visual group.
REALTIME_BLOCK_SPACING = 34

# Micro-utterance coalescing: the streaming engines endpoint on natural
# pauses, so a rhetorical pause yields a 1-3 word "utterance" that GPT would
# translate in isolation (the "Sack."/"Das Licht." class, and context-starved
# inversions). Short utterances are held and merged with the next one so GPT
# sees a whole clause. The merged text flushes when it reaches
# COALESCE_MIN_WORDS, after COALESCE_HOLD_SECONDS with no follow-up (a
# trailing clause), on the max-utterance cap, or on stop. Also cuts one
# full-prompt translation call per fragment.
STREAMING_COALESCE_MIN_WORDS = 6
# Kept short so a trailing clause reaches translation quickly — the shorter the
# hold, the sooner the translation lands under the transcription in Realtime
# mode (at the cost of GPT occasionally seeing a slightly shorter clause).
STREAMING_COALESCE_HOLD_SECONDS = 1

# -------------------------
# CONTEXT MANAGEMENT
# -------------------------
CONTEXT_RECENT_RAW_COUNT = 3  # Number of recent transcriptions to keep raw
CONTEXT_SUMMARIZE_EVERY_N = 10  # Summarize after N transcriptions
# Time floor for the rolling summary: in streaming mode utterances flush
# every ~3-8s, so "every 10 transcriptions" alone fired a summary LLM call
# as often as every ~45s (log-measured 2026-07-11) with near-identical
# output each time. Pending texts keep accumulating until BOTH conditions
# hold, so nothing is lost — the summary just covers more at once.
CONTEXT_SUMMARIZE_MIN_SECONDS = 180
CONTEXT_HOURLY_INTERVAL = 3600  # Seconds between hourly summaries

# -------------------------
# GUI SETTINGS
# -------------------------
LINE_SPACING = 18
MARGIN_BOTTOM = 45

# Announcement overlay (megaphone): a custom operator message shown big and
# centred above the subtitles. Preset display durations in seconds; 0 means
# "show until the operator stops it" (survives even a translation stop).
ANNOUNCEMENT_DURATIONS_SECONDS = [10, 30, 60, 300, 0]
# How many recent announcement texts to remember for quick re-use.
ANNOUNCEMENT_HISTORY_MAX = 3
# How many favorited (starred) announcements can be pinned at once. Favorites
# are excluded from the auto-rotating history above, so they never get
# evicted by newer sends. Bounded so the window can't grow unbounded from
# over-favoriting.
ANNOUNCEMENT_FAVORITES_MAX = 5
