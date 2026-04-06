# github-tools

Shared GitHub Actions reusable workflows and composite actions for Gradle/Android projects.

> **Personal use only.** This repo is built for my own projects and is not intended for general use.
> There are no releases, no stability guarantees, and no support for external consumers.

---

## Usage

Each project references components from this repo directly at `@main` — no releases required.
Consumer wrappers map only the secrets each reusable workflow needs; they do not use
`secrets: inherit`.

### Reusable workflows

Call from a consuming repo's workflow with `uses: LikeTheSalad/github-tools/.github/workflows/<name>@main`.

#### `pr-check.yml`

Runs an automatic wrapper sync check, the project's verification suite, optionally on Windows, and
optionally boots an Android emulator for instrumentation tests. The calling job (conventionally
named `checks`) serves as the branch-protection status check — register it as the single required
check.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `app-id` | string | required | GitHub App ID used by the wrapper sync check |
| `java-version` | string | `21` | JDK version |
| `checks-command` | string | `./gradlew check` | Main verification command |
| `run-on-windows` | boolean | `false` | Also run on `windows-latest` |
| `run-instrumentation-tests` | boolean | `false` | Boot an Android emulator and run tests |
| `instrumentation-test-command` | string | `./gradlew connectedDebugAndroidTest` | Instrumentation test command |
| `instrumentation-api-level` | number | `35` | Android API level for the emulator |

```yaml
# .github/workflows/pr-check.yml
name: PR Check
on:
  pull_request:
  workflow_dispatch:
jobs:
  checks:
    uses: LikeTheSalad/github-tools/.github/workflows/pr-check.yml@main
    with:
      app-id: ${{ vars.APP_ID }}
      checks-command: ./checks.sh
      run-instrumentation-tests: true
      instrumentation-test-command: ./gradlew -p demo-app connectedDebugAndroidTest
    secrets:
      GH_BOT_PRIVATE_KEY: ${{ secrets.GH_BOT_PRIVATE_KEY }}
```

---

#### `sync-wrappers.yml`

Validates that a consumer repo's generated github-tools wrapper files are in sync with this repo.
If drift is found, it runs the setup script, pushes the changes to a bot-managed branch, opens a
PR, and fails the workflow so the caller stays red until the update PR is merged.

This workflow is intended to be called both from `pr-check.yml` and from a consumer-side manual
wrapper that exposes `workflow_dispatch`.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `app-id` | string | required | GitHub App ID used to push the auto-update branch and open the PR |

```yaml
# .github/workflows/sync-wrappers.yml
name: Sync github-tools wrappers
on:
  workflow_dispatch:
jobs:
  sync:
    uses: LikeTheSalad/github-tools/.github/workflows/sync-wrappers.yml@main
    with:
      app-id: ${{ vars.APP_ID }}
    secrets:
      GH_BOT_PRIVATE_KEY: ${{ secrets.GH_BOT_PRIVATE_KEY }}
```

---

#### `release-prepare.yml`

Calculates the next version from merged PR labels (`breaking` → major, `enhancement` → minor,
neither → patch), creates `release/{version}` and `pre-release/{version}` branches, updates
`gradle.properties`, `CHANGELOG.md`, and optionally `README.md`, then opens a PR for human review.

Runs automatically on the 1st of every month (skips if any PR is open) and can be triggered
manually with an optional version override.

**Prerequisite:** `CHANGELOG.md` must contain a `<!-- CHANGELOG_INSERT -->` marker above the first
`## Version` heading.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `app-id` | string | required | GitHub App ID — pass `vars.APP_ID` |
| `java-version` | string | `21` | JDK version |
| `version-override` | string | `''` | Force bump type: `patch`, `minor`, `major`, or empty for auto |
| `readme-version-sed` | string | `''` | `sed` expression to update version in `README.md`; use `{version}` as placeholder; empty = skip |
| `changelog-file` | string | `CHANGELOG.md` | Path to the changelog |

```yaml
# .github/workflows/release-prepare.yml
name: Release — Prepare
on:
  schedule:
    - cron: '0 12 1 * *'
  workflow_dispatch:
    inputs:
      version_override:
        description: 'Force version bump type (overrides label detection)'
        type: choice
        options: ['', 'patch', 'minor', 'major']
        default: ''
jobs:
  release:
    uses: LikeTheSalad/github-tools/.github/workflows/release-prepare.yml@main
    with:
      app-id: ${{ vars.APP_ID }}
      version-override: ${{ inputs.version_override || '' }}
      readme-version-sed: 's/version "[0-9]\+\.[0-9]\+\.[0-9]\+"/version "{version}"/g'
    secrets:
      GH_BOT_PRIVATE_KEY: ${{ secrets.GH_BOT_PRIVATE_KEY }}
```

---

#### `release-publish.yml`

Triggered when a `pre-release/**` PR is merged into `release/**`. Publishes artifacts to the
configured destinations, creates a git tag and GitHub Release (body populated from `CHANGELOG.md`),
then opens `release/{version}` → `main` with auto-merge enabled.

**Prerequisite:** the repo must have **Allow auto-merge** enabled (Settings → General → Pull
Requests) so the final PR to `main` can merge automatically once CI passes.

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `app-id` | string | required | GitHub App ID — pass `vars.APP_ID` |
| `java-version` | string | `21` | JDK version |
| `publish-to-maven-central` | boolean | `false` | Publish to Maven Central |
| `publish-to-gradle-portal` | boolean | `false` | Publish to Gradle Plugin Portal |

```yaml
# .github/workflows/release-publish.yml
name: Release — Publish
on:
  pull_request:
    types: [closed]
    branches:
      - 'release/**'
jobs:
  release:
    uses: LikeTheSalad/github-tools/.github/workflows/release-publish.yml@main
    with:
      app-id: ${{ vars.APP_ID }}
      publish-to-maven-central: true
      publish-to-gradle-portal: true
    secrets:
      GH_BOT_PRIVATE_KEY: ${{ secrets.GH_BOT_PRIVATE_KEY }}
      MAVEN_CENTRAL_USERNAME: ${{ secrets.MAVEN_CENTRAL_USERNAME }}
      MAVEN_CENTRAL_PASSWORD: ${{ secrets.MAVEN_CENTRAL_PASSWORD }}
      GRADLE_PUBLISH_KEY: ${{ secrets.GRADLE_PUBLISH_KEY }}
      GRADLE_PUBLISH_SECRET: ${{ secrets.GRADLE_PUBLISH_SECRET }}
      GPG_PRIVATE_KEY: ${{ secrets.GPG_PRIVATE_KEY }}
      GPG_PASSWORD: ${{ secrets.GPG_PASSWORD }}
```

---

### Composite actions

Call from any workflow step with `uses: LikeTheSalad/github-tools/.github/actions/<name>@main`.

| Action | Inputs | Output | Description |
|--------|--------|--------|-------------|
| `setup-java` | `java-version` (default: `21`) | — | Installs Temurin JDK |
| `setup-bot-git-user` | `app-id`, `private-key` | `token` | Creates App token, configures git identity |
| `generate-changelog` | `version`, `token`, `changelog-file` (default: `CHANGELOG.md`) | — | Inserts a new version section into the changelog from merged PRs |
| `publish-maven-central` | `maven-central-username`, `maven-central-password`, `gpg-private-key`, `gpg-password` | — | Runs `./gradlew publishAndReleaseToMavenCentral -Prelease=true` |
| `publish-gradle-portal` | `gradle-publish-key`, `gradle-publish-secret`, `gpg-private-key`, `gpg-password` | — | Runs `./gradlew publishPlugins -Prelease=true` |

---

## Repository setup

Steps required when setting up these workflows in a new GitHub remote repository.

### 1. Create and install a GitHub App (bot account)

The release workflows use a GitHub App token to create branches, push commits, and open PRs. Using
an App token instead of `GITHUB_TOKEN` allows bot-created PRs to trigger `pr-check.yml`.

1. Go to **GitHub → Settings → Developer settings → GitHub Apps → New GitHub App**.
2. Fill in the required fields (name, homepage URL). Disable the webhook.
3. Set the following **repository permissions**:
    - Contents: **Read and write**
    - Pull requests: **Read and write**
    - Metadata: **Read-only** (mandatory)
4. Click **Create GitHub App**.
5. Note the **App ID** — you will need it in step 3.
6. Scroll to **Private keys** and click **Generate a private key**. Save the `.pem` file.
7. Click **Install App** and install it on the repository.

### 2. Create a GPG key for artifact signing

Maven Central and the Gradle Plugin Portal require signed artifacts.

```bash
gpg --gen-key
gpg --keyserver keyserver.ubuntu.com --send-keys <KEY_ID>
gpg --armor --export-secret-keys <KEY_ID>   # → GPG_PRIVATE_KEY secret
```

### 3. Add repository secrets and variables

Go to **Settings → Secrets and variables → Actions**.

**Secrets**

| Name | Value |
|------|-------|
| `GH_BOT_PRIVATE_KEY` | Full contents of the `.pem` file from step 1 |
| `GPG_PRIVATE_KEY` | Armored private key from step 2 |
| `GPG_PASSWORD` | Passphrase chosen when generating the GPG key |
| `MAVEN_CENTRAL_USERNAME` | Maven Central user token (if publishing there) |
| `MAVEN_CENTRAL_PASSWORD` | Maven Central token password (if publishing there) |
| `GRADLE_PUBLISH_KEY` | Gradle Plugin Portal API key (if publishing there) |
| `GRADLE_PUBLISH_SECRET` | Gradle Plugin Portal API secret (if publishing there) |

**Variables**

| Name | Value |
|------|-------|
| `APP_ID` | The App ID from step 1 |

### 4. Configure repository settings

**Allow auto-merge** — `release-publish.yml` opens the final `release/**` → `main` PR and enables
auto-merge on it. Go to **Settings → General → Pull Requests** and check **Allow auto-merge**.

**Allow squash merging** — Renovate uses squash merge for dependency updates. Check **Allow squash
merging** in the same section.

**Branch protection on `main`** — Go to **Settings → Branches**, add a rule for `main`:
- [x] Require status checks to pass before merging
    - Add **`checks`** as a required check
- [x] Require branches to be up to date before merging

**Branch protection on `release/**`** — Add a rule for `release/**`:
- [x] Require status checks to pass before merging
    - Add **`checks`** as a required check
- [x] Require branches to be up to date before merging

### 5. Prepare CHANGELOG.md

Add the `<!-- CHANGELOG_INSERT -->` marker above the first `## Version` heading:

```markdown
Change Log
==========

<!-- CHANGELOG_INSERT -->

## Version 1.0.0 (2025-01-01)
...
```

### 6. Configure Renovate (optional)

To enable automatic dependency updates with auto-merge:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:recommended"],
  "automerge": true,
  "automergeStrategy": "squash",
  "schedule": ["* * 2-31 * *"]
}
```

The `schedule` keeps Renovate from opening PRs on the 1st of the month, leaving the release
window clear for `release-prepare.yml`.
