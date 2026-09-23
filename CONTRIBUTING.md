# Contributing to openmotion-bloodflow-app

Thank you for contributing to the **openmotion-bloodflow-app** project maintained by OpenwaterHealth.

This document outlines the development workflow, branching strategy, communication expectations, and pull request process.

---

# 📌 Project Workflow Overview

* **Base branch for development:** `next`
* **Issues:** Use GitHub Issues for all task tracking and communication
* **Pull Requests target:** `next` (not `main`)

All work must be traceable to an existing GitHub Issue.

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

3. PR Title Format:

```
[Issue #123] Add export validation
```

4. PR Description Must Include:

   * Link to the issue
   * Summary of changes
   * Testing performed
   * Screenshots (if UI-related)

To auto-close an issue:

```
Closes #123
```

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
