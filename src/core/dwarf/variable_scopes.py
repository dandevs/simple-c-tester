import os
from bisect import bisect_left
from typing import Generator

from .line_index import lookup_address
from .models import (
    DwarfCoreError,
    DwarfLineIndex,
    DwarfScopeIndex,
    DwarfSourceLocation,
    DwarfVariableLiveRange,
)

try:
    from elftools.common.exceptions import ELFError
    from elftools.dwarf.die import DIE

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ELFError = Exception
    DIE = None
    _PYELFTOOLS_AVAILABLE = False


def build_scope_index(
    line_index: DwarfLineIndex,
    dwarf_info,
) -> DwarfScopeIndex:
    """Build a scope index from DWARF compilation units and location lists.

    For each variable/formal_parameter DIE, extract its live PC ranges from
    DW_AT_location (exprloc or location list), then map those PC ranges to
    source lines using the line index. The result is a mapping of
    file_path → line_number → variable_names alive at that line.
    """
    if not _PYELFTOOLS_AVAILABLE or dwarf_info is None:
        return DwarfScopeIndex()

    live_ranges: list[DwarfVariableLiveRange] = []

    try:
        for compile_unit in dwarf_info.iter_CUs():
            top_die = compile_unit.get_top_DIE()
            cu_comp_dir = _die_attr_str(top_die, "DW_AT_comp_dir")
            for var_range in _walk_die_for_variables(
                top_die,
                dwarf_info,
                line_index,
                cu_comp_dir,
            ):
                live_ranges.append(var_range)
    except Exception:
        # Defensive: if DWARF traversal fails, return empty scope index
        return DwarfScopeIndex()

    # Map live PC ranges to source lines using the line index
    file_line_vars: dict[str, dict[int, set[str]]] = {}

    for var_range in live_ranges:
        # Find all line index ranges that overlap with [low_pc, high_pc)
        overlapping_lines = _lines_for_pc_range(
            line_index,
            var_range.low_pc,
            var_range.high_pc,
        )
        for location in overlapping_lines:
            if not location.file_path or location.line <= 0:
                continue
            abs_path = os.path.abspath(location.file_path)
            if abs_path not in file_line_vars:
                file_line_vars[abs_path] = {}
            line_map = file_line_vars[abs_path]
            if location.line not in line_map:
                line_map[location.line] = set()
            line_map[location.line].add(var_range.name)

    # Build immutable result
    result: dict[str, dict[int, tuple[str, ...]]] = {}
    for file_path, line_map in file_line_vars.items():
        result[file_path] = {
            line: tuple(sorted(names))
            for line, names in sorted(line_map.items())
        }

    return DwarfScopeIndex(file_lines=result)


def _lines_for_pc_range(
    line_index: DwarfLineIndex,
    low_pc: int,
    high_pc: int,
) -> list[DwarfSourceLocation]:
    """Find all source lines whose PC range overlaps [low_pc, high_pc)."""
    if low_pc >= high_pc or not line_index.ranges:
        return []

    # Find the first range that could overlap using binary search
    start_addrs = line_index.start_addresses
    idx = bisect_left(start_addrs, low_pc)
    # Check the previous range too, as it might extend into our range
    if idx > 0:
        idx -= 1

    locations: list[DwarfSourceLocation] = []
    seen: set[tuple[str, int]] = set()

    for i in range(idx, len(line_index.ranges)):
        addr_range = line_index.ranges[i]
        if addr_range.start_address >= high_pc:
            # No more overlapping ranges (ranges are sorted by start)
            break
        if addr_range.end_address <= low_pc:
            continue
        # Overlapping range
        loc = addr_range.location
        key = (loc.file_path, loc.line)
        if key not in seen:
            seen.add(key)
            locations.append(loc)

    return locations


def _walk_die_for_variables(
    die: DIE,
    dwarf_info,
    line_index: DwarfLineIndex,
    cu_comp_dir: str,
    parent_low_pc: int = 0,
    parent_high_pc: int = 0,
) -> Generator[DwarfVariableLiveRange, None, None]:
    """Recursively walk DIEs, tracking scope PC ranges and yielding variable live ranges."""
    tag = die.tag
    low_pc = parent_low_pc
    high_pc = parent_high_pc

    # Update scope bounds for subprograms and lexical blocks
    if tag in ("DW_TAG_subprogram", "DW_TAG_lexical_block", "DW_TAG_inlined_subroutine"):
        new_low = _die_attr_int(die, "DW_AT_low_pc")
        new_high = _die_attr_high_pc(die)
        if new_low is not None and new_high is not None:
            low_pc = new_low
            high_pc = new_high

    if tag in ("DW_TAG_variable", "DW_TAG_formal_parameter"):
        name = _die_attr_str(die, "DW_AT_name")
        if name:
            for live_range in _extract_variable_ranges(die, dwarf_info, low_pc, high_pc):
                yield live_range

    for child in die.iter_children():
        yield from _walk_die_for_variables(
            child, dwarf_info, line_index, cu_comp_dir, low_pc, high_pc
        )


def _extract_variable_ranges(
    die: DIE,
    dwarf_info,
    default_low_pc: int,
    default_high_pc: int,
) -> list[DwarfVariableLiveRange]:
    """Extract live PC ranges for a variable/formal_parameter DIE."""
    loc_attr = die.attributes.get("DW_AT_location")
    if not loc_attr:
        return []

    name = _die_attr_str(die, "DW_AT_name")
    if not name:
        return []

    form = loc_attr.form
    ranges: list[tuple[int, int]] = []

    if form == "DW_FORM_exprloc":
        # Simple expression: variable alive for entire enclosing scope
        if default_low_pc < default_high_pc:
            ranges.append((default_low_pc, default_high_pc))
    elif form in ("DW_FORM_sec_offset", "DW_FORM_loclistx"):
        # Location list: variable may be alive for multiple disjoint ranges
        try:
            loc_lists = dwarf_info.location_lists()
            if loc_lists is not None:
                loc_list = loc_lists.get_location_list_at_offset(
                    loc_attr.value, die=die
                )
                for entry in loc_list:
                    begin = int(getattr(entry, "begin_offset", 0))
                    end = int(getattr(entry, "end_offset", 0))
                    # Skip entries that indicate "optimized out" (empty expression)
                    loc_expr = getattr(entry, "loc_expr", [])
                    if not loc_expr or (len(loc_expr) == 1 and loc_expr[0] == 0):
                        continue
                    ranges.append((begin, end))
        except Exception:
            # Fallback to default scope on parse failure
            if default_low_pc < default_high_pc:
                ranges.append((default_low_pc, default_high_pc))
    else:
        # Unknown form: fallback to default scope
        if default_low_pc < default_high_pc:
            ranges.append((default_low_pc, default_high_pc))

    result: list[DwarfVariableLiveRange] = []
    for low, high in ranges:
        result.append(
            DwarfVariableLiveRange(
                name=name,
                low_pc=low,
                high_pc=high,
            )
        )
    return result


def _die_attr_str(die: DIE, attr_name: str) -> str:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return ""
    value = getattr(attr, "value", b"")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value) if value else ""


def _die_attr_int(die: DIE, attr_name: str) -> int | None:
    attr = die.attributes.get(attr_name)
    if attr is None:
        return None
    try:
        return int(attr.value)
    except (TypeError, ValueError):
        return None


def _die_attr_high_pc(die: DIE) -> int | None:
    """Extract high_pc, handling the case where it's a relative offset from low_pc."""
    high_attr = die.attributes.get("DW_AT_high_pc")
    if high_attr is None:
        return None
    try:
        high_val = int(high_attr.value)
    except (TypeError, ValueError):
        return None

    # DWARF4+ allows high_pc to be an offset from low_pc when form is class_constant
    low_val = _die_attr_int(die, "DW_AT_low_pc")
    if low_val is None:
        return None

    form_class = _form_class(high_attr.form)
    if form_class == "constant":
        return low_val + high_val
    return high_val


def _form_class(form: str) -> str:
    """Return the class of a DWARF form (address, constant, string, etc.)."""
    constant_forms = {
        "DW_FORM_data1",
        "DW_FORM_data2",
        "DW_FORM_data4",
        "DW_FORM_data8",
        "DW_FORM_udata",
        "DW_FORM_sdata",
        "DW_FORM_implicit_const",
    }
    if form in constant_forms:
        return "constant"
    if form in ("DW_FORM_addr", "DW_FORM_addrx"):
        return "address"
    if form in ("DW_FORM_string", "DW_FORM_strp", "DW_FORM_line_strp", "DW_FORM_strx"):
        return "string"
    if form == "DW_FORM_exprloc":
        return "exprloc"
    if form in ("DW_FORM_sec_offset", "DW_FORM_loclistx", "DW_FORM_rnglistx"):
        return "reference"
    return "unknown"
