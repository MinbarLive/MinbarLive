"""Tests for JSON helper functions."""

import pytest
import json
import tempfile
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.json_helpers import load_json


class TestLoadJson:
    """Tests for load_json function."""

    def test_load_valid_json(self):
        """Should load valid JSON file."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value", "number": 42}, f)
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result == {"key": "value", "number": 42}
        finally:
            os.unlink(temp_path)

    def test_load_nonexistent_file(self):
        """Should return empty dict for nonexistent file."""
        result = load_json("/nonexistent/path/file.json")
        assert result == {}

    def test_load_empty_object(self):
        """Should load empty JSON object."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({}, f)
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result == {}
        finally:
            os.unlink(temp_path)

    def test_load_nested_json(self):
        """Should load nested JSON structures."""
        data = {"outer": {"inner": {"value": [1, 2, 3]}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result == data
        finally:
            os.unlink(temp_path)

    def test_load_unicode_content(self):
        """Should handle Unicode content (Arabic text)."""
        data = {"arabic": "بسم الله الرحمن الرحيم", "german": "Im Namen Gottes"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(data, f, ensure_ascii=False)
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result["arabic"] == "بسم الله الرحمن الرحيم"
            assert result["german"] == "Im Namen Gottes"
        finally:
            os.unlink(temp_path)

    def test_load_array_returns_as_is(self):
        """Should handle JSON arrays (though we expect dicts)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(["a", "b", "c"], f)
            temp_path = f.name

        try:
            result = load_json(temp_path)
            # The function returns whatever is in the JSON
            assert result == ["a", "b", "c"]
        finally:
            os.unlink(temp_path)


class TestLoadJsonEdgeCases:
    """Edge case tests for load_json."""

    def test_empty_path(self):
        """Should handle empty path string."""
        result = load_json("")
        assert result == {}

    def test_directory_path(self, tmp_path):
        """Should handle directory path (not a file) gracefully."""
        # Use a temp directory that exists on all platforms
        result = load_json(str(tmp_path))
        assert result == {}

    def test_malformed_json(self):
        """Should handle malformed JSON gracefully."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write("{invalid json content")
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result == {}  # Returns default on parse error
        finally:
            os.unlink(temp_path)

    def test_empty_file(self):
        """Should handle empty file gracefully."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write("")  # Empty file
            temp_path = f.name

        try:
            result = load_json(temp_path)
            assert result == {}  # Returns default for empty file
        finally:
            os.unlink(temp_path)

    def test_custom_default(self):
        """Should use custom default value."""
        result = load_json("/nonexistent/file.json", default={"fallback": True})
        assert result == {"fallback": True}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
