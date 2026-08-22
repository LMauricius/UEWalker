"""
Walk an Unreal Engine mod folder and yield every editable texture inside it.

Descends recursively: mod root -> .7z archives -> UE containers (.pak / .utoc+.ucas)
-> cooked Zen assets -> decoded .dds. Decoded textures and everything a later
write-back pass needs are written under `OUT_DIR`, keyed by the same relative path
that is yielded. See `ModWalker` / `fileIterator` for the public entry points.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
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

#: Mod folder to walk. Overridden by argv[1] when run as a script.
MOD_ROOT = "/path/to/mod"

#: Durable output tree. Decoded textures, their sidecars and the cooked assets they
#: came from are stored here under the yielded relative path.
OUT_DIR = "/path/to/output"

#: IoStore container unpacker. Prebuilt Linux binaries:
#: https://github.com/trumank/retoc/releases
RETOC_PATH = "retoc"

#: CUE4Parse-based texture decoder. Dumps raw mips as .dds plus a .dds.json sidecar
#: describing pixel format and mip layout, which is what makes the edit reversible.
#: No prebuilt CLI exists; build a small .NET console app against the library:
#: https://github.com/FabianFG/CUE4Parse (FModel, its GUI: https://fmodel.app)
DECODER_PATH = "cue4parse-cli"

#: AES key for encrypted containers. Mod-authored containers are normally plain.
AES_KEY: str | None = None

#: Engine version passed to the decoder; cooked assets are not self-describing.
UE_VERSION = "GAME_UE4_26"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
SEVENZIP_EXTS = {".7z"}
UE_EXTS = {".pak", ".ucas", ".utoc"}

#: Subdirectory of a container's output bundle holding the unpacked cooked assets.
COOKED_DIR = "_cooked"

# Which container member represents the set in the output path, most specific first.
UE_REPRESENTATIVE_ORDER = (".ucas", ".pak", ".utoc")

# (absolute file path, path relative to the mod root)
WalkItem = tuple[str, str]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

def ext_of(name: str) -> str:
    """Lowercase suffix of a path-like name, `.dds` style (empty string if none)."""
    return PurePosixPath(name).suffix.lower()


def is_image(name: str) -> bool:
    """True if the name has an extension we treat as an editable texture."""
    return ext_of(name) in IMAGE_EXTS


def to_posix(internal: str) -> str:
    """Normalise an archive-internal member name to forward slashes."""
    return internal.replace("\\", "/")


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


@contextmanager
def work_dir(parent: Path) -> Iterator[Path]:
    """Scratch directory under `parent`, removed on exit even if the generator is abandoned."""
    path = Path(tempfile.mkdtemp(dir=parent))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def require_tools() -> None:
    """Fail early, and by name, if a configured external tool is not on PATH."""
    for label, tool in (
        ("container unpacker", RETOC_PATH),
        ("texture decoder", DECODER_PATH),
    ):
        if shutil.which(tool) is None and not Path(tool).is_file():
            raise FileNotFoundError(
                f"{label} not found: {tool!r} (see the globals at the top)"
            )


def run_tool(argv: list[str]) -> None:
    """Run an external tool, raising with its own diagnostics attached on failure."""
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(
            f"{argv[0]} failed ({proc.returncode}): {detail[-1] if detail else 'no output'}"
        )


def images_under(root: Path) -> list[Path]:
    """All image files below `root`, ordered."""
    return sorted(p for p in root.rglob("*") if p.is_file() and is_image(p.name))


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
        return sorted(names)

    def extract(self, members: list[str], dest: Path) -> None:
        # py7zr must reopen the archive per call; selective extraction of a solid
        # archive therefore re-decodes preceding blocks (see ModWalker.selective).
        with py7zr.SevenZipFile(self.path, "r") as z:
            z.extract(path=dest, targets=members)


class UEContainerSource(ArchiveSource):
    """
    A UE container set already materialised on disk, unpacked via retoc.

    Always batch: retoc unpacks a whole container, so single-asset extraction is
    not available at this layer. Output is cooked Zen assets (.uasset/.uexp/.ubulk),
    not images; `TextureDecoder` handles the step after this one.
    """

    supports_selective = False

    def __init__(self, set_dir: Path) -> None:
        self.set_dir = set_dir

    def list_members(self) -> list[str]:
        # Contents are unknown until the container is unpacked.
        raise NotImplementedError("UE containers cannot be listed without unpacking")

    def extract(self, members: list[str], dest: Path) -> None:
        """`members` is ignored: the whole container is unpacked into `dest`."""
        # retoc reads the .ucas alongside its .utoc, so only the index file is named.
        # A legacy .pak without an .utoc is passed directly.
        entry = next(self.set_dir.rglob("*.utoc"), None) or next(
            self.set_dir.rglob("*.pak"), None
        )
        if entry is None:
            raise RuntimeError(f"no .pak/.utoc found in {self.set_dir}")

        dest.mkdir(parents=True, exist_ok=True)
        argv = [RETOC_PATH, "unpack", str(entry), str(dest)]
        if AES_KEY:
            argv += ["--aes-key", AES_KEY]
        run_tool(argv)


class TextureDecoder:
    """
    Cooked `Texture2D` -> editable `.dds`, through the CUE4Parse-based decoder.

    The decoder writes, per texture, a `.dds` holding the raw BC mip chain and a
    `<name>.dds.json` sidecar describing pixel format and mip layout. The sidecar
    plus the retained cooked asset are what a later write-back pass splices against;
    exporting a flat .png here would make the edit irreversible.
    """

    def __init__(self, mount: Path) -> None:
        #: Root of the unpacked cooked tree, needed as the decoder's mount point.
        self.mount = mount

    def decode(self, asset: Path, dest: Path) -> list[Path]:
        """Decode one cooked asset into `dest`; returns the images produced, ordered."""
        dest.mkdir(parents=True, exist_ok=True)
        argv = [
            DECODER_PATH,
            "--mount",
            str(self.mount),
            "--package",
            asset.relative_to(self.mount).as_posix(),
            "--game",
            UE_VERSION,
            "--out",
            str(dest),
        ]
        if AES_KEY:
            argv += ["--aes-key", AES_KEY]
        run_tool(argv)
        # A package may hold several textures, so every image produced is reported.
        return images_under(dest)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

class ModWalker:
    """
    Recursive texture walker over a mod folder.

    Yields `(absolute path, path relative to the mod root)`. Every archive adds a
    `<name><ext>-extracted` segment, and a cooked asset adds one too, so a nested
    texture reads as `abc.7z-extracted/def.utoc-extracted/Game/Foo.uasset-extracted/Foo.dds`.

    Lifetime: temp holds only intermediates (a .7z member, a container triplet) and is
    dropped as the walk moves on; in selective mode at most one member is extracted per
    call. Everything durable goes to `out_dir` under the yielded relative path: decoded
    textures, their sidecars, and the cooked assets they came from, which a later
    write-back pass needs. Loose images already on disk are yielded in place and never
    copied, so consumers edit the real mod file.

    Errors from a single archive, container or asset are logged and skipped.
    """

    def __init__(
        self,
        mod_root: str | Path = MOD_ROOT,
        out_dir: str | Path = OUT_DIR,
        selective: bool = True,
    ) -> None:
        self.root = Path(mod_root).resolve()
        self.out = Path(out_dir).resolve()
        self.selective = selective

    # -- public API ---------------------------------------------------------

    def __iter__(self) -> Iterator[WalkItem]:
        require_tools()
        self.out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="modextract_") as tmp:
            self.tmp_root = Path(tmp)
            yield from self._walk_disk()

    # -- layer 1: the mod folder on disk -------------------------------------

    def _walk_disk(self) -> Iterator[WalkItem]:
        for child in sorted(self.root.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(self.root).as_posix()
            if child.suffix.lower() in SEVENZIP_EXTS:
                with self._guard(rel):
                    yield from self._walk_7z(child, archive_segment(rel))
            elif is_image(child.name):
                yield str(child), rel

    # -- layer 2: a .7z archive ----------------------------------------------

    def _walk_7z(self, archive: Path, prefix: str) -> Iterator[WalkItem]:
        source = SevenZipSource(archive)
        members = source.list_members()
        images = [m for m in members if is_image(m)]
        containers = group_ue_members(m for m in members if ext_of(m) in UE_EXTS)

        yield from self._emit(source, images, prefix)

        for group in containers:
            with self._guard(join_rel(prefix, group.segment)):
                yield from self._walk_ue_group(source, group, prefix)

    def _walk_ue_group(
        self, source: ArchiveSource, group: UEContainerSet, prefix: str
    ) -> Iterator[WalkItem]:
        """Materialise one container set out of the .7z, then descend into it."""
        # The triplet itself is temp-only: `retoc to-zen` rebuilds it from the cooked
        # tree on the way back, so persisting it would just duplicate the mod.
        with work_dir(self.tmp_root) as set_dir:
            source.extract(group.internal_names(), set_dir)
            yield from self._walk_ue(set_dir, join_rel(prefix, group.segment))

    # -- layer 3: a UE container set -----------------------------------------

    def _walk_ue(self, set_dir: Path, prefix: str) -> Iterator[WalkItem]:
        """Unpack the container straight into its output bundle, then decode each asset."""

        cooked = self.out / prefix / COOKED_DIR
        UEContainerSource(set_dir).extract([], cooked)
        yield from self._walk_assets(cooked, prefix)

    # -- layer 4: cooked assets ----------------------------------------------

    def _walk_assets(self, cooked: Path, prefix: str) -> Iterator[WalkItem]:
        """Decode every cooked package below `cooked`, one at a time."""
        decoder = TextureDecoder(cooked)
        for asset in sorted(cooked.rglob("*.uasset")):
            asset_rel = join_rel(
                prefix, archive_segment(asset.relative_to(cooked).as_posix())
            )
            with self._guard(asset_rel):
                # Sidecars land beside the .dds; both stay for the write-back pass.
                for image in decoder.decode(asset, self.out / asset_rel):
                    yield str(image), join_rel(asset_rel, image.name)

    # -- emission -------------------------------------------------------------

    def _emit(
        self, source: ArchiveSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """Extract and yield `members` into the output tree, selectively or in one batch."""

        if not members:
            return
        dest = self.out / prefix
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
                source.extract([internal], dest)
                yield str(dest / internal), join_rel(prefix, internal)

    def _emit_batch(
        self, source: ArchiveSource, members: list[str], dest: Path, prefix: str
    ) -> Iterator[WalkItem]:
        """One extraction for the whole group; a solid archive is decoded only once."""

        with self._guard(prefix):
            source.extract(members, dest)
            for internal in members:
                yield str(dest / internal), join_rel(prefix, internal)

    # -- error handling -------------------------------------------------------

    @contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        """Log and swallow failures from one archive/member so the walk continues."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - a bad archive must not abort the walk
            log.warning("skipping %s: %s", what, exc)


def fileIterator() -> Iterator[WalkItem]:
    """Convenience wrapper over `ModWalker`; see it for semantics."""
    return iter(ModWalker(MOD_ROOT, OUT_DIR, True))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for abs_path, rel_path in fileIterator():
        print(rel_path, "->", abs_path)
