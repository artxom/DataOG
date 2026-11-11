"""DataOG 跨方言标识符、类型转换、DWS 语法与四层视图血缘穿透公共包."""

from core.platform.dialects.ddl_builder import (
    build_dialect_ddl,
    format_readable_sql_ddl,
)
from core.platform.dialects.dws_ddl import (
    find_matching_paren,
    format_partition_boundary_val,
    format_reloptions_with_clause,
    parse_dws_tabledef,
    partition_clause_has_child_defs,
    update_partition_clause_columns,
)
from core.platform.dialects.identifiers import (
    BACKTICK_DIALECTS,
    SYSTEM_SCHEMAS,
    clean_identifier_input,
    format_existing_db_ident,
    quote_backtick_str,
    quote_dialect_ident,
    quote_ident_str,
    quote_literal_str,
    validate_identifier,
)
from core.platform.dialects.types import (
    SUPPORTED_DIALECTS,
    clean_sql_type_input,
    is_cross_type_compatible,
    is_generic_or_unbounded_type,
    normalize_cross_type,
    normalize_dws_type,
    translate_column_type,
    validate_sql_type,
)
from core.platform.dialects.view_lineage import (
    enrich_view_columns_by_lineage,
    infer_sql_expr_type,
    parse_view_sql_lineage,
    split_top_level_select_items_with_comments,
    strip_sql_comments_preserve_literals,
)

__all__ = [
    "BACKTICK_DIALECTS",
    "SUPPORTED_DIALECTS",
    "SYSTEM_SCHEMAS",
    "build_dialect_ddl",
    "clean_identifier_input",
    "clean_sql_type_input",
    "enrich_view_columns_by_lineage",
    "find_matching_paren",
    "format_existing_db_ident",
    "format_partition_boundary_val",
    "format_readable_sql_ddl",
    "format_reloptions_with_clause",
    "infer_sql_expr_type",
    "is_cross_type_compatible",
    "is_generic_or_unbounded_type",
    "normalize_cross_type",
    "normalize_dws_type",
    "parse_dws_tabledef",
    "parse_view_sql_lineage",
    "partition_clause_has_child_defs",
    "quote_backtick_str",
    "quote_dialect_ident",
    "quote_ident_str",
    "quote_literal_str",
    "split_top_level_select_items_with_comments",
    "strip_sql_comments_preserve_literals",
    "translate_column_type",
    "update_partition_clause_columns",
    "validate_identifier",
    "validate_sql_type",
]
