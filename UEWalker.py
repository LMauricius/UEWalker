"""
Walk an Unreal Engine mod folder and yield every image inside it.

Descends recursively: mod root -> .7z archives -> UE containers (.pak / .utoc+.ucas).
See `ModWalker` / `file_iterator` for the public entry points.
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

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
SEVENZIP_EXTS = {".7z"}
UE_EXTS = {".pak", ".ucas", ".utoc"}

# Which container member represents the set in the output path, most specific first.
UE_REPRESENTATIVE_ORDER = (".ucas", ".pak", ".utoc")

# (temp file absolute path, path relative to the mod root)
WalkItem = tuple[str, str]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

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


def sole_file(root: Path) -> Path:
    """The single file produced under `root` by a one-member extraction."""
    return next(p for p in root.rglob("*") if p.is_file())


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
    A UE container set already materialised on disk, unpacked via retoc/repak.

    Always batch: the tools unpack a whole container, so single-asset extraction
    is not available.
    """

    supports_selective = False

    def __init__(self, set_dir: Path) -> None:
        self.set_dir = set_dir

    def list_members(self) -> list[str]:
        # Contents are unknown until the container is unpacked.
        raise NotImplementedError("UE containers cannot be listed without unpacking")

    def extract(self, members: list[str], dest: Path) -> None:
        """`members` is ignored: the whole container is unpacked into `dest`."""
        utoc = next(self.set_dir.rglob("*.utoc"), None)
        pak = next(self.set_dir.rglob("*.pak"), None)
        if utoc is not None:
            converted = dest / "converted.pak"
            self._utoc_to_pak(utoc, converted)
            self._unpack_pak(converted, dest)
            converted.unlink(missing_ok=True)
        elif pak is not None:
            self._unpack_pak(pak, dest)
        else:
            raise RuntimeError(f"no .pak/.utoc found in {self.set_dir}")

    # -- tool seams ---------------------------------------------------------

    @staticmethod
    def _utoc_to_pak(utoc: Path, out_pak: Path) -> None:
        """Convert an IoStore set (.utoc + sibling .ucas) to a legacy .pak. UNVERIFIED."""
        subprocess.run(["retoc", "to-legacy", str(utoc), str(out_pak)], check=True)

    @staticmethod
    def _unpack_pak(pak: Path, out_dir: Path) -> None:
        """Unpack a legacy .pak into `out_dir`. UNVERIFIED."""
        subprocess.run(["repak", "unpack", str(pak), "-o", str(out_dir)], check=True)


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

class ModWalker:
    """
    Recursive image walker over a mod folder.

    Yields `(absolute temp path, path relative to the mod root)`. Every archive adds a
    `<name><ext>-extracted` segment, so a nested texture reads as
    `abc.7z-extracted/def.ucas-extracted/texture.dds`.

    Lifetime: in selective mode at most one extracted image exists at a time, dropped
    before the next is produced. Where selective extraction is unsupported (UE
    containers) or disabled, a whole batch is extracted and kept until that batch is
    exhausted. Loose images already on disk are yielded in place and never removed.

    Errors from a single archive or container are logged and skipped; the walk continues.
    """

    def __init__(self, mod_root: str | Path, selective: bool = True) -> None:
        self.root = Path(mod_root).resolve()
        self.selective = selective

    # -- public API ---------------------------------------------------------

    def __iter__(self) -> Iterator[WalkItem]:
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
        with work_dir(self.tmp_root) as set_dir:
            source.extract(group.internal_names(), set_dir)
            yield from self._walk_ue(set_dir, join_rel(prefix, group.segment))

    # -- layer 3: a UE container set -----------------------------------------

    def _walk_ue(self, set_dir: Path, prefix: str) -> Iterator[WalkItem]:
        container = UEContainerSource(set_dir)
        with work_dir(self.tmp_root) as out_dir:
            container.extract([], out_dir)
            for image in images_under(out_dir):
                rel = join_rel(prefix, image.relative_to(out_dir).as_posix())
                yield from self._yield_and_drop(image, rel)

    # -- emission -------------------------------------------------------------

    def _emit(
        self, source: ArchiveSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """Extract and yield `members`, selectively or in one batch."""
        if not members:
            return
        if self.selective and source.supports_selective:
            yield from self._emit_selective(source, members, prefix)
        else:
            yield from self._emit_batch(source, members, prefix)

    def _emit_selective(
        self, source: ArchiveSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """One member per extraction: only a single temp file is ever live."""
        for internal in members:
            with self._guard(join_rel(prefix, internal)), work_dir(self.tmp_root) as scratch:
                source.extract([internal], scratch)
                yield str(sole_file(scratch)), join_rel(prefix, internal)

    def _emit_batch(
        self, source: ArchiveSource, members: list[str], prefix: str
    ) -> Iterator[WalkItem]:
        """
        One extraction for the whole group; files stay on disk until the group ends.

        Cheaper on solid archives, at the cost of holding the batch in the temp dir.
        """
        with self._guard(prefix), work_dir(self.tmp_root) as batch:
            source.extract(members, batch)
            for image in images_under(batch):
                yield str(image), join_rel(prefix, image.relative_to(batch).as_posix())

    def _yield_and_drop(self, path: Path, rel: str) -> Iterator[WalkItem]:
        """
        Hand one file to the consumer, then unlink it as soon as the consumer resumes.

        `finally` also covers an abandoned generator, so the file cannot outlive the
        iteration step it belongs to.
        """
        try:
            yield str(path), rel
        finally:
            path.unlink(missing_ok=True)

    # -- error handling -------------------------------------------------------

    @contextmanager
    def _guard(self, what: str) -> Iterator[None]:
        """Log and swallow failures from one archive/member so the walk continues."""
        try:
            yield
        except Exception as exc:  # noqa: BLE001 - a bad archive must not abort the walk
            log.warning("skipping %s: %s", what, exc)


def fileIterator(mod_root: str, selective: bool = True) -> Iterator[WalkItem]:
    """Convenience wrapper over `ModWalker`; see it for semantics."""
    return iter(ModWalker(mod_root, selective=selective))


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    for abs_path, rel_path in fileIterator(sys.argv[1]):
        print(rel_path, "->", abs_path)
