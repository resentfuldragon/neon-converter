# Putting Neon Converter on GitHub

Once this is done, anyone can read the code, **Fork** it, **Download ZIP** of the source, or download the
ready-to-run app from **Releases**.

## 1. One-time setup

1. Create a free account at <https://github.com> (or sign in).
2. Tell Git who you are (use the name/email you want shown on your commits):

   ```powershell
   git config --global user.name "Your Name"
   git config --global user.email "you@example.com"
   ```

## 2. Create the repository on GitHub

1. Go to <https://github.com/new>.
2. Repository name: `neon-converter` · Visibility: **Public**.
3. Leave **Add a README**, **.gitignore** and **license** all *unticked* (the project already has them,
   and an empty repo makes the first push simple).
4. Click **Create repository** and keep the page open — it shows your repo URL,
   e.g. `https://github.com/YOUR-NAME/neon-converter.git`.

## 3. Upload the code

Open PowerShell **in the project folder** (the one containing `main.py`) and run:

```powershell
git init
git add .
git commit -m "Neon Converter v1.1.0"
git branch -M main
git remote add origin https://github.com/YOUR-NAME/neon-converter.git
git push -u origin main
```

The first push opens a browser window to sign in to GitHub. `.venv`, `build` and `dist` are ignored, so
only the source (about 100 KB) is uploaded.

## 4. Add the license file

The app uses PyMuPDF, which is licensed AGPL-3.0, so the project should be too:

1. On your repo page click **Add file → Create new file**.
2. Type `LICENSE` as the file name; a **Choose a license template** button appears — click it.
3. Pick **GNU Affero General Public License v3.0** → **Review and submit** → **Commit changes**.
4. Back in PowerShell, pull it down so your copy matches: `git pull`

## 5. Publish a downloadable app (Release)

**From your PC (recommended — you test the exact file you ship):** build, then create the release with the
GitHub CLI (`winget install GitHub.cli`, then `gh auth login` once):

```powershell
.\build.ps1 -Zip
gh release create v1.1.0 dist\NeonConverter-windows.zip --title "Neon Converter v1.1.0" --notes "What's new…"
```

Or on GitHub: **Releases → Draft a new release**, create tag `v1.1.0`, drag in
`dist\NeonConverter-windows.zip`, and click **Publish release**. (Release files can be up to 2 GB, so the
≈ 575 MB zip is fine. Don't commit it to the repo itself — the 100 MB file limit applies there.)

**In the cloud:** the repo's **Actions** tab has a **Build Windows app** workflow. Click **Run workflow** to
build on GitHub's servers (~15 minutes; it downloads Calibre to bundle). Leave the tag empty to just get a
downloadable artifact, or enter an existing release tag to attach the zip to that release.

Because the release bundles Calibre, FFmpeg and Pandoc (GPL), keep `THIRD_PARTY_NOTICES.md` in the repo:
it tells people where to get each one's source code, which those licenses require when you share binaries.

## 6. What other people can do

- **Download the app:** Releases → `NeonConverter-windows.zip` → unzip → run `NeonConverter.exe`.
- **Download the code:** green **Code** button → **Download ZIP**.
- **Fork it:** **Fork** button (top right) makes their own copy to change; they can send improvements back
  with a Pull Request.

## Updating later

```powershell
git add .
git commit -m "Describe what changed"
git push
.\build.ps1 -Zip; gh release create v1.1.1 dist\NeonConverter-windows.zip   # optional: new download
```

Remember to bump `__version__` in `neonconvert/__init__.py` when you tag a new version.
