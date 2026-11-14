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

## 1.6.0

- [research] **Statistics pane.** Turn on Statistics in the plot's ⋯ menu to see live values, 5-second averages, peak-to-peak and the left − right difference beside the plots.
- [research] **Average and Aggregate views.** The plot's ⋯ menu now has View and Scale switches: one averaged plot per sensor, or mirrored camera pairs.
- [research] **Per-plot autoscale fits what you're looking at,** settles quickly at the start of a scan and stays responsive when zoomed out.
- **Smoother plots on large screens.** Traces redraw at full speed in a maximized window.
- **Window controls.** A fullscreen button in the title bar, Windows Snap (drag to an edge, Win+Arrow), and the window buttons keep working after minimize and restore.
- **Clearer contact check.** A camera that sends no data now says "No data from camera" instead of poor contact.
- [research] **"Hide for this scan"** in the contact-quality warning lets you continue a scan with a known-poor camera.
- **Richer scan notes.** The end-of-scan notes record the hardware serial numbers, scan settings and software versions.
- [research] **Auto-export CSV.** A Settings → Data Output switch exports every scan's CSV when it finishes. Every export also writes the scan's notes beside the CSV, and the quality columns record the contact-quality warning shown during the scan.
- **Scan History.** Durations read 1:00 instead of 0:60, timestamps show seconds, and selection and Delete are easier to use.
- **Data file names follow the subject** when you run several sessions with different subject names.
- [research] **Updates in one place.** One banner covers application and firmware updates, with Update all, and every update says which version it installs. This What's new window is new too.
- [research] **Liquid Glass is the default theme.** A theme you chose before is kept.
