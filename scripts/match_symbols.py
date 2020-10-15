#!/usr/bin/env python3

import argparse
from bisect import bisect_left, bisect_right
from io import BytesIO
import re
from pathlib import Path
from elftools.common.exceptions import ELFError
from elftools.elf.elffile import ELFFile
from elftools.elf.sections import SymbolTableSection
from elftools.elf.relocation import RelocationSection
import subprocess

parser = argparse.ArgumentParser(
    prog="match_symbols.py",
    description="Find symbols from static lib in memory dump."
)
parser.add_argument("haystack", metavar="HAYSTACK", type=str, help="Memdump to search")
parser.add_argument(
    "needles",
    metavar="NEEDLE",
    type=str,
    nargs="+",
    help="Static libraries containing objects to find",
)
parser.add_argument(
    "--no-reloc",
    type=bool,
    default=False,
    help="Disable reloc support and do only exact matches",
)
parser.add_argument(
    "--min_match", type=int, default=24, help="Minimum match size (ignore smaller)"
)
parser.add_argument("--match", type=int, default=32, help="Bytes to match")
parser.add_argument(
    "--haystack_base",
    type=lambda x: int(x, 0),
    default=0x80000000,
    help="Haystack base address",
)
parser.add_argument(
    "--haystack_size",
    type=lambda x: int(x, 0),
    default=0x88F400,
    help="Haystack max size",
)
args = parser.parse_args()


# RelocationMap is a sorted dict of relocation entries supporting efficient range lookups.
# https://code.activestate.com/recipes/577197-sortedcollection/
class RelocationMap(object):
    def __init__(self, iterable=()):
        self._items = [*iterable]
        self._keys = [rela["r_offset"] for rela in self._items]

    def slice_range(self, base, size):
        if size <= 0:
            return []
        # take the first item after base.
        i = bisect_left(self._keys, base)
        if not i or i >= len(self._keys):
            return []
        # take the first item PAST the right boundary.
        j = bisect_left(self._keys, base + size)
        if not j or j >= len(self._keys):
            return []
        # ignore if no overlap
        if i > j:
            return []
        return self._items[i:j]


def split_each(w, n):
    for i in range(0, len(w), n):
        yield w[i : i + n]


def hexdump(buf):
    return " ".join(split_each(buf.hex(), 8))


with open(args.haystack, "rb") as f:
    haystack = f.read()

if len(haystack) > args.haystack_size:
    haystack = haystack[: args.haystack_size]

print(f"haystack_base = 0x{'%08x' % args.haystack_base}")
print(f"len(haystack) = 0x{'%08x' % len(haystack)}")
print(f"min_match = {args.min_match}")
print(f"match = {args.match}")


def match_symbol_static(haystack, sym, text, strtab):
    sym_type = sym.entry["st_info"]["type"]
    sym_name = strtab.get_string(sym["st_name"])
    if len(sym_name) == 0:
        return
    if sym_type == "STT_FUNC":
        # Get section in .text referenced by symbol.
        func_value_ptr = sym["st_value"]
        func_value_size = sym["st_size"]
        if func_value_size < args.min_match:
            return
        sym_value = text[func_value_ptr : func_value_ptr + func_value_size]
        # Formulate a regex string.
        regex = b""
        for i, mask_bit in enumerate(mask):
            if mask_bit == 0:
                regex += b"\\x%02x" % sym_value[i]
            else:
                regex += b"."
        # Seach for symbol.
        print(f"[~] Searching for {sym_name}")
        needle = sym_value
        if len(needle) > args.match:
            needle = needle[: args.match]
        haystack_idx = haystack.find(needle)
        if haystack_idx > 0:
            print(
                f"[+] Match offset={'%08x' % (args.haystack_base + haystack_idx)} size={len(needle)} sym={sym_name}"
            )
        else:
            print(f"[-] Unknown sym={sym_name}")


def match_symbol_reloc(haystack, sym, text, strtab, relas_map):
    sym_type = sym.entry["st_info"]["type"]
    sym_name = strtab.get_string(sym["st_name"])
    if len(sym_name) == 0:
        return
    if sym_type != "STT_FUNC":
        return
    # Get section in .text referenced by symbol.
    func_value_ptr = sym["st_value"]
    func_value_size = sym["st_size"]
    if func_value_size < args.min_match:
        return
    sym_value = text[func_value_ptr : func_value_ptr + func_value_size]
    if len(sym_value) < func_value_size:
        print(f"[!] Malformed sym={sym_name}")
        return
    # Create a mask of static vs fuzzy bytes, based on relocatable entries.
    mask = [0] * func_value_size
    for rela in relas_map.slice_range(func_value_ptr, func_value_size):
        rela_offset = rela["r_offset"] - func_value_ptr
        # TODO 4 byte mask not applicable to all
        mask[rela_offset : rela_offset + 4] = [1, 1, 1, 1]
    # Formulate a regex string.
    regex = b""
    for i, mask_bit in enumerate(mask):
        if mask_bit == 0:
            regex += b"\\x%02x" % sym_value[i]
        else:
            regex += b"."
    # Seach for symbol.
    match = re.search(regex, haystack)
    if match is not None:
        haystack_pos = args.haystack_base + match.start()
        print(
            f"[+] Match offset={'%08x' % haystack_pos} size={func_value_size} sym={sym_name}"
        )
    else:
        print(f"[-] Unknown sym={sym_name}")


def match_elf(haystack, elf):
    symtab = elf.get_section_by_name(".symtab")  # Symbol table
    strtab = elf.get_section_by_name(".strtab")  # String table
    textrela = elf.get_section_by_name(".rela.text")  # Relocation table
    text_section = elf.get_section_by_name(".text")  # Text section
    if text_section is None:
        return
    text = text_section.data()
    if not args.no_reloc:
        relas_iter = ()
        if textrela is not None:
            relas_iter = textrela.iter_relocations()
        relas_map = RelocationMap(relas_iter)
        for sym in symtab.iter_symbols():
            match_symbol_reloc(haystack, sym, text, strtab, relas_map)
    else:
        match_symbol_static(haystack, sym, text, strtab)


for needle_path in args.needles:
    needle_short = Path(needle_path).name
    print(f"[~] Opening {needle_short}")
    # List the files in the (ar)chive.
    ar_table = subprocess.run(["ar", "t", needle_path], capture_output=True, check=True)
    ar_table_stdout = ar_table.stdout.decode("utf-8")
    object_files = ar_table_stdout.split()
    # Open each file (might not scale well, but whatever).
    for object_file in object_files:
        print(f"[~] Crawling {needle_short}/{object_file}")
        # Extract and parse ELF
        elf_buf_call = subprocess.run(
            ["ar", "p", needle_path, object_file], capture_output=True, check=True
        )
        elf_buf = elf_buf_call.stdout
        try:
            elf = ELFFile(BytesIO(elf_buf))
        except ELFError:
            print(f"[!] Malformed ELF: {needle_short}/{object_file}")
            continue
        match_elf(haystack, elf)
