# What's new — shown in the app after an upgrade (#597)

<!--
Authoring rules (read by whats_new.py; nothing above the first "## X.Y.Z"
heading is ever shown):

- Add a "## X.Y.Z" section at the TOP before tagging the rc for X.Y.Z.
  Pre-release tags share it (1.6.0-rc.1, 1.6.0-dev.2 and 1.6.0 all read
  "## 1.6.0"). A release with no section shows no modal at all.
- Operator-facing changes only, in plain language: same rules as the
  curated GitHub release notes, no engineering-mode content, no issue
  numbers, no Known Issues.
- Start a bullet with "- [research]" for a change Clinical builds don't
  have; it is dropped from Clinical and shown untagged in Research.
- Rendered as Markdown by QML (headings, bullets, **bold**, links).
-->

## 1.5.3

- **Signed installer.** The installer and the application are code-signed by Openwater, so Windows shows the publisher at install time.
- **One application file.** The app now ships as a single executable. Launch takes a few seconds longer while it unpacks.
- [research] **Verified updates.** The in-app updater only installs updates that carry a valid Openwater signature.
- **Each Windows account has its own scan history, settings and logs.** Previously only the first account on a computer could scan with a Clinical install. Scans from an earlier installed version stay in `C:\ProgramData\Openwater\data`.
- **Preferences now live with your scan data.** Plot ranges, colors and theme start from their defaults once after upgrading.
- [research] **Per-plot autoscale:** with Autoscale on, a switch in the plot's ⋯ menu lets each camera's plot fit its own trace.
- [research] **BVI low-pass filter** switch is back in Settings → Realtime Plot Display.
