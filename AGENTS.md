# AGENTS.md — openmotion-bloodflow-app

Procedure docs that AI agents (and humans) should follow when doing
release work in this repo. Repo structure / architecture lives in the
top-level `Projects/CLAUDE.md`; this file is **only** about
release-cutting.

## Release tagging

`.github/workflows/release-build.yml` triggers on three tag formats.
Each form determines which branch it must be cut from, what SDK source
the build uses, and whether the GitHub Release is flagged as
pre-release.

| Tag form / branch    | Cut from | SDK source                                                | GitHub pre-release |
|----------------------|----------|-----------------------------------------------------------|--------------------|
| `X.Y.Z`              | `main`   | PyPI (`pip install --upgrade openmotion-sdk`)             | No                 |
| `X.Y.Z-rc.N`         | `next`   | PyPI (`pip install --upgrade openmotion-sdk`)             | Yes                |
| `X.Y.Z-dev.N`        | `next`   | source (`git+...openmotion-sdk.git@next`)                 | Yes                |
| `refs/heads/next`    | `next`   | source (`git+...openmotion-sdk.git@next`)                 | n/a (no release)   |

`next`-branch pushes also pull SDK from `@next` source — the bloodflow
`next` branch tracks SDK API drift before any release, so it must
always pair with SDK `next`; PyPI's latest stable can be API-behind
(it lags the most recent connection-redesign / contact-quality merges).

`refs/heads/main` pushes and manual `workflow_dispatch` runs build
against the latest SDK wheel from GitHub Releases (with a
source-install of `main` as fallback). They are useful for
verification builds; they do **not** create a GitHub Release.

## Release progression

For a given version `X.Y.Z` the natural progression is:

1. `X.Y.Z-dev.N` on `next` — early iterations, validates the bloodflow
   app against in-flight SDK changes on `openmotion-sdk@next`.
2. `X.Y.Z-rc.N` on `next` — hardening pass once the matching SDK
   version is published to PyPI.
3. `X.Y.Z` on `main` — final release after `next` merges down.

## Release ordering — important

For `-rc.N` and production tags the workflow installs the SDK with
`pip install --upgrade openmotion-sdk` (unpinned), so it picks up
whatever is on PyPI at that moment. If the SDK release lags, the
bloodflow build silently picks up an older SDK.

**Order of operations for an RC or production release:**

1. Cut and publish the matching SDK release in `openmotion-sdk` (its
   `publish-pypi.yml` workflow handles PyPI upload).
2. Wait for PyPI to surface the new version
   (`pip index versions openmotion-sdk` or check
   <https://pypi.org/project/openmotion-sdk/>).
3. Then cut the bloodflow `-rc.N` or production tag.

`-dev.N` tags do not have this constraint — they pin to
`openmotion-sdk@next` directly, so PyPI is irrelevant.

## Cutting a release — exact commands

Always work from the appropriate branch and use **annotated** tags
(`-a` + `-m`) — the GitHub release-creation step expects a tag
message.

### Dev pre-release on `next`

```bash
git checkout next
git pull
git tag -a X.Y.Z-dev.N -m "X.Y.Z-dev.N"
git push origin next X.Y.Z-dev.N
```

### Release candidate on `next`

(Only after the matching SDK version is live on PyPI.)

```bash
git checkout next
git pull
git tag -a X.Y.Z-rc.N -m "X.Y.Z-rc.N"
git push origin next X.Y.Z-rc.N
```

### Production release on `main`

(Typically after `next` is merged down to `main`. Only after the
matching SDK version is live on PyPI.)

```bash
git checkout main
git pull
git tag -a X.Y.Z -m "X.Y.Z"
git push origin main X.Y.Z
```

## What the workflow does on a tag push

- Resolves SDK source per the table above.
- Stamps the tag (without leading `v`) into `version.py`'s
  `_FALLBACK_VERSION` so the frozen exe reports the right version.
- Builds via PyInstaller (`openwater.spec`).
- Zips `dist/` into `Open-Motion-<TAG>.zip` (and `Open-Motion-Research-<TAG>.zip`).
- Uploads the zip both as a workflow artifact and as a GitHub Release
  asset (the release is created automatically; pre-release flag is
  derived from the tag form).
- Generates release notes from the git log since the previous tag.

There is no built-in skip-CI for release tags. Don't push a tag unless
you intend the artifact and GitHub Release to be created.

## Sanity-checking the workflow without pushing

The SDK-source gate is plain bash. Simulate any tag locally:

```bash
GITHUB_REF=refs/tags/1.0.4-rc.0 bash -c '
  if [[ "$GITHUB_REF" == refs/tags/*-dev.* ]]; then
    echo "→ source @next"
  elif [[ "$GITHUB_REF" =~ ^refs/tags/[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
    echo "→ PyPI"
  else
    echo "→ GitHub-release wheel (with source fallback)"
  fi'
```

For a fuller dry-run see `act` (Linux runner only — won't fully
simulate the `windows-latest` PyInstaller step).
