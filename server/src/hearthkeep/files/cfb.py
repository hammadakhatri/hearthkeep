"""The entry names of a compound file (the container of .xls and encrypted Office files).

Only enough of the format ([MS-CFB]) is read to list the directory: the header, the FAT
sectors named by the DIFAT (the header's 109 entries, then the DIFAT sector chain), and the
directory's own FAT chain. Every offset
and chain is checked; anything malformed raises ValueError."""

from __future__ import annotations

import struct
from pathlib import Path
from typing import BinaryIO

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
HEADER_BYTES = 512
HEADER_DIFAT_ENTRIES = 109
DIR_ENTRY_BYTES = 128
MAX_DIR_ENTRIES = 4096
MAX_DIFAT_SECTORS = 1000
# Special sector numbers: the end of a chain and unused entries (MAXREGSECT is 0xFFFFFFFA).
END_OF_CHAIN = 0xFFFFFFFE
FREE_SECT = 0xFFFFFFFF
MAX_REG_SECT = 0xFFFFFFFA
# Directory entry object types: unused, storage, stream, root storage.
_STORAGE, _STREAM = 1, 2


def _read_exact(fh: BinaryIO, offset: int, size: int) -> bytes:
    fh.seek(offset)
    data = fh.read(size)
    if len(data) != size:
        raise ValueError("compound file is truncated")
    return data


def cfb_stream_names(path: Path) -> set[str]:
    """The names of every storage and stream in the compound file's directory."""
    with open(path, "rb") as fh:
        header = fh.read(HEADER_BYTES)
        if len(header) != HEADER_BYTES or not header.startswith(MAGIC):
            raise ValueError("not a compound file")
        major, byte_order, sector_shift = struct.unpack_from("<HHH", header, 26)
        if byte_order != 0xFFFE:
            raise ValueError("bad byte order mark")
        if (major, sector_shift) not in ((3, 9), (4, 12)):
            raise ValueError("unsupported sector size")
        sector_size = 1 << sector_shift
        fat_sector_count, first_dir_sector = struct.unpack_from("<II", header, 44)
        first_difat_sector, difat_sector_count = struct.unpack_from("<II", header, 68)
        if difat_sector_count > MAX_DIFAT_SECTORS:
            raise ValueError("too many DIFAT sectors")
        per_fat_sector = sector_size // 4
        fh.seek(0, 2)
        sector_count = (fh.tell() - sector_size) // sector_size if fh.tell() > sector_size else 0

        def sector_offset(sector: int) -> int:
            if sector > MAX_REG_SECT or sector >= sector_count:
                raise ValueError("sector out of range")
            return (sector + 1) * sector_size

        # The FAT sector list: 109 entries in the header, then each DIFAT sector's entries
        # (all but its last, which points at the next DIFAT sector).
        difat = list(struct.unpack_from(f"<{HEADER_DIFAT_ENTRIES}I", header, 76))
        seen_difat: set[int] = set()
        sector = first_difat_sector
        while sector not in (END_OF_CHAIN, FREE_SECT):
            if sector in seen_difat:
                raise ValueError("DIFAT chain loops")
            if len(seen_difat) >= min(difat_sector_count, MAX_DIFAT_SECTORS):
                raise ValueError("DIFAT chain is longer than its count")
            seen_difat.add(sector)
            raw = struct.unpack(
                f"<{per_fat_sector}I", _read_exact(fh, sector_offset(sector), sector_size)
            )
            difat.extend(raw[:-1])
            sector = raw[-1]

        fat_cache: dict[int, tuple[int, ...]] = {}

        def next_sector(sector: int) -> int:
            index = sector // per_fat_sector
            if index >= len(difat) or index >= fat_sector_count:
                raise ValueError("FAT sector out of range")
            if index not in fat_cache:
                raw = _read_exact(fh, sector_offset(difat[index]), sector_size)
                fat_cache[index] = struct.unpack(f"<{per_fat_sector}I", raw)
            return fat_cache[index][sector % per_fat_sector]

        per_dir_sector = sector_size // DIR_ENTRY_BYTES
        max_dir_sectors = -(-MAX_DIR_ENTRIES // per_dir_sector)
        names: set[str] = set()
        seen: set[int] = set()
        sector = first_dir_sector
        while sector != END_OF_CHAIN:
            if sector in seen:
                raise ValueError("directory chain loops")
            if len(seen) >= max_dir_sectors:
                raise ValueError("directory has too many entries")
            seen.add(sector)
            raw = _read_exact(fh, sector_offset(sector), sector_size)
            for i in range(per_dir_sector):
                entry = raw[i * DIR_ENTRY_BYTES : (i + 1) * DIR_ENTRY_BYTES]
                name_length, kind = struct.unpack_from("<HB", entry, 64)
                if kind not in (_STORAGE, _STREAM):
                    continue
                if name_length < 2 or name_length > 64 or name_length % 2:
                    raise ValueError("bad directory entry name")
                try:
                    name = entry[: name_length - 2].decode("utf-16-le")
                except UnicodeDecodeError:
                    raise ValueError("bad directory entry name") from None
                names.add(name)
            sector = next_sector(sector)
        if not seen:
            raise ValueError("empty directory")
        return names
