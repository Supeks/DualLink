"""Stand-in for rclone in tests. "gdrive:Some/Path" maps to $FAKE_DRIVE_ROOT/Some/Path."""
import json
import os
import shutil
import sys
import time

ROOT = os.environ["FAKE_DRIVE_ROOT"]


def local(remote):
    return os.path.join(ROOT, remote.split(":", 1)[1])


def opt(args, name):
    return args[args.index(name) + 1]


REMOTES = os.path.join(ROOT, "remotes.txt")  # if present, the configured remotes


def configured():
    if not os.path.exists(REMOTES):
        return ["gdrive"]
    with open(REMOTES, encoding="utf-8") as f:
        return [ln for ln in f.read().split() if ln]


def save_remotes(names):
    with open(REMOTES, "w", encoding="utf-8") as f:
        f.write("\n".join(names))


def main(args):
    cmd = args[0]
    if cmd == "listremotes":
        print("\n".join(f"{n}:" for n in configured()))
        return 0
    if args[:2] == ["config", "create"]:
        # Real rclone opens the browser here; FAKE_SIGNIN=fail acts like a cancelled sign-in.
        if os.environ.get("FAKE_SIGNIN") == "fail":
            print("Error: failed to get token: auth cancelled", file=sys.stderr)
            return 1
        save_remotes(configured() + [args[2]])
        with open(os.path.join(ROOT, "created.log"), "a", encoding="utf-8") as f:
            f.write(" ".join(args[2:]) + "\n")
        return 0
    if args[:2] == ["config", "delete"]:
        save_remotes([n for n in configured() if n != args[2]])
        return 0
    if cmd == "about":
        if args[1].rstrip(":") not in configured():
            print("Failed to about: didn't find section in config file", file=sys.stderr)
            return 1
        print(json.dumps({"total": 2_000_000_000_000, "used": 500_000_000_000,
                          "free": 1_500_000_000_000}))
        return 0
    if cmd == "lsjson":
        return 3  # nothing on the fake Drive yet
    if cmd == "copy":
        src, dest = args[1], local(args[2])
        bind = opt(args, "--bind")
        with open(opt(args, "--files-from-raw"), encoding="utf-8") as f:
            files = [ln for ln in f.read().splitlines() if ln]
        done = 0
        for rel in files:
            target = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(src, rel), target)
            time.sleep(float(os.environ.get("FAKE_RCLONE_DELAY", "0")))  # like a real upload
            done += os.path.getsize(target)
            print(json.dumps({"level": "info", "msg": "stats",
                              "stats": {"bytes": done, "speed": 1000.0}}), file=sys.stderr)
        with open(os.path.join(ROOT, "binds.log"), "a", encoding="utf-8") as f:
            f.write(f"{bind} {len(files)}\n")
        return 0
    return 1


sys.exit(main(sys.argv[1:]))
