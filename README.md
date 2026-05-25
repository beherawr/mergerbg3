# BG3 Mod Merger

Combine two Baldur's Gate 3 Toolkit mod projects into one, with a friendly
wizard. Handles stat names, treasure tables, GUI metadata, root templates,
localization handles, and the per-file mess of identifiers BG3 mods are
made of.

## Download

Get the latest **`bg3_mod_merger-x.y.z.exe`** from the
[Releases page](../../releases/latest).

No installer. No Python. Just download and run.

## First-run warning (this is normal)

Windows SmartScreen will probably show **"Windows protected your PC"** the
first time you launch the exe. This happens because the app isn't signed
with an expensive Microsoft-issued certificate, not because it's actually
dangerous. To run it anyway:

1. Click **More info**
2. Click **Run anyway**

You only have to do this once. Some antivirus tools also occasionally
flag PyInstaller-built apps as a precaution; the source code is in this
repo if you want to inspect it.

## Setup

1. Launch the app. The first page asks for two paths:
   - **Workspace folder**: your BG3 Toolkit Data folder
     (e.g. `F:\SteamLibrary\steamapps\common\Baldurs Gate 3\Data`)
   - **divine.exe path**: from [LSLib](https://github.com/Norbyte/lslib),
     optional but needed for full GUI metadata merging
2. Hit Next. These settings persist; you won't have to set them again.

## Merging two mods

1. Pick **Mod A** on the left and **Mod B** on the right from the list
   of mods found in your workspace
2. Pick a mode:
   - **Make a new mod combining A and B** - creates a fresh mod;
     A and B stay untouched
   - **Combine B into A** - folds B's content into A; B stays untouched
3. Click through Identity → Policy → Review → Run
4. Done. The merged mod is in your workspace, ready for the Toolkit to
   pick up

## What it does

- **Identifier clashes**: detected up front. Stat-name collisions get
  prefixed (configurable) or you can skip B's version
- **GUI metadata.lsf**: structurally unioned via divine.exe so UI
  widgets from both mods coexist (when divine is configured)
- **Stats files**: parsed and merged byte-exact; `using` chains preserved
- **Treasure tables**: subtables concatenate for `CanMerge=1` tables;
  identical entries deduplicate
- **Localization handles**: collision-free; conflicts get B's reassigned
- **In-place safety**: when combining B into A, writes go to a staging
  directory first and atomic-swap in. A crash mid-merge leaves the
  original A intact

## Troubleshooting

**"The system cannot find the path specified"** — your workspace path
plus mod folder names puts file paths past Windows' 260-char limit. The
merger auto-uses `\\?\` long-path prefixes past 240 chars, so this
shouldn't happen on current builds. If it does, please open an issue
with the exe version and a sample path from the log.

**"divine.exe not found"** — the merger keeps working without divine for
text files. Only GUI metadata.lsf needs divine for a real structural
merge; without it, you get A's version with B's UI widgets dropped (the
merge log says so explicitly).

**Merge said "X identifier clashes" but I expected zero** — read the
clash list in the Review page before clicking Next. Almost always
they're legitimate collisions you want to know about.

---

