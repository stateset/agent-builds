"""PTY assertions use screen contents, including curses differential updates."""

import unittest

from tui_terminal import TerminalScreen


class TerminalScreenTests(unittest.TestCase):
    def test_partial_cursor_sequences_and_utf8_are_retained_between_chunks(self):
        screen = TerminalScreen(3, 20)
        screen.feed(b"\x1b[2;")
        self.assertEqual(screen.text().split("\n"), ["", "", ""])
        screen.feed(b"3Hcaf\xc3")
        self.assertEqual(screen.text().split("\n")[1], "  caf")
        screen.feed(b"\xa9\xe7\x95")
        self.assertEqual(screen.text().split("\n")[1], "  café")
        screen.feed(b"\x8c!")
        self.assertEqual(screen.text().split("\n")[1], "  café界!")

    def test_details_header_updates_replace_existing_cells(self):
        screen = TerminalScreen(4, 40)
        screen.feed(b"\x1b[2;1HDETAILS unavailable")
        screen.feed(b"\x1b[2;9H1-2/2\x1b[K")
        self.assertEqual(screen.text().split("\n")[1], "DETAILS 1-2/2")
        screen.feed(b"\x1b[5D3-4/4")
        self.assertEqual(screen.text().split("\n")[1], "DETAILS 3-4/4")
        self.assertNotIn("unavailable", screen.text())

    def test_absolute_and_relative_cursor_commands(self):
        screen = TerminalScreen(4, 16)
        screen.feed(b"ab\x1b[2Ccd\x1b[D!")
        self.assertEqual(screen.text().split("\n")[0], "ab  c!")
        screen.feed(b"\x1b[Enext\x1b[FUP\x1b[3GZ")
        self.assertEqual(screen.text().split("\n")[:2], ["UPZ c!", "next"])
        screen.feed(b"\x1b[3dROW\x1b[A\x1b[2Dxy\x1b[B\x1b[1;1fH")
        self.assertEqual(screen.text().split("\n")[:3], ["HPZ c!", "nextxy", "   ROW"])

    def test_erase_line_and_display_preserve_unaffected_cells(self):
        screen = TerminalScreen(4, 10)
        screen.feed(b"abcdef\x1b[2;1Hsecond\x1b[3;1Hthird")
        screen.feed(b"\x1b[1;3H\x1b[K\x1b[2;3H\x1b[1K")
        self.assertEqual(screen.text().split("\n")[:3], ["ab", "   ond", "third"])
        screen.feed(b"\x1b[3;2H\x1b[J")
        self.assertEqual(screen.text().split("\n")[:3], ["ab", "   ond", "t"])
        screen.feed(b"\x1b[1J")
        self.assertEqual(screen.text().split("\n"), ["", "", "", ""])
        screen.feed(b"\x1b[Hnew\x1b[2Kagain\x1b[2J")
        self.assertEqual(screen.text().split("\n"), ["", "", "", ""])

    def test_character_insert_delete_and_erase(self):
        screen = TerminalScreen(2, 10)
        screen.feed(b"abcdef\x1b[1;3H\x1b[2@")
        self.assertEqual(screen.text().split("\n")[0], "ab  cdef")
        screen.feed(b"XY\x1b[2P")
        self.assertEqual(screen.text().split("\n")[0], "abXYef")
        screen.feed(b"\x1b[2X")
        self.assertEqual(screen.text().split("\n")[0], "abXY")

    def test_line_insert_delete_and_scroll_use_only_the_scroll_region(self):
        screen = TerminalScreen(5, 12)
        for row, text in enumerate(("top", "a", "b", "c", "bottom"), 1):
            screen.feed(("\x1b[{};1H{}".format(row, text)).encode())
        screen.feed(b"\x1b[2;4r\x1b[3;1H\x1b[L")
        self.assertEqual(screen.text().split("\n"), ["top", "a", "", "b", "bottom"])
        screen.feed(b"\x1b[M\x1b[S")
        self.assertEqual(screen.text().split("\n"), ["top", "b", "", "", "bottom"])
        screen.feed(b"\x1b[T")
        self.assertEqual(screen.text().split("\n"), ["top", "", "b", "", "bottom"])
        screen.feed(b"\x1b[4;1H\n")
        self.assertEqual(screen.text().split("\n"), ["top", "b", "", "", "bottom"])

    def test_save_restore_and_nonprinting_styles_titles_charsets_are_ignored(self):
        screen = TerminalScreen(3, 30)
        screen.feed(b"\x1b]0;invisible title\x1b")
        screen.feed(b"\\\x1b(B\x1b=\x1b[?25l\x1b[31mabc\x1b[0m\x1b7")
        screen.feed(b"\x1b[2;1Hother\x1b8Z\x1b[s\x1b[3;1Hlast\x1b[u!")
        screen.feed(b"\x1bPignored payload\x1b\\\x1b>")
        self.assertEqual(screen.text().split("\n"), ["abcZ!", "other", "last"])

    def test_wrap_carriage_return_backspace_tabs_and_scrolling(self):
        screen = TerminalScreen(2, 4)
        screen.feed(b"abcdefgh")
        self.assertEqual(screen.text().split("\n"), ["abcd", "efgh"])
        screen.feed(b"i\rX\bY")
        self.assertEqual(screen.text().split("\n"), ["efgh", "Y"])
        screen = TerminalScreen(2, 12)
        screen.feed(b"a\tZ\r\nnext")
        self.assertEqual(screen.text().split("\n"), ["a       Z", "next"])

    def test_resize_preserves_overlap_and_clamps_cursor_without_reflow(self):
        screen = TerminalScreen(2, 8)
        screen.feed(b"DETAILS\x1b[2;1Hlast\x1b[s")
        screen.resize(3, 10)
        self.assertEqual(screen.text().split("\n"), ["DETAILS", "last", ""])
        screen.resize(1, 4)
        self.assertEqual(screen.text(), "DETA")
        screen.feed(b"\x1b[u!")
        self.assertEqual(screen.text(), "DET!")
        screen.feed(b"\x1b[HNEW\x1b[K")
        screen.resize(2, 8)
        self.assertEqual(screen.text().split("\n"), ["NEW", ""])


if __name__ == "__main__":
    unittest.main()
