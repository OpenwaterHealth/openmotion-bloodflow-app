# Contributing to openmotion-bloodflow-app

Thank you for contributing to the **openmotion-bloodflow-app** project maintained by OpenwaterHealth.

This document outlines the development workflow, branching strategy, communication expectations, and pull request process.

---

# 📌 Project Workflow Overview

* **Base branch for development:** `next`
* **Issues:** Use GitHub Issues for all task tracking and communication
* **Pull Requests target:** `next` (not `main`)

All work must be traceable to an existing GitHub Issue.

## How a change flows

```
GitHub Issue
  → feature/<issue-number>-short-description   (branched from the latest next)
  → Pull Request into next                     (reviewed, then merged)
  → next → main in a release PR                (maintainers, at release time)
  → version tag on main
```

* `next` is the integration branch: every change lands there first, through a pull request. Never open a pull request against `main`. It only receives `next` when a release is cut.
* Branch from the latest `next`: one issue per branch, named `feature/<issue-number>-short-description`.
* Pre-release builds for testing (`X.Y.Z-dev.N`, `X.Y.Z-rc.N`) are tagged from `next`. A full release (`X.Y.Z`) is tagged on `main` after `next` has been merged into it.
* Pull requests are merged with a merge commit, not squashed, so your commits land on `next` exactly as you wrote them. Keep them small, focused and well described.
* Every commit must be signed off (`git commit -s`); see [Sign Off Every Commit](#sign-off-every-commit).

---

# 🚀 Getting Started

## 1. Fork & Clone

Fork the repository to your GitHub account, then:

```bash
git clone https://github.com/<your-username>/openmotion-bloodflow-app.git
cd openmotion-bloodflow-app
```

Add upstream:

```bash
git remote add upstream https://github.com/OpenwaterHealth/openmotion-bloodflow-app.git
git fetch upstream
```

---

# 🌿 Branching Strategy

## Always Branch from `next`

Before starting work:

```bash
git checkout next
git fetch upstream
git merge upstream/next
git push origin next
```

## Feature Branch Naming Convention

Each branch must reference a GitHub Issue.

```
feature/<issue-number>-short-description
```

Example:

```bash
git checkout -b feature/123-add-export-validation
```

Rules:

* Use kebab-case
* Keep description concise
* One issue per branch

---

# 💬 Communication Guidelines

All communication must happen in the related GitHub Issue.

Use the issue thread for:

* Requirement clarification
* Scope changes
* Design discussions
* API changes
* Blockers
* Screenshots/logs
* Acceptance confirmation

Avoid undocumented side-channel decisions (Slack/email). If discussions occur elsewhere, summarize outcomes in the issue.

---

# 🛠 Development Guidelines

* Keep commits small and focused.
* Avoid unrelated refactoring.
* Follow existing project architecture and conventions.
* Do not introduce major architectural changes without prior issue discussion.

## Commit Message Format

```
feat: add validation for export format (#123)
fix: correct acquisition null check (#145)
refactor: simplify session manager logic (#132)
```

Prefix suggestions:

* `feat`
* `fix`
* `refactor`
* `docs`
* `test`
* `chore`

## Sign Off Every Commit

This repository enforces the [Developer Certificate of Origin](https://developercertificate.org/): every commit needs a `Signed-off-by:` line, and a pull request with an unsigned commit fails the DCO check. Commit with `-s`:

```bash
git commit -s -m "fix: correct acquisition null check (#145)"
```

To sign off commits you have already made on your branch:

```bash
git rebase --signoff next
git push --force-with-lease origin feature/<branch-name>
```

---

# 🔄 Keeping Your Branch Updated

Before opening a PR:

```bash
git checkout next
git pull upstream next
git checkout feature/<branch-name>
git merge next
```

Resolve conflicts locally before submitting your PR.

---

# 🔁 Pull Request Process

1. Push your branch:

```bash
git push origin feature/<branch-name>
```

2. Open a Pull Request:

   * **Base branch:** `next`
   * **Target repository:** OpenwaterHealth/openmotion-bloodflow-app

3. PR Title Format: the same `<type>: <summary> (#<issue>)` form as a commit message (see [Commit Message Format](#commit-message-format)):

```
feat: add export validation (#123)
```

4. PR Description Must Include:

   * Link to the issue
   * Summary of changes
   * Testing performed
   * Screenshots (if UI-related)

Reference the issue with `Refs`:

```
Refs #123
```

Don't use `Closes` or `Fixes`. An issue stays open after its pull request merges, until the change has been verified in a pre-release build, and the maintainers close it then.

---

# ✅ Definition of Done

A task is complete when:

* Code builds and runs
* No new warnings introduced
* Related issue updated with implementation notes
* PR opened against `next`
* PR review approved
* CI checks pass (if applicable)

---

# 🚨 Handling Blockers

If blocked:

1. Comment in the issue with:

   * Description of blocker
   * Logs/screenshots
   * Steps to reproduce
2. Tag the relevant maintainer.
3. Pause work until clarification.

---

# 🔐 Scope & Requirement Changes

If requirements change:

* Document changes in the issue.
* Wait for confirmation before implementation.
* Update acceptance criteria in the issue if needed.

---

# 📎 Code Quality Expectations

* Write clear, maintainable code.
* Prefer explicit logic over clever shortcuts.
* Keep features modular and reviewable.
* Ensure changes are traceable to an issue.

---

# 🙌 Thank You

We appreciate your contribution and adherence to the workflow.
Clear communication, clean branches, and disciplined PRs keep the project stable and scalable.
