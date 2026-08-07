"""What ``MinbarLive.spec`` collects into the frozen build (issue #48).

The spec drops every dependency's own test suite from ``hiddenimports`` —
223 modules, ~195 of them ``google.genai.tests.*``. The risk that change
carries is not subtle and not theoretical: **a filter that reaches one module
too far removes a lazily imported provider SDK, and nothing notices until
somebody selects that provider in a shipped build.** Every call site imports
inside a function, so it cannot fail at startup, and a smoke launch cannot
see it either.

So the filter is asserted here rather than reasoned about. These execute the
REAL spec — with ``Analysis``/``PYZ``/``EXE``/``BUNDLE`` stubbed, the way the
3468-entry figure in #48 was originally measured — instead of re-implementing
its rules, which would only test a copy of them.

Costs one spec evaluation (~10 s), shared across the module.
"""

import ast
import importlib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

SPEC = Path(__file__).parent.parent / "MinbarLive.spec"

# Imported inside a function by the code that needs them, so a missing one is
# invisible until that feature is used. providers/<id>/ is the only place each
# SDK is touched; the rest are named in the spec's own comments.
LAZY_IMPORTS = [
    "google.genai",  # providers/gemini
    "anthropic",  # providers/anthropic
    "deepgram",  # providers/deepgram
    "openai",  # providers/openai
    "webrtcvad",  # audio/vad.py
    "soundcard",  # audio/loopback.py, gui/device_list.py
    "keyring",  # utils/keyring_storage.py
    "screeninfo",
    "sounddevice",
    "websockets",  # streaming transport
]


@pytest.fixture(scope="module")
def analysis_kwargs() -> dict:
    """Everything the real spec hands ``Analysis`` on this machine."""
    captured: dict[str, dict] = {}

    def _analysis(*_args, **kwargs):
        captured["kwargs"] = dict(kwargs)
        raise SystemExit(0)  # nothing after Analysis matters here

    class _Stub:
        def __init__(self, *a, **kw):
            self.pure = self.datas = self.binaries = self.scripts = []

        def __call__(self, *a, **kw):
            return self

    namespace = {
        "__file__": str(SPEC),
        "__name__": "__main__",
        "Analysis": _analysis,
        "PYZ": _Stub(),
        "EXE": _Stub(),
        "COLLECT": _Stub(),
        "BUNDLE": _Stub(),
        "SPEC": str(SPEC),
        "SPECPATH": str(SPEC.parent),
        "DISTPATH": str(SPEC.parent / "dist"),
        "workpath": str(SPEC.parent / "build"),
    }
    try:
        exec(compile(SPEC.read_text(encoding="utf-8"), str(SPEC), "exec"), namespace)
    except SystemExit:
        pass
    kwargs = captured.get("kwargs")
    assert kwargs, "the spec never reached Analysis — the harness is broken"
    return kwargs


@pytest.fixture(scope="module")
def hiddenimports(analysis_kwargs) -> list[str]:
    """``hiddenimports`` as the real spec resolves it on this machine."""
    names = list(analysis_kwargs.get("hiddenimports", []))
    assert names, "the spec collected no hiddenimports at all"
    return names


def _importable(package: str) -> bool:
    """Whether this machine can import it at all.

    ``collect_submodules`` IMPORTS a package to walk it, and when that fails it
    only WARNS and returns nothing. So on a machine missing the system library
    behind a binding the package is absent from the bundle for an environmental
    reason rather than because the filter dropped it — `ubuntu-latest` has no
    ``libpulse.so``, so ``soundcard`` collects as empty there while the real
    Linux builder, which installs it, collects it in full.

    That is the PyInstaller builder trap in miniature, and the reason
    release.yml installs those libraries *before* PyInstaller runs. Asserting
    on a package this machine cannot import would be asserting on the runner.
    """
    try:
        importlib.import_module(package)
    except Exception:  # noqa: BLE001 - any failure means it cannot be collected
        return False
    return True


def test_every_lazily_imported_package_is_bundled(hiddenimports):
    """The whole point. Each of these is imported inside a function, so a
    dropped one ships silently and fails the first time a user picks that
    provider."""
    checked = [pkg for pkg in LAZY_IMPORTS if _importable(pkg)]
    missing = [pkg for pkg in checked if pkg not in hiddenimports]
    assert not missing, f"not collected, so a lazy import would fail: {missing}"
    # Without a floor this degenerates silently: a runner where nothing imports
    # would check nothing and still pass green.
    assert len(checked) >= len(LAZY_IMPORTS) - 2, (
        f"only {len(checked)} of {len(LAZY_IMPORTS)} packages were importable "
        f"here — this check proved almost nothing. Missing: "
        f"{sorted(set(LAZY_IMPORTS) - set(checked))}"
    )


def test_no_test_code_is_bundled(hiddenimports):
    """The #48 change itself. `pytest` is in the spec's `excludes`, so these
    shipped in a state where they could not have run even if something tried."""
    leftovers = [
        name
        for name in hiddenimports
        if {"tests", "testing", "conftest"} & set(name.split("."))
    ]
    assert not leftovers, f"test modules still bundled: {leftovers[:10]}"


def test_the_filter_drops_test_code_and_nothing_else(hiddenimports):
    """Compares against an UNFILTERED collection of the same packages: every
    module the spec left out has to be test code. This is the check that would
    catch a filter reaching one module too far — the risk #48 named."""
    from PyInstaller.utils.hooks import collect_submodules

    bundled = set(hiddenimports)
    for package in ("google.genai", "scipy.io", "keyring", "soundcard"):
        if not _importable(package):
            continue  # collects as empty here; see _importable
        dropped = set(collect_submodules(package)) - bundled
        not_test_code = [
            name
            for name in dropped
            if not ({"tests", "testing", "conftest"} & set(name.split(".")))
            and not name.rsplit(".", 1)[-1].startswith(("test_", "_test_"))
        ]
        assert not not_test_code, (
            f"{package}: the filter dropped non-test modules {not_test_code[:10]}"
        )


def test_the_filter_actually_removes_something(hiddenimports):
    """Guards the three above from passing because the filter is a no-op — if
    it silently stopped matching, they would all still be green."""
    from PyInstaller.utils.hooks import collect_submodules

    unfiltered = set(collect_submodules("google.genai"))
    dropped = unfiltered - set(hiddenimports)
    assert len(dropped) > 50, (
        f"expected google.genai's test suite (~195 modules) to be dropped, got "
        f"{len(dropped)} — the filter is not doing anything"
    )


def test_the_spec_matches_on_path_components_not_substrings():
    """`a.contest.b` and `latest_thing` must survive. Read off the spec's own
    source so it cannot drift from what ships."""
    source = SPEC.read_text(encoding="utf-8")
    match = re.search(r"def _not_test_code.*?(?=\ndef )", source, re.S)
    assert match, "_not_test_code is gone from the spec"
    namespace: dict = {}
    exec(compile(ast.parse(match.group(0)), "<spec>", "exec"), namespace)
    not_test_code = namespace["_not_test_code"]

    assert not_test_code("google.genai.contests.helper")
    assert not_test_code("pkg.latest")
    assert not_test_code("pkg.attesting")
    assert not not_test_code("google.genai.tests.afc")
    assert not not_test_code("scipy.io._test_fortran")
    assert not not_test_code("soundcard.__pyinstaller.conftest")


# ── the Qt payload filter (build_qt_excludes) ────────────────────────────────
#
# Two layers, and both have to be wired for either to matter: `excludes` keeps
# the Python modules out, the a.binaries filter keeps Qt's native libraries
# out. The module sat in the repo unreferenced by the spec for a full release
# cycle, which is what these first two checks exist to prevent recurring.


def test_the_qt_module_excludes_reach_the_spec(analysis_kwargs):
    """The wiring itself. Without this the module is dead weight and the DLLs
    ship regardless of what it says."""
    from build_qt_excludes import QT_MODULE_EXCLUDES

    excludes = set(analysis_kwargs.get("excludes", []))
    missing = [name for name in QT_MODULE_EXCLUDES if name not in excludes]
    assert not missing, f"QT_MODULE_EXCLUDES not applied by the spec: {missing[:5]}"


def test_the_spec_filters_the_binaries_toc():
    """The other half. Excluding PySide6.QtQuick does NOT remove Qt6Quick.dll —
    the hook collects it as a binary, so the TOC has to be filtered too."""
    source = SPEC.read_text(encoding="utf-8")
    assert "drop_unused_qt_binaries(a.binaries)" in source, (
        "the spec excludes the Qt modules but never filters a.binaries — the "
        "native libraries would still ship"
    )


def test_the_qt_binary_filter_keeps_what_qtwidgets_needs():
    """The dangerous direction. Dropping a library QtWidgets links against
    produces a build that fails at launch, and no unit test of ours runs the
    frozen binary — so the naming rules are pinned here instead."""
    from build_qt_excludes import drop_unused_qt_binaries

    required = [
        ("Qt6Core.dll", "."),
        ("Qt6Gui.dll", "."),
        ("Qt6Widgets.dll", "."),
        ("Qt6DBus.dll", "."),
        ("Qt6Svg.dll", "."),
        ("libQt6Core.so.6", "."),
        ("libQt6XcbQpa.so.6", "."),
        ("PySide6/plugins/platforms/qwindows.dll", "PySide6/plugins/platforms"),
        ("PySide6/plugins/platforms/libqxcb.so", "PySide6/plugins/platforms"),
        ("PySide6/plugins/imageformats/qjpeg.dll", "PySide6/plugins/imageformats"),
        # Not Qt at all — the filter must not reach past its own payload.
        ("libportaudio.so.2", "."),
        ("_sounddevice_data/portaudio-binaries/libportaudio64bit.dll", "."),
    ]
    kept = {entry[0] for entry in drop_unused_qt_binaries(required)}
    dropped = [name for name, _ in required if name not in kept]
    assert not dropped, f"the filter removed libraries the app needs: {dropped}"


def test_the_qt_binary_filter_drops_the_unused_payload():
    """The check that would fail if the filter silently stopped matching — a Qt
    rename, or someone editing the fragment list. Without it the two above pass
    happily against a filter that drops nothing."""
    from build_qt_excludes import drop_unused_qt_binaries

    unused = [
        ("Qt6Quick.dll", "."),
        ("Qt6Qml.dll", "."),
        ("Qt6QmlModels.dll", "."),
        ("Qt6Pdf.dll", "."),
        ("Qt6WebEngineCore.dll", "."),
        ("opengl32sw.dll", "."),
        ("libGLESv2.dll", "."),
        ("libQt6Quick.so.6", "."),
        ("PySide6/qml/QtQml/libqmlplugin.so", "PySide6/qml/QtQml"),
    ]
    survivors = [entry[0] for entry in drop_unused_qt_binaries(unused)]
    assert not survivors, f"unused Qt payload still bundled: {survivors}"
