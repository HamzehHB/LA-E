"""Security regression tests for exception diagnostic sanitization.

Verifies that audit records, persisted JSONL journals, frontend events,
and user-facing diagnostics never expose private user input, parser snippets,
credentials, tokens, authenticated URLs, or local filesystem usernames/paths.
"""

import json
import re
import time
from pathlib import Path

import pytest

from src.living_authenticity.adapter.app import Launcher
from src.living_authenticity.knowledge.governance.audit import AuditWriter
from src.living_authenticity.security.diagnostic import (
    MAX_DIAGNOSTIC_LENGTH,
    safe_exception_diagnostic,
    sanitize_diagnostic_text,
    sanitize_path,
)

VALID_OBSERVATION_TEXT = (
    "Observation:\n"
    "I observed that quiet reflective practice consistently fosters emotional resilience in complex teams."
)


def _make_launcher(tmp_path, **kwargs) -> Launcher:
    staging_dir = tmp_path / "staging"
    audit_dir = tmp_path / "audit"
    staging_dir.mkdir(parents=True, exist_ok=True)
    audit_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "staging": {"root": str(staging_dir)},
        "audit": {"root": str(audit_dir)},
    }
    paths.update(kwargs.get("paths", {}))
    return Launcher(paths=paths)


# 1. Parser Privacy
def test_parser_privacy_malformed_yaml_does_not_leak_private_snippet(tmp_path, monkeypatch):
    """When a document parser fails, raw snippets of user input or private notes
    must never appear in audit records, stop_reason, or emitted events."""
    launcher = _make_launcher(tmp_path)
    private_marker = "CONFIDENTIAL_PRIVATE_OBSIDIAN_NOTE_CONTENT_XY77"

    yaml_error_msg = (
        'while scanning a simple key\n'
        '  in "<unicode string>", line 3, column 1\n'
        'could not find expected \':\'\n'
        '  in "<unicode string>", line 4, column 1:\n'
        f'    {private_marker}\n'
    )

    def failing_execute(*args, **kwargs):
        raise RuntimeError(f"YAML parse error: {yaml_error_msg}")

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    assert run_id in launcher._reports
    rep = launcher._reports[run_id]
    assert rep["outcome"] == "FAILED"
    paused = launcher._paused_run
    assert paused is not None

    # A. Check paused run state
    paused_err = paused.get("error_message", "")
    assert private_marker not in paused_err
    assert "<snippet redacted>" in paused_err

    # B. Check report reason
    assert private_marker not in rep["reason"]

    # C. Check emitted events
    events = [e for e in launcher.events.since(0) if e.get("run_id") == run_id]
    for ev in events:
        msg = ev.get("message", "")
        assert private_marker not in msg, f"Private marker leaked in event: {ev}"

    # D. Check persisted audit record in errors/audit.jsonl
    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    assert errors_file.exists()
    records = [json.loads(line) for line in errors_file.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
    matching = [r for r in records if r.get("run_id") == run_id]
    assert len(matching) == 1
    rec = matching[0]

    assert private_marker not in rec["stop_reason"]
    assert all(private_marker not in err for err in rec.get("errors", ()))
    assert "<snippet redacted>" in rec["stop_reason"]


# 2. Credential Protection
def test_credential_protection_tokens_keys_urls_redacted(tmp_path, monkeypatch):
    """Synthetic API keys, Bearer tokens, passwords, and authenticated URLs
    must be redacted before reaching audit records or event telemetry."""
    launcher = _make_launcher(tmp_path)

    # Construct synthetic secret values dynamically to avoid static detection false-positives
    secret_key = "sk-" + "live998877665544332211"
    bearer_token = "mock_test_bearer_token_" + "x" * 24
    secret_pass = "P@ssw0rd_SuperSecret!"
    synthetic_ghp = "g" + "hp_" + "1234567890abcdefghijklmnopqrstuvwxyz"
    auth_url = f"https://api_user:{secret_pass}@api.provider.internal/v1/generate?api_key={secret_key}"

    leak_payload = (
        f"ExternalServiceError: Failed calling {auth_url} with Authorization: Bearer {bearer_token} "
        f"and token: {synthetic_ghp}"
    )

    def failing_execute(*args, **kwargs):
        raise ConnectionError(leak_payload)

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    assert launcher._reports[run_id]["outcome"] == "FAILED"

    # Verify that none of the secrets appear in paused run or reports
    paused = launcher._paused_run
    assert paused is not None
    paused_msg = paused["error_message"]
    report_reason = launcher._reports[run_id]["reason"]
    for secret in (secret_key, bearer_token, secret_pass, synthetic_ghp):
        assert secret not in paused_msg, f"Secret {secret} leaked in paused run message"
        assert secret not in report_reason, f"Secret {secret} leaked in report reason"

    # Verify events
    events = [e for e in launcher.events.since(0) if e.get("run_id") == run_id]
    for ev in events:
        msg = ev.get("message", "")
        for secret in (secret_key, bearer_token, secret_pass, synthetic_ghp):
            assert secret not in msg, f"Secret {secret} leaked in event payload: {ev}"

    # Verify on-disk audit journal
    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    records = [json.loads(line) for line in errors_file.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
    rec = [r for r in records if r.get("run_id") == run_id][0]

    for secret in (secret_key, bearer_token, secret_pass, synthetic_ghp):
        assert secret not in rec["stop_reason"], f"Secret {secret} leaked in stop_reason"
        assert all(secret not in err for err in rec.get("errors", ())), f"Secret {secret} leaked in errors"

    assert "<redacted>" in rec["stop_reason"]


# 3. Filesystem Privacy
def test_filesystem_privacy_absolute_paths_and_usernames_redacted(tmp_path, monkeypatch):
    """Absolute filesystem paths with local usernames and machine directory structures
    must have host-identifying directories redacted while preserving safe basenames."""
    launcher = _make_launcher(tmp_path)

    secret_username = "ConfidentialResearcherName"
    raw_os_path = rf"C:\Users\{secret_username}\PrivateResearchVault\SensitiveNotes\deep_insight.md"
    raw_unix_path = f"/home/{secret_username}/vault/private_strategy.json"

    def failing_execute(*args, **kwargs):
        raise FileNotFoundError(
            f"[Errno 2] No such file or directory: '{raw_os_path}' (also referenced '{raw_unix_path}')"
        )

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    paused = launcher._paused_run
    assert paused is not None
    paused_msg = paused["error_message"]
    assert secret_username not in paused_msg
    assert "<path>/deep_insight.md" in paused_msg
    assert "<path>/private_strategy.json" in paused_msg

    # Verify on-disk audit journal
    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    records = [json.loads(line) for line in errors_file.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
    rec = [r for r in records if r.get("run_id") == run_id][0]

    assert secret_username not in rec["stop_reason"]
    assert "<path>/deep_insight.md" in rec["stop_reason"]
    assert all(secret_username not in err for err in rec.get("errors", ()))


# 4. Control-Character Handling & Log-Injection Prevention
def test_control_character_handling_prevents_log_splitting_and_ansi_escape(tmp_path, monkeypatch):
    """CR/LF, control characters, and ANSI escapes must not split JSONL journal records
    or create forged log entries."""
    launcher = _make_launcher(tmp_path)

    forged_entry_text = '{"run_id":"forged_run","terminal_outcome":"full_success"}'
    crlf_payload = (
        f"Crash with injection:\r\n{forged_entry_text}\r\n\x1b[31;1mFORGED_TERMINAL_OUTPUT\x1b[0m"
    )

    def failing_execute(*args, **kwargs):
        raise RuntimeError(crlf_payload)

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    raw_lines = [line for line in errors_file.read_text(encoding="utf-8").splitlines() if line.strip()]

    # Invariant: There MUST be exactly ONE line written for this run, not three lines!
    matching_lines = [l for l in raw_lines if run_id in l]
    assert len(matching_lines) == 1

    # Invariant: The line must parse as standard valid JSON without syntax error
    record = json.loads(matching_lines[0])
    assert record["run_id"] == run_id
    assert "\r" not in record["stop_reason"]
    assert "\n" not in record["stop_reason"]
    assert "\x1b" not in record["stop_reason"]
    assert "FORGED_TERMINAL_OUTPUT" in record["stop_reason"]


# 5. Diagnostic Usefulness
def test_diagnostic_usefulness_preserves_error_type_and_context(tmp_path, monkeypatch):
    """Sanitization must preserve the exception type and safe explanatory context
    so issues remain diagnosable without exposing sensitive internals."""
    launcher = _make_launcher(tmp_path)

    def failing_execute(*args, **kwargs):
        raise TimeoutError("LanceDB vector storage index reconciliation timed out after 45.0s")

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    rep = launcher._reports[run_id]
    assert rep["error"] == "TimeoutError"
    assert "LanceDB vector storage index reconciliation timed out after 45.0s" in rep["reason"]

    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    record = json.loads(errors_file.read_text(encoding="utf-8").strip())
    assert "TimeoutError: LanceDB vector storage index reconciliation timed out after 45.0s" in record["errors"][0]


# 6. Length Limits
def test_length_limits_bound_excessive_diagnostic_payloads(tmp_path, monkeypatch):
    """Excessively long exception messages must be bounded fail-closed."""
    launcher = _make_launcher(tmp_path)
    huge_message = "A" * 2000

    def failing_execute(*args, **kwargs):
        raise ValueError(huge_message)

    monkeypatch.setattr(launcher, "_execute_run", failing_execute)

    res = launcher.start_run(input_text=VALID_OBSERVATION_TEXT, source_mode="examples")
    run_id = res["run_id"]

    for _ in range(50):
        with launcher._lock:
            if run_id in launcher._reports:
                break
        time.sleep(0.05)

    paused = launcher._paused_run
    assert paused is not None
    paused_msg = paused["error_message"]
    assert len(paused_msg) <= MAX_DIAGNOSTIC_LENGTH
    assert paused_msg.endswith("... [truncated]")

    errors_file = tmp_path / "audit" / "errors" / "audit.jsonl"
    record = json.loads(errors_file.read_text(encoding="utf-8").strip())
    assert len(record["stop_reason"]) <= MAX_DIAGNOSTIC_LENGTH
    assert record["stop_reason"].endswith("... [truncated]")


# 7. Alternate Path Coverage: stop_paused_run and runner-level paths
def test_alternate_paths_enforce_consistent_diagnostic_sanitization(tmp_path, monkeypatch):
    """Verify that stop_paused_run, runner file ingestion, and text ingestion
    all apply identical diagnostic security guarantees."""
    # A. stop_paused_run path
    launcher = _make_launcher(tmp_path)
    paused_id = "run-paused-leak-test"
    secret_token = "sk-" + "supersecretoperatorkey999"
    launcher._paused_run = {
        "run_id": paused_id,
        "input_text": "Sample text",
        "source_mode": "examples",
        "retrieval_mode": "lexical",
        "error": "IOError",
        "error_message": f"Disk fault on C:\\Users\\SecretUser\\vault with api_key={secret_token}",
        "timestamp": "2026-11-04T00:00:00Z",
    }

    res = launcher.stop_paused_run(run_id=paused_id)
    assert res["stopped"] is True

    failed_file = tmp_path / "audit" / "failed" / "audit.jsonl"
    records = [json.loads(line) for line in failed_file.read_text(encoding="utf-8").strip().splitlines() if line.strip()]
    stop_rec = [r for r in records if r.get("run_id") == paused_id][0]

    assert secret_token not in stop_rec["stop_reason"]
    assert "SecretUser" not in stop_rec["stop_reason"]
    assert all(secret_token not in err for err in stop_rec.get("errors", ()))
    assert all("SecretUser" not in err for err in stop_rec.get("errors", ()))

    # B. Direct safe_exception_diagnostic unit behavior
    raw_exc = RuntimeError(r"Database error at C:\Users\AdminUser\data\db.lance with token: g" + "hp_secrettoken12345")
    reason, errs = safe_exception_diagnostic(raw_exc, stage="retrieval", category="operational_error")
    assert "AdminUser" not in reason
    assert "secrettoken12345" not in reason
    assert "<path>/db.lance" in reason
    assert reason in errs[0]


# 8. Edge Case Coverage: Quoted secrets, URI schemes, query params, UNC paths
def test_sanitizer_edge_cases_quoted_secrets_schemes_and_unc_paths():
    """Verify that quoted secrets (including JSON/YAML), multi-protocol URIs,
    extended query params, and UNC paths are thoroughly redacted."""
    # 1. Quoted secrets and JSON/YAML formats
    secret_val1 = "super_" + "secret_" + "token_111"
    secret_val2 = "db_" + "pass_" + "word_222"
    json_leak = f'{{"api_key": "{secret_val1}", "password": "{secret_val2}"}}'
    sanitized_json = sanitize_diagnostic_text(json_leak)
    assert secret_val1 not in sanitized_json
    assert secret_val2 not in sanitized_json
    assert '{"api_key": <redacted>, "password": <redacted>}' == sanitized_json

    single_quoted = f"token='{secret_val1}' and password='{secret_val2}'"
    sanitized_sq = sanitize_diagnostic_text(single_quoted)
    assert secret_val1 not in sanitized_sq
    assert secret_val2 not in sanitized_sq
    assert "token=<redacted> and password=<redacted>" == sanitized_sq

    # 2. Multi-protocol URI schemes with credentials
    db_pass = "pg_" + "pass_" + "word_333"
    mongo_pass = "mongo_" + "pass_" + "word_444"
    redis_pass = "redis_" + "pass_" + "word_555"
    ws_token = "ws_" + "token_" + "666"

    pg_url = f"postgresql://pg_admin:{db_pass}@localhost:5432/livedb"
    mongo_url = f"mongodb://mongo_user:{mongo_pass}@cluster0.internal:27017/admin"
    redis_url = f"redis://default:{redis_pass}@redis.internal:6379/0"
    ws_url = f"wss://client:{ws_token}@socket.example.com"

    for url, passw in [(pg_url, db_pass), (mongo_url, mongo_pass), (redis_url, redis_pass), (ws_url, ws_token)]:
        res = sanitize_diagnostic_text(f"Connection failed: {url}")
        assert passw not in res
        assert "<redacted>@" in res

    # 3. Extended query parameters
    query_secret = "client_" + "secret_" + "val_777"
    access_key = "access_" + "key_" + "val_888"
    query_url = f"https://api.example.com/v1?client_secret={query_secret}&access_key={access_key}&id=10"
    sanitized_query = sanitize_diagnostic_text(f"Query failed: {query_url}")
    assert query_secret not in sanitized_query
    assert access_key not in sanitized_query
    assert "client_secret=<redacted>" in sanitized_query
    assert "access_key=<redacted>" in sanitized_query

    # 4. UNC paths
    unc_user = "ConfidentialUser"
    unc_path1 = rf"\\corp-fileserver\users\{unc_user}\vault\notes.md"
    unc_path2 = rf'"\\10.0.0.5\shares\{unc_user}\export.pdf"'
    sanitized_unc = sanitize_diagnostic_text(f"File error on {unc_path1} and {unc_path2}")
    assert unc_user not in sanitized_unc
    assert "<path>/notes.md" in sanitized_unc
    assert '"<path>/export.pdf"' in sanitized_unc


# 9. Extended RFC 3986 URI Userinfo Redaction
def test_uri_userinfo_rfc3986_extended_forms():
    """Verify user-only, password-only, percent-encoded, and delimiter userinfo forms
    across diverse URI schemes while preserving non-sensitive host/port/path and
    leaving ordinary URLs with @ in path or query intact."""
    # A. User-only userinfo (e.g. postgresql://user@host)
    u_user = "db_admin_user"
    pg_url = f"postgresql://{u_user}@db.internal:5432/production"
    sanitized_pg = sanitize_diagnostic_text(f"Connection failed: {pg_url}")
    assert u_user not in sanitized_pg
    assert "postgresql://<redacted>@db.internal:5432/production" in sanitized_pg

    # B. Password-only userinfo (e.g. redis://:password@host)
    r_pass = "redis_" + "secret_" + "pass_999"
    redis_url = f"redis://:{r_pass}@redis.cache.internal:6379/0"
    sanitized_redis = sanitize_diagnostic_text(f"Redis AUTH error: {redis_url}")
    assert r_pass not in sanitized_redis
    assert "redis://:<redacted>@redis.cache.internal:6379/0" in sanitized_redis

    # C. Percent-encoded and sub-delimiter userinfo
    m_user = "app%2Bworker"
    m_pass = "p%40ss%21word%24secret"
    mongo_url = f"mongodb://{m_user}:{m_pass}@cluster0.internal:27017/admin?ssl=true&replicaSet=rs0"
    sanitized_mongo = sanitize_diagnostic_text(f"MongoDB handshake timeout: {mongo_url}")
    assert m_pass not in sanitized_mongo
    assert f"mongodb://{m_user}:<redacted>@cluster0.internal:27017/admin?ssl=true&replicaSet=rs0" in sanitized_mongo

    # D. AMQP and custom schemes
    q_pass = "rabbit_" + "pw_" + "123"
    amqp_url = f"amqp://service_acct:{q_pass}@queue.internal:5672/vhost"
    sanitized_amqp = sanitize_diagnostic_text(f"Queue error: {amqp_url}")
    assert q_pass not in sanitized_amqp
    assert "amqp://service_acct:<redacted>@queue.internal:5672/vhost" in sanitized_amqp

    # E. Ordinary URLs with @ in path or query parameters must NOT be mangled
    normal_url1 = "https://example.com/users/developer@domain.com/profile"
    normal_url2 = "https://example.com/api/search?email=contact@company.org&page=1"
    normal_msg = f"HTTP 404 at {normal_url1} and {normal_url2}"
    sanitized_normal = sanitize_diagnostic_text(normal_msg)
    assert normal_url1 in sanitized_normal
    assert normal_url2 in sanitized_normal


# 10. Extended Absolute Filesystem Path Coverage
def test_extended_absolute_filesystem_path_coverage():
    """Verify that root, mnt, media, Volumes, private, srv, data, and system directories
    are cleanly redacted while preserving safe basenames and leaving URL paths intact."""
    secret_user = "SecretResearcher"

    # Unix platform roots
    root_path = f"/root/{secret_user}/id_ed25519.pem"
    mnt_path = f"/mnt/storage/{secret_user}/vault/dataset.csv"
    media_path = f"/media/usb0/{secret_user}/backups/archive.tar.gz"
    vol_path = f"/Volumes/EncryptedDrive/{secret_user}/notes.md"
    priv_path = f"/private/var/log/{secret_user}/crash.log"
    srv_path = f"/srv/analytics/{secret_user}/report.parquet"
    data_path = f"/data/vaults/{secret_user}/index.lance"
    lib_path = f"/Library/Preferences/{secret_user}/settings.plist"
    proc_path = "/proc/sys/kernel/random/entropy_avail"

    test_input = (
        f"Errors encountered:\n"
        f"1. Failed key at {root_path}\n"
        f"2. Unmounted at '{mnt_path}'\n"
        f"3. Ejected: \"{media_path}\"\n"
        f"4. Volume unreadable: {vol_path}\n"
        f"5. Crash recorded: {priv_path}\n"
        f"6. Service error: {srv_path}\n"
        f"7. Database file: path={data_path}\n"
        f"8. Config plist: {lib_path}\n"
        f"9. Proc error: {proc_path}\n"
    )

    sanitized = sanitize_diagnostic_text(test_input, max_length=1000)
    assert secret_user not in sanitized
    assert "<path>/id_ed25519.pem" in sanitized
    assert "'<path>/dataset.csv'" in sanitized
    assert '"<path>/archive.tar.gz"' in sanitized
    assert "<path>/notes.md" in sanitized
    assert "<path>/crash.log" in sanitized
    assert "<path>/report.parquet" in sanitized
    assert "path=<path>/index.lance" in sanitized
    assert "<path>/settings.plist" in sanitized
    assert "<path>" in sanitized

    # URLs containing path segments like /data/ or /media/ must NOT be mangled
    url_text = (
        "Endpoints checked: https://cdn.example.com/data/export.csv, "
        "http://stream.example.org/media/video.mp4, and "
        "https://service.internal:8443/var/status"
    )
    sanitized_urls = sanitize_diagnostic_text(url_text)
    assert "https://cdn.example.com/data/export.csv" in sanitized_urls
    assert "http://stream.example.org/media/video.mp4" in sanitized_urls
    assert "https://service.internal:8443/var/status" in sanitized_urls


# 11. Sanitizer Contract, Edge Cases & Length Bounds
def test_sanitizer_contract_edge_cases_and_length_bounds():
    """Verify zero/negative max_length, small bounds, malformed input types,
    and safe_exception_diagnostic behavior."""
    # Zero and negative max_length
    assert sanitize_diagnostic_text("Critical security fault", max_length=0) == ""
    assert sanitize_diagnostic_text("Critical security fault", max_length=-1) == ""
    assert sanitize_diagnostic_text("Critical security fault", max_length=-100) == ""

    # Small max_length <= len("... [truncated]") (15 chars)
    assert sanitize_diagnostic_text("Critical fault", max_length=5) == "Criti"
    assert sanitize_diagnostic_text("Critical fault", max_length=10) == "Critical f"
    assert sanitize_diagnostic_text("Critical fault", max_length=15) == "Critical fault"

    # Moderate max_length > 15
    long_msg = "A" * 100
    res25 = sanitize_diagnostic_text(long_msg, max_length=25)
    assert len(res25) == 25
    assert res25.endswith("... [truncated]")

    # None and unusual inputs
    assert sanitize_diagnostic_text(None) == ""
    assert sanitize_diagnostic_text("") == ""
    assert sanitize_diagnostic_text(12345) == "12345"
    assert sanitize_diagnostic_text(["error", "detail"]) == "['error', 'detail']"

    # safe_exception_diagnostic with long or unusual exception type
    class ExceptionWithHugeCustomNameAcrossSubsystemsForTesting(RuntimeError):
        pass

    huge_exc = ExceptionWithHugeCustomNameAcrossSubsystemsForTesting("Database failed at /root/db/vault.db")
    reason, errs = safe_exception_diagnostic(huge_exc, max_length=100)
    assert len(reason) <= 100
    assert "/root/db" not in reason
    assert "<path>/vault.db" in reason
    # exc_type in errors is bounded
    assert len(errs[0]) <= 200
    assert reason in errs[0]
