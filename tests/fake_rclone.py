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


def main(args):
    cmd = args[0]
    if cmd == "listremotes":
        print("gdrive:")
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
