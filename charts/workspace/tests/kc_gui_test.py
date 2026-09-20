"""Unit tests for kc_gui.py — the agent-only X display helper (#716).

The X11 calls need a live server, so these drive the pure layers that sit
either side of them: keysym resolution, key-combo parsing, the ZPixmap ->
RGB conversion, and the stdlib PNG encoder that stands in for the
ImageMagick `import` the image does not ship. No display is opened.
"""

from __future__ import annotations

import contextlib
import io
import os
import struct
import sys
import unittest
import zlib
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import kc_gui  # noqa: E402


def decode_png(blob: bytes) -> tuple[int, int, list[bytes]]:
    """Minimal reader for the subset this encoder emits (RGB8, filter 0)."""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(blob):
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        kind = blob[pos + 4 : pos + 8]
        payload = blob[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", blob[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + payload) & 0xFFFFFFFF, kind
        chunks.setdefault(kind, b"")
        chunks[kind] += payload
        pos += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, colour) == (8, 2), "expected 8-bit truecolour RGB"
    raw = zlib.decompress(chunks[b"IDAT"])
    stride = width * 3 + 1
    rows = []
    for y in range(height):
        line = raw[y * stride : (y + 1) * stride]
        assert line[0] == 0, "only filter type 0 is emitted"
        rows.append(line[1:])
    return width, height, rows


class CharKeysymTest(unittest.TestCase):
    def test_ascii_is_its_own_keysym(self):
        self.assertEqual(kc_gui.char_keysym("a"), 0x61)
        self.assertEqual(kc_gui.char_keysym("A"), 0x41)
        self.assertEqual(kc_gui.char_keysym(" "), 0x20)
        self.assertEqual(kc_gui.char_keysym("~"), 0x7E)

    def test_control_characters_map_to_named_keys(self):
        # Typing a multi-line string must press Return, not try to bind the
        # 0x0a codepoint to a spare keycode.
        self.assertEqual(kc_gui.char_keysym("\n"), 0xFF0D)
        self.assertEqual(kc_gui.char_keysym("\r"), 0xFF0D)
        self.assertEqual(kc_gui.char_keysym("\t"), 0xFF09)
        self.assertEqual(kc_gui.char_keysym("\x1b"), 0xFF1B)

    def test_latin1_high_range_is_direct(self):
        self.assertEqual(kc_gui.char_keysym("é"), 0xE9)

    def test_beyond_latin1_uses_the_unicode_range(self):
        # X11 protocol appendix A: keysym = 0x01000000 + codepoint.
        self.assertEqual(kc_gui.char_keysym("→"), 0x01000000 + 0x2192)
        self.assertEqual(kc_gui.char_keysym("😀"), 0x01000000 + 0x1F600)


class ParseComboTest(unittest.TestCase):
    def test_bare_key(self):
        self.assertEqual(kc_gui.parse_combo("Return"), ([], "Return"))

    def test_modifiers_are_resolved_in_order(self):
        mods, key = kc_gui.parse_combo("ctrl+shift+t")
        self.assertEqual(mods, [kc_gui.XK_Control_L, kc_gui.XK_Shift_L])
        self.assertEqual(key, "t")

    def test_modifier_aliases(self):
        for alias in ("control", "ctrl"):
            self.assertEqual(kc_gui.parse_combo(f"{alias}+c")[0], [kc_gui.XK_Control_L])
        for alias in ("alt", "meta"):
            self.assertEqual(kc_gui.parse_combo(f"{alias}+F4")[0], [kc_gui.XK_Alt_L])
        for alias in ("super", "win", "cmd"):
            self.assertEqual(kc_gui.parse_combo(f"{alias}+l")[0], [kc_gui.XK_Super_L])

    def test_case_insensitive_modifiers_but_not_the_key(self):
        # Key names go to XStringToKeysym, which is case-sensitive: 'Return'
        # is a key, 'return' is not. Only the modifier half is folded.
        mods, key = kc_gui.parse_combo("CTRL+Page_Down")
        self.assertEqual(mods, [kc_gui.XK_Control_L])
        self.assertEqual(key, "Page_Down")

    def test_trailing_plus_means_the_plus_key(self):
        mods, key = kc_gui.parse_combo("ctrl++")
        self.assertEqual(mods, [kc_gui.XK_Control_L])
        self.assertEqual(key, "plus")

    def test_unknown_modifier_is_an_error(self):
        with self.assertRaises(kc_gui.KcGuiError):
            kc_gui.parse_combo("hyper+x")

    def test_empty_combo_is_an_error(self):
        with self.assertRaises(kc_gui.KcGuiError):
            kc_gui.parse_combo("")


class ParseWindowIdTest(unittest.TestCase):
    def test_hex_and_decimal(self):
        # `kc-gui windows` prints hex; humans and scripts paste both forms.
        self.assertEqual(kc_gui.parse_window_id("0x40000c"), 0x40000C)
        self.assertEqual(kc_gui.parse_window_id("4194316"), 4194316)

    def test_garbage_is_an_error(self):
        with self.assertRaises(kc_gui.KcGuiError):
            kc_gui.parse_window_id("the-xterm")


class RowsFromXImageTest(unittest.TestCase):
    @staticmethod
    def fb(width, height, stride, bits_per_pixel=32):
        # depth 24 in 32 bpp, LSB-first — what Xvfb :98 actually hands back.
        return kc_gui.Framebuffer(
            width=width, height=height, stride=stride,
            bits_per_pixel=bits_per_pixel,
            red_mask=0xFF0000, green_mask=0xFF00, blue_mask=0xFF,
        )

    def test_bgrx_fast_path(self):
        # Two pixels: pure red, pure blue. Bytes on the wire are B,G,R,X.
        line = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00, 0x00, 0x00])
        rows = kc_gui._rows_from_ximage(line, self.fb(2, 1, 8))
        self.assertEqual(rows, [bytes([0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF])])

    def test_stride_padding_is_skipped(self):
        # X pads scanlines to the bitmap unit; bytes past width*4 are junk and
        # must never leak into the PNG.
        raw = bytes([0x11, 0x22, 0x33, 0x00]) + b"\xde\xad\xbe\xef" * 2
        raw += bytes([0x44, 0x55, 0x66, 0x00]) + b"\xde\xad\xbe\xef" * 2
        rows = kc_gui._rows_from_ximage(raw, self.fb(1, 2, 12))
        self.assertEqual(rows, [bytes([0x33, 0x22, 0x11]), bytes([0x66, 0x55, 0x44])])

    def test_generic_mask_path_matches_the_fast_path(self):
        line = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00, 0x00, 0x00])
        fast = kc_gui._rows_from_ximage(line, self.fb(2, 1, 8))
        # The same pixels at 24 bpp force the per-pixel branch.
        packed = bytes([0x00, 0x00, 0xFF, 0xFF, 0x00, 0x00])
        slow = kc_gui._rows_from_ximage(packed, self.fb(2, 1, 6, bits_per_pixel=24))
        self.assertEqual(fast, slow)

    def test_unsupported_depth_is_a_clear_error(self):
        with self.assertRaises(kc_gui.KcGuiError) as ctx:
            kc_gui._rows_from_ximage(b"\x00\x00", self.fb(1, 1, 2, bits_per_pixel=16))
        self.assertIn("16 bpp", str(ctx.exception))

    def test_mask_shift(self):
        self.assertEqual(kc_gui._mask_shift(0xFF0000), 16)
        self.assertEqual(kc_gui._mask_shift(0xFF), 0)
        self.assertEqual(kc_gui._mask_shift(0), 0)


class EncodePngTest(unittest.TestCase):
    def test_round_trips_through_a_decoder(self):
        rows = [
            bytes([255, 0, 0, 0, 255, 0]),
            bytes([0, 0, 255, 255, 255, 255]),
        ]
        width, height, decoded = decode_png(kc_gui.encode_png(2, 2, rows))
        self.assertEqual((width, height), (2, 2))
        self.assertEqual(decoded, rows)

    def test_has_the_png_signature_and_iend(self):
        blob = kc_gui.encode_png(1, 1, [bytes([1, 2, 3])])
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(blob.endswith(b"IEND\xae\x42\x60\x82"))

    def test_large_uniform_image_compresses(self):
        # A mostly-black 1920x1280 desktop is the common case; the encoder
        # must not emit a ~7MB raw blob for it.
        rows = [bytes(1920 * 3)] * 1280
        blob = kc_gui.encode_png(1920, 1280, rows)
        self.assertLess(len(blob), 200_000)


class ParserTest(unittest.TestCase):
    def test_default_display_is_the_agent_one(self):
        # The whole point of #716: never default to the human's :99.
        self.assertEqual(kc_gui.DEFAULT_DISPLAY, ":98")
        args = kc_gui.build_parser().parse_args(["info"])
        self.assertEqual(args.display, ":98")

    def test_display_can_be_overridden(self):
        args = kc_gui.build_parser().parse_args(["-d", ":99", "windows"])
        self.assertEqual(args.display, ":99")

    def test_run_keeps_the_target_commands_own_flags(self):
        # REMAINDER so `kc-gui run xterm -geometry 80x24` does not have
        # -geometry eaten by kc-gui's own parser.
        args = kc_gui.build_parser().parse_args(["run", "xterm", "-geometry", "80x24"])
        self.assertEqual(args.command, ["xterm", "-geometry", "80x24"])

    def test_key_accepts_several_combos(self):
        args = kc_gui.build_parser().parse_args(["key", "ctrl+l", "Return"])
        self.assertEqual(args.keys, ["ctrl+l", "Return"])

    def test_a_subcommand_is_required(self):
        with self.assertRaises(SystemExit):
            kc_gui.build_parser().parse_args([])


class MainTest(unittest.TestCase):
    def test_kcguierror_becomes_exit_2_not_a_traceback(self):
        # "display :98 is not running" is the most likely failure in the
        # field (browser.agentDisplay=false); it must read as a message.
        def boom(_args):
            raise kc_gui.KcGuiError("cannot open X display :98")

        stderr = io.StringIO()
        with mock.patch.object(kc_gui, "cmd_info", boom), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(kc_gui.main(["info"]), 2)
        self.assertIn("cannot open X display :98", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
