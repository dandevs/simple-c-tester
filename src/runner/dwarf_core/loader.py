import os

from .line_index import build_line_index
from .lexical_scopes import build_lexical_scope_index
from .models import (
    DwarfCompilationUnit,
    DwarfCoreError,
    DwarfLineEntry,
    DwarfLoaderRequest,
    DwarfLoaderResponse,
    DwarfScopeIndex,
    DwarfSourceLocation,
)
from .variable_scopes import build_scope_index

try:
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime environment dependent
    ELFError = Exception
    ELFFile = None
    _PYELFTOOLS_AVAILABLE = False


def load_dwarf_data(request: DwarfLoaderRequest) -> DwarfLoaderResponse:
    if not request.binary_path:
        return _error_response(
            code="missing_binary_path",
            message="binary_path is required",
        )

    binary_path = os.path.abspath(request.binary_path)
    if not os.path.isfile(binary_path):
        return _error_response(
            code="binary_not_found",
            message="binary path does not exist",
            details={"binary_path": binary_path},
        )

    if not _PYELFTOOLS_AVAILABLE or ELFFile is None:
        return _error_response(
            code="pyelftools_unavailable",
            message="pyelftools is unavailable",
            details={"binary_path": binary_path},
            pyelftools_available=False,
            dwarf_info_available=False,
        )

    try:
        with open(binary_path, "rb") as binary_file:
            elf_file = ELFFile(binary_file)
            if not elf_file.has_dwarf_info():
                return _error_response(
                    code="dwarf_unavailable",
                    message="binary has no DWARF info",
                    details={"binary_path": binary_path},
                    dwarf_info_available=False,
                )

            dwarf_info = elf_file.get_dwarf_info()
            compilation_units = _collect_compilation_units(dwarf_info)
            line_index = build_line_index(compilation_units)
            scope_index = _build_scope_index(dwarf_info, line_index)
            lexical_scope_index = build_lexical_scope_index(line_index, dwarf_info)
            return DwarfLoaderResponse(
                ok=True,
                compilation_units=compilation_units,
                line_index=line_index,
                scope_index=scope_index,
                lexical_scope_index=lexical_scope_index,
                pyelftools_available=True,
                dwarf_info_available=True,
            )
    except OSError as error:
        return _error_response(
            code="binary_read_failed",
            message="failed to read binary",
            details={"binary_path": binary_path, "reason": str(error)},
        )
    except ELFError as error:
        return _error_response(
            code="elf_parse_failed",
            message="failed to parse ELF binary",
            details={"binary_path": binary_path, "reason": str(error)},
            dwarf_info_available=False,
        )
    except Exception as error:  # pragma: no cover - defensive fallback
        return _error_response(
            code="dwarf_load_failed",
            message="failed to load DWARF data",
            details={"binary_path": binary_path, "reason": str(error)},
            dwarf_info_available=False,
        )


def _build_scope_index(dwarf_info, line_index) -> DwarfScopeIndex:
    try:
        return build_scope_index(line_index, dwarf_info)
    except Exception:
        return DwarfScopeIndex()


def _collect_compilation_units(dwarf_info) -> tuple[DwarfCompilationUnit, ...]:
    compilation_units: list[DwarfCompilationUnit] = []
    for compile_unit in dwarf_info.iter_CUs():
        line_entries = _collect_line_entries(dwarf_info, compile_unit)
        unit_name, comp_dir = _read_cu_metadata(compile_unit)
        compilation_units.append(
            DwarfCompilationUnit(
                cu_offset=int(getattr(compile_unit, "cu_offset", 0)),
                unit_name=unit_name,
                comp_dir=comp_dir,
                line_entries=line_entries,
            )
        )
    return tuple(compilation_units)


def _collect_line_entries(dwarf_info, compile_unit) -> tuple[DwarfLineEntry, ...]:
    try:
        line_program = dwarf_info.line_program_for_CU(compile_unit)
    except Exception:
        return ()

    if line_program is None:
        return ()

    entries: list[DwarfLineEntry] = []
    for program_entry in line_program.get_entries():
        state = getattr(program_entry, "state", None)
        if state is None:
            continue

        location = _resolve_source_location(line_program, state)
        entries.append(
            DwarfLineEntry(
                address=int(getattr(state, "address", 0)),
                location=location,
                is_stmt=bool(getattr(state, "is_stmt", False)),
                basic_block=bool(getattr(state, "basic_block", False)),
                end_sequence=bool(getattr(state, "end_sequence", False)),
            )
        )

    return tuple(entries)


def _resolve_source_location(line_program, state) -> DwarfSourceLocation:
    file_path = _line_program_file_path(line_program, int(getattr(state, "file", 0)))
    line_number = int(getattr(state, "line", 0) or 0)
    column_number = int(getattr(state, "column", 0) or 0)
    return DwarfSourceLocation(file_path=file_path, line=line_number, column=column_number)


def _line_program_file_path(line_program, file_index: int) -> str:
    header = getattr(line_program, "header", None)
    if header is None or file_index < 0:
        return ""

    file_entries = _header_get(header, "file_entry")
    version = int(getattr(header, "version", 4))
    normalized_index = file_index - 1 if version < 5 else file_index
    if normalized_index < 0 or normalized_index >= len(file_entries):
        return ""

    file_entry = file_entries[normalized_index]
    filename = _decode_value(getattr(file_entry, "name", b""))
    if not filename:
        return ""

    dir_index = int(getattr(file_entry, "dir_index", 0) or 0)
    include_dirs = _header_get(header, "include_directory")
    normalized_dir_index = _normalize_dir_index(dir_index, version)
    if normalized_dir_index < 0 or normalized_dir_index >= len(include_dirs):
        return filename

    directory = _decode_value(include_dirs[normalized_dir_index])
    if not directory:
        return filename
    return os.path.join(directory, filename)


def _normalize_dir_index(dir_index: int, dwarf_version: int) -> int:
    if dwarf_version < 5:
        return dir_index - 1
    return dir_index


def _read_cu_metadata(compile_unit) -> tuple[str, str]:
    try:
        top_die = compile_unit.get_top_DIE()
    except Exception:
        return "", ""
    attributes = getattr(top_die, "attributes", {})
    unit_name = _decode_attr(attributes, "DW_AT_name")
    comp_dir = _decode_attr(attributes, "DW_AT_comp_dir")
    return unit_name, comp_dir


def _header_get(header, key: str):
    if hasattr(header, "get"):
        try:
            value = header.get(key)
            return value if value is not None else []
        except Exception:
            pass
    try:
        value = header[key]
        return value if value is not None else []
    except Exception:
        return []


def _decode_attr(attributes, key: str) -> str:
    attribute = attributes.get(key)
    if attribute is None:
        return ""
    return _decode_value(getattr(attribute, "value", b""))


def _decode_value(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value) if value else ""


def _error_response(
    *,
    code: str,
    message: str,
    details: dict[str, str] | None = None,
    pyelftools_available: bool = True,
    dwarf_info_available: bool = True,
) -> DwarfLoaderResponse:
    return DwarfLoaderResponse(
        ok=False,
        compilation_units=(),
        pyelftools_available=pyelftools_available,
        dwarf_info_available=dwarf_info_available,
        error=DwarfCoreError(code=code, message=message, details=details or {}),
    )
