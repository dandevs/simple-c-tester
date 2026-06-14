from __future__ import annotations

import os
from bisect import bisect_right
from dataclasses import dataclass
from typing import Generator

from .loader import (
    _decode_value,
    _line_program_file_path,
)
from .variable_scopes import _die_attr_high_pc, _die_attr_int, _die_attr_str

try:
    from elftools.elf.elffile import ELFFile

    _PYELFTOOLS_AVAILABLE = True
except ImportError:  # pragma: no cover - runtime environment dependent
    ELFFile = None
    _PYELFTOOLS_AVAILABLE = False


_NON_USER_PREFIXES = (
    "/usr/",
    "/lib/",
    "/lib64/",
    "/opt/",
    "/nix/",
)

_fallback_function_index_cache: dict[str, FunctionIndex] = {}


@dataclass(frozen=True)
class FunctionEntry:
    name: str
    start_pc: int
    end_pc: int
    file_path: str = ""
    line: int = 0
    is_user_function: bool = False


@dataclass(frozen=True)
class FunctionIndex:
    entries: tuple[FunctionEntry, ...] = ()
    start_pcs: tuple[int, ...] = ()
    user_function_names: tuple[str, ...] = ()

    def get_function_for_pc(self, pc: int) -> FunctionEntry | None:
        if not self.entries or not self.start_pcs:
            return None
        idx = bisect_right(self.start_pcs, pc) - 1
        if idx < 0:
            return None
        entry = self.entries[idx]
        if pc < entry.end_pc:
            return entry
        return None

    def is_user_function(self, name: str) -> bool:
        return name in self.user_function_names


def get_function_index(binary_path: str, cache=None) -> FunctionIndex:
    if not binary_path:
        return FunctionIndex()
    abs_path = os.path.abspath(binary_path)
    cache_dict = cache.function_index_cache if cache is not None else _fallback_function_index_cache
    cached = cache_dict.get(abs_path)
    if cached is not None:
        return cached
    if not os.path.isfile(abs_path):
        return FunctionIndex()
    if not _PYELFTOOLS_AVAILABLE or ELFFile is None:
        return FunctionIndex()
    try:
        with open(abs_path, "rb") as binary_file:
            elf_file = ELFFile(binary_file)
            if not elf_file.has_dwarf_info():
                cache_dict[abs_path] = FunctionIndex()
                return FunctionIndex()
            dwarf_info = elf_file.get_dwarf_info()
            index = _build_function_index(dwarf_info)
            cache_dict[abs_path] = index
            return index
    except Exception:
        return FunctionIndex()


def _build_function_index(dwarf_info) -> FunctionIndex:
    entries: list[FunctionEntry] = []
    try:
        for compile_unit in dwarf_info.iter_CUs():
            top_die = compile_unit.get_top_DIE()
            cu_comp_dir = _die_attr_str(top_die, "DW_AT_comp_dir")
            line_program = _get_line_program(dwarf_info, compile_unit)
            for die in top_die.iter_children():
                for entry in _walk_subprograms(die, cu_comp_dir, line_program):
                    entries.append(entry)
    except Exception:
        return FunctionIndex()

    if not entries:
        return FunctionIndex()

    entries.sort(key=lambda e: e.start_pc)
    start_pcs = tuple(e.start_pc for e in entries)
    user_names = tuple(sorted({e.name for e in entries if e.is_user_function}))
    return FunctionIndex(
        entries=tuple(entries),
        start_pcs=start_pcs,
        user_function_names=user_names,
    )


def _walk_subprograms(
    die,
    cu_comp_dir: str,
    line_program,
) -> Generator[FunctionEntry, None, None]:
    if die.tag == "DW_TAG_subprogram":
        entry = _parse_subprogram(die, cu_comp_dir, line_program)
        if entry:
            yield entry
    for child in die.iter_children():
        yield from _walk_subprograms(child, cu_comp_dir, line_program)


def _parse_subprogram(die, cu_comp_dir: str, line_program) -> FunctionEntry | None:
    name = _die_attr_str(die, "DW_AT_name")
    if not name:
        return None
    low_pc = _die_attr_int(die, "DW_AT_low_pc")
    high_pc = _die_attr_high_pc(die)
    if low_pc is None or high_pc is None or low_pc >= high_pc:
        return None
    file_path = _resolve_decl_file(die, line_program, cu_comp_dir)
    line = _die_attr_int(die, "DW_AT_decl_line") or 0
    is_user = not any(file_path.startswith(p) for p in _NON_USER_PREFIXES)
    return FunctionEntry(
        name=name,
        start_pc=low_pc,
        end_pc=high_pc,
        file_path=file_path,
        line=line,
        is_user_function=is_user,
    )


def _get_line_program(dwarf_info, compile_unit):
    try:
        return dwarf_info.line_program_for_CU(compile_unit)
    except Exception:
        return None


def _resolve_decl_file(die, line_program, cu_comp_dir: str) -> str:
    decl_file = die.attributes.get("DW_AT_decl_file")
    if decl_file is None:
        return cu_comp_dir or ""
    value = getattr(decl_file, "value", b"")
    try:
        file_index = int(value)
    except (TypeError, ValueError):
        path = _decode_value(value)
        return (
            os.path.join(cu_comp_dir, path)
            if cu_comp_dir and not os.path.isabs(path)
            else path
        )
    if line_program is None:
        return cu_comp_dir or ""
    resolved = _line_program_file_path(line_program, file_index)
    return resolved if resolved else (cu_comp_dir or "")
