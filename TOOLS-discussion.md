I'll research the current state of UE archive tools, focusing on Linux support and single-file extraction.retoc (Rust, cross-platform) looks like the strongest candidate. Let me dig into its actual capabilities — Linux support and whether it can do anything short of full-container extraction.retoc's subcommand list is the key find — `list`, `get` (single chunk to stdout), and `unpack`. Let me verify what single-file extraction actually looks like, and check the FF7 Rebirth specifics (engine version, encryption) plus the critical question of what format textures come out as.Good news and one important reality check came out of this. Let me give you the landscape, then the catch that changes your script design.

## The best tool: retoc

[retoc](https://github.com/trumank/retoc) (by trumank) is clearly the strongest fit for what you want. It's written in Rust, MIT-licensed, actively maintained (latest release v0.1.5, Jan 2026), and ships Linux builds. It handles the full IoStore container set (`.utoc`/`.ucas`/`.pak`). Its subcommands map almost exactly onto your needs:

- `list` — lists files in a `.utoc` via the directory index
- `get` — **gets a chunk by index and writes it to stdout** (this is your single-item extraction primitive)
- `unpack` — extracts chunks (files) from a `.utoc`
- `unpack-raw` — extracts raw chunks
- `to-legacy` — converts Zen assets to legacy `.pak` format (`.uasset`/`.uexp`)

So single-item extraction *is* possible: `list` to enumerate, then `get --chunk-index N` piped to a file. One caveat I couldn't fully confirm from the docs — whether `unpack` accepts a path filter to pull one named asset (ZenTools has an explicit `-PackageFilter` for this; retoc's per-file filtering I'd verify empirically before relying on it). The `get`-by-index route is documented and reliable.

## The reality check that matters for your script

Your spec assumes the `.pak`/`.ucas`/`.utoc` files "contain image files." They don't — not as `.png` or `.dds`. UE IoStore stores **cooked Zen packages**: `.uasset` + `.uexp` + `.ubulk`. A texture is a serialized `Texture2D` object, and its pixel data (BC-compressed mips) lives inside `.ubulk`/`.uexp` as an engine-specific payload, not as a standalone DDS you can open.

That means extracting a container gives you `.uasset`/`.ubulk`, and there's a **decode step** between "extracted" and "editable image":

- **retoc / ZenTools / UnrealPak** get you *out of the container* → cooked assets.
- To turn a cooked `Texture2D` into an actual DDS/PNG you need a **CUE4Parse-based** tool. FModel is the GUI for this; the underlying library (CUE4Parse, C#/.NET, runs on Linux via .NET) is what you'd script for a headless pipeline. There is no equally-mature pure-CLI Linux texture decoder — this is the weak point of a fully-automated Linux flow.

So your `fileIterator` for the container layer should yield `.uasset`/`.ubulk` (or run the decode and yield the resulting `.dds`), not assume images fall out directly. Only the loose files sitting at the top level of each `.7z` are genuinely `.png`/images.

## Can you avoid extracting the .7z?

Partially, and the distinction is useful:

- **The container triplet must land on disk as a set.** retoc reads `.utoc` + `.ucas` (+ often `.pak`) together from filesystem paths — it doesn't accept a container over stdin. So those members have to be extracted from the `.7z` to a temp dir before retoc touches them.
- **But you don't need to unpack the whole `.7z`.** You already do targeted member extraction with `py7zr`'s `targets=`. Extract just the `.pak`/`.ucas`/`.utoc` triplet (and loose images individually), never the full archive. That satisfies "don't extract full archives if not needed."

Net: loose images can be streamed one at a time; container sets need the triplet on disk, then retoc, then a decode pass.

## FF7 Rebirth specifics worth knowing

- The game is **UE 4.26** and uses **Oodle** compression — retoc/FModel need Oodle available to decompress. FF7 Rebirth mod tooling on Nexus explicitly ships an Oodle DLL for this reason.
- The **base-game** paks are **AES-encrypted** (you'd need the key via FModel's key repo). But a **downloaded mod's** containers are almost always **unencrypted** — modders build them with UnrealReZen, which doesn't re-apply the game's encryption. Since you're processing ready-made mods, you likely won't need an AES key at all, but retoc accepts `-a/--aes-key` if a given container turns out to need one.

## Tool comparison

| Tool                          | Platform                | Reads IoStore | Single-file             | Output                         | Notes                                                         |
| ----------------------------- | ----------------------- | ------------- | ----------------------- | ------------------------------ | ------------------------------------------------------------- |
| **retoc**                     | Linux/Win/Mac (Rust)    | Yes           | `get` by index → stdout | Cooked `.uasset`/legacy `.pak` | Best scriptable fit; MIT; active                              |
| **ZenTools**                  | Windows (C++)           | Yes           | `-PackageFilter`        | Cooked `.uasset`/`.uexp`       | Good filtering, but Windows                                   |
| **CUE4Parse / FModel**        | .NET (Linux via dotnet) | Yes           | Per-asset export        | **Decoded PNG/DDS**            | The only thing that gives real images; scriptable but heavier |
| **UnrealReZen / UnrealUnZen** | Windows                 | Partial       | —                       | —                              | Pack-oriented; the FF7 mod scene's repack tool                |
| **UnrealPak**                 | Windows (engine)        | `.pak` only   | —                       | Raw                            | Needs engine install; not IoStore-native                      |

## Practical recommendation for your pipeline

A realistic Linux-friendly flow is two-stage rather than one tool: **retoc** to get cooked assets out of each container (fast, single binary), then **CUE4Parse** to decode `Texture2D` → DDS for the assets you actually want to edit. If you're willing to run .NET, you could arguably do both extract *and* decode with a CUE4Parse script and skip retoc — that collapses it to one dependency and gives you true per-asset selection.

Two things would let me refine your `file_iterator` precisely:

1. When you open one of the mod's `.pak`/`.ucas`/`.utoc` sets in FModel or your other project's tooling, do the textures show up as `Texture2D` assets (cooked) — confirming the decode step — or has the mod author done something unusual?
2. Is running **.NET/CUE4Parse** on your target machine acceptable, or do you need to stay within native binaries only? That determines whether I wire the container path to retoc-plus-decode or to a single CUE4Parse step.