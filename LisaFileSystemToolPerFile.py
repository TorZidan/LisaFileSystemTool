#!/usr/bin/env python3
#####################################################################################################
# LisaFileSystemToolPerFile.py implements the "add", "replace", and "delete" commands:                  #
# they add / replace / delete a host file on a LOS/Workshop disk image. Works with binary and text.    #
#                                                                                                    #
# It is a thin extension of the adjacent LisaFileSystemTool.py: all the disk-image machinery         #
# (DC42/Raw ProFile parsing, MDDF, slist, catalog, tags, checksums, ...) is imported from there,     #
# and only the add/replace-specific code lives in this file (in the AddFileMixin /                   #
# ReplaceFileMixin classes below).                                                                   #
#                                                                                                    #
# Usage:                                                                                             #
#   python LisaFileSystemToolPerFile.py add      <disk image file name> <host file> <lisa file name> #
#   python LisaFileSystemToolPerFile.py replace  <disk image file name> <host file> <lisa file name> #
#   python LisaFileSystemToolPerFile.py delete   <disk image file name> <lisa file name>             #
#                                                                                                   #
# THIS IS EXPERIMENTAL CODE !!!                                                                     #
#                                                                                                   #
# Author: TorZidan                                                                                  #
# Date: Sept 20, 2026                                                                               #
# License: Published under the GNU General Public License v3.0.                                     #
#####################################################################################################

from typing import List
import os
import struct
import sys
import time

from LisaFileSystemTool import (
    DC42_HEADER_SIZE,
    SECTOR_SIZE_IN_BYTES,
    InMemoryFileSystem,
    flat_catalog_hash,
    interleave5,
)

# A Lisa ".TEXT" file is stored on disk as:
#   [ 1024-byte header page (two zero-filled sectors) ] [ the text data ]
# The structure of the text data is mandated by LisaOsTextFileSpecification.txt:
#   * the header page is not part of the file's contents; per the spec, it is
#     created with nulls in all 1024 bytes (this matches real .TEXT files on
#     flat-catalog volumes and has been confirmed to work on the Lisa);
#   * the text data is a sequence of 1024-byte pages (two sectors each);
#   * a line is zero or more characters followed by a CR (0x0D);
#   * each page contains complete lines only and is filled with nulls after
#     the last line; the first CR-null (0x0D 0x00) pair in a page signals the
#     end of the page;
#   * a line may be at most 1023 bytes long counting the CR (with room for
#     the terminating null at the end of the page); a line that does not fit
#     in the space left on the current page is moved whole to the next page,
#     leaving the previous page short (null-padded) — like the real Lisa text
#     editor; a line longer than 1023 bytes is continued on the next page,
#     with a CR inserted at the page boundary;
#   * the text data ends with a CR; a null text file (no contents) is the
#     1024-byte header page alone.
# build_lisa_text_file_data() below builds such a byte stream in memory.
# The "dump" command converts this layout back to host text when writing a
# .TEXT file to the host (see lisa_text_file_to_host_text() in
# LisaFileSystemTool.py); it also expands the optional DLE leading-space
# compression code into literal spaces (real Lisa editor files use it, so a
# dumped file shows the logical text rather than raw 0x10 bytes).
TEXT_FILE_PAGE_SIZE = 2 * SECTOR_SIZE_IN_BYTES  # one text page = two sectors
TEXT_FILE_HEADER_SIZE = TEXT_FILE_PAGE_SIZE  # the header page is one 1024-byte page

# ---------------------------------------------------------------------------
# b-tree catalog (fs_version 16/17) constants, from LISA_OS/OS/source-fsdir
# and source-fsasm:
BTREE_NODE_SIZE = 2048  # NODE_SIZE: one node spans 4 consecutive pages
BTREE_NODE_BLKS = 4  # NODE_BLKS: pages per node
BTREE_KEY_SIZE = 36  # KEY_SIZE: the on-disk key (RecKey)
BTREE_TREE_ID = 4  # TREE_ID: s-file number of the root catalog
BTREE_BAD = 0xFFFFFFFF  # BAD: null page number
BTREE_LEAF = 0  # NodeDesc.kind: leaf node
BTREE_NONLEAF = 1  # NodeDesc.kind: interior node
BTREE_NODEDESC_SIZE = 12  # nkeys(2) prior(4) next(4) kind(1) cksum(1)
BTREE_HE_VERSION = 21  # cur_file_version (source-sfileio), written by MAKE_ENTRY
BTREE_MAX_NAME = 32  # MakeKey fits at most 32 name bytes into the key


class _BTreeNode:
    """An in-memory image of one b-tree catalog node (source-fsdir).

    A node is 2048 bytes (4 consecutive MDDF-relative pages): records packed
    from offset 0, a per-record offset array (off[k] at byte 2034 - 2k,
    descending), and the NodeDesc at byte 2036.  Leaf records are the catalog
    entries themselves (fileentry 64 bytes, direntry 48, threadentry 78);
    interior records are (page pointer, 36-byte key) and are uniformly 40
    bytes.
    """

    __slots__ = ("page", "kind", "records", "prior", "next")

    def __init__(self, page, kind, records, prior, next):
        self.page = page
        self.kind = kind
        self.records = records
        self.prior = prior
        self.next = next

    @property
    def nkeys(self):
        return len(self.records)

    @property
    def used(self):
        return sum(len(r) for r in self.records)

    def first_key(self):
        """The 36-byte key of the node's first record.  Parent nodes store
        this as the separator key of this subtree."""
        rec = self.records[0]
        return rec[4:40] if self.kind == BTREE_NONLEAF else rec[0:36]


def strip_archive_trailing_0xff(data: bytes) -> bytes:
    """Remove the single 0xFF byte the Lisa OS source archive appends to the end of its text files.

    (Almost) every file in the CHM "Lisa_Source" tree ends with one 0xFF byte
    (Mac Roman "ÿ") — either as the very last byte or as a final line right
    before the trailing newline(s).  It is an archiving artifact, not content:
    the Lisa Pascal compiler rejects it with "illegal character in input", and
    the Lisa editor shows it as a stray "?" at the end of the file.

    Only a 0xFF with nothing but CR/LF bytes after it is removed; a 0xFF
    followed by any other content (legitimate text) is left untouched, as is
    any 0xFF in the middle of the file.  Returns the (possibly shorter)
    byte string.
    """
    i = data.rfind(b"\xff")
    if i < 0:
        return data
    if any(b not in (0x0D, 0x0A) for b in data[i + 1 :]):
        return data
    return data[:i]


def build_lisa_text_file_data(content: bytes) -> bytes:
    """Build the complete on-disk byte stream of a Lisa ".TEXT" file from raw host text.

    Returns: [ 1024-byte all-null header page ] [ the page-structured text data ].
    The layout implements the structure mandated by LisaOsTextFileSpecification.txt
    (see the summary in the comment above the constants):

      * host "\\r\\n" and "\\n" line endings are normalized to Lisa "\\r" first;
        a file that already uses "\\r" line endings is left unchanged;
      * the text data is laid out in 1024-byte pages; each page holds complete
        lines only (each line terminated by a CR) and is null-filled after its
        last line, so the first CR-null pair in the page signals its end;
      * a line that does not fit in the space left on the current page is
        moved whole to the next page, leaving the previous page short (null
        padding) — like the real Lisa text editor — so "dump" → "add" →
        "dump" is an exact round trip as long as no line is longer than
        1023 bytes counting its CR and the on-disk file does not use the
        optional DLE leading-space compression code (real Lisa editor files
        do; the dump expands the code into literal spaces, so a re-added file
        holds the same logical text with literal spaces);
      * a line longer than 1023 bytes counting its CR cannot be represented
        as one line; it is continued on the next page, with a CR inserted at
        the page boundary (such an inserted CR appears as an extra line end
        when the file is dumped again);
      * the text data ends with a CR (one is appended if the content does not
        already end with one);
      * content with no text at all yields a null text file: the 1024-byte
        header page alone.

    The input is treated as an arbitrary stream of characters (any byte
    sequence is accepted). Note that a NUL byte is an ordinary character in
    this format, except that a CR followed by a NUL cannot be represented:
    any reader will treat that pair as the end of the page.

    The result is always a whole number of 1024-byte pages.
    """
    text = content.replace(b"\r\n", b"\r").replace(b"\n", b"\r")

    if not text:
        return bytes(TEXT_FILE_HEADER_SIZE)  # null text file: header page only

    if not text.endswith(b"\r"):
        text += b"\r"  # per the spec, text files end with a CR at CLOSE

    out = bytearray(TEXT_FILE_HEADER_SIZE)  # null-filled header page
    page = bytearray(TEXT_FILE_PAGE_SIZE)  # zero-filled: nulls after the last line
    used = 0  # the number of content bytes on the current page
    i, n = 0, len(text)
    while i < n:
        line_end = text.find(b"\r", i) + 1  # the end of the line, CR included
        line_len = line_end - i
        if line_len <= TEXT_FILE_PAGE_SIZE - 1:
            # A complete line (at most 1023 bytes counting its CR):
            if used + line_len > TEXT_FILE_PAGE_SIZE - 1:
                # The line does not fit in the space left on this page. Like
                # the real Lisa editor, leave the page short (null padding)
                # and start the line on a fresh page:
                out += page
                page = bytearray(TEXT_FILE_PAGE_SIZE)
                used = 0
            page[used : used + line_len] = text[i:line_end]
            used += line_len
        else:
            # A line longer than 1023 bytes counting its CR: continue it on
            # the next page, with a CR inserted inside each full page (1022
            # characters + CR). Start on a fresh page so that every chunk
            # gets the full 1022-character run:
            if used:
                out += page
                page = bytearray(TEXT_FILE_PAGE_SIZE)
                used = 0
            while line_end - i > TEXT_FILE_PAGE_SIZE - 1:
                page[: TEXT_FILE_PAGE_SIZE - 2] = text[i : i + TEXT_FILE_PAGE_SIZE - 2]
                page[TEXT_FILE_PAGE_SIZE - 2] = 0x0D
                out += page
                page = bytearray(TEXT_FILE_PAGE_SIZE)
                used = 0
                i += TEXT_FILE_PAGE_SIZE - 2
            line_len = line_end - i
            page[:line_len] = text[i:line_end]
            used = line_len
        i = line_end
    out += page
    return bytes(out)


class AddFileMixin:
    """The "add" command: add_file() and its private helpers.

    This is a mixin: it does not duplicate any of the disk-image machinery of
    LisaFileSystemTool.InMemoryFileSystem, but only relies on its existing API
    (read_sector, read_tags_for_sector, is_flat_catalog_volume, _slist_entry,
    _find_rootcatalog_sfile, calculate_new_tag_checksum, fix_dc42_checksum, and
    the _file/_file_name/_mddf_sector_number/_is_dc42_format/_num_sectors/
    _single_tag_size/_fs_version attributes).
    """

    # =========================================================================
    #  add: add a host file into a flat-catalog (fs_version 14/15) volume.
    #  See the docstring of add_file() below for details.
    # =========================================================================

    def _mddf_u16(self, offset: int) -> int:
        """Read a big-endian 16-bit field from the CURRENT (in-memory) MDDF sector."""
        m = self.read_sector(self._mddf_sector_number)
        return struct.unpack(">H", m[offset : offset + 2])[0]

    def _mddf_u32(self, offset: int) -> int:
        """Read a big-endian 32-bit field from the CURRENT (in-memory) MDDF sector."""
        m = self.read_sector(self._mddf_sector_number)
        return struct.unpack(">I", m[offset : offset + 4])[0]

    def _sector_data_file_offset(self, sector_number: int) -> int:
        """Byte offset of the 512-byte data of the given sector in the image file."""
        if self._is_dc42_format:
            return DC42_HEADER_SIZE + sector_number * SECTOR_SIZE_IN_BYTES
        return (
            interleave5(sector_number) * (SECTOR_SIZE_IN_BYTES + self._single_tag_size)
            + self._single_tag_size
        )

    def _sector_tag_file_offset(self, sector_number: int) -> int:
        """Byte offset of the tag of the given sector in the image file."""
        if self._is_dc42_format:
            return (
                DC42_HEADER_SIZE
                + self._num_sectors * SECTOR_SIZE_IN_BYTES
                + sector_number * self._single_tag_size
            )
        return interleave5(sector_number) * (
            SECTOR_SIZE_IN_BYTES + self._single_tag_size
        )

    def _mem_write_sector_data(self, sector_number: int, data_bytes: bytes):
        """Write 512 bytes of sector data to the in-memory copy (self._file)."""
        assert len(data_bytes) == SECTOR_SIZE_IN_BYTES
        off = self._sector_data_file_offset(sector_number)
        self._file.seek(off)
        self._file.write(data_bytes)

    def _mem_write_sector_tag(self, sector_number: int, tag_bytes: bytes):
        """Write the tag of a sector to the in-memory copy (self._file)."""
        assert len(tag_bytes) == self._single_tag_size
        off = self._sector_tag_file_offset(sector_number)
        self._file.seek(off)
        self._file.write(tag_bytes)

    def _bitmap_free_pages(self) -> List[int]:
        """Return the sorted list of FREE MDDF-relative page numbers, from the allocation bitmap.

        Bit order: the bit for MDDF-relative page `rel` is in byte `rel//8`, bit
        `rel%8` (LSB first, as verified empirically). Bit 1 = allocated, 0 = free.
        """
        bitmap_addr = self._mddf_u32(0x88)
        bitmap_pages = self._mddf_u16(0x92)
        bitmap_size = self._mddf_u32(0x8C)
        mddf = self._mddf_sector_number
        bmp = b"".join(
            self.read_sector(mddf + bitmap_addr + i) for i in range(bitmap_pages)
        )
        free = []
        last_rel = self._num_sectors - mddf  # pages are addressed relative to MDDF
        for rel in range(min(bitmap_size, last_rel)):
            if (bmp[rel // 8] >> (rel % 8)) & 1 == 0:
                free.append(rel)
        return free

    def _find_contiguous_run(self, free_set: set, length: int):
        """Smallest start s such that s..s+length-1 are all in free_set, else None."""
        if length <= 0:
            return 0
        for s in sorted(free_set):
            if all((s + i) in free_set for i in range(1, length)):
                return s
        return None

    def _data_page_chain(self, fileaddr: int) -> List[int]:
        """List of MDDF-relative data-page numbers for a file, following tag fwdlinks.
        fileaddr is the MDDF-relative first data page (0 => no data)."""
        chain = []
        if fileaddr == 0:
            return chain
        rel = fileaddr
        mddf = self._mddf_sector_number
        while True:
            chain.append(rel)
            if len(chain) > self._num_sectors:
                raise RuntimeError(
                    f"the data-page chain from page {fileaddr} is longer than the whole volume; "
                    "the tag chain is corrupted (a loop?)"
                )
            tag = self.read_tags_for_sector(mddf + rel)
            fwd = (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
            if fwd == 0xFFFFFF:
                break
            rel = fwd
        return chain

    def _build_tag(
        self,
        version: int,
        volume: int,
        fileid: int,
        dataused_bytes: int,
        abspage: int,
        relpage: int,
        fwdlink: int,
        bkwdlink: int,
    ) -> bytes:
        """Build a 20-byte ProFile/hard-disk sector tag.

        fwdlink/bkwdlink are MDDF-relative page numbers, or 0xFFFFFF (END).
        dataused_bytes is the number of valid bytes (0..512); the high bit (cksum
        present) is set. The checksum byte (offset 11) is left 0 and patched later
        by calculate_new_tag_checksum().
        """
        END = 0xFFFFFF
        t = bytearray(20)
        struct.pack_into(">H", t, 0, version & 0xFFFF)
        struct.pack_into(">H", t, 2, volume & 0xFFFF)
        struct.pack_into(">H", t, 4, fileid & 0xFFFF)
        struct.pack_into(">H", t, 6, (0x8000 | (dataused_bytes & 0x7FFF)) & 0xFFFF)
        t[8] = (abspage >> 16) & 0xFF
        t[9] = (abspage >> 8) & 0xFF
        t[10] = abspage & 0xFF
        t[11] = 0  # checksum placeholder
        struct.pack_into(">H", t, 12, relpage & 0xFFFF)
        f = fwdlink if fwdlink != END else END
        t[14] = (f >> 16) & 0xFF
        t[15] = (f >> 8) & 0xFF
        t[16] = f & 0xFF
        b = bkwdlink if bkwdlink != END else END
        t[17] = (b >> 16) & 0xFF
        t[18] = (b >> 8) & 0xFF
        t[19] = b & 0xFF
        return bytes(t)

    def _group_runs(self, pages: List[int]) -> List[tuple]:
        """Group a sorted list of page numbers into contiguous (start, count) runs."""
        runs = []
        for p in pages:
            if runs and p == runs[-1][0] + runs[-1][1]:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((p, 1))
        return runs

    def _make_unique_id(self, sfile: int) -> bytes:
        """An 8-byte pseudo-unique id for the new file's hentry (a,b pair)."""
        t = int(time.time()) & 0xFFFFFFFF
        a = t
        b = (sfile * 0x9E3779B9 + (t & 0xFFFF)) & 0xFFFFFFFF
        return struct.pack(">II", a, b)

    def _build_centry(self, name_bytes: bytes, sfile: int) -> bytes:
        """Build a 54-byte flat-catalog centry for a regular file entry (cetype=3)."""
        c = bytearray(54)
        c[0] = len(name_bytes)
        c[1 : 1 + len(name_bytes)] = name_bytes
        c[34] = 3  # cetype = fileentry
        struct.pack_into(">H", c, 36, sfile & 0xFFFF)  # sfile
        # attributes, readpage, readoffset, writepage, writeoffset stay 0
        return bytes(c)

    def _find_insert_slot(self, cat_data: bytes, lisa_name: str, rootmaxentries: int):
        """Find the slot index to insert a new centry, using the same hash + linear
        probing as LOOKUP_BY_ENAME. Returns the slot index, or None if the catalog
        is full. A negative return (the duplicate's slot) means the name exists."""
        start = flat_catalog_hash(lisa_name, rootmaxentries)
        first_tombstone = -1
        for i in range(rootmaxentries):
            idx = (start + i) % rootmaxentries
            rec = cat_data[idx * 54 : idx * 54 + 54]
            name_len = rec[0]
            cetype = rec[34]
            if cetype == 0 and name_len == 0:
                return first_tombstone if first_tombstone >= 0 else idx
            if cetype == 7:  # 'removed' tombstone: remember, keep probing
                if first_tombstone < 0:
                    first_tombstone = idx
                continue
            if name_len:
                name = rec[1 : 1 + name_len].decode("mac-roman", errors="replace")
                if name.upper() == lisa_name.upper():
                    return -1  # duplicate name
        return first_tombstone if first_tombstone >= 0 else None

    # =========================================================================
    #  add: b-tree catalog (fs_version 16/17) support.
    #
    #  A port of the OS b-tree root-catalog code (LISA_OS/OS/source-fsdir:
    #  Search, SimpleInsert, SplitPoint/Solomon, RotatePoint/ManyRotates,
    #  Split_Rotate, InsertSplit, DelAdjust, AllocNode; source-fsasm:
    #  SearchNode, MakeKey).  The record-list model below keeps every node
    #  self-consistent: on write, records are repacked from offset 0 and the
    #  offset array is rebuilt as cumulative sums, which the OS's SearchNode
    #  read path consumes directly.
    # =========================================================================

    def _bt_key_cmp(self, k1: bytes, k2: bytes) -> int:
        """Compare two 36-byte b-tree keys exactly as the SearchNode assembly
        does: unsigned parent ID first, then the 32 name bytes case-
        insensitively (lowercase letters compare as uppercase).  Returns -1, 0,
        or 1."""
        p1 = (k1[1] << 8) | k1[2]
        p2 = (k2[1] << 8) | k2[2]
        if p1 != p2:
            return -1 if p1 < p2 else 1
        for b1, b2 in zip(k1[3:35], k2[3:35]):
            if 0x61 <= b1 <= 0x7A:
                b1 -= 32
            if 0x61 <= b2 <= 0x7A:
                b2 -= 32
            if b1 != b2:
                return -1 if b1 < b2 else 1
        return 0

    def _bt_make_key(self, parent_id: int, name_bytes: bytes) -> bytes:
        """Build the 36-byte key exactly as MakeKey (source-fsasm): length
        byte 36, 2-byte big-endian parent ID, the name left-justified in a
        32-byte null-padded field, and a forced null terminator."""
        key = bytearray(36)
        key[0] = 36
        key[1] = (parent_id >> 8) & 0xFF
        key[2] = parent_id & 0xFF
        key[3 : 3 + len(name_bytes)] = name_bytes[:BTREE_MAX_NAME]
        key[35] = 0
        return bytes(key)

    def _bt_read_node(self, page: int) -> _BTreeNode:
        """Read one b-tree node (4 sectors) from the in-memory image and
        decode it into a _BTreeNode (records as a list of byte strings)."""
        mddf = self._mddf_sector_number
        d = b"".join(self.read_sector(mddf + page + i) for i in range(BTREE_NODE_BLKS))
        nkeys = struct.unpack(">H", d[2036:2038])[0]
        prior = struct.unpack(">I", d[2038:2042])[0]
        nxt = struct.unpack(">I", d[2042:2046])[0]
        kind = d[2046]
        offs = [
            struct.unpack(">H", d[2034 - 2 * k : 2036 - 2 * k])[0]
            for k in range(nkeys + 1)
        ]
        records = [bytes(d[offs[i] : offs[i + 1]]) for i in range(nkeys)]
        return _BTreeNode(page, kind, records, prior, nxt)

    def _bt_write_node(self, node: _BTreeNode, modified: set):
        """Write a node back to the in-memory image (4 sectors + tags).

        Records are repacked from offset 0 and the offset array is rebuilt as
        cumulative sums.  The unused tail is zeroed (real nodes carry stale
        buffer-pool garbage there, which the OS never reads).  The sector
        tags match the OS's GetNode/PlRec setup for tree pages: version 0,
        volume 0, fileid TREE_ID, dataused 512 (the driver stores 0x8000|512),
        MDDF-relative abspage/fwdlink/bkwdlink, and relpage = position in node.
        """
        mddf = self._mddf_sector_number
        buf = bytearray(BTREE_NODE_SIZE)
        off = 0
        offs = [0]
        for rec in node.records:
            buf[off : off + len(rec)] = rec
            off += len(rec)
            offs.append(off)
        for k in range(len(offs)):
            struct.pack_into(">H", buf, 2034 - 2 * k, offs[k])
        struct.pack_into(">H", buf, 2036, node.nkeys)
        struct.pack_into(">I", buf, 2038, node.prior)
        struct.pack_into(">I", buf, 2042, node.next)
        buf[2046] = node.kind
        buf[2047] = 0xFF  # cksum (real tree pages always carry 0xFF here)
        END = 0xFFFFFF
        for i in range(BTREE_NODE_BLKS):
            absn = mddf + node.page + i
            self._mem_write_sector_data(absn, bytes(buf[i * 512 : (i + 1) * 512]))
            fwd = node.page + i + 1 if i + 1 < BTREE_NODE_BLKS else END
            bkw = node.page + i - 1 if i > 0 else END
            self._mem_write_sector_tag(
                absn,
                self._build_tag(0, 0, BTREE_TREE_ID, 512, node.page + i, i, fwd, bkw),
            )
            modified.add(absn)

    def _bt_alloc_node(
        self, free_set: set, kind: int, prior: int, nxt: int, modified: set
    ) -> _BTreeNode:
        """Allocate a fresh node (4 contiguous pages, like AllocNode/sallocate):
        prefer a run at/after MDDF freestart, else the lowest free run.  Marks
        the pages used in free_set, writes the empty node, and returns it.
        (The bitmap and MDDF accounting are done once, by the caller, for all
        pages of the new file.)"""
        freestart = self._mddf_u32(0xB2)

        def find_run(hint):
            for s in sorted(free_set):
                if s < hint:
                    continue
                if all((s + i) in free_set for i in (1, 2, 3)):
                    return s
            return None

        page = find_run(freestart)
        if page is None:
            page = find_run(0)
        if page is None:
            raise RuntimeError("b-tree: not enough free space for a new node")
        for i in range(BTREE_NODE_BLKS):
            free_set.discard(page + i)
        node = _BTreeNode(page, kind, [], prior, nxt)
        self._bt_write_node(node, modified)
        return node

    def _bt_search(self, key: bytes):
        """Descend the tree exactly as the OS Search does (SearchNode per
        level).  Returns (leaf, index, path, found): the leaf node, the
        insertion position in it, the [(page, index), ...] path from root to
        leaf (path[i] = the node at depth i and the record index it descended
        through), and whether a record with an equal key already exists.

        Interior search examines records 1..nkeys-1 (record 0 is the
        leftmost-subtree pointer and is never compared); a match descends via
        the *next* record (the OS sets index := index + 1)."""
        page = self._mddf_u32(0x12E)
        if page == BTREE_BAD:
            raise RuntimeError("b-tree: volume has no root catalog (root_page = BAD)")
        node = self._bt_read_node(page)
        path = []
        while True:
            path.append((node.page, 0))
            index = 0
            if node.kind == BTREE_LEAF:
                found = False
                index = len(node.records)  # key is greater than every record
                for i, rec in enumerate(node.records):
                    c = self._bt_key_cmp(rec[0:36], key)
                    if c == 0:
                        found = True
                        index = i  # OS: Search sets index := i (exact match)
                        break
                    if c > 0:
                        index = i
                        break
                return node, index, path, found
            # interior: SearchNode examines records 1..nkeys-1 (record 0 is
            # the leftmost-subtree pointer and is never compared) and returns
            # the insertion position p in that sub-array.  The OS descends
            # via off[index] where index = p (no match) or p+1 (a match, the
            # key equals the low key of subtree p+1 and lives there).
            found = False
            index = node.nkeys - 1  # key is greater than every separator
            for i in range(1, node.nkeys):
                rec = node.records[i]
                c = self._bt_key_cmp(rec[4:40], key)
                if c == 0:
                    found = True
                    index = i  # p = i-1, then the OS adds 1
                    break
                if c > 0:  # key < rec[i]
                    index = i - 1
                    break
            if found:
                # The key equals an interior separator key (a copy of some
                # record's key); the OS returns this node with found := TRUE
                # and Insert reports E_ENTRY_EXISTS.  The caller only uses
                # `found` here.
                return node, index, path, True
            child_page = struct.unpack(">I", node.records[index][0:4])[0]
            path[-1] = (node.page, index)
            node = self._bt_read_node(child_page)

    def _bt_simple_insert(self, node: _BTreeNode, rec: bytes, index: int) -> bool:
        """SimpleInsert: insert `rec` at `index` if the node has room
        (used + 12 + 2*(nkeys+2) + size <= NODE_SIZE); returns False if the
        node would overflow."""
        if (
            node.used + BTREE_NODEDESC_SIZE + (node.nkeys + 2) * 2 + len(rec)
            > BTREE_NODE_SIZE
        ):
            return False
        node.records.insert(index, rec)
        return True

    def _bt_solomon(self, index: int, left_size: int, right_size: int, size_of):
        """Solomon: rebalance a would-be split by moving records between the
        left and right halves.  `size_of(i)` gives the size of virtual record
        i (the node's records with the new one spliced in).  Returns
        (index, left_size, right_size): the left node gets virtual records
        0..index, the right node index+1..; the sizes are the corresponding
        V[0..index] / V[index+1..] byte counts maintained by the algorithm."""
        while True:
            if left_size < right_size:
                s = size_of(index + 1)
                new_left = left_size + s
                new_right = right_size - s
                if abs(new_right - new_left) < (right_size - left_size):
                    index += 1
                else:
                    return index, left_size, right_size
            elif left_size > right_size:
                s = size_of(index)
                new_left = left_size - s
                new_right = right_size + s
                if abs(new_right - new_left) < (left_size - right_size):
                    index -= 1
                else:
                    return index, left_size, right_size
            else:
                return index, left_size, right_size
            left_size, right_size = new_left, new_right

    def _bt_split_point(self, node: _BTreeNode, index: int, size: int) -> int:
        """SplitPoint: where to split `node` after inserting a `size`-byte
        record at `index`.  Returns s such that the left node keeps virtual
        records 0..s (s+1 records) and the right node gets s+1.."""
        n = node.nkeys
        mid = (n + 1) // 2
        sizes = [len(r) for r in node.records]

        def size_of(i):
            if i < index:
                return sizes[i]
            if i == index:
                return size
            return sizes[i - 1]

        if mid < index:
            left_size = sum(sizes[: mid + 1])  # off[mid+1]: no new rec in 0..mid
        else:
            left_size = sum(sizes[:mid]) + size  # off[mid] + size (as in the OS)
        right_size = node.used + size - left_size
        return self._bt_solomon(mid, left_size, right_size, size_of)[0]

    def _bt_rotate_point(
        self, L: _BTreeNode, R: _BTreeNode, virt: int, size: int
    ) -> int:
        """RotatePoint: given the two sibling nodes L and R and the virtual
        position `virt` of the new record in V = [L recs][NEW][R recs], find
        a rotation point that fits both nodes.  Returns rotPt (last virtual
        index kept in the left node), or -1 if either node would overflow.

        After rotating: left node = V[0..rotPt], right node = V[rotPt+1..].
        """
        left_keys = L.nkeys
        L_sizes = [len(r) for r in L.records]
        R_sizes = [len(r) for r in R.records]

        def size_of(i):
            if i < virt:
                if i < left_keys:
                    return L_sizes[i]
                return R_sizes[i - left_keys]
            if i == virt:
                return size
            j = i - 1
            if j < left_keys:
                return L_sizes[j]
            return R_sizes[j - left_keys]

        if virt < left_keys:
            # insertion is in the left node
            i = left_keys
            left_size = sum(L_sizes) + size
        else:
            # insertion is in the right node
            i = left_keys - 1
            left_size = sum(L_sizes)
        right_size = sum(L_sizes) + sum(R_sizes) + size - left_size
        i, left_size, right_size = self._bt_solomon(i, left_size, right_size, size_of)
        # RotatePoint's overflow test: (nkeys+1)*2 + used <= NODE_SIZE - NodeDesc
        # for each node after the rotation (the left node keeps one spare
        # offset slot, as the OS test's (i+2) term accounts for).
        if (i + 2) * 2 + left_size > BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE or (
            left_keys + R.nkeys + 1 - i
        ) * 2 + right_size > BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE:
            return -1
        return i

    def _bt_apply_rotation(
        self, L: _BTreeNode, R: _BTreeNode, rotPt: int, virt: int, new_rec: bytes
    ):
        """ManyRotates + the insertion step of Split_Rotate: rebuild the two
        nodes from V = [L recs][new_rec at virt][R recs]; the left node keeps
        V[0..rotPt] and the right node gets V[rotPt+1..]."""
        base = L.records + R.records
        v = base[:virt] + [new_rec] + base[virt:]
        L.records = v[: rotPt + 1]
        R.records = v[rotPt + 1 :]

    def _bt_split_node(
        self, node: _BTreeNode, index: int, rec: bytes, free_set: set, modified: set
    ):
        """The split tail of Split_Rotate: splice `rec` into `node` at
        `index`, split the resulting list of records at the SplitPoint, and
        move the right half into a freshly allocated sibling node.  Leaf
        prior/next links are patched as in the source.  Returns (left, right)."""
        s = self._bt_split_point(node, index, len(rec))
        v = node.records[:index] + [rec] + node.records[index:]
        if node.kind == BTREE_LEAF:
            old_next = node.next
            new = self._bt_alloc_node(
                free_set, BTREE_LEAF, node.page, old_next, modified
            )
            node.next = new.page
            if old_next != BTREE_BAD:
                nxt = self._bt_read_node(old_next)
                nxt.prior = new.page
                self._bt_write_node(nxt, modified)
        else:
            new = self._bt_alloc_node(
                free_set, BTREE_NONLEAF, BTREE_BAD, BTREE_BAD, modified
            )
        new.records = v[s + 1 :]
        node.records = v[: s + 1]
        self._bt_write_node(node, modified)
        self._bt_write_node(new, modified)
        return node, new

    def _bt_insert(self, key: bytes, rec: bytes, free_set: set, modified: set):
        """Insert a catalog record into the b-tree root catalog, mirroring the
        OS Insert procedure: Search, then at each level SimpleInsert, else
        rotate with a sibling (Split_Rotate), else split and promote the low
        key of the right node (with a brand-new root if the root splits).

        Returns (ok, new_root_page, actual_depth): ok is False if the key
        already exists; new_root_page is the MDDF-relative page of a freshly
        created root, or None; actual_depth is the tree depth measured from
        the real tree structure (len(path)), which the caller uses to keep
        the MDDF tree_depth field in sync (self-healing a stale value).
        (MDDF root_page/tree_depth, fs_overhead and the allocation bitmap
        are updated by the caller.)"""
        node, index, path, found = self._bt_search(key)
        if found:
            return False, None, len(path)
        # NB: the level bookkeeping comes from the ACTUAL descent (len(path)),
        # not from the MDDF tree_depth field: if that field is stale (e.g. a
        # previously interrupted write), deriving the parent chain from it
        # would mix tree levels and corrupt the catalog.  len(path) is the
        # true depth, and the caller writes it back to the MDDF.
        depth = len(path)
        level = depth - 1  # 0-based from the root; the leaf is depth-1
        ins_rec = rec
        ins_index = index

        while True:
            if self._bt_simple_insert(node, ins_rec, ins_index):
                self._bt_write_node(node, modified)
                return True, None, depth
            if level == 0:
                # the root is full: split it and grow the tree (InsertSplit)
                lft, rgt = self._bt_split_node(
                    node, ins_index, ins_rec, free_set, modified
                )
                root = self._bt_alloc_node(
                    free_set, BTREE_NONLEAF, BTREE_BAD, BTREE_BAD, modified
                )
                root.records = [
                    struct.pack(">I", lft.page) + lft.first_key(),
                    struct.pack(">I", rgt.page) + rgt.first_key(),
                ]
                self._bt_write_node(root, modified)
                return True, root.page, depth
            # a parent exists (Split_Rotate: Siblings, then rotate, then split)
            par_page, par_idx = path[level - 1]
            parent = self._bt_read_node(par_page)
            nkeys_p = parent.nkeys
            left_sib = (
                BTREE_BAD
                if par_idx == 0
                else struct.unpack(">I", parent.records[par_idx - 1][0:4])[0]
            )
            right_sib = (
                BTREE_BAD
                if par_idx == nkeys_p - 1
                else struct.unpack(">I", parent.records[par_idx + 1][0:4])[0]
            )
            if left_sib != BTREE_BAD:
                L = self._bt_read_node(left_sib)
                rotPt = self._bt_rotate_point(
                    L, node, ins_index + L.nkeys, len(ins_rec)
                )
                if rotPt >= 0:
                    self._bt_apply_rotation(
                        L, node, rotPt, ins_index + L.nkeys, ins_rec
                    )
                    self._bt_write_node(L, modified)
                    self._bt_write_node(node, modified)
                    # DelAdjust: copy the new low key of the right node (==
                    # node after the rotation) into the parent's record for it
                    pr = parent.records[par_idx]
                    parent.records[par_idx] = pr[:4] + node.first_key()
                    self._bt_write_node(parent, modified)
                    return True, None, depth
            if right_sib != BTREE_BAD:
                R = self._bt_read_node(right_sib)
                rotPt = self._bt_rotate_point(node, R, ins_index, len(ins_rec))
                if rotPt >= 0:
                    self._bt_apply_rotation(node, R, rotPt, ins_index, ins_rec)
                    self._bt_write_node(node, modified)
                    self._bt_write_node(R, modified)
                    # DelAdjust: copy the new low key of the right node (== R
                    # after the rotation) into the parent's record for it
                    pr = parent.records[par_idx + 1]
                    parent.records[par_idx + 1] = pr[:4] + R.first_key()
                    self._bt_write_node(parent, modified)
                    return True, None, depth
            lft, rgt = self._bt_split_node(node, ins_index, ins_rec, free_set, modified)
            # promote the low key of the right node up one level
            node = parent
            ins_rec = struct.pack(">I", rgt.page) + rgt.first_key()
            ins_index = par_idx + 1
            level -= 1

    def _add_file_btree(self, host_file_path: str, lisa_name: str) -> "int":
        """Add a host file into a b-tree-catalog (fs_version 16/17) volume as a
        new Lisa file.

        Mirrors the OS's MAKE_ENTRY path on a b-tree volume (source-fsprim,
        source-sfileio1 new_sfile, source-fsdir Insert): allocate an s-file
        number, allocate hint + data pages, write the hentry (the v17 hentry
        is 110 bytes, and the smallmap is inlined into hint page 0 at the
        MDDF smallmap_offset), write the data pages, update the slist sentry,
        and insert a 64-byte fileentry ObjRec into the b-tree root catalog via
        _bt_insert.  The MDDF (filecount, freecount, fs_overhead, empty_file,
        and root_page/tree_depth if the tree grows), the allocation bitmap, the
        tag checksums and the DC42 header checksums are all updated.

        Returns the exit code to use: 0 on success, 3 if a file with the
        same name is already on the volume, and 1 on any other failure.
        """
        if self._single_tag_size != 20:
            print("ERROR: add currently supports only 20-byte-tag (hard disk) images.")
            return 1
        if not os.path.isfile(host_file_path):
            print(f"ERROR: host file '{host_file_path}' not found.")
            return 1
        with open(host_file_path, "rb") as f:
            data = f.read()

        # ---- validate the Lisa file name ----
        # The b-tree key holds at most 32 name bytes (MakeKey), so the name
        # limit on b-tree volumes is 32, not the flat catalog's 33.
        if not lisa_name:
            print("ERROR: the Lisa file name is empty.")
            return 1
        if len(lisa_name) > BTREE_MAX_NAME:
            print(
                f"ERROR: Lisa file name '{lisa_name}' is {len(lisa_name)} chars; max is {BTREE_MAX_NAME} on a b-tree volume."
            )
            return 1
        # Lisa does not treat '/' as a folder delimiter in file names (real
        # volumes contain names like 'ALEX/ASM/APDM.TEXT'), so '/' is an
        # ordinary character and is allowed.
        try:
            name_bytes = lisa_name.encode("mac-roman")
        except UnicodeEncodeError as e:
            print(f"ERROR: cannot encode '{lisa_name}' as Mac Roman: {e}")
            return 1
        if len(name_bytes) > BTREE_MAX_NAME:
            print(
                f"ERROR: '{lisa_name}' is {len(name_bytes)} Mac-Roman bytes; max is {BTREE_MAX_NAME} on a b-tree volume."
            )
            return 1

        # ---- ".TEXT" files (same as the flat path) ----
        is_text_file = lisa_name.upper().endswith(".TEXT")
        if is_text_file:
            cleaned = strip_archive_trailing_0xff(data)
            if cleaned != data:
                print(
                    f"NOTE: stripped the archive's trailing 0xFF byte from '{host_file_path}'."
                )
            data = build_lisa_text_file_data(cleaned)
        filesize = len(data)

        mddf = self._mddf_sector_number

        # ---- MDDF fields ----
        empty_file = self._mddf_u16(0x9E)
        maxfiles = self._mddf_u16(0xA0)
        hintsize = self._mddf_u16(0xA2)
        filecount = self._mddf_u16(0xB0)
        freecount = self._mddf_u32(0xBA)
        fs_overhead = self._mddf_u16(0x122)
        bitmap_addr = self._mddf_u32(0x88)
        bitmap_pages = self._mddf_u16(0x92)
        slist_addr = self._mddf_u32(0x94)
        slist_packing = self._mddf_u16(0x98)
        smallmap_off = self._mddf_u16(
            0x114
        )  # byte offset of the smallmap in hint page 0

        if hintsize != 1:
            print(
                f"ERROR: add on b-tree volumes currently supports only hintsize 1 (found {hintsize})."
            )
            return 1
        if not (0x80 <= smallmap_off <= 0x100):
            print(
                f"ERROR: unexpected smallmap_offset 0x{smallmap_off:x}; cannot place the file map in hint page 0."
            )
            return 1

        num_data_pages = (filesize + 511) // 512
        needed_pages = hintsize + num_data_pages

        # ---- find a free s-file number (first free slist slot at/after empty_file) ----
        new_sfile = None
        for i in range(empty_file, maxfiles):
            e = self._slist_entry(i)
            if e is not None and e[0] == 0:  # hintaddr == 0 => free slot
                new_sfile = i
                break
        if new_sfile is None:
            print("ERROR: no free s-file slot available (slist is full).")
            return 1

        # ---- find free pages ----
        # The file needs `needed_pages` pages, and the catalog insert may
        # allocate up to two new b-tree nodes (a leaf split plus a possible
        # root split); require room for all of it up front.
        free_pages = self._bitmap_free_pages()
        if len(free_pages) < needed_pages + 2 * BTREE_NODE_BLKS:
            print(
                f"ERROR: not enough free space: need {needed_pages} file pages plus up to "
                f"{2 * BTREE_NODE_BLKS} b-tree node pages but only {len(free_pages)} free."
            )
            return 1
        free_set = set(free_pages)
        hs = self._find_contiguous_run(free_set, hintsize)
        hint_pages = (
            [hs + i for i in range(hintsize)]
            if hs is not None
            else sorted(free_set)[:hintsize]
        )
        for p in hint_pages:
            free_set.discard(p)
        if num_data_pages:
            ds = self._find_contiguous_run(free_set, num_data_pages)
            data_pages = (
                [ds + i for i in range(num_data_pages)]
                if ds is not None
                else sorted(free_set)[:num_data_pages]
            )
        else:
            data_pages = []
        for p in data_pages:
            free_set.discard(p)

        # ---- sentry version (the page tags carry it) ----
        spage = new_sfile // slist_packing
        soffs = (new_sfile % slist_packing) * 14
        slist_abs = mddf + slist_addr + spage
        sentry_version = struct.unpack(
            ">H", self.read_sector(slist_abs)[soffs + 12 : soffs + 14]
        )[0]

        END = 0xFFFFFF
        modified = set()

        # ---- hint page 0: the hentry, then the smallmap, then (zero) flabel ----
        now = int(time.time()) + 2177452800  # Lisa epoch (seconds since 1901-01-01)
        hp = bytearray(512)
        # hentry: the v16/17 hentry is 112 bytes on disk; the 128-byte region
        # is zero-filled and the fields below set it up.  parentID is at 108
        # and fsOverhead at 110 (UpgradeFile, source-fsprim: ClearMem(b+108,20)
        # "clear parentID"; real v17 hentries show parentID=0, fsOverhead=1
        # there for root-level files).
        hp[0] = len(name_bytes)
        hp[1 : 1 + len(name_bytes)] = name_bytes
        hp[0x22:0x2A] = self._make_unique_id(new_sfile)
        struct.pack_into(">H", hp, 0x2A, BTREE_HE_VERSION)  # version = cur_file_version
        struct.pack_into(">H", hp, 0x2C, 14 << 8)  # ftype = userfile
        struct.pack_into(">I", hp, 0x2E, now)  # DTC (created)
        # DTA/DTM/DTB/DTS, machine_id, the 7 booleans, result_scavenge, unused,
        # system/user type/subtype, build_info, file_portion, password: all 0
        struct.pack_into(">H", hp, 0x6C, 0)  # parentID (root level -> 0, as in keys)
        struct.pack_into(">H", hp, 0x6E, 1)  # fsOverhead = 1 (MAKE_ENTRY)
        # smallmap (inlined into hint page 0 on v16/17 volumes)
        struct.pack_into(">I", hp, smallmap_off, num_data_pages)  # size = data pages
        struct.pack_into(">H", hp, smallmap_off + 4, 9)  # max_entries
        runs = self._group_runs(data_pages)
        if len(runs) > 9:
            print(
                "ERROR: the file's data pages span more than 9 runs; the smallmap is full."
            )
            return 1
        struct.pack_into(">H", hp, smallmap_off + 6, len(runs))  # ecount
        for i, (st, cnt) in enumerate(runs):
            struct.pack_into(">I", hp, smallmap_off + 8 + i * 6, st)
            struct.pack_into(">H", hp, smallmap_off + 8 + i * 6 + 4, cnt)

        for i, p in enumerate(hint_pages):
            absn = mddf + p
            self._mem_write_sector_data(absn, bytes(hp))
            fwd = hint_pages[i + 1] if i + 1 < len(hint_pages) else END
            bkw = hint_pages[i - 1] if i > 0 else END
            # Hint-page tags on this volume: version = sentry version, volume =
            # 256, fileid = 0x10000 - sfile, dataused = 0 (matches every
            # existing hint page; the driver does not check the volume field).
            self._mem_write_sector_tag(
                absn,
                self._build_tag(
                    sentry_version,
                    256,
                    (0x10000 - new_sfile) & 0xFFFF,
                    0,
                    p,
                    i,
                    fwd,
                    bkw,
                ),
            )
            modified.add(absn)

        # ---- data pages ----
        for i, p in enumerate(data_pages):
            absn = mddf + p
            chunk = data[i * 512 : (i + 1) * 512]
            dp = bytearray(512)
            dp[: len(chunk)] = chunk
            self._mem_write_sector_data(absn, bytes(dp))
            fwd = data_pages[i + 1] if i + 1 < len(data_pages) else END
            bkw = data_pages[i - 1] if i > 0 else END
            self._mem_write_sector_tag(
                absn,
                self._build_tag(
                    sentry_version, 0, new_sfile, len(chunk), p, i, fwd, bkw
                ),
            )
            modified.add(absn)

        # ---- slist (sentry) entry ----
        sl = bytearray(self.read_sector(slist_abs))
        struct.pack_into(">I", sl, soffs, hint_pages[0])  # hintaddr
        struct.pack_into(
            ">I", sl, soffs + 4, data_pages[0] if data_pages else 0
        )  # fileaddr
        struct.pack_into(">I", sl, soffs + 8, filesize)  # filesize
        self._mem_write_sector_data(slist_abs, bytes(sl))
        modified.add(slist_abs)

        # ---- build the 64-byte fileentry ObjRec and insert it into the tree ----
        key = self._bt_make_key(0, name_bytes)  # parentID = ROOT_NID
        rec = bytearray(64)
        rec[0:36] = key
        struct.pack_into(">H", rec, 36, 3 << 8)  # eType = fileentry
        struct.pack_into(">H", rec, 38, new_sfile)
        struct.pack_into(">I", rec, 40, now)  # fileDTC
        struct.pack_into(">I", rec, 44, 0)  # fileDTM (never opened)
        struct.pack_into(">I", rec, 48, filesize)  # size
        struct.pack_into(">I", rec, 52, num_data_pages * 512)  # physSize (page-aligned)
        struct.pack_into(">H", rec, 56, 1)  # fsOvrhd
        # flags = 0 (never opened by the OS), fileUnused = 0
        free_before = set(free_set)
        try:
            ok, new_root, actual_depth = self._bt_insert(
                key, bytes(rec), free_set, modified
            )
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1
        if not ok:
            print(
                f"WARNING: a file named '{lisa_name}' already exists on this volume; not adding it."
            )
            return 3
        # the depth the tree has after this insert (measured from the real
        # tree structure, not from the possibly-stale MDDF field)
        new_depth = actual_depth + (1 if new_root is not None else 0)
        # the pages consumed from free_set are exactly the new node pages
        consumed = sorted(free_before - free_set)
        cset = set(consumed)
        node_pages = [
            q
            for s in consumed
            if s - 1 not in cset
            for q in range(s, s + BTREE_NODE_BLKS)
        ]

        # ---- update the MDDF ----
        m = bytearray(self.read_sector(mddf))
        struct.pack_into(">H", m, 0xB0, (filecount + 1) & 0xFFFF)  # filecount += 1
        struct.pack_into(
            ">I", m, 0xBA, freecount - needed_pages - len(node_pages)
        )  # freecount -= used
        struct.pack_into(
            ">H", m, 0x122, (fs_overhead + len(node_pages)) & 0xFFFF
        )  # fs_overhead += node pages
        if new_sfile < maxfiles:
            struct.pack_into(
                ">H", m, 0x9E, new_sfile + 1
            )  # empty_file := new_sfile + 1
        if new_root is not None:
            struct.pack_into(">I", m, 0x12E, new_root)  # root_page := new root
        # Always write the measured tree depth back: this self-heals a stale
        # MDDF tree_depth (e.g. left over from an interrupted write), which
        # would otherwise make a later add/delete mix tree levels and corrupt
        # the catalog.
        if new_depth != self._mddf_u16(0x132):
            print(
                f"WARNING: MDDF tree_depth was {self._mddf_u16(0x132)} but the tree actually has depth {new_depth}; correcting the MDDF."
            )
        struct.pack_into(">H", m, 0x132, new_depth)  # tree_depth := measured depth
        self._mem_write_sector_data(mddf, bytes(m))
        modified.add(mddf)

        # ---- mark the new pages allocated in the bitmap ----
        for i in range(bitmap_pages):
            bb = bytearray(self.read_sector(mddf + bitmap_addr + i))
            changed = False
            for p in hint_pages + data_pages + node_pages:
                if p // 4096 == i:
                    bb[(p % 4096) // 8] |= 1 << ((p % 4096) % 8)
                    changed = True
            if changed:
                self._mem_write_sector_data(mddf + bitmap_addr + i, bytes(bb))
                modified.add(mddf + bitmap_addr + i)

        # ---- flush modified sectors to disk, patching tag checksums in one pass ----
        with open(self._file_name, "r+b") as fw:
            for sn in sorted(modified):
                d = self.read_sector(sn)
                tg = self.read_tags_for_sector(sn)
                ck = self.calculate_new_tag_checksum(sn)
                tg = tg[:11] + bytes([ck]) + tg[12:]
                self._file.seek(self._sector_tag_file_offset(sn) + 11)
                self._file.write(bytes([ck]))  # keep the in-memory copy in sync
                fw.seek(self._sector_data_file_offset(sn))
                fw.write(d)
                fw.seek(self._sector_tag_file_offset(sn))
                fw.write(tg)

        self.fix_dc42_checksum(confirm=False)

        print(
            f"Added '{lisa_name}' ({filesize} bytes) as s-file {new_sfile}. [b-tree catalog]"
        )
        print(f"  hint pages (abs): {[mddf + p for p in hint_pages]}")
        print(f"  data pages (abs): {[mddf + p for p in data_pages]}")
        if node_pages:
            print(f"  new b-tree nodes (abs): {[mddf + p for p in node_pages]}")
        if new_root is not None:
            print(
                f"  new root page (abs): {mddf + new_root}; tree depth increased to {self._mddf_u16(0x132)}"
            )
        return 0

    def add_file(self, host_file_path: str, lisa_name: str) -> "int":
        """Add a host file into this flat-catalog (fs_version 14/15) volume as a new Lisa file.

        This replicates the OS's MAKE_ENTRY + NEW_SFILE + APPENDPAGES + write path
        (see LISA_OS/OS/source-fsprim, -sfileio, -sfileio1 .text.unix.txt):
        allocate an s-file number, allocate hint + data pages from the bitmap,
        write the hentry and smallmap, write the data + tag chains, write the
        slist sentry, insert a catalog centry, and update the MDDF and bitmap.
        All modified sectors get their tag checksums recomputed, and the DC42
        header checksums are fixed at the end.

        Text files: if lisa_name ends in ".TEXT" (case-insensitive), the file's
        complete on-disk byte stream is built in memory first, by
        build_lisa_text_file_data(): host "\r\n"/"\n" line endings are
        normalized to Lisa "\r", the text is laid out in 1024-byte pages per
        LisaOsTextFileSpecification.txt (CR-terminated lines, null-filled
        pages, CR-null page terminators, trailing CR), and the zero-filled
        1024-byte header page is prepended (the "dump" command strips that
        header). `data` is then exactly the byte stream written to the file's
        data pages.

        Returns the exit code to use: 0 on success, 3 if a file with the
        same name is already on the volume, and 1 on any other failure.
        """
        # ---- preconditions ----
        if not self.is_flat_catalog_volume():
            if self._fs_version in (16, 17):
                return self._add_file_btree(host_file_path, lisa_name)
            print(
                f"ERROR: add supports flat-catalog (fs_version 14/15) and b-tree (fs_version 16/17) volumes; this volume is fs_version {self._fs_version}."
            )
            return 1
        if self._single_tag_size != 20:
            print("ERROR: add currently supports only 20-byte-tag (hard disk) images.")
            return 1
        if not os.path.isfile(host_file_path):
            print(f"ERROR: host file '{host_file_path}' not found.")
            return 1
        with open(host_file_path, "rb") as f:
            data = f.read()

        # ---- validate the Lisa file name ----
        if not lisa_name:
            print("ERROR: the Lisa file name is empty.")
            return 1
        if len(lisa_name) > 33:
            print(
                f"ERROR: Lisa file name '{lisa_name}' is {len(lisa_name)} chars; max is 33."
            )
            return 1
        # Lisa does not treat '/' as a folder delimiter in file names, so
        # names containing '/' are allowed (same as on b-tree volumes).
        try:
            name_bytes = lisa_name.encode("mac-roman")
        except UnicodeEncodeError as e:
            print(f"ERROR: cannot encode '{lisa_name}' as Mac Roman: {e}")
            return 1
        if len(name_bytes) > 33:
            print(
                f"ERROR: '{lisa_name}' is {len(name_bytes)} Mac-Roman bytes; max is 33."
            )
            return 1

        # ---- ".TEXT" files (name ends in ".TEXT", case-insensitive) ----
        # Build the complete on-disk byte stream in memory first (see
        # build_lisa_text_file_data() for the text file binary structure
        # `data` is then exactly what gets
        # written to the file's data pages.
        is_text_file = lisa_name.upper().endswith(".TEXT")
        if is_text_file:
            cleaned = strip_archive_trailing_0xff(data)
            if cleaned != data:
                print(
                    f"NOTE: stripped the archive's trailing 0xFF byte from '{host_file_path}'."
                )
            data = build_lisa_text_file_data(cleaned)
        filesize = len(data)

        mddf = self._mddf_sector_number

        # ---- MDDF fields ----
        empty_file = self._mddf_u16(0x9E)
        maxfiles = self._mddf_u16(0xA0)
        hintsize = self._mddf_u16(0xA2)
        filecount = self._mddf_u16(0xB0)
        freecount = self._mddf_u32(0xBA)
        bitmap_addr = self._mddf_u32(0x88)
        bitmap_pages = self._mddf_u16(0x92)
        slist_addr = self._mddf_u32(0x94)
        slist_packing = self._mddf_u16(0x98)
        rootmaxentries = self._mddf_u16(0xC0)
        map_offset = self._mddf_u16(0xAC)  # hint-page index of the file map / smallmap

        num_data_pages = (filesize + 511) // 512
        needed_pages = hintsize + num_data_pages

        # ---- find a free s-file number (first free slist slot at/after empty_file) ----
        new_sfile = None
        for i in range(empty_file, maxfiles):
            e = self._slist_entry(i)
            if e is not None and e[0] == 0:  # hintaddr == 0 => free slot
                new_sfile = i
                break
        if new_sfile is None:
            print("ERROR: no free s-file slot available (slist is full).")
            return 1

        # ---- find free pages (prefer contiguous runs, like sallocate) ----
        free_pages = self._bitmap_free_pages()
        if len(free_pages) < needed_pages:
            print(
                f"ERROR: not enough free space: need {needed_pages} pages ({hintsize} hint + {num_data_pages} data) but only {len(free_pages)} free."
            )
            return 1
        free_set = set(free_pages)
        hs = self._find_contiguous_run(free_set, hintsize)
        hint_pages = (
            [hs + i for i in range(hintsize)]
            if hs is not None
            else free_pages[:hintsize]
        )
        for p in hint_pages:
            free_set.discard(p)
        if num_data_pages:
            ds = self._find_contiguous_run(free_set, num_data_pages)
            data_pages = (
                [ds + i for i in range(num_data_pages)]
                if ds is not None
                else sorted(free_set)[:num_data_pages]
            )
        else:
            data_pages = []
        for p in data_pages:
            free_set.discard(p)

        # ---- locate the catalog insert slot (read-only) BEFORE writing anything, so a
        #      failure (full catalog / duplicate) does not leave self._file dirtied ----
        rc_sfile = self._find_rootcatalog_sfile()
        rc = self._slist_entry(rc_sfile)
        chain = self._data_page_chain(rc[1])
        cat_data = b"".join(self.read_sector(mddf + p) for p in chain)
        slot = self._find_insert_slot(cat_data, lisa_name, rootmaxentries)
        if slot is None:
            print("ERROR: the rootcatalog is full; cannot add the file.")
            return 1
        if slot < 0:
            print(
                f"WARNING: a file named '{lisa_name}' already exists on this volume; not adding it."
            )
            return 3

        # ---- page-tag version/volume fields ----
        # Every page label (hint AND data) must carry the s-file's sentry version,
        # i.e. the 2-byte `version` field of the slist slot for this s-file. The OS
        # stamps it on allocation (source-sfileio APPENDPAGES:
        #   pl.version := ptrSent^.version;
        # and source-fsprim on the first data write:
        #   pl.version := fptr^.sent.version;). The driver verifies the tag's
        # version against the sentry version on read; a mismatch makes the file
        # unreadable (e.g. "Error 132 not a valid program file").
        # The slot's version persists across file creations (KILL_SFILE does
        # `version := version + 1`), so we must read the CURRENT slot value and
        # keep the sentry unchanged (below). It is NOT a constant copied from the
        # rootcatalog. `volume` is always 0 in OS-written labels (pl.volume := 0).
        spage = new_sfile // slist_packing
        soffs = (new_sfile % slist_packing) * 14
        slist_abs = mddf + slist_addr + spage
        sentry_version = struct.unpack(
            ">H", self.read_sector(slist_abs)[soffs + 12 : soffs + 14]
        )[0]
        hint_version = sentry_version
        hint_volume = 0
        data_version = sentry_version
        data_volume = 0

        END = 0xFFFFFF
        modified = set()

        # ---- hint page 0: the hentry ----
        now = int(time.time()) + 2177452800  # Lisa epoch (seconds since 1901-01-01)
        h = bytearray(512)
        h[0] = len(name_bytes)
        h[1 : 1 + len(name_bytes)] = name_bytes
        h[0x22:0x2A] = self._make_unique_id(new_sfile)
        struct.pack_into(">H", h, 0x2A, self._fs_version)  # version
        h[0x2C] = 14  # ftype = userfile
        struct.pack_into(">I", h, 0x2E, now)  # DTC (created)
        struct.pack_into(">I", h, 0x32, now)  # DTA (accessed)
        struct.pack_into(">I", h, 0x36, now)  # DTM (modified)
        # ---- hint page 1: the smallmap / file map ----
        sm = bytearray(512)
        struct.pack_into(">I", sm, 0, num_data_pages)  # size = number of data pages
        struct.pack_into(">H", sm, 4, 83)  # max_entries = MAXMAPINDEX (old_volume)
        runs = self._group_runs(data_pages)
        struct.pack_into(">H", sm, 6, len(runs))  # ecount
        for i, (st, cnt) in enumerate(runs[:9]):
            struct.pack_into(">I", sm, 8 + i * 6, st)
            struct.pack_into(">H", sm, 8 + i * 6 + 4, cnt)
        if map_offset >= hintsize:
            print(
                f"ERROR: map_offset ({map_offset}) is beyond hintsize ({hintsize}); cannot place the file map."
            )
            return 1
        # hentry on hint page 0; smallmap / file map on hint page `map_offset` (1 for fs 14/15).
        hint_page_data = {0: bytes(h), map_offset: bytes(sm)}
        for i, p in enumerate(hint_pages):
            absn = mddf + p
            self._mem_write_sector_data(absn, hint_page_data.get(i, bytes(512)))
            fwd = hint_pages[i + 1] if i + 1 < len(hint_pages) else END
            bkw = hint_pages[i - 1] if i > 0 else END
            self._mem_write_sector_tag(
                absn,
                self._build_tag(
                    hint_version,
                    hint_volume,
                    (0x10000 - new_sfile) & 0xFFFF,
                    0,
                    p,
                    i,
                    fwd,
                    bkw,
                ),
            )
            modified.add(absn)

        # ---- data pages ----
        for i, p in enumerate(data_pages):
            absn = mddf + p
            chunk = data[i * 512 : (i + 1) * 512]
            dp = bytearray(512)
            dp[: len(chunk)] = chunk
            self._mem_write_sector_data(absn, bytes(dp))
            fwd = data_pages[i + 1] if i + 1 < len(data_pages) else END
            bkw = data_pages[i - 1] if i > 0 else END
            self._mem_write_sector_tag(
                absn,
                self._build_tag(
                    data_version, data_volume, new_sfile, len(chunk), p, i, fwd, bkw
                ),
            )
            modified.add(absn)

        # ---- slist (sentry) entry ----
        # spage/soffs/slist_abs were computed above (to read the sentry version).
        # We overwrite only hintaddr/fileaddr/filesize; the 2-byte `version`
        # field is left exactly as found, matching the OS (which only changes it
        # in KILL_SFILE). The page tags written above use this same value.
        sl = bytearray(self.read_sector(slist_abs))
        struct.pack_into(">I", sl, soffs, hint_pages[0])  # hintaddr
        struct.pack_into(
            ">I", sl, soffs + 4, data_pages[0] if data_pages else 0
        )  # fileaddr
        struct.pack_into(">I", sl, soffs + 8, filesize)  # filesize
        self._mem_write_sector_data(slist_abs, bytes(sl))
        modified.add(slist_abs)

        # ---- insert the catalog centry into the rootcatalog data (slot found earlier) ----
        centry_off = slot * 54
        cpage_idx = centry_off // 512
        cpage_abs = mddf + chain[cpage_idx]
        cbyte = centry_off % 512
        cp = bytearray(self.read_sector(cpage_abs))
        cp[cbyte : cbyte + 54] = self._build_centry(name_bytes, new_sfile)
        self._mem_write_sector_data(cpage_abs, bytes(cp))
        modified.add(cpage_abs)

        # ---- update the MDDF ----
        m = bytearray(self.read_sector(mddf))
        struct.pack_into(">H", m, 0xB0, (filecount + 1) & 0xFFFF)  # filecount += 1
        struct.pack_into(">I", m, 0xBA, freecount - needed_pages)  # freecount -= used
        if new_sfile < maxfiles:
            struct.pack_into(
                ">H", m, 0x9E, new_sfile + 1
            )  # empty_file := new_sfile + 1
        self._mem_write_sector_data(mddf, bytes(m))
        modified.add(mddf)

        # ---- mark the new pages allocated in the bitmap ----
        for i in range(bitmap_pages):
            bb = bytearray(self.read_sector(mddf + bitmap_addr + i))
            changed = False
            for p in hint_pages + data_pages:
                if p // 4096 == i:
                    bit_in_page = p % 4096
                    bb[bit_in_page // 8] |= 1 << (bit_in_page % 8)
                    changed = True
            if changed:
                self._mem_write_sector_data(mddf + bitmap_addr + i, bytes(bb))
                modified.add(mddf + bitmap_addr + i)

        # ---- flush modified sectors to disk, patching tag checksums in one pass ----
        with open(self._file_name, "r+b") as fw:
            for sn in sorted(modified):
                d = self.read_sector(sn)
                tg = self.read_tags_for_sector(sn)
                ck = self.calculate_new_tag_checksum(sn)
                tg = tg[:11] + bytes([ck]) + tg[12:]
                self._file.seek(self._sector_tag_file_offset(sn) + 11)
                self._file.write(bytes([ck]))  # keep the in-memory copy in sync
                fw.seek(self._sector_data_file_offset(sn))
                fw.write(d)
                fw.seek(self._sector_tag_file_offset(sn))
                fw.write(tg)

        self.fix_dc42_checksum(confirm=False)

        if is_text_file:
            text_pages = (filesize - TEXT_FILE_HEADER_SIZE) // TEXT_FILE_PAGE_SIZE
            print(
                f"Added '{lisa_name}' as s-file {new_sfile}: "
                f"{text_pages} text page(s), "
                f"consisting of a {TEXT_FILE_HEADER_SIZE}-bytes all-zeroes header and "
                f"({filesize - TEXT_FILE_HEADER_SIZE} bytes of CR-terminated text."
            )
        else:
            print(f"Added '{lisa_name}' ({filesize} bytes) as s-file {new_sfile}.")
        print(f"  hint pages (abs): {[mddf + p for p in hint_pages]}")
        print(f"  data pages (abs): {[mddf + p for p in data_pages]}")
        return 0


class ReplaceFileMixin(AddFileMixin):
    """The "replace" command: replace_file() and its private helpers.

    Inherits every AddFileMixin helper (MDDF/sector/tag/bitmap/b-tree machinery)
    and adds replace_file(): replace the contents of an EXISTING file on the
    volume with the contents of a host file, reusing the sectors the file
    already occupies:
      * if the new file is LARGER, all of the old file's sectors are reused
        and only the extra sectors are newly allocated;
      * if the new file is SMALLER, only as many of the old sectors as needed
        are reused and the rest are freed.
    The file's s-file number, sentry version, hint pages, and catalog entry
    all stay exactly where they are; only the contents and the size
    bookkeeping change (sentry filesize/fileaddr, the file map, the data-page
    tag chain, the catalog record's size fields on b-tree volumes, the
    allocation bitmap, and the MDDF freecount).
    """

    def _bt_scan_entries(self):
        """Yield (leaf_node, record_index, record) for EVERY record in the
        b-tree root catalog, walking every node. Note the eType field is 16
        bits on disk: the high byte is the entry type (3 = fileentry, 8 =
        threadentry, 1 = direntry), the low byte is a subtype/flags value the
        OS stores there and we must preserve. A directory appears as a
        threadentry record keyed (parent, name) plus a direntry record keyed
        (own NID, empty name)."""
        root = self._mddf_u32(0x12E)
        if root == BTREE_BAD:
            return
        stack = [root]
        while stack:
            page = stack.pop()
            node = self._bt_read_node(page)
            if node.kind == BTREE_NONLEAF:
                for rec in node.records:
                    stack.append(struct.unpack(">I", rec[0:4])[0])
            else:
                for idx, rec in enumerate(node.records):
                    yield node, idx, rec

    def _find_flat_file_slot(
        self, cat_data: bytes, lisa_name: str, rootmaxentries: int
    ):
        """Find the slot of an EXISTING centry for lisa_name in flat-catalog
        data, with the same hash + linear probing as LOOKUP_BY_ENAME (cf.
        _find_insert_slot). Returns the slot index, or None if the name is not
        on the volume."""
        start = flat_catalog_hash(lisa_name, rootmaxentries)
        for i in range(rootmaxentries):
            idx = (start + i) % rootmaxentries
            rec = cat_data[idx * 54 : idx * 54 + 54]
            name_len = rec[0]
            cetype = rec[34]
            if cetype == 0 and name_len == 0:
                return None  # empty slot: the probe ends here, name not found
            if cetype == 7:  # 'removed' tombstone: keep probing
                continue
            if name_len:
                name = rec[1 : 1 + name_len].decode("mac-roman", errors="replace")
                if name.upper() == lisa_name.upper():
                    return idx
        return None

    def replace_file(self, host_file_path: str, lisa_name: str) -> "int":
        """Replace the contents of an EXISTING file on this volume with the
        contents of a host file, in place.

        The existing file is located by name (case-insensitive): on b-tree
        volumes every catalog record is scanned (fileentry records only), on
        flat-catalog volumes the rootcatalog is probed with the same hash +
        linear probing as the OS LOOKUP_BY_ENAME.

        Sector reuse: the new contents are written onto the old file's data
        pages in file order. If the new file is larger, ALL of the old file's
        sectors are reused and only the extra sectors are newly allocated
        (contiguous run preferred, like sallocate). If the new file is smaller,
        only as many of the old sectors as needed are reused and the rest are
        freed (allocation bitmap + MDDF freecount).

        What gets updated: the data-page contents and tag chain (fwd/bkwd
        links, dataused, relpage, checksums), the file map / smallmap (in
        hint page 0 at smallmap_offset on b-tree volumes, in hint page
        map_offset on flat volumes; the hentry around it is preserved and the
        map's max_entries is kept as found), the slist sentry's fileaddr and
        filesize (hintaddr and version untouched), the catalog record's
        size/physSize fields in place (b-tree volumes only; on flat volumes
        the 54-byte centry carries no size), and the MDDF freecount plus the
        allocation bitmap.

        ".TEXT" files: same rule as add — if lisa_name ends in ".TEXT"
        (case-insensitive) the host text is converted to the Lisa on-disk text
        layout first (1024-byte header page + CR-terminated text pages).

        Returns the exit code to use: 0 on success, 3 if no file with that
        name is on the volume, and 1 on any other failure.
        """
        # ---- preconditions (same as add) ----
        is_btree = not self.is_flat_catalog_volume()
        if self._fs_version not in (14, 15, 16, 17):
            print(
                f"ERROR: replace supports flat-catalog (fs_version 14/15) and b-tree (fs_version 16/17) volumes; this volume is fs_version {self._fs_version}."
            )
            return 1
        if self._single_tag_size != 20:
            print(
                "ERROR: replace currently supports only 20-byte-tag (hard disk) images."
            )
            return 1
        if not os.path.isfile(host_file_path):
            print(f"ERROR: host file '{host_file_path}' not found.")
            return 1
        with open(host_file_path, "rb") as f:
            data = f.read()

        # ---- validate the Lisa file name (same limits as add) ----
        max_name = BTREE_MAX_NAME if is_btree else 33
        if not lisa_name:
            print("ERROR: the Lisa file name is empty.")
            return 1
        if len(lisa_name) > max_name:
            print(
                f"ERROR: Lisa file name '{lisa_name}' is {len(lisa_name)} chars; max is {max_name}."
            )
            return 1
        try:
            name_bytes = lisa_name.encode("mac-roman")
        except UnicodeEncodeError as e:
            print(f"ERROR: cannot encode '{lisa_name}' as Mac Roman: {e}")
            return 1
        if len(name_bytes) > max_name:
            print(
                f"ERROR: '{lisa_name}' is {len(name_bytes)} Mac-Roman bytes; max is {max_name}."
            )
            return 1

        # ---- ".TEXT" files (same as add) ----
        is_text_file = lisa_name.upper().endswith(".TEXT")
        if is_text_file:
            cleaned = strip_archive_trailing_0xff(data)
            if cleaned != data:
                print(
                    f"NOTE: stripped the archive's trailing 0xFF byte from '{host_file_path}'."
                )
            data = build_lisa_text_file_data(cleaned)
        filesize = len(data)
        num_new_pages = (filesize + 511) // 512

        mddf = self._mddf_sector_number

        # ---- MDDF fields ----
        hintsize = self._mddf_u16(0xA2)
        freecount = self._mddf_u32(0xBA)
        bitmap_addr = self._mddf_u32(0x88)
        bitmap_pages = self._mddf_u16(0x92)
        slist_addr = self._mddf_u32(0x94)
        slist_packing = self._mddf_u16(0x98)
        smallmap_off = self._mddf_u16(
            0x114
        )  # b-tree: byte offset of the smallmap in hint page 0
        map_offset = self._mddf_u16(0xAC)  # flat: hint-page index of the file map

        # ---- locate the existing file (read-only, BEFORE writing anything) ----
        if is_btree:
            if hintsize != 1:
                print(
                    f"ERROR: replace on b-tree volumes currently supports only hintsize 1 (found {hintsize})."
                )
                return 1
            if not (0x80 <= smallmap_off <= 0x100):
                print(
                    f"ERROR: unexpected smallmap_offset 0x{smallmap_off:x}; cannot update the file map in hint page 0."
                )
                return 1
            file_matches = []
            dir_matches = 0
            for node, idx, rec in self._bt_scan_entries():
                name = (
                    rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", errors="replace")
                )
                if name.upper() != lisa_name.upper():
                    continue
                if rec[36] == 3:  # eType high byte = fileentry
                    file_matches.append((node, idx, rec))
                else:
                    dir_matches += 1  # a directory (threadentry/direntry) record
            if file_matches:
                if len(file_matches) > 1:
                    print(
                        f"ERROR: {len(file_matches)} files are named '{lisa_name}' on this volume (in "
                        "different directories); cannot tell which one to replace."
                    )
                    return 1
                leaf, idx, rec = file_matches[0]
                sfile = struct.unpack(">H", rec[38:40])[0]
            elif dir_matches:
                print(
                    f"ERROR: a catalog entry named '{lisa_name}' exists on this volume but it is a "
                    "directory, not a file; cannot replace it."
                )
                return 1
            else:
                print(
                    f"WARNING: no file named '{lisa_name}' on this volume; nothing to replace."
                )
                return 3
        else:
            rc_sfile = self._find_rootcatalog_sfile()
            rc = self._slist_entry(rc_sfile)
            if rc is None or rc[1] == 0:
                print(
                    "ERROR: could not read the rootcatalog; the volume's metadata is inconsistent."
                )
                return 1
            cat_chain = self._data_page_chain(rc[1])
            cat_data = b"".join(self.read_sector(mddf + p) for p in cat_chain)
            rootmaxentries = self._mddf_u16(0xC0)
            slot = self._find_flat_file_slot(cat_data, lisa_name, rootmaxentries)
            if slot is None:
                print(
                    f"WARNING: no file named '{lisa_name}' on this volume; nothing to replace."
                )
                return 3
            centry = cat_data[slot * 54 : slot * 54 + 54]
            if centry[34] != 3:  # cetype must be fileentry
                print(
                    f"ERROR: a catalog entry named '{lisa_name}' exists on this volume but is not a "
                    f"regular file (cetype = {centry[34]}); cannot replace it."
                )
                return 1
            sfile = struct.unpack(">H", centry[36:38])[0]
            leaf = idx = None  # no b-tree record to update on flat volumes

        # ---- the file's current layout (read-only) ----
        sentry = self._slist_entry(sfile)
        if sentry is None:
            print(
                f"ERROR: the slist has no entry for s-file {sfile}; the volume's metadata is inconsistent."
            )
            return 1
        old_hintaddr, old_fileaddr, old_filesize, sentry_version = sentry
        if old_hintaddr == 0:
            print(
                f"ERROR: s-file {sfile} has no hint pages; the volume's metadata is inconsistent."
            )
            return 1
        try:
            old_chain = self._data_page_chain(old_fileaddr)
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1
        old_num_pages = (old_filesize + 511) // 512
        if len(old_chain) != old_num_pages:
            print(
                f"NOTE: the tag chain of '{lisa_name}' has {len(old_chain)} page(s) but the sentry says "
                f"{old_filesize} bytes ({old_num_pages} page(s)); the chain and the sentry disagree. "
                "Using the chain as the set of sectors to reuse."
            )

        # ---- plan the new layout: reuse the old pages in file order, allocate the rest ----
        keep = min(num_new_pages, len(old_chain))
        reused_pages = old_chain[:keep]
        freed_pages = old_chain[keep:]
        need = num_new_pages - keep
        new_pages = []
        if need > 0:
            free_pages = self._bitmap_free_pages()
            # pages we are about to reuse are never "free", even if the bitmap disagrees
            free_set = set(free_pages) - set(reused_pages)
            if len(free_set) < need:
                print(
                    f"ERROR: not enough free space: need {need} more page(s) but only {len(free_set)} free."
                )
                return 1
            ds = self._find_contiguous_run(free_set, need)
            new_pages = (
                [ds + i for i in range(need)]
                if ds is not None
                else sorted(free_set)[:need]
            )
            for p in new_pages:
                free_set.discard(p)
        data_pages = reused_pages + new_pages

        END = 0xFFFFFF
        modified = set()

        # ---- data pages: overwrite the reused ones in place, fill the new ones ----
        # The tags carry the s-file's CURRENT sentry version (unchanged by this
        # operation, exactly as in the OS: only KILL_SFILE bumps it), volume 0,
        # fileid = sfile, and dataused = bytes actually written (the last page
        # is short unless the size is page-aligned).
        for i, p in enumerate(data_pages):
            absn = mddf + p
            chunk = data[i * 512 : (i + 1) * 512]
            dp = bytearray(512)
            dp[: len(chunk)] = chunk
            self._mem_write_sector_data(absn, bytes(dp))
            fwd = data_pages[i + 1] if i + 1 < len(data_pages) else END
            bkw = data_pages[i - 1] if i > 0 else END
            self._mem_write_sector_tag(
                absn,
                self._build_tag(sentry_version, 0, sfile, len(chunk), p, i, fwd, bkw),
            )
            modified.add(absn)

        # ---- file map / smallmap: rewrite it in its hint page, keep the hentry ----
        if is_btree:
            map_abs = mddf + old_hintaddr  # the smallmap is inlined into hint page 0
            sm_off = smallmap_off
        else:
            if map_offset >= hintsize:
                print(
                    f"ERROR: map_offset ({map_offset}) is beyond hintsize ({hintsize}); cannot update the file map."
                )
                return 1
            map_abs = mddf + old_hintaddr + map_offset
            sm_off = 0
        sm = bytearray(self.read_sector(map_abs))
        maxent = struct.unpack(">H", sm[sm_off + 4 : sm_off + 6])[0]
        runs = self._group_runs(data_pages)
        if maxent == 0 or len(runs) > maxent:
            print(
                f"ERROR: the new file spans {len(runs)} run(s); the file map holds at most {maxent}."
            )
            return 1
        struct.pack_into(">I", sm, sm_off, num_new_pages)  # size = number of data pages
        struct.pack_into(">H", sm, sm_off + 4, maxent)  # max_entries: keep as found
        struct.pack_into(">H", sm, sm_off + 6, len(runs))  # ecount
        for i, (st, cnt) in enumerate(runs):
            struct.pack_into(">I", sm, sm_off + 8 + i * 6, st)
            struct.pack_into(">H", sm, sm_off + 8 + i * 6 + 4, cnt)
        # zero the stale entries beyond the new ecount (the reader uses ecount,
        # but a clean map cannot mislead a future tool)
        for i in range(len(runs), maxent):
            struct.pack_into(">I", sm, sm_off + 8 + i * 6, 0)
            struct.pack_into(">H", sm, sm_off + 8 + i * 6 + 4, 0)
        self._mem_write_sector_data(map_abs, bytes(sm))
        modified.add(map_abs)

        # ---- slist sentry: fileaddr + filesize (hintaddr and version untouched) ----
        spage = sfile // slist_packing
        soffs = (sfile % slist_packing) * 14
        slist_abs = mddf + slist_addr + spage
        sl = bytearray(self.read_sector(slist_abs))
        struct.pack_into(
            ">I", sl, soffs + 4, data_pages[0] if data_pages else 0
        )  # fileaddr
        struct.pack_into(">I", sl, soffs + 8, filesize)  # filesize
        self._mem_write_sector_data(slist_abs, bytes(sl))
        modified.add(slist_abs)

        # ---- b-tree catalog: update the existing record's size fields in place ----
        # (no new entry: the name's record already exists; key, eType, sfile,
        #  dates, flags and the 2-byte eType subtype are all preserved)
        if is_btree:
            rec = bytearray(leaf.records[idx])
            struct.pack_into(">I", rec, 48, filesize)  # size
            struct.pack_into(
                ">I", rec, 52, num_new_pages * 512
            )  # physSize (page-aligned)
            leaf.records[idx] = bytes(rec)
            self._bt_write_node(leaf, modified)

        # ---- MDDF: freecount only (same file, no new s-file, no new catalog nodes) ----
        m = bytearray(self.read_sector(mddf))
        struct.pack_into(
            ">I", m, 0xBA, (freecount + len(freed_pages) - len(new_pages)) & 0xFFFFFFFF
        )
        self._mem_write_sector_data(mddf, bytes(m))
        modified.add(mddf)

        # ---- allocation bitmap: set the newly allocated pages, clear the freed ones ----
        for i in range(bitmap_pages):
            bb = bytearray(self.read_sector(mddf + bitmap_addr + i))
            changed = False
            for p in new_pages:
                if p // 4096 == i:
                    bit = p % 4096
                    if not (bb[bit // 8] >> (bit % 8)) & 1:
                        bb[bit // 8] |= 1 << (bit % 8)
                        changed = True
            for p in freed_pages:
                if p // 4096 == i:
                    bit = p % 4096
                    if (bb[bit // 8] >> (bit % 8)) & 1:
                        bb[bit // 8] &= ~(1 << (bit % 8))
                        changed = True
            if changed:
                self._mem_write_sector_data(mddf + bitmap_addr + i, bytes(bb))
                modified.add(mddf + bitmap_addr + i)

        # ---- flush modified sectors to disk, patching tag checksums in one pass ----
        with open(self._file_name, "r+b") as fw:
            for sn in sorted(modified):
                d = self.read_sector(sn)
                tg = self.read_tags_for_sector(sn)
                ck = self.calculate_new_tag_checksum(sn)
                tg = tg[:11] + bytes([ck]) + tg[12:]
                self._file.seek(self._sector_tag_file_offset(sn) + 11)
                self._file.write(bytes([ck]))  # keep the in-memory copy in sync
                fw.seek(self._sector_data_file_offset(sn))
                fw.write(d)
                fw.seek(self._sector_tag_file_offset(sn))
                fw.write(tg)

        self.fix_dc42_checksum(confirm=False)

        print(
            f"Replaced '{lisa_name}' (s-file {sfile}): {old_filesize} byte(s) -> {filesize} byte(s)."
        )
        print(
            f"  reused {keep} of {len(old_chain)} old data page(s); allocated {len(new_pages)} new page(s); "
            f"freed {len(freed_pages)} page(s)."
        )
        print(f"  data pages (abs): {[mddf + p for p in data_pages]}")
        return 0


class DeleteFileMixin(ReplaceFileMixin):
    """The "delete" command: delete_file() and its private helpers.

    Inherits every AddFileMixin/ReplaceFileMixin helper and adds delete_file():
    delete a regular file by name, exactly as the OS does on KILL (source-fsprim
    KILL_ENTRY; source-sfileio1 kill_sfile; source-sfileio RELEASEPAGES;
    source-fsdir Ddelete/RealDelete/DelAdjust for the b-tree catalog):

      * the catalog entry is removed (flat: the 54-byte centry is cleared with
        the removed/emptyentry bookkeeping of KILL_ENTRY; b-tree: the record is
        deleted and the tree rebalanced by merge/rotate as in Ddelete);
      * all of the file's data pages are freed in the allocation bitmap (their
        contents and tags are left in place, exactly as the OS does);
      * the hint pages are freed and their tags cleared (fileid 0, version 0,
        dataok with 0 bytes used, relpage 0, fwd/bkwd END) as in RELEASEPAGES;
      * the slist sentry is emptied (hintaddr/fileaddr/filesize 0, version+1);
      * the MDDF is updated: filecount-1, freecount+freed pages, fs_overhead-4
        per freed b-tree node, empty_file := min(empty_file, sfile), and the
        b-tree root_page/tree_depth when the root changes.
    """

    def _hint_page_chain(self, hintaddr: int) -> List[int]:
        """List of MDDF-relative hint-page numbers, following the tag fwdlinks
        from hintaddr (END = 0xFFFFFF). The same walk RELEASEPAGES does for
        leader pages."""
        chain = []
        rel = hintaddr
        mddf = self._mddf_sector_number
        while True:
            chain.append(rel)
            if len(chain) > self._num_sectors:
                raise RuntimeError(
                    f"the hint-page chain from page {hintaddr} is longer than the whole volume; "
                    "the tag chain is corrupted (a loop?)"
                )
            tag = self.read_tags_for_sector(mddf + rel)
            fwd = (tag[0x0E] << 16) | (tag[0x0F] << 8) | tag[0x10]
            if fwd == 0xFFFFFF:
                break
            rel = fwd
        return chain

    def _file_map_page_set(
        self, hint_pages: List[int], is_btree: bool, smallmap_off: int, map_offset: int
    ):
        """The set of MDDF-relative data pages listed in the file's map
        (smallmap at smallmap_off in hint page 0 on b-tree volumes, map at
        offset 0 of hint page map_offset on flat volumes), or None if the map
        cannot be read."""
        if not hint_pages:
            return None
        mddf = self._mddf_sector_number
        if is_btree:
            if not (0x80 <= smallmap_off <= 0x100):
                return None
            absn, off = mddf + hint_pages[0], smallmap_off
        else:
            if map_offset >= len(hint_pages):
                return None
            absn, off = mddf + hint_pages[map_offset], 0
        sm = self.read_sector(absn)
        if off + 8 > len(sm):
            return None
        size, maxent, ecount = struct.unpack(">IHH", sm[off : off + 8])
        pages = set()
        for i in range(min(ecount, maxent)):
            if off + 8 + i * 6 + 6 > len(sm):
                break
            st, cnt = struct.unpack(">IH", sm[off + 8 + i * 6 : off + 14 + i * 6])
            pages.update(range(st, st + cnt))
        return pages

    def _bt_search_to_leaf(self, key: bytes):
        """Descend the b-tree root catalog to the leaf exactly as the OS Search
        does (unlike _bt_search, which stops early when the key equals an
        interior separator; Search always descends and reports the LEAF's
        found flag). Returns (leaf, index, path, found) with path =
        [(page, child_index), ...] from the root (depth 0) to the leaf.  The
        descent follows the nodes' kind bytes, NOT the MDDF tree_depth field:
        if that field is stale the real structure is still followed (and
        len(path) is the true depth, which the caller writes back to the
        MDDF)."""
        page = self._mddf_u32(0x12E)
        if page == BTREE_BAD:
            raise RuntimeError("b-tree: volume has no root catalog (root_page = BAD)")
        node = self._bt_read_node(page)
        path = []
        while True:
            if len(path) > 64:  # sanity cap: a real tree is at most a few levels deep
                raise RuntimeError(
                    "b-tree: tree structure is inconsistent (no leaf reached)"
                )
            path.append((node.page, 0))
            if node.kind == BTREE_LEAF:
                # Leaf: SearchNode examines ALL records; found => index = i.
                index = len(node.records)
                found = False
                for i, rec in enumerate(node.records):
                    c = self._bt_key_cmp(rec[0:36], key)
                    if c == 0:
                        found = True
                        index = i
                        break
                    if c > 0:
                        index = i
                        break
                path[-1] = (node.page, index)
                return node, index, path, found
            # Interior: SearchNode examines records 1..nkeys-1 (record 0 holds
            # the leftmost child and its key is the tree minimum, never
            # compared). A match at record i descends via record i's own child
            # (the key is the low key of that child's subtree); a miss at
            # position p descends via child p.
            index = None
            for i in range(1, node.nkeys):
                c = self._bt_key_cmp(node.records[i][4:40], key)
                if c == 0:
                    index = i
                    break
                if c > 0:  # key < record i's key
                    index = i - 1
                    break
            if index is None:
                index = node.nkeys - 1  # key is greater than every separator
            path[-1] = (node.page, index)
            node = self._bt_read_node(struct.unpack(">I", node.records[index][0:4])[0])

    def _bt_delete_record(
        self, leaf: _BTreeNode, leaf_idx: int, path: list, modified: set
    ):
        """Delete leaf.records[leaf_idx] from the b-tree root catalog, with the
        OS's exact rebalancing (source-fsdir Ddelete + RealDelete +
        DelAdjust): underflowed nodes merge with a sibling when the merge
        fits, otherwise one record rotates; freed nodes are zeroed and their
        pages deallocated (FreeNode); a shrunken root may lower the tree
        depth.  `path` is the [(page, child_index), ...] descent from the root
        (as returned by _bt_search_to_leaf).  All modified node pages are
        written to the in-memory image (recorded in `modified`).

        Returns (freed_node_pages, root_page, tree_depth): the MDDF-relative
        pages of the freed nodes (4 pages each) and the (possibly new) MDDF
        root_page/tree_depth values.  `tree_depth` is measured from the real
        descent (len(path)), so a stale MDDF tree_depth is corrected by the
        caller's write-back."""
        mddf = self._mddf_sector_number
        depth = len(path)  # the actual tree depth (the MDDF field may be stale)
        root_page = self._mddf_u32(0x12E)
        freed = []

        def read(page):
            return self._bt_read_node(page)

        def write(node):
            self._bt_write_node(node, modified)

        def free_node(node):
            # FreeNode: deallocate the node's pages and clear the node on disk
            # "to make the Scavenger's job easier". The pages' tags are left
            # stale, exactly as the OS does (deallocate touches only the
            # bitmap; nothing clears the pagelabels of freed tree pages).
            for i in range(BTREE_NODE_BLKS):
                absn = mddf + node.page + i
                self._mem_write_sector_data(absn, b"\x00" * SECTOR_SIZE_IN_BYTES)
                modified.add(absn)
            freed.append(node.page)

        NOOP, ROTATE, MERGE, NDEL = range(4)

        # ---- sibling selection (Ddelete) ----
        # If the node is the last child of its parent, the OS uses the prior
        # (left) sibling, else the next (right) one.
        sibling = BTREE_BAD
        right_page = BTREE_BAD
        if depth > 1:
            parent = read(path[depth - 2][0])
            pidx = path[depth - 2][1]
            if parent.nkeys > 1:
                if pidx == parent.nkeys - 1:
                    sibling = leaf.prior
                    right_page = leaf.page
                else:
                    sibling = leaf.next
                    right_page = sibling

        # ---- RealDelete at the leaf level ----
        leaf.records.pop(leaf_idx)
        write(leaf)
        half = (BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE) // 2
        if leaf.used + (leaf.nkeys + 1) * 2 < half and sibling != BTREE_BAD:
            if leaf.next == sibling:
                lft, rgt = leaf, read(sibling)  # sibling is the right node
            else:
                lft, rgt = read(sibling), leaf  # sibling is the left node
            if (
                lft.used + rgt.used + (lft.nkeys + rgt.nkeys + 1) * 2
                <= BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE
            ):
                # MergeNodes: right node's records go into the left node.
                lft.records.extend(rgt.records)
                lft.next = rgt.next
                if rgt.next != BTREE_BAD:
                    nn = read(rgt.next)  # patch backlink of new right sibling
                    nn.prior = lft.page
                    write(nn)
                write(lft)
                free_node(rgt)
                state = MERGE
                rotate_key = None
            else:
                # OneRotate: move one record across so the parent's separator
                # can be re-keyed.
                if lft is leaf:
                    lft.records.append(rgt.records[0])  # rotate right to left
                    rgt.records.pop(0)
                else:
                    rgt.records.insert(0, lft.records[-1])  # rotate left to right
                    lft.records.pop()
                write(lft)
                write(rgt)
                state = ROTATE
                rotate_key = (
                    rgt.records[0][0:36]
                    if rgt.kind == BTREE_LEAF
                    else rgt.records[0][4:40]
                )
        elif leaf.nkeys != 0:
            state = NOOP  # underflow tolerated (no sibling) or none
            rotate_key = None
        elif leaf.page == root_page:
            # The root leaf was emptied: the tree is empty.
            state = NOOP
            rotate_key = None
            root_page = BTREE_BAD
            depth = 0
        else:
            # An empty non-root node is removed and its neighbors relinked.
            if leaf.prior != BTREE_BAD:
                pn = read(leaf.prior)
                pn.next = leaf.next
                write(pn)
            if leaf.next != BTREE_BAD:
                nn = read(leaf.next)
                nn.prior = leaf.prior
                write(nn)
            free_node(leaf)
            state = NDEL
            rotate_key = None

        if state not in (MERGE, ROTATE, NDEL):
            return freed, root_page, depth

        # ---- DelAdjust: propagate the merge/rotate/delete upward ----
        level = depth  # the level of the node that just changed (the leaf)
        parent_page = path[level - 2][0]
        pidx = path[level - 2][1]
        sup = None  # a node that underflowed at the previous level
        while True:
            parent = read(parent_page)
            if sup is not None:
                # `sup` underflowed at the level below; `parent` is its parent.
                if parent.nkeys == 1:
                    if sup.nkeys != 0:
                        # No siblings and the underflowed node is not empty:
                        # the OS tolerates the underflow and stops.
                        write(sup)
                        return freed, root_page, depth
                    # No siblings and the underflowed node is EMPTY: the whole
                    # subtree under `parent` is empty. The OS frees `parent`
                    # here and leaves the empty child dangling (a latent bug);
                    # we do the same net effect without the dangling buffer:
                    # free both nodes and delete `parent`'s record from its
                    # own parent on the next pass. (Unreachable for real
                    # catalogs, which always keep the root directory records.)
                    free_node(sup)
                    free_node(parent)
                    state = NDEL
                    sup = None
                    if level == 2:
                        # `parent` was the root: the tree is empty now.
                        root_page = BTREE_BAD
                        depth = 0
                        return freed, root_page, depth
                    level -= 1
                    parent_page = path[level - 2][0]
                    pidx = path[level - 2][1]
                    continue
                # Siblings exist: merge or rotate `sup` with its sibling.
                if pidx == parent.nkeys - 1:
                    sib_page = struct.unpack(">I", parent.records[pidx - 1][0:4])[0]
                    lft, rgt = read(sib_page), sup  # sibling is the left node
                else:
                    sib_page = struct.unpack(">I", parent.records[pidx + 1][0:4])[0]
                    lft, rgt = sup, read(sib_page)  # sibling is the right node
                sibling = sib_page
                right_page = rgt.page
                if (
                    lft.used + rgt.used + (lft.nkeys + rgt.nkeys + 1) * 2
                    <= BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE
                ):
                    lft.records.extend(rgt.records)
                    # NOTE: the OS DelAdjust interior merge does NOT patch
                    # prior/next links (only the RealDelete leaf-level merge
                    # does). Interior nodes in real volumes carry stale or
                    # garbage links (left over from when the node was created
                    # as a leaf, or from AllocNode never initializing them),
                    # and copying them - or patching the backlink of the node
                    # they point at - can poison the leaf chain (a leaf's
                    # prior/next can end up pointing at an interior node).
                    write(lft)
                    free_node(rgt)
                    state = MERGE
                    rotate_key = None
                else:
                    if lft is sup:
                        lft.records.append(rgt.records[0])
                        rgt.records.pop(0)
                    else:
                        rgt.records.insert(0, lft.records[-1])
                        lft.records.pop()
                    write(lft)
                    write(rgt)
                    state = ROTATE
                    rotate_key = (
                        rgt.records[0][0:36]
                        if rgt.kind == BTREE_LEAF
                        else rgt.records[0][4:40]
                    )
                sup = None
                continue  # process `parent`'s own record next
            # Normal step: adjust `parent`'s record for the state below.
            if state != NDEL and sibling == right_page:
                pidx += 1
            if state == ROTATE:
                # Copy the right node's new low key into the parent record
                # (the child pointer is kept).
                rec = bytearray(parent.records[pidx])
                rec[4:40] = rotate_key
                parent.records[pidx] = bytes(rec)
                write(parent)
                return freed, root_page, depth
            # Merge or node deletion: drop the indexing record.
            parent.records.pop(pidx)
            if level == 2:
                # The parent is the root: shrink it while it has a single key.
                while parent.kind == BTREE_NONLEAF and parent.nkeys == 1:
                    new_root = struct.unpack(">I", parent.records[0][0:4])[0]
                    free_node(parent)
                    depth -= 1
                    root_page = new_root
                    parent = read(new_root)
                if parent.kind == BTREE_NONLEAF and parent.nkeys == 0:
                    # Defensive: an emptied root (the OS would leave it
                    # dangling; normalize to the clean empty-tree state that
                    # RealDelete produces for an emptied leaf root).
                    free_node(parent)
                    depth = 0
                    root_page = BTREE_BAD
                else:
                    write(parent)
                return freed, root_page, depth
            # DelAdjust: halfNode := ((NODE_SIZE - sizeof(NodeDesc)) div 2) -
            # sizeof(integer); the parent does not underflow iff
            # halfNode <= used + nkeys * sizeof(integer).
            if (
                BTREE_NODE_SIZE - BTREE_NODEDESC_SIZE
            ) // 2 - 2 <= parent.used + parent.nkeys * 2:
                write(parent)  # no underflow in the parent
                return freed, root_page, depth
            # Underflow: continue one level up.
            level -= 1
            sup = parent
            parent_page = path[level - 2][0]
            pidx = path[level - 2][1]

    def delete_file(self, lisa_name: str) -> "int":
        """Delete the file named lisa_name from this volume, as the OS does on
        KILL (see the class docstring).

        The file is located by name (case-insensitive): on b-tree volumes
        every catalog record is scanned (fileentry records only), on
        flat-catalog volumes the rootcatalog is probed with the same hash +
        linear probing as the OS LOOKUP_BY_ENAME. Directories and other
        non-file entries are refused.

        Returns the exit code to use: 0 on success, 3 if no file with that
        name is on the volume, and 1 on any other failure.
        """
        # ---- preconditions (same as add/replace) ----
        is_btree = not self.is_flat_catalog_volume()
        if self._fs_version not in (14, 15, 16, 17):
            print(
                f"ERROR: delete supports flat-catalog (fs_version 14/15) and b-tree (fs_version 16/17) volumes; this volume is fs_version {self._fs_version}."
            )
            return 1
        if self._single_tag_size != 20:
            print(
                "ERROR: delete currently supports only 20-byte-tag (hard disk) images."
            )
            return 1
        max_name = BTREE_MAX_NAME if is_btree else 33
        if not lisa_name:
            print("ERROR: the Lisa file name is empty.")
            return 1
        if len(lisa_name) > max_name:
            print(
                f"ERROR: Lisa file name '{lisa_name}' is {len(lisa_name)} chars; max is {max_name}."
            )
            return 1
        try:
            name_bytes = lisa_name.encode("mac-roman")
        except UnicodeEncodeError as e:
            print(f"ERROR: cannot encode '{lisa_name}' as Mac Roman: {e}")
            return 1
        if len(name_bytes) > max_name:
            print(
                f"ERROR: '{lisa_name}' is {len(name_bytes)} Mac-Roman bytes; max is {max_name}."
            )
            return 1

        mddf = self._mddf_sector_number

        # ---- MDDF fields ----
        hintsize = self._mddf_u16(0xA2)
        filecount = self._mddf_u16(0xB0)
        freecount = self._mddf_u32(0xBA)
        fs_overhead = self._mddf_u16(0x122)
        empty_file = self._mddf_u16(0x9E)
        bitmap_addr = self._mddf_u32(0x88)
        bitmap_pages = self._mddf_u16(0x92)
        slist_addr = self._mddf_u32(0x94)
        slist_packing = self._mddf_u16(0x98)
        smallmap_off = self._mddf_u16(0x114)  # b-tree: smallmap offset in hint page 0
        map_offset = self._mddf_u16(0xAC)  # flat: hint-page index of the file map

        modified = set()

        # ---- locate the file (read-only, BEFORE writing anything) ----
        leaf = None
        leaf_idx = None
        path = None
        slot = None
        if is_btree:
            file_matches = []
            dir_matches = 0
            for node, idx, rec in self._bt_scan_entries():
                name = (
                    rec[3:35].split(b"\x00", 1)[0].decode("mac-roman", errors="replace")
                )
                if name.upper() != lisa_name.upper():
                    continue
                if rec[36] == 3:  # eType high byte = fileentry
                    file_matches.append(rec)
                else:
                    dir_matches += 1  # a directory (threadentry/direntry) record
            if file_matches:
                if len(file_matches) > 1:
                    print(
                        f"ERROR: {len(file_matches)} files are named '{lisa_name}' on this volume (in "
                        "different directories); cannot tell which one to delete."
                    )
                    return 1
                key = file_matches[0][0:36]
                sfile = struct.unpack(">H", file_matches[0][38:40])[0]
                try:
                    leaf, leaf_idx, path, found = self._bt_search_to_leaf(key)
                except RuntimeError as e:
                    print(f"ERROR: {e}")
                    return 1
                if not found:
                    print(
                        "ERROR: the catalog scan and the tree search disagree; the volume's metadata is inconsistent."
                    )
                    return 1
            elif dir_matches:
                print(
                    f"ERROR: a catalog entry named '{lisa_name}' exists on this volume but it is a "
                    "directory, not a file; it cannot be deleted by this command."
                )
                return 1
            else:
                print(
                    f"WARNING: no file named '{lisa_name}' on this volume; nothing to delete."
                )
                return 3
        else:
            rc_sfile = self._find_rootcatalog_sfile()
            rc = self._slist_entry(rc_sfile)
            if rc is None or rc[1] == 0:
                print(
                    "ERROR: could not read the rootcatalog; the volume's metadata is inconsistent."
                )
                return 1
            cat_chain = self._data_page_chain(rc[1])
            cat_data = b"".join(self.read_sector(mddf + p) for p in cat_chain)
            rootmaxentries = self._mddf_u16(0xC0)
            slot = self._find_flat_file_slot(cat_data, lisa_name, rootmaxentries)
            if slot is None:
                print(
                    f"WARNING: no file named '{lisa_name}' on this volume; nothing to delete."
                )
                return 3
            centry = cat_data[slot * 54 : slot * 54 + 54]
            if centry[34] != 3:  # cetype must be fileentry
                print(
                    f"ERROR: a catalog entry named '{lisa_name}' exists on this volume but is not a "
                    f"regular file (cetype = {centry[34]}); it cannot be deleted by this command."
                )
                return 1
            sfile = struct.unpack(">H", centry[36:38])[0]

        # ---- the file's current layout (read-only) ----
        sentry = self._slist_entry(sfile)
        if sentry is None:
            print(
                f"ERROR: the slist has no entry for s-file {sfile}; the volume's metadata is inconsistent."
            )
            return 1
        hintaddr, fileaddr, filesize, sentry_version = sentry
        # kill_sfile: if (ecode > 0) or (s_addr.hintaddr <= 0) then E2_SENTRY_BAD
        # (0 = empty, negative = locked/retired; in 32-bit two's complement,
        # <= 0 means 0x80000000 and up, which includes BAD = 0xFFFFFFFF)
        if hintaddr == 0 or hintaddr >= 0x80000000:
            print(
                f"ERROR: s-file {sfile} has no hint pages; the volume's metadata is inconsistent."
            )
            return 1

        # ---- hint pages: the tag chain from hintaddr (RELEASEPAGES' list) ----
        try:
            hint_pages = self._hint_page_chain(hintaddr)
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1
        if len(hint_pages) != hintsize:
            print(
                f"NOTE: the hint chain of '{lisa_name}' has {len(hint_pages)} page(s) but the MDDF says "
                f"hintsize {hintsize}; using the chain."
            )

        # ---- data pages: the tag chain from fileaddr ----
        # (the OS frees data pages without touching their contents or tags)
        try:
            data_pages = self._data_page_chain(fileaddr) if fileaddr else []
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1
        expected = (filesize + 511) // 512
        if len(data_pages) != expected:
            print(
                f"NOTE: the tag chain of '{lisa_name}' has {len(data_pages)} page(s) but the sentry says "
                f"{filesize} bytes ({expected} page(s)); using the chain."
            )
        # cross-check against the file map, like replace does
        map_pages = self._file_map_page_set(
            hint_pages, is_btree, smallmap_off, map_offset
        )
        if map_pages is not None and set(data_pages) != map_pages:
            print(
                f"NOTE: the file map of '{lisa_name}' lists {len(map_pages)} page(s) but the tag chain has "
                f"{len(data_pages)}; freeing the chain's pages."
            )
        # a corrupted chain may repeat pages; free each page once
        seen = set()
        uniq = []
        for p in data_pages + hint_pages:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        if len(uniq) != len(data_pages) + len(hint_pages):
            print(
                "NOTE: the tag chains contain duplicate pages; each page is freed only once."
            )
        data_pages = [p for p in uniq if p in set(data_pages)]
        hint_pages = [p for p in uniq if p in set(hint_pages)]

        # ---- b-tree catalog: delete the record and rebalance (source-fsdir) ----
        freed_nodes = []
        new_root = None
        new_depth = None
        if is_btree:
            freed_nodes, new_root, new_depth = self._bt_delete_record(
                leaf, leaf_idx, path, modified
            )
        else:
            # ---- flat catalog: clear the centry (KILL_ENTRY) ----
            # cetype := removed, unless the entry following on the SAME page is
            # empty and this is not the last entry in the catalog: then
            # cetype := emptyentry (shortens probe chains). The name, sfile
            # and the read/write location fields are cleared.
            slot_off = slot * 54
            cpage_idx = slot_off // 512
            cbyte = slot_off % 512
            cetype = 7  # removed
            if (
                cbyte + 108 <= 512 and slot_off < rc[2] - 54
            ):  # next fits on page; not last
                next_centry = cat_data[(slot + 1) * 54 : (slot + 1) * 54 + 35]
                if len(next_centry) >= 35 and next_centry[34] == 0:  # emptyentry
                    cetype = 0
            c = bytearray(54)
            c[34] = cetype
            if cbyte + 54 <= 512:
                cp = bytearray(self.read_sector(mddf + cat_chain[cpage_idx]))
                cp[cbyte : cbyte + 54] = c
                self._mem_write_sector_data(mddf + cat_chain[cpage_idx], bytes(cp))
                modified.add(mddf + cat_chain[cpage_idx])
            else:
                # The entry straddles two catalog pages; split the write.
                n1 = 512 - cbyte
                cp = bytearray(self.read_sector(mddf + cat_chain[cpage_idx]))
                cp[cbyte:512] = c[:n1]
                self._mem_write_sector_data(mddf + cat_chain[cpage_idx], bytes(cp))
                modified.add(mddf + cat_chain[cpage_idx])
                cp = bytearray(self.read_sector(mddf + cat_chain[cpage_idx + 1]))
                cp[0 : 54 - n1] = c[n1:]
                self._mem_write_sector_data(mddf + cat_chain[cpage_idx + 1], bytes(cp))
                modified.add(mddf + cat_chain[cpage_idx + 1])

        # ---- hint pages: clear the tags (RELEASEPAGES, fnum < 0) ----
        # The page DATA is left in place (the OS notes "could clear the data
        # part of the page here, if desired" and doesn't); only the pagelabel
        # is reset: fileid 0, version 0, dataok with 0 bytes used, relpage 0,
        # fwd/bkwd END. volume and abspage are untouched.
        for p in hint_pages:
            absn = mddf + p
            t = bytearray(self.read_tags_for_sector(absn))
            t[0:2] = b"\x00\x00"  # version := 0
            t[4:6] = b"\x00\x00"  # fileid := 0
            t[6:8] = b"\x80\x00"  # datastat := dataok, dataused := 0
            t[12:14] = b"\x00\x00"  # relpage := 0
            t[14:17] = b"\xff\xff\xff"  # fwdlink := END
            t[17:20] = b"\xff\xff\xff"  # bkwdlink := END
            self._mem_write_sector_tag(absn, bytes(t))
            modified.add(absn)

        # ---- file map: truncate the first map section (FMAP_MGR FMRELEASE) ----
        # When the file's data pages are released, the OS writes the first map
        # section back with size := 0, ecount := 0 and the released entries
        # cleared (address := 0, cpages := 0). Later map sections (large files)
        # are left in place; their pages are released with the hint pages and
        # their tags are cleared above, exactly as in the OS.
        if data_pages:
            if is_btree:
                map_abs, map_off = (
                    (mddf + hint_pages[0], smallmap_off)
                    if hint_pages and 0x80 <= smallmap_off <= 0x100
                    else (None, 0)
                )
            else:
                map_abs, map_off = (
                    (mddf + hint_pages[map_offset], 0)
                    if map_offset < len(hint_pages)
                    else (None, 0)
                )
            if map_abs is not None:
                ms = bytearray(self.read_sector(map_abs))
                if map_off + 8 <= len(ms):
                    msize, mmaxent, mecount = struct.unpack(
                        ">IHH", ms[map_off : map_off + 8]
                    )
                    if msize > 0:
                        struct.pack_into(">I", ms, map_off, 0)  # size := 0
                        struct.pack_into(">H", ms, map_off + 6, 0)  # ecount := 0
                        nent = min(mmaxent, (len(ms) - map_off - 8) // 6)
                        for i in range(nent):
                            ms[map_off + 8 + i * 6 : map_off + 14 + i * 6] = b"\x00" * 6
                        self._mem_write_sector_data(map_abs, bytes(ms))
                        modified.add(map_abs)

        # ---- slist sentry: empty it (kill_sfile) ----
        spage = sfile // slist_packing
        soffs = (sfile % slist_packing) * 14
        slist_abs = mddf + slist_addr + spage
        sl = bytearray(self.read_sector(slist_abs))
        struct.pack_into(">I", sl, soffs, 0)  # hintaddr := 0
        struct.pack_into(">I", sl, soffs + 4, 0)  # fileaddr := 0
        struct.pack_into(">I", sl, soffs + 8, 0)  # filesize := 0
        new_version = (sentry_version + 1) & 0xFFFF
        struct.pack_into(">H", sl, soffs + 12, new_version)  # version := version + 1
        if (
            new_version == 0x7FFF
        ):  # if version = maxint then hintaddr := -1 (retire the slot)
            struct.pack_into(">I", sl, soffs, 0xFFFFFFFF)
        self._mem_write_sector_data(slist_abs, bytes(sl))
        modified.add(slist_abs)

        # ---- MDDF: filecount, freecount, fs_overhead, empty_file, root/depth ----
        # A page that somehow appears in both tag chains is freed once.
        freed_total = len(set(data_pages) | set(hint_pages)) + BTREE_NODE_BLKS * len(
            freed_nodes
        )
        m = bytearray(self.read_sector(mddf))
        struct.pack_into(
            ">H", m, 0xB0, (filecount - 1) & 0xFFFF
        )  # filecount := filecount - 1
        struct.pack_into(
            ">I", m, 0xBA, (freecount + freed_total) & 0xFFFFFFFF
        )  # freecount += freed
        if is_btree:
            if freed_nodes:
                struct.pack_into(
                    ">H",
                    m,
                    0x122,
                    (fs_overhead - BTREE_NODE_BLKS * len(freed_nodes)) & 0xFFFF,
                )
            if new_root != self._mddf_u32(0x12E) or new_depth != self._mddf_u16(0x132):
                if new_depth != self._mddf_u16(0x132):
                    print(
                        f"WARNING: MDDF tree_depth was {self._mddf_u16(0x132)} but the tree actually has depth {new_depth}; correcting the MDDF."
                    )
                struct.pack_into(">I", m, 0x12E, new_root)  # root_page
                struct.pack_into(">H", m, 0x132, new_depth)  # tree_depth
        if sfile < empty_file:
            struct.pack_into(
                ">H", m, 0x9E, sfile
            )  # empty_file := min(empty_file, sfile)
        self._mem_write_sector_data(mddf, bytes(m))
        modified.add(mddf)

        # ---- allocation bitmap: clear the freed pages ----
        all_freed = set(data_pages) | set(hint_pages)
        for p in freed_nodes:
            all_freed.update(p + i for i in range(BTREE_NODE_BLKS))
        for i in range(bitmap_pages):
            bb = bytearray(self.read_sector(mddf + bitmap_addr + i))
            changed = False
            for p in all_freed:
                if p // 4096 == i:
                    bit = p % 4096
                    if (bb[bit // 8] >> (bit % 8)) & 1:
                        bb[bit // 8] &= ~(1 << (bit % 8))
                        changed = True
            if changed:
                self._mem_write_sector_data(mddf + bitmap_addr + i, bytes(bb))
                modified.add(mddf + bitmap_addr + i)

        # ---- flush modified sectors to disk, patching tag checksums in one pass ----
        with open(self._file_name, "r+b") as fw:
            for sn in sorted(modified):
                d = self.read_sector(sn)
                tg = self.read_tags_for_sector(sn)
                ck = self.calculate_new_tag_checksum(sn)
                tg = tg[:11] + bytes([ck]) + tg[12:]
                self._file.seek(self._sector_tag_file_offset(sn) + 11)
                self._file.write(bytes([ck]))  # keep the in-memory copy in sync
                fw.seek(self._sector_data_file_offset(sn))
                fw.write(d)
                fw.seek(self._sector_tag_file_offset(sn))
                fw.write(tg)

        self.fix_dc42_checksum(confirm=False)

        print(
            f"Deleted '{lisa_name}' (s-file {sfile}): {filesize} byte(s); freed {len(data_pages)} data "
            f"page(s) and {len(hint_pages)} hint page(s)."
        )
        if freed_nodes:
            print(
                f"  freed {len(freed_nodes)} b-tree catalog node(s) ({BTREE_NODE_BLKS} page(s) each); "
                f"the tree depth is now {new_depth}."
            )
        return 0


class FileSystemWithAddFile(InMemoryFileSystem, DeleteFileMixin):
    """InMemoryFileSystem plus the add (add_file), replace (replace_file) and
    delete (delete_file) capabilities.

    DeleteFileMixin already extends ReplaceFileMixin (which extends
    AddFileMixin), so all three commands are available.
    """

    pass


# Example usage:
if __name__ == "__main__":
    command = sys.argv[1].strip().lower() if len(sys.argv) >= 2 else ""

    if command not in ("add", "replace", "delete"):
        print(
            "Usage: python LisaFileSystemToolPerFile.py <command> <disk image file name> [host file] <lisa file name>"
        )
        print("       <command> is 'add', 'replace', or 'delete'.")
        print(
            "This tool implements the 'add', 'replace', and 'delete' commands; use LisaFileSystemTool.py for the other commands."
        )
        print(
            "Exit codes: 0 = success; 3 = add: a file with that name already exists (nothing was added) / "
            "replace or delete: no file with that name exists on the volume (nothing was changed); "
            "1 = any other failure."
        )
        sys.exit(1)

    if command == "delete":
        if len(sys.argv) != 4:
            print(
                "Usage: python LisaFileSystemToolPerFile.py delete <disk image file name> <lisa file name>"
            )
            sys.exit(1)
        disk_image_file_name = sys.argv[2]
        lisa_os_file_name = sys.argv[3]
    else:
        if len(sys.argv) != 5:
            print(
                f"Usage: python LisaFileSystemToolPerFile.py {command} <disk image file name> <host file> <lisa file name>"
            )
            sys.exit(1)
        disk_image_file_name = sys.argv[2]
        host_file_name = sys.argv[3]
        lisa_os_file_name = sys.argv[4]

    # The top level is the right place to turn a failed construction (FileNotFoundError,
    # ValueError, ...) into a one-line error message and a non-zero exit status.
    # FileSystem.__init__ itself just raises, so the class stays usable from other code.
    try:
        file_system = FileSystemWithAddFile(disk_image_file_name)
    except FileNotFoundError:
        print(f"File {disk_image_file_name} not found!")
        sys.exit(1)
    except IOError as e:
        print(f"IO Error: '{e}' while reading file {disk_image_file_name} !")
        sys.exit(1)
    except Exception as e:
        print(
            f"ERROR: {type(e).__name__}: {e} while reading file {disk_image_file_name}! Exiting."
        )
        sys.exit(1)

    # Every command method returns the exit code to use directly:
    # 0 = success, 3 = no action was needed (add: a file with that name
    # already exists / replace and delete: no file with that name on the
    # volume — so callers re-running an upload list can count "no action
    # needed" separately from success and from a real failure),
    # 1 = any other failure.
    if command == "add":
        sys.exit(file_system.add_file(host_file_name, lisa_os_file_name))
    elif command == "replace":
        sys.exit(file_system.replace_file(host_file_name, lisa_os_file_name))
    else:
        sys.exit(file_system.delete_file(lisa_os_file_name))
    # That's all, Folks!
