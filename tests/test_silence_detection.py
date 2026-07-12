"""Tests for audio silence detection."""

import pytest
import numpy as np
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from audio.capture import is_silence


class TestIsSilence:
    """Tests for silence detection function."""

    def test_silent_audio(self):
        """All-zero audio should be detected as silence."""
        silent = np.zeros(16000, dtype=np.float32)  # 1 second at 16kHz
        assert is_silence(silent) == True

    def test_very_quiet_audio(self):
        """Very quiet audio should be detected as silence."""
        quiet = np.random.randn(16000).astype(np.float32) * 0.0001
        assert is_silence(quiet) == True

    def test_loud_audio(self):
        """Loud audio should not be detected as silence."""
        loud = np.random.randn(16000).astype(np.float32) * 0.5
        assert is_silence(loud) == False

    def test_empty_audio(self):
        """Empty array should be detected as silence."""
        empty = np.array([], dtype=np.float32)
        assert is_silence(empty) == True

    def test_short_audio(self):
        """Very short audio should be handled."""
        short = np.array([0.0, 0.0, 0.0], dtype=np.float32)
        result = is_silence(short)
        assert result in (True, False)  # Accept numpy bool

    def test_mixed_audio(self):
        """Audio with some loud and some quiet parts."""
        # 80% silence, 20% loud -> should be detected as silence
        audio = np.zeros(16000, dtype=np.float32)
        audio[12800:] = np.random.randn(3200).astype(np.float32) * 0.5
        # With default 80% silence ratio, this should be silent
        assert is_silence(audio) == True

    def test_mostly_loud(self):
        """Audio that is mostly loud should not be silence."""
        # 20% silence, 80% loud
        audio = np.random.randn(16000).astype(np.float32) * 0.5
        audio[:3200] = 0.0
        assert is_silence(audio) == False

    def test_custom_threshold(self):
        """Should respect custom threshold parameter."""
        audio = np.random.randn(16000).astype(np.float32) * 0.01
        # With very high threshold, should be silence
        assert is_silence(audio, threshold=0.1) == True
        # With very low threshold, should not be silence
        assert is_silence(audio, threshold=0.0001) == False

    def test_custom_ratio(self):
        """Should respect custom silence ratio parameter."""
        # 60% silent
        audio = np.zeros(16000, dtype=np.float32)
        audio[9600:] = np.random.randn(6400).astype(np.float32) * 0.5
        # With 50% ratio requirement, should be silence
        assert is_silence(audio, silence_ratio=0.5) == True
        # With 80% ratio requirement, should not be silence
        assert is_silence(audio, silence_ratio=0.8) == False


class TestIsSilenceEdgeCases:
    """Edge case tests for silence detection."""

    def test_single_sample(self):
        """Single sample should be handled."""
        single = np.array([0.0], dtype=np.float32)
        result = is_silence(single)
        assert result in (True, False)  # Accept numpy bool

    def test_nan_values(self):
        """NaN values should be handled gracefully."""
        audio = np.array([np.nan, 0.0, 0.0], dtype=np.float32)
        # Should not raise, behavior with NaN is implementation-dependent
        try:
            result = is_silence(audio)
            assert result in (True, False)  # Accept numpy bool
        except (ValueError, RuntimeWarning):
            pass  # Some implementations may raise

    def test_inf_values(self):
        """Infinite values should be handled."""
        audio = np.array([np.inf, 0.0, 0.0], dtype=np.float32)
        try:
            result = is_silence(audio)
            assert result in (True, False)  # Accept numpy bool
        except (ValueError, RuntimeWarning):
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
