"""Pure rename argument checks shared by execution and approval boundaries."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def file_argument_error(tool: str, parameters: dict) -> str | None:
    """Reject incomplete/invalid rename requests without guessing or doing I/O."""
    if tool != "file_controller" or str(parameters.get("action", "")).strip().lower() != "rename":
        return None
    new_name = parameters.get("new_name")
    if not isinstance(new_name, str) or not new_name.strip():
        return (
            "Could not rename: supply the destination filename in 'new_name'. "
            "Use 'path' for the existing file, or its parent with 'name' for the existing filename."
        )
    if (
        any(char in new_name for char in '/\\:<>"|?*')
        or any(ord(char) < 32 for char in new_name)
        or new_name in (".", "..")
        or new_name.endswith((" ", "."))
        or new_name.split(".", 1)[0].upper().rstrip(" ") in _RESERVED_NAMES
    ):
        return (
            "Could not rename: 'new_name' must be a valid filename in the same folder, "
            "without a path, reserved device name, or trailing space/dot. Use 'move' to change folders."
        )
    if "path" in parameters and (
        not isinstance(parameters["path"], str) or not parameters["path"].strip()
    ):
        return "Could not rename: 'path' must be a nonempty existing file or parent folder path."
    if "name" in parameters and not isinstance(parameters["name"], str):
        return "Could not rename: 'name' must be the existing filename, not the destination."
    if not parameters.get("path") and not str(parameters.get("name", "")).strip():
        return "Could not rename: specify the existing file in 'path' or its name in 'name'."
    return None


def rename_source_label(parameters: dict) -> str:
    """Render the supplied source consistently without resolving a different file."""
    path = parameters.get("path") or "desktop"
    name = parameters.get("name") or ""
    if not isinstance(path, str) or not isinstance(name, str):
        return ""
    if not name:
        return path
    path_type = PureWindowsPath if "\\" in path + name or ":" in path + name else PurePosixPath
    return str(path_type(path) / name)
