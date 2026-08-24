# Tool patches

`UERePacker.py` packs edited textures with two third-party tools, and neither works on
Final Fantasy VII Rebirth as shipped. This document lists every source change the
repacker depends on, with the reasoning and the evidence behind it, so the fixes can be
offered upstream.

The changes themselves are in `patches/cue4parse.patch` and `patches/unrealrezen.patch`.
They are cut against these commits, and rebuilding from nothing but a clone and those two
files is verified to work:

```bash
git clone https://github.com/rm-NoobInCoding/UnrealReZen.git src && cd src
git checkout bf9e8de4abb63b80267f3895bbb83e8b86f53df5
git submodule update --init --depth 1 --recursive external/CUE4Parse   # 9539d4d8
git apply ../patches/unrealrezen.patch
git -C external/CUE4Parse apply ../patches/cue4parse.patch
dotnet publish UnrealReZen/UnrealReZen.csproj -c Release -r linux-x64 \
  --self-contained false -o ~/Programs/Mo/UnrealReZen
```

A binary built that way and the one in use produce the same `.utoc` byte for byte, chunk
hashes included, and the same chunk payloads. Both projects are built from source anyway,
so they live in the local checkouts:

- [CUE4Parse](https://github.com/FabianFG/CUE4Parse), pulled in as UnrealReZen's `external/CUE4Parse` submodule
- [UnrealReZen](https://github.com/rm-NoobInCoding/UnrealReZen)

The third tool, [UE4-DDS-Tools](https://github.com/matyalatte/UE4-DDS-Tools), needs no
changes at all. It reads and writes this game's Zen assets correctly out of the box.

## The root cause

Rebirth's engine fork writes one extra `uint32` into `FIoContainerHeader`, between
`PackageCount` and the name arrays. Every tool that assumes the stock UE4.26 layout
reads each following field four bytes early, runs off the end of the buffer, and gives
up. This single field is why retoc reports `packages: 0` on the game's own containers,
and why CUE4Parse hands back a null container header.

The real layout, recovered from the raw header chunk:

| Offset | Field | Notes |
| --- | --- | --- |
| `0x00` | `u64 ContainerId` | |
| `0x08` | `u32 PackageCount` | |
| `0x0c` | `u32` | the extra field; zero in every container examined but one |
| `0x10` | `u32 NamesSize` | always 0 in practice |
| `0x14` | `u32 NameHashesSize` | always 8 |
| `0x18` | `u64` | name hash algorithm ID, `0xC1640000` |
| `0x20` | `u32 PackageIdCount` | |
| `0x24` | `u64 PackageIds[]` | |
| ... | `u32 StoreEntriesSize` | |
| ... | `FFilePackageStoreEntry[]` | 32 bytes each, plus out-of-line imported package IDs |

Validated against five containers: `pakchunk0`, `pakchunk0optional`, `pakchunk8optional`
(26,225 packages), `pakchunk11` (the one where the extra field holds 202 rather than 0),
and a mod-authored `f80h_plants_color_textures_P`. In each case `PackageIdCount` equals
`PackageCount` and `StoreEntriesSize` accounts exactly for 32 bytes per package plus
eight per imported package ID.

## CUE4Parse

### 1. Read the extra header field

`CUE4Parse/UE4/IO/Objects/FIoContainerHeader.cs`, in the `BeforeVersionWasAdded` branch:

```csharp
if (Ar.Game == EGame.GAME_FinalFantasy7Rebirth) Ar.Position += 4;
var namesSize = Ar.Read<int>();
```

`EGame.GAME_FinalFantasy7Rebirth` already exists and already carries game-specific
handling for meshes and Niagara, so this fits the established pattern.

### 2. Keep `LoadOrder`

`CUE4Parse/UE4/IO/Objects/FFilePackageStoreEntry.cs` skips over `LoadOrder` on the
pre-UE5 path. A packer that copies a store entry from a source container has no way to
reproduce it, so the field is now read into a public member rather than skipped.

### 3. Never leave `ShaderMapHashes` null

Same file. The pre-UE5 entry has no shader map hashes, and the field was simply left
null, while the UE5 path always assigns an array. Any consumer that enumerates it throws
`ArgumentNullException`. It is now assigned `Array.Empty<FSHAHash>()`.

This one is a plain bug, independent of Rebirth.

### Worth considering separately

`IoStoreReader.ReadContainerHeader` catches every exception and, for pre-UE5 games,
returns null without a word:

```csharp
catch (Exception)
{
    if (Game >= EGame.GAME_UE5_0) throw;
    else return null!;
}
```

That is what turned a parse failure into a silently broken output container. Logging the
exception at warning level would have made the whole problem obvious immediately.

## UnrealReZen

### 4. A partial game directory must not be fatal

`Program.TryLoadProvider` calls `provider.LoadLocalization(ELanguage.English)`, which
throws `'en' is not a valid culture` when the directory holds no localization data. That
is the normal case when pointing the tool at a single container rather than a full game
install, and it aborts the run. The call is now wrapped and logged at debug level.

### 5. Look store entries up by package, not by position

`Program.BuildManifest` had:

```csharp
foreach (var storeEntry in header.StoreEntries)
{
    manifest.Deps.ChunkIDToDependencies.TryAdd(chunkId.ChunkId, storeEntry);
}
```

`TryAdd` keeps the first value for a key, so every asset in a run received
`StoreEntries[0]`, the first store entry of whichever container it was found in, along
with that entry's export count and imported package list. `PackageIds` and `StoreEntries`
are parallel arrays, so the entry is now selected by the index of the package's own ID,
and a package the header does not index produces a warning instead of silent nonsense.

Texture-only mods likely never noticed, since one 2D texture's store entry looks like any
other. Anything with imports would have been packed with the wrong dependency list.

### 6. Report the size of the chunk actually being written

`FFilePackageStoreEntry.ExportBundlesSize` describes the export bundle chunk. Copying it
from the source container describes the *original* asset, which is wrong by definition
for a patch, and stock UnrealReZen sidesteps this by writing zero. The manifest builder
now records each packed `.uasset`'s length, keyed by package ID, and the writer uses it.

`LoadOrder` is carried across from the source entry, and the `0xFFFFFFFF` pad that
follows it is written rather than zeroed. Real containers, both the game's and mod
authors', have both.

### 7. Write the Rebirth header layout

`FIoDependencyFormat` gains a `UE4_FF7Rebirth` member, selected in `Packer.PackToCasToc`
when the engine version is `GAME_FinalFantasy7Rebirth`, and `WriteDependenciesAsUE4`
takes the format so `DepsHeader_UE4.Write` can emit the extra `uint32`.

The stock writer is four bytes short for this game. Mapping its field names onto the real
layout shows the drift clearly: what it calls `IDSize` sits where `NamesSize` belongs,
`Padding` lands on `NameHashesSize`, and everything after that is displaced.

### Worth considering separately

The tool crashes with `DirectoryNotFoundException` when the output path's directory does
not exist, rather than creating it or reporting the problem.

The eight bytes of padding between chunks in the `.ucas` are never initialised, so they
carry whatever happened to be in memory and change from run to run even with a fixed
`--container-id`. No chunk covers them and the `.utoc` hashes are unaffected, so nothing
reads them, but it does mean a build cannot be checked by comparing output byte for byte.

## Result

With all of the above, packing one edited texture against its source container produces
this header:

```
00000000: c4e6 22c1 3fe6 52a8 0100 0000 0000 0000  ContainerId, PackageCount=1, extra=0
00000010: 0000 0000 0800 0000 0000 64c1 0000 0000  Names=0, NameHashes=8, algo=0xC1640000
00000020: 0100 0000 599e 2266 4d18 28c9 2000 0000  1 package ID, StoreEntriesSize=32
00000030: 0d10 0000 0000 0000 0100 0000 0100 0000  ExportBundlesSize=4109, exports=1, bundles=1
00000040: a100 0000 ffff ffff 0000 0000 0000 0000  LoadOrder=161, pad, no imported packages
```

`ExportBundlesSize` matches the injected asset's size on disk exactly, and `LoadOrder`
matches the value the source mod container carries for the same package. Mounting the
result in CUE4Parse and decoding the texture back returns mip payloads byte-identical to
the edited `.dds` that went in.
