# CafeModManager

A portable, open-source mod manager for **Cafe Business Manager**. It lists the
BepInEx plugins already installed in your game, lets you enable or disable them
with one click, and gives you a GUI editor for their `.cfg` parameters.

## Features

- Scans `<game>\BepInEx\plugins` and recognises both single-`.dll` mods and
  folder mods (folder containing a `.dll` plus assets).
- Enable / disable by renaming `.dll` <-> `.dll.disabled`. Non-destructive:
  nothing is deleted and the change can always be reverted.
- Parameter editor for BepInEx `.cfg` files (bool / number / text / dropdown),
  with an automatic `.cfg.bak` backup before the first edit.
- One-click BepInEx install: downloads the official x64 release from GitHub and
  extracts it into the game folder.
- 12 UI languages.
- No telemetry. The only network access is the BepInEx download you trigger
  yourself.

## Usage

1. Download the release archive and extract it anywhere.
2. Run `CafeModManager.exe`.
3. **Settings** -> set your game folder (Steam: Library -> right-click the game
   -> Manage -> Browse local files).
4. Click **Install BepInEx**.
5. Put mods into `<game>\BepInEx\plugins`, launch the game once so BepInEx
   generates their `.cfg` files, then manage them in the tool.

## Supported mods

- Any plugin BepInEx loads from `BepInEx\plugins`: a single `.dll`, or a folder
  containing a `.dll` plus its assets. Listing, enabling and disabling work for
  all of them.
- Parameter editing requires the mod to write a BepInEx config file (`.cfg`).
  Mods that store settings in their own format (JSON / INI / custom) or have no
  settings at all show "No Config" – that is normal, not an error.
- Not covered: mods loaded from `BepInEx\patchers` (preloader patchers), mods
  built for other loaders, and mods that require a third-party installer.

## Build from source

Requirements: Windows, Python 3.11+, PyInstaller.

```bat
pip install -r requirements.txt
python -m PyInstaller --noconfirm CafeModManager.spec
```

Output: `dist\CafeModManager.exe`.

The application uses **only the Python standard library** (tkinter, urllib,
zipfile, json, ...), so nothing has to be bundled besides the two source files.
`version_info.txt` supplies the Windows version resource. UPX compression is
disabled in the spec (`upx=False`); nothing is obfuscated or packed beyond what
PyInstaller does by default.

`config.json` is created on first launch and is intentionally not tracked.

## Antivirus false positives

PyInstaller onefile executables are commonly flagged by heuristic engines: the
bootloader is self-extracting, contains an embedded PYZ archive, and is not
code-signed. Those detections are generic and not evidence of malicious code.
The released file is built from this repository with the committed
`CafeModManager.spec`; rebuilding from the same sources produces an equivalent
executable (only build-machine paths embedded by PyInstaller differ).

## License

MIT – see [LICENSE](LICENSE).
