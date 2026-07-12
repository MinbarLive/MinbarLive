"""Version information for MinbarLive."""

__version__ = "1.0.0-beta"
# Split version into numeric and optional suffix (e.g., 'beta')
_version_parts = __version__.split("-")
_version_nums = _version_parts[0].split(".")
__version_info__ = tuple(int(x) for x in _version_nums)
# Optionally, you can also expose the suffix if needed:
# __version_suffix__ = _version_parts[1] if len(_version_parts) > 1 else None

# Version history:
# 1.0.0-beta - Initial open source release
#            - Real-time audio transcription and translation
#            - RAG-enhanced Quran verse matching
#            - Multi-language support (15+ source, 35+ target)
#            - Three subtitle display modes
#            - Persistent user settings
