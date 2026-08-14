"""Version information for MinbarLive."""

__version__ = "1.0.0"
# Split version into numeric and optional suffix (e.g., 'beta')
_version_parts = __version__.split("-")
_version_nums = _version_parts[0].split(".")
__version_info__ = tuple(int(x) for x in _version_nums)

# Version history:
# 1.0.0     - First stable release. Verified live in a mosque across the rc.1
#             cycle.
#           - Subtitles read as continuous speech: the translator now sees the
#             lines it has already put on screen, so a sentence split across
#             blocks is continued rather than restarted, and a fragment ends
#             unpunctuated instead of closing a sentence the speaker has not
#             (#103)
#           - Nothing that certifies a Quran verse can reach the translator's
#             context — neither the verified marker nor a (surah:ayah)
#             reference — so an unverified line can never be dressed as a
#             verified one (#103)
#           - The verified-verse log names the verse it certified (#103)
#           - Subtitles arrive every ~5s during fast recitation instead of in
#             18s bursts: a stalled turn is committed rather than the working
#             connection being torn down (#91)
#           - Quran recognition follows a running recitation through verses the
#             embedding alone would miss, and every verified verse is confirmed
#             by an exact text check (#93, #96, #97, #99)
#           - Settings → Delete everything: removes the app-data folder and
#             every provider's keychain entry (#81)
#           - Optional notices for pre-release builds, and an update can be
#             skipped per version (#79, #82)
#           - Footer can be hidden independently of the subtitles (#92, #95)
#           - Numerous Qt control-panel fixes: startup flash, window flags and
#             icon, column threshold, panel sizing, announcement newlines
#             (#85-#90)
#           - Linux: batch ffmpeg and file dialog, microphone source naming
#             (#94, #75)
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
