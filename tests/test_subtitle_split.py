"""Sentence-boundary splitting of oversized Realtime feed blocks.

Continuous speech flushes up to 12s of speech as ONE utterance; its settled
translation used to render as a wall of text. split_display_chunks breaks it
into readable blocks without losing a character.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui.subtitle_window import split_display_chunks


class TestSplitDisplayChunks:
    def test_short_text_passes_through(self):
        assert split_display_chunks("Kurzer Satz.", 220) == ["Kurzer Satz."]

    def test_empty_text(self):
        assert split_display_chunks("   ", 220) == []

    def test_splits_at_sentence_boundaries(self):
        text = "Erster Satz hier. Zweiter Satz folgt! Dritter Satz endet? Vierter."
        chunks = split_display_chunks(text, 40)
        assert chunks == [
            "Erster Satz hier. Zweiter Satz folgt!",
            "Dritter Satz endet? Vierter.",
        ]

    def test_no_text_lost(self):
        text = (
            "Der Prophet führt ihn mit dem Wort der Offenbarung. Denn Allah hat "
            "niemandem das Wort anvertraut. Er vertraute ihm das Wort an, und "
            "der Muslim folgt dem geliebten Gesandten. Derjenige, der die "
            "Führung übernommen hat, wird geliebt."
        )
        chunks = split_display_chunks(text, 100)
        assert len(chunks) >= 2
        assert all(len(c) <= 100 for c in chunks)
        assert " ".join(chunks) == text

    def test_overlong_single_sentence_stays_whole(self):
        text = "ein einziger sehr langer Satz ohne jede Interpunktion " * 5
        chunks = split_display_chunks(text.strip(), 60)
        assert chunks == [text.strip()]

    def test_closing_quotes_stay_with_their_sentence(self):
        text = "„Auf dass ihr dankbar sein möget.“ Qatada kommentiert dies."
        chunks = split_display_chunks(text, 40)
        assert chunks[0] == "„Auf dass ihr dankbar sein möget.“"
        assert chunks[1] == "Qatada kommentiert dies."

    def test_arabic_punctuation(self):
        text = "هل من خالق غير الله؟ الحمد لله رب العالمين. ثم قال الإمام كلاما طويلا جدا هنا."
        chunks = split_display_chunks(text, 45)
        assert len(chunks) >= 2
        assert " ".join(chunks) == text
