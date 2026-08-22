"""
Check that every tool UEWalker depends on is present and actually works.

Each check runs in the order the walk would reach it and prints `ok` / `FAIL`;
the exit code is the number of failures. Optional checks (repak, and the live
container check) report `skip` instead of failing.

    python checktools.py                 # tools only
    python checktools.py path/to/x.utoc  # also unpack and decode a real container
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import UEWalker as W
from UEWalkerConfig import *

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

FAILURES = 0


def report(name: str, status: str, detail: str = "") -> None:
    """Print one result line; `status` is ok / FAIL / skip."""
    print(f"  [{status:4}] {name}" + (f": {detail}" if detail else ""))


def check(name: str, fn) -> object | None:
    """Run one check, printing its outcome; returns its value, or None on failure."""
    global FAILURES
    try:
        value = fn()
    except Exception as exc:
        FAILURES += 1
        report(name, "FAIL", f"{type(exc).__name__}: {exc}")
        return None
    report(name, "ok", str(value) if value else "")
    return value


def optional(name: str, fn) -> None:
    """Like `check`, but a failure is reported as a skip and does not count."""
    try:
        report(name, "ok", str(fn() or ""))
    except Exception as exc:
        report(name, "skip", str(exc))


def tool_version(path: str) -> str:
    """First line `<path> --version` prints, for a binary already known to exist."""
    proc = subprocess.run([path, "--version"], capture_output=True, text=True)
    out = (proc.stdout or proc.stderr).strip().splitlines()
    return out[0] if out else f"exit {proc.returncode}, no output"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_py7zr(tmp: Path) -> str:
    """Round-trip a tiny archive: write, list, then extract one member selectively."""
    import py7zr

    payload = b"\x89PNG\r\n\x1a\n" + b"0" * 64
    src = tmp / "src"
    src.mkdir()
    for name in ("a.png", "b.png"):
        (src / name).write_bytes(payload)

    # Written one by one: `writeall` would prefix the source directory's own name.
    archive = tmp / "t.7z"
    with py7zr.SevenZipFile(archive, "w") as z:
        for name in ("a.png", "b.png"):
            z.write(src / name, f"sub/{name}")

    source = W.SevenZipSource(archive)
    members = source.list_members()
    if "sub/a.png" not in members:
        raise RuntimeError(f"listing lost members: {members}")

    dest = tmp / "out"
    source.extract(["sub/a.png"], dest)
    if (dest / "sub" / "a.png").read_bytes() != payload:
        raise RuntimeError("extracted bytes differ")
    if (dest / "sub" / "b.png").exists():
        raise RuntimeError("selective extract pulled an unrequested member")
    return f"{py7zr.__version__}, {len(members)} members round-tripped"


def check_retoc() -> str:
    """retoc is on disk, launches, and knows the configured engine version."""
    path = W.require_tool(RETOC_PATH, "retoc")
    proc = subprocess.run([path, "to-zen", "--help"], capture_output=True, text=True)
    if RETOC_VERSION not in (proc.stdout + proc.stderr):
        raise RuntimeError(f"retoc does not list --version {RETOC_VERSION}")
    return tool_version(path)


def check_cue4parse() -> str:
    """Boot the CLR, bind CUE4Parse, and resolve the configured EGame member."""
    t = W.CUE4Parse.types()
    game = getattr(t["EGame"], UE_VERSION)  # raises if UE_VERSION is misspelled
    t["VersionContainer"](game)             # raises if the ctor signature drifted
    return f"CLR up, {UE_VERSION} resolved"


def check_dds_writer() -> str:
    """The pure-python DDS packer: header length, magic and payload passthrough."""
    mips = [b"\x11" * 8, b"\x22" * 8]
    blob = W.dds_bytes("PF_DXT1", 4, 4, mips)
    if not blob.startswith(W.DDS_MAGIC):
        raise RuntimeError("missing DDS magic")
    header = 4 + W.DDS_HEADER_SIZE + 20  # magic + DDS_HEADER + DDS_HEADER_DXT10
    if len(blob) != header + 16 or blob[header:] != b"".join(mips):
        raise RuntimeError("mip payload not written through verbatim")
    return f"{header}-byte header + {len(mips)} mips"


def check_container(utoc: Path, tmp: Path) -> str:
    """End-to-end on a real container: unpack it, then decode what came out."""
    set_dir = tmp / "container"
    set_dir.mkdir()
    for sibling in utoc.parent.glob(utoc.stem + ".*"):
        shutil.copy2(sibling, set_dir)

    cooked = tmp / "cooked"
    W.UEContainerSource(set_dir).extract([], cooked)
    assets = sorted(cooked.rglob("*.uasset"))
    if not assets:
        raise RuntimeError("unpacked, but no .uasset came out")

    # Decode until one asset yields a texture: many packages hold no mips at all.
    decoder = W.TextureDecoder(cooked)
    for asset in assets:
        written = decoder.decode(asset, tmp / "dds")
        if written:
            return f"{len(assets)} assets, first texture {written[0].name}"
    return f"{len(assets)} assets, none holding textures"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    utoc = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None
    print("checking UEWalker tools")
    with tempfile.TemporaryDirectory(prefix="uewalker_check_") as tmp_name:
        tmp = Path(tmp_name)
        check("py7zr", lambda: check_py7zr(tmp))
        check("retoc", check_retoc)
        optional("repak (only for .pak-only sets)",
                 lambda: tool_version(W.require_tool(REPAK_PATH, "repak")))
        check("CUE4Parse", check_cue4parse)
        check("DDS writer", check_dds_writer)
        if utoc is None:
            report("container end-to-end", "skip", "pass a .utoc to run it")
        else:
            check(f"container {utoc.name}", lambda: check_container(utoc, tmp))

    print("all good" if not FAILURES else f"{FAILURES} check(s) failed")
    return FAILURES


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.DEBUG if UEWALKER_DEBUG else logging.WARNING,
                        format="%(levelname)s: %(message)s")
    sys.exit(main())
