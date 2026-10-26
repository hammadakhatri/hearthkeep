"""Chat files: deciding a file's kind from its content, and the compound-file reader."""

import io
import json
import struct
import zipfile

import pytest

from hearthkeep.files import checks
from hearthkeep.files.cfb import cfb_stream_names
from hearthkeep.files.checks import (
    MIME,
    FileCheck,
    FileRefused,
    check_file,
    clean_file_name,
)

PASSWORD = "This file is password-protected. Remove the password and attach it again."
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + bytes(40)

# ---- test files ---------------------------------------------------------------------------------

CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREE, END, FAT_SECTOR = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD


def _dir_entry(name: str, kind: int, start: int = END, size: int = 0) -> bytes:
    raw = (name + "\x00").encode("utf-16-le") if name else b""
    return (
        raw.ljust(64, b"\x00")
        + struct.pack("<HBB", len(raw), kind, 1)
        + struct.pack("<III", FREE, FREE, FREE)  # left, right, child
        + bytes(16 + 4 + 16)  # CLSID, state bits, times
        + struct.pack("<IQ", start, size)
    )


def make_cfb(streams: dict[str, bytes], storages: tuple[str, ...] = ()) -> bytes:
    """A small version-3 compound file (512-byte sectors): sector 0 is the FAT, then the
    directory, then each stream's data in whole sectors (the mini stream is not used)."""
    entries = [_dir_entry("Root Entry", 5)]
    entries += [_dir_entry(name, 1) for name in storages]
    data_sectors: list[bytes] = []
    dir_sectors = -(-(len(entries) + len(streams)) * 128 // 512)
    next_sector = 1 + dir_sectors
    chains: list[tuple[int, int]] = []
    for name, data in streams.items():
        count = max(1, -(-len(data) // 512))
        entries.append(_dir_entry(name, 2, next_sector, len(data)))
        chains.append((next_sector, count))
        data_sectors += [data[i * 512 : (i + 1) * 512].ljust(512, b"\x00") for i in range(count)]
        next_sector += count
    directory = b"".join(entries)
    directory = directory.ljust(dir_sectors * 512, b"\x00")
    fat = [FAT_SECTOR]
    fat += [*range(2, 1 + dir_sectors), END]
    for start, count in chains:
        fat += [*range(start + 1, start + count), END]
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat).ljust(512, b"\xff")
    difat = [0] + [FREE] * 108
    header = (
        CFB_MAGIC + bytes(16) + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + bytes(6)
        + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, END, 0, END, 0)
        + b"".join(struct.pack("<I", v) for v in difat)
    )  # fmt: skip
    assert len(header) == 512
    return header + fat_bytes + directory + b"".join(data_sectors)


def make_xlsx(extra: dict[str, bytes] | None = None, content_types: bytes | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            content_types
            or b'<Types><Override ContentType="application/vnd.openxmlformats-officedocument.'
            b'spreadsheetml.sheet.main+xml"/></Types>',
        )
        zf.writestr("xl/workbook.xml", b"<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", b"<worksheet/>")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buffer.getvalue()


def make_pdf() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def write(tmp_path, name: str, data: bytes):
    path = tmp_path / "staged.tmp"
    path.write_bytes(data)
    return path


def refused(tmp_path, name: str, data: bytes) -> FileRefused:
    with pytest.raises(FileRefused) as caught:
        check_file(write(tmp_path, name, data), name)
    return caught.value


# ---- detection ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "kind"),
    [
        ("sales.csv", b"date,amount\n2024-03-01,12.50\n", "csv"),
        ("notes.txt", b"Grocery list\nmilk\n", "txt"),
        ("data.json", b'{"a": [1, 2, 3]}', "json"),
        ("budget.xlsx", make_xlsx(), "xlsx"),
        ("old.xls", make_cfb({"Workbook": b"\x09\x08" + bytes(600)}), "xls"),
        ("older.xls", make_cfb({"Book": b"\x09\x08" + bytes(20)}), "xls"),
        ("statement.pdf", make_pdf(), "pdf"),
        ("export", b'[{"x": 1}]', "json"),  # no extension, parses as JSON
        ("export.dat", b"a;b\n1;2\n", "csv"),  # unknown extension, not JSON
        ("utf16.csv", "﻿a,b\n1,2\n".encode("utf-16-le"), "csv"),
        ("latin.csv", "Caf\xe9,Pr\xeas\n1,2\n".encode("cp1252"), "csv"),
    ],
)
def test_detects_each_kind_by_content(tmp_path, name, data, kind):
    found = check_file(write(tmp_path, name, data), name)
    assert found == FileCheck(kind=kind, mime=MIME[kind], ext=kind)


def test_mime_and_kinds_are_exact():
    assert checks.FILE_KINDS == ("csv", "xlsx", "xls", "pdf", "txt", "json")
    assert MIME == {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xls": "application/vnd.ms-excel",
        "pdf": "application/pdf",
        "txt": "text/plain",
        "json": "application/json",
    }
    assert checks.MAX_FILE_BYTES == 50 * 1024 * 1024
    assert checks.MAX_FILES_PER_MESSAGE == 5
    assert checks.MAX_XLSX_UNPACKED == 1024**3


def test_claimed_xlsx_that_is_not_refused(tmp_path):
    error = refused(tmp_path, "budget.xlsx", b"date,amount\n2024-03-01,12.50\n")
    assert error.reason == "mismatch"
    assert error.message == (
        "That file's name says .xlsx, but its contents aren't. Check the file and attach it again."
    )
    # Binary kinds must carry their own extension too.
    assert refused(tmp_path, "budget.csv", make_xlsx()).reason == "mismatch"
    assert refused(tmp_path, "scan.txt", make_pdf()).reason == "mismatch"
    assert refused(tmp_path, "old.xlsx", make_cfb({"Workbook": b"x"})).reason == "mismatch"
    assert refused(tmp_path, "statement.pdf", b"just text").reason == "mismatch"


def test_xlsm_and_macro_files_refused(tmp_path):
    message = (
        "Excel files with macros can't be attached. Save it as .xlsx without macros and attach "
        "it again."
    )
    cases = [
        ("budget.xlsm", make_xlsx()),
        ("budget.xlsx", make_xlsx({"xl/vbaProject.bin": b"vba"})),
        ("budget.xlsx", make_xlsx(content_types=b"<Types>macroEnabled.main+xml</Types>")),
        ("old.xls", make_cfb({"Workbook": b"x"}, storages=("_VBA_PROJECT_CUR",))),
    ]
    for name, data in cases:
        error = refused(tmp_path, name, data)
        assert (error.reason, error.message) == ("macros", message), name


def test_password_protected_office_file_refused_with_message(tmp_path):
    data = make_cfb({"EncryptionInfo": b"info", "EncryptedPackage": b"secret"})
    error = refused(tmp_path, "budget.xlsx", data)
    assert (error.reason, error.message) == ("password_protected", PASSWORD)


def test_xlsx_over_unpacked_cap_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "MAX_XLSX_UNPACKED", 1024 * 1024)
    data = make_xlsx({"xl/worksheets/sheet2.xml": b"<c>1</c>" * (2 * 1024 * 1024 // 8)})
    error = refused(tmp_path, "big.xlsx", data)
    assert error.reason == "too_large_to_open"
    assert error.message == "That spreadsheet is too large to open (over 1 GB once unpacked)."
    assert check_file(write(tmp_path, "small.xlsx", make_xlsx()), "small.xlsx").kind == "xlsx"


def test_csv_with_bom_and_semicolons_is_accepted(tmp_path):
    data = b"\xef\xbb\xbfDate;Amount\n12/03/2024;1.234,50\n"
    assert check_file(write(tmp_path, "march.csv", data), "march.csv").kind == "csv"


def test_invalid_json_refused(tmp_path):
    for data in (b'{"a": 1,', b"", b"\xef\xbb\xbf[1, 2"):
        error = refused(tmp_path, "data.json", data)
        assert error.reason == "invalid_json"
        assert error.message == "That JSON file can't be read: it isn't valid JSON."
    # A deeply nested document is refused, not a crash.
    assert refused(tmp_path, "deep.json", b"[" * 200000).reason == "invalid_json"
    assert check_file(write(tmp_path, "bom.json", b'\xef\xbb\xbf{"a": 1}'), "bom.json").kind == (
        "json"
    )


def test_binary_junk_refused(tmp_path):
    error = refused(tmp_path, "photo.csv", PNG)
    assert error.reason == "unsupported"
    assert error.message == (
        "Hearthkeep can't read that file. Attach a CSV, Excel (.xlsx or .xls), PDF, text or "
        "JSON file."
    )
    # Any other zip (a Word document, say) and any other compound file are unsupported.
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as zf:
        zf.writestr("word/document.xml", b"<w/>")
    assert refused(tmp_path, "letter.csv", docx.getvalue()).reason == "unsupported"
    assert refused(tmp_path, "old.xls", make_cfb({"WordDocument": b"x"})).reason == "unsupported"
    # Bytes that are not text in any encoding tried.
    assert refused(tmp_path, "x.csv", b"\x81\x8d\x8f\x90\x9d" * 10).reason == "unsupported"


def test_damaged_files_refused(tmp_path):
    message = "Hearthkeep couldn't open that file. It may be damaged."
    no_end = b"%PDF-1.7\n" + b"x" * 1000
    cases = [
        ("cut.pdf", no_end),
        ("cut.xlsx", make_xlsx()[:200]),
        ("cut.xls", make_cfb({"Workbook": b"x"})[:700]),
    ]
    for name, data in cases:
        error = refused(tmp_path, name, data)
        assert (error.reason, error.message) == ("damaged", message), name


def test_pdf_end_marker_must_be_near_the_end(tmp_path):
    early_end = make_pdf() + b"x" * (70 * 1024)
    assert refused(tmp_path, "a.pdf", early_end).reason == "damaged"
    padded = b"junk" * 10 + make_pdf()  # the header may sit a little way in
    assert check_file(write(tmp_path, "a.pdf", padded), "a.pdf").kind == "pdf"


# ---- names --------------------------------------------------------------------------------------


def test_clean_file_name_strips_paths_and_controls():
    assert clean_file_name("..\\..\\a\x00b.csv", "csv") == "ab.csv"
    assert clean_file_name("", "csv") == "file.csv"
    assert clean_file_name(None, "pdf") == "file.pdf"
    assert clean_file_name("/etc/passwd/", "txt") == "file.txt"
    assert clean_file_name('re:port*?"<>|.csv', "csv") == "report.csv"
    long = clean_file_name("x" * 300 + ".csv", "csv")
    assert len(long) <= 120 and long.endswith(".csv")
    # NFC: a decomposed "ä" becomes the single character.
    assert clean_file_name("märz.csv", "csv") == "märz.csv"


# ---- compound files -----------------------------------------------------------------------------


def test_cfb_reads_entry_names(tmp_path):
    path = tmp_path / "a.xls"
    path.write_bytes(make_cfb({"Workbook": b"x" * 1500, "SummaryInformation": b"s"}))
    assert {"Workbook", "SummaryInformation"} <= cfb_stream_names(path)


def test_cfb_malformed_raises_value_error(tmp_path):
    good = make_cfb({"Workbook": b"x"})
    looped_fat = bytearray(good)
    struct.pack_into("<I", looped_fat, 512 + 4, 1)  # directory sector 1 points at itself
    bad_shift = bytearray(good)
    struct.pack_into("<H", bad_shift, 30, 15)
    bad_dir_start = bytearray(good)
    struct.pack_into("<I", bad_dir_start, 48, 9999)
    bad_name_length = bytearray(good)
    struct.pack_into("<H", bad_name_length, 1024 + 128 + 64, 90)
    cases = [
        b"",
        good[:100],
        b"\x00" * 8 + good[8:],
        bytes(looped_fat),
        bytes(bad_shift),
        bytes(bad_dir_start),
        bytes(bad_name_length),
    ]
    for i, data in enumerate(cases):
        path = tmp_path / f"bad{i}.xls"
        path.write_bytes(data)
        with pytest.raises(ValueError):
            cfb_stream_names(path)


def test_cfb_directory_is_bounded(tmp_path, monkeypatch):
    # A directory chain longer than 4096 entries is refused rather than followed.
    from hearthkeep.files import cfb

    monkeypatch.setattr(cfb, "MAX_DIR_ENTRIES", 4)
    path = tmp_path / "many.xls"
    path.write_bytes(make_cfb({f"s{i}": b"x" for i in range(6)}))
    with pytest.raises(ValueError):
        cfb_stream_names(path)


def test_file_refused_carries_reason_and_message():
    error = FileRefused("damaged")
    assert error.reason == "damaged" and error.message.startswith("Hearthkeep couldn't open")
    assert json.dumps({"reason": error.reason, "message": error.message})


# ---- fix round 1 --------------------------------------------------------------------------------

DIFAT_SECTOR = 0xFFFFFFFC


def make_far_cfb(difat_next: int = END) -> bytes:
    """A version-3 compound file whose directory sits past what the header's 109 FAT sectors
    cover: 111 FAT sectors (0..110), a DIFAT sector (111) naming FAT sectors 109 and 110,
    filler, then the directory at sector 14100 (FAT index 110)."""
    fat_count, difat_at, dir_at = 111, 111, 14100
    fat = [FAT_SECTOR] * fat_count + [DIFAT_SECTOR] + [FREE] * (dir_at - difat_at - 1) + [END]
    fat += [FREE] * (fat_count * 128 - len(fat))
    fat_bytes = b"".join(struct.pack("<I", v) for v in fat)
    difat_sector = struct.pack("<II", 109, 110) + struct.pack("<I", FREE) * 125
    difat_sector += struct.pack("<I", difat_next)
    directory = (_dir_entry("Root Entry", 5) + _dir_entry("Workbook", 2)).ljust(512, b"\x00")
    header = (
        CFB_MAGIC + bytes(16) + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + bytes(6)
        + struct.pack("<IIIIIIIII", 0, fat_count, dir_at, 0, 4096, END, 0, difat_at, 1)
        + b"".join(struct.pack("<I", v) for v in range(109))
    )  # fmt: skip
    filler = bytes(512 * (dir_at - difat_at - 1))
    return header + fat_bytes + difat_sector + filler + directory


def test_cfb_follows_the_difat_chain(tmp_path):
    path = tmp_path / "big.xls"
    path.write_bytes(make_far_cfb())
    assert "Workbook" in cfb_stream_names(path)
    assert check_file(path, "big.xls").kind == "xls"


def test_cfb_difat_loop_and_count_are_refused(tmp_path):
    path = tmp_path / "loop.xls"
    path.write_bytes(make_far_cfb(difat_next=111))  # the DIFAT sector points at itself
    with pytest.raises(ValueError):
        cfb_stream_names(path)
    too_many = bytearray(make_cfb({"Workbook": b"x"}))
    struct.pack_into("<II", too_many, 68, 1, 1001)
    path.write_bytes(bytes(too_many))
    with pytest.raises(ValueError):
        cfb_stream_names(path)


def test_file_without_extension_takes_its_content_kind(tmp_path):
    assert check_file(write(tmp_path, "statement", make_pdf()), "statement").kind == "pdf"
    assert check_file(write(tmp_path, "budget", make_xlsx()), "budget").kind == "xlsx"
    assert check_file(write(tmp_path, "old", make_cfb({"Workbook": b"x"})), "old").kind == "xls"


def _zip_entry_data_offset(data: bytes) -> int:
    name_length, extra_length = struct.unpack_from("<HH", data, 26)
    return 30 + name_length + extra_length


def test_hostile_zips_refused(tmp_path):
    # The first entry is [Content_Types].xml (deflated): a corrupt deflate stream is damaged.
    good = make_xlsx(content_types=b"<Types>" + b"x" * 400 + b"</Types>")
    corrupt = bytearray(good)
    corrupt[_zip_entry_data_offset(good)] = 0xFF  # reserved block type
    error = refused(tmp_path, "bad.xlsx", bytes(corrupt))
    assert error.reason == "damaged"
    # An encrypted entry (flag bit 0, in the local and the central header) asks for a password.
    encrypted = bytearray(good)
    encrypted[6] |= 1
    central = encrypted.find(b"PK\x01\x02")
    encrypted[central + 8] |= 1
    error = refused(tmp_path, "locked.xlsx", bytes(encrypted))
    assert (error.reason, error.message) == ("password_protected", PASSWORD)


def test_zip_with_too_many_entries_refused(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("xl/workbook.xml", b"<workbook/>")
        for i in range(10_000):
            zf.writestr(f"xl/e{i}", b"")
    assert refused(tmp_path, "many.xlsx", buffer.getvalue()).reason == "too_large_to_open"


def test_macro_detection_ignores_case(tmp_path):
    cases = [
        make_xlsx({"XL/VBAPROJECT.BIN": b"vba"}),
        make_xlsx(content_types=b"<Types>MACROENABLED</Types>"),
        make_xlsx(content_types=b'<Types><Default ContentType="application/vnd.ms-office.'
                  b'vbaProject"/></Types>'),
    ]  # fmt: skip
    for data in cases:
        assert refused(tmp_path, "m.xlsx", data).reason == "macros"


def test_large_json_is_checked_without_parsing(tmp_path, monkeypatch):
    def no_parse(*args, **kwargs):
        raise AssertionError("json.loads on a large file")

    monkeypatch.setattr(checks.json, "loads", no_parse)
    big = b"  [" + b"1," * (3 * 1024 * 1024) + b"1]"
    assert check_file(write(tmp_path, "big.json", big), "big.json").kind == "json"
    assert check_file(write(tmp_path, "big", big), "big").kind == "json"
    not_json = b"hello " * (1024 * 1024)
    assert refused(tmp_path, "big.json", not_json).reason == "invalid_json"


def test_names_take_the_detected_extension():
    assert clean_file_name("run.hta", "csv") == "run.csv"
    assert clean_file_name("statement", "pdf") == "statement.pdf"
    assert clean_file_name("Notes.TXT", "txt") == "Notes.TXT"
    assert clean_file_name("export.dat", "csv") == "export.csv"
    long = clean_file_name("y" * 300, "xlsx")
    assert len(long) <= 120 and long.endswith(".xlsx")


def test_names_lose_bidi_and_format_characters():
    hidden = "​‌‍‎‏‪‫‬‭‮  "
    hidden += "⁦⁧⁨⁩﻿"
    assert clean_file_name(f"inv{hidden}oice.csv", "csv") == "invoice.csv"
    # "report‮xcs.pdf" would show as "reportfdp.scx": the override is removed.
    assert clean_file_name("report‮xcs.pdf", "pdf") == "reportxcs.pdf"


def test_mismatch_message_shows_a_clean_short_extension(tmp_path):
    error = refused(tmp_path, "scan.<b>PDFX‮overlong!", make_pdf())
    assert error.reason == "mismatch"
    assert error.message == (
        "That file's name says .bPDFXoverl, but its contents aren't. Check the file and attach "
        "it again."
    )
