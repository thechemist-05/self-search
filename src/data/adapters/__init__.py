"""Source adapters: turn dataset records into validated, provenance-tagged catalogue rows.

Each adapter implements ``SourceAdapter`` (``base.py``); ``pipeline.ingest`` inserts
its output. Adapters never download anything: they read records already on disk.
"""
