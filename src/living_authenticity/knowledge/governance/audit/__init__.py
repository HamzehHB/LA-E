"""Audit / traceability record (observational only).

The audit layer records the controlled lifecycle
(proposal -> approval -> revalidation -> execution) for inspection.
It never approves, authorizes, executes, or mutates knowledge.
"""
from .outcome import AuditRecord, build_audit_record, record_audit_for_execution
from .clock import local_now_iso
from .persistence import (AUDIT_LOG_FILENAME, FAILED_SUBDIR, PASSED_SUBDIR,
                          AuditWriter, audit_storage_category, persist_record,
                          record_digest, record_to_dict)

__all__ = ("AUDIT_LOG_FILENAME", "AuditRecord", "AuditWriter",
           "FAILED_SUBDIR", "PASSED_SUBDIR", "audit_storage_category",
           "build_audit_record", "local_now_iso", "persist_record",
           "record_audit_for_execution", "record_digest", "record_to_dict")
