"""Build the repository snapshot that issue sandboxes start from.

Run it after deploying, and again when the default branch or its dependencies
have moved far enough that agents should see the change:

    uv run python -m coding_agents.snapshot
"""

from datetime import UTC, datetime

from coding_agents.agent import AGENT_ENV, AGENT_HOME, REPO_DIR, as_agent, run_checked, started
from coding_agents.app import GITHUB_TOKEN, SNAPSHOT_KEY, STATE, repo_builder
from coding_agents.github import git_auth_env
from coding_agents.issues import RepoSnapshot
from coding_agents.settings import REPOSITORY, SETUP_COMMAND


def build_snapshot(token: str) -> RepoSnapshot:
    """Clone and set up the repository in a sandbox, then save its filesystem as an image.

    The token reaches git only through the clone's environment, so it is in no
    file of the image. /workspace is a mount and stays out of the image, so the
    clone lives in the agent's home.
    """
    with started(repo_builder) as instance:
        run_checked(
            instance,
            as_agent(
                "git",
                "clone",
                "--quiet",
                "--depth",
                "1",
                f"https://github.com/{REPOSITORY}.git",
                REPO_DIR,
            ),
            cwd=AGENT_HOME,
            env={**AGENT_ENV, **git_auth_env(token)},
            timeout_seconds=15 * 60,
        )
        run_checked(
            instance,
            as_agent("sh", "-c", SETUP_COMMAND),
            cwd=REPO_DIR,
            env=AGENT_ENV,
            timeout_seconds=30 * 60,
        )
        revision = run_checked(
            instance,
            as_agent("git", "rev-parse", "HEAD", "--abbrev-ref", "HEAD"),
            cwd=REPO_DIR,
            env=AGENT_ENV,
            timeout_seconds=30,
        )
        commit, branch = revision.split()
        image_id = instance.create_image_from_filesystem()
    return RepoSnapshot(
        image_id=image_id, commit=commit, branch=branch, created_at=datetime.now(UTC)
    )


if __name__ == "__main__":
    snapshot = build_snapshot(GITHUB_TOKEN.get())
    STATE.set(SNAPSHOT_KEY, snapshot.model_dump(mode="json"), ttl=0)
    print(f"Snapshot {snapshot.image_id}: {REPOSITORY} {snapshot.branch} at {snapshot.commit[:12]}")
