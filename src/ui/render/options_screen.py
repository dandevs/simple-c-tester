"""Options screen — user-editable, session-persistent settings.

Opened from the main test page with ``o``.  Renders one row per
:class:`core.userconfig.OptionField`, grouped by category, with steppers
(numeric +/-), cycles (rotating choices), and toggles (boolean).

Design notes
------------
* The screen is intentionally "dumb": it holds a local copy of the current
  values and notifies the app via an ``on_change(key, value)`` callback.  The
  app owns the side effects (mutating live engine state + persisting to disk),
  so this module imports nothing from ``api``, ``runner``, or the global
  ``state`` module.
* CLI-overridden fields are displayed at their effective (CLI) value and
  locked for the session — the persistent default is edited on launches that
  don't pass the relevant flag.  This keeps the menu a faithful view of the
  *current* effective state.
* Mouse clicks are the primary interaction; arrow keys + enter provide an
  equivalent keyboard path via a highlighted cursor row.
"""

from __future__ import annotations

from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer
from textual.screen import Screen
from textual.widgets import Button, Static

from core.userconfig import OPTION_FIELDS, OptionField


def _row_id(key: str) -> str:
    return f"opt_row_{key}"


def _label_id(key: str) -> str:
    return f"opt_label_{key}"


def _value_id(key: str) -> str:
    return f"opt_val_{key}"


class OptionsScreen(Screen[None]):
    """A full-page settings screen driven by :data:`OPTION_FIELDS`."""

    CSS = """
    OptionsScreen {
        align: center middle;
    }
    #options_panel {
        width: 80;
        max-width: 94vw;
        height: auto;
        max-height: 90vh;
        border: round ansi_blue;
        background: $surface;
        padding: 1 2;
    }
    #opt_title {
        text-style: bold;
        color: ansi_yellow;
        margin: 0 0 1 0;
    }
    #opt_banner {
        color: ansi_yellow;
        margin: 0 0 1 0;
    }
    #opt_body {
        height: auto;
        max-height: 76vh;
    }
    .opt_group {
        text-style: bold;
        color: ansi_cyan;
        margin: 1 0 0 0;
    }
    .opt_row {
        height: 1;
        layout: horizontal;
    }
    .opt_label {
        width: 34;
        height: 1;
        color: $text;
    }
    .opt_label_selected {
        text-style: bold;
        color: ansi_yellow;
    }
    .opt_ctrl {
        layout: horizontal;
        width: 1fr;
        height: 1;
    }
    .opt_btn {
        width: 6;
        height: 1;
        min-width: 6;
        padding: 0;
        border: none;
        background: transparent;
    }
    .opt_btn:focus {
        text-style: bold;
        color: ansi_yellow;
    }
    .opt_val {
        width: 1fr;
        height: 1;
        color: ansi_green;
        padding: 0 1;
    }
    .opt_toggle {
        width: 8;
        min-width: 8;
    }
    #opt_hint {
        color: ansi_bright_black;
        margin: 1 0 0 0;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("o", "close", "Close"),
        Binding("up", "cursor_up", "Prev", show=False),
        Binding("down", "cursor_down", "Next", show=False),
        Binding("k", "cursor_up", "", show=False),
        Binding("j", "cursor_down", "", show=False),
        Binding("left", "cursor_left", "Dec", show=False),
        Binding("right", "cursor_right", "Inc", show=False),
        Binding("h", "cursor_left", "", show=False),
        Binding("l", "cursor_right", "", show=False),
        Binding("enter", "cursor_activate", "Toggle", show=False),
    ]

    def __init__(
        self,
        values: dict,
        cli_overrides: set[str] | None = None,
        on_change: Callable[[str, object], None] | None = None,
    ):
        super().__init__()
        # Local mutable copy; updated on every interaction before notifying.
        self._values: dict = dict(values)
        self._cli_overrides: set[str] = set(cli_overrides or ())
        self._on_change = on_change
        self._fields: list[OptionField] = list(OPTION_FIELDS)
        self._cursor: int = 0

    # ----- composition --------------------------------------------------

    def compose(self) -> ComposeResult:
        title = Static("Options", id="opt_title", markup=False)
        with ScrollableContainer(id="options_panel"):
            yield title
            if self._cli_overrides:
                yield Static(self._banner_text(), id="opt_banner", markup=False)
            with ScrollableContainer(id="opt_body"):
                last_group: str | None = None
                for field in self._fields:
                    if field.group != last_group:
                        yield Static(
                            field.group, classes="opt_group", markup=False
                        )
                        last_group = field.group
                    yield from self._compose_row(field)
            yield Static(self._hint_text(), id="opt_hint", markup=False)

    def _compose_row(self, field: OptionField):
        overridden = field.key in self._cli_overrides
        label_text = field.label + ("  (CLI)" if overridden else "")
        with Horizontal(classes="opt_row", id=_row_id(field.key)):
            yield Static(
                label_text,
                id=_label_id(field.key),
                classes="opt_label",
                markup=False,
            )
            with Horizontal(classes="opt_ctrl"):
                if field.kind == "stepper":
                    yield Button(
                        "-",
                        id=f"opt_dec_{field.key}",
                        classes="opt_btn",
                        disabled=overridden,
                    )
                    yield Static(
                        self._value_text(field),
                        id=_value_id(field.key),
                        classes="opt_val",
                        markup=False,
                    )
                    yield Button(
                        "+",
                        id=f"opt_inc_{field.key}",
                        classes="opt_btn",
                        disabled=overridden,
                    )
                elif field.kind == "cycle":
                    yield Button(
                        "<",
                        id=f"opt_prev_{field.key}",
                        classes="opt_btn",
                        disabled=overridden,
                    )
                    yield Static(
                        self._value_text(field),
                        id=_value_id(field.key),
                        classes="opt_val",
                        markup=False,
                    )
                    yield Button(
                        ">",
                        id=f"opt_next_{field.key}",
                        classes="opt_btn",
                        disabled=overridden,
                    )
                else:  # toggle
                    yield Button(
                        self._value_text(field),
                        id=f"opt_toggle_{field.key}",
                        classes="opt_btn opt_toggle",
                        disabled=overridden,
                    )

    # ----- text helpers -------------------------------------------------

    def _value_text(self, field: OptionField) -> str:
        value = self._values.get(field.key, field.default)
        if field.kind == "toggle":
            return "on" if value else "off"
        return str(value)

    def _banner_text(self) -> str:
        keys = ", ".join(sorted(self._cli_overrides))
        return (
            "Overridden by CLI flags this session (locked): "
            + keys
            + "  - relaunch without the flag to edit them here."
        )

    def _hint_text(self) -> str:
        return "o/Esc close   \u2190/\u2192 change   \u2191/\u2193 navigate   enter toggle"

    # ----- interactions -------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if not bid.startswith("opt_"):
            return
        parts = bid.split("_", 2)
        if len(parts) != 3:
            return
        _, action, key = parts
        self._apply_action(action, key)

    def _field_by_key(self, key: str) -> OptionField | None:
        for f in self._fields:
            if f.key == key:
                return f
        return None

    def _apply_action(self, action: str, key: str) -> None:
        field = self._field_by_key(key)
        if field is None or key in self._cli_overrides:
            return  # locked or unknown
        current = self._values.get(key, field.default)

        if field.kind == "stepper":
            if action == "dec":
                new_value = max(field.min, int(current) - 1)
            elif action == "inc":
                new_value = min(field.max, int(current) + 1)
            else:
                return
        elif field.kind == "cycle":
            choices = list(field.choices)
            try:
                idx = choices.index(current)
            except ValueError:
                idx = 0
            if action == "prev":
                new_value = choices[(idx - 1) % len(choices)]
            elif action == "next":
                new_value = choices[(idx + 1) % len(choices)]
            else:
                return
        elif field.kind == "toggle":
            if action != "toggle":
                return
            new_value = not bool(current)
        else:
            return

        self._values[key] = new_value
        self._refresh_value_widget(field)
        if self._on_change is not None:
            self._on_change(key, new_value)

    def _refresh_value_widget(self, field: OptionField) -> None:
        text = self._value_text(field)
        try:
            if field.kind == "toggle":
                btn = self.query_one(f"#opt_toggle_{field.key}", Button)
                btn.label = text
            else:
                widget = self.query_one(f"#{_value_id(field.key)}", Static)
                widget.update(text)
        except Exception:
            pass  # widget not yet mounted; ignore

    # ----- keyboard cursor ----------------------------------------------

    def on_mount(self) -> None:
        self._cursor = 0
        self._render_cursor()

    def _editable_indices(self) -> list[int]:
        return [i for i, f in enumerate(self._fields) if f.key not in self._cli_overrides]

    def _render_cursor(self) -> None:
        for i, field in enumerate(self._fields):
            try:
                widget = self.query_one(f"#{_label_id(field.key)}", Static)
            except Exception:
                continue
            selected = i == self._cursor
            base = field.label + ("  (CLI)" if field.key in self._cli_overrides else "")
            if selected and field.key not in self._cli_overrides:
                widget.update("\u25b6 " + base)
                widget.add_class("opt_label_selected")
            else:
                widget.update("  " + base)
                widget.remove_class("opt_label_selected")

    def action_cursor_up(self) -> None:
        self._move_cursor(-1)

    def action_cursor_down(self) -> None:
        self._move_cursor(1)

    def _move_cursor(self, delta: int) -> None:
        n = len(self._fields)
        if n == 0:
            return
        # Step through all rows (including locked ones) so navigation is
        # contiguous; actions on locked rows no-op.
        self._cursor = (self._cursor + delta) % n
        self._render_cursor()
        self._scroll_cursor_into_view()

    def _scroll_cursor_into_view(self) -> None:
        field = self._fields[self._cursor]
        try:
            row = self.query_one(f"#{_row_id(field.key)}")
            row.scroll_visible()
        except Exception:
            pass

    def action_cursor_left(self) -> None:
        field = self._fields[self._cursor]
        if field.kind == "stepper":
            self._apply_action("dec", field.key)
        elif field.kind == "cycle":
            self._apply_action("prev", field.key)

    def action_cursor_right(self) -> None:
        field = self._fields[self._cursor]
        if field.kind == "stepper":
            self._apply_action("inc", field.key)
        elif field.kind == "cycle":
            self._apply_action("next", field.key)

    def action_cursor_activate(self) -> None:
        field = self._fields[self._cursor]
        if field.kind == "toggle":
            self._apply_action("toggle", field.key)
        elif field.kind == "cycle":
            self._apply_action("next", field.key)
        elif field.kind == "stepper":
            self._apply_action("inc", field.key)

    def action_close(self) -> None:
        self.app.pop_screen()
