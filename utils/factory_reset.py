"""Remove everything MinbarLive keeps on this machine — the "I am leaving" path.

The counterpart to deleting the binary, and deliberately NOT the same thing:
deleting the ``.exe`` / ``.app`` / ``.AppImage`` leaves the settings, the
session history and the keychain entries exactly where they are, which is what
somebody moving to a newer build wants and precisely what somebody uninstalling
does not.

Two stores, in this order:

1. **the OS keychain entries**, one per provider in ``providers.KEYED_PROVIDERS``
   — the only thing that survives deleting the folder, and the only thing a
   user cannot reasonably find and remove by hand;
2. **the app-data directory** (``settings.json``, ``history/``, ``logs/``,
   ``recordings/``, ``batch/``), removed whole.

Keychain first on purpose. A reset that drops the keys and then fails on the
folder leaves a recoverable mess — the folder is right there and the user can
delete it. The reverse order fails the other way: credentials left in the
keychain with no settings, no window and no GUI left to remove them with.

**Nothing here may write to the app-data directory, and one thing nearly does.**
``log()`` appends to a file it does not create
(``utils.logging._write_to_file`` opens in ``"a"`` mode and swallows the error),
so a line logged after step 2 reaches the panel's log view and is silently
dropped on disk — which is what keeps "the folder is gone" true.
``save_settings`` does create it, and would resurrect a ``settings.json`` with
``onboarding_completed`` still set: the next launch would then skip the wizard
and the reset would have deleted the user's history for nothing. That is why a
successful reset calls :func:`utils.settings.block_writes`.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from utils.app_paths import get_app_data_dir
from utils.logging import log
from utils.settings import block_writes


@dataclass
class ResetResult:
    """What the reset removed, and what it could not.

    Reported to the user rather than reduced to a bool: "everything is gone"
    and "the keys are gone but the folder is still there" need different
    actions from them, and a destructive operation owes them the difference.
    """

    data_dir: Path
    data_dir_removed: bool = False
    keys_removed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.data_dir_removed and not self.errors


def _clear_keychain(result: ResetResult) -> None:
    from providers import KEYED_PROVIDERS, clear_api_key, has_configured_key

    for provider in KEYED_PROVIDERS:
        # has_configured_key, not has_usable_key: the latter also counts an
        # ambient OPENAI_API_KEY/GEMINI_API_KEY left in the environment by some
        # unrelated tool, and this app neither stored that nor can delete it —
        # reporting it as "removed" would be a lie.
        if not has_configured_key(provider):
            continue
        try:
            clear_api_key(provider)
        except Exception as exc:  # noqa: BLE001 - one provider must not stop the rest
            result.errors.append(f"{provider}: {exc}")
            continue
        # Verified, not assumed. clear_api_key returns None and swallows the
        # keychain's own refusal (delete_api_key_from_keyring logs it and
        # returns False), so looking again is the only trustworthy answer.
        if has_configured_key(provider):
            result.errors.append(f"{provider}: the key is still in the keychain")
        else:
            result.keys_removed.append(provider)


def _remove_data_dir(result: ResetResult) -> None:
    if not result.data_dir.exists():
        result.data_dir_removed = True
        return
    try:
        shutil.rmtree(result.data_dir)
    except OSError as exc:
        result.errors.append(f"{result.data_dir}: {exc}")
    # Read back rather than inferring from the absence of an exception: a
    # partial rmtree raises on the first file it cannot remove and leaves the
    # rest of the tree standing, and "removed" has to mean the folder is gone.
    result.data_dir_removed = not result.data_dir.exists()


def factory_reset() -> ResetResult:
    """Delete the keychain entries, then the app-data directory.

    Never raises: every failure is collected in the result so the caller can
    name it. The caller is expected to have stopped any running session first
    — a live pipeline holds the recordings directory open and keeps writing
    history — and to quit the app afterwards, because what is left running is
    an install whose storage no longer exists.
    """
    result = ResetResult(data_dir=get_app_data_dir())
    _clear_keychain(result)
    _remove_data_dir(result)
    if result.data_dir_removed:
        # Before the log line below, so nothing between here and process exit
        # can put a settings.json back. See the module docstring.
        block_writes()
    log(
        f"Factory reset: data dir removed={result.data_dir_removed}, "
        f"keys removed={result.keys_removed or 'none'}, "
        f"errors={result.errors or 'none'}",
        level="INFO",
    )
    return result
