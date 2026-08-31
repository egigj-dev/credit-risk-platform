# Architecture

The platform has two explicit modes: a deterministic in-memory demo for immediate UI
evaluation, and a full PostgreSQL production adapter serving the same dashboard alongside
the richer CRM API.

The canonical production sequence is: `source_staging → data_validation → feature_store_rebuild → pd_scoring → npl_scoring → ownership_refresh → nbo_scoring → reporting_refresh`.

The package orchestrator owns the complete sequence. The original v2 PD runner begins at
feature-store reading, so preprocessing is invoked before it and checkpointed by business
date. The stored ETL log remains the source for dashboard status and health responses.

Artifacts are external to Git. Each import writes `legacy-import-manifest.json` with file checksums. Production should replace that import with an approved object store or model registry.
