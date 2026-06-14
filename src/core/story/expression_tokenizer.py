from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    """Single lexical token from a C source line."""

    type: str
    value: str
    position: int


_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")

# Sorted longest-first so greedy matching prefers multi-character operators.
_OPERATORS = (
    "->", "++", "--", "<<", ">>", "<=", ">=", "==", "!=", "&&", "||",
    "+", "-", "*", "/", "%", "=", "<", ">", "!", "&", "|", "^", "~",
)

_PUNCTUATION = {"(", ")", "[", "]", "{", "}", ",", ";", "."}


def _skip_whitespace_and_comments(line: str, pos: int) -> int:
    """Advance past whitespace and C comments, returning new position."""
    length = len(line)
    while pos < length:
        ch = line[pos]
        if ch in " \t\n\r":
            pos += 1
            continue
        if ch == "/" and pos + 1 < length:
            if line[pos + 1] == "/":
                return length
            if line[pos + 1] == "*":
                end = line.find("*/", pos + 2)
                pos = end + 2 if end != -1 else length
                continue
        break
    return pos


def _read_string_literal(line: str, pos: int) -> tuple[Token, int] | None:
    """Read a double-quoted string literal starting at pos."""
    if line[pos] != '"':
        return None
    start = pos
    pos += 1
    while pos < len(line):
        ch = line[pos]
        if ch == "\\" and pos + 1 < len(line):
            pos += 2
            continue
        if ch == '"':
            pos += 1
            return Token("literal_string", line[start:pos], start), pos
        pos += 1
    return Token("literal_string", line[start:pos], start), pos


def _read_char_literal(line: str, pos: int) -> tuple[Token, int] | None:
    """Read a single-quoted character literal starting at pos."""
    if line[pos] != "'":
        return None
    start = pos
    pos += 1
    while pos < len(line):
        ch = line[pos]
        if ch == "\\" and pos + 1 < len(line):
            pos += 2
            continue
        if ch == "'":
            pos += 1
            return Token("literal_char", line[start:pos], start), pos
        pos += 1
    return Token("literal_char", line[start:pos], start), pos


def _read_number_literal(line: str, pos: int) -> tuple[Token, int] | None:
    """Read a numeric literal (hex, decimal, float) starting at pos."""
    start = pos
    length = len(line)

    if line[pos] == "0" and pos + 1 < length and line[pos + 1] in "xX":
        pos += 2
        while pos < length and line[pos] in "0123456789abcdefABCDEF":
            pos += 1
        return Token("literal_number", line[start:pos], start), pos

    has_dot = False
    has_exp = False
    has_digit = False
    while pos < length:
        ch = line[pos]
        if ch == ".":
            if has_dot:
                break
            has_dot = True
            pos += 1
            continue
        if ch in "eE":
            if has_exp:
                break
            has_exp = True
            pos += 1
            if pos < length and line[pos] in "+-":
                pos += 1
            continue
        if ch.isdigit():
            has_digit = True
            pos += 1
            continue
        break

    if not has_digit:
        return None
    return Token("literal_number", line[start:pos], start), pos


def _read_identifier(line: str, pos: int) -> tuple[Token, int] | None:
    """Read an identifier starting at pos."""
    match = _IDENTIFIER_RE.match(line, pos)
    if not match:
        return None
    return Token("identifier", match.group(0), pos), match.end()


def _read_operator_or_punct(line: str, pos: int) -> tuple[Token, int] | None:
    """Read an operator or punctuation mark starting at pos."""
    two_char = line[pos : pos + 2]
    if two_char in _OPERATORS:
        return Token("operator", two_char, pos), pos + 2

    ch = line[pos]
    if ch in _OPERATORS:
        return Token("operator", ch, pos), pos + 1

    if ch in _PUNCTUATION:
        return Token("punctuation", ch, pos), pos + 1

    return None


def tokenize_line(line: str) -> list[Token]:
    """Tokenize a single source line into a flat list of Token objects."""
    tokens: list[Token] = []
    pos = 0
    length = len(line)

    while pos < length:
        pos = _skip_whitespace_and_comments(line, pos)
        if pos >= length:
            break

        result = (
            _read_string_literal(line, pos)
            or _read_char_literal(line, pos)
            or _read_number_literal(line, pos)
            or _read_identifier(line, pos)
            or _read_operator_or_punct(line, pos)
        )

        if result is None:
            pos += 1
            continue

        token, pos = result
        tokens.append(token)

    return tokens


_C_KEYWORDS = frozenset(
    [
        "auto",
        "break",
        "case",
        "char",
        "const",
        "continue",
        "default",
        "do",
        "double",
        "else",
        "enum",
        "extern",
        "float",
        "for",
        "goto",
        "if",
        "inline",
        "int",
        "long",
        "register",
        "restrict",
        "return",
        "short",
        "signed",
        "sizeof",
        "static",
        "struct",
        "switch",
        "typedef",
        "union",
        "unsigned",
        "void",
        "volatile",
        "while",
        "_Bool",
        "_Complex",
        "_Imaginary",
    ]
)

_C_TYPE_KEYWORDS = frozenset(
    [
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "void",
        "signed",
        "unsigned",
        "struct",
        "union",
        "enum",
        "_Bool",
        "_Complex",
        "_Imaginary",
    ]
)


def _is_keyword(token: Token) -> bool:
    return token.type == "identifier" and token.value in _C_KEYWORDS


def _looks_like_cast(tokens: list[Token]) -> bool:
    if not tokens:
        return False
    has_type = any(t.type == "identifier" and t.value in _C_TYPE_KEYWORDS for t in tokens)
    all_valid = all(t.type == "identifier" or t.value == "*" for t in tokens)
    return has_type and all_valid


def _skip_matching(
    tokens: list[Token], start: int, open_ch: str, close_ch: str
) -> int:
    depth = 1
    pos = start + 1
    while pos < len(tokens) and depth > 0:
        if tokens[pos].value == open_ch:
            depth += 1
        elif tokens[pos].value == close_ch:
            depth -= 1
        pos += 1
    return pos


def _strip_side_effects(expr_str: str, tokens: list[Token], start: int, end: int) -> str | None:
    had_any = False
    for t in tokens[start:end]:
        if t.value in ("++", "--"):
            expr_str = expr_str.replace(t.value, "", 1)
            had_any = True
    return None if had_any and not expr_str else expr_str


def _consume_expression(
    tokens: list[Token], start: int, line: str
) -> tuple[str, int, list[str]] | None:
    if start >= len(tokens): return None
    pos = start
    if tokens[pos].value in ("*", "&", "+", "-", "++", "--"):
        pos += 1
    if pos >= len(tokens):
        return None
    expr_start = tokens[start].position
    if tokens[pos].value != "(" and (tokens[pos].type != "identifier" or _is_keyword(tokens[pos])):
        return None
    if tokens[pos].value == "(":
        paren_open = pos
        pos = _skip_matching(tokens, pos, "(", ")")
        if tokens[pos - 1].value != ")":
            return None
        if _looks_like_cast(tokens[paren_open + 1 : pos - 1]):
            cast_str = line[expr_start: tokens[pos - 1].position + len(tokens[pos - 1].value)]
            inner = _consume_expression(tokens, pos, line)
            return None if inner is None else (cast_str + inner[0], inner[1], inner[2])
        return None
    else:
        pos += 1
    expr_end = tokens[pos - 1].position + len(tokens[pos - 1].value)
    nested: list[str] = []
    while pos < len(tokens):
        tok = tokens[pos]
        if tok.value == "[":
            pos = _skip_matching(tokens, pos, "[", "]")
        elif tok.value in ("->", "."):
            pos += 1
            if not (pos < len(tokens) and tokens[pos].type == "identifier" and not _is_keyword(tokens[pos])):
                break
            pos += 1
        elif tok.value in ("++", "--"):
            pos += 1
        elif tok.value == "(":
            paren_open = pos
            pos = _skip_matching(tokens, pos, "(", ")")
            nested.extend(_extract_from_tokens(tokens[paren_open + 1 : pos - 1], line))
        else:
            break
        expr_end = tokens[pos - 1].position + len(tokens[pos - 1].value)
    expr_str = _strip_side_effects(line[expr_start:expr_end], tokens, start, pos)
    return None if expr_str is None else (expr_str, pos, nested)


def _extract_from_tokens(tokens: list[Token], line: str) -> list[str]:
    expressions: list[str] = []
    i = 0
    while i < len(tokens):
        result = _consume_expression(tokens, i, line)
        if result is not None:
            expr_str, next_i, nested = result
            expressions.append(expr_str)
            expressions.extend(nested)
            i = next_i
        else:
            i += 1
    return expressions


def extract_expressions(line: str) -> list[str]:
    """Tokenize *line* and extract all valid C sub-expressions."""
    tokens = tokenize_line(line)
    return _extract_from_tokens(tokens, line)


if __name__ == "__main__":
    # Target case: arr[i]
    toks = tokenize_line("arr[i]")
    assert [t.type for t in toks] == [
        "identifier",
        "punctuation",
        "identifier",
        "punctuation",
    ]
    assert [t.value for t in toks] == ["arr", "[", "i", "]"]

    # Target case: *ptr
    toks = tokenize_line("*ptr")
    assert [t.type for t in toks] == ["operator", "identifier"]
    assert [t.value for t in toks] == ["*", "ptr"]

    # Target case: foo->bar.baz
    toks = tokenize_line("foo->bar.baz")
    assert [t.type for t in toks] == [
        "identifier",
        "operator",
        "identifier",
        "punctuation",
        "identifier",
    ]
    assert [t.value for t in toks] == ["foo", "->", "bar", ".", "baz"]

    # Target case: func(a,b)
    toks = tokenize_line("func(a,b)")
    assert [t.type for t in toks] == [
        "identifier",
        "punctuation",
        "identifier",
        "punctuation",
        "identifier",
        "punctuation",
    ]
    assert [t.value for t in toks] == ["func", "(", "a", ",", "b", ")"]

    # Target case: i++
    toks = tokenize_line("i++")
    assert [t.type for t in toks] == ["identifier", "operator"]
    assert [t.value for t in toks] == ["i", "++"]

    # Target case: &var
    toks = tokenize_line("&var")
    assert [t.type for t in toks] == ["operator", "identifier"]
    assert [t.value for t in toks] == ["&", "var"]

    # Target case: (int)x
    toks = tokenize_line("(int)x")
    assert [t.type for t in toks] == [
        "punctuation",
        "identifier",
        "punctuation",
        "identifier",
    ]
    assert [t.value for t in toks] == ["(", "int", ")", "x"]

    # Comments and whitespace
    toks = tokenize_line("  foo /* comment */ + bar // end")
    assert [t.value for t in toks] == ["foo", "+", "bar"]

    # String literal
    toks = tokenize_line('printf("hello")')
    assert [t.type for t in toks] == [
        "identifier",
        "punctuation",
        "literal_string",
        "punctuation",
    ]
    assert [t.value for t in toks] == ['printf', '(', '"hello"', ')']

    # Character literal
    toks = tokenize_line("x = 'a'")
    assert [t.value for t in toks] == ["x", "=", "'a'"]

    # Numeric literals
    toks = tokenize_line("0x1F + 3.14")
    assert [t.value for t in toks] == ["0x1F", "+", "3.14"]

    # Complex expression
    toks = tokenize_line("(int)arr[i]->field + 42")
    assert [t.value for t in toks] == [
        "(", "int", ")", "arr", "[", "i", "]", "->", "field", "+", "42",
    ]

    # --- Expression extractor tests ---

    # Target case: arr[i]
    assert extract_expressions("arr[i]") == ["arr[i]"]

    # Target case: *ptr
    assert extract_expressions("*ptr") == ["*ptr"]

    # Target case: foo->bar.baz
    assert extract_expressions("foo->bar.baz") == ["foo->bar.baz"]

    # Target case: func(a,b) — also extracts arguments
    assert extract_expressions("func(a,b)") == ["func(a,b)", "a", "b"]

    # Target case: i++ (side-effect safety)
    assert extract_expressions("i++") == ["i"]

    # Target case: ++i (prefix side-effect safety)
    assert extract_expressions("++i") == ["i"]

    # Target case: &var
    assert extract_expressions("&var") == ["&var"]

    # Target case: (int)x
    assert extract_expressions("(int)x") == ["(int)x"]

    # Additional: array with side-effect in subscript
    assert extract_expressions("arr[i++]") == ["arr[i]"]

    # Additional: function call with side-effect argument
    assert extract_expressions("func(a++, b)") == ["func(a, b)", "a", "b"]

    # Additional: mixed statement
    assert extract_expressions("if (x) { y = z; }") == ["x", "y", "z"]

    # Additional: keyword identifiers are skipped
    assert extract_expressions("int foo = 42;") == ["foo"]

    # Additional: pointer member access
    assert extract_expressions("ptr->field->inner") == ["ptr->field->inner"]

    # Additional: address-of array element
    assert extract_expressions("&arr[i]") == ["&arr[i]"]

    # Additional: cast with pointer
    assert extract_expressions("(int *)ptr") == ["(int *)ptr"]

    print("All tokenizer and extractor tests passed.")
