from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Generator

try:
    from elftools.common.exceptions import ELFError
    from elftools.elf.elffile import ELFFile
    from elftools.dwarf.die import DIE

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime environment dependent
    ELFError = Exception
    ELFFile = None
    DIE = None
    _PYELFTOOLS_AVAILABLE = False


@dataclass(frozen=True)
class DwarfTypeInfo:
    kind: str
    name: str
    size: int = 0
    enum_values: dict[int, str] = field(default_factory=dict)
    members: tuple[tuple[str, "DwarfTypeInfo | None"], ...] = ()
    element_type: "DwarfTypeInfo | None" = None
    dimensions: tuple[tuple[int, int], ...] = ()
    pointed_to_type: "DwarfTypeInfo | None" = None


_fallback_type_index_cache: dict[str, dict[tuple[str, str, int], DwarfTypeInfo]] = {}


def resolve_variable_type(
    binary_path: str,
    variable_name: str,
    file_path: str = "",
    line: int = 0,
    cache=None,
) -> DwarfTypeInfo | None:
    if not binary_path or not variable_name:
        return None

    abs_binary = os.path.abspath(binary_path)

    if cache is not None:
        if abs_binary not in cache.type_index_cache:
            cache.type_index_cache[abs_binary] = _build_type_index(abs_binary)
        index = cache.type_index_cache[abs_binary]
    else:
        if abs_binary not in _fallback_type_index_cache:
            _fallback_type_index_cache[abs_binary] = _build_type_index(abs_binary)
        index = _fallback_type_index_cache[abs_binary]

    if file_path:
        abs_file = os.path.abspath(file_path)
        result = index.get((variable_name, abs_file, line))
        if result is not None:
            return result

    return index.get((variable_name, "", 0))


def _build_type_index(binary_path: str) -> dict[tuple[str, str, int], DwarfTypeInfo]:
    index: dict[tuple[str, str, int], DwarfTypeInfo] = {}
    if not _PYELFTOOLS_AVAILABLE or ELFFile is None:
        return index
    if not os.path.isfile(binary_path):
        return index

    try:
        with open(binary_path, "rb") as binary_file:
            elf_file = ELFFile(binary_file)
            if not elf_file.has_dwarf_info():
                return index
            dwarf_info = elf_file.get_dwarf_info()
            for compile_unit in dwarf_info.iter_CUs():
                try:
                    top_die = compile_unit.get_top_DIE()
                    cu_file = _die_attr_file_path(top_die, "DW_AT_name")
                    cu_dir = _die_attr_str(top_die, "DW_AT_comp_dir")
                    if cu_file and cu_dir and not os.path.isabs(cu_file):
                        cu_file = os.path.join(cu_dir, cu_file)
                        cu_file = os.path.abspath(cu_file)
                    for name, var_file, var_line, type_die in _walk_die_for_variables(
                        top_die, dwarf_info, cu_file, 0
                    ):
                        type_info = _resolve_type_die(type_die, dwarf_info)
                        if type_info is None:
                            continue
                        name_key = (name, "", 0)
                        if name_key not in index:
                            index[name_key] = type_info
                        if var_file:
                            index[(name, var_file, var_line)] = type_info
                except Exception:
                    continue
    except Exception:
        pass

    return index


def _walk_die_for_variables(
    die: DIE,
    dwarf_info,
    default_file: str = "",
    default_line: int = 0,
) -> Generator[tuple[str, str, int, DIE], None, None]:
    tag = die.tag
    file_path = default_file
    line = default_line

    if tag == "DW_TAG_subprogram":
        file_path = _die_attr_file_path(die, "DW_AT_decl_file") or default_file
        line = _die_attr_int(die, "DW_AT_decl_line") or default_line

    if tag in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
        name = _die_attr_str(die, "DW_AT_name")
        if name:
            var_file = _die_attr_file_path(die, "DW_AT_decl_file") or file_path
            var_line = _die_attr_int(die, "DW_AT_decl_line") or line or 0
            type_die = _get_referenced_die(die, dwarf_info, "DW_AT_type")
            if type_die is not None:
                yield name, var_file, var_line, type_die

    for child in die.iter_children():
        yield from _walk_die_for_variables(child, dwarf_info, file_path, line)


def _resolve_type_die(die: DIE, dwarf_info, _visited: set[int] | None = None) -> DwarfTypeInfo | None:
    if die is None:
        return None
    visited = _visited if _visited is not None else set()
    offset = int(getattr(die, "offset", 0))
    if offset in visited:
        return None
    visited.add(offset)
    tag = die.tag
    if tag == "DW_TAG_typedef":
        return _resolve_type_die(_get_referenced_die(die, dwarf_info, "DW_AT_type"), dwarf_info, visited)
    if tag == "DW_TAG_const_type":
        return _resolve_type_die(_get_referenced_die(die, dwarf_info, "DW_AT_type"), dwarf_info, visited)
    if tag == "DW_TAG_volatile_type":
        return _resolve_type_die(_get_referenced_die(die, dwarf_info, "DW_AT_type"), dwarf_info, visited)
    if tag == "DW_TAG_base_type":
        return _parse_base_type(die)
    if tag == "DW_TAG_pointer_type":
        return _parse_pointer_type(die, dwarf_info, visited)
    if tag == "DW_TAG_enumeration_type":
        return _parse_enumeration_type(die)
    if tag == "DW_TAG_structure_type":
        return _parse_structure_type(die, dwarf_info, visited)
    if tag == "DW_TAG_array_type":
        return _parse_array_type(die, dwarf_info, visited)
    ref = _get_referenced_die(die, dwarf_info, "DW_AT_type")
    if ref is not None:
        return _resolve_type_die(ref, dwarf_info, visited)
    return None


def _parse_base_type(die: DIE) -> DwarfTypeInfo:
    return DwarfTypeInfo(
        kind="base",
        name=_die_attr_str(die, "DW_AT_name"),
        size=_die_attr_int(die, "DW_AT_byte_size") or 0,
    )


def _parse_pointer_type(die: DIE, dwarf_info, visited: set[int]) -> DwarfTypeInfo:
    pointed_to = _resolve_type_die(
        _get_referenced_die(die, dwarf_info, "DW_AT_type"), dwarf_info, visited
    )
    return DwarfTypeInfo(
        kind="pointer",
        name=_die_attr_str(die, "DW_AT_name") or "pointer",
        size=_die_attr_int(die, "DW_AT_byte_size") or 0,
        pointed_to_type=pointed_to,
    )


def _parse_enumeration_type(die: DIE) -> DwarfTypeInfo:
    values: dict[int, str] = {}
    try:
        for child in die.iter_children():
            if child.tag == "DW_TAG_enumerator":
                enum_name = _die_attr_str(child, "DW_AT_name")
                enum_val = _die_attr_int(child, "DW_AT_const_value")
                if enum_name and enum_val is not None:
                    values[enum_val] = enum_name
    except Exception:
        pass
    return DwarfTypeInfo(
        kind="enum",
        name=_die_attr_str(die, "DW_AT_name") or "enum",
        size=_die_attr_int(die, "DW_AT_byte_size") or 0,
        enum_values=values,
    )


def _parse_structure_type(die: DIE, dwarf_info, visited: set[int]) -> DwarfTypeInfo:
    members: list[tuple[str, DwarfTypeInfo | None]] = []
    try:
        for child in die.iter_children():
            if child.tag == "DW_TAG_member":
                member_name = _die_attr_str(child, "DW_AT_name")
                member_type = _resolve_type_die(
                    _get_referenced_die(child, dwarf_info, "DW_AT_type"), dwarf_info, visited
                )
                members.append((member_name, member_type))
    except Exception:
        pass
    return DwarfTypeInfo(
        kind="struct",
        name=_die_attr_str(die, "DW_AT_name") or "struct",
        size=_die_attr_int(die, "DW_AT_byte_size") or 0,
        members=tuple(members),
    )


def _parse_array_type(die: DIE, dwarf_info, visited: set[int]) -> DwarfTypeInfo:
    element_type = _resolve_type_die(
        _get_referenced_die(die, dwarf_info, "DW_AT_type"), dwarf_info, visited
    )
    dimensions: list[tuple[int, int]] = []
    try:
        for child in die.iter_children():
            if child.tag == "DW_TAG_subrange_type":
                lower = _die_attr_int(child, "DW_AT_lower_bound")
                if lower is None:
                    lower = 0
                upper = _die_attr_int(child, "DW_AT_upper_bound")
                if upper is None:
                    count = _die_attr_int(child, "DW_AT_count")
                    upper = lower + count - 1 if count is not None else -1
                dimensions.append((lower, upper))
    except Exception:
        pass
    return DwarfTypeInfo(
        kind="array",
        name=_die_attr_str(die, "DW_AT_name") or "array",
        size=_die_attr_int(die, "DW_AT_byte_size") or 0,
        element_type=element_type,
        dimensions=tuple(dimensions),
    )


def _get_referenced_die(die: DIE, dwarf_info, attr_name: str) -> DIE | None:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return None
    try:
        return die.get_DIE_from_attribute(attr_name)
    except Exception:
        return None


def _die_attr_str(die: DIE, attr_name: str) -> str:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return ""
    value = getattr(attr, "value", b"")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value) if value is not None else ""


def _die_attr_int(die: DIE, attr_name: str) -> int | None:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return None
    try:
        return int(attr.value)
    except (TypeError, ValueError):
        return None


def _die_attr_file_path(die: DIE, attr_name: str) -> str:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return ""
    value = getattr(attr, "value", b"")
    if isinstance(value, bytes):
        decoded = value.decode("utf-8", errors="replace")
        return os.path.abspath(decoded) if decoded else ""
    if isinstance(value, str):
        return os.path.abspath(value) if value else ""
    return ""
