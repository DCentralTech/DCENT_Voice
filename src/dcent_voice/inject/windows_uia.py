# DCENT_Voice — open-source, local-first voice dictation
# Copyright (c) 2026 D-Central Technologies — decentralized technologies for digital sovereignty
# SPDX-License-Identifier: MIT
"""Read and classify the focused Windows UI Automation text field.

Browser injection is only proven when the focused control is an editable page
field (textarea, input, or contenteditable). The Chromium address bar is also
an Edit control; treating it as a successful landing site is forbidden.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_ADDRESS_BAR_TOKENS = (
    "address and search",
    "search or enter web address",
    "search or enter address",
    "address bar",
    "omnibox",
)


@dataclass(frozen=True)
class FocusedEditable:
    """Snapshot of the focused UIA control that can receive text."""

    text: str
    name: str
    automation_id: str
    control_type: str
    kind: str


def is_browser_chrome_field(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
) -> bool:
    """True when the focused edit is Chromium chrome, not a page field."""
    blob = f"{name} {automation_id} {control_type}".casefold()
    return any(token in blob for token in _ADDRESS_BAR_TOKENS)


def page_field_name_matches(name: str, candidates: Sequence[str]) -> bool:
    """Match a page field by exact or alias name. Never matches the omnibox."""
    observed = name.casefold().strip()
    if not observed or is_browser_chrome_field(name):
        return False
    wanted = [item.casefold().strip() for item in candidates if item.strip()]
    if observed in wanted:
        return True
    if _looks_like_page_title(observed):
        return False
    return any(
        observed.startswith(f"{candidate} ") and not _looks_like_page_title(observed)
        for candidate in wanted
    )


def _looks_like_page_title(name: str) -> bool:
    """Tab/document titles are not page fields, even when they start with Search."""
    observed = name.casefold()
    return " - " in observed or " · " in observed or "microsoft edge" in observed


def looks_like_navigated_url(text: str) -> bool:
    value = text.strip().casefold()
    return value.startswith("http://") or value.startswith("https://")


_SEARCH_CHROME_LABELS = frozenset(
    {
        "i'm feeling lucky",
        "i’m feeling lucky",
        "google search",
        "advanced search",
        "advanced search:",
        "search google",
        "search duckduckgo",
        "search wikipedia",
        "search the web",
    }
)


def looks_like_search_chrome_label(text: str) -> bool:
    """True when readback grabbed a button/legend, not the search input."""
    value = text.strip().casefold()
    if not value:
        return False
    if value in _SEARCH_CHROME_LABELS:
        return True
    if "settings and more" in value or "address and search" in value:
        return True
    return value.endswith(":") and len(value) < 40


_PAGE_DOCUMENT_FIELD_NAMES = frozenset(
    {
        "article draft",
        "message body",
        "body",
        "compose",
        "meeting notes",
    }
)


def _is_page_document_root(name: str, automation_id: str, control_type: str) -> bool:
    """True for the page Document/RootWebArea, not a contenteditable composer."""
    if "document" not in control_type.casefold():
        return False
    if automation_id == "RootWebArea":
        return True
    observed = name.casefold().strip()
    if not observed:
        return True
    if observed in _PAGE_DOCUMENT_FIELD_NAMES:
        return False
    return _looks_like_page_title(observed) or observed in {
        "google",
        "duckduckgo",
        "github",
        "gmail",
        "wikipedia",
    }


def read_focused_editable(control: Any | None = None) -> FocusedEditable:
    """Read the focused editable via ValuePattern or TextPattern.

    ``control`` is injectable for tests. Production callers omit it and the
    live focused UIA control is used.
    """
    target = control if control is not None else _focused_control()
    if target is None:
        raise RuntimeError("no focused UI Automation control")
    name = str(getattr(target, "Name", "") or "")
    automation_id = str(getattr(target, "AutomationId", "") or "")
    control_type = str(
        getattr(target, "ControlTypeName", "") or getattr(target, "LocalizedControlType", "") or ""
    )
    if is_browser_chrome_field(name, automation_id, control_type):
        raise RuntimeError(f"focused control is browser chrome, not a page field: {name!r}")
    if _is_page_document_root(name, automation_id, control_type):
        raise RuntimeError(f"focused control is the page document, not a field: {name!r}")
    text = _text_from_control(target)
    if looks_like_navigated_url(text) and "document" in control_type.casefold():
        raise RuntimeError(f"focused control is a page document/URL, not a field: {name!r}")
    if text.endswith(":") and len(text) < 40:
        raise RuntimeError(f"focused control is a label, not a field: {text!r}")
    kind = _field_kind(control_type, name, automation_id)
    if kind == "unknown" and not text:
        raise RuntimeError(
            f"focused control is not an editable page field: type={control_type!r} name={name!r}"
        )
    return FocusedEditable(
        text=text,
        name=name,
        automation_id=automation_id,
        control_type=control_type,
        kind=kind,
    )


_COMMON_PAGE_FIELD_NAMES = (
    "Search",
    "Search Google",
    "Search Wikipedia",
    "Search GitHub",
    "Search DuckDuckGo",
    "Search without being tracked",
    "Article draft",
    "Meeting notes",
    "Message Body",
    "Body",
    "Compose",
)


def refocus_page_field(
    *,
    hwnd: int | None = None,
    timeout_s: float = 1.2,
    steal_recovered: bool = False,
) -> bool:
    """Re-assert a page field after overlay/Start-menu/SPA blur.

    The Chromium address bar is never a successful landing site. A blind
    search-box click is used only after a real foreground steal — clicking
    again on an already-focused SPA field lands on labels or the page body.
    """
    if focused_page_search_field(_COMMON_PAGE_FIELD_NAMES) is not None:
        return True
    try:
        snapshot = read_focused_editable()
        if snapshot.kind == "edit" and page_field_name_matches(
            snapshot.name, _COMMON_PAGE_FIELD_NAMES
        ):
            return True
    except Exception:
        pass
    root = _control_from_hwnd(hwnd)
    try:
        focus_page_editable(
            names=_COMMON_PAGE_FIELD_NAMES,
            search_from=root,
            timeout_s=timeout_s,
        )
        return True
    except Exception:
        pass
    try:
        host = _walk_search_host(root or _uia().GetForegroundControl())
    except Exception:
        host = None
    if host is not None and _click_control_input_row(host):
        if steal_recovered:
            _send_ctrl_a()
        return True
    if steal_recovered and hwnd is not None:
        if _click_search_offset(hwnd):
            _send_ctrl_a()
            return True
        return False
    return True


def _click_control_input_row(control: Any) -> bool:
    """Click the first row of a search host, not a tall form's midpoint."""
    rect = getattr(control, "BoundingRectangle", None)
    if rect is None:
        return False
    try:
        left = int(getattr(rect, "left", getattr(rect, "Left", rect[0])))
        top = int(getattr(rect, "top", getattr(rect, "Top", rect[1])))
        width = int(getattr(rect, "width", getattr(rect, "Width", 0)) or 0)
        height = int(getattr(rect, "height", getattr(rect, "Height", 0)) or 0)
        if width == 0 and height == 0:
            width = int(rect[2] - rect[0])
            height = int(rect[3] - rect[1])
    except Exception:
        return False
    if width < 8 or height < 8:
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    x = left + width // 2
    y = top + (min(max(16, height // 5), 28) if height > 48 else height // 2)
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return True


def _click_search_offset(hwnd: int) -> bool:
    """Click a typical live search box below Chromium chrome."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    client = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return False
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return False
    width = max(1, int(client.right - client.left))
    height = max(1, int(client.bottom - client.top))
    x = origin.x + max(40, width // 2)
    y = origin.y + min(max(200, int(height * 0.46)), max(0, height - 16))
    user32.SetCursorPos(x, y)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    return True


def _send_ctrl_a() -> None:
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0x0002, 0)
    user32.keybd_event(0x11, 0, 0x0002, 0)


def focus_named_editable(
    name: str,
    *,
    search_from: Any | None = None,
    timeout_s: float = 5.0,
) -> Any:
    """Focus the first editable whose accessible name matches ``name``."""
    return focus_page_editable(names=(name,), search_from=search_from, timeout_s=timeout_s)


def focus_page_editable(
    *,
    names: Sequence[str],
    search_from: Any | None = None,
    timeout_s: float = 8.0,
    named_only: bool = False,
) -> Any:
    """Focus a page field by alias list, then the first RootWebArea edit.

    Delayed-focus SPAs are waited out. The Chromium address bar is never a
    successful match, even when its name contains ``Search``.
    """
    auto = _uia()
    candidates = tuple(item for item in names if item.strip())
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            focused = _focused_control()
            if _is_named_page_field(focused, candidates) and _is_editable_type(focused):
                return focused
            root = search_from if search_from is not None else auto.GetForegroundControl()
            match = _walk_named_editable(root, candidates)
            if match is None and not named_only:
                web = _walk_root_web_area(root)
                match = _first_page_editable(web)
            if match is not None:
                setter = getattr(match, "SetFocus", None)
                if callable(setter):
                    setter()
                return match
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"no editable page field named {candidates!r}")


def _is_named_page_field(control: Any | None, wanted: str | Sequence[str]) -> bool:
    if control is None:
        return False
    name = str(getattr(control, "Name", "") or "")
    automation_id = str(getattr(control, "AutomationId", "") or "")
    control_type = str(getattr(control, "ControlTypeName", "") or "")
    if is_browser_chrome_field(name, automation_id, control_type):
        return False
    candidates = (wanted,) if isinstance(wanted, str) else wanted
    return page_field_name_matches(name, candidates) or page_field_name_matches(
        automation_id, candidates
    )


def _is_editable_type(control: Any | None) -> bool:
    if control is None:
        return False
    control_type = str(getattr(control, "ControlTypeName", "") or "").casefold()
    return "edit" in control_type or "document" in control_type or "combo" in control_type


def _is_field_edit(control: Any | None) -> bool:
    """True for input/combobox fields. Page documents are not search boxes."""
    if control is None:
        return False
    control_type = str(getattr(control, "ControlTypeName", "") or "").casefold()
    return "edit" in control_type or "combo" in control_type


_SEARCH_HOST_IDS = frozenset(
    {
        "searchform",
        "p-search",
        "search",
        "q",
        "searchinput",
        "search-input",
        "search-box",
    }
)


def _walk_search_host(
    root: Any | None,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any | None:
    """Find a page search host (form/group) that is not Chromium chrome."""
    if root is None or depth > 40:
        return None
    if seen is None:
        seen = set()
    marker = id(root)
    if marker in seen:
        return None
    seen.add(marker)
    name = str(getattr(root, "Name", "") or "")
    automation_id = str(getattr(root, "AutomationId", "") or "")
    control_type = str(getattr(root, "ControlTypeName", "") or "")
    aid = automation_id.casefold()
    if (
        not is_browser_chrome_field(name, automation_id, control_type)
        and not _looks_like_page_title(name)
        and _has_visible_rect(root)
        and (
            aid in _SEARCH_HOST_IDS
            or (
                name.casefold().strip() in {"search", "search wikipedia", "search github"}
                and "document" not in control_type.casefold()
            )
        )
    ):
        return root
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for child in children or []:
        match = _walk_search_host(child, depth=depth + 1, seen=seen)
        if match is not None:
            return match
    return None


def _walk_named_editable(
    root: Any | None,
    wanted: str | Sequence[str],
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any | None:
    if root is None or depth > 40:
        return None
    if seen is None:
        seen = set()
    marker = id(root)
    if marker in seen:
        return None
    seen.add(marker)
    if _is_named_page_field(root, wanted) and _is_editable_type(root):
        return root
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for child in children or []:
        match = _walk_named_editable(child, wanted, depth=depth + 1, seen=seen)
        if match is not None:
            return match
    return None


def _walk_root_web_area(
    root: Any | None,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any | None:
    if root is None or depth > 40:
        return None
    if seen is None:
        seen = set()
    marker = id(root)
    if marker in seen:
        return None
    seen.add(marker)
    automation_id = str(getattr(root, "AutomationId", "") or "")
    if automation_id == "RootWebArea":
        return root
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for child in children or []:
        match = _walk_root_web_area(child, depth=depth + 1, seen=seen)
        if match is not None:
            return match
    return None


def _first_page_editable(
    root: Any | None,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> Any | None:
    """First visible non-chrome edit under a page root (never the omnibox)."""
    if root is None or depth > 40:
        return None
    if seen is None:
        seen = set()
    marker = id(root)
    if marker in seen:
        return None
    seen.add(marker)
    name = str(getattr(root, "Name", "") or "")
    automation_id = str(getattr(root, "AutomationId", "") or "")
    control_type = str(getattr(root, "ControlTypeName", "") or "")
    if (
        _is_field_edit(root)
        and not is_browser_chrome_field(name, automation_id, control_type)
        and automation_id != "RootWebArea"
        and not _looks_like_page_title(name)
        and _has_visible_rect(root)
    ):
        return root
    try:
        children = root.GetChildren()
    except Exception:
        return None
    for child in children or []:
        match = _first_page_editable(child, depth=depth + 1, seen=seen)
        if match is not None:
            return match
    return None


def _rect_dimension(rect: Any, *names: str) -> int:
    for name in names:
        value = getattr(rect, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            continue
        if number:
            return number
    return 0


def _has_visible_rect(control: Any) -> bool:
    rect = getattr(control, "BoundingRectangle", None)
    if rect is None:
        return True
    width = _rect_dimension(rect, "width", "Width")
    height = _rect_dimension(rect, "height", "Height")
    if width == 0 and height == 0:
        try:
            width = int(rect[2] - rect[0])
            height = int(rect[3] - rect[1])
        except Exception:
            return True
    return width > 8 and height > 8


def set_focused_editable_text(text: str, *, control: Any | None = None) -> None:
    """Replace the focused field. Prefer ValuePattern; otherwise fail closed."""
    target = control if control is not None else _focused_control()
    if target is None:
        raise RuntimeError("no focused UI Automation control")
    if is_browser_chrome_field(
        str(getattr(target, "Name", "") or ""),
        str(getattr(target, "AutomationId", "") or ""),
        str(getattr(target, "ControlTypeName", "") or ""),
    ):
        raise RuntimeError("refusing to write into browser chrome")
    setter = _value_setter(target)
    if setter is None:
        raise RuntimeError("focused control has no ValuePattern setter")
    setter(text)


def _field_kind(control_type: str, name: str, automation_id: str) -> str:
    blob = f"{control_type} {name} {automation_id}".casefold()
    if "document" in blob:
        return "contenteditable"
    if "edit" in blob or "text" in blob or "combo" in blob:
        return "edit"
    return "unknown"


def _text_from_control(control: Any) -> str:
    getter = _value_getter(control)
    if getter is not None:
        try:
            value = getter()
        except Exception:
            value = None
        if value is not None:
            return str(value)
    pattern_fn = getattr(control, "GetTextPattern", None)
    if callable(pattern_fn):
        try:
            pattern = pattern_fn()
        except Exception:
            pattern = None
        if pattern is not None:
            document = getattr(pattern, "DocumentRange", None)
            get_text = getattr(document, "GetText", None) if document is not None else None
            if callable(get_text):
                try:
                    return str(get_text(-1) or "")
                except Exception:
                    pass
    return str(getattr(control, "Name", "") or "")


def _value_getter(control: Any):
    pattern_fn = getattr(control, "GetValuePattern", None)
    if not callable(pattern_fn):
        return None
    try:
        pattern = pattern_fn()
    except Exception:
        return None
    if pattern is None:
        return None
    if callable(getattr(pattern, "Value", None)):
        return pattern.Value
    if hasattr(pattern, "Value"):
        return lambda: pattern.Value
    getter = getattr(pattern, "GetValue", None)
    if callable(getter):
        return getter
    return None


def _value_setter(control: Any):
    pattern_fn = getattr(control, "GetValuePattern", None)
    if not callable(pattern_fn):
        return None
    try:
        pattern = pattern_fn()
    except Exception:
        return None
    if pattern is None:
        return None
    setter = getattr(pattern, "SetValue", None)
    if callable(setter):
        return setter
    return None


def _control_from_hwnd(hwnd: int | None) -> Any | None:
    """Bound the UIA walk to the owned window, not the overlay."""
    if not hwnd:
        return None
    getter = getattr(_uia(), "ControlFromHandle", None)
    if not callable(getter):
        return None
    try:
        return getter(int(hwnd))
    except Exception:
        return None


def focused_page_search_field(names: Sequence[str]) -> Any | None:
    """Google's search box is a focused ComboBox that often never appears in the hwnd walk."""
    focused = _focused_control()
    if focused is None or not _is_editable_type(focused):
        return None
    if not _is_named_page_field(focused, names):
        return None
    return focused


def _focused_control() -> Any | None:
    auto = _uia()
    getter = getattr(auto, "GetFocusedControl", None)
    if not callable(getter):
        return None
    return getter()


def _uia() -> Any:
    try:
        import uiautomation as auto
    except ImportError as exc:  # pragma: no cover - Windows dependency
        raise RuntimeError("uiautomation is required for web-field readback") from exc
    return auto
