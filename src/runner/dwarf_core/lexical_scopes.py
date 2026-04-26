"""Extract DWARF lexical-block boundaries and map them to source line ranges."""

from __future__ import annotations

import os
from typing import Generator

from .line_index import lookup_address
from .models import (
    DwarfAddressLookupRequest,
    DwarfLineIndex,
    DwarfScopeBlock,
    DwarfSourceLocation,
    LexicalScopeIndex,
)
from .variable_scopes import _die_attr_high_pc, _die_attr_int, _die_attr_str

try:
    from elftools.common.exceptions import ELFError
    from elftools.dwarf.die import DIE

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover
    ELFError = Exception
    DIE = None
    _PYELFTOOLS_AVAILABLE = False


def build_lexical_scope_index(
    line_index: DwarfLineIndex,
    dwarf_info,
) -> LexicalScopeIndex:
    """Walk DWARF DIEs and collect every lexical block with its source range."""
    if not _PYELFTOOLS_AVAILABLE or dwarf_info is None:
        return LexicalScopeIndex()

    blocks: list[DwarfScopeBlock] = []

    try:
        for compile_unit in dwarf_info.iter_CUs():
            top_die = compile_unit.get_top_DIE()
            cu_comp_dir = _die_attr_str(top_die, "DW_AT_comp_dir")
            for block in _walk_blocks(
                top_die,
                dwarf_info,
                line_index,
                cu_comp_dir,
                parent_offset=None,
                depth=0,
            ):
                blocks.append(block)
    except Exception:
        return LexicalScopeIndex()

    return LexicalScopeIndex(blocks=tuple(blocks))


def _walk_blocks(
    die: DIE,
    dwarf_info,
    line_index: DwarfLineIndex,
    cu_comp_dir: str,
    parent_offset: int | None,
    depth: int,
) -> Generator[DwarfScopeBlock, None, None]:
    """Recursively walk DIEs, yielding every lexical block / subprogram."""
    tag = die.tag

    if tag in ("DW_TAG_subprogram", "DW_TAG_lexical_block", "DW_TAG_inlined_subroutine"):
        low_pc = _die_attr_int(die, "DW_AT_low_pc")
        high_pc = _die_attr_high_pc(die)
        if low_pc is not None and high_pc is not None and low_pc < high_pc:
            start_loc = _loc_for_pc(line_index, low_pc)
            end_loc = _loc_for_pc(line_index, high_pc - 1)
            func_name = ""
            if tag == "DW_TAG_subprogram":
                func_name = _die_attr_str(die, "DW_AT_name")

            block = DwarfScopeBlock(
                die_offset=int(getattr(die, "offset", 0)),
                parent_die_offset=parent_offset,
                depth=depth,
                low_pc=low_pc,
                high_pc=high_pc,
                tag=tag,
                start_loc=start_loc,
                end_loc=end_loc,
                function_name=func_name,
            )
            yield block
            parent_offset = block.die_offset
            depth = depth + 1 if tag != "DW_TAG_subprogram" else 0

    for child in die.iter_children():
        yield from _walk_blocks(
            child,
            dwarf_info,
            line_index,
            cu_comp_dir,
            parent_offset,
            depth,
        )


def _loc_for_pc(line_index: DwarfLineIndex, pc: int) -> DwarfSourceLocation:
    """Map a single PC to its source location via the line index."""
    result = lookup_address(DwarfAddressLookupRequest(line_index=line_index, address=pc))
    if result.ok and result.location is not None:
        return result.location
    return DwarfSourceLocation()
