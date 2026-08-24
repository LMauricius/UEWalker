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


#: Keep an untouched copy of every yielded file as `backup-<name>` in `OUT_DIR`,
#: so an edit can be compared against, or reverted to, the original.
BACKUP = False


#: Resume mode: treat `OUT_DIR` as work already done. An image whose real relative
#: path is already there is neither extracted nor yielded, a container's cooked tree
#: is unpacked only when missing, and an existing sidecar is left untouched.
SKIP_EXISTING = True
