"""DAX/TMDL identifier escaping helpers.

Power BI table and column names may contain spaces, single quotes, or other
characters that are not safe to splice directly into generated DAX or TMDL
text. Splicing them raw can either produce invalid syntax (names with spaces)
or, in the worst case, let a name break out of its quoted context (names
containing a single quote ``'``) -- analogous to an injection.

These helpers centralise the quoting rules so every code path that emits DAX
or TMDL goes through one place. The two key rules are:

* In DAX, a table name is wrapped in single quotes and any embedded ``'`` is
  doubled: ``My Table`` -> ``'My Table'``, ``Bob's`` -> ``'Bob''s'``.
* In TMDL, column references inside ``fromColumn:``/``toColumn:`` use the
  bracket form ``'Table'[Column]``.

Every helper returns a *plain string* ready to be embedded in generated text.
The helpers never raise on unusual names; they only guarantee the result is
syntactically valid for the target language.
"""

from __future__ import annotations


def _escape_single_quotes(name: str) -> str:
    """Double every single quote in ``name`` (the DAX/TMDL escape rule)."""
    return name.replace("'", "''")


def quote_dax_table(name: str) -> str:
    """Return ``name`` wrapped in single quotes, safe for DAX.

    ``"Sales"``        -> ``"'Sales'"``
    ``"Bob's Table"``  -> ``"'Bob''s Table'"``
    """
    return f"'{_escape_single_quotes(name)}'"


def quote_dax_column(table: str, column: str) -> str:
    """Return a fully-qualified DAX column reference ``'Table'[Column]``.

    The table is single-quote-escaped; the column stays inside ``[]`` which is
    already safe for any column name in DAX.

    ``("Sales", "Amount")`` -> ``"'Sales'[Amount]"``
    """
    return f"{quote_dax_table(table)}[{column}]"


def quote_dax_measure(measure: str) -> str:
    """Return a DAX measure reference ``[Measure]``.

    Measure names live inside ``[]`` which is already safe in DAX.
    """
    return f"[{measure}]"


def quote_tmdl_identifier(name: str) -> str:
    """Return ``name`` wrapped in single quotes, safe for a TMDL identifier.

    TMDL uses the same single-quote-doubling rule as DAX for table/column
    identifiers that contain spaces or special characters.
    """
    return f"'{_escape_single_quotes(name)}'"


def escape_tmdl_string(value: str) -> str:
    """Escape an arbitrary string for embedding in a TMDL string literal.

    TMDL string literals are single-quoted and ``'`` is doubled.
    """
    return _escape_single_quotes(value)


def tmdl_column_ref(table: str, column: str) -> str:
    """Return a TMDL ``fromColumn:``/``toColumn:`` reference ``'Table'[Column]``."""
    return f"{quote_tmdl_identifier(table)}[{column}]"
