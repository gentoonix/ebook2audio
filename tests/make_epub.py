import struct
import sys
import zlib

from ebooklib import epub


def png(w=200, h=300):
    raw = b"".join(b"\x00" + bytes([40, 90, 160]) * w for _ in range(h))
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def make(path, title, author, nch=3):
    b = epub.EpubBook()
    b.set_identifier(title)
    b.set_title(title)
    b.set_language("en")
    b.add_author(author)
    b.add_metadata("DC", "date", "2021-05-01")
    b.set_cover("cover.png", png())
    chs = []
    for i in range(1, nch + 1):
        c = epub.EpubHtml(title=f"The Chapter {i}", file_name=f"ch{i}.xhtml", lang="en")
        body = "".join(
            f"<p>Mr. Smith walked to the store on day {j}. He said, \"Hello there.\" Then he left, quietly and without fuss, for home. NASA was mentioned.</p>"
            for j in range(1, 6)
        )
        c.content = f"<h1>The Chapter {i}</h1>{body}<p>Dr. Who? Yes!</p>"
        b.add_item(c)
        chs.append(c)
    b.toc = tuple(chs)
    b.add_item(epub.EpubNcx())
    b.add_item(epub.EpubNav())
    b.spine = ["nav"] + chs
    epub.write_epub(path, b)


if __name__ == "__main__":
    make(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 3)
