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
    if action == "spam":
        for i in range(args[0]):
            print(f"{i:08d}" + "x" * 1015)
        return args[0]
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
        # The shell exits at once, so its background sleep is re-parented to
        # PID 1, which must reap it.
        subprocess.run(["sh", "-c", "sleep 0.2 &"], check=True)
        return "spawned"
    if action == "zombies":
        zombies = 0
        for pid in filter(str.isdigit, os.listdir("/proc")):
            try:
                with open(f"/proc/{pid}/stat") as stat:
                    zombies += stat.read().rsplit(")", 1)[1].split()[0] == "Z"
            except OSError:
                pass
        return zombies
    raise ValueError(f"unknown action {action}")
