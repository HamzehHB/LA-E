PERSONAL_VAULT = "personal_vault"
EXAMPLES = "examples"

OPERATING_MODES = (PERSONAL_VAULT, EXAMPLES)


def normalize_mode(value) -> str:
    """Return the canonical mode name or raise ``ValueError``."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if text in OPERATING_MODES:
        return text
    raise ValueError("operating mode must be one of: personal_vault, examples")


def resolve_vault_for_mode(mode, configured_vault="",
                           explicit_vault="") -> str:
    """Return the vault root authorized for ``mode``.

    Personal-vault mode returns the explicitly supplied vault when given,
    otherwise the configured ``obsidian.vault``. Examples mode never
    returns a vault: it raises ``ValueError`` when any vault path is
    supplied (fail-closed isolation), otherwise returns ``""``.
    """
    canonical = normalize_mode(mode)
    if canonical == EXAMPLES:
        for candidate in (explicit_vault, configured_vault):
            if isinstance(candidate, str) and candidate.strip():
                raise ValueError(
                    "examples mode must not access the personal vault")
        return ""
    explicit = explicit_vault.strip() if isinstance(explicit_vault, str) else ""
    if explicit:
        return explicit
    configured = (configured_vault.strip()
                  if isinstance(configured_vault, str) else "")
    return configured


__all__ = ("PERSONAL_VAULT", "EXAMPLES", "OPERATING_MODES",
           "normalize_mode", "resolve_vault_for_mode")
