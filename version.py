"""Version information for MinbarLive."""

__version__ = "1.0.0-rc.1"
# Split version into numeric and optional suffix (e.g., 'beta')
_version_parts = __version__.split("-")
_version_nums = _version_parts[0].split(".")
__version_info__ = tuple(int(x) for x in _version_nums)

# Version history:
# 1.0.0-rc.1 - Release candidate for 1.0.0. Published as a PRE-RELEASE, so it is
#              invisible to installed copies: the update check and the website
#              download buttons both read /releases/latest, which skips it.
#            - Streaming translates finished sentences out of a running turn
#              instead of only at its end, so pauseless speech no longer shows
#              one late block (#26, openai_realtime only)
#            - Qt control panel: the log panel gives the window its width back
#              when it closes, and the column count no longer oscillates at the
#              three-column threshold
#            - Build: dependencies' own test suites are no longer bundled
#              (hiddenimports 3468 -> 3245, #48)
#            - Fortlaufend (Ticker) no longer dims every line but the newest;
#              the original text keeps its muted tone
#            - The language swap button reopens the stream, so the engine stops
#              transcribing in the language you just switched away from
#            - Linux: the real microphone is no longer filtered out of the input
#              list (PulseAudio's description vs its source name)
# 1.0.0-beta - Initial open source release
#            - Real-time audio transcription and translation
#            - RAG-enhanced Quran verse matching
#            - Multi-language support (15+ source, 35+ target)
#            - Three subtitle display modes
#            - Persistent user settings
