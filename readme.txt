Cafe Business Manager · ModManagerTool​
Works with any BepInEx plugin mod, wherever it was downloaded from (Nexus or anywhere else). The tool only reads the mods already installed in your game so you can manage and configure them quickly — it does not download or install mods.

Supported Mods

Any plugin BepInEx loads from BepInEx/plugins: a single .dll, or a folder containing a .dll plus its assets. Listing, Enabling and Disabling work for all of them.
Parameter editing requires the mod to write a BepInEx config file (.cfg). Mods that store settings in their own format (JSON / INI / custom) or have no settings at all show "No Config" — that is normal, not an error.
Not covered: mods loaded from BepInEx/patchers (preloader patchers), mods built for other loaders (MelonLoader etc.), and mods that come with a third-party installer or need extra manual setup.

How to Use

1.Open Settings in the top-right corner and enter your game install directory.
Can't find it? Steam Library → right-click the game → Manage → Browse local files.
2.Click Install BepInEx. The tool automatically downloads the latest version from the official BepInEx release page and installs it into your game directory.
3.Put the downloaded mod's .dll file into the BepInEx/plugins folder inside your game directory. If the mod ships with assets, drop the whole folder in.
4.Launch the game once so BepInEx generates each mod's config file (.cfg, in BepInEx/config).
5.Close the game and open this tool. Installed mods are listed on the right; click Edit Params for the mod you want, adjust the values, then save and restart the game for them to take effect.

Enabling / Disabling Mods

Each mod has a checkbox on its left: checked = enabled, unchecked = disabled. Click Save in the top-right corner to apply, then restart the game to load the change.
Disabling does not delete files — it only renames .dll to .dll.disabled (BepInEx only loads *.dll). Check the box again and save to rename it back; you can revert at any time. For folder-type mods with assets, every .dll inside the folder is renamed.
Close the game before saving. While the game is running the .dll is locked and renaming will fail.

Notes

The first time you save a mod's parameters, the tool automatically backs up its cfg (.cfg.bak). Click Restore from Backup in the parameter window to roll back to the state before your first edit at any time.
Hotkey conflicts are checked when you save: if a key you just set is already bound by another mod, or by another parameter of the same mod, a dialog lists the conflicting entries and asks whether to save anyway. Only plugins currently installed in BepInEx/plugins are counted, so leftover configs from removed mods do not trigger false warnings.
If multiple cfg files are detected for the same mod (usually because an old config wasn't deleted after a mod update), a warning appears at the top of the parameter window and the newest file is edited by default. If your changes don't take effect, delete the leftover old cfg as prompted.

UI Language

Settings → UI Language in the top-right corner switches between 12 languages: English / Русский / Deutsch / Français / Español / Italiano / Türkçe / Português (Brasil) / Chinese (Simplified) / 日本語 / 한국어 / Polski.
Mod parameter descriptions are shown as written in the cfg and are not translated with the UI — whatever language the author used is what you see. Every configurable entry in the cfg (bool / number / text / dropdown, etc.) is listed; no parameters are hidden.
