"""Shared thinking setting for the Gemini generate_content providers.

Gemini 3.x models think by default, which multiplies latency — live subtitles
cannot afford it, so translation and transcription both pin the lowest level.

One constant, two call sites: translation.py and transcription.py must stay in
step, and the field they pass is version-sensitive (see below).
"""

from __future__ import annotations

# "minimal", not thinking_budget=0. Live-probed 2026-07-22 against the real
# API: gemini-3.6-flash and gemini-3.5-flash-lite reject thinking_budget with
# 400 INVALID_ARGUMENT (the newer models replaced the budget field with
# levels), while thinking_level="minimal" is accepted by every model in our
# dropdowns — including the older gemini-3.1-flash-lite — and is faster than
# omitting the config entirely (3.17s → 1.23s on gemini-3.6-flash).
THINKING_LEVEL = "minimal"

__all__ = ["THINKING_LEVEL"]
