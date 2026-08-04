"""PySide6 (Qt) GUI layer — the only GUI (issue #44).

It replaced a CustomTkinter tree of the same name in the 2026-08-04 cut-over,
and three whole classes of bug went with it: text shaping (Qt lays out Arabic
with HarfBuzz on every platform, so ``arabic-reshaper``/``python-bidi`` and
their per-platform branches are gone), manual line wrapping, and per-widget
theme registries.

Nothing is imported here at package level. ``control_state``, ``device_list``
and ``subtitle_text`` are deliberately free of any Qt import — they are plain
logic, and a test asserts the windows can be imported without ``tkinter``
appearing in ``sys.modules``.
"""
