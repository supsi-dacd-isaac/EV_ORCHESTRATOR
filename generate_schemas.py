#!/usr/bin/env python3
"""
Generate Pydantic v2 schemas from SQLAlchemy declarative models.

Usage:
  python generate_schemas.py --models-module app.models --out app/schemas/database.py
Optional:
  --include-relationships          include simple FK-id fields (no nested models)
  --include-tables users,orders    only include these tables
  --exclude-tables audit_log,...   skip these tables
  --base-class-name Base           declarative base variable name (default: auto)
"""

from __future__ import annotations
import argparse
import importlib
import inspect as pyinspect
import sys
from typing import Any, Dict, List, Optional, Tuple

from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID


# --- Type mapping helpers ----------------------------------------------------

from sqlalchemy import inspect as sqla_inspect
from sqlalchemy.orm import DeclarativeMeta, Mapper, ColumnProperty, RelationshipProperty
from sqlalchemy.schema import MetaData
from sqlalchemy.sql.schema import Column
from sqlalchemy.sql.sqltypes import (
    Integer, BigInteger, SmallInteger, Numeric, Float, DECIMAL,
    String, Text, Unicode, UnicodeText, LargeBinary,
    Boolean, Date, DateTime, Time, JSON as SA_JSON, Uuid
)

from sqlalchemy.dialects.postgresql import UUID as PG_UUID, ARRAY

PY_TYPE_FALLBACKS = {
    Integer: int,
    SmallInteger: int,
    BigInteger: int,
    Float: float,
    Numeric: Decimal,
    DECIMAL: Decimal,
    Boolean: bool,
    Date: date,
    DateTime: datetime,
    Time: time,
    String: str,
    Text: str,
    Unicode: str,
    UnicodeText: str,
    LargeBinary: bytes,
}

def sqlalchemy_to_python_type(col: Column) -> str:
    """
    Return a Python type hint string for a SQLAlchemy Column.
    """
    t = type(col.type)

    # PostgreSQL UUID
    if PG_UUID and isinstance(col.type, Uuid):
        return "UUID"

    # Enums become str by default (custom Enum class → str for schema I/O)
    from sqlalchemy import Enum as SAEnum
    if isinstance(col.type, SAEnum):
        return "str"

    # JSON
    if isinstance(col.type, SA_JSON):
        return "dict | list | None"

    # ARRAY[T] → list[T]
    if ARRAY and isinstance(col.type, ARRAY):
        # best effort inner type
        inner = "Any"
        try:
            inner = sqlalchemy_scalar_type_to_str(col.type.item_type)
        except Exception:
            pass
        return f"list[{inner}]"

    return sqlalchemy_scalar_type_to_str(col.type)

def sqlalchemy_scalar_type_to_str(satype: Any) -> str:
    for klass, py in PY_TYPE_FALLBACKS.items():
        if isinstance(satype, klass):
            return py.__name__
    # ultimate fallback: str
    return "str"

def is_autoincrement_pk(col: Column) -> bool:
    return (
        col.primary_key
        and isinstance(col.type, (Integer, SmallInteger, BigInteger))
        and (col.autoincrement is True or col.server_default is not None)
    )

def snake_to_camel(s: str) -> str:
    return "".join(p.capitalize() or "_" for p in s.split("_"))

# --- Schema text builders ----------------------------------------------------

def build_schema_text(mapper: Mapper, include_relationships: bool = False) -> str:
    table_name = mapper.local_table.name
    class_name = snake_to_camel(table_name)

    cols: List[Column] = []
    for attr in mapper.attrs:
        if isinstance(attr, ColumnProperty):
            for c in attr.columns:
                if c.table is mapper.local_table:
                    cols.append(c)

    create_fields, update_fields, read_fields = [], [], []

    for c in cols:
        pytype = sqlalchemy_to_python_type(c)
        optional = c.nullable and not c.primary_key
        opt_prefix = "Optional[" if optional else ""
        opt_suffix = "]" if optional else ""

        read_fields.append(f"    {c.name}: {opt_prefix}{pytype}{opt_suffix}")

        if not is_autoincrement_pk(c) and not c.primary_key:
            default = " = None" if c.nullable and c.default is None and c.server_default is None else ""
            create_fields.append(f"    {c.name}: {opt_prefix}{pytype}{opt_suffix}{default}")

        update_fields.append(f"    {c.name}: Optional[{pytype}] = None")

    create_model = f"""class {class_name}Create(BaseModel):
{chr(10).join(create_fields) if create_fields else "    pass"}
"""

    update_model = f"""class {class_name}Update(BaseModel):
{chr(10).join(update_fields) if update_fields else "    pass"}
"""

    read_model = f"""class {class_name}Read(BaseModel):
{chr(10).join(read_fields) if read_fields else "    pass"}
    model_config = ConfigDict(from_attributes=True)
"""

    return "\n".join([create_model, update_model, read_model])


def locate_declarative_bases(module):
    """
    Return [(name, BaseClass), ...] for any plausible SQLAlchemy declarative Base.
    Handles:
      - class Base(DeclarativeBase): pass        (2.x)
      - Base = declarative_base()                (1.x / 2.x)
    """
    bases = []
    for name, obj in vars(module).items():
        # Some projects export the Base as a *class* (DeclarativeBase) — metaclass DeclarativeMeta
        if isinstance(obj, type) and isinstance(obj, DeclarativeMeta):
            # Heuristic: mapped classes have __tablename__; base usually doesn't
            if not hasattr(obj, "__tablename__") and hasattr(obj, "registry"):
                bases.append((name, obj))
                continue

        # Some export a base class without DeclarativeMeta check; ensure it looks like a Base
        if isinstance(obj, type) and hasattr(obj, "metadata") and isinstance(getattr(obj, "metadata", None), MetaData):
            if hasattr(obj, "registry"):  # SQLAlchemy 2.x registry present
                bases.append((name, obj))
                continue

    return bases


# --- CLI ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-module", required=True, help="Python import path to SQLAlchemy models (e.g. app.models)")
    ap.add_argument("--out", required=True, help="Output .py file for Pydantic schemas")
    ap.add_argument("--include-relationships", action="store_true", help="Expose simple relationship ids (no nested models)")
    ap.add_argument("--include-tables", default="", help="Comma-separated allowlist of table names")
    ap.add_argument("--exclude-tables", default="", help="Comma-separated blocklist of table names")
    ap.add_argument("--base-class-name", default="", help="Declarative base variable/class name if multiple bases exist")
    args = ap.parse_args()

    mod = importlib.import_module(args.models_module)

    # Find declarative bases
    bases = locate_declarative_bases(mod)
    if not bases:
        print("Could not find a SQLAlchemy declarative Base in the module. "
              "Make sure your models import created a Base = declarative_base().", file=sys.stderr)
        sys.exit(1)

    if args.base_class_name:
        bases = [b for b in bases if b[0] == args.base_class_name]
        if not bases:
            print(f"Base class '{args.base_class_name}' not found in {args.models_module}.", file=sys.stderr)
            sys.exit(1)

    # Use the first (or the selected) base
    base_name, Base = bases[0]

    include = {t.strip() for t in args.include_tables.split(",") if t.strip()}
    exclude = {t.strip() for t in args.exclude_tables.split(",") if t.strip()}

    lines: List[str] = []
    header = """# Auto-generated by generate_schemas.py
from __future__ import annotations
from typing import Optional, Any
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
"""
    lines.append(header)

    # Iterate mappers
    # Note: iterate over all mapped classes in this Base's registry
    for mapper in list(Base.registry.mappers):  # type: ignore[attr-defined]
        if not isinstance(mapper, Mapper):
            continue
        table = mapper.local_table
        if table is None:
            continue
        tname = table.name
        if include and tname not in include:
            continue
        if tname in exclude:
            continue
        lines.append(build_schema_text(mapper, include_relationships=args.include_relationships))

    outpath = args.out
    with open(outpath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).strip() + "\n")
    print(f"Wrote {outpath} with Pydantic schemas.")

if __name__ == "__main__":
    main()
