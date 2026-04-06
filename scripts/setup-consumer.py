#!/usr/bin/env python3
"""
setup-consumer.py — creates or updates a consumer repo's github-tools integration.

What it automates
  • Thin-wrapper workflow files for every reusable workflow in this repo.
  • .github/renovate.json with auto-merge settings (merged, not replaced).
  • CHANGELOG.md with <!-- CHANGELOG_INSERT --> marker (created if missing).

Idempotent: safe to run repeatedly. Running it again on an already-configured
repo is a no-op when nothing has changed.

Maintenance: when reusable-workflow inputs are added or removed in github-tools,
re-running this script updates each consumer wrapper's `with` block:
  • Newly-required inputs are added with a conventional or placeholder value.
  • Inputs removed from the schema are deleted.
  • All existing input values the user has set are preserved unchanged.
  • Everything outside the `with` block (triggers, job name, etc.) is left as-is
    in existing files; only newly-created files get the full template.

Usage:
    python scripts/setup-consumer.py <path-to-consumer-repo>

Exit codes:
    0  Completed (with or without changes)
    1  Fatal error
    2  Bad arguments or missing dependencies
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, List, Optional

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required.  pip install pyyaml", file=sys.stderr)
    sys.exit(2)

OWNER_REPO = "LikeTheSalad/github-tools"
REQUIRED_REF = "main"
USES_PREFIX = f"{OWNER_REPO}/.github/workflows/"

REPO_ROOT = Path(__file__).parent.parent.resolve()
REUSABLE_WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


# ── Per-wrapper configuration ─────────────────────────────────────────────────

# Full YAML template for each wrapper. The only dynamic part is {with_section},
# which is filled in from the reusable workflow's input schema.
# If the wrapper trigger conventions ever change, update the template here and
# also update AGENTS.md accordingly.
WRAPPER_TEMPLATES: dict[str, str] = {
    "pr-check.yml": """\
name: PR Check
on:
  pull_request:
  workflow_dispatch:
jobs:
  pr_check:
    uses: LikeTheSalad/github-tools/.github/workflows/pr-check.yml@main
{with_section}{secrets_section}
  checks:
    name: checks
    if: ${{{{ always() }}}}
    needs: pr_check
    runs-on: ubuntu-latest
    steps:
      - name: Finalize check status
        env:
          RESULT: ${{{{ needs.pr_check.result }}}}
        run: |
          test "$RESULT" = "success"
""",
    "sync-wrappers.yml": """\
name: Sync github-tools wrappers
on:
  workflow_dispatch:
jobs:
  sync:
    uses: LikeTheSalad/github-tools/.github/workflows/sync-wrappers.yml@main
{with_section}{secrets_section}
""",
    "release-prepare.yml": """\
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
{with_section}{secrets_section}
""",
    "release-publish.yml": """\
name: Release — Publish
on:
  pull_request:
    types: [closed]
    branches:
      - 'release/**'
jobs:
  release:
    uses: LikeTheSalad/github-tools/.github/workflows/release-publish.yml@main
{with_section}{secrets_section}
""",
}

# Conventional values for specific inputs — used as smart defaults when
# generating `with` blocks. For new files, these are included even for optional
# inputs (e.g. version-override wires up the workflow_dispatch input defined in
# the template above). For existing files, they are only used as placeholders
# for newly-required inputs the user has not configured yet.
CONVENTIONAL_INPUTS: dict[str, dict[str, str]] = {
    "pr-check.yml": {
        "app-id": "${{ vars.APP_ID }}",
    },
    "release-prepare.yml": {
        "app-id": "${{ vars.APP_ID }}",
        "version-override": "${{ inputs.version_override || '' }}",
    },
    "release-publish.yml": {
        "app-id": "${{ vars.APP_ID }}",
    },
    "sync-wrappers.yml": {
        "app-id": "${{ vars.APP_ID }}",
    },
}

# ── YAML helpers ──────────────────────────────────────────────────────────────

def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def workflow_call_block(reusable_path: Path) -> dict:
    """
    Returns the workflow_call block from a reusable workflow.
    Handles PyYAML's YAML 1.1 quirk where `on:` is parsed as boolean True.
    """
    data = load_yaml(reusable_path)
    on_block = data.get("on") or data.get(True) or {}
    if not isinstance(on_block, dict):
        return {}
    return on_block.get("workflow_call") or {}


def workflow_call_inputs(reusable_path: Path) -> dict:
    return workflow_call_block(reusable_path).get("inputs") or {}


def workflow_call_secrets(reusable_path: Path) -> dict:
    return workflow_call_block(reusable_path).get("secrets") or {}


def extract_existing_job(wrapper_file: Path) -> Optional[dict]:
    """
    Returns the first github-tools job in the given file, or None if absent.
    """
    try:
        data = load_yaml(wrapper_file)
    except yaml.YAMLError:
        return None
    for job in (data.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str) and job["uses"].startswith(USES_PREFIX):
            return job
    return None


def extract_existing_with(wrapper_file: Path) -> Optional[dict]:
    job = extract_existing_job(wrapper_file)
    if job is None:
        return None
    return dict(job.get("with") or {})


def extract_existing_secrets(wrapper_file: Path) -> Optional[dict]:
    job = extract_existing_job(wrapper_file)
    if job is None:
        return None
    secrets = job.get("secrets")
    return dict(secrets) if isinstance(secrets, dict) else {}


# ── `with` block computation ──────────────────────────────────────────────────

def compute_with_block(workflow_filename: str, existing_with: Optional[dict]) -> dict:
    """
    Returns the merged `with` block, applying these rules in order:

    1. Existing input values are preserved (for inputs still in the schema).
    2. Required inputs not yet present are added (conventional value → placeholder).
    3. Conventional optional inputs are added for NEW files only (existing_with
       is None), so they aren't re-injected into files the user may have
       intentionally kept minimal.
    4. Inputs no longer in the schema are dropped.
    """
    reusable_path = REUSABLE_WORKFLOWS_DIR / workflow_filename
    schema = workflow_call_inputs(reusable_path)
    conventional = CONVENTIONAL_INPUTS.get(workflow_filename, {})
    is_new = existing_with is None
    existing_with = existing_with or {}

    result = {}
    for input_name, defn in schema.items():
        if not isinstance(defn, dict):
            continue
        if input_name in existing_with:
            result[input_name] = existing_with[input_name]       # rule 1: preserve
        elif defn.get("required"):
            result[input_name] = conventional.get(             # rule 2: required
                input_name, f"<TODO: set {input_name}>"
            )
        elif input_name in conventional and is_new:
            result[input_name] = conventional[input_name]        # rule 3: new-file only

    return result


def default_secret_mapping(secret_name: str) -> str:
    return f"${{{{ secrets.{secret_name} }}}}"


def release_publish_active_optional_secrets(existing_with: Optional[dict]) -> set[str]:
    existing_with = existing_with or {}
    publish_maven = existing_with.get("publish-to-maven-central") is True
    publish_gradle = existing_with.get("publish-to-gradle-portal") is True

    active = set()
    if publish_maven:
        active.update({"MAVEN_CENTRAL_USERNAME", "MAVEN_CENTRAL_PASSWORD"})
    if publish_gradle:
        active.update({"GRADLE_PUBLISH_KEY", "GRADLE_PUBLISH_SECRET"})
    if publish_maven or publish_gradle:
        active.update({"GPG_PRIVATE_KEY", "GPG_PASSWORD"})
    return active


def compute_secrets_blocks(
    workflow_filename: str,
    existing_with: Optional[dict],
    existing_secrets: Optional[dict],
) -> tuple[dict, list[tuple[str, str]]]:
    reusable_path = REUSABLE_WORKFLOWS_DIR / workflow_filename
    schema = workflow_call_secrets(reusable_path)
    is_new = existing_secrets is None
    existing_secrets = existing_secrets or {}

    active = {}
    commented = []
    active_optional = set()
    if workflow_filename == "release-publish.yml":
        active_optional = release_publish_active_optional_secrets(existing_with)

    for secret_name, defn in schema.items():
        if not isinstance(defn, dict):
            continue
        if secret_name in existing_secrets:
            if defn.get("required") or secret_name in active_optional:
                active[secret_name] = existing_secrets[secret_name]
            else:
                commented.append((secret_name, default_secret_mapping(secret_name)))
        elif defn.get("required"):
            active[secret_name] = default_secret_mapping(secret_name)
        elif secret_name in active_optional:
            active[secret_name] = default_secret_mapping(secret_name)
        elif is_new:
            commented.append((secret_name, default_secret_mapping(secret_name)))

    return active, commented


# ── YAML value formatting ─────────────────────────────────────────────────────

def format_yaml_value(value: Any) -> str:
    """Formats a Python value as a YAML scalar for use in block mappings.

    Canonical style: booleans unquoted, numbers unquoted, all strings
    single-quoted. Single quotes inside strings are escaped as ''.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def format_with_section(with_dict: dict) -> str:
    """Returns a formatted `with:` block (4-space indent) or empty string."""
    if not with_dict:
        return ""
    lines = ["    with:\n"]
    for key, value in with_dict.items():
        lines.append(f"      {key}: {format_yaml_value(value)}\n")
    return "".join(lines)

def format_secrets_section(secrets_dict: dict, commented_secrets: list[tuple[str, str]]) -> str:
    if not secrets_dict and not commented_secrets:
        return ""
    lines = ["    secrets:\n"]
    for key, value in secrets_dict.items():
        lines.append(f"      {key}: {format_yaml_value(value)}\n")
    for key, value in commented_secrets:
        lines.append(f"      # {key}: {value}\n")
    return "".join(lines)


def generate_wrapper(
    workflow_filename: str,
    existing_with: Optional[dict],
    existing_secrets: Optional[dict],
) -> str:
    """Returns the full YAML content for a wrapper workflow file."""
    template = WRAPPER_TEMPLATES[workflow_filename]
    with_dict = compute_with_block(workflow_filename, existing_with)
    secrets_dict, commented_secrets = compute_secrets_blocks(
        workflow_filename, existing_with, existing_secrets
    )
    return template.format(
        with_section=format_with_section(with_dict),
        secrets_section=format_secrets_section(secrets_dict, commented_secrets),
    )


# ── Setup tasks ───────────────────────────────────────────────────────────────

def setup_workflow(
    workflow_filename: str, workflows_dir: Path, changes: List[str]
) -> None:
    # Canonical target is always .yml. If only a .yaml file exists (legacy),
    # read its `with` values, write the canonical .yml, then delete the .yaml.
    target = workflows_dir / workflow_filename
    legacy = workflows_dir / workflow_filename.replace(".yml", ".yaml")
    rel = target.relative_to(workflows_dir.parent.parent)

    if target.exists():
        existing_with = extract_existing_with(target)
        existing_secrets = extract_existing_secrets(target)
        migrating = False
    elif legacy.exists():
        existing_with = extract_existing_with(legacy)
        existing_secrets = extract_existing_secrets(legacy)
        migrating = True
    else:
        existing_with = None
        existing_secrets = None
        migrating = False

    new_content = generate_wrapper(workflow_filename, existing_with, existing_secrets)

    if target.exists() and target.read_text() == new_content:
        if legacy.exists():
            legacy.unlink()
            legacy_rel = legacy.relative_to(workflows_dir.parent.parent)
            msg = f"[DELETE]  {legacy_rel} — replaced by {rel}"
            print(msg)
            changes.append(msg)
        else:
            print(f"[OK]     {rel}")
        return

    target.write_text(new_content)

    if migrating:
        legacy.unlink()
        msg = f"[MIGRATE] {legacy.name} → {rel}"
    elif existing_with is not None:
        old_keys = set(existing_with)
        new_keys = set(compute_with_block(workflow_filename, existing_with))
        old_secret_keys = set(existing_secrets or {})
        new_secret_keys = set(
            compute_secrets_blocks(workflow_filename, existing_with, existing_secrets)[0]
        )
        added = sorted(new_keys - old_keys)
        removed = sorted(old_keys - new_keys)
        added_secrets = sorted(new_secret_keys - old_secret_keys)
        removed_secrets = sorted(old_secret_keys - new_secret_keys)
        if added or removed or added_secrets or removed_secrets:
            parts = []
            if added:
                parts.append(f"added input(s): {', '.join(added)}")
            if removed:
                parts.append(f"removed input(s): {', '.join(removed)}")
            if added_secrets:
                parts.append(f"added secret(s): {', '.join(added_secrets)}")
            if removed_secrets:
                parts.append(f"removed secret(s): {', '.join(removed_secrets)}")
            detail = "; ".join(parts)
        else:
            detail = "reformatted"
        msg = f"[UPDATE] {rel} — {detail}"
    else:
        msg = f"[CREATE] {rel}"

    print(msg)
    changes.append(msg)


RENOVATE_REQUIRED: dict[str, Any] = {
    "automerge": True,
    "automergeStrategy": "squash",
    "schedule": ["* * 2-31 * *"],
}

RENOVATE_BASE: dict[str, Any] = {
    "$schema": "https://docs.renovatebot.com/renovate-schema.json",
    "extends": ["config:recommended"],
}


def setup_renovate(consumer_root: Path, changes: List[str]) -> None:
    # Accept renovate config at either the conventional .github/ location or repo root.
    candidates = [
        consumer_root / ".github" / "renovate.json",
        consumer_root / "renovate.json",
    ]
    target = next((p for p in candidates if p.exists()), candidates[0])
    rel = target.relative_to(consumer_root)

    if target.exists():
        try:
            data: dict = json.loads(target.read_text())
        except json.JSONDecodeError:
            print(f"[SKIP]   {rel} — could not parse JSON; fix manually")
            return

        missing_or_wrong = {k: v for k, v in RENOVATE_REQUIRED.items() if data.get(k) != v}
        if not missing_or_wrong:
            print(f"[OK]     {rel}")
            return

        data.update(missing_or_wrong)
        target.write_text(json.dumps(data, indent=2) + "\n")
        updated_keys = ", ".join(missing_or_wrong)
        msg = f"[UPDATE] {rel} — set: {updated_keys}"
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        config = {**RENOVATE_BASE, **RENOVATE_REQUIRED}
        target.write_text(json.dumps(config, indent=2) + "\n")
        msg = f"[CREATE] {rel}"

    print(msg)
    changes.append(msg)


CHANGELOG_MARKER = "<!-- CHANGELOG_INSERT -->"


def setup_changelog(consumer_root: Path, changes: List[str]) -> None:
    target = consumer_root / "CHANGELOG.md"

    if not target.exists():
        target.write_text("Change Log\n==========\n\n" + CHANGELOG_MARKER + "\n")
        msg = "[CREATE] CHANGELOG.md"
        print(msg)
        changes.append(msg)
        return

    content = target.read_text()
    if CHANGELOG_MARKER in content:
        print("[OK]     CHANGELOG.md")
        return

    # Insert above the first `## Version` heading, or after the document title.
    version_match = re.search(r"^## Version ", content, re.MULTILINE)
    if version_match:
        pos = version_match.start()
        new_content = content[:pos] + CHANGELOG_MARKER + "\n\n" + content[pos:]
    else:
        lines = content.splitlines(keepends=True)
        insert_after = next(
            (i + 1 for i, line in enumerate(lines) if line.strip()), 0
        )
        lines.insert(insert_after, "\n" + CHANGELOG_MARKER + "\n")
        new_content = "".join(lines)

    target.write_text(new_content)
    msg = "[UPDATE] CHANGELOG.md — added <!-- CHANGELOG_INSERT --> marker"
    print(msg)
    changes.append(msg)


# ── Manual-steps notice ───────────────────────────────────────────────────────

MANUAL_STEPS = """\
╔═ Manual steps still required ══════════════════════════════════════════════╗
║                                                                             ║
║  1. GitHub App (one-time, shared across repos)                              ║
║     • GitHub → Settings → Developer settings → GitHub Apps → New App       ║
║     • Permissions: Contents (R/W), Pull requests (R/W), Metadata (R/O)     ║
║     • Install the app on this repository                                    ║
║     • App ID → repository variable  APP_ID                                 ║
║     • Generated private key (.pem) → repository secret  GH_BOT_PRIVATE_KEY ║
║                                                                             ║
║  2. GPG key for artifact signing (if publishing to Maven Central / Portal)  ║
║     gpg --gen-key                                                           ║
║     gpg --keyserver keyserver.ubuntu.com --send-keys <KEY_ID>              ║
║     gpg --armor --export-secret-keys <KEY_ID>   # → GPG_PRIVATE_KEY secret ║
║     # also set GPG_PASSWORD secret                                          ║
║                                                                             ║
║  3. Repository secrets  (Settings → Secrets and variables → Actions)        ║
║     Always:         GH_BOT_PRIVATE_KEY   APP_ID (variable, not secret)     ║
║     Maven Central:  MAVEN_CENTRAL_USERNAME   MAVEN_CENTRAL_PASSWORD        ║
║     Gradle Portal:  GRADLE_PUBLISH_KEY   GRADLE_PUBLISH_SECRET             ║
║     Signing:        GPG_PRIVATE_KEY   GPG_PASSWORD                         ║
║                                                                             ║
║  4. Repository settings  (Settings → General → Pull Requests)               ║
║     ☑  Allow auto-merge                                                    ║
║     ☑  Allow squash merging                                                ║
║                                                                             ║
║  5. Branch protection on `main`  (Settings → Branches)                     ║
║     ☑  Require status checks → add `checks` as required check              ║
║     ☑  Require branches to be up to date before merging                    ║
║                                                                             ║
║  6. Branch protection on `release/**`  (same settings as main)             ║
║                                                                             ║
║  After completing the above, run validate-consumer.py to confirm setup.     ║
╚═════════════════════════════════════════════════════════════════════════════╝"""


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if args else 2)

    consumer_root = Path(args[0]).expanduser().resolve()
    if not consumer_root.is_dir():
        print(f"Error: '{consumer_root}' is not a directory", file=sys.stderr)
        sys.exit(1)

    expected_workflows = sorted(p.name for p in REUSABLE_WORKFLOWS_DIR.glob("*.yml"))
    if not expected_workflows:
        print("Error: no reusable workflows found in this repo's .github/workflows/", file=sys.stderr)
        sys.exit(1)

    workflows_dir = consumer_root / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)

    print(f"Setting up github-tools integration in: {consumer_root}\n")

    changes: List[str] = []

    for workflow_filename in expected_workflows:
        setup_workflow(workflow_filename, workflows_dir, changes)

    setup_renovate(consumer_root, changes)
    setup_changelog(consumer_root, changes)

    print()
    if changes:
        print(f"{len(changes)} change(s) made.")
    else:
        print("Everything already up to date.")

    print()
    print(MANUAL_STEPS)


if __name__ == "__main__":
    main()
