"""Qt exclusion lists for PyInstaller (issue #44).

Build-time only — never imported at runtime. A PyInstaller spec uses it as::

    import sys; sys.path.insert(0, SPECPATH)
    from build_qt_excludes import QT_MODULE_EXCLUDES, drop_unused_qt_binaries
    ...
    a.binaries = drop_unused_qt_binaries(a.binaries)

Two layers are needed, and this is the part that is easy to get wrong:
**Python-level ``excludes`` do NOT remove Qt's native libraries.** PyInstaller's
PySide6 hook collects Qt DLLs as *binaries*, so excluding ``PySide6.QtQuick``
stops the Python module being bundled while ``Qt6Quick.dll`` still ships.
Measured on a minimal QtWidgets app: excludes alone left Qt6Quick, Qt6Qml and
Qt6Pdf (15.8 MB) plus opengl32sw.dll (19.7 MB) in the bundle.

MinbarLive's Qt tree imports only QtCore, QtGui and QtWidgets — no QML, no
WebEngine, no multimedia (audio goes through sounddevice, not Qt).
"""

from __future__ import annotations

# Python modules that must not be bundled.
QT_MODULE_EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQml",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtSpatialAudio",
    "PySide6.QtSql",
    "PySide6.QtNetworkAuth",
    "PySide6.QtBluetooth",
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtNfc",
    "PySide6.QtSerialPort",
    "PySide6.QtSensors",
    "PySide6.QtTest",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
]

# Native libraries to drop, matched case-insensitively against the file name.
# Anything Qt6Core/Qt6Gui/Qt6Widgets genuinely need must NOT appear here.
_BINARY_NAME_FRAGMENTS = (
    "qt6webengine",
    "qt6quick",
    "qt6qml",
    "qt6qmlmodels",
    "qt6qmlmeta",
    "qt6qmlworkerscript",
    "qt63d",
    "qt6charts",
    "qt6datavisualization",
    "qt6multimedia",
    "qt6spatialaudio",
    "qt6sql",
    "qt6bluetooth",
    "qt6positioning",
    "qt6location",
    "qt6nfc",
    "qt6serialport",
    "qt6sensors",
    "qt6test",
    "qt6designer",
    "qt6uitools",
    "qt6help",
    "qt6pdf",
    "qt6remoteobjects",
    "qt6scxml",
    "qt6statemachine",
    "qt6webchannel",
    "qt6websockets",
    "qt6shadertools",
    "qt6networkauth",
    # Mesa's software OpenGL fallback (~20 MB). Qt uses it only when no GPU
    # driver can provide OpenGL; MinbarLive's overlay is plain QPainter 2D
    # rendering, which falls back to software rasterisation without it.
    "opengl32sw",
    # ANGLE (GL-on-D3D) — same reasoning: no GL surfaces in this app.
    "libglesv2",
    "libegl",
)

# Qt plugin directories that are pure QML/3D payload.
_PLUGIN_DIR_FRAGMENTS = (
    "pyside6/qml",
    "pyside6\\qml",
    "qt6/qml",
    "qt6\\qml",
)


def _is_droppable(dest: str) -> bool:
    lowered = dest.replace("\\", "/").lower()
    name = lowered.rsplit("/", 1)[-1]
    if any(fragment in name for fragment in _BINARY_NAME_FRAGMENTS):
        return True
    return any(fragment in lowered for fragment in _PLUGIN_DIR_FRAGMENTS)


def drop_unused_qt_binaries(binaries):
    """Filter a PyInstaller ``a.binaries`` TOC, removing unused Qt payload."""
    return [entry for entry in binaries if not _is_droppable(entry[0])]


def report(binaries) -> tuple[int, int]:
    """(kept, dropped) counts — useful to print from a spec during a build."""
    dropped = sum(1 for entry in binaries if _is_droppable(entry[0]))
    return len(binaries) - dropped, dropped
