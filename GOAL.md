This is a Python script that yields pairs of (absolute input path, relative output path). It is meant for walking and editing textures in an existing Unreal Engine game mod.

I'll need a script with one important function: fileIterator->tuple[str,str] that returns pairs of the absolute path to an extracted path and relative path from the mod root.

The mod folder is structured as follows:

- A number of .7z files in the mod root
- Each .7z file contains a directory structure. Some files are plain .png or other images. Some files are further compressed archives:
- Those other files in the .7z are .pak, .ucas and .utoc files. They should also contain image files

Iterate over all actual files inside the mod. Each iteration goes as follows:
- Extract the file to a temporary location (recursively, don't extract full archives if not needed) Preserve extension
- Yield a tuple (temp file abs filepath, relative path to mod root)
- Before extracting the next file, delete/unlink the last one

The relative path should be constructed as follows:
- Starts from the mod root
- Each archive file becomes a directory segment. Preserve archive extension, add -extracted (e.g. abc.7z-extracted/def.ucas-extracted/texture.dds)

Process only image files