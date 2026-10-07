# HIL test suite — branch workflow

The hardware-in-the-loop pytest suite is developed on the **`test/hil-suite`**
branch (it supersedes the former `Varun-Test` and `test/test-fixes` branches,
whose history is fully merged here and into `next`).

## Rules for `test/hil-suite`

1. **Standalone by default.** Test-script work is pushed directly to this
   branch — no PRs or reviews required, and it carries no obligation to stay
   synced with `next` while a test campaign is running.
2. **Never targets `main` directly.** Changes reach production only through
   `test/hil-suite` → `next` → `main`.
3. **Sync before shipping.** When scripts are ready for the team, the latest
   `next` is merged *into* this branch first (plain merge — never a
   force-push), so the hand-off is conflict-free.
4. **Enter `next` by pull request.** `next` itself does not require PRs, but
   test changes arrive there via a PR anyway, so the team has a visible
   record of what test code landed and why.

## Sync modes

- **Sync** — merge the latest `next` into this branch to pick up app changes
  the tests must track.
- **Freeze** — during a verification campaign against a frozen build (e.g. a
  SWVER sweep), the branch is deliberately *not* synced, so the test baseline
  does not move under the campaign. Sync resumes when the campaign closes.

## History never rewrites

No force-pushes to this branch, ever. Rebases happen only locally with a
backup ref taken first; what is on GitHub only grows.
