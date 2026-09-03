"""Decide which release artifacts a commit has to rebuild.

A release names its artifacts by digest, so a manifest may point at an image or
binary a previous release built. Each artifact is rebuilt when one of its inputs
changed since the previous release. Everything else is reused from the previous
manifest, and the plan says which, so the workflow's summary reads as a list of
reasons rather than a list of skipped steps.

An artifact's inputs are read from the workspace, not written down here. The
agent binary bundles the transitive closure of `agent-app`'s workspace
dependencies and the worker image ships `container-worker-app`'s plus the
managed runtime packages, so the paths that matter are whatever the pyproject
files say today; a list kept by hand would be wrong the first time a dependency
was added.

Node images have their own host-runtime workflow and catalog. They are not
release artifacts and never enter this plan.

No previous manifest means both application artifacts rebuild. A first release,
and a release whose baseline cannot be read, are both built rather than guessed
at.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from provider_clients.release_manifest import AwsReleaseManifest

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKER_APP = "container-worker-app"
AGENT_APP = "agent-app"
MANAGED_RUNTIME_DISTRIBUTIONS = ("lazycloud-shared", "foundation", "lazycloud-client", "runner")
WORKER_FIXED_INPUTS = (
    "docker/Dockerfile.worker",
    "deploy/ami/gvisor-version",
    "deploy/managed-runtime/",
    "uv.lock",
)
AGENT_FIXED_INPUTS = ("deploy/agent-binary/", "uv.lock")


@dataclass(frozen=True, slots=True)
class ArtifactInputs:
    worker: tuple[str, ...]
    agent: tuple[str, ...]


def workspace_members(root: Path = REPO_ROOT) -> dict[str, tuple[str, list[str]]]:
    """Every workspace member: name -> (directory, workspace dependency names)."""
    manifests = [
        *root.glob("packages/*/pyproject.toml"),
        *root.glob("packages/providers/*/pyproject.toml"),
        *root.glob("apps/*/pyproject.toml"),
    ]
    raw: dict[str, tuple[str, list[str]]] = {}
    for manifest in manifests:
        project = tomllib.loads(manifest.read_text(encoding="utf-8")).get("project", {})
        name = str(project.get("name", "")).strip()
        if not name:
            continue
        directory = manifest.parent.relative_to(root).as_posix() + "/"
        dependencies = [
            _requirement_name(item)
            for item in project.get("dependencies", [])
            if isinstance(item, str)
        ]
        raw[name] = (directory, dependencies)
    return {
        name: (directory, [dep for dep in dependencies if dep in raw])
        for name, (directory, dependencies) in raw.items()
    }


def _requirement_name(requirement: str) -> str:
    name = requirement.strip()
    for separator in ("[", ">", "<", "=", "!", "~", ";", " "):
        name = name.split(separator, 1)[0]
    return name.strip().lower()


def closure(names: Iterable[str], members: dict[str, tuple[str, list[str]]]) -> tuple[str, ...]:
    """Directories of the named members and everything they depend on, sorted."""
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if name not in members:
            raise ValueError(f"{name!r} is not a workspace member")
        seen.add(name)
        pending.extend(members[name][1])
    return tuple(sorted(members[name][0] for name in seen))


def artifact_inputs(root: Path = REPO_ROOT) -> ArtifactInputs:
    members = workspace_members(root)
    worker = closure((WORKER_APP, *MANAGED_RUNTIME_DISTRIBUTIONS), members)
    agent = closure((AGENT_APP,), members)
    return ArtifactInputs(
        worker=tuple(sorted({*worker, *WORKER_FIXED_INPUTS})),
        agent=tuple(sorted({*agent, *AGENT_FIXED_INPUTS})),
    )


@dataclass(frozen=True, slots=True)
class PreviousRelease:
    version: str
    worker_image: str
    agent_sha256: str
    agent_url: str
    agent_size_bytes: int

    @classmethod
    def from_manifest(cls, manifest: AwsReleaseManifest) -> PreviousRelease:
        agent = manifest.agent_artifact_object
        return cls(
            version=manifest.release_version,
            worker_image=manifest.container_worker_image,
            agent_sha256=manifest.agent_artifact_sha256,
            agent_url=agent.public_url,
            agent_size_bytes=agent.size_bytes,
        )


@dataclass(frozen=True, slots=True)
class ReleasePlan:
    rebuild_worker: bool
    rebuild_agent: bool
    reasons: dict[str, str]
    previous: PreviousRelease | None = None
    changed: tuple[str, ...] = field(default_factory=tuple)

    def as_outputs(self) -> dict[str, str]:
        previous = self.previous
        return {
            "rebuild_worker": str(self.rebuild_worker).lower(),
            "rebuild_agent": str(self.rebuild_agent).lower(),
            "previous_version": previous.version if previous else "",
            "previous_worker_image": previous.worker_image if previous else "",
            "previous_agent_sha256": previous.agent_sha256 if previous else "",
            "previous_agent_url": previous.agent_url if previous else "",
            "previous_agent_size_bytes": str(previous.agent_size_bytes) if previous else "",
            "reused_from": json.dumps(
                {
                    name: previous.version
                    for name, rebuild in (
                        ("container_worker_image", self.rebuild_worker),
                        ("agent_artifact", self.rebuild_agent),
                    )
                    if previous and not rebuild
                },
                sort_keys=True,
            ),
        }

    def summary(self) -> str:
        lines = ["## Release plan", ""]
        baseline = self.previous.version if self.previous else "none"
        lines.append(f"Baseline: previous release `{baseline}`.")
        lines.append("")
        for name in ("worker", "agent"):
            action = "rebuild" if getattr(self, f"rebuild_{name}") else "reuse"
            lines.append(f"- **{name}**: {action}. {self.reasons[name]}")
        return "\n".join(lines) + "\n"


def _shipped(path: str) -> bool:
    """Whether a path is part of what an artifact carries.

    Tests and prose live beside the code but are not built into anything, so a
    change to them cannot change an artifact.
    """
    parts = path.split("/")
    if "tests" in parts[:-1]:
        return False
    return not path.endswith((".md", ".rst"))


def _touched(changed: Iterable[str], inputs: Sequence[str]) -> list[str]:
    hits: list[str] = []
    for path in changed:
        if not _shipped(path):
            continue
        for prefix in inputs:
            if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
                hits.append(path)
                break
    return hits


def _explain(hits: list[str]) -> str:
    shown = ", ".join(f"`{path}`" for path in hits[:3])
    more = f" and {len(hits) - 3} more" if len(hits) > 3 else ""
    return f"Inputs changed: {shown}{more}."


def plan_release(
    changed: Sequence[str],
    previous: PreviousRelease | None,
    inputs: ArtifactInputs,
) -> ReleasePlan:
    if previous is None:
        reason = "No previous release to reuse from; both application artifacts are built."
        return ReleasePlan(
            rebuild_worker=True,
            rebuild_agent=True,
            reasons={"worker": reason, "agent": reason},
            changed=tuple(changed),
        )
    worker_hits = _touched(changed, inputs.worker)
    agent_hits = _touched(changed, inputs.agent)
    rebuild_worker = bool(worker_hits)
    rebuild_agent = bool(agent_hits)
    reasons: dict[str, str] = {}
    reasons["worker"] = (
        _explain(worker_hits)
        if rebuild_worker
        else f"No worker input changed since {previous.version}; reusing its image."
    )
    reasons["agent"] = (
        _explain(agent_hits)
        if rebuild_agent
        else f"No agent input changed since {previous.version}; reusing its binary."
    )
    return ReleasePlan(
        rebuild_worker=rebuild_worker,
        rebuild_agent=rebuild_agent,
        reasons=reasons,
        previous=previous,
        changed=tuple(changed),
    )


def changed_paths(base: str, head: str, *, git: str = "git") -> list[str]:
    result = subprocess.run(
        [git, "diff", "--name-only", f"{base}..{head}"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-manifest", type=Path, default=None)
    parser.add_argument("--base", default="", help="Previous release tag; empty means no baseline.")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--output", type=Path, required=True, help="Where the plan JSON is written."
    )
    parser.add_argument("--summary", type=Path, default=None, help="Markdown summary destination.")
    parser.add_argument(
        "--github-output", type=Path, default=None, help="Append plan outputs for a workflow step."
    )
    args = parser.parse_args(argv)

    previous: PreviousRelease | None = None
    if args.previous_manifest is not None and args.base:
        previous = PreviousRelease.from_manifest(
            AwsReleaseManifest.model_validate_json(
                args.previous_manifest.read_text(encoding="utf-8")
            )
        )
    changed = changed_paths(args.base, args.head) if previous is not None else []
    plan = plan_release(changed, previous, artifact_inputs())

    outputs = plan.as_outputs()
    args.output.write_text(json.dumps(outputs, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.summary is not None:
        with args.summary.open("a", encoding="utf-8") as handle:
            handle.write(plan.summary())
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                if "\n" in value:
                    raise SystemExit(f"plan output {key} cannot span lines")
                handle.write(f"{key}={value}\n")
    sys.stdout.write(plan.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
