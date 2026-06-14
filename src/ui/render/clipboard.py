"""Clipboard helper — copy text via pyperclip, wl-copy, or xclip.

Used by both TestOutputScreen and TestDebuggerScreen for text selection.
"""

import shutil
import subprocess


def copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard.

    Tries pyperclip first, then wl-copy (Wayland), then xclip (X11).
    Returns ``True`` on success, ``False`` if no clipboard tool is available.
    """
    try:
        import pyperclip

        pyperclip.copy(text)
        return True
    except Exception:
        pass

    for command in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
        executable = command[0]
        if shutil.which(executable) is None:
            continue
        try:
            result = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode == 0:
                return True
        except Exception:
            continue

    return False
