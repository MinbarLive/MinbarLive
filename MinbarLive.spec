# -*- mode: python ; coding: utf-8 -*-

import glob
import importlib.util
import os
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"

# --- Fix contrib hooks that hardcode an import name as the distribution name ---
# webrtcvad is installed as the 'webrtcvad-wheels' distribution (the Windows
# wheels), so the bundled hook-webrtcvad.py's copy_metadata('webrtcvad') finds
# no metadata and aborts the build. Wrap copy_metadata to retry under the real
# distribution name (resolved from the import name) before failing. Re-raises
# when no distribution can be resolved, so genuinely-missing metadata still
# surfaces instead of being silently swallowed.
import PyInstaller.utils.hooks as _pyi_hooks

_orig_copy_metadata = _pyi_hooks.copy_metadata


def _copy_metadata_by_import_name(package_name, *args, **kwargs):
    try:
        return _orig_copy_metadata(package_name, *args, **kwargs)
    except Exception:
        import importlib.metadata as _im

        for dist in _im.packages_distributions().get(package_name, []):
            try:
                return _orig_copy_metadata(dist, *args, **kwargs)
            except Exception:
                continue
        raise


_pyi_hooks.copy_metadata = _copy_metadata_by_import_name

# Windows/macOS only. PyInstaller has no use for an icon on Linux — the desktop
# environment takes it from a .desktop entry, not from the binary.
ICON_PATH = "public/MinbarLive.ico"
# Embeds per-monitor DPI awareness (plus longPathAware and Common-Controls v6)
# into the frozen EXE, so the packaged app is aware from process start.
# Windows-only.
MANIFEST_PATH = "MinbarLive.manifest"

hiddenimports = (
    collect_submodules("sounddevice")
    # numpy is collected by PyInstaller's comprehensive built-in hook-numpy.
    # collect_submodules("numpy") on top of it only added numpy.testing/f2py/
    # distutils/tests (build+test tooling the app never imports), so it is
    # dropped — the hook still bundles everything numpy needs at runtime.
    # scipy: the app only uses scipy.io.wavfile. Collecting just scipy.io (not
    # all of scipy) drops ~90 MB of unused subpackages — stats/special/optimize/
    # linalg/signal/spatial/interpolate/…. The scipy.sparse base modules that
    # scipy.io.matlab imports are still pulled in automatically by PyInstaller's
    # import graph, so wavfile reading/writing keeps working. Collecting all of
    # scipy.sparse instead would re-pull scipy.linalg (via scipy.sparse.linalg)
    # and undo most of the saving.
    + collect_submodules("scipy.io")
    + collect_submodules("openai")
    + collect_submodules("google.genai")  # imported lazily by providers/gemini
    + collect_submodules("anthropic")  # imported lazily by providers/anthropic
    + collect_submodules("deepgram")  # imported lazily by providers/deepgram
    + collect_submodules("websockets")  # streaming transport (deepgram + openai realtime)
    + collect_submodules("dotenv")
    + collect_submodules("screeninfo")
    + collect_submodules("keyring")
    # PySide6 has a comprehensive PyInstaller hook of its own — it collects the
    # Qt libraries, the platform PLUGINS and the translations. Listing
    # submodules on top of it only re-pulls what the hook already has. What the
    # hook cannot do is conjure system libraries the build machine lacks: on
    # Linux the xcb platform plugin links against libxcb-cursor and friends, so
    # the builder must install them BEFORE this runs (release.yml does) or the
    # plugin ships unloadable and the app falls through to Wayland, where the
    # subtitle overlay can be neither placed nor kept on top.
    + collect_submodules("webrtcvad")  # imported lazily by audio/vad.py
    + collect_submodules("soundcard")  # imported lazily for WASAPI loopback capture
)

# keyring's Linux Secret Service backend (GNOME Keyring / KWallet) is provided by
# secretstorage + jeepney, which keyring only declares as dependencies on Linux.
# collect_submodules("keyring") pulls in the SecretService backend module but not
# those two external packages, so PyInstaller may miss them — leaving the frozen
# Linux binary with no keyring backend, which makes every provider's key
# session-only (nothing is persisted without a keychain, see utils/settings.py).
# Bundle them explicitly. They are not installed on Windows/macOS, so guard on the
# platform; secretstorage's cryptography dependency rides along via the import
# graph (PyInstaller ships a dedicated cryptography hook).
if IS_LINUX:
    hiddenimports += collect_submodules("secretstorage")
    hiddenimports += collect_submodules("jeepney")

# Exclude the MASSIVE unused libraries
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "keras",
    "dask",
    "pygments",
    "pytest",
    "ruff",
    "matplotlib",
    # PIL is NOT excluded: utils/icons.py crops and scales the header logo
    # with Pillow. Excluding it left the frozen app with a wordmark-only
    # header, and nothing failed loudly.
    # Tkinter is gone with the CustomTkinter tree, and excluding it keeps a
    # stray tkinter import from ever pulling Tcl/Tk into the bundle again.
    "tkinter",
    "pandas",
    "IPython",
    "notebook",
    "jupyter",
    # Build/packaging tooling never used at runtime. keyring's deps
    # (jaraco.*, more_itertools) are installed standalone, not via
    # setuptools._vendor, so dropping setuptools/pkg_resources is safe; the
    # only references are conditional/optional (webrtcvad, urllib3).
    "setuptools",
    "pkg_resources",
    "pip",
    "wheel",
]

# Collect native binaries (DLLs) required by these packages.
binaries = (
    collect_dynamic_libs("sounddevice")
    + collect_dynamic_libs("numpy")
    + collect_dynamic_libs("scipy")
)

# The Windows and macOS sounddevice wheels ship PortAudio inside the package,
# so collect_dynamic_libs above finds it. The Linux wheel does not — sounddevice
# resolves it at import time via ctypes.util.find_library("portaudio"), which
# searches only the system library cache (ldconfig), never LD_LIBRARY_PATH or the
# onefile extraction directory. So bundling the lib here is necessary but not
# sufficient: rthook_portaudio.py points find_library at the bundled copy at
# runtime, otherwise a machine without the system libportaudio2 package crashes
# at startup with "PortAudio library not found". Build dep: apt libportaudio2.
if IS_LINUX:
    _portaudio = glob.glob("/usr/lib/*/libportaudio.so*") + glob.glob(
        "/usr/lib/libportaudio.so*"
    )
    if _portaudio:
        binaries += [(path, ".") for path in _portaudio]
    else:
        print("WARNING: libportaudio not found - the Linux build will have no audio.")

# Bundle project data/ and public/ into the executable (available under sys._MEIPASS/)
datas = [("data", "data"), ("public", "public")]

# soundcard reads a cffi header out of its own package directory AT IMPORT
# (pulseaudio.py.h on Linux, coreaudio.py.h on macOS, mediafoundation.py.h on
# Windows). Those are DATA files, so the import graph never carries them: the
# only thing that bundles them is soundcard's own PyInstaller hook, which
# PyInstaller discovers through an entry point it resolves by IMPORTING
# soundcard.
#
# On Linux that import runs `_pulse = _PulseAudio()` at module level, and the
# constructor ends in `assert ... == PA_CONTEXT_READY`. No CI runner runs a
# PulseAudio DAEMON, so the context never becomes ready and the import raises
# AssertionError. PyInstaller downgrades that to a warning, skips the hook, and
# ships soundcard/pulseaudio.py without pulseaudio.py.h — so `import soundcard`
# dies with FileNotFoundError inside the AppImage. Every call site catches
# Exception (gui/device_list.py, app_controller.py), so nothing fails loudly:
# the microphone dropdown silently loses both the JACK-monitor filter and every
# loopback device. Installing libpulse0 on the builder was necessary but not
# sufficient — it fixes the dlopen, not the absent daemon.
#
# So resolve the package path WITHOUT importing it (find_spec locates, it does
# not execute) and bundle the headers here. What ships no longer depends on the
# builder's audio stack, which is the same rule the xcb and libEGL notes in
# release.yml are built on. Hard-fail rather than warn: a silent skip is what
# made this survive a green build in the first place.
_soundcard_spec = importlib.util.find_spec("soundcard")
if _soundcard_spec is None or not _soundcard_spec.submodule_search_locations:
    raise SystemExit("soundcard is not installed - loopback capture would ship broken.")
_soundcard_headers = glob.glob(
    os.path.join(_soundcard_spec.submodule_search_locations[0], "*.py.h")
)
if not _soundcard_headers:
    raise SystemExit("soundcard ships no *.py.h - it cannot be imported at runtime.")
datas += [(header, "soundcard") for header in _soundcard_headers]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # Makes the bundled libportaudio.so.2 discoverable on Linux (see the
    # PortAudio note above). No-op on Windows/macOS.
    runtime_hooks=["rthook_portaudio.py"],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

# The app loads the compact quran_embeddings_*.npz matrices; the raw 400+ MB
# JSON is only the notebook's output and must stay out of the bundled EXE.
a.datas = [
    d
    for d in a.datas
    if not d[0].replace("\\", "/").endswith("data/embeddings/quran_embeddings.json")
]

pyz = PYZ(a.pure)

# One-file mode - slower startup but easier distribution
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MinbarLive",
    icon=ICON_PATH if IS_WINDOWS else None,
    manifest=MANIFEST_PATH if IS_WINDOWS else None,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# macOS: wrap the one-file binary in a .app bundle so it is double-clickable
# (a bare Unix executable opens a Terminal, not the GUI). EXPERIMENTAL and
# UNSIGNED — Gatekeeper will warn; users right-click → Open on first launch.
# NSMicrophoneUsageDescription is REQUIRED: modern macOS (10.14+) hard-kills any
# app that opens an input stream without a usage string in Info.plist, so
# without it the very first capture crashes the app under TCC. No icon: the .ico
# we ship is Windows-only and macOS wants .icns; the experimental build ships
# with the default icon rather than converting one in CI.
if IS_MACOS:
    try:
        from version import __version__ as _mac_version
    except Exception:
        _mac_version = "0.0.0"
    app = BUNDLE(
        exe,
        name="MinbarLive.app",
        icon=None,
        bundle_identifier="live.minbar.app",
        info_plist={
            "NSMicrophoneUsageDescription": (
                "MinbarLive captures live audio to transcribe and translate "
                "speech into subtitles."
            ),
            "NSHighResolutionCapable": True,
            "CFBundleShortVersionString": _mac_version,
        },
    )
