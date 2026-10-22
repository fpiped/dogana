"""Explicit file-drop ingestion, without scheduling or file movement."""

from .drop import FileDrop, FilePattern

__all__ = ["FileDrop", "FilePattern"]
