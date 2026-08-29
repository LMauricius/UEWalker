"""
Check that every tool UEWalker depends on is present and actually works.

Each check runs in the order the walk would reach it and prints `ok` / `FAIL`;
the exit code is the number of failures. Optional checks report `skip` instead of
failing: retoc and repak, which only the later patch pass needs, and the live
container check.

    python checktools.py                 # tools only
    python checktools.py path/to/x.utoc  # also mount and decode a real container
"""

from __future__ import annotations

import json
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


def require_tool(configured: str, what: str) -> str:
    """Resolve a configured binary against PATH, or raise naming it."""
    found = shutil.which(configured) or (
        configured if Path(configured).is_file() else None
    )
    if found is None:
        raise RuntimeError(f"{what} not found: {configured!r} (see UEWalkerConfig)")
    return found


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
    path = require_tool(RETOC_PATH, "retoc")
    proc = subprocess.run([path, "to-zen", "--help"], capture_output=True, text=True)
    if RETOC_VERSION not in (proc.stdout + proc.stderr):
        raise RuntimeError(f"retoc does not list --version {RETOC_VERSION}")
    return tool_version(path)


def missing_dependencies(deps_json: Path) -> list[str]:
    """
    Dependency assemblies named by a `.deps.json` that are not next to it.

    A plain `dotnet build` writes the manifest but leaves the packages in the NuGet
    cache, so the manifest is the only reliable way to tell a build output from a
    publish output.
    """
    manifest = json.loads(deps_json.read_text())
    wanted: set[str] = set()
    for target in manifest.get("targets", {}).values():
        for library in target.values():
            wanted.update(Path(f).name for f in library.get("runtime", {}))
    return sorted(
        name
        for name in wanted
        if name.endswith(".dll") and not (deps_json.parent / name).is_file()
    )


def target_framework(dll: Path) -> str:
    """Framework moniker baked into an assembly, e.g. `.NETCoreApp,Version=v10.0`."""
    blob = dll.read_bytes()
    marker = b".NETCoreApp,Version=v"
    at = blob.find(marker)
    if at < 0:
        return "unknown (not a .NETCoreApp assembly?)"
    end = blob.index(b"\0", at)
    return blob[at:end].decode("ascii", "replace")


def check_dotnet_layout() -> str:
    """
    The .NET side before any CLR boot: a runtime to host the DLL, and its siblings.

    `clr.AddReference` resolves dependencies from the DLL's own directory and only
    fails once a type actually needs one, so the directory is checked for completeness
    here instead.
    """
    dll = Path(CUE4PARSE_DLL)
    if not dll.is_file():
        raise RuntimeError(f"{CUE4PARSE_DLL} does not exist")
    framework = target_framework(dll)

    problems = []
    if shutil.which("dotnet") is None:
        problems.append("no `dotnet` on PATH: install the runtime matching " + framework)

    # The manifest is named after whichever project pulled CUE4Parse in, not after
    # CUE4Parse itself, so any *.deps.json in the directory is the right one.
    manifests = sorted(dll.parent.glob("*.deps.json"))
    if not manifests:
        problems.append(
            "no *.deps.json beside the DLL: copy the whole `dotnet publish` output "
            "directory, not just the one file"
        )
    else:
        absent = missing_dependencies(manifests[0])
        if absent:
            shown = ", ".join(absent[:4]) + (f", +{len(absent) - 4} more" if len(absent) > 4 else "")
            problems.append(
                f"{len(absent)} dependency assemblies missing ({shown}): this is a "
                f"`dotnet build` output, which leaves them in the NuGet cache. Re-run as "
                f"`dotnet publish -c Release -o <dir>` and point CUE4PARSE_DLL at <dir>"
            )

    if DOTNET_RUNTIME_CONFIG and not Path(DOTNET_RUNTIME_CONFIG).is_file():
        problems.append(f"DOTNET_RUNTIME_CONFIG missing: {DOTNET_RUNTIME_CONFIG}")
    if problems:
        raise RuntimeError("; ".join(problems))
    return f"{framework}, dependencies complete"


def check_cue4parse() -> str:
    """Boot the CLR, bind CUE4Parse, and resolve the configured EGame member."""
    t = W.CUE4Parse.types()
    game = getattr(t["EGame"], UE_VERSION)  # raises if UE_VERSION is misspelled
    t["VersionContainer"](game)             # raises if the ctor signature drifted
    # Booting the loader already fails hard on a missing Oodle library; naming it here
    # keeps a decode-blocking dependency visible in the report instead of implied.
    oodle = t["OodleHelper"].OodleFileName
    return f"CLR up, {UE_VERSION} resolved, {oodle} loaded"


def check_usmap() -> str:
    """Parse the configured `.usmap`, reporting how many types it maps."""
    if not USMAP_PATH:
        raise RuntimeError(
            "USMAP_PATH unset: packages with unversioned properties cannot be "
            "serialized (MappingException), which is most of the game's own containers"
        )
    if not Path(USMAP_PATH).is_file():
        raise RuntimeError(f"USMAP_PATH missing: {USMAP_PATH}")
    # Constructing the provider parses the file, so a truncated or wrong-version dump
    # fails here rather than mid-walk. `Types` is the mapped-class table.
    mappings = W.mappings_container()
    types = mappings.MappingsForGame.Types.Count
    if not types:
        raise RuntimeError(f"{USMAP_PATH} parsed but maps no types")
    return f"{types} type(s) from {Path(USMAP_PATH).name}"


def check_dds_writer() -> str:
    """The pure-python DDS packer: header length, magic and payload passthrough."""
    mips = [b"\x11" * 8, b"\x22" * 8]
    blob = W.dds_bytes("PF_DXT1", 4, 4, mips)
    if not blob.startswith(W.DDS_MAGIC):
        raise RuntimeError("missing DDS magic")
    header = 4 + W.DDS_HEADER_SIZE + 20  # magic + DDS_HEADER + DDS_HEADER_DXT10
    if len(blob) != header + 16 or blob[header:] != b"".join(mips):
        raise RuntimeError("mip payload not written through verbatim")
    # dwPitchOrLinearSize is the whole top surface, not one row of blocks.
    linear = int.from_bytes(blob[20:24], "little")
    if linear != W.mip_nbytes("PF_DXT1", 4, 4):
        raise RuntimeError(f"linear size is {linear}, expected one 4x4 block")
    # No format is ever written as its `_SRGB` twin: NVIDIA Texture Tools refuses to
    # open those, and the colour space travels in the sidecar instead.
    twins = {72, 75, 78, 91, 29, 99}
    emitted = {W.dxgi_of(name) for name in W.PIXEL_FORMATS}
    if emitted & twins:
        raise RuntimeError(f"sRGB DXGI codes emitted: {sorted(emitted & twins)}")
    return f"{header}-byte header + {len(mips)} mips"


def check_container(utoc: Path, tmp: Path) -> str:
    """End-to-end on a real container: mount it, then decode out of it."""

    # The container set is copied whole: CUE4Parse reads the .ucas alongside its
    # .utoc, and mounts the directory rather than an individual file.
    set_dir = tmp / "container"
    set_dir.mkdir()
    for sibling in utoc.parent.glob(utoc.stem + ".*"):
        shutil.copy2(sibling, set_dir)

    decoder = W.TextureDecoder(set_dir)
    assets = decoder.packages()
    if not assets:
        raise RuntimeError("mounted, but the index holds no .uasset")

    # Decode until one asset yields a texture: many packages hold no mips at all.
    for asset in assets:
        written = decoder.decode(asset, tmp / "dds", tmp / "meta", lambda name: False)
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
        # The walk itself never runs either: both belong to the patch pass.
        optional("retoc (only for the patch pass)", check_retoc)
        optional(
            "repak (only for .pak-only sets)",
            lambda: tool_version(require_tool(REPAK_PATH, "repak")),
        )
        check(".NET layout", check_dotnet_layout)
        check("CUE4Parse", check_cue4parse)
        # A skip only costs the packages that carry unversioned properties, so the
        # walk still runs without mappings; it just loses those assets.
        optional("type mappings (.usmap)", check_usmap)
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
