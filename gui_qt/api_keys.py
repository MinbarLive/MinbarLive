"""API key entry for the Qt GUI.

Mirrors ``utils/api_key_manager.py`` but built on Qt widgets. Storage is
unchanged and goes through ``providers.save_api_key``: the OS keychain, or
session-only if no keychain exists. Keys are NEVER written to disk — the
plaintext settings.json fallback was removed in PR #43.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from providers import (
    PROVIDER_CHOICES,
    get_key_placeholder,
    get_stored_api_key,
    has_usable_key,
    save_api_key,
)


def activate_stored_keys() -> None:
    """Load the stored OpenAI key into its client singleton.

    Required at startup: the OpenAI client holds its key in module state and
    raises "OpenAI API key is not configured." until ``set_api_key`` is called
    — a stored key is not enough, which is why ``has_usable_key`` can be True
    while ``get_client()`` still fails. The Gemini, Anthropic and Deepgram
    clients resolve their own key lazily on first use and need nothing here.

    Done even when another provider is active, because the OpenAI key still
    serves the Quran RAG query embeddings.
    """
    from providers.openai.client import set_api_key

    key = (get_stored_api_key("openai") or "").strip()
    if key:
        set_api_key(key)


def provider_label(provider_id: str) -> str:
    """Human-readable provider name, e.g. "openai" -> "OpenAI"."""
    for name, pid in PROVIDER_CHOICES:
        if pid == provider_id:
            return name
    return provider_id


class ApiKeyDialog(QDialog):
    def __init__(self, provider_id: str, texts: dict, parent=None):
        super().__init__(parent)
        self._provider = provider_id
        label = provider_label(provider_id)
        self.setWindowTitle(f"{label} API Key")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        prompt = texts.get("dlg_key_prompt", "Enter your {provider} API key:")
        layout.addWidget(QLabel(prompt.format(provider=label)))

        self.edit = QLineEdit()
        # Masked like a password field: the key is a secret and may be entered
        # in front of a congregation.
        self.edit.setEchoMode(QLineEdit.Password)
        self.edit.setPlaceholderText(get_key_placeholder(provider_id))
        layout.addWidget(self.edit)

        note = QLabel(
            texts.get(
                "dlg_key_storage_note",
                "Stored in your operating system's keychain. Never written to disk.",
            )
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def key(self) -> str:
        return self.edit.text().strip()


def ensure_keys(provider_ids: list[str], texts: dict, parent=None) -> bool:
    """Prompt for any provider in ``provider_ids`` without a usable key.

    Returns False if the user cancelled, so the caller can abort the start
    instead of failing later inside the pipeline.
    """
    for provider_id in provider_ids:
        if has_usable_key(provider_id):
            continue
        dialog = ApiKeyDialog(provider_id, texts, parent)
        if dialog.exec() != QDialog.Accepted:
            return False
        key = dialog.key()
        if not key:
            return False
        stored = save_api_key(provider_id, key)
        if not stored:
            # No keychain available: the key works now but is gone on restart.
            QMessageBox.warning(
                parent,
                "MinbarLive",
                texts.get(
                    "dlg_key_session_only_warning",
                    "No system keychain was available, so this key is only "
                    "active until you close MinbarLive.",
                ),
            )
    return True
