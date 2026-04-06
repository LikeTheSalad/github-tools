# AGENTS.md

This file is intended for AI agents working in this repository. It explains the architecture,
conventions, and how to safely extend or modify components.

**Keeping this file current:** any architectural change — new workflow, new action, new convention,
modified inputs, changed branching model — must be followed by an update to this file before the
task is considered complete. Letting `AGENTS.md` go stale defeats its purpose.

For consumer-facing documentation (inputs, usage examples, repository setup), see
[README.md](README.md).

---

## What this repo is

A library of reusable GitHub Actions components for Gradle/Android personal projects. Consuming
repos call components here directly at `@main` — there are no releases or version tags on this repo
itself.

**Consequence:** any change merged to `main` immediately affects all consuming repos.

---

## Repository layout

```
.github/
├── actions/
│   ├── generate-changelog/      # Composite: inserts a CHANGELOG section from merged PRs
│   ├── publish-gradle-portal/   # Composite: publishes to Gradle Plugin Portal
│   ├── publish-maven-central/   # Composite: publishes to Maven Central
│   ├── setup-bot-git-user/      # Composite: creates GitHub App token + configures git identity
│   └── setup-java/              # Composite: installs Temurin JDK
└── workflows/
    ├── pr-check.yml             # Reusable: runs wrapper sync check + verification
    ├── release-prepare.yml      # Reusable: calculates version, creates branches, opens review PR
    ├── release-publish.yml      # Reusable: publishes artifacts, tags, creates GH release
    └── sync-wrappers.yml        # Reusable: validates wrapper drift and opens auto-fix PRs
```

---

## Scripts

Two Python scripts live in `scripts/`. Use them whenever a task involves
consumer repo setup or validation.

**Prerequisite:** PyYAML must be installed — run `pip install pyyaml` if needed.

| Script | When to use |
|--------|-------------|
| `python3 scripts/validate-consumer.py <path>` | Check that a consumer repo's wrapper files are correct and up to date with this repo's current inputs |
| `python3 scripts/setup-consumer.py <path>` | Create or update a consumer repo's wrapper files, `renovate.json`, and `CHANGELOG.md` marker |

**Typical workflow** when asked to set up or fix a consumer repo:
1. Run `validate-consumer.py` first to see what (if anything) is wrong.
2. Run `setup-consumer.py` to apply fixes.
3. Run `validate-consumer.py` again to confirm everything passes.

Both scripts derive the expected input schema directly from the reusable workflow
files in `.github/workflows/`, so they stay accurate automatically as inputs
change — no separate schema file to maintain.

See the "Setting up a new consumer repo" and "Validating consumer repos" sections
below for full details on what each script does and checks.

---

## How each component works

### `pr-check.yml`

Three internal jobs: `sync-check`, `build` (matrix: ubuntu + optionally windows), and
`instrumentation` (Android emulator, only when `run-instrumentation-tests: true`). The workflow
now also has a required `app-id` input, passed through to `sync-check`, which calls
`sync-wrappers.yml`. Both verification jobs upload build reports as artifacts on failure.

When a consuming repo calls this workflow, the calling job (conventionally named `checks`) passes
only if all internal jobs pass — this is how GitHub handles reusable workflow results. Branch
protection in consuming repos registers `checks`, not the internal job names.

To add a new job to `pr-check.yml`: just add it. The consuming repo's `checks` job will
automatically fail if the new job fails — no changes needed in consuming repos.

### `sync-wrappers.yml`

Single job `sync`: validates the consumer repo against the current reusable workflow schema using
`scripts/validate-consumer.py`. If wrapper files have drifted, it runs `scripts/setup-consumer.py`,
pushes the generated changes to the bot-managed branch `github-tools/auto-update`, opens or reuses
a PR back to `main`, then fails the job so the caller's status check stays red until the wrapper
update is merged.

### `release-prepare.yml`

Two internal jobs:

- **`check`**: guards against running on a noisy repo (skips if open PRs exist on scheduled runs),
  finds the latest `v{semver}` tag, queries merged PRs since that tag (excluding `pre-release/*`
  and `release/*` branches), and computes the next version via PR labels (`breaking` → major,
  `enhancement` → minor, else patch). Manual `version-override` wins over labels.
- **`prepare`**: creates `release/{version}` from `main`, then `pre-release/{version}` from that;
  updates `gradle.properties` (`version=X.Y.Z`), runs `generate-changelog`, optionally patches
  `README.md` via `sed`, commits, pushes, and opens a `pre-release/{version}` → `release/{version}`
  PR.

### `release-publish.yml`

Single job, guarded to only run when a `pre-release/*` PR is merged into `release/**`. Reads
the version from `gradle.properties` (already committed during prepare), publishes to the configured
destinations, creates a `v{version}` tag, creates a GitHub Release (body from the top `CHANGELOG.md`
section), and opens `release/{version}` → `main` with `gh pr merge --auto --merge`.

### `generate-changelog` action

Finds the latest semver tag, fetches merged PRs since that tag date (excluding automation branches),
categorises by label (`bug` → Bug Fixes, `enhancement` → Features, unlabelled → listed first), and
inserts a `## Version X.Y.Z (YYYY-MM-DD)` block immediately after the `<!-- CHANGELOG_INSERT -->`
marker in the changelog.

### `setup-bot-git-user` action

Creates a GitHub App installation token via `actions/create-github-app-token`, resolves the bot's
numeric user ID, and configures global git author identity. Outputs `token` for callers to use in
subsequent `actions/checkout` and `GH_TOKEN` env vars.

---

## Conventions — follow these when making changes

### Composite actions: explicit shell on every `run` step

Every `run` step inside a composite action (`using: composite`) **must** declare `shell:`. GitHub
requires it; omitting it causes a runtime error.

### Version is always read from `gradle.properties`

The line `version=X.Y.Z` in `gradle.properties` is the single source of truth. Both
`release-prepare.yml` and `release-publish.yml` read and write exactly this format. Do not add
alternative version sources.

### `{version}` placeholder in `readme-version-sed`

The `readme-version-sed` input accepts a `sed` expression where `{version}` is a literal
placeholder that the workflow replaces with the actual version string at runtime (via shell
parameter expansion). Example:
```
's/version "[0-9]\+\.[0-9]\+\.[0-9]\+"/version "{version}"/g'
```

### `<!-- CHANGELOG_INSERT -->` marker

The `generate-changelog` action inserts above this marker. Consuming repos must have it in their
`CHANGELOG.md`. Do not remove or rename it.

### Bot token, not `GITHUB_TOKEN`

PRs and commits in release workflows use the GitHub App token from `setup-bot-git-user`. This is
intentional: `GITHUB_TOKEN`-created PRs do not trigger other workflows; the App token does.

### Final release PR uses regular merge

`release-publish.yml` opens `release/{version}` → `main` and merges it with `--merge` (not squash).
The release tag points at a commit on `release/{version}`; squash would create a divergent commit
on `main` and leave the tag outside `main`'s ancestry.

### Tag format

Tags are always `v{major}.{minor}.{patch}`. The version scripts grep for
`^v[0-9]+\.[0-9]+\.[0-9]+$`.

---

## Setting up a new consumer repo

`scripts/setup-consumer.py` creates or updates a consumer repo's github-tools integration.

```
python3 scripts/setup-consumer.py <path-to-consumer-repo>
```

What it automates:
- Thin-wrapper workflow files for every reusable workflow in this repo (`.github/workflows/`)
- `.github/renovate.json` with auto-merge settings (merges into existing config, doesn't replace)
- `CHANGELOG.md` `<!-- CHANGELOG_INSERT -->` marker (if the file exists)

**Idempotent and maintenance-aware:** safe to re-run on already-configured repos. When workflow
inputs change in this repo, re-running the script on a consumer:
- Adds newly-required inputs (with conventional value or `<TODO: set …>` placeholder)
- Removes inputs deleted from the schema
- Preserves all existing user-configured input values unchanged
- Normalizes file structure to the current template on first run; subsequent runs are no-ops

After the script runs, run `validate-consumer.py` to confirm the result.

The script prints a "Manual steps required" notice at the end covering things it cannot automate
(GitHub App setup, secrets, branch protection — see README.md for details).

**If the wrapper trigger conventions change** (e.g. the `on:` block in `WRAPPER_TEMPLATES` or the
job name for a workflow), update the `WRAPPER_TEMPLATES` dict in `setup-consumer.py` and this file.

---

## Validating consumer repos

`scripts/validate-consumer.py` checks that a consumer repo's thin-wrapper workflows are correct.
It derives the expected input schema directly from the reusable workflow files in this repo, so it
stays accurate automatically as inputs change.

```
python3 scripts/validate-consumer.py <path-to-consumer-repo>
```

What it checks:
- `uses:` path references an existing workflow in this repo and is pinned to `@main`
- All required inputs (those marked `required: true` with no `default`) are present
- No inputs are passed that aren't defined in the reusable workflow
- Boolean/number inputs have the right YAML type (unquoted), unless the value is a `${{ }}` expression
- `secrets: inherit` is present on every github-tools job

It warns (without failing) if a `pr-check` caller job is not named `checks`, since branch
protection in consuming repos is registered against that name.

**Run this after any input change** (add, rename, remove, change required/optional status) to
confirm known consumers are still valid, and to guide the updates they need.

---

## How to add a new composite action

1. Create `.github/actions/<name>/action.yml`.
2. Add `using: composite` under `runs:`.
3. Declare `shell:` on every `run` step.
4. Update `README.md` — the composite actions table under "Composite actions".

## How to add a new reusable workflow

1. Create `.github/workflows/<name>.yml` with `on: workflow_call:`.
2. Document inputs/outputs.
3. Update `scripts/setup-consumer.py` — add entries in `WRAPPER_TEMPLATES` and
   `CONVENTIONAL_INPUTS` if consumers should get a generated thin wrapper.
4. Update `README.md` — add a section under "Reusable workflows".

## How to modify an existing workflow or action

- **Adding an input with a default:** safe; consuming repos that don't pass it get the default.
- **Adding a required input (no default):** breaking change — all consuming repos must be updated
  before or at the same time as this repo, since they pin to `@main`.
- **Renaming or removing an input:** breaking change — same constraint.
- **Adding a job to `pr-check.yml`:** safe; the consuming repo's `checks` caller job automatically
  reflects the failure without any changes in consuming repos.
- **Adding a reusable workflow:** if consumers should wrap it, update `WRAPPER_TEMPLATES` and
  `CONVENTIONAL_INPUTS` in `setup-consumer.py` before running the consumer setup script.

After any input change, run `validate-consumer.py` against each known consuming repo.

---

## Known consuming repos

| Repo | Branch protection check | Notable config |
|------|------------------------|----------------|
| `LikeTheSalad/aaper` | `checks` | Instrumentation tests, Maven Central + Gradle Portal publish |
| `LikeTheSalad/asmifier` | `checks` | Windows runs, Gradle Portal publish |
| `LikeTheSalad/android-stem` | `checks` | Java 17, Windows runs, Maven Central publish |

When making breaking changes, check these repos and update their thin wrapper files accordingly.
