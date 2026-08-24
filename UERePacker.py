"""
Pack the textures edited in `OUT_DIR` back into patch containers under `PATCH_DIR`.

The reverse of `UEWalker.py`, and its consumer: the walk leaves a `.dds.json` sidecar
beside every texture it decoded and the cooked package under `<container>/_cooked` for
every texture an edit reached, and this turns those into `.utoc`/`.ucas`/`.pak` triplets
the game can mount. Only edited textures are packed. A texture the consumer never wrote
back has no `.dds` in the output, so it is never looked at, and the container it lived in
carries only what actually changed.

The tree is mirrored at every step. A directory named `<name>.<ext>-extracted` is one
container and becomes `<name>.utoc` at the same relative path under `PATCH_DIR`; anything
else is copied across unchanged, minus the walk's own bookkeeping.

Two external tools do the heavy lifting, both configured in `UEWalkerConfig`:

- UE4-DDS-Tools writes an edited `.dds` back into the cooked Zen asset, rewriting the
  texture's dimensions, mip count and bulk descriptors. Edits are routinely smaller than
  what they replace, so this is a re-serialization and not a byte splice.
- UnrealReZen packs the result. It must be the locally patched build; see `TOOL-PATCHES.md`
  for why a stock one silently emits a container that declares no packages.

Run it with no arguments to pack `OUT_DIR` into `PATCH_DIR`.
"""

from __future__ import annotations

import logging
import shutil
import struct
import subprocess
import sys
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path, PurePosixPath

from UEWalker import (
    BACKUP_PREFIX,
    COOKED_DIR,
    DONE_MARKER,
    CUE4Parse,
    ToolError,
    read_json,
    release_scratch,
    scratch_dir,
    scratch_guard,
    sweep_stale_scratch,
    write_json,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

from UEWalkerConfig import *

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: A directory the walk made to stand in for something it opened. The container ones are
#: repacked; the rest (a `.7z`, a cooked asset) are just structure and are mirrored.
EXTRACTED_SUFFIX = "-extracted"
CONTAINER_EXTS = (".ucas", ".utoc", ".pak")

#: Written beside every decoded texture, and the authority on which package and export
#: that texture came from. The walk builds the same path twice over, so the sidecar is
#: what settles disagreements.
SIDECAR_SUFFIX = ".dds.json"

#: The walk's own records. They describe the output tree rather than the mod, so none of
#: them belongs in a patch.
BOOKKEEPING_NAMES = {DONE_MARKER}
BOOKKEEPING_SUFFIXES = (SIDECAR_SUFFIX,)

#: Where a package lives in the game, cached so a run costs one mount of `GAME_PAKS`
#: instead of one per container. Lives in `PATCH_DIR`; delete it to rebuild.
INDEX_NAME = ".uerepacker-index.json"

#: Chunk kinds a packed package is made of. `.uptnl` is deliberately absent: injection
#: folds every mip into the inline tail or the `.ubulk`, so an optional chunk is read but
#: never written back.
PACKED_EXTS = (".uasset", ".ubulk")

#: `FIoContainerHeader` as this game writes it, for the verification pass. Nothing else
#: reads it correctly, which is the whole reason `TOOL-PATCHES.md` exists.
NAME_HASH_ALGORITHM = 0xC1640000
STORE_ENTRY_SIZE = 32

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def container_stem(name: str) -> str | None:
    """
    Container a `<name>.<ext>-extracted` directory stands for, or None if it is not one.

    `f80h_plants_color_textures_P.ucas-extracted` -> `f80h_plants_color_textures_P`.
    """
    if not name.endswith(EXTRACTED_SUFFIX):
        return None
    inner = name[: -len(EXTRACTED_SUFFIX)]
    stem, dot, ext = inner.rpartition(".")
    return stem if dot and f".{ext}".lower() in CONTAINER_EXTS else None


def is_bookkeeping(path: Path) -> bool:
    """True for a file the walk wrote about itself rather than about the mod."""
    return (
        path.name in BOOKKEEPING_NAMES
        or path.name.startswith(BACKUP_PREFIX)
        or path.name.endswith(BOOKKEEPING_SUFFIXES)
        or path.name == INDEX_NAME
    )


def package_of(asset_dir: Path, container: Path) -> str:
    """
    Package path a `<package>.uasset-extracted` directory stands for.

    `<container>/End/Content/Foo/Bar.uasset-extracted` -> `End/Content/Foo/Bar`.
    """
    rel = asset_dir.relative_to(container).as_posix()
    return rel[: -len(EXTRACTED_SUFFIX)].rsplit(".", 1)[0]


def cooked_asset(cooked: Path, package: str) -> Path | None:
    """
    The `.uasset` under `_cooked` holding `package`, or None if the walk never saved it.

    `save_package` writes each chunk at the path CUE4Parse hands back, and those are
    relative to the container's mount point rather than to the package root, so
    `End/Content/Environment/Nature/Texture/T_X` is stored as `Nature/Texture/T_X`. The
    mount point is not recorded anywhere, so the package path is walked from the left
    until one of its suffixes lands on a file.
    """
    parts = package.split("/")
    for start in range(len(parts)):
        candidate = cooked.joinpath(*parts[start:]).with_suffix(".uasset")
        if candidate.is_file():
            return candidate
    return None


def cooked_parts(asset: Path, cooked: Path) -> dict[str, Path]:
    """
    Every chunk file of one cooked package, keyed by extension.

    They sit together in anything `save_package` writes now, but output from before it
    anchored every chunk on the package path can have an optional-mip (`.uptnl`) chunk at
    the root of `_cooked` instead of beside its own package. UE4-DDS-Tools resolves the
    chunks as siblings of the `.uasset` and fails outright when one is missing, so a stray
    is looked up by name and reported here for the caller to stage. Re-walking a mod just
    to move those is not worth it, so this stays.
    """

    parts = {".uasset": asset}
    for ext in (".ubulk", ".uptnl"):
        sibling = asset.with_suffix(ext)
        if sibling.is_file():
            parts[ext] = sibling
            continue
        stray = cooked / f"{asset.stem}{ext}"
        if stray.is_file():
            log.debug("%s: %s is at the root of _cooked", asset.stem, stray.name)
            parts[ext] = stray
    return parts


@contextmanager
def quiet():
    """Send a chatty library's stdout to the debug log instead of the terminal."""
    buffer = StringIO()
    try:
        with redirect_stdout(buffer):
            yield
    finally:
        for line in buffer.getvalue().splitlines():
            if line.strip():
                log.debug("%s", line.strip())


def parse_container_header(payload: bytes) -> dict:
    """
    Read this game's `FIoContainerHeader`, for the verification pass.

    Its layout carries one `uint32` more than stock UE4.26 does, right after the package
    count, which is why neither retoc nor an unpatched CUE4Parse can read it. Only the
    fields worth checking are returned: the package IDs and their store entries.
    """
    offset = 0
    container_id, package_count, _extra, names_size, hashes_size = struct.unpack_from(
        "<QIIII", payload, offset
    )
    offset += 24
    algorithm = struct.unpack_from("<Q", payload, offset + names_size)[0] if hashes_size >= 8 else 0
    offset += names_size + hashes_size

    id_count = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    package_ids = struct.unpack_from(f"<{id_count}Q", payload, offset)
    offset += id_count * 8

    entries_size = struct.unpack_from("<I", payload, offset)[0]
    offset += 4
    entries = []
    for index in range(id_count):
        bundles, exports, bundle_count, load_order, pad, imports, _off = struct.unpack_from(
            "<QiiIIII", payload, offset + index * STORE_ENTRY_SIZE
        )
        entries.append({
            "export_bundles_size": bundles,
            "exports": exports,
            "bundles": bundle_count,
            "load_order": load_order,
            "pad": pad,
            "imports": imports,
        })
    return {
        "container_id": container_id,
        "package_count": package_count,
        "name_hash_algorithm": algorithm,
        "package_ids": list(package_ids),
        "store_entries_size": entries_size,
        "store_entries": entries,
    }


def require_tools() -> None:
    """Fail early, and by name, if a configured dependency is missing."""
    if not (Path(UE4_DDS_TOOLS) / "unreal" / "uasset.py").is_file():
        raise ToolError(f"UE4-DDS-Tools src not found: {UE4_DDS_TOOLS!r}")
    if not Path(UNREALREZEN_PATH).is_file():
        raise ToolError(f"UnrealReZen not found: {UNREALREZEN_PATH!r}")
    paks = REZEN_GAME_DIR or GAME_PAKS
    if not paks or not Path(paks).is_dir():
        raise ToolError(f"game Paks folder not found: {paks!r} (see GAME_PAKS)")


# ---------------------------------------------------------------------------
# UE4-DDS-Tools
# ---------------------------------------------------------------------------

class DdsTools:
    """
    Lazy loader for UE4-DDS-Tools, which is imported as a library rather than shelled to.

    Its modules import each other by bare name (`from util import ...`), so its `src`
    directory has to go on `sys.path` ahead of everything else. That is done once, on
    first use, so a run that packs nothing never pays for it.
    """

    _api: dict[str, object] = {}

    @classmethod
    def api(cls) -> dict[str, object]:
        """`{"Uasset": ..., "DDS": ...}`, imported on first call."""
        if cls._api:
            return cls._api
        root = str(Path(UE4_DDS_TOOLS).resolve())
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from directx.dds import DDS  # noqa: PLC0415 - deliberately deferred
            from unreal.uasset import Uasset  # noqa: PLC0415
        except ImportError as exc:
            raise ToolError(f"cannot import UE4-DDS-Tools from {root!r}: {exc}") from exc
        log.debug("UE4-DDS-Tools loaded from %s", root)
        cls._api = {"Uasset": Uasset, "DDS": DDS}
        return cls._api


@dataclass
class Edit:
    """One edited texture: the `.dds` the consumer wrote, and where it came from."""

    package: str
    export: str
    dds: Path


def inject(asset: Path, edits: list[Edit], destination: Path) -> None:
    """
    Write every edit of one package into its cooked asset, saving to `destination`.

    Nothing is copied: the cooked chunks and the `.dds` are read where they lie and the
    re-serialized package is written once, straight to the virtual path it is packed
    under. Exports the consumer did not touch are carried through untouched.
    """
    api = DdsTools.api()
    with quiet():
        package = api["Uasset"](str(asset), version=DDS_TOOLS_VERSION)
        # Textures are matched by export name rather than by position: a package can hold
        # several, and the walk names each `.dds` after the export it decoded.
        textures = {export.name: export.object
                    for export in package.exports if export.is_texture()}
        for edit in edits:
            texture = textures.get(edit.export)
            if texture is None:
                raise RuntimeError(f"{edit.package}: no texture export named {edit.export!r}")
            texture.inject_dds(api["DDS"].load(str(edit.dds)))
        package.update_package_source(is_official=False)
        package.save(str(destination))


# ---------------------------------------------------------------------------
# Game index
# ---------------------------------------------------------------------------

class GameIndex:
    """
    Which game container holds each package, so UnrealReZen can be pointed at a few.

    UnrealReZen reads a chunk ID and a package store entry for every file it packs, out of
    whatever containers `--game-dir` covers. Handed the whole `Content/Paks` it indexes
    half a million entries, seven seconds and seven gigabytes at a time, once per
    container. Handed a directory of symlinks to just the one or two containers a mod
    container actually needs, it takes half a second and 150 MB.

    Building the map costs one mount of `GAME_PAKS`, and only the `.utoc` directory
    indexes are read: the 150 GB of `.ucas` payload beside them is never touched. The
    result is cached in `PATCH_DIR`, so later runs usually mount nothing at all.
    """

    def __init__(self, cache: Path, paks: str) -> None:
        self.cache = cache
        self.paks = paks
        #: Virtual file path -> container file name, e.g. `pakchunk8optional-...utoc`.
        self.map: dict[str, str] = dict(read_json(cache) or {})
        self.missing: set[str] = set()

    def resolve(self, wanted: set[str]) -> None:
        """Make sure every path in `wanted` is in the map, mounting the game if needed."""
        unknown = {path for path in wanted if path not in self.map}
        if not unknown:
            log.info("game index: %d path(s) served from %s", len(wanted), self.cache.name)
            return
        log.info("game index: mounting %s for %d unknown path(s)", self.paks, len(unknown))
        found = self._lookup(unknown)
        self.map.update(found)
        self.missing = unknown - found.keys()
        for path in sorted(self.missing):
            log.warning("no game container holds %s; it cannot be packed", path)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.cache, dict(sorted(self.map.items())))

    def _lookup(self, wanted: set[str]) -> dict[str, str]:
        """Mount the game once and read the container of each wanted path out of it."""
        t = CUE4Parse.types()
        versions = t["VersionContainer"](getattr(t["EGame"], UE_VERSION))
        provider = t["DefaultFileProvider"](
            t["DirectoryInfo"](self.paks), t["SearchOption"].TopDirectoryOnly, True, versions
        )
        provider.Initialize()
        if AES_KEY:
            provider.SubmitKey(AES_KEY)
        provider.Mount()
        files = provider.Files
        found = {}
        for path in wanted:
            if files.ContainsKey(path):
                found[path] = str(files[path].Vfs.Name)
        log.info("game index: %d of %d path(s) located", len(found), len(wanted))
        return found

    def containers_for(self, paths: set[str]) -> set[str]:
        """Container file names covering `paths`, skipping any the game does not have."""
        return {self.map[path] for path in paths if path in self.map}


# ---------------------------------------------------------------------------
# Repacker
# ---------------------------------------------------------------------------

@dataclass
class ContainerJob:
    """One container's worth of work: where it came from, and what changed in it."""

    source: Path
    relative: PurePosixPath
    stem: str
    #: Package path -> its edits, ordered as the walk yielded them.
    packages: dict[str, list[Edit]] = field(default_factory=dict)
    #: Package path -> the `.uasset` under `_cooked` it has to be written into.
    cooked: dict[str, Path] = field(default_factory=dict)
    #: Packages the game does not ship, so no chunk ID or store entry can be sourced for
    #: them. A mod that adds a texture rather than replacing one lands here.
    dropped: set[str] = field(default_factory=set)

    @property
    def output(self) -> PurePosixPath:
        """`.utoc` path relative to `PATCH_DIR`, mirroring where the container sat."""
        return self.relative.parent / f"{self.stem}.utoc"

    def wanted_paths(self) -> set[str]:
        """Virtual paths this container's packed chunks will be looked up under."""
        return {f"{package}{ext}" for package in self.packages for ext in PACKED_EXTS}


class ModRepacker:
    """
    Turns an edited output tree into patch containers, mirroring its structure.

    `out_dir` is read, never written. Everything produced lands under `patch_dir` at the
    relative path its source held, so a container that sat in `clothes/` comes out in
    `clothes/`, and the loose files beside it come out beside it.

    Scratch goes through the walker's own registry, so an interrupted run leaves nothing
    behind: the injected assets for one container are written, packed and dropped before
    the next container starts, which keeps the peak at one container's worth (132 MiB for
    the largest here) rather than the whole mod's.

    Failures are per package: one texture that will not inject is logged and skipped, and
    the container is still packed with the rest. `run` returns the failure count, so a
    caller can tell a clean run from a partial one.
    """

    def __init__(
        self,
        out_dir: str | Path = OUT_DIR,
        patch_dir: str | Path = PATCH_DIR,
        game_paks: str | None = None,
        skip_existing: bool = SKIP_EXISTING,
        verify: bool = True,
        only: str | None = None,
    ) -> None:
        self.out = Path(out_dir).resolve()
        self.patch = Path(patch_dir).resolve()
        self.paks = game_paks or REZEN_GAME_DIR or GAME_PAKS
        #: Restrict the run to one subtree, given relative to `out_dir`. Both roots stay
        #: where they are, so the mirrored layout and the cached index are unaffected.
        #: A walk still in progress is the usual reason: only the part it has finished
        #: with can safely be packed.
        self.only = PurePosixPath(only) if only else None
        #: Leave a container alone when its `.utoc` is already in the output.
        self.skip_existing = skip_existing
        #: Mount each packed container afterwards and check it reads back.
        self.verify = verify
        self._failed = 0
        self.tmp_root: Path | None = None

    def _in_scope(self, relative: PurePosixPath) -> bool:
        """True when `relative` is inside the subtree this run was restricted to."""
        return self.only is None or relative.is_relative_to(self.only)

    # -- public API ---------------------------------------------------------

    def run(self) -> int:
        """Pack every container and mirror everything else. Returns the failure count."""
        require_tools()
        log.info("repacking %s -> %s", self.out, self.patch)
        self.patch.mkdir(parents=True, exist_ok=True)
        sweep_stale_scratch()

        jobs = self._plan()
        log.info("%d container(s) with edits, %d package(s) to write",
                 len(jobs), sum(len(job.packages) for job in jobs))

        index = GameIndex(self.patch / INDEX_NAME, self.paks)
        index.resolve(set().union(*(job.wanted_paths() for job in jobs)) if jobs else set())
        self._drop_unsourceable(jobs, index)

        with scratch_guard():
            self.tmp_root = scratch_dir()
            try:
                for job in jobs:
                    with self._guard(str(job.output)):
                        self._repack(job, index)
            finally:
                release_scratch(self.tmp_root)
                self.tmp_root = None

        self._mirror()
        if self._failed:
            log.error("%d failure(s); the patch is incomplete", self._failed)
        else:
            log.info("done, no failures")
        return self._failed

    # -- planning -----------------------------------------------------------

    def _plan(self) -> list[ContainerJob]:
        """Find every container directory holding at least one edited texture."""
        jobs = []
        for directory in sorted(self.out.rglob("*" + EXTRACTED_SUFFIX)):
            stem = container_stem(directory.name)
            if stem is None or not directory.is_dir():
                continue
            relative = PurePosixPath(directory.relative_to(self.out).as_posix())
            if not self._in_scope(relative):
                continue
            job = ContainerJob(directory, relative, stem)
            if self.skip_existing and (self.patch / job.output).is_file():
                log.info("skipping container %s: already packed", job.output)
                continue
            # No marker means the walk either lost an asset in this container or has not
            # finished with it, and in the second case its edits are still arriving.
            if not (directory / DONE_MARKER).is_file():
                log.warning("%s: no %s, packing it anyway; it may be incomplete",
                            relative, DONE_MARKER)
            self._collect(job)
            if job.packages:
                jobs.append(job)
            else:
                log.debug("%s: nothing edited", relative)
        return jobs

    def _collect(self, job: ContainerJob) -> None:
        """Gather the edits in one container and the cooked assets they belong to."""
        cooked_root = job.source / COOKED_DIR
        for dds in sorted(job.source.rglob("*.dds")):
            if dds.name.startswith(BACKUP_PREFIX) or cooked_root in dds.parents:
                continue
            with self._guard(str(job.relative / dds.name)):
                edit = self._edit_of(dds, job)
                if job.packages.get(edit.package) is None:
                    asset = cooked_asset(cooked_root, edit.package)
                    if asset is None:
                        raise RuntimeError(
                            f"{edit.package}: edited but no cooked package in {COOKED_DIR}"
                        )
                    job.cooked[edit.package] = asset
                    job.packages[edit.package] = []
                job.packages[edit.package].append(edit)

    def _edit_of(self, dds: Path, job: ContainerJob) -> Edit:
        """Read one edited texture's identity, preferring its sidecar over its path."""
        # The sidecar is what the decoder wrote, so it names the package and the export
        # even when the export's name was suffixed to dodge a collision on disk.
        sidecar = read_json(dds.with_name(dds.name + ".json"))
        package = package_of(dds.parent, job.source)
        if isinstance(sidecar, dict):
            if sidecar.get("package") and sidecar["package"] != package:
                log.warning("%s: sidecar says %s, path says %s; trusting the sidecar",
                            dds.name, sidecar["package"], package)
                package = sidecar["package"]
            return Edit(package, str(sidecar.get("export") or dds.stem), dds)
        log.warning("%s: no sidecar, taking the export name from the file name", dds.name)
        return Edit(package, dds.stem, dds)

    def _drop_unsourceable(self, jobs: list[ContainerJob], index: GameIndex) -> None:
        """
        Set aside the packages the game does not ship, and say so loudly.

        UnrealReZen reads a chunk ID and a store entry for every file it packs out of the
        game's own containers, so a texture the mod adds rather than replaces has nothing
        to read. Dropping the one package keeps the rest of its container shippable, which
        beats losing thirty-five textures over one, but it is a hole in the patch and is
        counted as a failure so the summary stays honest.
        """
        for job in jobs:
            job.dropped = {package for package in job.packages
                           if f"{package}.uasset" not in index.map}
            for package in sorted(job.dropped):
                self._failed += 1
                log.warning("%s: %s is not in the game, dropping it from the patch",
                            job.relative, package)
                del job.packages[package]

    # -- packing ------------------------------------------------------------

    def _repack(self, job: ContainerJob, index: GameIndex) -> None:
        """Inject every edit in one container, pack it, and drop the scratch it used."""
        log.info("container %s: %d package(s)", job.output, len(job.packages))
        assert self.tmp_root is not None
        # Named after the output path rather than the container stem: two mods can ship
        # the same stem in different folders, and their scratch must not collide.
        work = self.tmp_root / str(job.output).replace("/", "_")
        content = work / "content"
        try:
            written = self._inject_all(job, content, work)
            if not written:
                log.warning("%s: nothing injected, not packing", job.output)
                return
            game_dir = self._game_dir(work, index, written)
            target = self.patch / job.output
            target.parent.mkdir(parents=True, exist_ok=True)
            self._pack(content, game_dir, target)
            if self.verify:
                try:
                    self._verify(job, target, written)
                except Exception:
                    # A container that does not read back is worse than none at all:
                    # left on disk it would be shipped, and `skip_existing` would take
                    # it for finished work on the next run.
                    for suffix in (".utoc", ".ucas", ".pak"):
                        target.with_suffix(suffix).unlink(missing_ok=True)
                    raise
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _inject_all(self, job: ContainerJob, content: Path, work: Path) -> set[str]:
        """Write every package of one container into `content`; returns what landed."""
        written = set()
        for package, edits in sorted(job.packages.items()):
            with self._guard(package):
                asset = job.cooked[package]
                parts = cooked_parts(asset, job.source / COOKED_DIR)
                inject(self._readable(asset, parts, work, package), edits,
                       content / f"{package}.uasset")
                written.add(package)
        return written

    @staticmethod
    def _readable(asset: Path, parts: dict[str, Path], work: Path, package: str) -> Path:
        """
        A path whose `.uasset` has all of its chunks beside it.

        Normally that is the cooked asset itself and nothing is staged. When the walk
        misfiled an optional chunk (see `cooked_parts`), the chunks are gathered into a
        scratch directory as symlinks, which costs three inodes and copies no payload.

        Staged under the full package path rather than the asset's bare name: two packages
        in one mod can share a name, and pointing the second at the first one's chunks
        would inject an edit into the wrong texture.
        """
        if all(path.parent == asset.parent for path in parts.values()):
            return asset
        staged = work / "staged" / package
        staged.mkdir(parents=True, exist_ok=True)
        for ext, source in parts.items():
            link = staged / f"{asset.stem}{ext}"
            link.unlink(missing_ok=True)
            link.symlink_to(source)
        log.debug("%s: chunks staged as symlinks in %s", package, staged)
        return staged / f"{asset.stem}.uasset"

    def _game_dir(self, work: Path, index: GameIndex, packages: set[str]) -> Path:
        """
        A `--game-dir` holding only the containers this container's packages need.

        Symlinks, so nothing is copied and the `.ucas` payload is only ever read from
        where it already lives. Both halves of a container have to be present: CUE4Parse
        opens the `.utoc` for the index and the `.ucas` for the header chunk.
        """
        wanted = {f"{package}{ext}" for package in packages for ext in PACKED_EXTS}
        containers = index.containers_for(wanted)
        game_dir = work / "gamedir"
        game_dir.mkdir(parents=True, exist_ok=True)
        paks = Path(self.paks)
        for name in sorted(containers):
            for suffix in (".utoc", ".ucas"):
                source = (paks / name).with_suffix(suffix)
                link = game_dir / source.name
                if source.is_file() and not link.exists():
                    link.symlink_to(source)
        log.debug("game dir for %d package(s): %s", len(packages), sorted(containers))
        return game_dir

    def _pack(self, content: Path, game_dir: Path, target: Path) -> None:
        """Run UnrealReZen over the injected assets, writing the triplet at `target`."""
        command = [
            str(UNREALREZEN_PATH),
            "--game-dir", str(game_dir),
            "--content-path", str(content),
            "--engine-version", REZEN_VERSION,
            "--compression-format", PATCH_COMPRESSION,
            "--output-path", str(target),
        ]
        if AES_KEY:
            command += ["--aes-key", AES_KEY]
        log.debug("running %s", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True)
        for line in (result.stdout or "").splitlines():
            if "Skipping" in line or "No store entry" in line:
                log.warning("UnrealReZen: %s", line.strip())
            elif line.strip():
                log.debug("UnrealReZen: %s", line.strip())
        if result.returncode != 0:
            raise RuntimeError(
                f"UnrealReZen failed ({result.returncode}): "
                f"{(result.stderr or result.stdout or '').strip()[:400]}"
            )

    # -- verification -------------------------------------------------------

    def _verify(self, job: ContainerJob, target: Path, packages: set[str]) -> None:
        """
        Check the packed container reads back the way the game will need it to.

        Two things are worth checking and nothing else checks them: that every package
        made it into the container's index, and that its header declares those same
        packages with a sane store entry each. The header is the part that was silently
        wrong before `TOOL-PATCHES.md`, so it is checked on every container rather than
        sampled.
        """
        mounted = self._mounted_packages(target)
        expected = {f"{package}.uasset" for package in packages}
        if missing := expected - mounted:
            raise RuntimeError(f"{len(missing)} package(s) missing from {target.name}: "
                               f"{sorted(missing)[:3]}")

        header = self._header_of(target)
        if header is None:
            log.warning("%s: header not checked, retoc unavailable", job.output)
            return
        if header["package_count"] != len(packages):
            raise RuntimeError(f"{target.name}: header declares "
                               f"{header['package_count']} package(s), packed {len(packages)}")
        if header["name_hash_algorithm"] != NAME_HASH_ALGORITHM:
            raise RuntimeError(f"{target.name}: header name hash algorithm is "
                               f"{header['name_hash_algorithm']:#x}")
        if header["store_entries_size"] != len(packages) * STORE_ENTRY_SIZE:
            raise RuntimeError(f"{target.name}: store entries are "
                               f"{header['store_entries_size']} bytes for {len(packages)}")
        for entry in header["store_entries"]:
            if entry["export_bundles_size"] == 0 or entry["exports"] < 1:
                raise RuntimeError(f"{target.name}: empty store entry {entry}")
        log.info("%s: %d package(s) verified", job.output, len(packages))

    def _mounted_packages(self, target: Path) -> set[str]:
        """
        Every package key CUE4Parse finds in the packed container.

        The provider is pointed at a directory of symlinks holding this container alone,
        and disposed afterwards. Both matter: a provider mounts every container in the
        directory it is given and keeps each `.ucas` open for as long as it lives, so
        verifying one container in place would hold its neighbours open and the next pack
        into that directory would fail with the file still in use.
        """
        assert self.tmp_root is not None
        alone = self.tmp_root / "verify"
        shutil.rmtree(alone, ignore_errors=True)
        alone.mkdir(parents=True)
        for suffix in (".utoc", ".ucas", ".pak"):
            part = target.with_suffix(suffix)
            if part.is_file():
                (alone / part.name).symlink_to(part)

        t = CUE4Parse.types()
        versions = t["VersionContainer"](getattr(t["EGame"], UE_VERSION))
        provider = t["DefaultFileProvider"](
            t["DirectoryInfo"](str(alone)), t["SearchOption"].TopDirectoryOnly,
            True, versions,
        )
        try:
            provider.Initialize()
            provider.Mount()
            return {str(key) for key in provider.Files.Keys if str(key).endswith(".uasset")}
        finally:
            provider.Dispose()
            shutil.rmtree(alone, ignore_errors=True)

    @staticmethod
    def _header_of(target: Path) -> dict | None:
        """
        The container header of a packed `.utoc`, read through retoc.

        retoc cannot parse this game's header, but it can still hand back the raw chunk,
        which `parse_container_header` understands. None when retoc is not configured.
        """
        try:
            listing = subprocess.run(
                [str(RETOC_PATH), "list", str(target)],
                capture_output=True, text=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        # `retoc list` pads its columns, so the kind is the last field, not the line end.
        rows = (line.split() for line in listing.stdout.splitlines())
        chunk = next((row[1] for row in rows
                      if len(row) >= 3 and row[-1] == "ContainerHeader"), None)
        if chunk is None:
            return None
        payload = subprocess.run(
            [str(RETOC_PATH), "get", str(target), chunk, "-"],
            capture_output=True, check=True,
        ).stdout
        return parse_container_header(payload)

    # -- everything that is not a container ---------------------------------

    def _mirror(self) -> None:
        """Copy every non-container file across, keeping the tree exactly as it is."""
        copied = 0
        for source in sorted(self.out.rglob("*")):
            relative = source.relative_to(self.out)
            if not self._in_scope(PurePosixPath(relative.as_posix())):
                continue
            # A container directory is packed, not walked into, and the walk's own
            # records describe the output tree rather than the mod.
            if any(container_stem(part) for part in relative.parts):
                continue
            if not source.is_file() or is_bookkeeping(source):
                continue
            target = self.patch / relative
            if self.skip_existing and target.is_file() and \
                    target.stat().st_size == source.stat().st_size:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied += 1
        log.info("mirrored %d loose file(s)", copied)

    # -- error handling -----------------------------------------------------

    @contextmanager
    def _guard(self, what: str):
        """Log and swallow one package's or one container's failure."""
        try:
            yield
        except ToolError:
            raise  # a broken tool breaks everything after it
        except Exception as exc:  # noqa: BLE001 - one bad asset must not stop the pack
            self._failed += 1
            log.warning("skipping %s: %s: %s", what, type(exc).__name__, exc)
            log.debug("traceback for %s", what, exc_info=True)


def repack(
    out_dir: str | Path = OUT_DIR,
    patch_dir: str | Path = PATCH_DIR,
    only: str | None = None,
) -> int:
    """Convenience wrapper over `ModRepacker`; see it for semantics."""
    return ModRepacker(out_dir, patch_dir, only=only).run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if UEWALKER_DEBUG else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    # `only` restricts the run to one subtree of the output without moving either root,
    # which is what a walk that is still filling the rest of it calls for.
    only = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(1 if repack(OUT_DIR, PATCH_DIR, only) else 0)
