#!/usr/bin/env python3
"""
validate-consumer.py — validates that a consumer repo's GitHub Actions workflows
correctly use the reusable workflows from LikeTheSalad/github-tools.

The expected input schema is derived directly from the reusable workflow files in
this repo, so no separate schema file needs to be maintained. When inputs are
added, renamed, or removed in a workflow here, this script automatically reflects
those changes on the next run.

Usage:
    python scripts/validate-consumer.py <path-to-consumer-repo>

Exit codes:
    0  All checks passed (or no github-tools workflows found)
    1  One or more validation errors found
    2  Bad arguments or missing dependencies
"""

import sys
from pathlib import Path

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


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def workflow_call_inputs(reusable_path: Path) -> dict:
    """
    Returns the workflow_call inputs block from a reusable workflow, e.g.:
        { "app-id": { "type": "string", "required": True }, ... }
    Returns {} if the file has no workflow_call inputs.

    Note: PyYAML (YAML 1.1) parses the bare key `on:` as the boolean True,
    so we check for both "on" and True when looking up that key.
    """
    data = load_yaml(reusable_path)
    # `on:` is parsed as True by PyYAML's YAML 1.1 loader
    on_block = data.get("on") or data.get(True) or {}
    if not isinstance(on_block, dict):
        return {}
    wc = on_block.get("workflow_call") or {}
    return wc.get("inputs") or {}


def github_tools_jobs(consumer_data: dict) -> list[tuple[str, dict, str]]:
    """
    Returns [(job_name, job_dict, workflow_filename), ...] for every job in the
    consumer workflow that calls a LikeTheSalad/github-tools reusable workflow.
    """
    results = []
    for job_name, job in (consumer_data.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses", "")
        if isinstance(uses, str) and uses.startswith(USES_PREFIX):
            # e.g. "LikeTheSalad/github-tools/.github/workflows/pr-check.yml@main"
            path_part = uses.split("@")[0]
            workflow_filename = Path(path_part).name   # "pr-check.yml"
            results.append((job_name, job, workflow_filename))
    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class Result:
    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, ctx: str, msg: str):
        self.errors.append(f"  {ctx}: {msg}")

    def warning(self, ctx: str, msg: str):
        self.warnings.append(f"  {ctx}: {msg}")


def validate_job(
    file_name: str,
    job_name: str,
    job: dict,
    workflow_filename: str,
    result: Result,
):
    ctx = f"{file_name} » job '{job_name}'"
    uses: str = job.get("uses", "")

    # ── Ref must be @main ────────────────────────────────────────────────────
    ref = uses.split("@")[1] if "@" in uses else ""
    if ref != REQUIRED_REF:
        result.error(ctx, f"'uses' must pin to @{REQUIRED_REF}, got @{ref!r}")

    # ── Reusable workflow must exist in this repo ────────────────────────────
    reusable_path = REUSABLE_WORKFLOWS_DIR / workflow_filename
    if not reusable_path.exists():
        result.error(
            ctx,
            f"References unknown workflow '{workflow_filename}' "
            f"— not found in .github/workflows/ of this repo",
        )
        return

    schema = workflow_call_inputs(reusable_path)
    consumer_with: dict = job.get("with") or {}

    # ── secrets: inherit ────────────────────────────────────────────────────
    if job.get("secrets") != "inherit":
        result.error(ctx, "'secrets: inherit' is required")

    # ── Required inputs must be present ─────────────────────────────────────
    for name, defn in schema.items():
        if isinstance(defn, dict) and defn.get("required") and name not in consumer_with:
            result.error(ctx, f"Missing required input '{name}'")

    # ── No unknown inputs ────────────────────────────────────────────────────
    for name in consumer_with:
        if name not in schema:
            result.error(
                ctx,
                f"Unknown input '{name}' — not defined in {workflow_filename}",
            )

    # ── Type compatibility (skips GitHub expression values) ──────────────────
    for name, value in consumer_with.items():
        defn = schema.get(name)
        if not isinstance(defn, dict):
            continue
        expected = defn.get("type")
        if not expected:
            continue
        # Can't type-check runtime expressions; skip them
        if isinstance(value, str) and "${{" in value:
            continue
        if expected == "boolean" and not isinstance(value, bool):
            result.error(
                ctx,
                f"Input '{name}' expects boolean, got {type(value).__name__} ({value!r})"
                " — use true/false (unquoted)",
            )
        elif expected == "number" and not isinstance(value, (int, float)):
            result.error(
                ctx,
                f"Input '{name}' expects number, got {type(value).__name__} ({value!r})",
            )

    # ── Convention: pr-check caller job should be named 'checks' ────────────
    if workflow_filename in ("pr-check.yml", "pr-check.yaml") and job_name != "checks":
        result.warning(
            ctx,
            f"Job is named '{job_name}'; branch-protection convention expects 'checks'",
        )


def validate_file(wf_file: Path, result: Result) -> set[str]:
    """
    Validates one consumer workflow file.
    Returns the set of reusable workflow filenames referenced by this file
    (e.g. {"pr-check.yml"}), or an empty set if this file has no github-tools jobs.
    Extra workflow files in the consumer that don't reference github-tools at all
    are simply ignored (empty set returned, no errors).
    """
    try:
        data = load_yaml(wf_file)
    except yaml.YAMLError as e:
        result.error(wf_file.name, f"YAML parse error: {e}")
        return set()

    jobs = github_tools_jobs(data)
    if not jobs:
        return set()

    referenced = set()
    for job_name, job, workflow_filename in jobs:
        validate_job(wf_file.name, job_name, job, workflow_filename, result)
        referenced.add(workflow_filename)

    return referenced


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0 if args and args[0] in ("-h", "--help") else 2)

    consumer_root = Path(args[0]).expanduser().resolve()

    if not consumer_root.is_dir():
        print(f"Error: '{consumer_root}' is not a directory", file=sys.stderr)
        sys.exit(2)

    workflows_dir = consumer_root / ".github" / "workflows"
    if not workflows_dir.is_dir():
        print(f"Error: '.github/workflows/' not found in '{consumer_root}'", file=sys.stderr)
        sys.exit(2)

    wf_files = sorted(
        list(workflows_dir.glob("*.yml")) + list(workflows_dir.glob("*.yaml"))
    )

    # Workflows this repo exposes — every consumer must reference each one.
    expected_workflows: set[str] = {p.name for p in REUSABLE_WORKFLOWS_DIR.glob("*.yml")}

    result = Result()
    # Map each reusable workflow filename to the consumer files that call it.
    reusable_to_files: dict[str, list[Path]] = {}

    for wf_file in wf_files:
        for ref in validate_file(wf_file, result):
            reusable_to_files.setdefault(ref, []).append(wf_file)

    referenced_workflows = set(reusable_to_files)
    checked = len(referenced_workflows)

    if checked == 0:
        print(f"No github-tools workflows found in '{workflows_dir}'.")
        sys.exit(0)

    # ── Each reusable workflow must be wrapped by exactly one file ────────────
    for reusable_wf, files in sorted(reusable_to_files.items()):
        if len(files) > 1:
            names = ", ".join(f.name for f in files)
            result.error(
                consumer_root.name,
                f"'{reusable_wf}' is called from multiple files ({names})"
                f" — only one wrapper per reusable workflow is allowed",
            )

    # ── Every expected workflow must be referenced somewhere ─────────────────
    for missing in sorted(expected_workflows - referenced_workflows):
        result.error(
            consumer_root.name,
            f"No workflow file references '{missing}' "
            f"— a wrapper calling it must exist in .github/workflows/",
        )

    print(f"Validating github-tools usage in: {consumer_root}")
    print(f"Checked {checked} of {len(expected_workflows)} expected workflow(s).\n")

    if result.warnings:
        for w in result.warnings:
            print(f"WARN  {w}")
        print()

    if result.errors:
        for e in result.errors:
            print(f"ERROR {e}")
        print(f"\n{len(result.errors)} error(s) found.")
        sys.exit(1)

    print("All checks passed.")


if __name__ == "__main__":
    main()
