# vendor/lslib/

This directory is where LSLib (Norbyte's BG3 modding library, which
ships divine.exe) lives in the build tree. The contents are NOT
committed to git: they're downloaded from Norbyte's GitHub release
on every CI build, and developers building the app locally need to
unpack the same release here.

## What to put here (for local builds)

Download Norbyte's release zip from
https://github.com/Norbyte/lslib/releases (tag v1.19.5 is what CI
ships; newer 1.19.x should also work) and extract its contents so
that `vendor/lslib/divine.exe` ends up directly in this folder.

You should see, at a minimum:

```
vendor/lslib/
├── divine.exe
├── LSLib.dll
├── ConverterApp.exe       (unused but doesn't hurt)
└── (various .dll dependencies)
```

If `divine.exe` isn't at `vendor/lslib/divine.exe` exactly, the
PyInstaller spec won't bundle it and the resulting build will fall
back to the legacy "user must configure divine.exe in Settings"
behavior.

## Why it isn't committed

LSLib is ~5MB of binary files that change with every Norbyte release.
Bloating git history with them isn't worth it when a CI download is
trivial. The license (MIT) permits redistribution; we just don't
need every fork to carry a binary copy.

## Runtime requirement: .NET 8 Desktop Runtime

LSLib v1.19+ is a .NET 8 app. End users need the .NET 8 Desktop
Runtime installed: https://dotnet.microsoft.com/download/dotnet/8.0/runtime

The app detects when this is missing and surfaces a clear error
with the download link, so most users won't need to know this.
