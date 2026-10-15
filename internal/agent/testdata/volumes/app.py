import hashlib
import os


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def handle(action, path, text=None):
    if action == "write":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return os.path.getsize(path)
    if action == "read":
        with open(path) as f:
            return f.read()
    if action == "list":
        return sorted(os.listdir(path))
    if action == "env":
        return sorted(k for k in os.environ if "AWS" in k or "SECRET" in k)
    if action == "large":
        # Writes text MiB under a temporary name and renames it, as
        # downloaders do.
        partial = path + ".incomplete"
        with open(partial, "wb") as f:
            for n in range(int(text)):
                f.write(hashlib.sha256(str(n).encode()).digest() * (1 << 15))
        os.rename(partial, path)
        return digest(path)
    if action == "many":
        # Writes text files of 64 KiB into the directory path.
        os.makedirs(path, exist_ok=True)
        for n in range(int(text)):
            with open(os.path.join(path, f"f{n:05d}"), "wb") as f:
                f.write(os.urandom(1 << 16))
        return len(os.listdir(path))
    if action == "sum":
        return digest(path)
    if action == "opened":
        # Whether a read-only open reaches the host's cached copy rather
        # than the mount's file: the two are on different devices.
        with open(path, "rb") as f:
            return os.fstat(f.fileno()).st_dev != os.stat(path).st_dev
    if action == "symlink":
        os.symlink(text, path)
        return os.readlink(path)
    if action == "readlink":
        return os.readlink(path)
    raise ValueError(f"unknown action {action}")
