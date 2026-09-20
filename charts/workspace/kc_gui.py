#!/usr/bin/env python3
"""kc-gui — drive and capture the agent-only X display (:98).

Issue #716, Gap 1: kube-coder used to have exactly one virtual display, so a
GUI app an agent launched rendered into the same framebuffer the human was
watching over VNC, stole fluxbox focus, and showed up in their Browser tab.
`start.sh` now also runs an `Xvfb :98` that `x11vnc` does *not* export. This
script is the agent-side handle on that display: launch an app, list windows,
screenshot, click, type.

Deliberately stdlib-only. The image ships `xvfb`, `x11vnc`, `fluxbox`,
`xterm` and `x11-xserver-utils`, but **not** `xdotool` and **not** ImageMagick
`import`, so the usual "just shell out" recipes do not exist here. Rather than
grow the image, we talk to libX11/libXtst through ctypes (both are already
pulled in as x11vnc/Xvfb runtime deps) and encode PNGs with `zlib`.

This is visual separation, not a security boundary: X11 has no seat isolation,
so anything that can open :98 can also open :99. See the issue for the
nested-compositor options that would give a real boundary.

Usage:
    kc-gui info
    kc-gui run xterm -geometry 100x30
    kc-gui windows
    kc-gui screenshot -o /tmp/shot.png
    kc-gui click 400 300
    kc-gui type 'echo hello'
    kc-gui key Return
    kc-gui key ctrl+c
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import os
import struct
import subprocess
import sys
import time
import zlib
from typing import NamedTuple

# The display start.sh reserves for agents. Never :99 — that one is the
# human's, and the whole point of this script is to stay off it.
DEFAULT_DISPLAY = os.environ.get("KC_AGENT_DISPLAY") or ":98"

# ── Xlib constants (X11/X.h, X11/Xutil.h) ────────────────────────────────────
Z_PIXMAP = 2
ALL_PLANES = 0xFFFFFFFFFFFFFFFF
IS_VIEWABLE = 2
CURRENT_TIME = 0

XK_Shift_L = 0xFFE1
XK_Control_L = 0xFFE3
XK_Alt_L = 0xFFE9
XK_Super_L = 0xFFEB

MODIFIER_KEYSYMS = {
    "shift": XK_Shift_L,
    "ctrl": XK_Control_L,
    "control": XK_Control_L,
    "alt": XK_Alt_L,
    "meta": XK_Alt_L,
    "super": XK_Super_L,
    "win": XK_Super_L,
    "cmd": XK_Super_L,
}

# Characters whose X keysym name is not simply the character itself.
CHAR_KEYSYMS = {
    "\n": 0xFF0D,  # Return
    "\r": 0xFF0D,
    "\t": 0xFF09,  # Tab
    "\b": 0xFF08,  # BackSpace
    "\x1b": 0xFF1B,  # Escape
}


class Framebuffer(NamedTuple):
    """The XImage fields the ZPixmap -> RGB conversion needs."""

    width: int
    height: int
    stride: int          # bytes_per_line: >= width * step, X pads scanlines
    bits_per_pixel: int
    red_mask: int
    green_mask: int
    blue_mask: int


class KcGuiError(RuntimeError):
    """Anything the caller can act on: no display, no such window, bad args."""


# ── ctypes bindings ──────────────────────────────────────────────────────────


class _XImageFuncs(ctypes.Structure):
    _fields_ = [
        ("create_image", ctypes.c_void_p),
        ("destroy_image", ctypes.c_void_p),
        ("get_pixel", ctypes.c_void_p),
        ("put_pixel", ctypes.c_void_p),
        ("sub_image", ctypes.c_void_p),
        ("add_pixel", ctypes.c_void_p),
    ]


class XImage(ctypes.Structure):
    """X11/Xlib.h `struct _XImage`. ctypes reproduces the 64-bit padding."""

    _fields_ = [
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("xoffset", ctypes.c_int),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("byte_order", ctypes.c_int),
        ("bitmap_unit", ctypes.c_int),
        ("bitmap_bit_order", ctypes.c_int),
        ("bitmap_pad", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("bytes_per_line", ctypes.c_int),
        ("bits_per_pixel", ctypes.c_int),
        ("red_mask", ctypes.c_ulong),
        ("green_mask", ctypes.c_ulong),
        ("blue_mask", ctypes.c_ulong),
        ("obdata", ctypes.c_void_p),
        ("f", _XImageFuncs),
    ]


class XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


def _load(name: str) -> ctypes.CDLL:
    path = ctypes.util.find_library(name)
    if not path:
        # find_library shells out to ldconfig/gcc; on a slim image the SONAME
        # is still present even when the -dev symlink is not.
        path = {"X11": "libX11.so.6", "Xtst": "libXtst.so.6"}.get(name, "")
    try:
        return ctypes.CDLL(path or f"lib{name}.so.6")
    except OSError as exc:  # pragma: no cover - only on a broken image
        raise KcGuiError(
            f"lib{name} is missing from this image; kc-gui cannot drive X "
            f"without it ({exc})"
        ) from exc


class X11:
    """Thin ctypes facade over the handful of Xlib/XTest calls we need."""

    def __init__(self, display: str):
        self.xlib = _load("X11")
        self.xtst = _load("Xtst")
        self._declare()
        self.display_name = display
        self.dpy = self.xlib.XOpenDisplay(display.encode())
        if not self.dpy:
            raise KcGuiError(
                f"cannot open X display {display}. Is it running? "
                f"(`pgrep -af 'Xvfb {display}'`). The agent display is started "
                f"by start.sh only when browser.enabled and "
                f"browser.agentDisplay are both true."
            )
        self.root = self.xlib.XDefaultRootWindow(self.dpy)
        self.screen = self.xlib.XDefaultScreen(self.dpy)

    def _declare(self) -> None:
        x = self.xlib
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XDefaultScreen.restype = ctypes.c_int
        x.XDefaultScreen.argtypes = [ctypes.c_void_p]
        x.XGetImage.restype = ctypes.POINTER(XImage)
        x.XGetImage.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        x.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(XWindowAttributes),
        ]
        x.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x.XFetchName.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_char_p),
        ]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XSetInputFocus.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        x.XDisplayKeycodes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        x.XGetKeyboardMapping.restype = ctypes.POINTER(ctypes.c_ulong)
        x.XGetKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ubyte,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        x.XChangeKeyboardMapping.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_int,
        ]
        x.XKeysymToKeycode.restype = ctypes.c_ubyte
        x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        x.XStringToKeysym.restype = ctypes.c_ulong
        x.XStringToKeysym.argtypes = [ctypes.c_char_p]

        t = self.xtst
        t.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        t.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        t.XTestFakeKeyEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_ulong,
        ]

    # ── geometry ─────────────────────────────────────────────────────────────

    def geometry(self, window: int | None = None) -> tuple[int, int, int, int]:
        attrs = XWindowAttributes()
        win = self.root if window is None else window
        if not self.xlib.XGetWindowAttributes(self.dpy, win, ctypes.byref(attrs)):
            raise KcGuiError(f"no such window: 0x{win:x}")
        return attrs.x, attrs.y, attrs.width, attrs.height

    # ── capture ──────────────────────────────────────────────────────────────

    def capture(self, window: int | None = None) -> tuple[int, int, list[bytes]]:
        """Grab a drawable and return (width, height, RGB rows)."""
        win = self.root if window is None else window
        _, _, width, height = self.geometry(win)
        img_ptr = self.xlib.XGetImage(
            self.dpy, win, 0, 0, width, height, ALL_PLANES, Z_PIXMAP
        )
        if not img_ptr:
            raise KcGuiError(f"XGetImage failed for 0x{win:x} ({width}x{height})")
        img = img_ptr.contents
        raw = ctypes.string_at(img.data, img.bytes_per_line * img.height)
        fb = Framebuffer(
            width=img.width,
            height=img.height,
            stride=img.bytes_per_line,
            bits_per_pixel=img.bits_per_pixel,
            red_mask=img.red_mask,
            green_mask=img.green_mask,
            blue_mask=img.blue_mask,
        )
        return fb.width, fb.height, _rows_from_ximage(raw, fb)

    # ── windows ──────────────────────────────────────────────────────────────

    def window_name(self, window: int) -> str:
        name = ctypes.c_char_p()
        if self.xlib.XFetchName(self.dpy, window, ctypes.byref(name)) and name.value:
            text = name.value.decode("utf-8", "replace")
            self.xlib.XFree(name)
            return text
        return ""

    def windows(self, root: int | None = None, depth: int = 0) -> list[dict]:
        """Viewable, named windows under `root`.

        fluxbox reparents clients into its own frames, so the interesting
        window is usually a grandchild of the root rather than a child.
        """
        parent = self.root if root is None else root
        root_ret = ctypes.c_ulong()
        parent_ret = ctypes.c_ulong()
        children = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint()
        ok = self.xlib.XQueryTree(
            self.dpy,
            parent,
            ctypes.byref(root_ret),
            ctypes.byref(parent_ret),
            ctypes.byref(children),
            ctypes.byref(count),
        )
        if not ok:
            return []
        found: list[dict] = []
        for i in range(count.value):
            win = children[i]
            attrs = XWindowAttributes()
            if not self.xlib.XGetWindowAttributes(self.dpy, win, ctypes.byref(attrs)):
                continue
            if attrs.map_state == IS_VIEWABLE:
                name = self.window_name(win)
                if name:
                    found.append(
                        {
                            "id": f"0x{win:x}",
                            "name": name,
                            "x": attrs.x,
                            "y": attrs.y,
                            "width": attrs.width,
                            "height": attrs.height,
                        }
                    )
            if depth < 3:
                found.extend(self.windows(win, depth + 1))
        if children:
            self.xlib.XFree(children)
        return found

    def focus(self, window: int) -> None:
        self.xlib.XRaiseWindow(self.dpy, window)
        # RevertToPointerRoot == 1
        self.xlib.XSetInputFocus(self.dpy, window, 1, CURRENT_TIME)
        self.xlib.XSync(self.dpy, 0)

    # ── input ────────────────────────────────────────────────────────────────

    def move(self, x: int, y: int) -> None:
        self.xtst.XTestFakeMotionEvent(self.dpy, self.screen, x, y, 0)
        self.xlib.XSync(self.dpy, 0)

    def click(self, x: int | None, y: int | None, button: int = 1) -> None:
        if x is not None and y is not None:
            self.move(x, y)
        self.xtst.XTestFakeButtonEvent(self.dpy, button, 1, 0)
        self.xtst.XTestFakeButtonEvent(self.dpy, button, 0, 0)
        self.xlib.XSync(self.dpy, 0)

    def _keymap(self) -> dict[int, tuple[int, bool]]:
        """keysym -> (keycode, needs_shift), built from the server's mapping."""
        lo = ctypes.c_int()
        hi = ctypes.c_int()
        self.xlib.XDisplayKeycodes(self.dpy, ctypes.byref(lo), ctypes.byref(hi))
        per = ctypes.c_int()
        count = hi.value - lo.value + 1
        syms = self.xlib.XGetKeyboardMapping(
            self.dpy, lo.value, count, ctypes.byref(per)
        )
        table: dict[int, tuple[int, bool]] = {}
        self._free_keycodes: list[int] = []
        for i in range(count):
            keycode = lo.value + i
            row = [syms[i * per.value + j] for j in range(per.value)]
            if not any(row):
                self._free_keycodes.append(keycode)
                continue
            for level, keysym in enumerate(row[:2]):
                if keysym and keysym not in table:
                    table[keysym] = (keycode, level == 1)
        self.xlib.XFree(syms)
        self._keysyms_per_keycode = per.value
        return table

    def _keycode_for(self, keysym: int) -> tuple[int, bool]:
        if not hasattr(self, "_keymap_cache"):
            self._keymap_cache = self._keymap()
        if keysym in self._keymap_cache:
            return self._keymap_cache[keysym]
        direct = self.xlib.XKeysymToKeycode(self.dpy, keysym)
        if direct:
            return int(direct), False
        # Not on the us layout at all (an emoji, a dead accent). Bind it to a
        # spare keycode for the duration of this process — the xdotool trick.
        if not self._free_keycodes:
            raise KcGuiError(
                f"no free keycode left to bind keysym 0x{keysym:x}; "
                f"restart kc-gui or send the character another way"
            )
        keycode = self._free_keycodes.pop()
        per = self._keysyms_per_keycode
        buf = (ctypes.c_ulong * per)()
        for j in range(per):
            buf[j] = keysym
        self.xlib.XChangeKeyboardMapping(self.dpy, keycode, per, buf, 1)
        self.xlib.XSync(self.dpy, 0)
        self._keymap_cache[keysym] = (keycode, False)
        return keycode, False

    def _tap(self, keysym: int, modifiers: list[int], delay: float) -> None:
        mod_codes = [self._keycode_for(m)[0] for m in modifiers]
        keycode, shift = self._keycode_for(keysym)
        if shift:
            mod_codes.append(self._keycode_for(XK_Shift_L)[0])
        for code in mod_codes:
            self.xtst.XTestFakeKeyEvent(self.dpy, code, 1, 0)
        self.xtst.XTestFakeKeyEvent(self.dpy, keycode, 1, 0)
        self.xtst.XTestFakeKeyEvent(self.dpy, keycode, 0, 0)
        for code in reversed(mod_codes):
            self.xtst.XTestFakeKeyEvent(self.dpy, code, 0, 0)
        self.xlib.XSync(self.dpy, 0)
        if delay:
            time.sleep(delay)

    def type_text(self, text: str, delay: float = 0.012) -> None:
        for char in text:
            self._tap(char_keysym(char), [], delay)

    def key(self, combo: str, delay: float = 0.0) -> None:
        modifiers, name = parse_combo(combo)
        keysym = self.xlib.XStringToKeysym(name.encode())
        if not keysym:
            if len(name) == 1:
                keysym = char_keysym(name)
            else:
                raise KcGuiError(f"unknown key name: {name!r}")
        self._tap(keysym, modifiers, delay)

    def close(self) -> None:
        if getattr(self, "dpy", None):
            self.xlib.XCloseDisplay(self.dpy)
            self.dpy = None


# ── pure helpers (unit-tested without an X server) ───────────────────────────


def char_keysym(char: str) -> int:
    """X keysym for a single character."""
    if char in CHAR_KEYSYMS:
        return CHAR_KEYSYMS[char]
    codepoint = ord(char)
    if 0x20 <= codepoint <= 0x7E:
        # Latin-1 keysyms are their own codepoints.
        return codepoint
    if 0xA0 <= codepoint <= 0xFF:
        return codepoint
    # Everything else uses the Unicode keysym range (X11 protocol appendix A).
    return 0x01000000 + codepoint


def parse_combo(combo: str) -> tuple[list[int], str]:
    """'ctrl+shift+s' -> ([XK_Control_L, XK_Shift_L], 's')."""
    parts = [p for p in combo.split("+") if p != ""]
    if not parts:
        raise KcGuiError("empty key combination")
    # A trailing '+' means the key itself is plus: 'ctrl++'.
    if combo.endswith("+") and len(parts) >= 1:
        parts.append("plus")
    modifiers: list[int] = []
    for part in parts[:-1]:
        keysym = MODIFIER_KEYSYMS.get(part.lower())
        if keysym is None:
            raise KcGuiError(f"unknown modifier: {part!r}")
        modifiers.append(keysym)
    return modifiers, parts[-1]


def _rows_from_ximage(raw: bytes, fb: Framebuffer) -> list[bytes]:
    """Convert a ZPixmap buffer into per-row RGB triples."""
    if fb.bits_per_pixel not in (24, 32):
        raise KcGuiError(
            f"unsupported framebuffer depth ({fb.bits_per_pixel} bpp); "
            f"the agent display is created as 24-bit truecolor"
        )
    step = fb.bits_per_pixel // 8
    r_shift = _mask_shift(fb.red_mask)
    g_shift = _mask_shift(fb.green_mask)
    b_shift = _mask_shift(fb.blue_mask)
    # The overwhelmingly common Xvfb case: depth 24 in 32 bits, LSB-first, so
    # the bytes are already B,G,R,X and a slice-reverse beats per-pixel math.
    fast_bgrx = (
        step == 4
        and fb.red_mask == 0xFF0000
        and fb.green_mask == 0xFF00
        and fb.blue_mask == 0xFF
    )
    rows: list[bytes] = []
    for y in range(fb.height):
        base = y * fb.stride
        line = raw[base : base + fb.width * step]
        out = bytearray(fb.width * 3)
        if fast_bgrx:
            out[0::3] = line[2::4]
            out[1::3] = line[1::4]
            out[2::3] = line[0::4]
            rows.append(bytes(out))
            continue
        for x in range(fb.width):
            off = x * step
            pixel = int.from_bytes(line[off : off + step], "little")
            out[x * 3] = (pixel & fb.red_mask) >> r_shift
            out[x * 3 + 1] = (pixel & fb.green_mask) >> g_shift
            out[x * 3 + 2] = (pixel & fb.blue_mask) >> b_shift
        rows.append(bytes(out))
    return rows


def _mask_shift(mask: int) -> int:
    if mask == 0:
        return 0
    shift = 0
    while not mask & 1:
        mask >>= 1
        shift += 1
    return shift


def encode_png(width: int, height: int, rows: list[bytes]) -> bytes:
    """Minimal RGB8 PNG encoder (stdlib zlib only — no Pillow in the image)."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    # Filter type 0 (None) on every scanline: Xvfb output compresses fine and
    # this keeps the encoder trivially auditable.
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def parse_window_id(value: str) -> int:
    """Accept '0x1e00003' or '31457283'."""
    try:
        return int(value, 16) if value.lower().startswith("0x") else int(value, 10)
    except ValueError as exc:
        raise KcGuiError(f"not a window id: {value!r}") from exc


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_info(args) -> int:
    x11 = X11(args.display)
    try:
        _, _, width, height = x11.geometry()
        wins = x11.windows()
        print(f"display    {args.display}")
        print(f"root       {width}x{height}")
        print(f"windows    {len(wins)}")
        for win in wins:
            print(f"  {win['id']}  {win['width']}x{win['height']}  {win['name']}")
    finally:
        x11.close()
    return 0


def cmd_windows(args) -> int:
    x11 = X11(args.display)
    try:
        for win in x11.windows():
            print(
                f"{win['id']}\t{win['width']}x{win['height']}"
                f"+{win['x']}+{win['y']}\t{win['name']}"
            )
    finally:
        x11.close()
    return 0


def cmd_run(args) -> int:
    if not args.command:
        raise KcGuiError("kc-gui run needs a command")
    env = dict(os.environ)
    env["DISPLAY"] = args.display
    log_path = args.log or f"/tmp/kc-gui-{os.path.basename(args.command[0])}.log"
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(  # noqa: S603 - caller-supplied command by design
            args.command,
            env=env,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    print(f"pid {proc.pid} on {args.display} (log: {log_path})")
    return 0


def cmd_screenshot(args) -> int:
    x11 = X11(args.display)
    try:
        window = parse_window_id(args.window) if args.window else None
        width, height, rows = x11.capture(window)
        png = encode_png(width, height, rows)
    finally:
        x11.close()
    if args.output == "-":
        sys.stdout.buffer.write(png)
    else:
        with open(args.output, "wb") as handle:
            handle.write(png)
        print(f"{args.output} ({width}x{height}, {len(png)} bytes)")
    return 0


def cmd_click(args) -> int:
    x11 = X11(args.display)
    try:
        x11.click(args.x, args.y, args.button)
    finally:
        x11.close()
    return 0


def cmd_move(args) -> int:
    x11 = X11(args.display)
    try:
        x11.move(args.x, args.y)
    finally:
        x11.close()
    return 0


def cmd_type(args) -> int:
    x11 = X11(args.display)
    try:
        if args.window:
            x11.focus(parse_window_id(args.window))
        x11.type_text(args.text, args.delay)
    finally:
        x11.close()
    return 0


def cmd_key(args) -> int:
    x11 = X11(args.display)
    try:
        if args.window:
            x11.focus(parse_window_id(args.window))
        for combo in args.keys:
            x11.key(combo, args.delay)
    finally:
        x11.close()
    return 0


def cmd_focus(args) -> int:
    x11 = X11(args.display)
    try:
        x11.focus(parse_window_id(args.window))
    finally:
        x11.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kc-gui",
        description=(
            "Drive and capture the agent-only X display. Defaults to "
            f"{DEFAULT_DISPLAY}, which x11vnc does not export, so nothing here "
            "reaches the human's Browser tab. Visual separation only — X11 has "
            "no seat isolation."
        ),
    )
    parser.add_argument(
        "-d",
        "--display",
        default=DEFAULT_DISPLAY,
        help=f"X display to target (default {DEFAULT_DISPLAY}, $KC_AGENT_DISPLAY)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="display geometry and window list").set_defaults(
        func=cmd_info
    )
    sub.add_parser("windows", help="list viewable named windows").set_defaults(
        func=cmd_windows
    )

    run = sub.add_parser("run", help="launch a GUI app on the agent display")
    run.add_argument("--log", help="append stdout/stderr here")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=cmd_run)

    shot = sub.add_parser("screenshot", help="write a PNG of the display")
    shot.add_argument("-o", "--output", default="/tmp/kc-gui.png", help="'-' = stdout")
    shot.add_argument("-w", "--window", help="capture one window instead of the root")
    shot.set_defaults(func=cmd_screenshot)

    click = sub.add_parser("click", help="move the pointer and click")
    click.add_argument("x", type=int)
    click.add_argument("y", type=int)
    click.add_argument("-b", "--button", type=int, default=1)
    click.set_defaults(func=cmd_click)

    move = sub.add_parser("move", help="move the pointer")
    move.add_argument("x", type=int)
    move.add_argument("y", type=int)
    move.set_defaults(func=cmd_move)

    typer = sub.add_parser("type", help="type a literal string")
    typer.add_argument("text")
    typer.add_argument("-w", "--window", help="focus this window first")
    typer.add_argument("--delay", type=float, default=0.012)
    typer.set_defaults(func=cmd_type)

    key = sub.add_parser("key", help="tap keys, e.g. Return or ctrl+shift+t")
    key.add_argument("keys", nargs="+")
    key.add_argument("-w", "--window", help="focus this window first")
    key.add_argument("--delay", type=float, default=0.03)
    key.set_defaults(func=cmd_key)

    focus = sub.add_parser("focus", help="raise and focus a window")
    focus.add_argument("window")
    focus.set_defaults(func=cmd_focus)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KcGuiError as exc:
        print(f"kc-gui: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
