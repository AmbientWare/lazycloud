#!/usr/bin/env bash
set -euo pipefail

checkout=$(git rev-parse --show-toplevel)
common=$(git rev-parse --path-format=absolute --git-common-dir)
primary=$(dirname "$common")
source=$checkout/.githooks/post-checkout
hook=$common/hooks/post-checkout

if [[ $(basename "$common") != .git ]]; then
    echo 'Local setup requires a main checkout with a .git directory.' >&2
    exit 1
fi
if [[ -n $(git config --get core.hooksPath || true) ]]; then
    echo 'A custom core.hooksPath is configured. Install .githooks/post-checkout there without replacing existing hooks.' >&2
    exit 1
fi
if [[ -e "$hook" ]] && ! cmp -s "$source" "$hook"; then
    echo 'An existing post-checkout hook differs. Preserve it and add the local environment hook explicitly.' >&2
    exit 1
fi
if [[ ! -e "$primary/.env" && ! -L "$primary/.env" ]]; then
    install -m 600 "$checkout/.env.example" "$primary/.env"
fi
install -m 755 "$source" "$hook"
python3 "$checkout/deploy/local_credentials.py" "$primary/.env"
while IFS= read -r -d '' field; do
    if [[ "$field" == 'worktree '* ]]; then
        tree=${field#worktree }
        (cd "$tree" && "$hook")
    fi
done < <(git worktree list --porcelain -z)
echo "Shared development environment: $primary/.env"
echo 'Existing .env files were preserved. New worktrees will link to the shared file.'
