#!/bin/sh
# Starts the agent release that current links to; the lazycloud-agent
# systemd unit runs it. An update leaves update-trial in the state directory
# naming the new version. Each start appends a line to it, and the agent
# deletes it once a session has stayed open. A release that has not done so
# after three starts is rolled back: current returns to previous and
# update-rejected names the version, which the server then stops offering.
set -eu

root=${LAZYCLOUD_AGENT_ROOT:?LAZYCLOUD_AGENT_ROOT is not set}
state=${LAZYCLOUD_AGENT_STATE_DIR:?LAZYCLOUD_AGENT_STATE_DIR is not set}
trial="$state/update-trial"
max_starts=3

if [ -f "$trial" ]; then
  starts=$(($(wc -l <"$trial") - 1))
  if [ "$starts" -ge "$max_starts" ]; then
    version=$(head -n 1 "$trial")
    if [ -L "$root/previous" ]; then
      ln -sfn "$(readlink "$root/previous")" "$root/current.rollback"
      mv -T "$root/current.rollback" "$root/current"
    fi
    printf '%s\n' "$version" >"$state/update-rejected"
    rm -f "$trial"
    echo "lazycloud-agent $version did not connect after $max_starts starts; rolled back to the previous release" >&2
  else
    echo start >>"$trial"
  fi
fi

exec "$root/current/lazycloud-agent" "$@"
