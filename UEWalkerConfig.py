# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


#: Mod folder to walk. Overridden by argv[1] when run as a script.
SOURCE_ROOT_DIR = "/path/to/source"

#: Edit tree, owned by the consumer: it writes each edited texture here under the
#: relative path it was yielded, keeping the name. The walk adds the `.dds.json`
#: sidecars and a marker per finished container; the cooked package behind an edit goes
#: to `UASSET_DIR` instead.
EDIT_ROOT_DIR = "/path/to/edits/"

#: Original cooked assets, mirroring `EDIT_ROOT_DIR`'s tree: a container directory there
#: holds, at the package's own path, the `.uasset` (plus `.ubulk`/`.uptnl`) every edited
#: texture was decoded from. Written by the walk only where an edit appeared, and read
#: back by the repacker as the payload an edit is spliced into. Kept apart from the edits
#: so the edit tree stays small enough to hand around; both trees are needed to repack.
UASSET_DIR = "/path/to/uassets"

#: Packed patch containers for the new mod, mirroring `EDIT_ROOT_DIR`'s tree.
PATCH_ROOT_DIR = "/path/to/results"

#: UE4-DDS-Tools `src` directory, imported by the repacker to write an edited `.dds`
#: back into the cooked Zen asset it came from. It rewrites the texture's dimensions,
#: mip count and bulk descriptors, which is what a downscaled edit needs. Pure Python
#: and MIT licensed; the bundled `texconv` library is never reached, since it is only
#: loaded for sources that are not already `.dds`.
#: https://github.com/matyalatte/UE4-DDS-Tools
UE4_DDS_TOOLS = "UE4-DDS-Tools/src"

#: UnrealReZen, which packs the edited assets into a `.utoc`/`.ucas`/`.pak` triplet.
#: Must be the locally patched build: stock UnrealReZen and the CUE4Parse it bundles
#: both misread this game's container header and silently emit a container that
#: declares no packages. `TOOL-PATCHES.md` lists every change and why.
#: https://github.com/rm-NoobInCoding/UnrealReZen
UNREALREZEN_PATH = "UnrealReZen/UnrealReZen"

#: Container the repacker reads chunk IDs and package store entries out of. Only the
#: `.utoc` directory indexes and one header chunk per container are touched, never the
#: `.ucas` payload, so pointing this at the whole game costs seconds rather than the
#: 150 GB the folder holds. None falls back to `GAME_PAKS`.
REZEN_GAME_DIR: str | None = None

#: Compression for the packed container: None, Zlib, Oodle, or LZ4. `None` keeps the
#: output readable and the packer dependency-free; Oodle matches what the game ships.
PATCH_COMPRESSION = "None"

#: IoStore inspector. Useful for looking at a container, but it cannot pack for this
#: game: it misreads the same header field UnrealReZen did, which is why `to-legacy`
#: reports `packages: 0` and writes nothing.
#: Prebuilt Linux binaries: https://github.com/trumank/retoc/releases
RETOC_PATH = "retoc"

#: Legacy `.pak` packer, reached only by a container set with no `.utoc`. Leave as-is
#: for a pure IoStore mod. https://github.com/trumank/repak/releases
REPAK_PATH = "repak"

#: CUE4Parse.dll, loaded in-process through pythonnet. Build it with
#: `dotnet build -c Release`; sibling dependency DLLs are resolved from the same
#: directory. https://github.com/FabianFG/CUE4Parse (FModel, its GUI: https://fmodel.app)
CUE4PARSE_DLL = "/path/to/CUE4Parse.dll"


#: `CUE4Parse.runtimeconfig.json` from the same publish output. pythonnet needs it to
#: pick the runtime version the assembly was built for; None lets it guess, which only
#: works when exactly one matching runtime is installed.
DOTNET_RUNTIME_CONFIG: str | None = (
    "/home/mauricios/Programs/Mo/CUE4Parse/c4pfetch.runtimeconfig.json"
)


#: The game's `Content/Paks` folder, read only for `global.utoc`/`global.ucas`.
#: An IoStore package keeps its script-object table in that global container rather
#: than in the container shipping it, so a mod container cannot be decoded on its own.
#: Only the two global files are ever mounted; the rest of the folder is untouched.
#: None means IoStore mods will not decode, though legacy `.pak` ones still will.
GAME_PAK_DIR: str | None = "/path/to/game/End/Content/Paks"

#: AES key for encrypted containers. Mod-authored containers are normally plain.
AES_KEY: str | None = None

#: Oodle library (`liboodle-data-shared.so`). None lets CUE4Parse fetch an
#: open-source build itself on first use: https://github.com/WorkingRobot/OodleUE
OODLE_LIB: str | None = None

#: Engine version. Cooked assets are not self-describing, and the two tools spell
#: it differently: retoc takes `UE4_26`, CUE4Parse takes the `EGame` member name.
RETOC_VERSION = "UE4_26"
UE_VERSION = "GAME_UE4_26"

#: Engine version handed to UE4-DDS-Tools, which spells it differently again.
DDS_TOOLS_VERSION = "4.26"

#: Engine version handed to UnrealReZen. Unlike the walker, the packer needs the
#: game-specific member: the container header layout differs from stock UE4.26, and
#: the patched CUE4Parse keys its fix off this value.
REZEN_VERSION = "GAME_FinalFantasy7Rebirth"

UEWALKER_DEBUG = True


#: Keep an untouched copy of every file an edit reached, as `backup-<name>` beside
#: the edit in `EDIT_ROOT_DIR`. A deliberate duplicate: the same mip bytes are already in
#: that texture's `_cooked` payload, this is just the readable form of them.
BACKUP = False


#: Resume mode: treat `EDIT_ROOT_DIR` as work already done. A container marked finished is
#: skipped before its payload is extracted, and inside one that was interrupted, a
#: file already at its relative path is neither decoded nor yielded again.
SKIP_EXISTING = True
