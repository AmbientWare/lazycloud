import os


def handle(action, path, text=None):
    if action == "write":
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return os.path.getsize(path)
    if action == "read":
        with open(path) as f:
            return f.read()
    if action == "env":
        return sorted(k for k in os.environ if "AWS" in k or "SECRET" in k)
    raise ValueError(f"unknown action {action}")
