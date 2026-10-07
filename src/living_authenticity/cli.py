"""Command-line entry point for one bounded local-integration run."""
import argparse
import sys

from Config.settings import load_models, load_paths
from src.living_authenticity.knowledge.integration import (
    check_staging_eligible,
    index_corpus,
    ingest_units,
    resolve_authorized_audit_failed,
    resolve_authorized_audit_passed,
    resolve_authorized_audit_root,
    resolve_authorized_staging_root,
    resolve_guarded_roots,
    run_bounded_integration,
)
from src.living_authenticity.knowledge.orchestration.orchestrator import (
    EvidenceFirstPipeline,
)


def _configure_unicode_output(streams=None) -> None:
    """Let stdout/stderr emit legitimate Unicode text on any console.

    Windows consoles default to a legacy code page (for example cp1252)
    whose encoder raises ``UnicodeEncodeError`` for the Persian and other
    non-Latin characters that legitimately occur in Vault notes, LLM
    titles/bodies, and proposed notes. Switching the text streams to
    UTF-8 removes that crash without altering, transliterating, or
    dropping any character. ``backslashreplace`` is only a last-resort
    for a stream that still cannot encode a character: the character is
    then shown as an escape rather than being silently lost or crashing
    the run.
    """
    targets = (sys.stdout, sys.stderr) if streams is None else streams
    for stream in targets:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError, AttributeError):
            continue


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (no side effects, no path defaults)."""
    parser = argparse.ArgumentParser(
        prog="bounded-integration",
        description=(
            "Run one bounded local-integration pass. LLM synthesis is a "
            "mandatory stage: without a usable provider the affected unit "
            "stops safely instead of proceeding."
        ),
    )
    parser.add_argument(
        "--input-file", default="",
        help="Single .md/.txt file to process (mutually exclusive "
             "with --text and --source-root).",
    )
    parser.add_argument(
        "--text", default="",
        help="Single in-memory text input (no temp files, no discovery).",
    )
    parser.add_argument(
        "--source-root", default="",
        help="Existing directory holding the .md/.txt inputs to process "
             "(no discovery, no recursion).",
    )
    parser.add_argument(
        "--staging-root", default="",
        help="Explicit staging destination; defaults to the config-declared "
             "staging.root and must still pass the unchanged guard.",
    )
    parser.add_argument(
        "--audit-root", default="",
        help="Explicit persistent audit root; defaults to the config-declared "
             "audit.root. Leave unset to keep audit records in memory only.",
    )
    parser.add_argument(
        "--audit-passed", default="",
        help="Explicit directory for passed audit records (approved CREATE "
             "that actually created its staging artifact); defaults "
             "to <audit-root>/passed. Must resolve inside the audit root.",
    )
    parser.add_argument(
        "--audit-failed", default="",
        help="Explicit directory for failed/rejected/held audit records; defaults "
             "to <audit-root>/failed. Must resolve inside the audit root.",
    )
    parser.add_argument(
        "--max-files", type=int, default=1,
        help="Upper bound applied to source, corpus, and Core files "
             "alike (default: 1).",
    )
    parser.add_argument(
        "--corpus-root", default="",
        help="Optional explicit directory of existing knowledge: its "
             "units form the retrieval context in token mode, and are "
             "embedded into the isolated vector store with --embed.",
    )
    parser.add_argument(
        "--core-root", default="",
        help="Optional explicit directory of approved Core references, "
             "used only as analytical Core observations.",
    )
    parser.add_argument(
        "--embed", action="store_true",
        help="Use the real local BGE-M3 embedding service and an "
             "isolated LanceDB vector store for retrieval.",
    )
    parser.add_argument(
        "--vector-db", default="",
        help="Required with --embed: explicitly supplied isolated "
             "vector database directory (never the production vector "
             "database). Created empty on first use.",
    )
    parser.add_argument(
        "--vault-context", action="store_true",
        help="Opt-in read-only vault excerpts as bounded LLM context "
             "(not semantic retrieval).",
    )
    parser.add_argument(
        "--vault-retrieval", action="store_true",
        help="Opt-in read-only retrieval against the configured Obsidian "
             "vault, so new input is evaluated against existing knowledge. "
             "Semantic with --embed; token-overlap otherwise.",
    )
    parser.add_argument(
        "--vault-max-files", type=int, default=10,
        help="Upper bound on vault notes read for context/retrieval "
             "(default: 10).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print inspectable intermediates for each unit.",
    )
    parser.add_argument(
        "--app-language", default="",
        help="APP/UI/process presentation language (en/fa); defaults to "
             "language.app from Config/localization.example.yaml.",
    )
    parser.add_argument(
        "--staging-language", default="",
        help="STAGING NOTE output language (en/fa), independent of app "
             "and audit languages.",
    )
    parser.add_argument(
        "--audit-language", default="",
        help="AUDIT human-readable presentation language (en/fa), "
             "independent of app and staging languages.",
    )
    return parser


def main(argv=None) -> int:
    """Parse arguments, run one bounded pass, and print a plain summary."""
    _configure_unicode_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not isinstance(args.max_files, int) or isinstance(
            args.max_files, bool) or args.max_files < 1:
        print("error: --max-files must be a positive integer")
        return 2
    if args.embed and not args.vector_db.strip():
        print("error: --embed requires an explicit --vector-db directory")
        return 2
    if args.vector_db.strip() and not args.embed:
        print("error: --vector-db is only used together with --embed")
        return 2
    modes = [bool(args.input_file.strip()), bool(args.text.strip()),
             bool(args.source_root.strip())]
    if sum(1 for flag in modes if flag) != 1:
        print("error: exactly one of --input-file, --text, --source-root "
              "is required")
        return 2
    if args.input_file.strip() and args.text.strip():
        print("error: --input-file and --text are mutually exclusive")
        return 2

    paths = load_paths()
    try:
        from Config.settings import load_llm
        llm_config = load_llm()
    except Exception as exc:
        print("error: LLM configuration invalid: " + str(exc))
        return 2
    guarded = resolve_guarded_roots(paths)
    authorized = resolve_authorized_staging_root(paths)
    staging_arg = args.staging_root.strip() or authorized

    # Core resolution: an explicit --core-root wins; otherwise the
    # configured vault's schema-defined Core folder ("01 Core") is used so
    # the preview evaluates against the project's real Core. Both are
    # read-only and revalidated by the boundary-checked ingest path.
    core_source = args.core_root.strip()
    core_origin = "explicit --core-root"
    if not core_source:
        from src.living_authenticity.knowledge.vault.context import (
            resolve_vault_core_root,
        )

        vault_for_core = paths.get("obsidian", {}).get("vault", "")
        core_source = resolve_vault_core_root(vault_for_core)
        if core_source:
            core_origin = "configured vault Core folder (schema '01 Core')"

    print("== bounded local integration ==")
    print("source -> ingestion -> retrieval -> comparison -> relation")
    print("  -> core analysis -> classification -> proposal -> confidence")
    print("  -> knowledge filter -> LLM synthesis (mandatory) -> human review")
    print("  -> explicit approval -> revalidation -> controlled execution")
    print("  -> audit")
    print("input: " + (args.input_file or args.source_root)
          if not args.text.strip() else "input: <text> (in memory)")
    print("staging root: " + (staging_arg or "(none)"))
    authorized_audit_preview = resolve_authorized_audit_root(paths)
    audit_root_preview = args.audit_root.strip() or authorized_audit_preview
    print("audit root: " + (audit_root_preview or "(memory only)"))
    if audit_root_preview:
        passed_preview = (args.audit_passed.strip()
                          or resolve_authorized_audit_passed(paths)
                          or (audit_root_preview.rstrip("/\\") + "/passed"))
        failed_preview = (args.audit_failed.strip()
                          or resolve_authorized_audit_failed(paths)
                          or (audit_root_preview.rstrip("/\\") + "/failed"))
        print("audit passed: " + passed_preview)
        print("audit failed: " + failed_preview)
    print("max files (source, corpus, core): " + str(args.max_files))
    print("corpus root: " + (args.corpus_root or "(none)"))
    print("core root: " + (core_source or "(none)") + (
        "" if not core_source else " (" + core_origin + ")"))
    print("retrieval: " + ("bge-m3 + isolated lancedb" if args.embed
                           else "token overlap"))
    if args.embed:
        print("vector store: " + args.vector_db)
        print("authorized staging: " + (authorized or "(none declared)"))

    staging_ok, staging_reason, _staging = check_staging_eligible(
        staging_arg, guarded, authorized)
    if not staging_ok:
        print("error: staging rejected: " + staging_reason)
        return 2
    vector_ok, vector_reason, _vector = (True, "", "")
    if args.embed:
        from src.living_authenticity.knowledge.integration.vector import (
            check_vector_store_eligible,
            resolve_production_vector_db,
        )

        production_db = resolve_production_vector_db(paths)
        print("production vector database: "
              + (production_db or "(none declared)"))
        vault_root = paths.get("obsidian", {}).get("vault", "")
        vector_ok, vector_reason, _vector = check_vector_store_eligible(
            args.vector_db, production_db, vault_root)
        if not vector_ok:
            print("error: vector store rejected: " + vector_reason)
            return 2

    from src.living_authenticity.llm import build_provider, build_synthesis
    try:
        synthesis = build_synthesis(build_provider(llm_config),
                                    config=llm_config)
    except Exception as exc:
        print("error: LLM provider unavailable: " + str(exc))
        return 2
    pipe = EvidenceFirstPipeline()
    corpus_units: list = []
    core_units: list = []
    if args.embed:
        from src.living_authenticity.knowledge.integration.vector import (
            build_vector_components,
        )
        models = load_models()
        embedding = models.get("embedding")
        if not isinstance(embedding, dict) or not isinstance(
                embedding.get("active"), str):
            print("error: embedding configuration is missing")
            return 2
        model_path = ""
        try:
            model_path = paths["models"]["bge_m3"]
        except Exception:
            model_path = ""
        if not isinstance(model_path, str) or not model_path.strip():
            print("error: configured models.bge_m3 is missing")
            return 2
        from Config.settings import ROOT as REPO_ROOT
        from src.living_authenticity.security.path_boundary import (
            PathBoundary,
        )
        if PathBoundary(str(REPO_ROOT)).is_allowed(model_path):
            print("error: embedding model inside repository workspace")
            return 2
        service, store, retriever = build_vector_components(
            args.vector_db, model_path=model_path)
        pipe = EvidenceFirstPipeline(retriever=retriever)
        existing = 0
        try:
            existing = len(store.show_all())
        except Exception:
            existing = 0
        print("vectors already stored: " + str(existing))
        if args.corpus_root.strip():
            if existing:
                print("corpus indexing refused: the isolated vector store "
                      "already holds " + str(existing) + " vectors.")
                print("  reuse the existing index by omitting --corpus-root, "
                      "or point --vector-db at a fresh empty directory.")
                return 2
            try:
                figures = index_corpus(
                    args.corpus_root, pipe, service, store, args.max_files)
            except ValueError as exc:
                print("error: corpus indexing failed: " + str(exc))
                return 2
            print("corpus indexed: files=" + str(figures[0])
                  + " units=" + str(figures[1])
                  + " vectors=" + str(figures[2])
                  + " skipped=" + str(figures[3]))
        if args.vault_retrieval:
            from src.living_authenticity.knowledge.vault.context import (
                read_vault_units,
            )
            vault_root = paths.get("obsidian", {}).get("vault", "")
            if not vault_root:
                print("error: --vault-retrieval requires obsidian.vault "
                      "in the local configuration")
                return 2
            try:
                vault_units, vault_files, vault_skipped = read_vault_units(
                    vault_root, pipe, max_files=args.vault_max_files)
            except Exception as exc:
                print("error: vault retrieval failed: " + str(exc))
                return 2
            if not vault_units:
                print("vault retrieval: UNAVAILABLE - 0 vault notes discovered "
                      "under the configured root (" + str(vault_skipped)
                      + " skipped). Semantic vault comparison was NOT performed.")
            elif existing:
                print("vault retrieval: semantic (reusing existing isolated "
                      "index of " + str(existing) + " vectors)")
                print("  vault files discovered: " + str(vault_files))
                print("  vault files skipped: " + str(vault_skipped))
            else:
                stored = 0
                figures = None
                try:
                    from src.living_authenticity.knowledge.integration import (
                        index_units,
                    )
                    figures = index_units(vault_units, service, store)
                    stored = figures[1]
                except ValueError as exc:
                    print("error: vault indexing failed: " + str(exc))
                    return 2
                print("vault retrieval: semantic (isolated index)")
                print("  vault files discovered: " + str(vault_files))
                print("  vault notes indexed: " + str(stored))
                print("  vault files skipped: " + str(vault_skipped))
            corpus_units.extend(vault_units)
    elif args.corpus_root.strip():
        try:
            corpus_units = ingest_units(
                args.corpus_root, pipe, args.max_files)
        except ValueError as exc:
            print("error: corpus ingestion failed: " + str(exc))
            return 2
        print("corpus units loaded: " + str(len(corpus_units)))
    if args.vault_retrieval and not args.embed:
        from src.living_authenticity.knowledge.vault.context import (
            read_vault_units,
        )
        vault_root = paths.get("obsidian", {}).get("vault", "")
        if not vault_root:
            print("error: --vault-retrieval requires obsidian.vault "
                  "in the local configuration")
            return 2
        try:
            vault_units, vault_files, vault_skipped = read_vault_units(
                vault_root, pipe, max_files=args.vault_max_files)
        except Exception as exc:
            print("error: vault retrieval failed: " + str(exc))
            return 2
        if not vault_units:
            print("vault retrieval: UNAVAILABLE - 0 vault notes discovered "
                  "under the configured root (" + str(vault_skipped)
                  + " skipped). Vault comparison was NOT performed.")
        else:
            print("vault retrieval: token-overlap (no --embed)")
            print("  vault files discovered: " + str(vault_files))
            print("  vault units loaded: " + str(len(vault_units)))
            print("  vault files skipped: " + str(vault_skipped))
        corpus_units.extend(vault_units)
    if core_source:
        try:
            core_units = ingest_units(core_source, pipe, args.max_files)
        except ValueError as exc:
            print("error: core ingestion failed: " + str(exc))
            return 2
        print("core units loaded: " + str(len(core_units)))
        print("core access is read-only; Core is never modified")

    vault_context_excerpts: list = []
    vault_context_skipped = 0
    if args.vault_context:
        try:
            from src.living_authenticity.knowledge.vault.context import (
                read_vault_context,
            )
            vault_root = paths.get("obsidian", {}).get("vault", "")
            if not vault_root:
                print("error: --vault-context requires obsidian.vault in config")
                return 2
            vault_context_excerpts, vault_context_skipped = read_vault_context(
                vault_root, max_files=args.vault_max_files)
            print("vault context (bounded LLM excerpts, informational only): "
                  + str(len(vault_context_excerpts)) + " excerpts loaded, "
                  + str(vault_context_skipped) + " skipped (recursive, read-only)")
            print("vault context is supplied to LLM synthesis as bounded "
                  "context only; it is not semantic retrieval and it "
                  "authorizes nothing")
        except Exception as exc:
            print("error: vault context failed: " + str(exc))
            return 2
    print("== human review begins: each candidate is shown, then "
          "approved one at a time ==")
    print("Display is informational only: shown text authorizes nothing.")
    print("Only explicit approval, revalidation, and controlled execution")
    print("may authorize a CREATE artifact in staging; nothing shown here")
    print("is placed in any authoritative location.")
    if args.text.strip():
        try:
            ingestion = pipe.ingestion.ingest_text(
                args.text, source="<text-input>", kind="text-input.md")
        except Exception as exc:
            print("error: text ingestion failed: " + str(exc))
            return 2
        authorized_audit = resolve_authorized_audit_root(paths)
        authorized_passed = resolve_authorized_audit_passed(paths)
        authorized_failed = resolve_authorized_audit_failed(paths)
        audit_root_arg = args.audit_root.strip() or authorized_audit
        audit_passed_arg = args.audit_passed.strip() or authorized_passed
        audit_failed_arg = args.audit_failed.strip() or authorized_failed
        report = run_bounded_integration(
            "", staging_arg, pipeline=pipe,
            corpus=corpus_units, core_units=core_units,
            max_files=args.max_files,
            llm_config=llm_config, synthesis=synthesis,
            ingestion=ingestion,
            vault_context=vault_context_excerpts or None,
            audit_root=audit_root_arg,
            authorized_audit_root=authorized_audit,
            authorized_audit_passed=audit_passed_arg,
            authorized_audit_failed=audit_failed_arg,
        )
    elif args.input_file.strip():
        from pathlib import Path as _Path
        single = _Path(args.input_file)
        if single.is_symlink() or not single.is_file():
            print("error: --input-file must be an existing file")
            return 2
        if single.suffix.lower() not in (".md", ".txt"):
            print("error: --input-file must be .md or .txt")
            return 2
        authorized_audit = resolve_authorized_audit_root(paths)
        authorized_passed = resolve_authorized_audit_passed(paths)
        authorized_failed = resolve_authorized_audit_failed(paths)
        audit_root_arg = args.audit_root.strip() or authorized_audit
        audit_passed_arg = args.audit_passed.strip() or authorized_passed
        audit_failed_arg = args.audit_failed.strip() or authorized_failed
        report = run_bounded_integration(
            str(single.parent), staging_arg, pipeline=pipe,
            corpus=corpus_units, core_units=core_units,
            max_files=args.max_files,
            llm_config=llm_config, synthesis=synthesis,
            source_files=[str(single)],
            vault_context=vault_context_excerpts or None,
            audit_root=audit_root_arg,
            authorized_audit_root=authorized_audit,
            authorized_audit_passed=audit_passed_arg,
            authorized_audit_failed=audit_failed_arg,
        )
    else:
        authorized_audit = resolve_authorized_audit_root(paths)
        authorized_passed = resolve_authorized_audit_passed(paths)
        authorized_failed = resolve_authorized_audit_failed(paths)
        audit_root_arg = args.audit_root.strip() or authorized_audit
        audit_passed_arg = args.audit_passed.strip() or authorized_passed
        audit_failed_arg = args.audit_failed.strip() or authorized_failed
        report = run_bounded_integration(
            args.source_root, staging_arg, pipeline=pipe,
            corpus=corpus_units, core_units=core_units,
            max_files=args.max_files,
            llm_config=llm_config, synthesis=synthesis,
            vault_context=vault_context_excerpts or None,
            audit_root=audit_root_arg,
            authorized_audit_root=authorized_audit,
            authorized_audit_passed=audit_passed_arg,
            authorized_audit_failed=audit_failed_arg,
        )
    if not report.accepted:
        print("run rejected: " + report.rejection_reason)
        print("failed check: " + report.rejection_check)
        return 2
    print("files seen: " + str(report.files_seen))
    print("files processed: " + str(report.files_processed))
    print("files skipped: " + str(report.files_skipped))
    executed = 0
    held = 0
    for entry in report.units:
        print(
            entry.query_unit_id + " | " + entry.proposal_action
            + " | " + entry.outcome + " | " + entry.reason
            + " | artifact=" + entry.artifact_reference
        )
        print("  source: " + str(entry.query_source)
              + " position=" + str(entry.query_position))
        if entry.query_text:
            print("  source text: " + str(entry.query_text)[:800])
        if entry.proposal_title:
            print("  proposed title: " + str(entry.proposal_title))
        print("  proposed action: " + str(entry.proposal_action)
              + " reason=" + str(entry.proposal_reason or entry.reason))
        if entry.classification_type or entry.classification_rationale:
            print("  classification: " + str(entry.classification_type)
                  + " rationale=" + str(entry.classification_rationale)[:400])
        if entry.confidence_level or entry.confidence_basis:
            print("  confidence: " + str(entry.confidence_level)
                  + " basis=" + str(entry.confidence_basis)[:400])
        if entry.filter_verdict or entry.filter_basis:
            print("  filter: " + str(entry.filter_verdict)
                  + " basis=" + str(entry.filter_basis)[:400])
        if entry.llm_candidates:
            for i, lc in enumerate(entry.llm_candidates):
                body = str(lc.get("body", ""))
                print("  llm_candidate[" + str(i) + "]: title="
                      + str(lc.get("title", "")) + " type="
                      + str(lc.get("suggested_type", "")))
                print("    reason=" + str(lc.get("reason", ""))[:600])
                print("    uncertainty=" + str(lc.get("uncertainty", ""))[:400])
                print("    body=" + body[:1200])
        if entry.note_markdown:
            print("  proposed note (informational, not authorized):")
            print("  " + str(entry.note_markdown)[:2500].replace("\n", "\n  "))
        if entry.proposed_destination:
            print("  proposed destination (inert, not written): "
                  + str(entry.proposed_destination))
        if entry.retrieval_candidates:
            for i, rc in enumerate(entry.retrieval_candidates[:5]):
                print("  retrieval[" + str(i) + "]: unit_id="
                      + str(rc.get("unit_id", "")) + " source="
                      + str(rc.get("source", "")) + " overlap="
                      + str(rc.get("overlap_score", 0)) + " excerpt="
                      + str(rc.get("excerpt", ""))[:200])
        if entry.outcome == "executed":
            executed += 1
        else:
            held += 1
    print("executed: " + str(executed)
          + ", held or rejected: " + str(held))
    print("audit records: " + str(len(report.audits)))
    for audit in report.audits:
        print(
            "audit: hash=" + str(audit.proposal_hash)
            + " action=" + str(audit.proposal_action)
            + " approved=" + str(audit.approval_approved)
            + " revalidated=" + str(audit.revalidation_valid)
            + " executed=" + str(audit.execution_executed)
            + " artifact=" + str(audit.artifact_reference)
        )
    print(
        "staging artifacts are non-authoritative proposals requiring "
        "human review; nothing was placed in any authoritative location."
    )
    if args.verbose and getattr(report, "details", ()):
        print("\n== VERBOSE INTERMEDIATES (CREATE proposals) ==")
        for detail in report.details:
            print("---")
            print("source_file: " + str(detail.source_file))
            print("query_unit_id: " + str(getattr(detail.query_unit, "id", "")))
            print("candidate_index: " + str(detail.candidate_index))
            print("prompt_sha256: " + str(detail.prompt_sha256))
            if detail.llm_result and getattr(detail.llm_result, "candidates", ()):
                for idx, c in enumerate(detail.llm_result.candidates):
                    print("  candidate " + str(idx) + ": title=" + str(getattr(c, "title", ""))
                          + " body_len=" + str(len(getattr(c, "body", ""))))
            if detail.note_markdown:
                print("note_markdown (truncated): " + detail.note_markdown[:200] + "...")
            print("outcome: " + str(getattr(detail.human_decision, "approved", "")))
            print("revalidation_valid: " + str(getattr(detail.revalidation, "valid", "")))
            print("execution_executed: " + str(getattr(detail.execution, "executed", "")))

    if args.verbose and report.units:
        print("\n== ALL PROPOSALS (including non-CREATE) ==")
        for entry in report.units:
            print("---")
            print("source_file: " + str(entry.source_file))
            print("query_unit_id: " + str(entry.query_unit_id))
            print("query_source: " + str(entry.query_source))
            print("query_position: " + str(entry.query_position))
            print("query_text: " + str(entry.query_text)[:1200])
            print("proposal_action: " + str(entry.proposal_action))
            print("proposal_title: " + str(entry.proposal_title))
            print("proposal_body (truncated): " + str(entry.proposal_body[:1200]) + ("..." if len(entry.proposal_body) > 1200 else ""))
            print("proposal_reason: " + str(entry.proposal_reason))
            print("proposed_type: " + str(entry.proposed_type))
            print("uncertainty: " + str(entry.uncertainty))
            print("confidence_level: " + str(entry.confidence_level))
            print("confidence_basis: " + str(entry.confidence_basis))
            print("filter_verdict: " + str(entry.filter_verdict))
            print("filter_basis: " + str(entry.filter_basis))
            print("classification_type: " + str(entry.classification_type))
            print("classification_rationale: " + str(entry.classification_rationale))
            print("core_relevance: " + str(entry.core_relevance))
            print("core_basis: " + str(entry.core_basis))
            print("retrieval_candidates: " + str(len(entry.retrieval_candidates)))
            for i, rc in enumerate(entry.retrieval_candidates):
                print("  retrieval[" + str(i) + "]: unit_id=" + str(rc.get("unit_id", "")) + " source=" + str(rc.get("source", "")) + " overlap=" + str(rc.get("overlap_score", 0)))
            print("comparison_results: " + str(len(entry.comparison_results)))
            for i, cr in enumerate(entry.comparison_results):
                print("  comparison[" + str(i) + "]: candidate=" + str(cr.get("candidate_id", "")) + " category=" + str(cr.get("category", "")))
            print("relation_results: " + str(len(entry.relation_results)))
            for i, rr in enumerate(entry.relation_results):
                print("  relation[" + str(i) + "]: candidate=" + str(rr.get("candidate_id", "")) + " type=" + str(rr.get("relation", "")))
            print("core_relevance: " + str(entry.core_relevance))
            print("core_basis: " + str(entry.core_basis))
            if entry.llm_candidates:
                print("llm_candidates: " + str(len(entry.llm_candidates)))
                for i, lc in enumerate(entry.llm_candidates):
                    print("  llm_candidate[" + str(i) + "]: title=" + str(lc.get("title", "")) + " type=" + str(lc.get("suggested_type", "")) + " body_len=" + str(len(lc.get("body", ""))))
            print("note_markdown (truncated): " + str(entry.note_markdown[:2500]) + ("..." if len(entry.note_markdown) > 2500 else ""))
            print("proposed destination (inert, not written): "
                  + str(entry.proposed_destination))
            print("outcome: " + str(entry.outcome))
            print("reason: " + str(entry.reason))
            print("proposal_hash: " + str(entry.proposal_hash))
            print("approval_approved: " + str(entry.approval_approved))
            print("revalidation_valid: " + str(entry.revalidation_valid))
            print("execution_executed: " + str(entry.execution_executed))
            print("artifact_reference: " + str(entry.artifact_reference))
    return 0


if __name__ == "__main__":
    sys.exit(main())
