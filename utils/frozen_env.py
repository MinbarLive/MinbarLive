"""Environment for launching a *system* executable from inside the frozen app.

PyInstaller's onefile bootloader prepends its extraction directory to
``LD_LIBRARY_PATH`` (Linux) / ``DYLD_LIBRARY_PATH`` (macOS) so the bundled
Python can find its own shared objects. A system binary spawned from here
inherits that variable and then loads *our* libraries instead of the system's:
a bundled ``libstdc++.so.6`` older than the one the child's own dependencies
need fails with, e.g.,

    ffmpeg: /tmp/_MEIxxxx/libstdc++.so.6: version `GLIBCXX_3.4.32' not found
    (required by /usr/lib/libSPIRV-Tools.so)

The same trap catches any child launched from the frozen app — ffmpeg, but
equally ``xdg-open``/``open`` and the file manager or browser they hand off to.

The bootloader saves each original value as ``<VAR>_ORIG``; restore it (or drop
the injected variable when there was none) so the child sees the library
environment it would outside the bundle. The *parent* environment is left
untouched — the app still needs its own ``LD_LIBRARY_PATH`` for Qt to dlopen
plugins lazily — so this is only ever handed to a child via ``subprocess``'s
``env=``.
"""

from __future__ import annotations

import os

from config import IS_FROZEN


def external_process_env() -> dict[str, str] | None:
    """Env dict for a child system process, or None to inherit unchanged.

    None when not frozen: there is no injected library path to undo, and
    returning the real environment verbatim would be the same thing.
    """
    if not IS_FROZEN:
        return None
    env = os.environ.copy()
    for var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        original = env.pop(f"{var}_ORIG", None)
        if original is not None:
            env[var] = original
        else:
            env.pop(var, None)
    return env
