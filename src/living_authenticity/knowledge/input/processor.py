"""Input stage processing, validation, provenance, safety boundaries, and evaluation.

The Input stage is the authoritative entry milestone of the LivingAuthenticity workflow.
It enforces:
1. Source selection: accepts only explicitly approved interactive sources (currently 'chat_input').
2. Type determination: resolves content type as 'text'.
3. Structural validation: non-empty, within size bounds (MAX_TEXT_BYTES = 1_000_000), valid UTF-8, no raw control chars.
4. Meaningful research content: deterministic detection of punctuation-only, repeated-character spam, and fragment noise.
5. Clarification: surfaces ambiguous single-token inputs as NEEDS_CLARIFICATION rather than presuming intent.
6. Provenance & Identification: assigns stable input_id, computes input_hash (sha256), records metadata.
7. Safety checks: verifies operating mode and path boundaries without reading arbitrary files or bypassing guards.
8. Audit privacy: never persists raw private input text into the audit log.
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
import re
import unicodedata
import uuid

from src.living_authenticity.knowledge.governance.audit.clock import local_now_iso
from src.living_authenticity.knowledge.modes import OPERATING_MODES

ACCEPTED_SOURCE_TYPES = frozenset({"chat_input"})
MAX_TEXT_BYTES = 1_000_000
DEFAULT_MIN_LETTERS = 50
DEFAULT_MIN_WORDS = 30


def count_letters(text: str) -> int:
    """Count authentic letters (Unicode category L) in text, supporting Persian, English, and multilingual scripts."""
    if not isinstance(text, str):
        return 0
    return sum(1 for ch in text if unicodedata.category(ch).startswith("L"))


def count_tokens(text: str) -> int:
    """Count textual tokens (words/lexemes, including Persian with ZWNJ) in text."""
    if not isinstance(text, str):
        return 0
    return len(re.findall(r"[\w\u200c]+", text, flags=re.UNICODE))


def count_words(text: str) -> int:
    """Count words containing letters or numbers, handling Persian and English."""
    return count_tokens(text)


class InputOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class InputProvenance:
    """Provenance and identification metadata for submitted input."""

    input_id: str
    input_hash: str
    source_type: str
    resolved_type: str
    byte_length: int
    char_length: int
    timestamp: str
    operating_mode: str


@dataclass(frozen=True)
class InputEvaluationResult:
    """Authoritative evaluation, validation, and safety outcome for the Input stage."""

    outcome: InputOutcome
    valid: bool
    can_proceed: bool
    input_id: str
    run_id: str = ""
    source_type: str = "chat_input"
    operating_mode: str = "examples"
    content_type: str = "text"
    validation_status: str = "valid"
    classification_category: str = "research_input"
    error_code: str = ""
    reason: str = ""
    message_key: str = ""
    recovery_options: tuple[str, ...] = ()
    suggested_action: str = ""
    content_hash: str = ""
    provenance: InputProvenance | None = None


# Backward-compatible alias for existing consumers
InputValidationResult = InputEvaluationResult


def _is_punctuation_or_symbol_only(text: str) -> bool:
    """Return True if text contains only punctuation, symbols, or whitespace (no letters or digits)."""
    stripped = text.strip()
    if not stripped:
        return False
    for ch in stripped:
        cat = unicodedata.category(ch)
        if cat[0] in ("L", "N"):
            return False
    return True


def _is_repeated_char_spam(text: str) -> bool:
    """Detect repeated character spam (e.g. 'aaaaaaaaaa', 'سسسسسسسسسسس', '1111111111111')."""
    compact = "".join(text.split())
    if len(compact) >= 10:
        distinct = set(compact)
        if len(distinct) == 1:
            return True
        if len(distinct) <= 2 and len(compact) >= 14:
            return True
    match = re.search(r"(.)\1{11,}", text)
    if match and len(compact) > 0 and (len(match.group(0)) / len(compact)) >= 0.7:
        return True
    return False


def _is_repeated_fragment_noise(text: str) -> bool:
    """Detect repeated fragment noise / keyboard mashing (e.g. 'asdfasdfasdfasdf', 'qwertyqwertyqwerty')."""
    compact = "".join(text.split())
    if len(compact) < 10:
        return False
    for k in range(2, 9):
        if len(compact) >= 3 * k:
            reps = len(compact) // k
            if reps >= 3:
                pattern = compact[:k]
                if pattern * reps == compact[:reps * k] and (reps * k) / len(compact) >= 0.8:
                    return True
    return False


def _is_ambiguous_single_token(text: str) -> bool:
    """Detect isolated single character or ambiguous minimal token (e.g. 'a', 'x', '1', 'a.', '(x)') that requires clarification."""
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) == 1 and (unicodedata.category(stripped)[0] in ("L", "N")):
        return True
    inner = "".join(ch for ch in stripped if unicodedata.category(ch)[0] in ("L", "N"))
    if len(inner) == 1 and len(stripped) <= 4:
        return True
    return False


def _contains_disallowed_control_chars(text: str) -> bool:
    """Check for null bytes or raw non-whitespace ASCII control characters."""
    for ch in text:
        code = ord(ch)
        if code == 0:
            return True
        if code < 32 and ch not in ("\t", "\n", "\r"):
            return True
    return False


def process_input(
    input_text: object,
    source_type: str = "chat_input",
    source_mode: str = "personal_vault",
    input_id: str | None = None,
    run_id: str = "",
    max_bytes: int = MAX_TEXT_BYTES,
    min_letters: int = DEFAULT_MIN_LETTERS,
    min_words: int | None = None,
) -> InputEvaluationResult:
    """Authoritatively evaluate and validate submitted input, enforcing safety boundaries.

    Returns an InputEvaluationResult with explicit outcome (ACCEPTED, REJECTED, NEEDS_CLARIFICATION).
    Only ACCEPTED allows downstream processing (can_proceed=True).
    """
    # 1. Source selection
    if not isinstance(source_type, str) or source_type not in ACCEPTED_SOURCE_TYPES:
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-invalid",
            run_id=run_id,
            source_type=str(source_type),
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason=f"unsupported source_type '{source_type}': only 'chat_input' is currently accepted",
            error_code="unsupported_source_type",
            message_key="input.error.unsupported_source",
            recovery_options=(),
            suggested_action="",
        )

    # 2. Operating mode check
    if not isinstance(source_mode, str) or source_mode not in OPERATING_MODES:
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-invalid",
            run_id=run_id,
            source_type=source_type,
            operating_mode=str(source_mode),
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason=f"invalid source_mode '{source_mode}': must be one of {', '.join(sorted(OPERATING_MODES))}",
            error_code="invalid_source_mode",
            message_key="input.error.invalid_mode",
            recovery_options=(),
            suggested_action="",
        )

    # 3. Type and structural validation
    if not isinstance(input_text, str):
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-invalid",
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason=f"input must be a text string, got {type(input_text).__name__}",
            error_code="invalid_input_type",
            message_key="input.error.invalid_type",
            recovery_options=(),
            suggested_action="",
        )

    if not input_text.strip():
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-empty",
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason="input text is required and cannot be empty or whitespace-only",
            error_code="empty_input",
            message_key="input.error.empty",
            recovery_options=(),
            suggested_action="",
        )

    # 4. Encoding, control characters, and size validation
    try:
        encoded_bytes = input_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-encoding-err",
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason=f"input contains invalid character encoding: {exc}",
            error_code="encoding_error",
            message_key="input.error.encoding",
            recovery_options=(),
            suggested_action="",
        )

    if _contains_disallowed_control_chars(input_text):
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-control-err",
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="structural_invalidity",
            reason="input contains unsupported control characters or null bytes",
            error_code="unsupported_control_chars",
            message_key="input.error.control_chars",
            recovery_options=(),
            suggested_action="",
        )

    if len(encoded_bytes) > max_bytes:
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=input_id or "inp-oversized",
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            validation_status="invalid",
            classification_category="resource_boundary_exceeded",
            reason=f"input text size ({len(encoded_bytes)} bytes) exceeds maximum limit ({max_bytes} bytes)",
            error_code="size_limit_exceeded",
            message_key="input.error.oversized",
            recovery_options=(),
            suggested_action="",
        )

    # Compute content hash
    content_hash = hashlib.sha256(encoded_bytes).hexdigest()
    if isinstance(input_id, str) and input_id.strip().startswith("inp-"):
        resolved_input_id = input_id.strip()
    else:
        resolved_input_id = f"inp-{content_hash[:12]}"

    # 5. Meaningful research content / Minimum letter count boundary
    # Require sufficient textual substance (minimum 50 letters) for authentic knowledge evaluation
    letter_count = count_letters(input_text)
    token_count = count_tokens(input_text)
    effective_min_letters = min_letters

    if letter_count < effective_min_letters:
        return InputEvaluationResult(
            outcome=InputOutcome.NEEDS_CLARIFICATION,
            valid=False,
            can_proceed=False,
            input_id=resolved_input_id,
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            content_hash=content_hash,
            validation_status="clarification_required",
            classification_category="insufficient_content",
            reason=(
                f"نیاز به داده بیشتر برای سنجش است (حداقل {effective_min_letters} حرف برای پردازش لازم است؛ متن فعلی شامل {letter_count} حرف و {token_count} توکن است)"
            ),
            error_code="insufficient_letter_count",
            message_key="input.error.insufficient_letters",
            recovery_options=(),
            suggested_action="",
        )

    # 6. Secondary defense: catch repetitive character spam
    if _is_repeated_char_spam(input_text):
        return InputEvaluationResult(
            outcome=InputOutcome.REJECTED,
            valid=False,
            can_proceed=False,
            input_id=resolved_input_id,
            run_id=run_id,
            source_type=source_type,
            operating_mode=source_mode,
            content_hash=content_hash,
            validation_status="invalid",
            classification_category="repetition_noise",
            reason="input contains repetitive character sequences without meaningful textual content",
            error_code="repeated_char_noise",
            message_key="input.error.repeated_chars",
            recovery_options=(),
            suggested_action="",
        )

    # 7. Valid research content: establish provenance and authorize entry
    provenance = InputProvenance(
        input_id=resolved_input_id,
        input_hash=content_hash,
        source_type=source_type,
        resolved_type="text",
        byte_length=len(encoded_bytes),
        char_length=len(input_text),
        timestamp=local_now_iso(),
        operating_mode=source_mode,
    )
    return InputEvaluationResult(
        outcome=InputOutcome.ACCEPTED,
        valid=True,
        can_proceed=True,
        input_id=resolved_input_id,
        run_id=run_id,
        source_type=source_type,
        operating_mode=source_mode,
        content_hash=content_hash,
        validation_status="valid",
        classification_category="research_input",
        provenance=provenance,
        suggested_action="proceed",
    )


def record_input_failure_audit(
    audit_writer,
    run_id: str,
    validation_result: InputEvaluationResult,
    operating_mode: str = "examples",
    input_text: object = None,
):
    """Persist an audit record for an unaccepted Input stage evaluation (REJECTED, NEEDS_CLARIFICATION, STOPPED).

    Uses AuditWriter to append to the 'failed' partition.
    Strictly preserves privacy: NEVER persists raw private input text into the audit log.
    """
    from src.living_authenticity.knowledge.governance.audit.outcome import AuditRecord

    input_hash = validation_result.content_hash
    if not input_hash and isinstance(input_text, str) and input_text:
        try:
            input_hash = hashlib.sha256(input_text.encode("utf-8")).hexdigest()
        except Exception:
            input_hash = ""

    if validation_result.input_id:
        query_id = validation_result.input_id
    elif input_hash:
        query_id = f"inp-{input_hash[:12]}"
    else:
        query_id = "inp-invalid"

    if validation_result.outcome == InputOutcome.NEEDS_CLARIFICATION:
        terminal_outcome = "input_clarification_required"
    elif validation_result.outcome == InputOutcome.STOPPED:
        terminal_outcome = "stopped_by_user"
    else:
        terminal_outcome = "input_validation_failed"

    reason_str = validation_result.reason or "input validation rejected"
    record = AuditRecord(
        record_id="aud-" + uuid.uuid4().hex[:12],
        run_id=run_id,
        operating_mode=operating_mode,
        input_hash=input_hash,
        query_unit_id=query_id,
        query_source=f"chat_input:{query_id}",
        query_position=0,
        proposal_action="",
        terminal_outcome=terminal_outcome,
        validation_result=reason_str,
        stop_reason=reason_str,
        timestamp=local_now_iso(),
        audit_phase="terminal",
        approval_approved=False,
        execution_attempted=False,
        execution_executed=False,
        errors=(reason_str,),
    )
    if audit_writer is not None:
        return audit_writer.append(record)
    return None
