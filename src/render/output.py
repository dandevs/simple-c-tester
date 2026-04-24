from rich.text import Text

from models import Test, TestState
from .styles import (
    TEST_FAILED_STYLE,
    OUTPUT_BOX_PASS_BORDER_STYLE,
    TREE_GUIDE_STYLE,
    OutputBoxRenderMeta,
)


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
        if compile_err_raw or compile_err.strip():
            compile_text = _to_text(compile_err_raw, compile_err)
            if compile_text and compile_text.plain.strip():
                for line in compile_text.split(allow_blank=True):
                    sections.append(line)
            return _strip_trailing(sections)

        stderr_text = _to_text(stderr_raw, stderr)
        stdout_text = _to_text(stdout_raw, stdout)
        if stderr_text and stderr_text.plain.strip():
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


def render_output_box(
    output_lines: list[Text],
    test: Test,
    child_prefix: str,
    log,
    max_lines: int,
    max_total_width: int,
) -> OutputBoxRenderMeta:
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
        TEST_FAILED_STYLE
        if test.state == TestState.FAILED
        else OUTPUT_BOX_PASS_BORDER_STYLE
    )
    dashes = "─" * box_inner_width

    top = Text(child_prefix, style=TREE_GUIDE_STYLE)
    top.append("└── ╭" + dashes + "╮", style=border_style)
    log.write(top)

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
        log.write(content_line)

    bottom = Text(child_prefix, style=TREE_GUIDE_STYLE)
    bottom.append("    ╰" + dashes + "╯", style=border_style)
    log.write(bottom)

    top_length = len(child_prefix) + len("└── " + dashes + "╮")
    return OutputBoxRenderMeta(
        rendered_lines=len(visible_lines) + 2,
        left_col=len(child_prefix),
        right_col=max(0, top_length - 1),
    )
