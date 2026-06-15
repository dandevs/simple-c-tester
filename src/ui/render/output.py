import re

from rich.text import Text

from core.models import Test, TestState
from core.assertions import AssertionFailure, parse_assertion_failures, is_assertion_line
from .styles import (
    OUTPUT_FAIL_BORDER_STYLE,
    OUTPUT_PASS_BORDER_STYLE,
    TREE_GUIDE_STYLE,
    OutputBoxRenderMeta,
)


# Patterns for sanitizer error banners.
_ASAN_RE = re.compile(r"==\d+==ERROR: AddressSanitizer: (\S+)")
_UBSAN_RE = re.compile(r"runtime error: (.+)")


# ---------------------------------------------------------------------------
# Assertion-failure rendering (replaces raw [CTEST:v] lines with a diff block)
# ---------------------------------------------------------------------------

def _render_assertion_failure(af: AssertionFailure) -> list[Text]:
    """Render one assertion failure as a 3-line coloured diff block."""
    lines: list[Text] = []

    header = Text()
    header.append("\u2717 ", style="bold red")
    header.append(f"{af.macro}({af.args})", style="bold red")
    lines.append(header)

    diff = Text("  ")
    diff.append("expected: ", style="bright_black")
    diff.append(af.expected, style="green")
    diff.append("  ")
    diff.append("actual: ", style="bright_black")
    diff.append(af.actual, style="red")
    lines.append(diff)

    loc = Text(f"  at {af.file}:{af.line}", style="bright_black")
    lines.append(loc)
    return lines


def _filter_assertion_lines(lines: list[Text]) -> list[Text]:
    """Remove [CTEST:v] FAIL lines from a list of Text (they're rendered
    separately by ``_render_assertion_failure``)."""
    return [line for line in lines if not is_assertion_line(line.plain)]


# ---------------------------------------------------------------------------
# Raw output extraction (uncached — used by output screen which renders
# differently and re-runs infrequently).
# ---------------------------------------------------------------------------

def get_test_output(test: Test) -> list[Text] | None:
    sections: list[Text] = []

    def _to_text(raw: bytes, plain: str) -> Text | None:
        if raw:
            return Text.from_ansi(raw.decode(errors="replace"))
        if plain.strip():
            return Text(plain)
        return None

    run = test.current_run
    compile_err_raw = run.compile_err_raw if run is not None else b""
    compile_err = run.compile_err if run is not None else ""
    stderr_raw = run.stderr_raw if run is not None else b""
    stderr = run.stderr if run is not None else ""
    stdout_raw = run.stdout_raw if run is not None else b""
    stdout = run.stdout if run is not None else ""

    if test.state == TestState.FAILED:
        # Signal/crash banner
        run_ref = test.current_run
        sig = run_ref.signal_name if run_ref is not None else ""
        if sig:
            crash = Text()
            crash.append(f"\u2620 CRASHED: {sig}", style="bold red")
            sections.append(crash)
            sections.append(Text())

        if compile_err_raw or compile_err.strip():
            compile_text = _to_text(compile_err_raw, compile_err)
            if compile_text and compile_text.plain.strip():
                for line in compile_text.split(allow_blank=True):
                    sections.append(line)
            return _strip_trailing(sections)

        stderr_text = _to_text(stderr_raw, stderr)
        stdout_text = _to_text(stdout_raw, stdout)

        # Sanitizer error banner (ASan / UBSan)
        if stderr_text and stderr_text.plain.strip():
            asan_match = _ASAN_RE.search(stderr_text.plain)
            ubsan_match = _UBSAN_RE.search(stderr_text.plain)
            if asan_match:
                san = Text()
                san.append(f"\u26a0 ASan: {asan_match.group(1)}", style="bold yellow")
                sections.append(san)
                sections.append(Text())
            elif ubsan_match:
                san = Text()
                san.append(
                    f"\u26a0 UBSan: {ubsan_match.group(1).strip()[:80]}",
                    style="bold yellow",
                )
                sections.append(san)
                sections.append(Text())

        # Structured assertion-failure rendering (replaces raw wire-format lines)
        if stderr_text and stderr_text.plain.strip():
            failures = parse_assertion_failures(stderr_text.plain)
            if failures:
                for af in failures:
                    sections.extend(_render_assertion_failure(af))
                # Show remaining non-assertion stderr below the diff block
                remaining = _filter_assertion_lines(
                    list(stderr_text.split(allow_blank=True))
                )
                if any(line.plain.strip() for line in remaining):
                    sections.append(Text())
                    sections.extend(remaining)
            else:
                for line in stderr_text.split(allow_blank=True):
                    sections.append(line)

        if stdout_text and stdout_text.plain.strip():
            if sections:
                sections.append(Text())
            for line in stdout_text.split(allow_blank=True):
                sections.append(line)
        return _strip_trailing(sections)

    stdout_text = _to_text(stdout_raw, stdout)
    if stdout_text and stdout_text.plain.strip():
        for line in stdout_text.split(allow_blank=True):
            sections.append(line)
    return _strip_trailing(sections)


def _strip_trailing(sections: list[Text]) -> list[Text] | None:
    while sections and not sections[-1].plain.strip():
        sections.pop()
    return sections if sections else None


# ---------------------------------------------------------------------------
# Output-box line building (pure — returns lines, does not write to log).
# ---------------------------------------------------------------------------

def _text_visual_width(text: Text) -> int:
    return max((len(line) for line in text.split(allow_blank=True)), default=0)


def _wrap_output_lines(
    output_lines: list[Text], max_content_width: int, log
) -> list[Text]:
    width = max(1, max_content_width)
    wrapped: list[Text] = []
    console = getattr(log.app, "console", None)

    for line in output_lines:
        source = line.copy()
        if not source.plain:
            wrapped.append(Text())
            continue

        if console is None:
            if len(source) <= width:
                wrapped.append(source)
            else:
                offsets = list(range(width, len(source), width))
                wrapped.extend(source.divide(offsets))
            continue

        wrapped.extend(
            source.wrap(
                console,
                width,
                justify="left",
                overflow="fold",
                no_wrap=False,
            )
        )

    return wrapped if wrapped else [Text()]


def _build_box_lines(
    output_lines: list[Text],
    test: Test,
    child_prefix: str,
    max_lines: int,
    max_total_width: int,
    log,
) -> tuple[list[Text], OutputBoxRenderMeta]:
    """Build the full output-box render as a list of Text lines.

    Returns (lines, meta) — caller is responsible for writing to the log.
    """
    max_lines = max(1, max_lines)

    border_overhead = 6
    available_inner_width = max(
        2, max_total_width - len(child_prefix) - border_overhead
    )
    box_inner_width = available_inner_width
    max_content_width = max(0, box_inner_width - 2)
    wrapped_lines = _wrap_output_lines(output_lines, max_content_width, log)
    visible_lines = wrapped_lines[-max_lines:]

    border_style = (
        OUTPUT_FAIL_BORDER_STYLE
        if test.state == TestState.FAILED
        else OUTPUT_PASS_BORDER_STYLE
    )
    dashes = "─" * box_inner_width

    lines: list[Text] = []

    top = Text(child_prefix, style=TREE_GUIDE_STYLE)
    top.append("└── ╭" + dashes + "╮", style=border_style)
    lines.append(top)

    for line in visible_lines:
        padded = line.copy()
        pad_count = max(0, max_content_width - _text_visual_width(padded))
        if pad_count > 0:
            padded.append(" " * pad_count)

        content_line = Text()
        content_line.append(child_prefix, style=TREE_GUIDE_STYLE)
        content_line.append("    ")
        content_line.append("│ ", style=border_style)
        content_line.append(padded)
        content_line.append(" │", style=border_style)
        lines.append(content_line)

    bottom = Text(child_prefix, style=TREE_GUIDE_STYLE)
    bottom.append("    ╰" + dashes + "╯", style=border_style)
    lines.append(bottom)

    top_length = len(child_prefix) + len("└── " + dashes + "╮")
    meta = OutputBoxRenderMeta(
        rendered_lines=len(visible_lines) + 2,
        left_col=len(child_prefix),
        right_col=max(0, top_length - 1),
    )
    return lines, meta


# ---------------------------------------------------------------------------
# Cached public API — eliminates redundant byte decoding + ANSI parsing +
# line wrapping on every frame for finished tests whose output never changes.
# ---------------------------------------------------------------------------

def _output_signature(test: Test) -> tuple:
    """Cheap content fingerprint for cache invalidation."""
    run = test.current_run
    if run is None:
        return ()
    return (
        test.state,
        len(run.compile_err_raw), run.compile_err_raw[:64],
        len(run.stderr_raw), run.stderr_raw[:64],
        len(run.stdout_raw), run.stdout_raw[:64],
    )


def get_cached_output_box(
    test: Test,
    child_prefix: str,
    max_lines: int,
    max_total_width: int,
    log,
) -> tuple[list[Text], OutputBoxRenderMeta] | None:
    """Return (rendered_lines, meta) for ``test``'s output box, using a cache
    stored on ``test.current_run`` to skip redundant work.

    Returns ``None`` when the test has no output to display.
    """
    output_lines = get_test_output(test)
    if not output_lines:
        return None

    run = test.current_run
    cache = run.output_box_cache if run is not None else None
    sig = _output_signature(test)
    cache_key = (len(child_prefix), max_lines, max_total_width)

    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            cached_sig, cached_lines, cached_meta = cached
            if cached_sig == sig:
                return cached_lines, cached_meta

    lines, meta = _build_box_lines(
        output_lines, test, child_prefix, max_lines, max_total_width, log
    )

    if cache is not None:
        cache[cache_key] = (sig, lines, meta)

    return lines, meta


# ---------------------------------------------------------------------------
# Legacy write-directly API — kept for any callers that write incrementally.
# tree.py now uses get_cached_output_box + batch write instead.
# ---------------------------------------------------------------------------

def render_output_box(
    output_lines: list[Text],
    test: Test,
    child_prefix: str,
    log,
    max_lines: int,
    max_total_width: int,
) -> OutputBoxRenderMeta:
    """Build box lines and write them to ``log`` immediately."""
    lines, meta = _build_box_lines(
        output_lines, test, child_prefix, max_lines, max_total_width, log
    )
    for line in lines:
        log.write(line)
    return meta
