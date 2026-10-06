"""Tables the engine can query, with the statistics the optimizer uses."""

from __future__ import annotations

from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.compute as pc

TYPES = {"int64", "double", "string", "bool", "date32"}


@dataclass
class TableInfo:
    """A registered table: its data, row count and distinct values per column."""

    name: str
    data: pa.Table
    rows: int
    ndv: dict[str, int] = field(default_factory=dict)

    def column_type(self, column: str) -> str:
        """Engine type name of a column."""
        return arrow_type_name(self.data.schema.field(column).type)


def arrow_type_name(t: pa.DataType) -> str:
    """Map an Arrow type to the engine's small type system."""
    if pa.types.is_integer(t):
        return "int64"
    if pa.types.is_floating(t) or pa.types.is_decimal(t):
        return "double"
    if pa.types.is_string(t) or pa.types.is_large_string(t):
        return "string"
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_date32(t):
        return "date32"
    raise TypeError(f"unsupported column type {t}")


class Catalog:
    """Name to table mapping; statistics are computed once at registration."""

    def __init__(self) -> None:
        self._tables: dict[str, TableInfo] = {}

    def register(self, name: str, data: pa.Table) -> None:
        """Add a table and compute row count and distinct counts (checking every type is supported)."""
        for f in data.schema:
            arrow_type_name(f.type)
        ndv = {
            f.name: max(1, int(pc.count_distinct(data.column(f.name)).as_py())) for f in data.schema
        }
        self._tables[name.lower()] = TableInfo(name.lower(), data, data.num_rows, ndv)

    def get(self, name: str) -> TableInfo | None:
        """The table, or None."""
        return self._tables.get(name.lower())

    def names(self) -> list[str]:
        """Registered table names."""
        return sorted(self._tables)
