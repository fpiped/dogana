"""Transactional reference store. Install dogana[duckdb]."""

from dogana.reference._sources import Rows

from .store import DuckDBStore

__all__ = ["DuckDBStore", "Rows"]
