"""
Walk an Unreal Engine mod folder and yield every editable texture inside it.

Descends recursively: mod root -> .7z archives -> UE containers (.pak / .utoc+.ucas)
-> cooked Zen assets -> decoded .dds. Containers lying loose in the mod root, with no
archive around them, are walked the same way. Only the .7z layer is unpacked (py7zr); a
UE container is mounted through CUE4Parse and read in place, so no cooked tree is written
and no external binary is run. Yielded files are temporary: they live in the
walk's scratch tree and each dies as the next one is produced. `EDIT_ROOT_DIR` belongs to
the consumer: it writes its edited textures there under the yielded relative path, and
the walker follows behind, keeping only what an edit makes worth keeping -- the cooked
package it came from (into `UASSET_DIR`, whose tree mirrors the edit tree), and
optionally a `backup-` copy of the original. Edits are turned
into patch containers later, by a separate pass; nothing unedited is ever stored.
See `ModWalker` / `fileIterator` for the public entry points.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import signal
import struct
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import py7zr

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

from UEWalkerConfig import *

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
SEVENZIP_EXTS = {".7z"}
UE_EXTS = {".pak", ".ucas", ".utoc"}

#: Prefix marking an untouched copy. Also excluded from image scans, so a backup is
#: never mistaken for a texture on a later pass over the same output directory.
BACKUP_PREFIX = "backup-"

#: Written into a container's output directory once every texture in it has been
#: handed over without error. A container carrying it is skipped whole on a later
#: walk, before its payload is extracted from the archive again.
DONE_MARKER = ".uewalker-done"

# UE pixel format -> (DXGI format, bytes per 4x4 block, block-compressed?).
# Only the formats game textures actually ship in; anything else is skipped loudly.
PIXEL_FORMATS = {
    "PF_DXT1":       (71, 8, True),    # BC1_UNORM
    "PF_DXT3":       (74, 16, True),   # BC2_UNORM
    "PF_DXT5":       (77, 16, True),   # BC3_UNORM
    "PF_BC4":        (80, 8, True),    # BC4_UNORM
    "PF_BC5":        (83, 16, True),   # BC5_UNORM
    "PF_BC7":        (98, 16, True),   # BC7_UNORM
    "PF_B8G8R8A8":   (87, 4, False),   # B8G8R8A8_UNORM
    "PF_R8G8B8A8":   (28, 4, False),   # R8G8B8A8_UNORM
    "PF_G8":         (61, 1, False),   # R8_UNORM
    "PF_FloatRGBA":  (10, 8, False),   # R16G16B16A16_FLOAT
}

#: Every DXGI code above is the linear UNORM one, and a colour map keeps it even
#: though the texture says otherwise. A cooked texture carries the colour space in its
#: own SRGB flag rather than in its pixel format, and the honest reading would be the
#: `_SRGB` twin (BC1 71 -> 72, BC3 77 -> 78, BC7 98 -> 99, ...); NVIDIA Texture Tools
#: rejects every one of those outright ("this type of DDS file is not supported"),
#: while accepting each UNORM code, so tagging the header honestly would cost us the
#: only decompressor that reads these files. The bytes are identical either way: the
#: twin differs only in how a reader interprets them. The flag itself is not lost, it
#: travels in the sidecar (`srgb`), which is where the patch pass reads it anyway.

DDS_MAGIC = b"DDS "
DDS_HEADER_SIZE = 124  # bytes after the magic, excluding the DX10 extension

# Which container member represents the set in the output path, most specific first.
UE_REPRESENTATIVE_ORDER = (".ucas", ".pak", ".utoc")

# (absolute file path, path relative to the mod root)
WalkItem = tuple[str, str]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

#: Memo for `global_dir`, which resolves the game's global container once per process.
_GLOBAL_DIR: dict[str, str | None] = {}

def ext_of(name: str) -> str:
    """Lowercase suffix of a path-like name, `.dds` style (empty string if none)."""
    return PurePosixPath(name).suffix.lower()


def is_image(name: str) -> bool:
    """True if the name has an extension we treat as an editable texture."""
    return ext_of(name) in IMAGE_EXTS


def to_posix(internal: str) -> str:
    """Normalise an archive-internal member name to forward slashes."""
    return internal.replace("\\", "/")


def write_atomic(path: Path, payload: bytes) -> None:
    """
    Write a durable file so an interrupted run cannot leave a half one behind.

    Everything in `EDIT_ROOT_DIR` outlives the walk and `skip_existing` trusts whatever is
    already there, so a truncated file would be believed on every later run. The
    rename is atomic within a directory: the file is either the previous content or
    the new one, never a prefix of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def write_json(path: Path, payload: object) -> None:
    """Write a JSON sidecar; see `write_atomic` for why it goes through a rename."""
    write_atomic(path, json.dumps(payload, indent=2).encode())


def read_json(path: Path) -> object | None:
    """Parsed contents of a JSON file, or None if it is missing or truncated."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def drop(path: Path) -> None:
    """Remove a file or directory tree, ignoring anything that is not there."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Scratch lifetime
# ---------------------------------------------------------------------------

#: Every temp directory this module makes is `uewalker-<pid>-<random>`. The pid is
#: what lets a later run tell a dead process's leftovers from a live walk's scratch.
SCRATCH_PREFIX = "uewalker-"

#: Scratch roots this process owns, dropped on exit or on a fatal signal.
_scratch_roots: set[Path] = set()

#: Handlers displaced by `scratch_guard`, keyed by signal, restored when it exits.
_displaced: dict[int, object] = {}


def scratch_dir() -> Path:
    """New temp directory, tracked so it is removed even on an abrupt exit."""
    path = Path(tempfile.mkdtemp(prefix=f"{SCRATCH_PREFIX}{os.getpid()}-"))
    _scratch_roots.add(path)
    return path


def release_scratch(path: Path) -> None:
    """Remove one tracked scratch directory now, and stop tracking it."""
    _scratch_roots.discard(path)
    shutil.rmtree(path, ignore_errors=True)


def _drop_scratch() -> None:
    """Remove every scratch directory this process still owns. Runs on exit."""
    for path in list(_scratch_roots):
        release_scratch(path)


# A walk's scratch runs to gigabytes, so an ordinary exit must never leave it behind:
# normal returns, uncaught exceptions and `sys.exit` all pass through here.
atexit.register(_drop_scratch)


def _on_fatal_signal(signum: int, frame: object) -> None:
    """Drop the scratch, then hand the signal on to the handler we displaced."""
    _drop_scratch()
    previous = _displaced.get(signum, signal.SIG_DFL)
    if callable(previous):
        previous(signum, frame)  # type: ignore[operator]
        return
    # Nothing was installed: put the default back and re-raise, so the process dies
    # exactly as it would have (right exit status, right "killed by" report).
    signal.signal(signum, previous)  # type: ignore[arg-type]
    os.kill(os.getpid(), signum)


@contextmanager
def scratch_guard() -> Iterator[None]:
    """
    Clean up after fatal signals for as long as a walk is running.

    SIGTERM and SIGHUP kill the process outright: no `atexit`, no `finally` in an
    abandoned generator, so the scratch tree would survive the walk that made it.
    SIGINT is deliberately left alone, since its default already raises
    KeyboardInterrupt, which unwinds the walk normally and can even be caught and
    the walk resumed. Handlers are installed only for the duration of the walk and
    always chain to what was there before, so an embedding application keeps its own
    shutdown behaviour. Signals can only be handled on the main thread; elsewhere
    the walk simply runs unguarded, and the stale sweep is the backstop.
    """
    installed: list[int] = []
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        previous = signal.getsignal(sig)
        # SIG_IGN means the host is deliberately deaf to this signal, and our own
        # handler means an outer walk already guards it: in both cases, hands off.
        if previous is signal.SIG_IGN or previous is _on_fatal_signal:
            continue
        try:
            signal.signal(sig, _on_fatal_signal)
        except ValueError:  # not the main thread
            break
        _displaced[sig] = previous
        installed.append(sig)
    try:
        yield
    finally:
        for sig in installed:
            signal.signal(sig, _displaced.pop(sig))  # type: ignore[arg-type]


def sweep_stale_scratch() -> None:
    """
    Remove scratch left behind by a run that never got to clean up.

    A SIGKILL, a power cut or a crash inside the CLR takes the process down with no
    chance to run anything at all, so the tree is still there on the next run. The
    owning pid is in the directory name: a dead owner makes the tree ours to remove,
    a live one means a concurrent walk we must not touch. Liveness is a POSIX check,
    so elsewhere the sweep does nothing rather than risk deleting live scratch.
    """
    if os.name != "posix":
        return
    for path in Path(tempfile.gettempdir()).glob(f"{SCRATCH_PREFIX}*-*"):
        owner = path.name[len(SCRATCH_PREFIX) :].split("-", 1)[0]
        if not owner.isdigit() or int(owner) == os.getpid():
            continue
        try:
            os.kill(int(owner), 0)
        except ProcessLookupError:
            pass  # the owner is gone, so nothing can still be using this
        except OSError:
            continue  # alive, just not ours to signal
        else:
            continue  # alive: a concurrent walk's scratch
        log.info("sweeping stale scratch %s", path)
        shutil.rmtree(path, ignore_errors=True)


def join_rel(*parts: str) -> str:
    """Join relative path segments POSIX-style, dropping empty and `.` segments."""
    kept = [p.strip("/") for p in parts if p and p.strip("/") not in ("", ".")]
    return "/".join(kept)


def archive_segment(relpath: str) -> str:
    """
    Path segment standing in for an archive: `pack/abc.7z` -> `pack/abc.7z-extracted`.

    Parent directories are preserved; only the archive's own name is tagged.
    """
    p = PurePosixPath(relpath)
    return join_rel(str(p.parent), p.name + "-extracted")


def flat_dds(root: Path, name: str, flat: bool) -> Path:
    """
    Where one decoded texture goes: `<root>/<name>.dds`, or `<root>.dds` when `flat`.

    A package holding a single texture named after it needs no directory of its own, so
    the `-extracted` tag moves onto the file itself: `Foo.uasset-extracted.dds` rather
    than `Foo.uasset-extracted/Foo.dds`. Both shapes carry the same information, and the
    patch pass tells them apart by which of the two wears the tag.
    """
    return root.with_name(root.name + ".dds") if flat else root / f"{name}.dds"


#: `<base>_P`, `<base>_P2`, ... : UE's patch-container naming.
PATCH_SUFFIX = re.compile(r"^(?P<base>.+)_P(?P<priority>\d*)$")


def patch_family(stem: str) -> tuple[str, int]:
    """
    Split a container stem into its family name and patch priority.

    UE names patch containers `<base>_P`, `<base>_P2`, ..., mounted over the base in
    ascending order. They are one logical container: a payload in one belongs to
    packages indexed in another, so a texture's optional (`.uptnl`) mip routinely
    lives in a higher-numbered sibling. A stem with no patch suffix is a family of
    its own at priority 0.
    """
    m = PATCH_SUFFIX.match(stem)
    if m is None:
        return stem, 0
    return m["base"], int(m["priority"] or 1)


def global_dir() -> str | None:
    """
    Directory to mount beside a container so IoStore packages can be serialized.

    An IoStore package resolves its script objects through the game's global
    container, so a mod container alone cannot be read. Only `global.utoc` and
    `global.ucas` are wanted, and `GAME_PAK_DIR` also holds the game's full content
    (well over a hundred gigabytes), which the provider would otherwise index in
    its entirety: the two files are symlinked into a directory of their own and
    that is mounted instead. Returns None when the global container is unavailable,
    which costs IoStore mods but leaves legacy `.pak` ones working.
    """
    if _GLOBAL_DIR.get("path", ...) is not ...:  # resolved once per process
        return _GLOBAL_DIR["path"]

    _GLOBAL_DIR["path"] = None
    if not GAME_PAK_DIR:
        log.warning("GAME_PAK_DIR is unset: IoStore containers cannot be decoded")
        return None

    paks = Path(GAME_PAK_DIR)
    members = [paks / "global.utoc", paks / "global.ucas"]
    if missing := [m.name for m in members if not m.is_file()]:
        log.warning("%s: no %s: IoStore containers cannot be decoded", paks, ", ".join(missing))
        return None

    # Long-lived: every container mounts it, so it outlives any one walk rather than
    # being tied to a walk's scratch tree, and is dropped when the process ends.
    holder = scratch_dir()
    for member in members:
        (holder / member.name).symlink_to(member)
    log.debug("global container mounted from %s", paks)
    _GLOBAL_DIR["path"] = str(holder)
    return _GLOBAL_DIR["path"]


class ToolError(RuntimeError):
    """
    An internal tool is missing or cannot be loaded.

    Distinct from the per-file failures the walk skips: nothing downstream can
    succeed once this is raised, so `ModWalker._guard` lets it through.
    """


def require_tools() -> None:
    """Fail early, and by name, if a configured dependency is missing."""
    # Only CUE4Parse: the walk reads containers through it and unpacks nothing, so
    # retoc and repak are the later patch pass's problem, not this module's.
    if not Path(CUE4PARSE_DLL).is_file():
        log.error("CUE4Parse.dll not found: %r (see UEWalkerConfig)", CUE4PARSE_DLL)
        raise ToolError(f"CUE4Parse.dll not found: {CUE4PARSE_DLL!r}")


def dxgi_of(pixel_format: str) -> int:
    """DXGI code a UE pixel format is written as; always the linear one (see the table)."""
    return PIXEL_FORMATS[pixel_format][0]


def mip_extent(width: int, height: int, level: int) -> tuple[int, int]:
    """True dimensions of mip `level`, halving and stopping at 1 the way UE does."""
    return max(1, width >> level), max(1, height >> level)


def mip_nbytes(pixel_format: str, width: int, height: int) -> int:
    """Payload size of one mip at those dimensions, in bytes."""
    _, block, compressed = PIXEL_FORMATS[pixel_format]
    if not compressed:
        return width * height * block
    return max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * block


def mip_chain_prefix(
    pixel_format: str, sizes: list[tuple[int, int, int]]
) -> tuple[int, str | None]:
    """
    How many leading mips form a DDS-legal chain, and why the rest were cut.

    A DDS stores only a mip *count*: every level's dimensions and payload size are
    derived by halving the top, so one missing or short mip silently shifts every
    level after it. `sizes` is `(width, height, payload length)` per mip, in order;
    the returned count is the longest prefix that matches the derived chain.

    A block-compressed mip below 4x4 is reported either as its true size or clamped
    to the block, depending on how the texture was cooked. Both describe the same
    bytes, so both are accepted.
    """
    if not sizes:
        return 0, "no mips"
    compressed = PIXEL_FORMATS[pixel_format][2]
    top_w, top_h, _ = sizes[0]
    for level, (width, height, length) in enumerate(sizes):
        true_w, true_h = mip_extent(top_w, top_h, level)
        allowed = {(true_w, true_h)}
        if compressed:
            allowed.add((max(4, true_w), max(4, true_h)))
        if (width, height) not in allowed:
            return level, f"mip {level} is {width}x{height}, the chain expects {true_w}x{true_h}"
        want = mip_nbytes(pixel_format, true_w, true_h)
        if length != want:
            return level, f"mip {level} ({true_w}x{true_h}) has {length} bytes, expected {want}"
    return len(sizes), None


def dds_bytes(pixel_format: str, width: int, height: int, mips: list[bytes]) -> bytes:
    """
    Pack raw mip payloads into a DDS with a DX10 header.

    Mip bytes are written through untouched: this is a container swap, not a
    re-encode, so the edit stays reversible against the cooked original. The header
    is deliberately colour-space-blind; `PIXEL_FORMATS` says why.
    """
    dxgi = dxgi_of(pixel_format)
    block, compressed = PIXEL_FORMATS[pixel_format][1:]

    # DDSD_CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT, plus LINEARSIZE or PITCH.
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000
    flags |= 0x80000 if compressed else 0x8
    # LINEARSIZE is the whole top surface, not one row of blocks; PITCH is one row.
    pitch = mip_nbytes(pixel_format, width, height) if compressed else width * block

    # DDS_HEADER: 7 dwords, 44 reserved bytes, DDS_PIXELFORMAT (8 dwords),
    # 4 caps dwords, 1 reserved dword. 128 bytes with the magic.
    caps = 0x1000 | (0x400000 | 0x8 if len(mips) > 1 else 0)  # TEXTURE | MIPMAP|COMPLEX
    header = struct.pack(
        "<4sIIIIIII44sIIIIIIIIIIIII",
        DDS_MAGIC, DDS_HEADER_SIZE, flags, height, width, pitch, 0, len(mips),
        b"\0" * 44,                                        # dwReserved1
        32, 0x4, int.from_bytes(b"DX10", "little"), 0, 0, 0, 0, 0,  # ddspf, DDPF_FOURCC
        caps, 0, 0, 0,                                      # dwCaps1..4
        0,                                                  # dwReserved2
    )
    # DDS_HEADER_DXT10: format, D3D10_RESOURCE_DIMENSION_TEXTURE2D, flags, 1 slice, flags2
    header += struct.pack("<IIIII", dxgi, 3, 0, 1, 0)
    return header + b"".join(mips)


# ---------------------------------------------------------------------------
# UE container grouping
# ---------------------------------------------------------------------------

@dataclass
class UEContainerSet:
    """
    A `.pak` / `.utoc` + `.ucas` group sharing one directory and patch family.

    UE containers only unpack as a set, so members are collected before extraction.
    The whole `_P<n>` family counts as one set rather than one set per stem: a patch
    container holds payload for packages indexed in its base, so mounting either half
    alone leaves those payloads unreachable. Members of each extension are ordered by
    ascending patch priority, which is the order UE mounts them in.
    """

    dirname: str
    family: str
    members: dict[str, list[str]] = field(default_factory=dict)  # ext -> internal names

    @property
    def representative(self) -> str:
        """Archive-internal name the `-extracted` segment is named after."""
        for e in UE_REPRESENTATIVE_ORDER:
            if e in self.members:
                return self.members[e][0]
        raise RuntimeError(f"empty container set {self.dirname}/{self.family}")

    @property
    def segment(self) -> str:
        """Relative segment this set contributes, e.g. `chars/skin.ucas-extracted`."""
        return archive_segment(self.representative)

    def internal_names(self) -> list[str]:
        return sorted(n for names in self.members.values() for n in names)


def group_ue_members(internals: Iterable[str]) -> list[UEContainerSet]:
    """Group container member names into ordered `UEContainerSet`s by (directory, family)."""
    groups: dict[tuple[str, str], UEContainerSet] = {}
    for internal in internals:
        p = PurePosixPath(internal)
        dirname = "" if str(p.parent) == "." else str(p.parent)
        family, _ = patch_family(p.stem)
        key = (dirname, family)
        group = groups.setdefault(key, UEContainerSet(dirname, family))
        group.members.setdefault(ext_of(internal), []).append(internal)

    # Priority order, so the lowest-numbered member names the segment (keeping the
    # relative path a plain mod would have had) and the highest is unpacked last.
    for group in groups.values():
        for names in group.members.values():
            names.sort(key=lambda n: patch_family(PurePosixPath(n).stem)[1])
    return [groups[k] for k in sorted(groups)]


# ---------------------------------------------------------------------------
# Sources: one adapter per container kind
# ---------------------------------------------------------------------------

class SevenZipSource:
    """
    `.7z` archive read through py7zr.

    Members are listed without extracting; extraction is then either selective (one
    member at a time) or batch, depending on what the caller is trading off.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        #: POSIX-normalised member name -> the name py7zr matches `targets` against.
        self._internal: dict[str, str] = {}

    def list_members(self) -> list[str]:
        with py7zr.SevenZipFile(self.path, "r") as z:
            infos = z.list()
        self._internal = {
            to_posix(i.filename): i.filename
            for i in infos
            if not getattr(i, "is_directory", False)
        }
        log.debug("%s: %d members", self.path, len(self._internal))
        return sorted(self._internal)

    def extract(self, members: list[str], dest: Path) -> None:
        # py7zr must reopen the archive per call; selective extraction of a solid
        # archive therefore re-decodes preceding blocks (see ModWalker.selective).
        log.debug(
            "extracting %d member(s) from %s -> %s", len(members), self.path, dest
        )
        # Names are normalised for the walk's own bookkeeping, but py7zr matches
        # `targets` against what the archive stores, so they go back as they came.
        targets = [self._internal.get(m, m) for m in members]
        with py7zr.SevenZipFile(self.path, "r") as z:
            z.extract(path=dest, targets=targets)


class DiskSource:
    """
    Container set lying loose in the mod folder, with no archive around it.

    Nothing is copied: the set's members are symlinked into a directory of their own,
    and that is what gets mounted. A provider indexes every container in the directory
    it is handed, so pointing it at the mod folder itself would pull in every unrelated
    container beside this one, breaking the one-set-at-a-time mount the walk relies on.
    Member names are paths relative to the mod root, and the link tree mirrors them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def extract(self, members: list[str], dest: Path) -> None:
        log.debug("linking %d member(s) from %s -> %s", len(members), self.root, dest)
        for member in members:
            link = dest / member
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.root / member)


class CUE4Parse:
    """
    Lazily loaded handle on the CUE4Parse assembly.

    pythonnet boots the CLR once per process, so the imported types are cached at
    module level. Loading is deferred until a container is actually reached: a walk
    over a mod with no UE containers needs no .NET runtime at all.
    """

    _types: dict[str, object] | None = None

    @classmethod
    def types(cls) -> dict[str, object]:
        """CUE4Parse types used below, keyed by name; loads the CLR on first call."""
        if cls._types is not None:
            return cls._types

        # coreclr only. CUE4Parse is built for .NETCoreApp, and Mono's BCL cannot
        # resolve its System.Runtime reference, so a fallback chain would only turn a
        # clear "no runtime installed" into a confusing type-resolution error. Booting
        # the CLR, binding the assembly and resolving its types either all work or the
        # decoder is unusable for every asset: report once, fatally.
        try:
            from pythonnet import load

            # The runtime config names the exact .NET version the assembly wants.
            config = {"runtime_config": DOTNET_RUNTIME_CONFIG} if DOTNET_RUNTIME_CONFIG else {}
            load("coreclr", **config)  # must precede `import clr`
            import clr

            # AddReference resolves by assembly name off sys.path, not by file path:
            # the publish directory goes on the path so the sibling dependency DLLs
            # (Oodle, Serilog, Newtonsoft, ...) resolve from there too.
            dll = Path(CUE4PARSE_DLL).resolve()
            sys.path.append(str(dll.parent))
            clr.AddReference(dll.stem)

            from CUE4Parse.Compression import OodleHelper  # noqa: PLC0415
            from CUE4Parse.FileProvider import DefaultFileProvider  # noqa: PLC0415
            from CUE4Parse.UE4.Assets.Exports.Texture import UTexture2D  # noqa: PLC0415
            from CUE4Parse.UE4.Versions import EGame, VersionContainer  # noqa: PLC0415
            from System.IO import DirectoryInfo, SearchOption  # noqa: PLC0415

            # UE 4.26 container data is Oodle-compressed, and the native Oodle library
            # is loaded separately from the assembly. `Initialize` downloads an
            # open-source build to the path it is given when that path does not exist
            # yet, so the cache lives next to the DLL unless OODLE_LIB overrides it.
            #
            # The path must never be None: `Initialize` is overloaded on (str) and
            # (Oodle), and a None binds the second overload, which installs a null
            # instance and returns cleanly. The failure would then only surface much
            # later, as a NullReferenceException inside the decompressor.
            oodle = OODLE_LIB or str(dll.parent / OodleHelper.OodleFileName)
            OodleHelper.Initialize(oodle)
            if OodleHelper.Instance is None:
                raise RuntimeError(f"Oodle library unavailable at {oodle!r}")
        except Exception as exc:
            log.error("cannot load CUE4Parse from %r: %s", CUE4PARSE_DLL, exc)
            raise ToolError(
                f"cannot load CUE4Parse: {exc}. Run `python checktools.py` for a "
                f"breakdown of what the .NET side is missing."
            ) from exc

        cls._types = {
            "DefaultFileProvider": DefaultFileProvider,
            "UTexture2D": UTexture2D,
            "VersionContainer": VersionContainer,
            "EGame": EGame,
            "SearchOption": SearchOption,
            "DirectoryInfo": DirectoryInfo,
            "OodleHelper": OodleHelper,
        }
        return cls._types


class TextureDecoder:
    """
    Cooked `Texture2D` -> editable `.dds`, through CUE4Parse in-process.

    The provider is mounted on the container directory (the `.utoc`/`.ucas`/`.pak`
    set itself): CUE4Parse reads IoStore/Zen natively, so the container never has to
    be unpacked first. One provider is mounted per container and reused for every
    asset in it, so the mount cost is paid once. Each texture is written as a `.dds`
    holding the raw BC mip chain plus a `<name>.dds.json` sidecar recording pixel
    format and mip layout. Mip bytes are copied, never re-encoded, so the patch pass
    can splice an edited mip straight back into the cooked payload.

    `save_package` is the other half: the cooked bytes of one package, for the assets
    an edit actually reached. It is what the patch pass splices edited mips into.

    CUE4Parse is read-only; nothing here writes back into the container.
    """

    def __init__(self, mount: Path, skip_existing: bool = SKIP_EXISTING) -> None:
        #: Directory holding the container set, used as the provider's mount point.
        self.mount = mount
        #: Leave a cooked file that is already on disk alone; see `save_package`.
        self.skip_existing = skip_existing
        self._provider = None

    @property
    def provider(self):
        """Mounted CUE4Parse provider over the container set, created on first use."""
        if self._provider is None:
            t = CUE4Parse.types()
            versions = t["VersionContainer"](getattr(t["EGame"], UE_VERSION))

            # The game's global container rides along as an extra mount so IoStore
            # packages can resolve their script objects; without it every one of them
            # fails to serialize. The overload taking extra directories is only
            # selected when there is one, since it is absent for legacy `.pak` sets.
            extra = global_dir()
            directory = t["DirectoryInfo"](str(self.mount))
            if extra:
                provider = t["DefaultFileProvider"](
                    directory,
                    [t["DirectoryInfo"](extra)],
                    t["SearchOption"].AllDirectories,
                    True,
                    versions,
                )
            else:
                provider = t["DefaultFileProvider"](
                    directory, t["SearchOption"].AllDirectories, True, versions
                )
            provider.Initialize()
            if AES_KEY:
                provider.SubmitKey(AES_KEY)
            provider.Mount()
            log.debug("mounted CUE4Parse provider at %s", self.mount)
            self._provider = provider
        return self._provider

    def packages(self) -> list[str]:
        """Every package in the mounted container, as provider keys, sorted."""
        keys = [str(k) for k in self.provider.Files.Keys]
        return sorted(k for k in keys if k.lower().endswith(".uasset"))

    def decode(
        self, key: str, dest: Path, meta: Path, done: Callable[[str], bool]
    ) -> list[Path]:
        """
        Decode every texture export in one cooked package; returns the .dds written.

        `dest` is the directory the package would own under the scratch tree and `meta`
        the same directory under the durable one, so a scratch texture and its sidecar
        can live apart. A package holding a single texture named after itself gets no
        directory at all: both files land beside it instead (see `flat_dds`), which is
        why every path here, `done` included, is expressed relative to `dest`'s parent
        rather than to `dest`. `done` is asked before any payload is touched, so a
        texture the consumer has already dealt with costs nothing but the package load.
        """
        # Keys come from the provider's own index, so they always address a package.
        pkg_path = key.rsplit(".", 1)[0]
        log.debug("decoding package %s", pkg_path)
        package = self.provider.LoadPackage(pkg_path)

        # Every texture export is found before any is written: whether the package is
        # flat depends on how many there are, and the first path built already needs
        # the answer.
        textures = [(e, m) for e in package.GetExports()
                    if (m := self._mips_of(e)) is not None]
        flat = (len(textures) == 1
                and str(textures[0][0].Name) == PurePosixPath(pkg_path).name)

        # Directories are created by `_write_texture`, not here: a package that decodes
        # nothing (no texture export, or no reachable mip) must not leave an empty
        # `-extracted` directory behind in the output tree.
        written: list[Path] = []
        taken: set[str] = set()
        for export, mips in textures:
            # Two exports of one package can share a name. They would otherwise write
            # the same .dds, leaving the consumer the second one's bytes under the
            # first one's name, so a repeat is suffixed rather than allowed to collide.
            name = str(export.Name)
            if name in taken:
                name = f"{name}-{len(taken)}"
                log.warning("%s: duplicate export name %s, writing as %s",
                            pkg_path, export.Name, name)
            taken.add(name)
            dds_path = flat_dds(dest, name, flat)
            if done(dds_path.relative_to(dest.parent).as_posix()):
                continue
            written.append(self._write_texture(
                export, mips, name, dds_path, flat_dds(meta, name, flat), pkg_path
            ))
        log.debug("%s: %d texture(s) decoded", pkg_path, len(written))
        return sorted(written)

    def save_package(self, key: str, root: Path) -> list[Path]:
        """
        Write one package's cooked files below `root`, keyed by their package path.

        The provider hands back the chunks as they sit in the container (Zen format:
        `.uasset` plus `.ubulk`, no `.uexp`), which is what `retoc unpack` would have
        produced from the whole container -- one package at a time, and without
        unpacking or re-extracting anything.

        Every chunk is written beside the package's own `.uasset`, under the package path
        `key` gives, rather than at whatever path the provider returned for it. UE resolves
        a payload as a sibling of the asset that references it, so siblings they have to
        be, and the returned paths do not always agree: `SavePackage` keys the `.uasset`
        and `.ubulk` off the provider index but finds an optional (`.uptnl`) payload
        through the owning container's own index instead, which is mounted somewhere else
        entirely. Trusting that key drops the `.uptnl` at the root of the store, where
        nothing reading the package can find it.
        """

        ok, data = self.provider.TrySavePackage(key)
        if not ok:
            raise RuntimeError(f"no cooked payload for {key}")

        # The package path is the anchor, and it is the one path here that is known to
        # address the package: it is what the caller looked the package up by.
        package = PurePosixPath(to_posix(key))
        if package.is_absolute() or ".." in package.parts:
            raise RuntimeError(f"unsafe package path {key!r}")

        written: list[Path] = []
        for name in data.Keys:
            # Only the file name is taken from the chunk's own path, so several payloads
            # of one package stay distinct; the directory always comes from the package.
            # A name is still a name and ends up joined onto a durable directory, so
            # anything that could climb out of it is refused rather than trusted.
            chunk = PurePosixPath(to_posix(str(name))).name
            if not chunk or chunk in (".", "..") or "/" in chunk:
                raise RuntimeError(f"unsafe payload name {name!r} in {key}")
            if not chunk.startswith(package.stem):
                log.warning("%s: payload %s does not belong to this package", key, name)
            rel = package.parent / chunk
            path = root / rel
            if self.skip_existing and path.is_file():
                log.debug("cooked %s already saved", rel)
                continue
            write_atomic(path, bytes(data[name]))
            log.debug("cooked %s (%d bytes)", rel, path.stat().st_size)
            written.append(path)
        return written

    # -- extraction from the object model -------------------------------------

    @staticmethod
    def _mips_of(export):
        """
        Mip list of a 2D texture export, or None for anything else.

        A cube, array or volume texture packs several surfaces into one mip payload,
        which a plain 2D DDS cannot describe: written as one it would come out as a
        single face at the wrong dimensions, so those are skipped whole. The test is
        the .NET type rather than the export name, since `UTextureCube` and
        `UTexture2DArray` derive from `UTexture` while every genuinely 2D class
        (`UShadowMapTexture2D`, `UTextureFlipBook`, ...) derives from `UTexture2D`.
        """
        mips = getattr(getattr(export, "PlatformData", None), "Mips", None)
        if not mips:
            return None
        if not isinstance(export, CUE4Parse.types()["UTexture2D"]):
            log.info("skipping %s: %s is not a 2D texture", export.Name, export.ExportType)
            return None
        return mips

    def _write_texture(
        self, export, mips, name: str, dds_path: Path, meta_dds: Path, pkg_path: str
    ) -> Path:
        """
        Write one export to `dds_path`, plus its sidecar beside `meta_dds`.

        `meta_dds` is where the same texture would sit in the durable tree; only the
        sidecar is written there, under that name plus `.json`.
        """
        pixel_format = str(export.PlatformData.PixelFormat)
        if pixel_format not in PIXEL_FORMATS:
            raise RuntimeError(f"unsupported pixel format {pixel_format} in {pkg_path}")
        # The colour space lives in the texture, not in the pixel format, and has to
        # travel with it: a resize of an sRGB map filtered as linear shifts its colours.
        srgb = bool(getattr(export, "SRGB", False))

        # Marshal each mip out of .NET once; bytes(...) copies the managed array.
        # A mip can parse yet carry no payload: an optional (`.uptnl`) or streamed one
        # lives in a chunk that this mount does not have, and reads back as null.
        kept, payloads = [], []
        for mip in mips:
            data = mip.BulkData.Data
            if data is None:
                log.warning(
                    "%s: %dx%d mip of %s has no payload here (%s), skipped",
                    pkg_path, mip.SizeX, mip.SizeY, export.Name, mip.BulkData.BulkDataFlags,
                )
                continue
            kept.append(mip)
            payloads.append(bytes(data))

        # A DDS derives every level from the top one, so the chain has to be whole:
        # a gap left by a dropped mip, or a payload that disagrees with its own
        # dimensions, would silently shift every level after it. The chain is cut at
        # that point instead, which costs the tail but keeps what is written exact.
        sizes = [(m.SizeX, m.SizeY, len(p)) for m, p in zip(kept, payloads)]
        usable, reason = mip_chain_prefix(pixel_format, sizes)
        if usable < len(kept):
            log.warning(
                "%s: %s chain cut to %d of %d mips: %s",
                pkg_path, export.Name, usable, len(kept), reason,
            )
        kept, payloads = kept[:usable], payloads[:usable]
        if not payloads:
            raise RuntimeError(f"no usable mip payload for {export.Name} in {pkg_path}")

        mips, top = kept, kept[0]
        dds_path.parent.mkdir(parents=True, exist_ok=True)
        meta_dds.parent.mkdir(parents=True, exist_ok=True)
        log.debug(
            "texture %s (%s%s %dx%d, %d mips)",
            name,
            pixel_format,
            " sRGB" if srgb else "",
            top.SizeX,
            top.SizeY,
            len(mips),
        )
        dds_path.write_bytes(dds_bytes(pixel_format, top.SizeX, top.SizeY, payloads))

        # Sidecar: everything the write-back pass needs to place edited mips back into
        # the cooked payload. `offset` is into the .dds, so a resized edit can be
        # located even when its length no longer matches the original.
        offset = DDS_HEADER_SIZE + 4 + 20
        records = []
        for mip, payload in zip(mips, payloads):
            records.append({
                "width": mip.SizeX,
                "height": mip.SizeY,
                "size": len(payload),
                "dds_offset": offset,
            })
            offset += len(payload)

        sidecar = {
            "package": pkg_path,
            "export": str(export.Name),
            "pixel_format": pixel_format,
            # The two disagree on purpose for a colour map: `dxgi_format` is what the
            # .dds header really says, `srgb` what the cooked texture says, and the
            # patch pass restores the flag from the latter.
            "srgb": srgb,
            "dxgi_format": dxgi_of(pixel_format),
            "mips": records,
        }
        sidecar_path = meta_dds.with_name(meta_dds.name + ".json")
        # Rewritten whenever it would not come out identical, which covers a truncated
        # leftover (it does not parse) and one an older version wrote with different
        # fields, without needing to know which version that was.
        if read_json(sidecar_path) != sidecar:
            write_json(sidecar_path, sidecar)
        return dds_path


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

class ModWalker:
    """
    Recursive texture walker over a mod folder.

    Yields `(absolute path, path relative to the mod root)`. Every archive adds a
    `<name><ext>-extracted` segment, and so does a cooked asset holding more than one
    texture, so a nested one reads as
    `abc.7z-extracted/def.utoc-extracted/Game/Foo.uasset-extracted/Bar.dds`. A package
    holding a single texture named after itself is flattened to
    `abc.7z-extracted/def.utoc-extracted/Game/Foo.uasset-extracted.dds` instead.

    The consumer owns `edit_root_dir`: it writes its edited version of a yielded file there,
    under the same relative path and the same name, before asking for the next file.
    The walker follows behind it. Every texture gets its `.dds.json` sidecar, and a
    container handed over whole gets a `.uewalker-done` marker; beyond that, only an
    edit makes anything worth storing. When one appears, the cooked package it was
    decoded from is written under `asset_root_dir`, whose tree mirrors the edit one,
    and with `backup` on the untouched original is kept beside the edit as
    `backup-<name>`. An unedited texture costs nothing but its sidecar.

    With `skip_existing` on, `edit_root_dir` is read back as the record of previous walks.
    A container whose marker is there is skipped before its payload leaves the
    archive; inside one that was interrupted, a file already sitting under its own
    relative path is skipped too, so its texture is never decoded and its member never
    extracted. Interrupted walks resume with it.

    Lifetime: everything yielded is temporary. Extracted members and decoded textures
    land in the walk's scratch tree, and each is unlinked as the next file is yielded,
    so a consumer has to do its work before asking for the next item. Container
    triplets are temporary too, dropped as soon as this container's assets are decoded,
    so one container's raw payload is the peak. Extraction is batched per group
    (`selective` trades that back for one member at a time). Nothing is extracted or
    delivered twice in one walk. Loose images already on disk are yielded in place and
    never copied or unlinked, so consumers edit the real mod file.

    The scratch tree is released when the walk ends, however it ends: exhausted,
    closed, abandoned, killed by SIGTERM, or left behind by a process that crashed
    outright (the next walk sweeps that one). `close` releases it on demand, and the
    walker doubles as a context manager.

    Errors from a single archive, container or asset are logged and skipped.
    """

    def __init__(
        self,
        source_root_dir: str | Path = SOURCE_ROOT_DIR,
        edit_root_dir: str | Path = EDIT_ROOT_DIR,
        asset_root_dir: str | Path = UASSET_DIR,
        selective: bool = False,
        backup: bool = BACKUP,
        skip_existing: bool = SKIP_EXISTING,
    ) -> None:
        self.root = Path(source_root_dir).resolve()
        self.out = Path(edit_root_dir).resolve()
        #: Cooked packages behind the edits, mirroring `out`'s tree. Only written to
        #: where an edit appeared, and the only durable output that is not next to it.
        self.assets = Path(asset_root_dir).resolve()
        #: One extract call per member instead of one per group. Bounds peak disk on a
        #: huge archive, at the cost of re-decoding a solid archive once per member.
        self.selective = selective
        #: Keep an untouched `backup-<name>` copy of every file an edit reached.
        self.backup = backup
        #: Resume mode: anything already sitting in `out` under its yielded relative
        #: path counts as done, and is neither redone nor delivered again.
        self.skip_existing = skip_existing
        #: Relative paths already handed to the consumer, and container sets already
        #: walked, so nothing is extracted or delivered twice in one walk.
        self._seen: set[str] = set()
        self._done_containers: set[str] = set()
        #: Container assets whose cooked payload has been written, keyed by the asset's
        #: relative path rather than its package path: a patch container can carry its
        #: own version of a package a base container also holds.
        self._cooked: set[str] = set()
        #: Failures swallowed by `_guard`. Read as a delta, so a container that lost
        #: an asset is not marked done and is retried by the next walk.
        self._failed = 0
        #: The scratch file last yielded, unlinked when the next one is delivered.
        #: Loose mod images are yielded in place and never enter this.
        self._live: Path | None = None
        #: This walk's scratch tree, live only while iterating. See `close`.
        self.tmp_root: Path | None = None

    # -- public API ---------------------------------------------------------

    def __iter__(self) -> Iterator[WalkItem]:
        require_tools()
        log.info("walking mod root %s -> %s", self.root, self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        # Anything a previous run died holding is dead weight now: reclaim the disk
        # before this walk starts filling it again.
        sweep_stale_scratch()
        with scratch_guard():
            self.tmp_root = scratch_dir()
            #: Everything handed to the consumer, mirroring the yielded relative path.
            self.work = self.tmp_root / "delivered"
            self._live = None
            try:
                yield from self._walk_disk()
            finally:
                self.close()

    def close(self) -> None:
        """
        Drop this walk's scratch tree now.

        The walk does this for itself when it ends, is exhausted or is closed, and
        `atexit` and `scratch_guard` cover the paths where none of that happens. This
        is for a consumer that abandons the iterator and would rather not wait for the
        garbage collector to notice. Calling it mid-walk pulls the tree out from under
        the iteration; do not then ask for another file.
        """
        if self.tmp_root is not None:
            self._live = None
            release_scratch(self.tmp_root)
            self.tmp_root = None

    def __enter__(self) -> ModWalker:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- layer 1: the mod folder on disk -------------------------------------

    def _walk_disk(self) -> Iterator[WalkItem]:
        """
        Walk the mod folder: its archives and loose images, then its loose containers.

        A container found on disk is collected rather than walked where it is met,
        since a set only mounts whole and its members can be met in any order. Grouping
        is the archive layer's, keyed on directory and patch family alike.
        """
        loose: list[str] = []
        for child in sorted(self.root.rglob("*")):
            if not child.is_file():
                continue
            # The output trees may sit inside the mod root: the consumer's edits, the
            # backups and the cooked sources are this walk's own product, not mod
            # content to walk again.
            if (self.out in child.parents or self.assets in child.parents
                    or child.name.startswith(BACKUP_PREFIX)):
                continue
            rel = child.relative_to(self.root).as_posix()
            log.debug("disk: %s", rel)
            if child.suffix.lower() in SEVENZIP_EXTS:
                with self._guard(rel):
                    yield from self._walk_7z(child, archive_segment(rel))
            elif ext_of(rel) in UE_EXTS:
                loose.append(rel)
            elif is_image(child.name):
                yield from self._deliver(child, rel)

        source = DiskSource(self.root)
        for group in group_ue_members(loose):
            with self._guard(group.segment):
                yield from self._walk_ue_group(source, group, "")

    # -- layer 2: a .7z archive ----------------------------------------------

    def _walk_7z(self, archive: Path, prefix: str) -> Iterator[WalkItem]:
        """
        Walk one `.7z`: its loose images first, then each container set in turn.

        py7zr reopens the archive per `extract` call, and a solid archive re-decodes
        every block preceding the members asked for. Calls are therefore batched: one
        for all images, one per container set. Never one per member, unless
        `selective` is set for a caller that would rather trade time for peak disk.
        """
        log.info("archive %s", prefix)
        source = SevenZipSource(archive)
        members = source.list_members()
        images = [m for m in members if is_image(m)]
        containers = group_ue_members(m for m in members if ext_of(m) in UE_EXTS)

        log.debug(
            "%s: %d image(s), %d container set(s)", prefix, len(images), len(containers)
        )
        yield from self._emit(source, images, prefix)

        for group in containers:
            with self._guard(join_rel(prefix, group.segment)):
                yield from self._walk_ue_group(source, group, prefix)

    def _walk_ue_group(
        self, source: SevenZipSource | DiskSource, group: UEContainerSet, prefix: str
    ) -> Iterator[WalkItem]:
        """
        Materialise one container set out of its source, then decode straight out of it.

        The marker is checked first and written last, so a container is extracted at
        most once across all walks: hundreds of megabytes of payload for a set whose
        textures are already dealt with. It is written only when the group was walked
        to its end with nothing swallowed by `_guard`, so a lost asset (or an abandoned
        iterator) leaves the container to be retried rather than silently dropped.
        """
        set_prefix = join_rel(prefix, group.segment)
        if not self._claim(self._done_containers, set_prefix, "container"):
            return
        marker = self.out / set_prefix / DONE_MARKER
        if self.skip_existing and marker.exists():
            log.info("skipping container %s: already processed", set_prefix)
            return

        log.info("container %s", set_prefix)
        # The triplet is temp-only: the mod already holds it (as archive members, or
        # as the files these are links to), and the patch pass re-extracts the one
        # container it needs. It has to outlive the decode, though, since the decoder
        # reads the container directly rather than an unpacked tree. Still one
        # container's worth at a time.
        set_dir = Path(tempfile.mkdtemp(dir=self.tmp_root))
        failed = self._failed
        try:
            source.extract(group.internal_names(), set_dir)
            yield from self._walk_assets(set_dir, set_prefix)
        finally:
            shutil.rmtree(set_dir, ignore_errors=True)

        if self._failed == failed:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()

    # -- layer 3: cooked assets ----------------------------------------------

    def _walk_assets(self, mount: Path, prefix: str) -> Iterator[WalkItem]:
        """Decode every package in the container at `mount`, one at a time."""
        # Mounts once, reused for every asset below.
        decoder = TextureDecoder(mount, self.skip_existing)
        cooked = self.assets / prefix
        # Assets come from the provider's own index, which is the only authority on
        # what the container holds and how its packages are addressed.
        assets = decoder.packages()
        log.debug("%s: %d cooked asset(s)", prefix, len(assets))
        for asset in assets:
            asset_rel = join_rel(prefix, archive_segment(asset))
            # A single-texture package writes its .dds where its directory would have
            # been, so both the resume check and the delivered path are built against
            # that directory's parent rather than against the directory itself.
            parent_rel = str(PurePosixPath(asset_rel).parent)
            work_dir = self.work / asset_rel
            # Saved at most once even when several edited exports share the package:
            # its `.ubulk` is the whole mip chain, so a repeat is megabytes of nothing.
            # Both captures are explicit: this runs when the consumer asks for the next
            # file, and reading the loop variables then would be reading a moving target.
            def save_cooked(key: str = asset, claim: str = asset_rel) -> None:
                if self._claim(self._cooked, claim, "cooked package"):
                    decoder.save_package(key, cooked)

            with self._guard(asset_rel):
                # The .dds is scratch; its sidecar is durable, so the patch pass still
                # knows where each mip goes. `_done` runs inside the decoder, before a
                # skipped texture's mips are marshalled out of .NET at all.
                for image in decoder.decode(
                    asset,
                    work_dir,
                    self.out / asset_rel,
                    lambda rel: self._done(join_rel(parent_rel, rel)),
                ):
                    yield from self._deliver(
                        image,
                        join_rel(parent_rel,
                                 image.relative_to(work_dir.parent).as_posix()),
                        save_cooked,
                    )

    # -- emission -------------------------------------------------------------

    def _emit(
        self, source: SevenZipSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """Extract and yield `members` into the output tree, selectively or in one batch."""

        if self.skip_existing:
            # Filtered before extraction, not at delivery: the point of resuming is to
            # not pay for the member again, and py7zr charges per member asked for.
            members = [m for m in members if not self._done(join_rel(prefix, m))]
        if not members:
            return
        dest = self.work / prefix
        if self.selective:
            yield from self._emit_selective(source, members, dest, prefix)
        else:
            yield from self._emit_batch(source, members, dest, prefix)

    def _emit_selective(
        self, source: SevenZipSource, members: list[str], dest: Path, prefix: str
    ) -> Iterator[WalkItem]:
        """One member per extraction: cheapest on disk, costliest on a solid archive."""

        for internal in members:
            with self._guard(join_rel(prefix, internal)):
                log.debug("member %s", join_rel(prefix, internal))
                source.extract([internal], dest)
                yield from self._deliver(dest / internal, join_rel(prefix, internal))
                continue
            # The guard swallowed a failure: drop whatever half-landed on disk.
            drop(dest / internal)

    def _emit_batch(
        self, source: SevenZipSource, members: list[str], dest: Path, prefix: str
    ) -> Iterator[WalkItem]:
        """One extraction for the whole group; a solid archive is decoded only once."""

        delivered: set[str] = set()
        try:
            with self._guard(prefix):
                log.debug("batch extract of %d member(s) into %s", len(members), prefix)
                source.extract(members, dest)
                for internal in members:
                    for item in self._deliver(dest / internal, join_rel(prefix, internal)):
                        delivered.add(internal)
                        yield item
        finally:
            # A failed or abandoned batch still leaves extracted files behind: only
            # what actually reached the consumer is allowed to stay in the output.
            for internal in members:
                if internal not in delivered:
                    drop(dest / internal)

    # -- work already done ----------------------------------------------------

    @staticmethod
    def _claim(seen: set[str], key: str, what: str) -> bool:
        """True the first time `key` is claimed; logs and returns False on a repeat."""
        if key in seen:
            log.debug("skipping repeated %s %s", what, key)
            return False
        seen.add(key)
        return True

    def _done(self, rel: str) -> bool:
        """True when `skip_existing` and `rel` is already in the output tree."""
        # The consumer writes its result under the relative path it was yielded, and
        # under the same name, so the file being there is the record that it ran.
        if not self.skip_existing or not (self.out / rel).exists():
            return False
        log.info("skipping %s: already in the output", rel)
        return True

    def _deliver(
        self, path: Path | str, rel: str, save_cooked: Callable[[], None] | None = None
    ) -> Iterator[WalkItem]:
        """
        Retire the previously yielded file, hand this one over, then harvest it.

        The file yielded is scratch, and is unlinked the moment the next one is
        delivered. Loose mod images are the exception, yielded in place and left alone.

        Under `skip_existing` a file already in the output is dropped here rather than
        yielded. Textures are caught earlier, in the decoder, so what reaches this is
        an archive member or a loose image.
        """
        path = Path(path)
        if self._done(rel) or not self._claim(self._seen, rel, "file"):
            # Nothing was delivered, so nothing will retire the scratch file made for
            # it: on a resumed walk that is every texture in the mod, gigabytes of
            # them, sitting in the scratch tree until the walk ends.
            if self.work in path.parents:
                drop(path)
            return
        # A loose mod image is edited in place, so its original has to be copied out
        # before the consumer sees it. Everything else is scratch and stays untouched
        # while the consumer works, so it can be copied afterwards, and only if needed.
        in_place = self.work not in path.parents
        if self.backup and in_place:
            self._backup(path, rel)
        self._retire()
        if not in_place:  # scratch, so it dies with the next delivery
            self._live = path
        log.info("yielding %s", rel)
        yield str(path), rel

        # Resumed: the consumer has finished with this file and written its result.
        self._harvest(path, rel, in_place, save_cooked)

    def _harvest(
        self,
        path: Path,
        rel: str,
        in_place: bool,
        save_cooked: Callable[[], None] | None,
    ) -> None:
        """
        Keep what the consumer's edit made worth keeping, and nothing else.

        The edit appearing at `rel` is the signal: everything is a patch, so an
        untouched texture must leave nothing behind but its sidecar. The container is
        still mounted and its triplet still on disk at this point (a generator resumes
        innermost first, so this runs before the walk unwinds out of the container),
        which is what lets the cooked package be pulled now instead of re-extracted
        from the mod months later.
        """
        if not (self.out / rel).is_file():
            log.debug("%s: not edited, nothing to keep", rel)
            return
        log.info("harvesting %s", rel)
        if self.backup and not in_place:
            self._backup(path, rel)
        if save_cooked is not None:
            save_cooked()

    def _backup(self, path: Path, rel: str) -> None:
        """Copy a file to `backup-<name>` beside where its edit goes in the output."""
        rel_path = PurePosixPath(rel)
        backup = self.out / rel_path.parent / f"{BACKUP_PREFIX}{rel_path.name}"
        if self.skip_existing and backup.is_file():
            return
        backup.parent.mkdir(parents=True, exist_ok=True)
        log.debug("backup %s", backup)
        shutil.copy2(path, backup)

    def _retire(self) -> None:
        """Unlink the scratch file yielded last; the consumer is done with it."""
        if self._live is not None:
            log.debug("retiring %s", self._live)
            drop(self._live)
            self._live = None

    # -- error handling -------------------------------------------------------

    @contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        """Log and swallow failures from one archive/member so the walk continues."""
        try:
            yield
        except ToolError:
            raise  # a broken tool breaks every remaining file: abort, do not skip
        except Exception as exc:  # noqa: BLE001 - a bad archive must not abort the walk
            # Counted as well as logged: a container that lost an asset here must not
            # be marked done, or the next walk would skip it with the asset missing.
            self._failed += 1
            log.warning("skipping %s: %s: %s", what, type(exc).__name__, exc)
            log.debug("traceback for %s", what, exc_info=True)


def fileIterator(
    backup: bool = BACKUP, skip_existing: bool = SKIP_EXISTING
) -> Iterator[WalkItem]:
    """Convenience wrapper over `ModWalker`; see it for semantics."""
    return iter(
        ModWalker(SOURCE_ROOT_DIR, EDIT_ROOT_DIR, UASSET_DIR, False, backup, skip_existing)
    )


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG if UEWALKER_DEBUG else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    for abs_path, rel_path in fileIterator():
        print(rel_path, "->", abs_path)
