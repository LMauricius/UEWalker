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

#: Legacy .pak unpacker, needed only for sets that have no .utoc; retoc handles
#: IoStore only. Leave as-is if the mod ships pure IoStore containers.
#: https://github.com/trumank/repak/releases
REPAK_PATH = "repak"

#: CUE4Parse.dll, loaded in-process through pythonnet. Build it with
#: `dotnet build -c Release`; sibling dependency DLLs are resolved from the same
#: directory. https://github.com/FabianFG/CUE4Parse (FModel, its GUI: https://fmodel.app)
CUE4PARSE_DLL = "/path/to/CUE4Parse.dll"

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


#: Keep an untouched copy of every yielded file as `backup-<name>` in `OUT_DIR`,
#: so an edit can be compared against, or reverted to, the original.
BACKUP = False
