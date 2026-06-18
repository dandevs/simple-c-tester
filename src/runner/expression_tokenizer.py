"""Compatibility shim — re-exports the expression tokenizer from ``core.story``."""

from core.story.expression_tokenizer import (  # noqa: F401
    Token,
    _consume_expression,
    _extract_from_tokens,
    _is_keyword,
    _looks_like_cast,
    _read_char_literal,
    _read_identifier,
    _read_number_literal,
    _read_operator_or_punct,
    _read_string_literal,
    _skip_matching,
    _skip_whitespace_and_comments,
    _strip_side_effects,
    extract_expressions,
    tokenize_line,
)
