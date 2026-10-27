"""Explicit file-drop ingestion, without scheduling or file movement."""

from .drop import FileDrop, FilePattern, ingest_together

__all__ = ["FileDrop", "FilePattern", "ingest_together"]
