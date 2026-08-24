"""
Walk an Unreal Engine mod folder and yield every editable texture inside it.

Descends recursively: mod root -> .7z archives -> UE containers (.pak / .utoc+.ucas)
-> cooked Zen assets -> decoded .dds. Yielded files are temporary: they live in the
walk's scratch tree and each dies as the next one is produced. `OUT_DIR` keeps only
what outlives the walk, keyed by the same relative path that is yielded: the
`backup-<name>` copies and the cooked trees a later write-back pass needs.
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
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
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

#: Subdirectory of a container's output bundle holding the unpacked cooked assets.
COOKED_DIR = "_cooked"

#: Where a cooked tree is built before it is moved onto `COOKED_DIR`. Anything under
#: this name is the leftover of an interrupted unpack, and is never read.
COOKED_PARTIAL = f"{COOKED_DIR}.partial"

#: Prefix marking an untouched copy. Also excluded from image scans, so a backup is
#: never mistaken for a texture on a later pass over the same output directory.
BACKUP_PREFIX = "backup-"

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

#: Linear DXGI format -> its sRGB twin. A cooked texture carries the colour space in
#: its own SRGB flag rather than in the pixel format, so the two have to be recombined
#: here: a colour map written as plain UNORM decodes, and resizes, as if it were linear.
SRGB_DXGI = {71: 72, 74: 75, 77: 78, 98: 99, 87: 91, 28: 29}

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


def write_json(path: Path, payload: object) -> None:
    """
    Write JSON so an interrupted run cannot leave a half file behind.

    Sidecars outlive the walk and `skip_existing` trusts whatever is already there,
    so a truncated one would be believed on every later run. The rename is atomic
    within a directory: the file is either the previous content or the new one.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def read_json(path: Path) -> object | None:
    """Parsed contents of a JSON file, or None if it is missing or truncated."""
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def tree_state(root: Path) -> set[tuple[str, int, int]]:
    """Name, size and mtime of every file under `root`: enough to tell a write from a no-op."""
    return {
        (str(p), st.st_size, st.st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file() and (st := p.stat())
    }


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
    `global.ucas` are wanted, and `GAME_PAKS` also holds the game's full content
    (well over a hundred gigabytes), which the provider would otherwise index in
    its entirety: the two files are symlinked into a directory of their own and
    that is mounted instead. Returns None when the global container is unavailable,
    which costs IoStore mods but leaves legacy `.pak` ones working.
    """
    if _GLOBAL_DIR.get("path", ...) is not ...:  # resolved once per process
        return _GLOBAL_DIR["path"]

    _GLOBAL_DIR["path"] = None
    if not GAME_PAKS:
        log.warning("GAME_PAKS is unset: IoStore containers cannot be decoded")
        return None

    paks = Path(GAME_PAKS)
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


@contextmanager
def work_dir(parent: Path) -> Iterator[Path]:
    """Scratch directory under `parent`, removed on exit even if the generator is abandoned."""
    path = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class ToolError(RuntimeError):
    """
    An internal tool is missing or cannot be loaded.

    Distinct from the per-file failures the walk skips: nothing downstream can
    succeed once this is raised, so `ModWalker._guard` lets it through.
    """


def tool_path(configured: str) -> str | None:
    """Resolve a configured binary against PATH, or None if it is not there."""
    return shutil.which(configured) or (configured if Path(configured).is_file() else None)


def require_tool(configured: str, what: str) -> str:
    """Resolved path to one binary, or a fatal `ToolError` naming it."""
    found = tool_path(configured)
    if found is None:
        log.error("%s not found: %r (see UEWalkerConfig)", what, configured)
        raise ToolError(f"{what} not found: {configured!r}")
    return found


def require_tools() -> None:
    """Fail early, and by name, if a configured dependency is missing."""
    require_tool(RETOC_PATH, "container unpacker (retoc)")
    if not Path(CUE4PARSE_DLL).is_file():
        log.error("CUE4Parse.dll not found: %r (see UEWalkerConfig)", CUE4PARSE_DLL)
        raise ToolError(f"CUE4Parse.dll not found: {CUE4PARSE_DLL!r}")
    # REPAK_PATH is deliberately not checked here: it is only reached by a .pak-only
    # set, and pure-IoStore mods never need it. It is still fatal when reached.


def run_tool(argv: list[str]) -> None:
    """
    Run an external tool.

    A tool that cannot be launched at all is a `ToolError` and aborts the walk; a
    tool that ran and rejected its input is an ordinary failure of that one file.
    """
    log.debug("running: %s", " ".join(argv))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True)
    except OSError as exc:
        log.error("cannot run %s: %s", argv[0], exc)
        raise ToolError(f"cannot run {argv[0]}: {exc}") from exc
    if proc.stdout and proc.stdout.strip():
        log.debug("%s stdout: %s", argv[0], proc.stdout.strip())
    if proc.stderr and proc.stderr.strip():
        log.debug("%s stderr: %s", argv[0], proc.stderr.strip())
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{argv[0]} failed ({proc.returncode}): {detail[-1] if detail else 'no output'}"
        )


def dxgi_of(pixel_format: str, srgb: bool) -> int:
    """DXGI code for a UE pixel format, promoted to its sRGB twin where there is one."""
    dxgi = PIXEL_FORMATS[pixel_format][0]
    return SRGB_DXGI.get(dxgi, dxgi) if srgb else dxgi


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


def dds_bytes(
    pixel_format: str, width: int, height: int, mips: list[bytes], srgb: bool = False
) -> bytes:
    """
    Pack raw mip payloads into a DDS with a DX10 header.

    Mip bytes are written through untouched: this is a container swap, not a
    re-encode, so the edit stays reversible against the cooked original.
    """
    dxgi = dxgi_of(pixel_format, srgb)
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

class ArchiveSource(ABC):
    """
    Adapter over one archive file on disk.

    Members are listed without extracting; extraction is then either selective
    (one member at a time) or batch, depending on what the format supports.
    """

    #: False when the backing tool cannot extract a single member cheaply.
    supports_selective = True

    @abstractmethod
    def list_members(self) -> list[str]:
        """All non-directory member names, POSIX-normalised and ordered."""

    @abstractmethod
    def extract(self, members: list[str], dest: Path) -> None:
        """Extract the given members below `dest`, preserving internal structure."""


class SevenZipSource(ArchiveSource):
    """`.7z` archive read through py7zr."""

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


class UEContainerSource(ArchiveSource):
    """
    A UE container set already materialised on disk, unpacked via retoc.

    Always batch: retoc converts a whole container, so single-asset extraction is
    not available at this layer. Output is legacy cooked assets (.uasset/.uexp/.ubulk),
    not images; `TextureDecoder` handles the step after this one.
    """

    supports_selective = False

    def __init__(self, set_dir: Path) -> None:
        self.set_dir = set_dir

    def list_members(self) -> list[str]:
        # Contents are unknown until the container is unpacked.
        raise NotImplementedError("UE containers cannot be listed without unpacking")

    def extract(self, members: list[str], dest: Path) -> None:
        """`members` is ignored: the whole container set is converted/unpacked into `dest`."""
        dest.mkdir(parents=True, exist_ok=True)

        # retoc reads the .ucas alongside its .utoc, so only the index file is named.
        # A patch family is several containers unpacked into one tree, in ascending
        # priority so a patched package overwrites the one it replaces. One of them
        # yielding nothing is normal (a patch carrying only optional mips has no
        # packages of its own), so the set fails only when none of them yielded.
        utocs = sorted(
            self.set_dir.rglob("*.utoc"), key=lambda p: patch_family(p.stem)[1]
        )
        if utocs:
            # `to-legacy` and `unpack` emit different asset formats, and `retoc to-zen`
            # can only repack one of them: a family that came out as both is not a
            # write-backable tree, so it is rejected rather than quietly kept.
            modes = {self._from_iostore(utoc, dest) for utoc in utocs} - {None}
            if not modes:
                raise RuntimeError(f"retoc unpacked no packages from {self.set_dir}")
            if len(modes) > 1:
                raise RuntimeError(
                    f"{self.set_dir}: patch family unpacked as both legacy and Zen assets"
                )
            return

        # No .utoc: the set is already legacy, and retoc speaks IoStore only.
        pak = next(self.set_dir.rglob("*.pak"), None)
        if pak is None:
            raise RuntimeError(f"no .pak/.utoc found in {self.set_dir}")
        repak = require_tool(REPAK_PATH, "legacy .pak unpacker (repak)")
        run_tool([repak, "unpack", "-o", str(dest), str(pak)])

    @staticmethod
    def _from_iostore(utoc: Path, dest: Path) -> str | None:
        """
        Get assets out of an IoStore container, preferring the legacy conversion.

        `to-legacy` is tried first: it emits legacy .uasset/.uexp, which can still be
        rewritten when an edit changes a texture's size, and `retoc to-zen` reverses
        it. It resolves package names through the container header, though, and
        mod-authored containers frequently ship one retoc cannot parse -- it then
        reports `packages: 0` and silently writes nothing at all.

        `unpack` is the fallback: it works off the directory index instead, which
        survives in those containers, at the cost of emitting Zen-format assets.
        """
        aes = ["-a", AES_KEY] if AES_KEY else []
        # `dest` may already hold packages from an earlier container of the same patch
        # family, so "did to-legacy write anything?" has to be asked of the whole tree.
        # Not of the set of package *paths*: a patch container usually replaces the
        # packages it patches, leaving that set unchanged while every file is rewritten.
        before = tree_state(dest)
        # -a is a global option and must precede the subcommand.
        run_tool([RETOC_PATH, *aes, "to-legacy", "--version", RETOC_VERSION,
                  str(utoc), str(dest)])
        if tree_state(dest) != before:
            return "legacy"

        log.info(
            "%s: to-legacy produced no packages (unparsable container header?), "
            "falling back to unpack", utoc.name,
        )
        run_tool([RETOC_PATH, *aes, "unpack", str(utoc), str(dest)])
        return "zen" if tree_state(dest) != before else None


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
    set itself), not on retoc's unpacked tree: CUE4Parse reads IoStore/Zen natively,
    while `retoc unpack` output has no legacy package header and cannot be parsed as
    loose assets. One provider is mounted per container and reused for every asset in
    it, so the mount cost is paid once. Each texture is written as a `.dds` holding the raw BC
    mip chain plus a `<name>.dds.json` sidecar recording pixel format and mip layout.
    Mip bytes are copied, never re-encoded, so a later write-back pass can splice
    edited mips into the retained cooked asset.

    CUE4Parse is read-only; nothing here writes back into the container.
    """

    def __init__(self, mount: Path) -> None:
        #: Directory holding the container set, used as the provider's mount point.
        self.mount = mount
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

    def decode(self, key: str, dest: Path, meta: Path) -> list[Path]:
        """
        Decode every texture export in one cooked package; returns the .dds written.

        `dest` takes the `.dds` the consumer edits, `meta` the sidecars the write-back
        pass reads, so a scratch texture and its durable metadata can live apart.
        """
        # Keys come from the provider's own index, so they always address a package.
        pkg_path = key.rsplit(".", 1)[0]
        log.debug("decoding package %s", pkg_path)
        package = self.provider.LoadPackage(pkg_path)

        # The two directories are created by `_write_texture`, not here: a package that
        # decodes nothing (no texture export, or no reachable mip) must not leave an
        # empty `-extracted` directory behind in the output tree.
        written: list[Path] = []
        taken: set[str] = set()
        for export in package.GetExports():
            mips = self._mips_of(export)
            if mips is None:
                continue
            # Two exports of one package can share a name. They would otherwise write
            # the same .dds, leaving the consumer the second one's bytes under the
            # first one's name, so a repeat is suffixed rather than allowed to collide.
            name = str(export.Name)
            if name in taken:
                name = f"{name}-{len(taken)}"
                log.warning("%s: duplicate export name %s, writing as %s",
                            pkg_path, export.Name, name)
            taken.add(name)
            written.append(self._write_texture(export, mips, name, dest, meta, pkg_path))
        log.debug("%s: %d texture(s) decoded", pkg_path, len(written))
        return sorted(written)

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
        self, export, mips, name: str, dest: Path, meta: Path, pkg_path: str
    ) -> Path:
        """Write one export as `<name>.dds` in `dest` plus its sidecar in `meta`."""
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
        dest.mkdir(parents=True, exist_ok=True)
        meta.mkdir(parents=True, exist_ok=True)
        dds_path = dest / f"{name}.dds"
        log.debug(
            "texture %s (%s%s %dx%d, %d mips)",
            name,
            pixel_format,
            " sRGB" if srgb else "",
            top.SizeX,
            top.SizeY,
            len(mips),
        )
        dds_path.write_bytes(dds_bytes(pixel_format, top.SizeX, top.SizeY, payloads, srgb))

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
            "srgb": srgb,
            "dxgi_format": dxgi_of(pixel_format, srgb),
            "mips": records,
        }
        sidecar_path = meta / f"{dds_path.name}.json"
        # An existing sidecar is kept, but only if it is one this version wrote: a
        # truncated leftover does not parse, and one written before the colour space
        # was recorded has no `srgb` key and the wrong `dxgi_format` for an sRGB map.
        current = read_json(sidecar_path)
        if not (SKIP_EXISTING and isinstance(current, dict) and "srgb" in current):
            write_json(sidecar_path, sidecar)
        return dds_path


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

class ModWalker:
    """
    Recursive texture walker over a mod folder.

    Yields `(absolute path, path relative to the mod root)`. Every archive adds a
    `<name><ext>-extracted` segment, and a cooked asset adds one too, so a nested
    texture reads as `abc.7z-extracted/def.utoc-extracted/Game/Foo.uasset-extracted/Foo.dds`.

    With `backup` on, an untouched `backup-<name>` copy of every yielded file is kept
    in the matching `out_dir` directory before the consumer sees it.

    With `skip_existing` on, `out_dir` is read as the record of a previous walk: an
    image already there under its own relative path is skipped (not extracted, not
    yielded), a cooked tree is unpacked only when it is missing, and an existing
    sidecar is kept. Interrupted walks resume with it; it is off by default, since a
    consumer that writes its results elsewhere would see nothing skipped anyway.

    Lifetime: everything yielded is temporary. Extracted members and decoded textures
    land in the walk's scratch tree, and each is unlinked as the next file is yielded,
    so a consumer has to do its work before asking for the next item. Container
    triplets are temporary too, dropped as soon as this container's assets are decoded,
    so one container's raw payload is the peak. Extraction is batched per group
    (`selective` trades that back for one member at a time). Nothing is extracted or
    delivered twice in one walk. `out_dir` keeps only what outlives the walk, under the
    yielded relative path: the backups, and the cooked assets a later write-back pass
    splices into. Loose images already on disk are yielded in place and never copied or
    unlinked, so consumers edit the real mod file.

    The scratch tree is released when the walk ends, however it ends: exhausted,
    closed, abandoned, killed by SIGTERM, or left behind by a process that crashed
    outright (the next walk sweeps that one). `close` releases it on demand, and the
    walker doubles as a context manager.

    Errors from a single archive, container or asset are logged and skipped.
    """

    def __init__(
        self,
        mod_root: str | Path = MOD_ROOT,
        out_dir: str | Path = OUT_DIR,
        selective: bool = False,
        backup: bool = BACKUP,
        skip_existing: bool = SKIP_EXISTING,
    ) -> None:
        self.root = Path(mod_root).resolve()
        self.out = Path(out_dir).resolve()
        #: One extract call per member instead of one per group. Bounds peak disk on a
        #: huge archive, at the cost of re-decoding a solid archive once per member.
        self.selective = selective
        self.backup = backup
        #: Resume mode: anything already sitting in `out` under its yielded relative
        #: path counts as done, and is neither redone nor delivered again.
        self.skip_existing = skip_existing
        #: Relative paths already handed to the consumer, and container sets already
        #: unpacked, so nothing is extracted or delivered twice in one walk.
        self._seen: set[str] = set()
        self._done_containers: set[str] = set()
        #: Files skipped as already present in `out`. Only read as a delta, to tell a
        #: container that decoded nothing from one whose textures were all done before.
        self._skipped = 0
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
        for child in sorted(self.root.rglob("*")):
            if not child.is_file():
                continue
            # The output tree may sit inside the mod root: its backups and decoded
            # textures are this walk's own product, not mod content to walk again.
            if self.out in child.parents or child.name.startswith(BACKUP_PREFIX):
                continue
            rel = child.relative_to(self.root).as_posix()
            log.debug("disk: %s", rel)
            if child.suffix.lower() in SEVENZIP_EXTS:
                with self._guard(rel):
                    yield from self._walk_7z(child, archive_segment(rel))
            elif is_image(child.name):
                yield from self._deliver(child, rel)

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
        self, source: ArchiveSource, group: UEContainerSet, prefix: str
    ) -> Iterator[WalkItem]:
        """Materialise one container set out of the .7z, then descend into it."""
        set_prefix = join_rel(prefix, group.segment)
        if not self._claim(self._done_containers, set_prefix, "container"):
            return

        # The triplet is temp-only: `retoc to-zen` rebuilds it from the cooked tree on
        # the way back, so persisting it would just duplicate the mod. It has to outlive
        # retoc, though: the decoder reads the container directly, so it is dropped only
        # once this container's assets are done. Still one container's worth at a time.
        set_dir = Path(tempfile.mkdtemp(dir=self.tmp_root))
        try:
            source.extract(group.internal_names(), set_dir)
            yield from self._walk_ue(set_dir, set_prefix)
        finally:
            shutil.rmtree(set_dir, ignore_errors=True)

    # -- layer 3: a UE container set -----------------------------------------

    def _unpack_ue(self, set_dir: Path, prefix: str) -> Path:
        """Unpack the container into its output bundle for write-back; returns the root."""
        log.info("container %s", prefix)
        cooked = self.out / prefix / COOKED_DIR
        # A cooked tree only ever depends on the container it came from, so an existing
        # one is the same tree retoc would write again; unpacking is the expensive half.
        if self.skip_existing and cooked.is_dir() and any(cooked.rglob("*")):
            log.info("%s: cooked tree already unpacked", prefix)
            return cooked

        # retoc merges into its output directory instead of replacing it, so a tree a
        # killed run left half written would be merged into rather than redone -- and
        # nothing in its contents says which kind it is. It is built under a name of
        # its own and moved into place in one rename, so `_cooked` only ever exists
        # finished: the remains of an interrupted run are the `.partial` directory,
        # which is dropped here rather than read. The old tree is replaced only once
        # the new one is whole, so a failed unpack leaves what was already there.
        partial = cooked.with_name(COOKED_PARTIAL)
        drop(partial)
        UEContainerSource(set_dir).extract([], partial)
        drop(cooked)
        os.replace(partial, cooked)
        return cooked

    def _walk_ue(self, set_dir: Path, prefix: str) -> Iterator[WalkItem]:
        """
        Unpack a container set for write-back, then decode straight out of it.

        retoc's cooked tree is the write-back target only; the textures come from the
        container through CUE4Parse. Neither side blocks the other: an unusable cooked
        tree is dropped and decoding goes on without it, and if nothing decodes the
        tree is dead weight (nothing to splice back in), so the whole bundle goes.
        """
        # An unusable cooked tree does not stop the textures from being decoded; it
        # only means there is nothing to splice them back into, so it is not kept.
        cooked = None
        with self._guard(join_rel(prefix, COOKED_DIR)):
            cooked = self._unpack_ue(set_dir, prefix)
        if cooked is not None and not any(cooked.rglob("*")):
            cooked = None
        if cooked is None:
            log.warning("%s: no cooked tree, textures will not be write-backable", prefix)
            drop(self.out / prefix / COOKED_DIR)

        delivered = 0
        skipped = self._skipped
        try:
            for item in self._walk_assets(set_dir, prefix):
                delivered += 1
                yield item
        finally:
            # Skips count as deliveries here: a container whose textures were all done
            # by an earlier walk has an output bundle worth keeping, not an empty one.
            if not delivered and self._skipped == skipped:
                log.info("%s: nothing decoded, dropping the output bundle", prefix)
                shutil.rmtree(self.out / prefix, ignore_errors=True)

    # -- layer 4: cooked assets ----------------------------------------------

    def _walk_assets(self, mount: Path, prefix: str) -> Iterator[WalkItem]:
        """Decode every package in the container at `mount`, one at a time."""
        decoder = TextureDecoder(mount)  # mounts once, reused for every asset below
        # Assets come from the provider's own index, which is the only authority on
        # what the container holds and how its packages are addressed.
        assets = decoder.packages()
        log.debug("%s: %d cooked asset(s)", prefix, len(assets))
        for asset in assets:
            asset_rel = join_rel(prefix, archive_segment(asset))
            with self._guard(asset_rel):
                # The .dds is scratch; its sidecar is durable, next to the backup, so
                # the write-back pass still knows where each mip goes.
                for image in decoder.decode(
                    asset, self.work / asset_rel, self.out / asset_rel
                ):
                    yield from self._deliver(image, join_rel(asset_rel, image.name))

    # -- emission -------------------------------------------------------------

    def _emit(
        self, source: ArchiveSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """Extract and yield `members` into the output tree, selectively or in one batch."""

        if self.skip_existing:
            # Filtered before extraction, not at delivery: the point of resuming is to
            # not pay for the member again, and py7zr charges per member asked for.
            members = [m for m in members if not self._done(join_rel(prefix, m))]
        if not members:
            return
        dest = self.work / prefix
        if self.selective and source.supports_selective:
            yield from self._emit_selective(source, members, dest, prefix)
        else:
            yield from self._emit_batch(source, members, dest, prefix)

    def _emit_selective(
        self, source: ArchiveSource, members: list[str], dest: Path, prefix: str
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
        self, source: ArchiveSource, members: list[str], dest: Path, prefix: str
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
        # The real relative path, not the `backup-<name>` copy beside it: what counts
        # as done is the image the consumer wrote back, not the untouched original.
        if not self.skip_existing or not (self.out / rel).exists():
            return False
        log.info("skipping %s: already in the output", rel)
        self._skipped += 1
        return True

    def _deliver(self, path: Path | str, rel: str) -> Iterator[WalkItem]:
        """
        Back the file up if asked, retire the previous one, then hand it over.

        The backup mirrors `rel` under `out` and is the only copy that survives the
        walk: the file yielded is scratch, and is unlinked the moment the next one is
        delivered. Loose mod images are the exception, yielded in place and left alone.

        Under `skip_existing` a file already in the output is dropped here rather than
        yielded, so it is not backed up over either.
        """
        path = Path(path)
        if self._done(rel) or not self._claim(self._seen, rel, "file"):
            # Nothing was delivered, so nothing will retire the scratch file made for
            # it: on a resumed walk that is every texture in the mod, gigabytes of
            # them, sitting in the scratch tree until the walk ends.
            if self.work in path.parents:
                drop(path)
            return
        if self.backup:
            rel_path = PurePosixPath(rel)
            backup = self.out / rel_path.parent / f"{BACKUP_PREFIX}{rel_path.name}"
            backup.parent.mkdir(parents=True, exist_ok=True)
            log.debug("backup %s", backup)
            shutil.copy2(path, backup)
        self._retire()
        if self.work in path.parents:  # scratch, so it dies with the next delivery
            self._live = path
        log.info("yielding %s", rel)
        yield str(path), rel

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
            log.warning("skipping %s: %s: %s", what, type(exc).__name__, exc)
            log.debug("traceback for %s", what, exc_info=True)


def fileIterator(
    backup: bool = BACKUP, skip_existing: bool = SKIP_EXISTING
) -> Iterator[WalkItem]:
    """Convenience wrapper over `ModWalker`; see it for semantics."""
    return iter(ModWalker(MOD_ROOT, OUT_DIR, False, backup, skip_existing))


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG if UEWALKER_DEBUG else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    for abs_path, rel_path in fileIterator():
        print(rel_path, "->", abs_path)
