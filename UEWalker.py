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
import shutil
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


def drop(path: Path) -> None:
    """Remove a file or directory tree, ignoring anything that is not there."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


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
    holder = Path(tempfile.mkdtemp(prefix="uewalker-global-"))
    atexit.register(shutil.rmtree, holder, ignore_errors=True)
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


def dds_bytes(pixel_format: str, width: int, height: int, mips: list[bytes]) -> bytes:
    """
    Pack raw mip payloads into a DDS with a DX10 header.

    Mip bytes are written through untouched: this is a container swap, not a
    re-encode, so the edit stays reversible against the cooked original.
    """
    dxgi, block, compressed = PIXEL_FORMATS[pixel_format]

    # DDSD_CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT, plus LINEARSIZE or PITCH.
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000
    flags |= 0x80000 if compressed else 0x8
    pitch = max(1, (width + 3) // 4) * block if compressed else width * block

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
    A `.pak` / `.utoc` + `.ucas` group sharing one directory and stem.

    UE containers only unpack as a set, so members are collected before extraction.
    """

    dirname: str
    stem: str
    members: dict[str, str] = field(default_factory=dict)  # ext -> archive-internal name

    @property
    def representative_ext(self) -> str:
        """Extension used to name the `-extracted` segment for this set."""
        for e in UE_REPRESENTATIVE_ORDER:
            if e in self.members:
                return e
        raise RuntimeError(f"empty container set {self.dirname}/{self.stem}")

    @property
    def segment(self) -> str:
        """Relative segment this set contributes, e.g. `chars/skin.ucas-extracted`."""
        return join_rel(self.dirname, f"{self.stem}{self.representative_ext}-extracted")

    def internal_names(self) -> list[str]:
        return sorted(self.members.values())


def group_ue_members(internals: Iterable[str]) -> list[UEContainerSet]:
    """Group container member names into ordered `UEContainerSet`s by (directory, stem)."""
    groups: dict[tuple[str, str], UEContainerSet] = {}
    for internal in internals:
        p = PurePosixPath(internal)
        dirname = "" if str(p.parent) == "." else str(p.parent)
        key = (dirname, p.stem)
        group = groups.setdefault(key, UEContainerSet(dirname, p.stem))
        group.members[ext_of(internal)] = internal
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

    def list_members(self) -> list[str]:
        with py7zr.SevenZipFile(self.path, "r") as z:
            infos = z.list()
        names = [
            to_posix(i.filename)
            for i in infos
            if not getattr(i, "is_directory", False)
        ]
        log.debug("%s: %d members", self.path, len(names))
        return sorted(names)

    def extract(self, members: list[str], dest: Path) -> None:
        # py7zr must reopen the archive per call; selective extraction of a solid
        # archive therefore re-decodes preceding blocks (see ModWalker.selective).
        log.debug(
            "extracting %d member(s) from %s -> %s", len(members), self.path, dest
        )
        with py7zr.SevenZipFile(self.path, "r") as z:
            z.extract(path=dest, targets=members)


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
        """`members` is ignored: the whole container is converted/unpacked into `dest`."""
        dest.mkdir(parents=True, exist_ok=True)

        # retoc reads the .ucas alongside its .utoc, so only the index file is named.
        utoc = next(self.set_dir.rglob("*.utoc"), None)
        if utoc is not None:
            self._from_iostore(utoc, dest)
            return

        # No .utoc: the set is already legacy, and retoc speaks IoStore only.
        pak = next(self.set_dir.rglob("*.pak"), None)
        if pak is None:
            raise RuntimeError(f"no .pak/.utoc found in {self.set_dir}")
        repak = require_tool(REPAK_PATH, "legacy .pak unpacker (repak)")
        run_tool([repak, "unpack", "-o", str(dest), str(pak)])

    @staticmethod
    def _from_iostore(utoc: Path, dest: Path) -> None:
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
        # -a is a global option and must precede the subcommand.
        run_tool([RETOC_PATH, *aes, "to-legacy", "--version", RETOC_VERSION,
                  str(utoc), str(dest)])
        if any(dest.rglob("*.uasset")):
            return

        log.info(
            "%s: to-legacy produced no packages (unparsable container header?), "
            "falling back to unpack", utoc.name,
        )
        run_tool([RETOC_PATH, *aes, "unpack", str(utoc), str(dest)])


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

        dest.mkdir(parents=True, exist_ok=True)
        meta.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for export in package.GetExports():
            mips = self._mips_of(export)
            if mips is None:
                continue
            written.append(self._write_texture(export, mips, dest, meta, pkg_path))
        log.debug("%s: %d texture(s) decoded", pkg_path, len(written))
        return sorted(written)

    # -- extraction from the object model -------------------------------------

    @staticmethod
    def _mips_of(export):
        """
        Mip list of an export, or None when it carries none.

        Every export holding mips is treated as a texture; no class check is made,
        since the walk only cares about the payload, not the UObject type.
        """
        mips = getattr(getattr(export, "PlatformData", None), "Mips", None)
        return mips if mips else None

    def _write_texture(self, export, mips, dest: Path, meta: Path, pkg_path: str) -> Path:
        """Write one export as `<name>.dds` in `dest` plus its sidecar in `meta`."""
        pixel_format = str(export.PlatformData.PixelFormat)
        if pixel_format not in PIXEL_FORMATS:
            raise RuntimeError(f"unsupported pixel format {pixel_format} in {pkg_path}")

        # Marshal each mip out of .NET once; bytes(...) copies the managed array.
        payloads = [bytes(mip.BulkData.Data) for mip in mips]
        top = mips[0]
        dds_path = dest / f"{export.Name}.dds"
        log.debug(
            "texture %s (%s %dx%d, %d mips)",
            export.Name,
            pixel_format,
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
            "dxgi_format": PIXEL_FORMATS[pixel_format][0],
            "mips": records,
        }
        (meta / f"{dds_path.name}.json").write_text(json.dumps(sidecar, indent=2))
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

    Errors from a single archive, container or asset are logged and skipped.
    """

    def __init__(
        self,
        mod_root: str | Path = MOD_ROOT,
        out_dir: str | Path = OUT_DIR,
        selective: bool = False,
        backup: bool = BACKUP,
    ) -> None:
        self.root = Path(mod_root).resolve()
        self.out = Path(out_dir).resolve()
        #: One extract call per member instead of one per group. Bounds peak disk on a
        #: huge archive, at the cost of re-decoding a solid archive once per member.
        self.selective = selective
        self.backup = backup
        #: Relative paths already handed to the consumer, and container sets already
        #: unpacked, so nothing is extracted or delivered twice in one walk.
        self._seen: set[str] = set()
        self._done_containers: set[str] = set()
        #: The scratch file last yielded, unlinked when the next one is delivered.
        #: Loose mod images are yielded in place and never enter this.
        self._live: Path | None = None

    # -- public API ---------------------------------------------------------

    def __iter__(self) -> Iterator[WalkItem]:
        require_tools()
        log.info("walking mod root %s -> %s", self.root, self.out)
        self.out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="modextract_") as tmp:
            self.tmp_root = Path(tmp)
            #: Everything handed to the consumer, mirroring the yielded relative path.
            self.work = self.tmp_root / "delivered"
            self._live = None
            yield from self._walk_disk()

    # -- layer 1: the mod folder on disk -------------------------------------

    def _walk_disk(self) -> Iterator[WalkItem]:
        for child in sorted(self.root.rglob("*")):
            if not child.is_file():
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
        UEContainerSource(set_dir).extract([], cooked)
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
        try:
            for item in self._walk_assets(set_dir, prefix):
                delivered += 1
                yield item
        finally:
            if not delivered:
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

    def _deliver(self, path: Path | str, rel: str) -> Iterator[WalkItem]:
        """
        Back the file up if asked, retire the previous one, then hand it over.

        The backup mirrors `rel` under `out` and is the only copy that survives the
        walk: the file yielded is scratch, and is unlinked the moment the next one is
        delivered. Loose mod images are the exception, yielded in place and left alone.
        """
        if not self._claim(self._seen, rel, "file"):
            return
        path = Path(path)
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


def fileIterator(backup: bool = BACKUP) -> Iterator[WalkItem]:
    """Convenience wrapper over `ModWalker`; see it for semantics."""
    return iter(ModWalker(MOD_ROOT, OUT_DIR, False, backup))


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.DEBUG if UEWALKER_DEBUG else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    for abs_path, rel_path in fileIterator():
        print(rel_path, "->", abs_path)
