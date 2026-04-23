from dataclasses import dataclass, field

from .source_parser_models import SourceExpressionMatch


@dataclass(frozen=True)
class DwarfCoreError:
    code: str
    message: str
    details: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DwarfSourceLocation:
    file_path: str = ""
    line: int = 0
    column: int = 0


@dataclass(frozen=True)
class DwarfLineEntry:
    address: int
    location: DwarfSourceLocation
    is_stmt: bool = False
    basic_block: bool = False
    end_sequence: bool = False


@dataclass(frozen=True)
class DwarfCompilationUnit:
    cu_offset: int
    unit_name: str = ""
    comp_dir: str = ""
    line_entries: tuple[DwarfLineEntry, ...] = ()


@dataclass(frozen=True)
class DwarfAddressRange:
    start_address: int
    end_address: int
    location: DwarfSourceLocation
    cu_offset: int = 0


@dataclass(frozen=True)
class DwarfLineIndex:
    ranges: tuple[DwarfAddressRange, ...] = ()
    start_addresses: tuple[int, ...] = ()


@dataclass(frozen=True)
class DwarfVariableLiveRange:
    """A single live range for a variable (from location list or scope)."""
    name: str
    low_pc: int
    high_pc: int
    file_path: str = ""
    line: int = 0


@dataclass(frozen=True)
class DwarfScopeIndex:
    """Maps file_path → line_number → tuple of variable names alive at that line."""
    file_lines: dict[str, dict[int, tuple[str, ...]]] = field(default_factory=dict)


@dataclass(frozen=True)
class DwarfLoaderRequest:
    binary_path: str


@dataclass(frozen=True)
class DwarfLoaderResponse:
    ok: bool
    compilation_units: tuple[DwarfCompilationUnit, ...] = ()
    line_index: DwarfLineIndex = DwarfLineIndex()
    scope_index: DwarfScopeIndex = DwarfScopeIndex()
    pyelftools_available: bool = True
    dwarf_info_available: bool = True
    error: DwarfCoreError | None = None


@dataclass(frozen=True)
class DwarfAddressLookupRequest:
    line_index: DwarfLineIndex
    address: int


@dataclass(frozen=True)
class DwarfAddressLookupResponse:
    ok: bool
    location: DwarfSourceLocation | None = None
    error: DwarfCoreError | None = None


@dataclass(frozen=True)
class DwarfLocationRange:
    begin_address: int
    end_address: int
    expression: str = ""


@dataclass(frozen=True)
class DwarfLocationList:
    offset: int
    ranges: tuple[DwarfLocationRange, ...] = ()


@dataclass(frozen=True)
class DwarfLocationListsResponse:
    ok: bool
    location_lists: tuple[DwarfLocationList, ...] = ()
    error: DwarfCoreError | None = None


@dataclass(frozen=True)
class SourceExpressionParseRequest:
    expression: str = ""
    source_line: str = ""


@dataclass(frozen=True)
class SourceExpressionParseResponse:
    ok: bool
    normalized_expression: str = ""
    variables: tuple[str, ...] = ()
    tokens: tuple[str, ...] = ()
    expressions: tuple[SourceExpressionMatch, ...] = ()
    error: DwarfCoreError | None = None


@dataclass(frozen=True)
class DwarfResolveRequest:
    binary_path: str
    address: int
    file_path: str = ""
    line: int = 0
    runtime_variables: tuple[tuple[str, str, str], ...] = ()
    source_line: str = ""
    program_counter: int = 0


@dataclass(frozen=True)
class ResolvedVariableAnnotation:
    name: str
    value: str
    source_expression: str = ""
    is_live: bool = False
    liveness_status: str = "unknown"
    liveness_assumed: bool = True
    availability: str = "available"


@dataclass(frozen=True)
class DwarfResolveResponse:
    ok: bool
    location: DwarfSourceLocation | None = None
    annotations: tuple[ResolvedVariableAnnotation, ...] = ()
    availability: str = "available"
    dwarf_available: bool = True
    symbols_available: bool = True
    error: DwarfCoreError | None = None
