# WinGet manifest for Beacon Desktop

This directory holds the WinGet (Windows Package Manager) manifest sources for
**Beacon Desktop**. They are submitted as PRs to
[microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs) so end users
can install Beacon with:

```powershell
winget install BeaconAI.BeaconDesktop
```

## File layout

WinGet's "multi-file manifest" format requires three YAMLs per version. Files are
named with the `PackageIdentifier` (`BeaconAI.BeaconDesktop`) plus the file kind:

```
packaging/winget/
  BeaconAI.BeaconDesktop.installer.yaml   # binary URLs + SHA256 + install rules
  BeaconAI.BeaconDesktop.locale.en-US.yaml # display strings (description, license, etc.)
  BeaconAI.BeaconDesktop.yaml              # version + ManifestType: version (entry point)
```

When submitting to `microsoft/winget-pkgs`, these three files are placed under:

```
manifests/b/BeaconAI/BeaconDesktop/<version>/
```

## Per-release update flow

1. After cutting a `desktop-v*` tag and the GitHub Actions workflow uploads the
   `.msi` artifact, grab the **SHA-256** from the `.msi.sha256` sidecar on the
   Release page.
2. Bump `PackageVersion` (in all three files) and `InstallerSha256` (in
   `*.installer.yaml`).
3. Update `InstallerUrl` to point at the new release URL.
4. Validate locally on a Windows machine with WinGet installed:

   ```powershell
   winget validate --manifest .\packaging\winget
   ```

5. Fork `microsoft/winget-pkgs`, copy these three files into
   `manifests/b/BeaconAI/BeaconDesktop/<new-version>/`, and open a PR.

## Notes

- **PackageIdentifier** (`BeaconAI.BeaconDesktop`) must remain stable across
  versions. The "Publisher.PackageName" format is enforced by WinGet.
- **InstallerType** is `wix` because Tauri emits MSI installers via WiX
  (`tauri.conf.json`'s default for Windows). If we ever switch to NSIS (`*.exe`)
  the `InstallerType` becomes `nullsoft` and the per-installer rules change.
- Until the package has built a positive reputation in the WinGet index, users
  may still see SmartScreen warnings (separate from WinGet itself). See
  `INSTALL.md` for the user-facing workaround.

## Status (2026-06)

These manifests are **drafts** — the placeholder SHA-256s and URLs reference
a not-yet-published `0.1.0` release. Replace before submission. The first
release that actually exercises this manifest will be Beacon Desktop `0.1.0` or
later.
