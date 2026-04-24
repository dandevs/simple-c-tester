from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime environment dependent
    ELFError = Exception
    ELFFile = None
    _PYELFTOOLS_AVAILABLE = False

if TYPE_CHECKING:
    from ..debugger import GdbMIController


@dataclass(frozen=True)
class GlobalVariableEntry:
    name: str
    linkage_name: str
    file_path: str
    line: int
    type_die_offset: int | None = None
    location_expr: bytes | None = None


GlobalVariableIndex = dict[str, GlobalVariableEntry]

_fallback_global_index_cache: dict[str, GlobalVariableIndex] = {}


def get_global_variables(binary_path: str, cache=None) -> GlobalVariableIndex:
    """Load and cache a global-variable index for the given binary path."""
    if not binary_path or not os.path.isfile(binary_path):
        return {}
    abs_path = os.path.abspath(binary_path)
    if cache is not None:
        if abs_path in cache.global_index_cache:
            return cache.global_index_cache[abs_path]
        index = _load_global_index(abs_path)
        cache.global_index_cache[abs_path] = index
        return index
    if abs_path in _fallback_global_index_cache:
        return _fallback_global_index_cache[abs_path]
    index = _load_global_index(abs_path)
    _fallback_global_index_cache[abs_path] = index
    return index


async def evaluate_global(controller: "GdbMIController", var_name: str) -> str | None:
    """Evaluate a global variable by name using the gdb controller."""
    if not var_name:
        return None
    try:
        return await controller.evaluate_expression(var_name)
    except Exception:
        return None


def _load_global_index(binary_path: str) -> GlobalVariableIndex:
    if not _PYELFTOOLS_AVAILABLE or ELFFile is None:
        return {}
    try:
        with open(binary_path, "rb") as f:
            elf = ELFFile(f)
            if not elf.has_dwarf_info():
                return {}
            return _walk_globals(elf)
    except Exception:
        return {}


def _walk_globals(elf_file) -> GlobalVariableIndex:
    index: GlobalVariableIndex = {}
    try:
        dwarf_info = elf_file.get_dwarf_info()
        for compile_unit in dwarf_info.iter_CUs():
            cu_globals = _collect_globals_from_cu(compile_unit, dwarf_info)
            index.update(cu_globals)
    except Exception:
        pass
    return index


def _collect_globals_from_cu(compile_unit, dwarf_info) -> GlobalVariableIndex:
    result: GlobalVariableIndex = {}
    try:
        top_die = compile_unit.get_top_DIE()
    except Exception:
        return result

    cu_comp_dir = _die_attr_str(top_die, "DW_AT_comp_dir")
    line_program = None
    try:
        line_program = dwarf_info.line_program_for_CU(compile_unit)
    except Exception:
        pass

    for die in _walk_die_for_globals(top_die):
        attrs = getattr(die, "attributes", {})
        name = _die_attr_str(die, "DW_AT_name")
        if not name:
            continue

        linkage_name = _die_attr_str(die, "DW_AT_linkage_name")
        if not linkage_name:
            linkage_name = _die_attr_str(die, "DW_AT_MIPS_linkage_name")

        line = _die_attr_int(die, "DW_AT_decl_line") or 0
        file_path = ""
        decl_file = attrs.get("DW_AT_decl_file")
        if decl_file is not None and line_program is not None:
            try:
                file_path = _resolve_decl_file(
                    line_program, decl_file.value, cu_comp_dir
                )
            except Exception:
                pass

        type_die_offset = None
        type_attr = attrs.get("DW_AT_type")
        if type_attr is not None:
            try:
                type_die_offset = int(type_attr.value)
            except Exception:
                pass

        loc_expr = None
        loc_attr = attrs.get("DW_AT_location")
        if loc_attr is not None:
            try:
                if isinstance(loc_attr.value, (bytes, list)):
                    loc_expr = bytes(loc_attr.value)
            except Exception:
                pass

        result[name] = GlobalVariableEntry(
            name=name,
            linkage_name=linkage_name,
            file_path=file_path or cu_comp_dir,
            line=line,
            type_die_offset=type_die_offset,
            location_expr=loc_expr,
        )

    return result


def _walk_die_for_globals(die):
    if die.tag == "DW_TAG_variable":
        attrs = getattr(die, "attributes", {})
        if "DW_AT_external" in attrs or "DW_AT_linkage_name" in attrs:
            yield die
    for child in die.iter_children():
        yield from _walk_die_for_globals(child)


def _resolve_decl_file(line_program, file_index, cu_comp_dir: str) -> str:
    if isinstance(file_index, str):
        return file_index
    try:
        idx = int(file_index)
    except (TypeError, ValueError):
        return ""

    header = getattr(line_program, "header", None)
    if header is None:
        return ""

    entries = _header_get(header, "file_entry")
    version = int(getattr(header, "version", 4))
    nidx = idx - 1 if version < 5 else idx
    if nidx < 0 or nidx >= len(entries):
        return ""

    filename = _decode_value(getattr(entries[nidx], "name", b""))
    if not filename:
        return ""

    dindex = int(getattr(entries[nidx], "dir_index", 0) or 0)
    ndidx = dindex - 1 if version < 5 else dindex
    dirs = _header_get(header, "include_directory")
    if 0 <= ndidx < len(dirs):
        directory = _decode_value(dirs[ndidx])
        if directory:
            return os.path.join(directory, filename)
    if cu_comp_dir:
        return os.path.join(cu_comp_dir, filename)
    return filename


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


def _die_attr_str(die, attr_name: str) -> str:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return ""
    return _decode_value(getattr(attr, "value", b""))


def _die_attr_int(die, attr_name: str) -> int | None:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return None
    try:
        return int(attr.value)
    except (TypeError, ValueError):
        return None


def _decode_value(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value) if value else ""
