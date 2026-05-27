# Distribution checklist

How to ship a release. Followed start-to-finish, this gets your `.exe`
into people's hands without you needing to touch PyInstaller manually.

## One-time setup

### 1. Create a GitHub repository

1. Sign in to https://github.com: free account is fine
2. New repo → name it something like `bg3-mod-merger`
3. Make it **Public** (so the Actions runner minutes are free; private
   repos get 2,000 free minutes/month, which is also plenty)
4. From your local machine:

   ```bash
   cd bg3_mod_merger
   git init
   git add .
   git commit -m "Initial commit"
   git remote add origin https://github.com/YOUR_USERNAME/bg3-mod-merger.git
   git branch -M main
   git push -u origin main
   ```

That's it. The `.github/workflows/release.yml` file already in this repo
will start working on the next tag push.

### 2. (Optional) Add a Nexus Mods page

BG3 modders live on Nexus Mods. Even if your primary download host is
GitHub, having a Nexus page surfaces your tool to the right audience.

1. https://www.nexusmods.com/baldursgate3 → Submit a file → "Utility"
2. In the description, link back to your GitHub repo for the latest exe
3. Upload the same `.exe` here so users who never leave Nexus can find it

The two pages don't have to stay in sync perfectly; you can update Nexus
when a release is significant enough to warrant attention.

## Releasing a new version

```bash
# In your local repo, after committing your changes:
git tag v0.1.0
git push origin v0.1.0
```

Then wait ~3-5 minutes. GitHub Actions will:

1. Spin up a Windows runner
2. Install Python, PyInstaller, PySide6, lxml
3. Run the full test suite (build fails if tests fail)
4. Build `bg3_mod_merger-0.1.0.exe`
5. Create a GitHub Release at the tag
6. Attach the `.exe` to the release
7. Auto-generate release notes from the commits since the previous tag

When it's done, your release URL is:

```
https://github.com/YOUR_USERNAME/bg3-mod-merger/releases/latest
```

That's the link to share. The "Assets" section on the release page has
the `.exe` your users download with one click.

### Watching the build

After pushing the tag, go to the **Actions** tab on your repo. The
"Build and release Windows exe" workflow shows up; click it to see live
build logs. If something fails, the logs tell you why.

## Version numbering

Use semantic versioning: `vMAJOR.MINOR.PATCH`.

- **PATCH** (v0.1.0 → v0.1.1): bug fixes, no behavior change for users
- **MINOR** (v0.1.0 → v0.2.0): new features, no breaking change
- **MAJOR** (v0.1.0 → v1.0.0): breaking change (e.g. saved-settings
  format changed and existing settings are lost)

For your first release, use `v0.1.0`. Don't start at `v1.0.0` unless
you're prepared to commit to API stability.

## The "Windows protected your PC" problem

This will come up for every new user. The exe isn't signed with a
Microsoft-issued certificate, so SmartScreen blocks it on first run.

### What users see

> Windows protected your PC. Microsoft Defender SmartScreen prevented an
> unrecognized app from starting. Running this app might put your PC at
> risk.

### What to tell them (and the README already does)

Click **More info** → **Run anyway**.

### Long-term fix (only if it bothers you)

Real solution: code-sign the exe. Either:

- Buy an **EV (Extended Validation) code signing certificate**:
  ~$200-400/year from DigiCert, Sectigo, etc. Eliminates SmartScreen
  warnings immediately.
- Use a **standard code signing certificate**: cheaper (~$80/year)
  but SmartScreen still warns until your exe builds up enough downloads
  to gain "reputation". Takes weeks/months.
- **azuresigntool** + a Microsoft hardware token + EV cert: what most
  small software shops do.

For a free hobby project shared with the BG3 community, the README note
explaining the warning is sufficient. Don't spend money on signing
unless you're getting real complaints.

## Updating users

Users find new versions one of three ways:

1. **"Watch" your repo on GitHub**: they get an email on every release.
   Tell power users about this in the README.
2. **Re-visiting the releases page**: most people just check
   periodically.
3. **Nexus Mods notifications**: if you maintain a Nexus page, users
   who endorsed/tracked it get a notification on update.

There's no auto-update mechanism built into the app currently. Adding
one is doable (a "check for updates" menu item that hits the GitHub
Releases API) but it's not in this version.

## If you don't want GitHub Actions

You can also build locally. On a Windows machine:

```powershell
pip install pyinstaller PySide6 lxml
pyinstaller bg3_mod_merger.spec --clean --noconfirm
# Result: dist\bg3_mod_merger.exe
```

Then upload that exe to a GitHub Release manually (Releases → Draft a
new release → Attach binaries). More clicks for you, same experience
for users.
