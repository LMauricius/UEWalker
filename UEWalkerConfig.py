# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


#: Mod folder to walk. Overridden by argv[1] when run as a script.
MOD_ROOT = "/path/to/mod"

#: Output tree, owned by the consumer: it writes each edited texture here under the
#: relative path it was yielded, keeping the name. The walk adds the `.dds.json`
#: sidecars, a marker per finished container, and -- only where an edit appeared --
#: the cooked package it came from, under `<container>/_cooked`.
OUT_DIR = "/path/to/output"

#: IoStore container packer. The walk never runs it, since containers are read
#: through CUE4Parse; it is the later patch pass that needs it, to build a `_P`
#: container out of the edited assets and `_cooked` payloads in `OUT_DIR`.
#: Prebuilt Linux binaries: https://github.com/trumank/retoc/releases
RETOC_PATH = "retoc"

#: Legacy `.pak` packer for that same pass, reached only by a container set with no
#: `.utoc`, since retoc speaks IoStore only. Leave as-is for a pure IoStore mod.
#: https://github.com/trumank/repak/releases
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
GAME_PAKS: str | None = "/path/to/game/End/Content/Paks"

#: AES key for encrypted containers. Mod-authored containers are normally plain.
AES_KEY: str | None = None

#: Oodle library (`liboodle-data-shared.so`). None lets CUE4Parse fetch an
#: open-source build itself on first use: https://github.com/WorkingRobot/OodleUE
OODLE_LIB: str | None = None

#: Engine version. Cooked assets are not self-describing, and the two tools spell
#: it differently: retoc takes `UE4_26`, CUE4Parse takes the `EGame` member name.
RETOC_VERSION = "UE4_26"
UE_VERSION = "GAME_UE4_26"

UEWALKER_DEBUG = True


#: Keep an untouched copy of every file an edit reached, as `backup-<name>` beside
#: the edit in `OUT_DIR`. A deliberate duplicate: the same mip bytes are already in
#: that texture's `_cooked` payload, this is just the readable form of them.
BACKUP = False


#: Resume mode: treat `OUT_DIR` as work already done. A container marked finished is
#: skipped before its payload is extracted, and inside one that was interrupted, a
#: file already at its relative path is neither decoded nor yielded again.
SKIP_EXISTING = True
