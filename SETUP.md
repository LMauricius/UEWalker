# Setup

Everything UEWalker needs beyond the Python standard library. Written for Ubuntu 24.04
and derivatives (KDE neon included); other distributions differ only in the package
manager lines.

Four pieces are involved:

| Dependency                              | Why                                                                                                           | Source                                                                                                                                        |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `py7zr`                                 | reads the mod's `.7z` archives                                                                                | `pip install py7zr` ([PyPI](https://pypi.org/project/py7zr/))                                                                                 |
| `retoc`                                 | converts IoStore containers (`.utoc`/`.ucas`) to legacy cooked assets, and back again for the write-back pass | [github.com/trumank/retoc](https://github.com/trumank/retoc) ([releases](https://github.com/trumank/retoc/releases), prebuilt Linux binaries) |
| .NET 10 + `pythonnet` + `CUE4Parse.dll` | reads cooked `Texture2D` assets and hands their raw mips to Python                                            | the [CUE4Parse](https://www.nuget.org/packages/CUE4Parse) NuGet package ([source](https://github.com/FabianFG/CUE4Parse))                     |
| Oodle                                   | decompresses UE 4.26 container data on CUE4Parse's behalf                                                     |                                                                                                                                               |

`repak` is a fifth, optional piece: it is needed only if a mod ships a legacy `.pak`
with no `.utoc` beside it.

## 1. Python packages

```bash
pip install py7zr pythonnet
```

`pythonnet` needs a .NET runtime present at import time, so install .NET first if you
hit an error here.

## 2. .NET 10

```bash
sudo apt install dotnet-sdk-10.0
```

This is needed on Linux. The SDK builds CUE4Parse from source; the `aspnetcore-runtime-10.0`
package is the runtime `pythonnet` actually needs at import time.
If you got CUE4Parse without building, you can just install `aspnetcore-runtime-10.0` instead.

Version 10 is what current CUE4Parse targets (`net10.0`). Keep the runtime in step with
whatever your copy was built for. Check the build's
`bin/Release/` directory name if you are unsure.

Add those two exports to your shell profile; `pythonnet` locates the runtime through
`DOTNET_ROOT`.

## 3. retoc

Prebuilt binaries are published per release. Pick the `x86_64-unknown-linux-gnu`
tarball:

```bash
mkdir -p ~/.local/bin
curl -sSL https://github.com/trumank/retoc/releases/download/v0.1.5/retoc_cli-x86_64-unknown-linux-gnu.tar.xz \
  | tar -xJ -C /tmp
install /tmp/retoc_cli-x86_64-unknown-linux-gnu/retoc ~/.local/bin/
retoc --version    # retoc_cli 0.1.5
```

Make sure `~/.local/bin` is on your `PATH`. Newer releases live at
<https://github.com/trumank/retoc/releases>.

## 4. CUE4Parse

CUE4Parse ships as a NuGet package.
NuGet is .NET's package feed, the `pip` of that ecosystem, and
the SDK talks to it for you. CUE4Parse is a DLL to be used by other .NET programs,
and it is pulled as a dependency during the .NET app building process.
Since this is a Python project, one awkward part is that a NuGet package is fetched into a
shared cache rather than into a folder you can point at, so the job here is to make the
SDK gather CUE4Parse and its dependencies into one directory of your choosing.

An empty class library does that, since a project is what NuGet installs into:

```bash
dotnet new classlib -o cue4parse-fetch
cd cue4parse-fetch
dotnet add package CUE4Parse
dotnet add package CUE4Parse-Conversion
dotnet publish -c Release -o ./CUE4Parse -p:EnableDynamicLoading=true
```

Three details make that work:

- `publish`, not `build`. It restores the packages and then copies every assembly next
  to the output. `build` leaves the dependencies in the cache and gives you a lone
  `CUE4Parse.dll`, which binds and then fails on the first type that needs one.
- `-p:EnableDynamicLoading=true`. A class library otherwise publishes without a
  runtimeconfig, and that file is what pins the .NET version to boot.
- The project's own name. `dotnet new classlib` targets whichever framework your SDK
  installs, so the versions stay consistent by themselves, and the runtimeconfig is
  named after the project.

Around 40 files land in the output directory. Two of them go into the config:
`CUE4Parse.dll` into `CUE4PARSE_DLL`, and `cue4parse-fetch.runtimeconfig.json` into
`DOTNET_RUNTIME_CONFIG`. The rest resolve automatically from that directory, so keep
them together. The `cue4parse-fetch` project itself can be deleted; recreate it when you
want a newer release, or just run the two `dotnet add package` lines again and republish.

`checktools.py` reads the published `deps.json` and names any assembly that did not make
it into the directory.

## 5. Oodle

FF7 Rebirth-era containers (UE 4.26) are Oodle-compressed, and CUE4Parse needs the
native Oodle library to read them. On Linux the file is called
`liboodle-data-shared.so` (`oodle-data-shared.dll` on Windows; older builds went by
`oo2core_9_win64.dll`).

Usually there is nothing to do here. The script always hands `OodleHelper.Initialize()`
a real path (by default beside `CUE4Parse.dll`), and CUE4Parse downloads an open-source
Oodle build from [OodleUE](https://github.com/WorkingRobot/OodleUE) into it on first use,
then reuses that copy. This needs network access once.

The path matters: `Initialize` is overloaded on a string and on an `Oodle` instance, and
a null selects the second, quietly installing an empty decompressor. Containers then read
as corrupt much later on, so the script checks that a library really was loaded.

For an offline machine, fetch `gcc-x64-release.zip` from the OodleUE releases page
yourself, extract `liboodle-data-shared.so`, and point the global at it:

```python
OODLE_LIB = "/path/to/liboodle-data-shared.so"
```

The library shipped with the game or with an FF7R modding toolkit works equally well.

## 6. The game's global container

An IoStore package stores its script-object table in the game's `global.utoc`, not in
the container that ships it, so a mod container cannot be decoded on its own. Point
`GAME_PAKS` at the game's `Content/Paks` folder:

```python
GAME_PAKS = "/path/to/FINAL FANTASY VII REBIRTH/End/Content/Paks"
```

Only `global.utoc` and `global.ucas` are ever read (about 2 MB together); the rest of
the folder is left alone and never indexed. Leaving this as `None` is allowed: legacy
`.pak` mods still decode, while IoStore ones report `global data is missing` per asset.

## 7. repak (optional)

Only if a mod contains a `.pak` with no matching `.utoc`:

```bash
curl -sSL https://github.com/trumank/repak/releases/latest/download/repak_cli-x86_64-unknown-linux-gnu.tar.xz \
  | tar -xJ -C /tmp
install /tmp/repak_cli-x86_64-unknown-linux-gnu/repak ~/.local/bin/
```

## 8. Point the script at everything

Edit `UEWalkerConfig.py`:

```python
MOD_ROOT      = "/path/to/mod"
OUT_DIR       = "/path/to/output"
RETOC_PATH    = "retoc"                                   # or an absolute path
REPAK_PATH    = "repak"
CUE4PARSE_DLL = "/home/you/Programs/CUE4Parse/CUE4Parse.dll"   # a publish output; `~` is not expanded
GAME_PAKS     = "/path/to/game/End/Content/Paks"          # read only for global.utoc/.ucas
BACKUP        = False                                     # True keeps a backup- copy of each file
OODLE_LIB     = None                                      # None = let CUE4Parse fetch it
AES_KEY       = None                                      # mod containers are normally plain
RETOC_VERSION = "UE4_26"
UE_VERSION    = "GAME_UE4_26"
DOTNET_RUNTIME_CONFIG = "/home/you/Programs/CUE4Parse/cue4parse-fetch.runtimeconfig.json"
```

`DOTNET_RUNTIME_CONFIG` matters when more than one .NET runtime is installed (having
both 8.0 and 10.0 side by side is common): it tells `pythonnet` which version to boot
instead of letting it guess. The file sits beside `CUE4Parse.dll` in the build output.

## 9. Verify

```bash
python checktools.py                  # every dependency, in the order the walk hits them
python checktools.py /path/to/x.utoc  # plus a real unpack and decode
python UEWalker.py
```

`checktools.py` prints one `ok` / `FAIL` / `skip` line per dependency and exits with the
number of failures. It round-trips a small archive through py7zr, launches retoc and
checks it supports the configured engine version, inspects the .NET layout before any
CLR boot (hosting runtime present, publish output complete), then loads CUE4Parse and
resolves the configured `EGame` member.

The lighter check `require_tools()` runs automatically at the start of every walk, so a
misconfigured path fails immediately rather than halfway through a mod.

## Verification status

`retoc` is confirmed against release v0.1.5: `to-legacy <utoc> <outdir>` and the global
`-a/--aes-key` flag both match what the script calls.

The CUE4Parse side is confirmed against a built assembly and a real mod, decoding 567
textures to valid BC1 DX10 `.dds` files with matching sidecars.

One rough edge remains on the retoc side. Mod containers often carry a `ContainerHeader`
retoc cannot parse, and `to-legacy` then writes nothing at all without reporting a
failure; the script notices the empty output and falls back to `unpack`. Supplying the
game's `global.utoc` does not help, so those containers currently yield Zen-format cooked
assets, which decode fine but are not yet a write-back target.
