# BG3 Mod Merger + Icon Add Utility

A Windows desktop tool for **merging two Baldur's Gate 3 Toolkit mods into a single mod**, with a built-in **icon generator** for spells, items, classes, action resources, and portraits.

Made by For_Kiramay.

---

## What it does

### Merge two mods into one

Pick two mod projects from your BG3 Toolkit workspace and combine them into a new mod that contains the content of both. The merger handles:

- All four Toolkit subtrees (`Mods/`, `Public/`, `Editor/Mods/`, `Projects/`) plus the `Generated/Public/` tree where the Toolkit bakes virtual textures and models
- Structural merging of registry files (root templates, banks, UI atlases, virtual texture banks, GUI metadata, stats, localization) so two mods with overlapping registries combine cleanly instead of one overwriting the other
- Cross-mod identifier collisions - duplicate stat names, conflicting UUIDs, identically-named asset files - with configurable resolution policies (skip / prefix / fail)
- Path remapping so any reference to the original mod folders gets rewritten to the merged mod's folder name throughout the output

Two merge modes:

- **Create a new merged mod** in your workspace (recommended)
- **Combine in place** by merging mod B's content into mod A's existing folder

### Add icons to a mod

Generate the full icon asset set BG3 needs from a single source PNG. Pick a mod, name your icon, pick the icon type, choose the PNG, click Add. The tool writes the DDS files at every required size, builds or extends the hotbar atlas, generates the UV map and TextureBank, and registers everything in the mod's `metadata.lsf`.

For spell/skill/passive/status/item icons, an optional cosmetic-options panel lets you:

- Add backgrounds to the 64×64 hotbar tile
- Apply a soft radial fade to the 380×380 tooltip image

A live preview shows what each icon will look like at hotbar, controller, and tooltip sizes as you adjust the controls.

---

## Install

1. Go to the [Releases page](../../releases) and download the latest `BG3.Mod.Merger.exe`.
2. **Install [.NET 8 Desktop Runtime](https://dotnet.microsoft.com/en-us/download/dotnet/8.0/runtime)** if you don't already have it. Pick "Windows x64 Desktop Runtime 8.x.x" from the Microsoft page. This is required - the merger bundles LSLib (the tool that reads and writes BG3's binary file formats), and LSLib needs .NET 8 to run.
3. Run the .exe.

### First-run check

Open Settings (the first page of the wizard) and click **Test bundled LSLib (.NET 8 required)**. You should see "OK: divine.exe responded to a probe." If you see ".NET 8 isn't installed", click the link in the dialog and install .NET 8, then test again.

### Windows Defender / SmartScreen

On first run, Windows may show a "Windows protected your PC" SmartScreen prompt. Click **More info → Run anyway** to launch the app. This is normal for unsigned indie tools.

---

