#!/usr/bin/env python3
"""Build every LOOM example firmware image into `sw/prog/` + a placement manifest.

Usage
-----
    python sw/prog/build_examples.py            # assemble all sw/examples/*.asm
    python sw/prog/build_examples.py --check    # build into a temp dir and verify
                                                # sizes, do not touch sw/prog/

For each `<name>.asm` this writes:
    sw/prog/<name>.hex   exact-size image, one 8-digit hex word per line
    sw/prog/<name>.lst   assembler listing
    sw/prog/<name>.sym   symbol table
    sw/prog/manifest.json

The images are **relocatable**: every jump is PC-relative, so a program can be
loaded at any PMEM word address and run by setting the thread PC to that base.
`manifest.json` fixes the demo placement (one program per thread) and carries
the pin map / doorbell / DMEM requirements so host code does not hard-code them.

The FF-fallback build has only 128 PMEM words, so each image is checked against
that limit; the full four-thread demo needs the 1024-word (SRAM) build.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sw import loom_asm  # noqa: E402

EXAMPLES = HERE.parent / "examples"
PROG = HERE
FF_FALLBACK_WORDS = 128

#: demo_all placement: one program per thread, base addresses far enough apart
#: for the SRAM (1024-word) build.  All images are position independent.
DEMO_BASE = {
    "uart_tx": 0x000,
    "uart_rx": 0x080,
    "spi_master": 0x100,
    "i2c_master": 0x180,
}

#: per-program metadata that host code needs (pins are instruction pin indices)
META = {
    "uart_tx": dict(thread=0, evt_bit=0, dir_mask=0x00, setm=0x0400,
                    pins=dict(TX=10), fifo="host->RXFIFO, one doorbell per burst",
                    notes="8N1 TX, BAUD_DIV 0x1216 (289.375 cyc/bit @ 33.33 MHz)"),
    "uart_rx": dict(thread=1, evt_bit=None, dir_mask=0x00, setm=0x0000,
                    pins=dict(RX=11), fifo="RXFIFO->host (decoded bytes)",
                    notes="8N1 RX, relative WAITD mid-bit sampling"),
    "spi_master": dict(thread=2, evt_bit=2, dir_mask=0x0B, setm=0x0000,
                       pins=dict(SCK=0, MOSI=1, MISO=2, CS_N=3),
                       fifo="host->RXFIFO byte, MISO byte -> TXFIFO",
                       notes="mode 0, QUARTER_CYCLES=4 -> ~32 cyc/bit (~1.03 MHz @ 33.33 MHz)"),
    "i2c_master": dict(thread=3, evt_bit=3, dir_mask=0x30, setm=0x0000,
                       pins=dict(SCL=4, SDA=5), fifo="4-byte job -> RXFIFO, read byte -> TXFIFO",
                       notes="open-drain, 100 kHz, clock-stretch tolerant"),
    "spi_slave": dict(thread=0, evt_bit=None, dir_mask=0x01, setm=0x0000,
                      pins=dict(MISO=0, SCK=4, MOSI=5, CS_N=6),
                      fifo="one byte per CS frame -> TXFIFO",
                      notes="mode 0 target, MISO = previous MOSI + 1"),
    "i2c_target": dict(thread=0, evt_bit=None, dir_mask=0xC0, setm=0x0000,
                       pins=dict(SCL=6, SDA=7), fifo="none",
                       notes="register file = DMEM[0..7], addr 0x50"),
    "ws2812": dict(thread=0, evt_bit=None, dir_mask=0x00, setm=0x1000,
                   pins=dict(DATA=12), fifo="3 bytes (G,R,B) per LED -> RXFIFO",
                   notes="800 kHz, T0H 13.3 / T1H 26.7 cycles, 50 us latch @ 33.33 MHz"),
    "ps2_target": dict(thread=0, evt_bit=None, dir_mask=0x00, setm=0x0000,
                       pins=dict(CLK=13, DATA=14), fifo="one scancode per RXFIFO byte",
                       notes="11-bit device->host frames, ~14.3 kHz clock"),
    "onewire": dict(thread=0, evt_bit=None, dir_mask=0x80, setm=0x0000,
                    pins=dict(DQ=7), fifo="none",
                    notes="DS18B20-style, +25.0 C scratchpad in DMEM[0..8]"),
    "uart_loopback": dict(thread=0, evt_bit=None, dir_mask=0x00, setm=0x0400,
                          pins=dict(TX=10, RX=11), fifo="echo also PUSHed to TXFIFO",
                          notes="half-duplex echo, needs inter-byte gap"),
}


def build(out_dir: pathlib.Path, *, quiet: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "generated_by": "sw/prog/build_examples.py",
        "clock_hz": 33_333_333,
        "pmem_words": 1024,
        "ff_fallback_words": FF_FALLBACK_WORDS,
        "target": "tt_um_rajesh963_loom",
        "programs": {},
        "demo_all": {"base": DEMO_BASE,
                     "threads": ["uart_tx", "uart_rx", "spi_master", "i2c_master"]},
    }
    sources = sorted(EXAMPLES.glob("*.asm"))
    if not sources:
        raise SystemExit(f"no .asm sources in {EXAMPLES}")
    ok = True
    for src in sources:
        name = src.stem
        # Assemble with a repo-relative source name: listings must be identical
        # on every machine (no absolute build paths), so rebuilding a tracked
        # image set never depends on where the checkout lives.
        try:
            source_name = src.relative_to(REPO).as_posix()
        except ValueError:  # tree copied outside the repo root
            source_name = src.name
        try:
            result = loom_asm.assemble(
                src.read_text(encoding="utf-8"), filename=source_name)
        except loom_asm.AsmError as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            ok = False
            continue
        words = result.words
        if len(words) > FF_FALLBACK_WORDS:
            print(f"WARN {name}: {len(words)} words > {FF_FALLBACK_WORDS} "
                  f"(SRAM build only)", file=sys.stderr)
        (out_dir / f"{name}.hex").write_text(
            loom_asm.format_hex32(words), encoding="utf-8")
        (out_dir / f"{name}.lst").write_text(
            result.listing_text(), encoding="utf-8")
        (out_dir / f"{name}.sym").write_text(
            result.symbols_text(), encoding="utf-8")
        meta = dict(META.get(name, {}))
        meta.update(dict(
            source=f"sw/examples/{src.name}",
            image=f"sw/prog/{name}.hex",
            words=len(words),
            relocatable=True,
            relocs=len(result.relocs),
            symbols=len(result.symbols),
            base=DEMO_BASE.get(name),
        ))
        manifest["programs"][name] = meta
        if not quiet:
            print(f"{name:14s} {len(words):3d} words  {' '.join(sorted(meta.get('pins', {})))}")

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n",
                                           encoding="utf-8")
    if not ok:
        raise SystemExit(1)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="build LOOM example firmware")
    ap.add_argument("--check", action="store_true",
                    help="build into a temporary directory (no sw/prog changes)")
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        with tempfile.TemporaryDirectory() as td:
            manifest = build(pathlib.Path(td), quiet=args.quiet)
        print(f"BUILD_CHECK: PASS ({len(manifest['programs'])} programs)")
        return 0
    manifest = build(PROG, quiet=args.quiet)
    print(f"BUILD: {len(manifest['programs'])} programs -> {PROG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
