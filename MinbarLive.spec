# -*- mode: python ; coding: utf-8 -*-

import glob
import sys

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules, collect_data_files

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

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
# into the frozen EXE, so the packaged app is aware from process start rather
# than from the first CustomTkinter window. Windows-only.
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
    + collect_submodules("customtkinter")
    + collect_submodules("arabic_reshaper")
    + collect_submodules("bidi")
    + collect_submodules("webrtcvad")  # imported lazily by audio/vad.py
    + collect_submodules("soundcard")  # imported lazily for WASAPI loopback capture
    # PIL.ImageTk (utils/icons.py header logo) pulls in the C helper module
    # PIL._tkinter_finder indirectly. PyInstaller's PIL hook picks it up on
    # Windows but misses it on Linux, so the frozen Linux app crashes the
    # logo render with "No module named 'PIL._tkinter_finder'" and shows a
    # wordmark-only header. Harmless to list on every platform.
    + ["PIL._tkinter_finder"]
)

# keyring's Linux Secret Service backend (GNOME Keyring / KWallet) is provided by
# secretstorage + jeepney, which keyring only declares as dependencies on Linux.
# collect_submodules("keyring") pulls in the SecretService backend module but not
# those two external packages, so PyInstaller may miss them — leaving the frozen
# Linux binary with no keyring backend and silently falling back to plaintext for
# the OpenAI key / session-only for every other provider (utils/settings.py).
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
    # header (CustomTkinter's own PIL import is in a try/except, so nothing
    # else failed loudly).
    "pandas",
    "IPython",
    "notebook",
    "jupyter",
    "tkinter.test",
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
datas = [("data", "data"), ("public", "public")] + collect_data_files("customtkinter")

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
