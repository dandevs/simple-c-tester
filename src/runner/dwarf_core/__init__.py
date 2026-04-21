from .api import DwarfCoreApi, create_dwarf_core_api
from .models import (
    DwarfAddressLookupRequest,
    DwarfAddressLookupResponse,
    DwarfAddressRange,
    DwarfCompilationUnit,
    DwarfCoreError,
    DwarfLineIndex,
    DwarfLineEntry,
    DwarfLoaderRequest,
    DwarfLoaderResponse,
    DwarfLocationList,
    DwarfLocationListsResponse,
    DwarfLocationRange,
    DwarfResolveRequest,
    DwarfResolveResponse,
    DwarfSourceLocation,
    ResolvedVariableAnnotation,
    SourceExpressionParseRequest,
    SourceExpressionParseResponse,
)
from .line_index import build_line_index, lookup_address
from .loader import load_dwarf_data
from .location_lists import is_location_live_at_address, load_location_lists
from .resolver import resolve_inline_annotations
from .resolver_models import DwarfResolverInput, DwarfResolverOutput
from .source_parser import (
    extract_source_expressions,
    normalize_member_access_expression,
    parse_source_expression_match,
)
from .source_parser_models import (
    SourceExpressionIdentifier,
    SourceExpressionMatch,
    SourceExpressionSpan,
)


__all__ = [
    "DwarfCoreApi",
    "DwarfAddressLookupRequest",
    "DwarfAddressLookupResponse",
    "DwarfAddressRange",
    "DwarfCompilationUnit",
    "DwarfCoreError",
    "DwarfLineIndex",
    "DwarfLineEntry",
    "DwarfLoaderRequest",
    "DwarfLoaderResponse",
    "DwarfLocationList",
    "DwarfLocationListsResponse",
    "DwarfLocationRange",
    "DwarfResolveRequest",
    "DwarfResolveResponse",
    "DwarfResolverInput",
    "DwarfResolverOutput",
    "DwarfSourceLocation",
    "ResolvedVariableAnnotation",
    "SourceExpressionParseRequest",
    "SourceExpressionParseResponse",
    "build_line_index",
    "lookup_address",
    "SourceExpressionIdentifier",
    "SourceExpressionMatch",
    "SourceExpressionSpan",
    "extract_source_expressions",
    "create_dwarf_core_api",
    "is_location_live_at_address",
    "load_dwarf_data",
    "load_location_lists",
    "normalize_member_access_expression",
    "parse_source_expression_match",
    "resolve_inline_annotations",
]
