from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import py7zr

IMAGE_EXTS = {".png", ".dds", ".tga", ".bmp", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"}
UE_EXTS = {".pak", ".ucas", ".utoc"}
SEVENZIP_EXTS = {".7z"}


def _ext(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def _is_image(name: str) -> bool:
    return _ext(name) in IMAGE_EXTS


def _archive_segment(posix_relpath: str) -> str:
    """`pack/abc.7z` -> `pack/abc.7z-extracted`  (keeps parent dirs, tags the archive name)."""
    p = PurePosixPath(posix_relpath)
    tagged = p.name + "-extracted"
    return f"{p.parent}/{tagged}" if str(p.parent) != "." else tagged


def file_iterator(mod_root: str) -> Iterator[tuple[str, str]]:
    """
    Yield (absolute temp path, relative-to-mod-root path) for every image inside the mod,
    descending recursively through .7z and UE containers.

    Each archive contributes a `<name><ext>-extracted` directory segment to the relative path,
    e.g. abc.7z-extracted/def.ucas-extracted/texture.dds

    Only one extracted temp file exists at a time: the previous one is removed before the
    next is produced. Loose images sitting directly on disk are yielded in place (not deleted).
    """
    root = Path(mod_root).resolve()
    with tempfile.TemporaryDirectory(prefix="modextract_") as tmp:
        tmp_root = Path(tmp)
        for child in sorted(root.rglob("*")):
            if not child.is_file():
                continue
            rel = child.relative_to(root).as_posix()
            ext = child.suffix.lower()
            if ext in SEVENZIP_EXTS:
                yield from _walk_7z(child, _archive_segment(rel), tmp_root)
            elif _is_image(child.name):
                yield str(child), rel  # loose image already on disk


def _walk_7z(archive: Path, prefix: str, tmp_root: Path) -> Iterator[tuple[str, str]]:
    with py7zr.SevenZipFile(archive, "r") as z:
        infos = z.list()

    images: list[str] = []
    ue_groups: dict[tuple[str, str], dict[str, str]] = {}  # (dir, stem) -> {ext: internal}

    for info in infos:
        if getattr(info, "is_directory", False):
            continue
        internal = info.filename.replace("\\", "/")
        e = _ext(internal)
        if e in UE_EXTS:
            key = (str(PurePosixPath(internal).parent), PurePosixPath(internal).stem)
            ue_groups.setdefault(key, {})[e] = internal
        elif e in IMAGE_EXTS:
            images.append(internal)

    # Images: extract strictly one at a time, delete before the next is produced.
    # (Per-file reopen is O(n^2) on solid archives; relax to a single batch extract
    #  if your .7z files are solid and huge.)
    for internal in images:
        work = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            with py7zr.SevenZipFile(archive, "r") as z:
                z.extract(path=work, targets=[internal])
            extracted = next(p for p in work.rglob("*") if p.is_file())
            yield str(extracted), f"{prefix}/{internal}"
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # UE containers: extract the whole set once (single-asset extraction isn't reliable),
    # then yield + unlink each image individually.
    for (dirname, stem), members in ue_groups.items():
        set_dir = Path(tempfile.mkdtemp(dir=tmp_root))
        try:
            with py7zr.SevenZipFile(archive, "r") as z:
                z.extract(path=set_dir, targets=list(members.values()))

            rep_ext = ".ucas" if ".ucas" in members else (".pak" if ".pak" in members else ".utoc")
            seg = f"{stem}{rep_ext}-extracted"
            base = "" if dirname in (".", "") else f"{dirname}/"
            new_prefix = f"{prefix}/{base}{seg}"

            yield from _walk_ue(set_dir, new_prefix, tmp_root)
        finally:
            shutil.rmtree(set_dir, ignore_errors=True)


def _walk_ue(set_dir: Path, prefix: str, tmp_root: Path) -> Iterator[tuple[str, str]]:
    out_dir = Path(tempfile.mkdtemp(dir=tmp_root))
    try:
        _unpack_ue_container(set_dir, out_dir)  # <-- the one seam you wire to your tool
        for extracted in sorted(p for p in out_dir.rglob("*") if p.is_file()):
            internal = extracted.relative_to(out_dir).as_posix()
            if not _is_image(internal):
                continue
            rel = f"{prefix}/{internal}"
            try:
                yield str(extracted), rel
            finally:
                extracted.unlink(missing_ok=True)  # drop before the next one
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _unpack_ue_container(set_dir: Path, out_dir: Path) -> None:
    """
    Unpack a UE container set (found in set_dir) into out_dir, preserving internal structure.
    REPLACE the command below with whatever you use — repak / retoc / CUE4Parse-based.

    The exact subcommands/flags depend on your tool version, so confirm them:
      - IoStore (.utoc + .ucas present): convert to a legacy .pak first, then unpack.
      - Legacy .pak only: unpack directly.
    """
    utoc = next(set_dir.rglob("*.utoc"), None)
    pak = next(set_dir.rglob("*.pak"), None)

    if utoc is not None:
        # Example shape only — verify the actual invocation for your tool:
        subprocess.run(["retoc", "to-legacy", str(utoc), str(out_dir / "converted.pak")], check=True)
        subprocess.run(["repak", "unpack", str(out_dir / "converted.pak"), "-o", str(out_dir)], check=True)
        (out_dir / "converted.pak").unlink(missing_ok=True)
    elif pak is not None:
        subprocess.run(["repak", "unpack", str(pak), "-o", str(out_dir)], check=True)
    else:
        raise RuntimeError(f"No .pak/.utoc found in {set_dir}")


if __name__ == "__main__":
    import sys
    for abs_path, rel_path in file_iterator(sys.argv[1]):
        print(rel_path, "->", abs_path)