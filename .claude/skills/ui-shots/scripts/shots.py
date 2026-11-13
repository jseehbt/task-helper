#!/usr/bin/env python3
"""Start the TaskHelper app in demo mode and save a PNG of each of its windows, per Windows colour mode.

The app contract (AGENTS.md, "App-Vertrag für ui-shots"): `--demo` shows
static items without network access and uses its own state directory and
single-instance name, so it neither touches the real state.db nor hands over
to a running instance; `--show-flyout` opens the flyout at start and keeps it
open on focus loss.

For each requested theme the script sets the per-user app colour mode
(HKCU ...\\Themes\\Personalize\\AppsUseLightTheme), broadcasts the change, starts
the app, waits for its windows, captures them with PrintWindow and ends the
app. The original colour mode is restored in every case, also on errors; it
is saved to a temp file first, so a run that gets killed is undone by the next.

Usage (Windows only):
    uv run --no-project python shots.py [--exe PATH] [--out DIR] [--themes light,dark]
                                        [--app-args "--demo --show-flyout"] [--settle SECONDS]

Output: one `shot=PATH theme=… dpi=… size=WxH title=…` line per window.

Exit codes:
    0  screenshots written
    2  usage error, no app executable found
    4  not on Windows
    5  the app exited early or showed no window
"""

from __future__ import annotations

import argparse
import json
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from datetime import datetime
from pathlib import Path

PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
THEME_VALUE = "AppsUseLightTheme"
WINDOW_TIMEOUT_SECONDS = 20  # a cold start of a self-contained single-file exe unpacks first
MIN_WINDOW_PIXELS = 20       # hidden helper windows (tray message window) are smaller or 0x0
# The user's colour mode while a run has changed it; a killed run leaves it behind for the next one.
PENDING_THEME = Path(tempfile.gettempdir()) / "taskhelper-shots-theme.json"


# --- PNG (pure function, testable on any platform) ------------------------

def png_bytes(width: int, height: int, bgra: bytes) -> bytes:
    """Encode top-down 32-bit BGRA pixels as an opaque RGBA PNG."""
    rgba = bytearray(bgra)
    rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
    rgba[3::4] = b"\xff" * (width * height)
    stride = width * 4
    raw = b"".join(b"\x00" + bytes(rgba[y * stride:(y + 1) * stride]) for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


# --- Win32 ------------------------------------------------------------------

def win32():
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    # Explicit signatures: without them ctypes passes handles as 32-bit ints and
    # truncates them on 64-bit Windows.
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    sigs = {
        (user32, "EnumWindows"): ([enum_proc, wt.LPARAM], wt.BOOL),
        (user32, "GetWindowThreadProcessId"): ([wt.HWND, ctypes.POINTER(wt.DWORD)], wt.DWORD),
        (user32, "IsWindowVisible"): ([wt.HWND], wt.BOOL),
        (user32, "GetWindowRect"): ([wt.HWND, ctypes.POINTER(wt.RECT)], wt.BOOL),
        (user32, "GetWindowTextW"): ([wt.HWND, wt.LPWSTR, ctypes.c_int], ctypes.c_int),
        (user32, "GetDpiForWindow"): ([wt.HWND], wt.UINT),
        (user32, "GetWindowDC"): ([wt.HWND], wt.HDC),
        (user32, "ReleaseDC"): ([wt.HWND, wt.HDC], ctypes.c_int),
        (user32, "PrintWindow"): ([wt.HWND, wt.HDC, wt.UINT], wt.BOOL),
        (user32, "SetProcessDpiAwarenessContext"): ([ctypes.c_void_p], wt.BOOL),
        (user32, "SendMessageTimeoutW"): ([wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_wchar_p, wt.UINT, wt.UINT,
                                           ctypes.POINTER(ctypes.c_size_t)], ctypes.c_size_t),
        (gdi32, "CreateCompatibleDC"): ([wt.HDC], wt.HDC),
        (gdi32, "CreateCompatibleBitmap"): ([wt.HDC, ctypes.c_int, ctypes.c_int], wt.HBITMAP),
        (gdi32, "SelectObject"): ([wt.HDC, wt.HGDIOBJ], wt.HGDIOBJ),
        (gdi32, "GetDIBits"): ([wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT],
                               ctypes.c_int),
        (gdi32, "DeleteObject"): ([wt.HGDIOBJ], wt.BOOL),
        (gdi32, "DeleteDC"): ([wt.HDC], wt.BOOL),
    }
    for (dll, name), (args, res) in sigs.items():
        fn = getattr(dll, name)
        fn.argtypes, fn.restype = args, res
    return ctypes, wt, user32, gdi32, enum_proc


class Desktop:
    def __init__(self):
        self.ctypes, self.wt, self.user32, self.gdi32, self.enum_proc = win32()
        # Per-monitor DPI aware v2, so window rectangles are physical pixels.
        self.user32.SetProcessDpiAwarenessContext(self.ctypes.c_void_p(-4))

    def windows_of(self, pid: int) -> list[tuple[int, int, int, str]]:
        found = []
        ctypes, wt, user32 = self.ctypes, self.wt, self.user32

        def callback(hwnd, _):
            owner = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                rect = wt.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                width, height = rect.right - rect.left, rect.bottom - rect.top
                if width > MIN_WINDOW_PIXELS and height > MIN_WINDOW_PIXELS:
                    title = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(hwnd, title, 256)
                    found.append((hwnd, width, height, title.value))
            return True

        user32.EnumWindows(self.enum_proc(callback), 0)
        return found

    def capture(self, hwnd: int, width: int, height: int) -> bytes:
        ctypes, user32, gdi32 = self.ctypes, self.user32, self.gdi32

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 - ctypes reads _fields_ as a class attribute
                ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32), ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32), ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32), ("biClrImportant", ctypes.c_uint32)]

        window_dc = user32.GetWindowDC(hwnd)
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            PW_RENDERFULLCONTENT = 2  # needed for WPF/DirectX content
            user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
            header = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=width, biHeight=-height,
                                      biPlanes=1, biBitCount=32, biCompression=0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            return buffer.raw
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)

    def dpi(self, hwnd: int) -> int:
        return int(self.user32.GetDpiForWindow(hwnd))

    def broadcast_theme_change(self) -> None:
        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x001A, 0x0002
        result = self.ctypes.c_size_t()
        self.user32.SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
                                        SMTO_ABORTIFHUNG, 2000, self.ctypes.byref(result))


def read_theme():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
            return winreg.QueryValueEx(key, THEME_VALUE)[0]
    except FileNotFoundError:
        return None


def write_theme(value) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, PERSONALIZE) as key:
        if value is None:
            try:
                winreg.DeleteValue(key, THEME_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, THEME_VALUE, 0, winreg.REG_DWORD, int(value))


def recover_theme(desktop: Desktop) -> None:
    """Restore the colour mode a killed earlier run (timeout, Ctrl+C) left behind."""
    if not PENDING_THEME.is_file():
        return
    try:
        original = json.loads(PENDING_THEME.read_text(encoding="utf-8")).get("original")
        if original is not None and not isinstance(original, int):
            raise ValueError(original)
        write_theme(original)
    except (ValueError, AttributeError, OSError):
        PENDING_THEME.unlink(missing_ok=True)
        print("warning=theme_not_recovered detail=saved colour mode unreadable; check Settings > Personalization")
        return
    desktop.broadcast_theme_change()
    PENDING_THEME.unlink(missing_ok=True)
    print(f"theme_recovered={original}")


def find_exe(root: Path) -> Path | None:
    candidates = [p for p in root.glob("**/TaskHelper.App/bin/**/*.exe") if "TaskHelper" in p.name]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def shoot(desktop: Desktop, exe: Path, app_args: list[str], theme: str, out: Path, settle: float) -> int:
    process = subprocess.Popen([str(exe), *app_args])
    try:
        deadline = time.monotonic() + WINDOW_TIMEOUT_SECONDS
        windows = []
        while time.monotonic() < deadline and not windows:
            if process.poll() is not None:
                print(f"error=app_exited detail=exit code {process.returncode} (single instance or crash?)",
                      file=sys.stderr)
                return 5
            time.sleep(0.5)
            windows = desktop.windows_of(process.pid)
        if not windows:
            print(f"error=no_window detail=no visible window after {WINDOW_TIMEOUT_SECONDS}s; does the app "
                  "support --show-flyout?", file=sys.stderr)
            return 5
        time.sleep(settle)  # let animations and the first layout pass finish
        for index, (hwnd, width, height, title) in enumerate(desktop.windows_of(process.pid) or windows):
            pixels = desktop.capture(hwnd, width, height)
            dpi = desktop.dpi(hwnd)
            path = out / f"{theme}-dpi{dpi}-{index}.png"
            path.write_bytes(png_bytes(width, height, pixels))
            blank = len({pixels[i:i + 4] for i in range(0, len(pixels), 4 * 97)}) <= 1
            print(f"shot={path.as_posix()} theme={theme} dpi={dpi} size={width}x{height} title={title}"
                  + (" warning=blank_capture" if blank else ""))
        return 0
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--themes", default="light,dark", help="comma list of light, dark, current")
    parser.add_argument("--app-args", default="--demo --show-flyout")
    parser.add_argument("--settle", type=float, default=2.0)
    args = parser.parse_args(argv)

    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    if not themes or any(t not in {"light", "dark", "current"} for t in themes):
        print("error=bad_themes detail=use light, dark or current", file=sys.stderr)
        return 2
    if sys.platform != "win32":
        print("error=windows_only detail=needs the Windows desktop", file=sys.stderr)
        return 4
    exe = args.exe or find_exe(Path.cwd())
    if exe is None or not exe.is_file():
        print("error=exe_not_found detail=build TaskHelper.App first or pass --exe", file=sys.stderr)
        return 2
    out = args.out or Path(tempfile.gettempdir()) / "taskhelper-shots" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"exe={exe.as_posix()} out={out.as_posix()}")

    desktop = Desktop()
    recover_theme(desktop)
    original = read_theme()
    PENDING_THEME.write_text(json.dumps({"original": original}), encoding="utf-8")
    status = 0
    try:
        for theme in themes:
            if theme != "current":
                write_theme(1 if theme == "light" else 0)
                desktop.broadcast_theme_change()
            status = shoot(desktop, exe, shlex.split(args.app_args, posix=False), theme, out, args.settle) or status
    finally:
        write_theme(original)
        desktop.broadcast_theme_change()
        PENDING_THEME.unlink(missing_ok=True)
        print(f"theme_restored={original}")
    return status


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
