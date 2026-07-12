"""Tests for batch processing (SRT generation from files)."""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import scipy.io.wavfile as wavfile

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import translation.stt
from batch import processor
from batch.srt_writer import SrtEntry, build_srt, format_timestamp, write_srt
from config import DURATION, FS


class TestFormatTimestamp:
    def test_zero(self):
        assert format_timestamp(0) == "00:00:00,000"

    def test_full_fields(self):
        assert format_timestamp(3661.5) == "01:01:01,500"

    def test_millisecond_rounding(self):
        assert format_timestamp(1.0009) == "00:00:01,001"

    def test_negative_clamped(self):
        assert format_timestamp(-3.2) == "00:00:00,000"

    def test_hours_beyond_24(self):
        assert format_timestamp(90000) == "25:00:00,000"


class TestBuildSrt:
    def test_block_structure(self):
        srt = build_srt([SrtEntry(0, 12, "Hallo"), SrtEntry(12, 24, "Welt")])
        assert srt == (
            "1\n00:00:00,000 --> 00:00:12,000\nHallo\n\n"
            "2\n00:00:12,000 --> 00:00:24,000\nWelt\n"
        )

    def test_empty_text_skipped_and_renumbered(self):
        srt = build_srt(
            [SrtEntry(0, 1, "a"), SrtEntry(1, 2, "   "), SrtEntry(2, 3, "b")]
        )
        assert "1\n" in srt and "2\n00:00:02,000" in srt
        assert srt.count("-->") == 2

    def test_no_entries(self):
        assert build_srt([]) == ""

    def test_bilingual_block_source_above_translation(self):
        # Any language pair: the original transcription sits above the
        # translation as its own line within the block.
        srt = build_srt([SrtEntry(0, 12, "Hallo", source="مرحبا")])
        assert srt == "1\n00:00:00,000 --> 00:00:12,000\nمرحبا\nHallo\n"

    def test_bilingual_identical_source_collapses(self):
        # Same-language runs / code-switching pass-through: identical source
        # and text must not print the same line twice.
        srt = build_srt([SrtEntry(0, 1, "نص عربي", source="نص عربي")])
        assert srt.count("نص عربي") == 1

    def test_bilingual_empty_text_still_dropped(self):
        # A block whose translation is empty is dropped even with a source.
        assert build_srt([SrtEntry(0, 1, "  ", source="original")]) == ""

    def test_write_srt_utf8_sig(self, tmp_path):
        path = tmp_path / "out.srt"
        write_srt([SrtEntry(0, 1, "الحمد لله — Übersetzung")], str(path))
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")  # BOM for legacy Windows players
        assert "الحمد لله — Übersetzung" in raw.decode("utf-8-sig")


class TestOutputPath:
    def test_target_language_code_in_name(self):
        out = processor.output_path_for(r"C:\rec\khutbah.mp4", "German")
        assert out == r"C:\rec\khutbah.de.srt"

    def test_unknown_language_plain_srt(self):
        out = processor.output_path_for("talk.wav", "Klingon")
        assert out == "talk.srt"


class TestToFloat32:
    def test_int16_normalized(self):
        data = np.array([0, 32767, -32767], dtype=np.int16)
        out = processor._to_float32(data)
        assert out.dtype == np.float32
        assert out[1] == pytest.approx(1.0, abs=1e-4)

    def test_stereo_downmixed(self):
        data = np.array([[1000, 3000], [2000, 4000]], dtype=np.int16)
        out = processor._to_float32(data)
        assert out.shape == (2,)


def _tone(seconds, amp=0.3):
    t = np.arange(int(seconds * FS)) / FS
    return (amp * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def _sil(seconds):
    return np.zeros(int(seconds * FS), dtype=np.float32)


class TestSegmentSpeech:
    """Voice-activity segmentation is the fix for the old blind-chunk skipping."""

    def test_all_silence_no_segments(self):
        assert processor._segment_speech(_sil(30)) == []

    def test_short_utterance_in_silence_kept(self):
        # The old fixed-clock gate dropped a 1s utterance sitting in a mostly
        # silent block; VAD must keep it as its own segment. Segments are
        # 4-tuples: (audio_start, audio_end, disp_start, disp_end).
        audio = np.concatenate([_sil(0.5), _tone(1.0), _sil(20)])
        segs = processor._segment_speech(audio)
        assert len(segs) == 1
        a_start, a_end, d_start, d_end = segs[0]
        # Display bounds are the tight speech extent (~0.5–1.5s)…
        assert d_start / FS == pytest.approx(0.5, abs=0.1)
        assert d_end / FS == pytest.approx(1.5, abs=0.1)
        # …and the audio window fully covers the speech.
        assert a_start <= d_start and a_end >= d_end

    def test_micro_gap_bridged_into_one_segment(self):
        # A sub-threshold pause (0.2s < BATCH_MIN_SILENCE_GAP_SECONDS) between
        # words must not split the sentence.
        audio = np.concatenate([_tone(1.0), _sil(0.2), _tone(1.0)])
        assert len(processor._segment_speech(audio)) == 1

    def test_real_pause_splits(self):
        # A 1s pause between normal-length phrases is a sentence break → two
        # segments. (Phrases under BATCH_MIN_STANDALONE_SECONDS get merged
        # instead — see the merge tests below.)
        audio = np.concatenate([_tone(3.0), _sil(1.0), _tone(3.0)])
        assert len(processor._segment_speech(audio)) == 2

    def test_short_gap_absorbed_no_speech_dropped(self):
        # A pause up to BATCH_MAX_SILENCE_KEEP_SECONDS is absorbed: the two
        # segments meet (no audio between them is dropped), so quiet speech
        # trailing/leading a phrase can never fall into a discarded gap.
        audio = np.concatenate([_tone(3.0), _sil(1.0), _tone(3.0)])
        segs = processor._segment_speech(audio)
        assert len(segs) == 2
        # audio_end of seg 0 == audio_start of seg 1 (contiguous coverage)
        assert segs[0][1] == segs[1][0]

    def test_short_block_merged_into_neighbor(self):
        # A 1s snippet shortly after a phrase would hallucinate if transcribed
        # alone ("giggle"-class output) — it is absorbed into the neighbor.
        audio = np.concatenate([_tone(8.0), _sil(1.0), _tone(1.0), _sil(20)])
        segs = processor._segment_speech(audio)
        assert len(segs) == 1
        _a_s, _a_e, d_start, d_end = segs[0]
        assert d_end / FS == pytest.approx(10.0, abs=0.2)  # snippet included

    def test_isolated_short_utterance_stays_standalone(self):
        # No neighbor within BATCH_MAX_SILENCE_KEEP_SECONDS → keep it as its
        # own segment (real content must never be silently dropped).
        audio = np.concatenate([_tone(8.0), _sil(5.0), _tone(1.0), _sil(5.0)])
        assert len(processor._segment_speech(audio)) == 2

    def test_long_run_capped_without_tiny_fragments(self):
        # 40s of unbroken speech is split into a few cap-sized pieces, none of
        # them a degenerate sliver.
        segs = processor._segment_speech(_tone(40))
        assert len(segs) >= 2
        durations = [(a_e - a_s) / FS for a_s, a_e, _ds, _de in segs]
        assert max(durations) <= processor.BATCH_MAX_SEGMENT_SECONDS + 0.05
        # No forced split shorter than ~half the cap (the sliver bug).
        assert min(durations[:-1]) >= processor.BATCH_MAX_SEGMENT_SECONDS * 0.5

    def test_forced_split_keeps_display_on_speech(self):
        # A split long segment must not inherit the absorbed leading/trailing
        # silence into its subtitle timing: the first piece's display starts
        # at the speech, the last piece's display ends with it.
        audio = np.concatenate([_sil(1.5), _tone(20.0), _sil(1.5)])
        segs = processor._segment_speech(audio)
        assert len(segs) >= 2
        assert segs[0][2] / FS == pytest.approx(1.5, abs=0.1)
        assert segs[-1][3] / FS == pytest.approx(21.5, abs=0.1)

    def test_forced_split_leaves_no_tiny_tail(self):
        # A quiet dip right before the cap must not attract the cut so close
        # to the segment end that a sub-BATCH_MIN_STANDALONE_SECONDS tail
        # remains — that is exactly the hallucination-prone segment class the
        # short-block merge exists to prevent.
        audio = _tone(15.5)
        audio[int(14.6 * FS) : int(14.9 * FS)] *= 0.1
        segs = processor._segment_speech(audio)
        assert len(segs) >= 2
        durations = [(a_e - a_s) / FS for a_s, a_e, _ds, _de in segs]
        assert min(durations) >= processor.BATCH_MIN_STANDALONE_SECONDS - 0.05


def _write_test_wav(path, segments_pattern):
    """Build a WAV of DURATION-second chunks: 't' = tone, 's' = silence."""
    parts = []
    t = np.arange(DURATION * FS) / FS
    tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    silence = np.zeros(DURATION * FS, dtype=np.float32)
    for kind in segments_pattern:
        parts.append(tone if kind == "t" else silence)
    audio = np.concatenate(parts)
    wavfile.write(str(path), FS, (audio * 32767).astype(np.int16))


class TestProcessFile:
    @pytest.fixture
    def pipeline(self, monkeypatch, tmp_path):
        """Mocked pipeline around a real 3-segment WAV (tone, silence, tone)."""
        wav = tmp_path / "khutbah.wav"
        _write_test_wav(wav, "tst")

        calls = {
            "transcribe": [],
            "translate": [],
            "record": [],
            "record_formats": [],
            "prompts": [],
        }

        class FakeTranscription:
            def transcribe(self, audio, *, model, language=None, prompt=None):
                calls["transcribe"].append((model, language))
                calls["prompts"].append(prompt)
                # Arabic passes return Arabic script, others Latin (matters
                # for the Arabic-script reuse check in the re-pass condition).
                return "نص عربي" if language == "ar" else "metin"

        monkeypatch.setattr(
            processor,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Arabic",
                target_language="German",
                islamic_mode=True,
                noise_filter=False,  # tone-based test WAVs are not speech
            ),
        )
        monkeypatch.setattr(
            processor, "get_transcription_model_chain", lambda: ["m1", "m2"]
        )
        monkeypatch.setattr(
            processor, "get_transcription_provider", lambda: FakeTranscription()
        )
        # Per-run provider override path.
        monkeypatch.setattr(
            processor,
            "get_transcription_provider_for",
            lambda pid: FakeTranscription(),
        )
        monkeypatch.setattr(
            processor,
            "get_transcription_model_chain_for",
            lambda pid, model=None: [model or "d1", "d2"],
        )
        # The fallback/re-pass helpers live in translation.stt since the
        # controller/batch consolidation — patch the retry there.
        monkeypatch.setattr(
            translation.stt, "retry_with_backoff", lambda fn, **kw: fn()
        )

        def fake_translate(
            text,
            context="",
            arabic_text="",
            model=None,
            provider=None,
            source_language=None,
            target_language=None,
        ):
            calls["translate"].append(
                (text, context, arabic_text, model, provider,
                 source_language, target_language)
            )
            return f"DE({text})"

        monkeypatch.setattr(processor, "translate_text", fake_translate)

        # Don't touch the real AppData batch dir during tests; capture instead.
        def fake_record(name, src, tgt, entries, formats=None):
            calls["record"].append((name, src, tgt, entries))
            calls["record_formats"].append(formats)

        monkeypatch.setattr(processor, "write_batch_record", fake_record)
        return wav, calls

    def test_end_to_end(self, pipeline):
        wav, calls = pipeline
        progress = []
        out = processor.process_file(
            str(wav), progress_callback=lambda done, total: progress.append((done, total))
        )
        assert out == str(wav.with_name("khutbah.de.srt"))
        content = Path(out).read_text(encoding="utf-8-sig")
        # Middle (silent) segment produced no subtitle and no API call
        assert content.count("-->") == 2
        assert len(calls["transcribe"]) == 2
        assert "DE(نص عربي)" in content
        # Timestamps from segment positions
        assert format_timestamp(0) in content
        assert format_timestamp(DURATION) in content
        assert format_timestamp(2 * DURATION) in content
        assert format_timestamp(3 * DURATION) in content
        # Silence between the two tones is not a segment: 2 segments, not 3.
        assert progress == [(1, 2), (2, 2)]

    def test_empty_translation_produces_no_srt_entry(self, pipeline, monkeypatch):
        """GPT judging a segment unintelligible returns "" — no SRT line (the
        'Das Wort ist unverständlich' meta-comment class)."""
        wav, calls = pipeline
        monkeypatch.setattr(processor, "translate_text", lambda *a, **k: "")
        out = processor.process_file(str(wav))
        content = Path(out).read_text(encoding="utf-8-sig")
        assert content.count("-->") == 0  # both segments dropped from output
        assert len(calls["transcribe"]) == 2  # transcription still ran

    def test_arabic_source_skips_re_transcription(self, pipeline):
        wav, calls = pipeline
        processor.process_file(str(wav))
        # One transcription per non-silent segment, none with a forced "ar"
        assert all(lang == "ar" for _m, lang in calls["transcribe"])
        assert all(c[2] == "" for c in calls["translate"])  # arabic_text

    def test_non_arabic_source_adds_arabic_pass(self, pipeline, monkeypatch):
        wav, calls = pipeline
        monkeypatch.setattr(
            processor,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Turkish",
                target_language="German",
                islamic_mode=True,
                noise_filter=False,
            ),
        )
        processor.process_file(str(wav))
        # Two calls per non-silent segment: source language + Arabic for RAG
        assert len(calls["transcribe"]) == 4
        assert [lang for _m, lang in calls["transcribe"]] == ["tr", "ar", "tr", "ar"]
        assert all(c[2] == "نص عربي" for c in calls["translate"])  # arabic_text

    def test_same_language_run_skips_arabic_pass(self, pipeline, monkeypatch):
        wav, calls = pipeline
        monkeypatch.setattr(
            processor,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="German",
                target_language="German",
                islamic_mode=True,
                noise_filter=False,
            ),
        )
        processor.process_file(str(wav))
        # Same-language: translation is bypassed, so the Arabic RAG pass
        # would be pure cost — one transcription per segment, never forced "ar"
        assert [lang for _m, lang in calls["transcribe"]] == ["de", "de"]
        assert all(c[2] == "" for c in calls["translate"])  # arabic_text

    def test_arabic_script_transcription_skips_re_pass(self, pipeline, monkeypatch):
        wav, calls = pipeline
        monkeypatch.setattr(
            processor,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Automatic",
                target_language="German",
                islamic_mode=True,
                noise_filter=False,
            ),
        )

        class ArabicSpeech:
            def transcribe(self, audio, *, model, language=None, prompt=None):
                calls["transcribe"].append((model, language))
                return "نص عربي"

        monkeypatch.setattr(
            processor, "get_transcription_provider", lambda: ArabicSpeech()
        )
        processor.process_file(str(wav))
        # The primary transcription already came back in Arabic script: the
        # matchers use it directly (translate_text falls back to the main
        # text), so no second forced-Arabic call is made.
        assert [lang for _m, lang in calls["transcribe"]] == [None, None]
        assert all(c[2] == "" for c in calls["translate"])  # arabic_text

    def test_prompt_chains_previous_transcription(self, pipeline):
        wav, calls = pipeline
        processor.process_file(str(wav))
        # First segment has no context; the second is prompted with the tail
        # of the first segment's transcription (cross-segment continuity).
        assert calls["prompts"][0] is None
        assert calls["prompts"][1] == "نص عربي"

    def test_verbatim_prompt_echo_dropped(self, pipeline, monkeypatch):
        """Live-observed: on non-speech audio the model can echo the
        continuity prompt back verbatim — that segment must be dropped, not
        written to the SRT as duplicated text."""
        wav, _calls = pipeline
        long_text = "كلمة " * 40  # above the echo-guard length gate

        class Echo:
            def __init__(self):
                self.n = 0

            def transcribe(self, audio, *, model, language=None, prompt=None):
                self.n += 1
                return long_text if self.n == 1 else prompt

        monkeypatch.setattr(processor, "get_transcription_provider", lambda: Echo())
        out = processor.process_file(str(wav))
        content = Path(out).read_text(encoding="utf-8-sig")
        assert content.count("-->") == 1  # echoed second segment dropped

    def test_short_repeat_not_mistaken_for_echo(self, pipeline):
        """Identical short phrases across segments (dhikr, takbir) are real
        speech and must be kept — the echo guard is length-gated."""
        wav, _calls = pipeline
        out = processor.process_file(str(wav))
        # The fake returns the same short text for both segments.
        content = Path(out).read_text(encoding="utf-8-sig")
        assert content.count("-->") == 2

    def test_rolling_context(self, pipeline):
        wav, calls = pipeline
        processor.process_file(str(wav))
        first_context = calls["translate"][0][1]
        second_context = calls["translate"][1][1]
        assert first_context == ""
        assert "نص عربي" in second_context

    def test_cancel_returns_none_and_writes_nothing(self, pipeline):
        wav, _calls = pipeline
        cancel = threading.Event()
        cancel.set()
        assert processor.process_file(str(wav), cancel_event=cancel) is None
        assert not wav.with_name("khutbah.de.srt").exists()

    def test_failed_segments_are_skipped(self, pipeline, monkeypatch):
        wav, _calls = pipeline

        class Broken:
            def transcribe(self, audio, *, model, language=None, prompt=None):
                raise RuntimeError("api down")

        monkeypatch.setattr(processor, "get_transcription_provider", lambda: Broken())
        out = processor.process_file(str(wav))
        assert Path(out).read_text(encoding="utf-8-sig") == ""

    def test_model_overrides_lead_the_chains(self, pipeline):
        wav, calls = pipeline
        processor.process_file(
            str(wav),
            transcription_model="whisper-x",
            translation_model="gpt-x",
        )
        # The explicit STT model leads the transcription fallback chain.
        assert calls["transcribe"][0][0] == "whisper-x"
        # The explicit translation model reaches translate_text (index 3).
        assert all(c[3] == "gpt-x" for c in calls["translate"])

    def test_provider_overrides_use_by_id_accessors(self, pipeline):
        wav, calls = pipeline
        processor.process_file(
            str(wav),
            transcription_provider="gemini",
            transcription_model="gem-stt",
            translation_provider="anthropic",
            translation_model="claude-x",
        )
        # STT chain comes from get_transcription_model_chain_for → leads model.
        assert calls["transcribe"][0][0] == "gem-stt"
        # Translation provider + model both reach translate_text (idx 3, 4).
        assert all(c[3] == "claude-x" and c[4] == "anthropic" for c in calls["translate"])

    def test_language_overrides_flow_through(self, pipeline, monkeypatch, tmp_path):
        # Batch picks its own languages; settings say Arabic→German but the run
        # overrides to French→English. Output name, record and translate_text
        # all use the override, not settings.
        wav, calls = pipeline
        out = processor.process_file(
            str(wav), source_language="French", target_language="English"
        )
        assert out.endswith(".en.srt")  # target override drives the SRT name
        name, src, tgt, _entries = calls["record"][0]
        assert (src, tgt) == ("French", "English")
        assert all(
            c[5] == "French" and c[6] == "English" for c in calls["translate"]
        )

    def test_batch_record_written_with_pairs(self, pipeline):
        wav, calls = pipeline
        processor.process_file(str(wav))
        assert len(calls["record"]) == 1
        name, src, tgt, entries = calls["record"][0]
        assert name == "khutbah.wav"
        assert (src, tgt) == ("Arabic", "German")
        # One (offset, transcription, translation) per non-silent segment.
        assert len(entries) == 2
        assert entries[0][1] == "نص عربي"
        assert entries[0][2] == "DE(نص عربي)"

    def test_non_wav_without_ffmpeg_raises(self, pipeline, monkeypatch):
        _wav, _calls = pipeline
        monkeypatch.setattr(processor, "is_ffmpeg_available", lambda: False)
        with pytest.raises(processor.FfmpegNotFoundError):
            processor.process_file("recording.mp4")

    def test_noise_filter_skips_non_speech_segments(self, pipeline, monkeypatch):
        """With the filter on, a loud-but-not-speech segment costs no API
        call and produces no subtitle."""
        wav, calls = pipeline
        monkeypatch.setattr(
            processor,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Arabic",
                target_language="German",
                islamic_mode=True,
                noise_filter=True,
            ),
        )
        monkeypatch.setattr(processor, "has_speech", lambda chunk: False)
        progress = []
        out = processor.process_file(
            str(wav), progress_callback=lambda d, t: progress.append((d, t))
        )
        assert calls["transcribe"] == []
        assert Path(out).read_text(encoding="utf-8-sig") == ""
        # Skipped segments still advance the progress bar.
        assert progress == [(1, 2), (2, 2)]

    def test_output_format_txt(self, pipeline):
        wav, _calls = pipeline
        out = processor.process_file(str(wav), output_format="txt")
        assert out == str(wav.with_name("khutbah.de.txt"))
        content = Path(out).read_text(encoding="utf-8-sig")
        assert "TRANSCRIPT (Arabic)" in content
        assert "TRANSLATION (German)" in content
        assert "DE(نص عربي)" in content
        assert not wav.with_name("khutbah.de.srt").exists()

    def test_output_format_both(self, pipeline):
        wav, _calls = pipeline
        out = processor.process_file(str(wav), output_format="both")
        # Status shows the SRT; both files land next to the source.
        assert out == str(wav.with_name("khutbah.de.srt"))
        assert wav.with_name("khutbah.de.srt").exists()
        assert wav.with_name("khutbah.de.txt").exists()

    def test_output_format_unknown_defaults_to_srt(self, pipeline):
        wav, _calls = pipeline
        out = processor.process_file(str(wav), output_format="pdf")
        assert out == str(wav.with_name("khutbah.de.srt"))
        assert not wav.with_name("khutbah.de.txt").exists()

    def test_bilingual_srt_includes_source_line(self, pipeline):
        wav, _calls = pipeline
        out = processor.process_file(str(wav), bilingual_srt=True)
        content = Path(out).read_text(encoding="utf-8-sig")
        # Each block carries the original transcription above the translation.
        assert "نص عربي\nDE(نص عربي)" in content

    def test_non_bilingual_srt_is_translation_only(self, pipeline):
        wav, _calls = pipeline
        out = processor.process_file(str(wav))  # bilingual off by default
        content = Path(out).read_text(encoding="utf-8-sig")
        assert "DE(نص عربي)" in content
        assert "نص عربي\nDE(نص عربي)" not in content  # no standalone source line

    def test_output_format_passed_to_record(self, pipeline):
        wav, calls = pipeline
        processor.process_file(str(wav), output_format="both")
        assert calls["record_formats"] == ["both"]

    def test_srt_sidecar_stored_next_to_record(self, pipeline, monkeypatch, tmp_path):
        """A 'both' run keeps an exact SRT next to the record, and the record
        header carries the exported formats + full language names."""
        from utils import history as history_mod

        wav, _calls = pipeline
        batch_dir = tmp_path / "batch"
        batch_dir.mkdir()
        monkeypatch.setattr(history_mod, "BATCH_DIR", str(batch_dir))
        monkeypatch.setattr(
            processor, "write_batch_record", history_mod.write_batch_record
        )
        processor.process_file(str(wav), output_format="both")

        records = list(batch_dir.glob("*.txt"))
        srts = list(batch_dir.glob("*.srt"))
        assert len(records) == 1 and len(srts) == 1
        head = records[0].read_text(encoding="utf-8")
        assert "# formats: srt,txt" in head
        assert "# langs: Arabic|German" in head
        assert "-->" in srts[0].read_text(encoding="utf-8-sig")  # real timing

    def test_txt_only_run_stores_no_srt_sidecar(self, pipeline, monkeypatch, tmp_path):
        from utils import history as history_mod

        wav, _calls = pipeline
        batch_dir = tmp_path / "batch"
        batch_dir.mkdir()
        monkeypatch.setattr(history_mod, "BATCH_DIR", str(batch_dir))
        monkeypatch.setattr(
            processor, "write_batch_record", history_mod.write_batch_record
        )
        processor.process_file(str(wav), output_format="txt")

        assert list(batch_dir.glob("*.srt")) == []
        head = list(batch_dir.glob("*.txt"))[0].read_text(encoding="utf-8")
        assert "# formats: txt" in head


class TestTextWriter:
    def test_two_sections_when_translated(self):
        from batch.text_writer import build_text

        text = build_text(
            [(0.0, "نص", "DE(نص)"), (12.0, "ثاني", "DE(ثاني)")],
            "Arabic",
            "German",
        )
        assert "TRANSCRIPT (Arabic)" in text
        assert "TRANSLATION (German)" in text
        # One paragraph per segment, in order.
        assert text.index("نص") < text.index("ثاني")
        assert text.index("DE(نص)") < text.index("DE(ثاني)")

    def test_identical_pairs_collapse_to_one_section(self):
        """Same-language runs must not print the same text twice (mirrors
        the history viewer's collapsing)."""
        from batch.text_writer import build_text

        text = build_text([(0.0, "نص عربي", "نص عربي")], "Arabic", "Arabic")
        assert text.count("نص عربي") == 1
        assert "TRANSLATION" not in text

    def test_empty_records(self):
        from batch.text_writer import build_text

        assert build_text([], "Arabic", "German") == ""

    def test_write_text_uses_bom(self, tmp_path):
        from batch.text_writer import write_text

        path = tmp_path / "out.txt"
        write_text([(0.0, "a", "b")], str(path), "Arabic", "German")
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
