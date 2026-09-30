import os
import signal
import subprocess
import sys
import time

print("app imported")


def handle(action, *args):
    if action == "total":
        print(f"summing {len(args[0])} values")
        print("stderr line", file=sys.stderr)
        return sum(args[0])
    if action == "sleep":
        time.sleep(args[0])
        return "slept"
    if action == "hang":
        print("hanging", flush=True)
        time.sleep(3600)
    if action == "crash":
        print("crashing", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    if action == "orphan":
        # The child outlives this call in its own session, so the supervisor
        # must reap it when it exits.
        subprocess.Popen(["sleep", "0.2"], start_new_session=True)
        return "spawned"
    raise ValueError(f"unknown action {action}")
