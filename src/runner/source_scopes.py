from __future__ import annotations

import bisect
import os
from dataclasses import dataclass, field


_CONTROL_KEYWORDS = ("if", "for", "while", "switch")
_STATEMENT_KEYWORDS = ("if", "for", "while", "switch", "do")


@dataclass
class SourceScopeBlock:
    start_line: int
    end_line: int
    kind: str = "block"
    source_file_path: str = ""
    children: list["SourceScopeBlock"] = field(default_factory=list)
    parent: "SourceScopeBlock | None" = None


@dataclass
class _CachedSourceScopes:
    mtime_ns: int
    size: int
    roots: list[SourceScopeBlock]


_source_scope_cache: dict[str, _CachedSourceScopes] = {}


def get_source_scope_chain(
    file_path: str,
    line_number: int,
    cache=None,
) -> list[SourceScopeBlock]:
    if not file_path or line_number <= 0:
        return []
    roots = _load_source_scopes(file_path, cache=cache)
    if not roots:
        return []
    return _find_best_chain(roots, line_number)


def _load_source_scopes(file_path: str, cache=None) -> list[SourceScopeBlock]:
    abs_path = os.path.abspath(file_path)
    try:
        stat = os.stat(abs_path)
    except OSError:
        return []

    cache_store = cache.lexical_scope_cache if cache is not None else _source_scope_cache
    cached = cache_store.get(abs_path)
    if (
        isinstance(cached, _CachedSourceScopes)
        and cached.mtime_ns == stat.st_mtime_ns
        and cached.size == stat.st_size
    ):
        return cached.roots

    text = _read_source_text(abs_path)
    if not text:
        roots: list[SourceScopeBlock] = []
    else:
        roots = _parse_source_scopes(abs_path, text)
    cache_store[abs_path] = _CachedSourceScopes(
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        roots=roots,
    )
    return roots


def _read_source_text(abs_path: str) -> str:
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def _parse_source_scopes(file_path: str, source_text: str) -> list[SourceScopeBlock]:
    sanitized = _strip_comments_and_literals(source_text)
    line_starts = _line_starts(sanitized)
    roots = _collect_brace_blocks(file_path, sanitized, line_starts)
    single_statement_controls = _collect_single_statement_controls(
        file_path,
        sanitized,
        line_starts,
    )
    for control in single_statement_controls:
        _insert_scope_block(roots, control)
    _sort_scope_tree(roots)
    return roots


def _collect_brace_blocks(
    file_path: str,
    text: str,
    line_starts: list[int],
) -> list[SourceScopeBlock]:
    roots: list[SourceScopeBlock] = []
    stack: list[SourceScopeBlock] = []

    for idx, ch in enumerate(text):
        if ch == "{":
            start_line, kind = _brace_scope_start(text, idx, line_starts)
            block = SourceScopeBlock(
                start_line=start_line,
                end_line=start_line,
                kind=kind,
                source_file_path=file_path,
            )
            stack.append(block)
            continue

        if ch == "}" and stack:
            block = stack.pop()
            block.end_line = _index_to_line(line_starts, idx)
            if stack:
                parent = stack[-1]
                block.parent = parent
                parent.children.append(block)
            else:
                roots.append(block)

    return roots


def _collect_single_statement_controls(
    file_path: str,
    text: str,
    line_starts: list[int],
) -> list[SourceScopeBlock]:
    controls: list[SourceScopeBlock] = []
    index = 0
    length = len(text)

    while index < length:
        keyword = _match_any_keyword(text, index, _CONTROL_KEYWORDS)
        if keyword is None:
            index += 1
            continue

        header_end = _parse_control_header_end(text, index, keyword)
        if header_end is None:
            index += len(keyword)
            continue

        body_start = _skip_whitespace(text, header_end)
        if body_start >= length:
            break

        if text[body_start] == "{":
            index = body_start + 1
            continue

        end_index = _statement_end_index(text, body_start)
        if end_index < body_start:
            index += len(keyword)
            continue

        controls.append(
            SourceScopeBlock(
                start_line=_index_to_line(line_starts, index),
                end_line=_index_to_line(line_starts, end_index),
                kind=keyword,
                source_file_path=file_path,
            )
        )
        index = body_start + 1

    return controls


def _statement_end_index(text: str, start_index: int) -> int:
    index = _skip_whitespace(text, start_index)
    if index >= len(text):
        return len(text) - 1

    if text[index] == "{":
        close_index = _find_matching_forward(text, index, "{", "}")
        return close_index if close_index is not None else len(text) - 1

    keyword = _match_any_keyword(text, index, _STATEMENT_KEYWORDS)
    if keyword in _CONTROL_KEYWORDS:
        header_end = _parse_control_header_end(text, index, keyword)
        if header_end is None:
            return _simple_statement_end_index(text, index)
        body_start = _skip_whitespace(text, header_end)
        body_end = _statement_end_index(text, body_start)
        if keyword == "if":
            else_index = _skip_whitespace(text, body_end + 1)
            if _matches_keyword(text, else_index, "else"):
                return _statement_end_index(text, _skip_whitespace(text, else_index + 4))
        return body_end

    if keyword == "do":
        body_start = _skip_whitespace(text, index + 2)
        body_end = _statement_end_index(text, body_start)
        while_index = _skip_whitespace(text, body_end + 1)
        if _matches_keyword(text, while_index, "while"):
            header_end = _parse_control_header_end(text, while_index, "while")
            if header_end is not None:
                return _simple_statement_end_index(text, _skip_whitespace(text, header_end))
        return body_end

    return _simple_statement_end_index(text, index)


def _simple_statement_end_index(text: str, start_index: int) -> int:
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0
    index = start_index
    length = len(text)

    while index < length:
        ch = text[index]
        if ch == "(":
            paren_depth += 1
        elif ch == ")" and paren_depth > 0:
            paren_depth -= 1
        elif ch == "[":
            bracket_depth += 1
        elif ch == "]" and bracket_depth > 0:
            bracket_depth -= 1
        elif ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth > 0:
            brace_depth -= 1
        elif ch == ";" and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
            return index
        index += 1

    return max(length - 1, start_index)


def _brace_scope_start(
    text: str,
    brace_index: int,
    line_starts: list[int],
) -> tuple[int, str]:
    brace_line = _index_to_line(line_starts, brace_index)
    prev_index = _skip_whitespace_backward(text, brace_index - 1)
    if prev_index < 0:
        return brace_line, "block"

    if text[prev_index] == ")":
        open_paren = _find_matching_backward(text, prev_index, "(", ")")
        if open_paren is None:
            return brace_line, "block"
        ident = _identifier_before(text, open_paren - 1)
        if ident is None:
            return brace_line, "block"
        ident_name, ident_start = ident
        if ident_name in _CONTROL_KEYWORDS:
            return _index_to_line(line_starts, ident_start), ident_name
        return _index_to_line(line_starts, ident_start), "function"

    else_ident = _identifier_before(text, prev_index)
    if else_ident is not None and else_ident[0] == "else":
        return _index_to_line(line_starts, else_ident[1]), "else"

    return brace_line, "block"


def _insert_scope_block(roots: list[SourceScopeBlock], block: SourceScopeBlock) -> None:
    parent = _find_deepest_container(roots, block.start_line, block.end_line)
    if parent is None:
        roots.append(block)
        return
    for existing in parent.children:
        if (
            existing.start_line == block.start_line
            and existing.end_line == block.end_line
            and existing.kind == block.kind
        ):
            return
    block.parent = parent
    parent.children.append(block)


def _find_deepest_container(
    nodes: list[SourceScopeBlock],
    start_line: int,
    end_line: int,
) -> SourceScopeBlock | None:
    best: SourceScopeBlock | None = None
    for node in nodes:
        if not _contains_range(node, start_line, end_line):
            continue
        deeper = _find_deepest_container(node.children, start_line, end_line)
        candidate = deeper or node
        if best is None:
            best = candidate
            continue
        if _span(candidate) < _span(best):
            best = candidate
    return best


def _find_best_chain(nodes: list[SourceScopeBlock], line_number: int) -> list[SourceScopeBlock]:
    best_chain: list[SourceScopeBlock] = []
    for node in nodes:
        if not _line_in_scope(node, line_number):
            continue
        child_chain = _find_best_chain(node.children, line_number)
        candidate = [node, *child_chain]
        if not best_chain:
            best_chain = candidate
            continue
        if len(candidate) > len(best_chain):
            best_chain = candidate
            continue
        if len(candidate) == len(best_chain):
            if _span(candidate[-1]) < _span(best_chain[-1]):
                best_chain = candidate
    return best_chain


def _contains_range(node: SourceScopeBlock, start_line: int, end_line: int) -> bool:
    return node.start_line <= start_line and end_line <= node.end_line


def _line_in_scope(node: SourceScopeBlock, line_number: int) -> bool:
    return node.start_line <= line_number <= node.end_line


def _span(node: SourceScopeBlock) -> int:
    return max(1, node.end_line - node.start_line + 1)


def _sort_scope_tree(nodes: list[SourceScopeBlock]) -> None:
    nodes.sort(key=lambda node: (node.start_line, node.end_line, node.kind))
    for node in nodes:
        _sort_scope_tree(node.children)


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, ch in enumerate(text):
        if ch == "\n":
            starts.append(index + 1)
    return starts


def _index_to_line(line_starts: list[int], index: int) -> int:
    if index <= 0:
        return 1
    return bisect.bisect_right(line_starts, index)


def _skip_whitespace(text: str, start_index: int) -> int:
    index = start_index
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _skip_whitespace_backward(text: str, start_index: int) -> int:
    index = start_index
    while index >= 0 and text[index].isspace():
        index -= 1
    return index


def _matches_keyword(text: str, index: int, keyword: str) -> bool:
    if index < 0:
        return False
    end = index + len(keyword)
    if not text.startswith(keyword, index):
        return False
    before_ok = index == 0 or not _is_identifier_char(text[index - 1])
    after_ok = end >= len(text) or not _is_identifier_char(text[end])
    return before_ok and after_ok


def _match_any_keyword(text: str, index: int, keywords: tuple[str, ...]) -> str | None:
    for keyword in keywords:
        if _matches_keyword(text, index, keyword):
            return keyword
    return None


def _parse_control_header_end(text: str, keyword_index: int, keyword: str) -> int | None:
    open_paren_index = _skip_whitespace(text, keyword_index + len(keyword))
    if open_paren_index >= len(text) or text[open_paren_index] != "(":
        return None
    close_paren_index = _find_matching_forward(text, open_paren_index, "(", ")")
    if close_paren_index is None:
        return None
    return close_paren_index + 1


def _identifier_before(text: str, start_index: int) -> tuple[str, int] | None:
    end_index = _skip_whitespace_backward(text, start_index)
    if end_index < 0 or not _is_identifier_char(text[end_index]):
        return None
    begin_index = end_index
    while begin_index > 0 and _is_identifier_char(text[begin_index - 1]):
        begin_index -= 1
    return text[begin_index : end_index + 1], begin_index


def _is_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _find_matching_forward(
    text: str,
    start_index: int,
    open_char: str,
    close_char: str,
) -> int | None:
    if start_index >= len(text) or text[start_index] != open_char:
        return None
    depth = 0
    for index in range(start_index, len(text)):
        ch = text[index]
        if ch == open_char:
            depth += 1
        elif ch == close_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def _find_matching_backward(
    text: str,
    start_index: int,
    open_char: str,
    close_char: str,
) -> int | None:
    if start_index < 0 or text[start_index] != close_char:
        return None
    depth = 0
    for index in range(start_index, -1, -1):
        ch = text[index]
        if ch == close_char:
            depth += 1
        elif ch == open_char:
            depth -= 1
            if depth == 0:
                return index
    return None


def _strip_comments_and_literals(source_text: str) -> str:
    output: list[str] = []
    index = 0
    length = len(source_text)
    state = "code"

    while index < length:
        ch = source_text[index]
        nxt = source_text[index + 1] if index + 1 < length else ""

        if state == "code":
            if ch == "/" and nxt == "/":
                output.extend([" ", " "])
                index += 2
                state = "line_comment"
                continue
            if ch == "/" and nxt == "*":
                output.extend([" ", " "])
                index += 2
                state = "block_comment"
                continue
            if ch == '"':
                output.append(" ")
                index += 1
                state = "string"
                continue
            if ch == "'":
                output.append(" ")
                index += 1
                state = "char"
                continue
            output.append(ch)
            index += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                output.append("\n")
                state = "code"
            else:
                output.append(" ")
            index += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                output.extend([" ", " "])
                index += 2
                state = "code"
            else:
                output.append("\n" if ch == "\n" else " ")
                index += 1
            continue

        if state in {"string", "char"}:
            if ch == "\\":
                output.append(" ")
                if index + 1 < length:
                    output.append("\n" if source_text[index + 1] == "\n" else " ")
                index += 2
                continue
            if (state == "string" and ch == '"') or (state == "char" and ch == "'"):
                output.append(" ")
                index += 1
                state = "code"
                continue
            output.append("\n" if ch == "\n" else " ")
            index += 1

    return "".join(output)
