"""An example store on DuckDB and a file drop that feeds it: one way to publish rows and dates together."""

from ._sources import Rows
from .drop import FileDrop, FilePattern, ingest_together
from .store import DuckDBStore

__all__ = ["DuckDBStore", "FileDrop", "FilePattern", "Rows", "ingest_together"]
