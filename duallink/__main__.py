"""Start DualLink: python -m duallink [--no-admin | --restore]"""
import argparse
import ctypes
import os
import sys

from . import config, links

ERROR_ALREADY_EXISTS = 183


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def relaunch_as_admin(extra_args):
    exe = sys.executable
    windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(windowless) and "--restore" not in extra_args:
        exe = windowless
    params = " ".join(["-m", "duallink"] + extra_args)
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, project_dir, 1) > 32


def main():
    parser = argparse.ArgumentParser(prog="duallink")
    parser.add_argument("--no-admin", action="store_true",
                        help="run without changing which link Windows prefers")
    parser.add_argument("--restore", action="store_true",
                        help="put the original network metrics back and exit")
    args = parser.parse_args()
    cfg = config.load()

    if args.restore:
        if not is_admin():
            if relaunch_as_admin(["--restore"]):
                return
            sys.exit("Admin rights are needed to restore network settings.")
        ok = links.RouteControl(cfg).restore()
        print("Network settings restored." if ok else
              "Restored what's connected; the rest is restored when that adapter is plugged in.")
        return

    if not args.no_admin and not is_admin():
        if relaunch_as_admin([]):
            return
        # Admin prompt declined: carry on without touching routing.

    global _instance_mutex  # held open until the process exits
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\DualLinkSingleInstance")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        ctypes.windll.user32.MessageBoxW(None, "DualLink is already running (check the tray).",
                                         "DualLink", 0x40)
        return

    control_routes = is_admin() and not args.no_admin
    if control_routes:
        links.RouteControl(cfg).restore()  # undo anything left by a crash

    from .app import App
    App(cfg, control_routes).run()


if __name__ == "__main__":
    main()
