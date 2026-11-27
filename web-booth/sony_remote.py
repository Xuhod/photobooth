"""Windows Imaging Edge Remote shutter bridge and local JPEG intake."""

import ctypes
import os
import shutil
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image


def find_remote_window():
    if os.name != "nt":
        raise OSError("Sony Remote capture requires Windows.")

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    def bind(dll, name, result, *args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, args
        return fn

    B, D, H = wintypes.BOOL, wintypes.DWORD, wintypes.HWND
    L = wintypes.LPARAM
    callback_type = ctypes.WINFUNCTYPE(B, H, L)
    enum = bind(user, "EnumWindows", B, callback_type, L)
    visible = bind(user, "IsWindowVisible", B, H)
    owner = bind(user, "GetWindowThreadProcessId", D, H, ctypes.POINTER(D))
    open_process = bind(kernel, "OpenProcess", wintypes.HANDLE, D, B, D)
    process_path = bind(kernel, "QueryFullProcessImageNameW", B, wintypes.HANDLE, D,
                        wintypes.LPWSTR, ctypes.POINTER(D))
    close = bind(kernel, "CloseHandle", B, wintypes.HANDLE)
    windows = []

    @callback_type
    def collect(hwnd, _):
        if not visible(hwnd):
            return True
        pid = D()
        owner(hwnd, ctypes.byref(pid))
        handle = open_process(0x1000, False, pid.value)  # Query metadata only.
        if not handle:
            return True
        try:
            path = ctypes.create_unicode_buffer(32768)
            size = D(len(path))
            if process_path(handle, 0, path, ctypes.byref(size)):
                exe = Path(path.value)
                if exe.name.casefold() == "remote.exe" and any(
                    "imaging edge" in part.casefold() or part.casefold() == "sony"
                    for part in exe.parts[:-1]
                ):
                    windows.append(hwnd)
        finally:
            close(handle)
        return True

    if not enum(collect, 0) or len(windows) != 1:
        raise OSError("Open exactly one Sony Imaging Edge Remote camera window before starting.")
    return windows[0]


def trigger_shutter():
    hwnd = find_remote_window()
    post = ctypes.WinDLL("user32", use_last_error=True).PostMessageW
    post.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    post.restype = wintypes.BOOL
    # Sony documents the top-row '1' key as Photo. Target only the Remote window,
    # never whichever application happens to be in the foreground.
    if not post(hwnd, 0x0100, ord("1"), 0x00020001) or not post(hwnd, 0x0101, ord("1"), 0xC0020001):
        raise OSError("Could not send the shutter shortcut to Sony Remote.")


def capture_jpeg(folder: Path, destination: Path, *, trigger=trigger_shutter, timeout=15):
    """Copy only a new, completed JPEG; keep the camera's encoded bytes intact."""
    def files():
        return {path for path in folder.iterdir() if path.suffix.lower() in {".jpg", ".jpeg"}}

    before = files()
    trigger()
    deadline = time.monotonic() + timeout
    seen = {}
    while time.monotonic() < deadline:
        for path in sorted(files() - before):
            try:
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                if not stat.st_size or seen.get(path) != signature:
                    seen[path] = signature
                    continue
                with Image.open(path) as photo:
                    if photo.format != "JPEG":
                        continue
                    photo.load()
                shutil.copyfile(path, destination)
                return True
            except OSError:
                continue  # Sony Remote may still be writing the JPEG.
        time.sleep(0.15)
    raise OSError("No new JPEG arrived from Sony Remote. Check the connection and save folder.")
