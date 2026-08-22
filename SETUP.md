# Setup

Everything UEWalker needs beyond the Python standard library. Written for Ubuntu 24.04
and derivatives (KDE neon included); other distributions differ only in the package
manager lines.

Four pieces are involved:

| Dependency                              | Why                                                                                                           | Source                                                                                                                                        |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| `py7zr`                                 | reads the mod's `.7z` archives                                                                                | `pip install py7zr` ([PyPI](https://pypi.org/project/py7zr/))                                                                                 |
| `retoc`                                 | converts IoStore containers (`.utoc`/`.ucas`) to legacy cooked assets, and back again for the write-back pass | [github.com/trumank/retoc](https://github.com/trumank/retoc) ([releases](https://github.com/trumank/retoc/releases), prebuilt Linux binaries) |
| .NET 10 + `pythonnet` + `CUE4Parse.dll` | reads cooked `Texture2D` assets and hands their raw mips to Python                                            | no prebuilt CLI exists; build against [CUE4Parse](https://github.com/FabianFG/CUE4Parse).                                                     |
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

If your distribution does not package these, use Microsoft's installer script:

```bash
curl -sSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 10.0
export DOTNET_ROOT="$HOME/.dotnet"
export PATH="$DOTNET_ROOT:$PATH"
```

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

Ideally build it once:

```bash
git clone --recursive https://github.com/FabianFG/CUE4Parse.git
cd CUE4Parse
dotnet build CUE4Parse/CUE4Parse.csproj -c Release
```

`--recursive` matters: CUE4Parse pulls its compression backends in as submodules. The
build drops `CUE4Parse.dll` and its dependencies in
`CUE4Parse/bin/Release/net10.0/`; that path goes into `CUE4PARSE_DLL`. Sibling DLLs in
the same directory are resolved automatically, so do not move the file out on its own.

## 5. Oodle

FF7 Rebirth-era containers (UE 4.26) are Oodle-compressed, and CUE4Parse needs the
native Oodle library to read them. On Linux the file is called
`liboodle-data-shared.so` (`oodle-data-shared.dll` on Windows; older builds went by
`oo2core_9_win64.dll`).

Usually there is nothing to do here. The script calls `OodleHelper.Initialize()` with
no path, and CUE4Parse then downloads an open-source Oodle build from
[OodleUE](https://github.com/WorkingRobot/OodleUE) on first use and caches it. That
needs network access once.

For an offline machine, fetch `gcc-x64-release.zip` from the OodleUE releases page
yourself, extract `liboodle-data-shared.so`, and point the global at it:

```python
OODLE_LIB = "/path/to/liboodle-data-shared.so"
```

The library shipped with the game or with an FF7R modding toolkit works equally well.

## 6. repak (optional)

Only if a mod contains a `.pak` with no matching `.utoc`:

```bash
curl -sSL https://github.com/trumank/repak/releases/latest/download/repak_cli-x86_64-unknown-linux-gnu.tar.xz \
  | tar -xJ -C /tmp
install /tmp/repak_cli-x86_64-unknown-linux-gnu/repak ~/.local/bin/
```

## 7. Point the script at everything

Edit `UEWalkerConfig.py`:

```python
MOD_ROOT      = "/path/to/mod"
OUT_DIR       = "/path/to/output"
RETOC_PATH    = "retoc"                                   # or an absolute path
REPAK_PATH    = "repak"
CUE4PARSE_DLL = "/home/you/src/CUE4Parse/CUE4Parse/bin/Release/net10.0/CUE4Parse.dll"
BACKUP        = False                                     # True keeps a backup- copy of each file
OODLE_LIB     = None                                      # None = let CUE4Parse fetch it
AES_KEY       = None                                      # mod containers are normally plain
RETOC_VERSION = "UE4_26"
UE_VERSION    = "GAME_UE4_26"
DOTNET_RUNTIME_CONFIG = None   # or the CUE4Parse.runtimeconfig.json beside the DLL
```

`DOTNET_RUNTIME_CONFIG` matters when more than one .NET runtime is installed (having
both 8.0 and 10.0 side by side is common): it tells `pythonnet` which version to boot
instead of letting it guess. The file sits beside `CUE4Parse.dll` in the build output.

## 8. Verify

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
`-a/--aes-key` flag both match what the script calls. The CUE4Parse side (type names,
`PlatformData.Mips`, `BulkData.Data`) is written from documentation and has not yet
been run against a built assembly. Expect to adjust `TextureDecoder` on first contact.
