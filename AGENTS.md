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
| `X.Y.Z`              | `main`   | PyPI, pinned (`openmotion-sdk==<sdk-version.txt>`)        | No                 |
| `X.Y.Z-rc.N`         | `next`   | PyPI, pinned (`openmotion-sdk==<sdk-version.txt>`)        | Yes                |
| `X.Y.Z-dev.N`        | `next`   | source (`git+...openmotion-sdk.git@next`)                 | Yes                |
| `refs/heads/next`    | `next`   | source (`git+...openmotion-sdk.git@next`)                 | n/a (no release)   |

`next`-branch pushes also pull SDK from `@next` source — the bloodflow
`next` branch tracks SDK API drift before any release, so it must
always pair with SDK `next`; PyPI's latest stable can be API-behind.

`refs/heads/main` pushes and manual `workflow_dispatch` runs (from any
branch except `next`, which uses `@next`) install the pinned SDK, like
rc and production tags. They are useful for verification builds; they
do **not** create a GitHub Release.

The pin is enforced: every build that installs it runs
`scripts/check_sdk_pin.py --enforce`, which fails the build if the
installed SDK differs from `sdk-version.txt`, and the SBOM step asserts
the same. Builds that install `@next` only report the pin.

## What a tag publishes (#573)

This repo is public, so `release-build.yml` builds **Research only**.
Clinical is built by the private
`OpenwaterHealth/openmotion-desktop-app-clinical` repo (same build action,
checked out at the tag) and delivered to a Google Shared Drive.

| Tag | Research → GitHub Release | Clinical → Google Drive |
|---|---|---|
| `X.Y.Z-dev.N` | portable zip + installer, unsigned | portable zip + installer, unsigned |
| `X.Y.Z-rc.N` | portable zip + installer, **signed** | portable zip + installer, **signed** |
| `X.Y.Z` | installer only, **signed** | nothing automatic |

After a production release, the signed Clinical installer is a **manual**
run of `clinical-release.yml` in the private repo on that tag. Never add a
Clinical build, artifact upload or release asset to a workflow in this repo.

## Release progression

For a given version `X.Y.Z` the natural progression is:

1. `X.Y.Z-dev.N` on `next` — early iterations, validates the bloodflow
   app against in-flight SDK changes on `openmotion-sdk@next`.
2. `X.Y.Z-rc.N` on `next` — hardening pass once the matching SDK
   version is published to PyPI and pinned in `sdk-version.txt`.
3. `X.Y.Z` on `main` — final release after `next` merges down.

User-facing explanation of the three: README "Release types";
contributor-facing: CONTRIBUTING "From Merge to Release". Keep them in
step with this file.

## Release ordering — important

For `-rc.N` and production tags the workflow installs exactly
`openmotion-sdk==<sdk-version.txt>` from PyPI. If that version is not on
PyPI yet, the install fails. If `sdk-version.txt` still names an older
SDK release, the pin check passes and the build ships that older SDK.

**Order of operations for an RC or production release:**

1. Cut and publish the matching SDK release in `openmotion-sdk`. Its
   `publish-pypi.yml` workflow uploads to PyPI; if it did not start
   after the SDK's GitHub Release was created, run it by hand:
   `gh workflow run publish-pypi.yml -R OpenwaterHealth/openmotion-sdk -f tag=X.Y.Z`.
2. Wait for PyPI to surface the new version
   (`pip index versions openmotion-sdk` or check
   <https://pypi.org/project/openmotion-sdk/>).
3. Bump `sdk-version.txt` on `next` to that version (a normal PR).
4. Then cut the bloodflow `-rc.N` or production tag.

`-dev.N` tags do not have this constraint — they install
`openmotion-sdk@next` directly, so PyPI is irrelevant and the pin is
only reported, not enforced.

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

(Only after `sdk-version.txt` on `next` names an SDK release that is
live on PyPI.)

```bash
git checkout next
git pull
git tag -a X.Y.Z-rc.N -m "X.Y.Z-rc.N"
git push origin next X.Y.Z-rc.N
```

### Production release on `main`

(Typically after `next` is merged down to `main`. Only after
`sdk-version.txt` names an SDK release that is live on PyPI.)

```bash
git checkout main
git pull
git tag -a X.Y.Z -m "X.Y.Z"
git push origin main X.Y.Z
```

## What the workflow does on a tag push

- Resolves SDK source per the table above and, on rc/production tags,
  fails the build if the installed SDK differs from `sdk-version.txt`.
- Stamps the tag (without leading `v`) into `version.py`'s
  `_FALLBACK_VERSION` so the frozen exe reports the right version.
- Builds the Research variant with Nuitka (`scripts/build_nuitka.ps1`);
  a manual `workflow_dispatch` with `compiler=pyinstaller` uses
  `openwater.spec` instead. The macOS job builds the DMG with PyInstaller.
- Signs on rc and production tags (see `docs/SIGNING.md`).
- Uploads the packaged `dist/research` tree as a workflow artifact, and
  attaches the Setup installer, the driver zip and (dev/rc only) the
  portable `Open-Motion-Research-<TAG>.zip` to the GitHub Release. The
  release is created automatically, with the pre-release flag derived
  from the tag form; the macOS job attaches the DMG to the same release.
- Generates release notes from the git log since the previous tag.

There is no built-in skip-CI for release tags. Don't push a tag unless
you intend the artifact and GitHub Release to be created.

## Sanity-checking the workflow without pushing

The SDK-source gate is plain bash. Simulate any tag locally:

```bash
GITHUB_REF=refs/tags/1.0.4-rc.0 bash -c '
  if [[ "$GITHUB_REF" == refs/tags/*-dev.* || "$GITHUB_REF" == refs/heads/next ]]; then
    echo "→ source @next"
  else
    echo "→ PyPI openmotion-sdk==$(tr -d "[:space:]" < sdk-version.txt)"
  fi'
```

For a fuller dry-run see `act` (Linux runner only — won't fully
simulate the `windows-latest` Nuitka build).
