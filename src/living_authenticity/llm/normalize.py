"""Provider-neutral response normalization for the AI synthesis capability.

This is the one shared boundary where a raw model string becomes a
candidate JSON document. It is deliberately minimal and deterministic:

* it removes at most one *outer* Markdown code fence (````` ```json ````
  or ````` ``` ````) together with surrounding whitespace — an
  unambiguous, purely mechanical wrapper that many chat-tuned models add
  even when told not to;
* it never searches for JSON inside prose, never repairs braces or
  quotes, never trims a partial object, and never invents content.

If a response is prose with an embedded JSON fragment, it is left as-is
and the strict parser rejects it. Normalization is not validation: every
rule of the result contract is still enforced afterwards.
"""
_FENCE = "```"


def normalize_model_text(text: str) -> str:
    """Return ``text`` without a single outer Markdown code fence.

    Anything that is not exactly one surrounding fenced block is returned
    unchanged (only outer whitespace is trimmed), so the caller's strict
    parser decides whether the result is acceptable.
    """
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped.startswith(_FENCE):
        return stripped
    first_line_end = stripped.find("\n")
    if first_line_end == -1:
        return stripped
    opening = stripped[:first_line_end].strip()
    # Only a bare fence or a language-tagged fence counts as a fence.
    tag = opening[len(_FENCE):].strip()
    if tag and not tag.isalnum():
        return stripped
    body = stripped[first_line_end + 1:]
    closing = body.rfind(_FENCE)
    if closing == -1:
        return stripped
    if body[closing:].strip() != _FENCE:
        return stripped
    inner = body[:closing].strip()
    # A fence with trailing prose after it is not a pure wrapper.
    if _FENCE in inner:
        return stripped
    return inner


__all__ = ("normalize_model_text",)
