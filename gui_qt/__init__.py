"""PySide6 (Qt) GUI layer — the migration target for issue #44.

This package grows alongside the CustomTkinter ``gui/`` package while the port
is in progress; ``gui/`` is deleted in the final cut-over. Tk and Qt each want
their own mainloop and cannot share a process, so exactly one of the two trees
is selected at launch (see ``main.py``) — they are never both live.

Nothing may be imported here at package level: modules such as ``palette`` are
deliberately free of any Qt import so the Tk tree can share them during the
migration without pulling PySide6 into a Tk process.
"""
