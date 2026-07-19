"""Snapshot ingestion: parse custodian files, validate, preview, commit.

Phase 1 of the roadmap. The custodian file is the source of truth; this package
turns it into `holdings_snapshot` rows and nothing else derives from re-keyed data.
"""
