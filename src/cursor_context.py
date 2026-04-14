"""
cursor_context.py — Read the character immediately left of the cursor.

Uses Windows UIAutomation ITextPattern (via comtypes) to query the focused
element's text range, then walks back one character.  Works in any app that
exposes an accessibility text pattern: Word, Notepad, Chrome/Edge, VS Code,
and standard Win32 text controls.

Falls back to None on any failure so callers degrade gracefully.

Requires:  pip install comtypes
"""

import logging

# True = confirmed working, False = confirmed unavailable, None = unchecked.
_available: bool | None = None


def get_preceding_char() -> str | None:
    """Return the character immediately left of the insertion cursor.

    Queries the currently focused UI element via UIAutomation ITextPattern.

    Returns:
        A single character string, or None if:
          - comtypes is not installed
          - the focused element does not expose ITextPattern (e.g. a terminal)
          - the cursor is at the very start of the document
          - any other COM / accessibility error occurs
    """
    global _available

    if _available is False:
        return None

    try:
        result = _read_via_uia()
        _available = True
        return result
    except ImportError:
        _available = False
        logging.warning(
            "cursor_context: comtypes not found — "
            "run `pip install comtypes` to enable cursor-aware spacing."
        )
        return None
    except Exception as e:
        # Transient failures (e.g. app doesn't support ITextPattern) —
        # don't permanently disable, just return None for this call.
        logging.debug(f"cursor_context: {e}")
        return None


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _ensure_typelib():
    """Generate (or load from cache) UIAutomationClient COM type bindings."""
    try:
        from comtypes.gen import UIAutomationClient  # noqa: F401  (import for side effect)
    except ImportError:
        import comtypes.client
        comtypes.client.GetModule('UIAutomationCore.dll')


def _read_via_uia() -> str | None:
    import ctypes
    import comtypes
    import comtypes.client

    # Initialise COM as STA on this thread (safe to call multiple times).
    ctypes.windll.ole32.CoInitialize(None)

    _ensure_typelib()

    from comtypes.gen import UIAutomationClient  # type: ignore

    # Create the root UIAutomation object.
    _CLSID_CUIAutomation = '{FF48DBA4-60EF-4201-AA87-54103EEF594E}'
    uia = comtypes.client.CreateObject(
        _CLSID_CUIAutomation,
        interface=UIAutomationClient.IUIAutomation,
    )

    focused = uia.GetFocusedElement()
    if focused is None:
        return None

    # Ask the element for an ITextPattern.  Many elements (buttons, toolbars,
    # terminals) don't support this — that's fine, we just return None.
    try:
        raw_pattern = focused.GetCurrentPattern(10014)  # UIA_TextPatternId
    except comtypes.COMError:
        return None

    if raw_pattern is None:
        return None

    tp = raw_pattern.QueryInterface(UIAutomationClient.IUIAutomationTextPattern)

    # GetSelection() returns the caret position as a degenerate text range.
    sel = tp.GetSelection()
    if sel.Length == 0:
        return None

    cursor_range = sel.GetElement(0)

    # Move the START endpoint one character to the left.
    # Endpoint_Start=0, TextUnit_Character=1, count=-1
    moved = cursor_range.MoveEndpointByUnit(0, 1, -1)
    if moved == 0:
        return None  # Already at start of document

    char = cursor_range.GetText(1)
    return char if char else None
