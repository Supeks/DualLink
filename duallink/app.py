"""DualLink window and tray icon."""
import os
import queue
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import backup, config, downloader, links

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:  # still usable as a plain window
    pystray = None

LIVE, OFFAIR = "live", "offair"
LIVE_RED, OFFAIR_GREEN, IDLE_GREY = "#d93025", "#1e8e3e", "#e8eaed"
LINK_NAMES = {"ethernet": "Ethernet", "tether": "Phone tether"}


def fmt_bytes(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


SPEED_UNITS = ("Mbps", "MB/s")


def fmt_rate(bytes_per_s, unit="Mbps"):
    if unit == "MB/s":
        v = bytes_per_s / 1e6
    else:
        unit, v = "Mbps", bytes_per_s * 8 / 1e6
    return f"{v:.1f} {unit}" if v < 100 else f"{v:.0f} {unit}"


def fmt_eta(seconds):
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "-"
    s = int(seconds + 0.5)
    if s >= 3600:
        return f"{s // 3600}h {s % 3600 // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s}s"


class RateMeter:
    """Smoothed speed from a growing byte count, so ETAs don't jump around."""

    def __init__(self, smoothing=0.25):
        self.smoothing = smoothing
        self.rate = None
        self._last = None

    def update(self, total_bytes, now):
        if self._last is not None:
            dt = now - self._last[1]
            if dt > 0:
                inst = max(total_bytes - self._last[0], 0) / dt
                self.rate = inst if self.rate is None else (
                    self.smoothing * inst + (1 - self.smoothing) * self.rate)
        self._last = (total_bytes, now)
        return self.rate or 0.0

    def eta(self, remaining):
        return remaining / self.rate if self.rate and self.rate > 1 else None


def _tray_image(mode):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=LIVE_RED if mode == LIVE else OFFAIR_GREEN)
    d.rectangle((18, 22, 46, 28), fill="white")
    d.rectangle((18, 36, 46, 42), fill="white")
    return img


class App:
    def __init__(self, cfg, control_routes):
        self.cfg = cfg
        self.control_routes = control_routes
        self.monitor = links.LinkMonitor(cfg, control_routes)
        self.monitor.start()
        self.downloads = downloader.DownloadManager(cfg, self.monitor.ips)
        self.backup_job = None
        self.resume_backup = False
        self.mode = None
        self.events = queue.Queue()
        self.dl_prev = {}
        self.dl_meters = {}
        self.backup_meter = RateMeter()
        self.obs_ip = None
        self.obs_checked = 0.0
        self.tether_was_up = None
        self.told_about_tray = False

        self.root = tk.Tk()
        self.root.title("DualLink")
        self.root.geometry("820x600")
        self.root.minsize(640, 480)
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self._build()
        self.tray = self._make_tray()
        self.set_mode(cfg.get("mode", OFFAIR))
        self.root.after(500, self.tick)

    # ---------- layout ----------

    def _build(self):
        pad = {"padx": 10, "pady": 6}
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        self.live_btn = tk.Button(top, text="LIVE", width=14, font=("Segoe UI", 12, "bold"),
                                  relief="flat", command=lambda: self.set_mode(LIVE))
        self.off_btn = tk.Button(top, text="OFF-AIR", width=14, font=("Segoe UI", 12, "bold"),
                                 relief="flat", command=lambda: self.set_mode(OFFAIR))
        self.live_btn.pack(side="left")
        self.off_btn.pack(side="left", padx=(8, 0))
        unit_box = ttk.Frame(top)
        unit_box.pack(side="right")
        ttk.Label(unit_box, text="Speed in").pack(side="left")
        self.unit_var = tk.StringVar(value=self.cfg.get("speed_unit", "Mbps"))
        unit_menu = ttk.Combobox(unit_box, textvariable=self.unit_var, values=SPEED_UNITS,
                                 state="readonly", width=6)
        unit_menu.pack(side="left", padx=(6, 0))
        unit_menu.bind("<<ComboboxSelected>>", lambda _e: self.set_unit(self.unit_var.get()))
        self.mode_text = ttk.Label(top, wraplength=400, justify="left")
        self.mode_text.pack(side="left", padx=12)

        lf = ttk.LabelFrame(self.root, text="Links")
        lf.pack(fill="x", **pad)
        for col, head in enumerate(("Link", "Adapter", "IP address", "Download", "Upload")):
            ttk.Label(lf, text=head, font=("Segoe UI", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=8)
        self.link_cells = {}
        for row, role in enumerate(links.ROLES, start=1):
            cells = [ttk.Label(lf, text=LINK_NAMES[role] if c == 0 else "-") for c in range(5)]
            for c, w in enumerate(cells):
                w.grid(row=row, column=c, sticky="w", padx=8, pady=2)
            self.link_cells[role] = cells
        lf.columnconfigure(1, weight=1)

        self.notice = tk.Label(self.root, justify="left", anchor="w", fg="#b3261e",
                               wraplength=780)
        self.notice.pack(fill="x", padx=12)

        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, **pad)
        nb.add(self._build_downloads(nb), text="Downloads")
        nb.add(self._build_backup(nb), text="Google Drive backup")

    def _build_downloads(self, parent):
        f = ttk.Frame(parent, padding=8)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Link:").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(row, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda _e: self.add_download())
        ttk.Button(row, text="Download", command=self.add_download).pack(side="left")

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=(6, 6))
        self.dir_label = ttk.Label(row2, text=f"Save to: {self.cfg['download_dir']}")
        self.dir_label.pack(side="left")
        ttk.Button(row2, text="Change...", command=self.pick_download_dir).pack(side="left", padx=6)
        ttk.Button(row2, text="Open folder",
                   command=lambda: os.startfile(self.cfg["download_dir"])).pack(side="left")

        cols = ("size", "progress", "speed", "eta", "ethernet", "tether", "status")
        self.dl_tree = ttk.Treeview(f, columns=cols, height=8)
        self.dl_tree.heading("#0", text="File")
        self.dl_tree.column("#0", width=200)
        for c, head, w in zip(cols, ("Size", "Progress", "Speed", "Time left", "Ethernet",
                                     "Tether", "Status"),
                              (75, 65, 85, 75, 85, 85, 170)):
            self.dl_tree.heading(c, text=head)
            self.dl_tree.column(c, width=w, anchor="w")
        self.dl_tree.pack(fill="both", expand=True)

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(6, 0))
        for text, fn in (("Pause", self.downloads.pause), ("Resume", self.downloads.resume),
                         ("Cancel", self.downloads.cancel)):
            ttk.Button(btns, text=text, command=lambda fn=fn: self._on_selected(fn)).pack(side="left")
        ttk.Button(btns, text="Delete file", command=self.delete_selected).pack(side="left", padx=(12, 0))
        ttk.Button(btns, text="Clear finished", command=self.clear_finished).pack(side="right")
        return f

    def _build_backup(self, parent):
        f = ttk.Frame(parent, padding=8)
        ttk.Label(f, text="Folders to back up:").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.src_list = tk.Listbox(row, height=5)
        self.src_list.pack(side="left", fill="x", expand=True)
        for s in self.cfg["backup_sources"]:
            self.src_list.insert("end", s)
        side = ttk.Frame(row)
        side.pack(side="left", padx=6, anchor="n")
        ttk.Button(side, text="Add folder...", command=self.add_source).pack(fill="x")
        ttk.Button(side, text="Remove", command=self.remove_source).pack(fill="x", pady=4)

        row2 = ttk.Frame(f)
        row2.pack(fill="x", pady=8)
        ttk.Label(row2, text="Google Drive destination (rclone):").pack(side="left")
        self.remote_var = tk.StringVar(value=self.cfg["backup_remote"])
        ttk.Entry(row2, textvariable=self.remote_var, width=30).pack(side="left", padx=6)
        ttk.Button(row2, text="Save", command=self.save_remote).pack(side="left")

        row3 = ttk.Frame(f)
        row3.pack(fill="x")
        self.backup_start = ttk.Button(row3, text="Start backup", command=self.start_backup)
        self.backup_start.pack(side="left")
        self.backup_stop = ttk.Button(row3, text="Stop", command=self.stop_backup)
        self.backup_stop.pack(side="left", padx=6)
        self.backup_status = ttk.Label(row3, text="Not started")
        self.backup_status.pack(side="left", padx=8)

        self.backup_bar = ttk.Progressbar(f, maximum=1000)
        self.backup_bar.pack(fill="x", pady=8)
        self.backup_detail = ttk.Label(f, text="", justify="left")
        self.backup_detail.pack(anchor="w")
        self.backup_problems = tk.Text(f, height=5, wrap="word", fg="#b3261e", relief="flat",
                                       background=self.root.cget("background"))
        self.backup_problems.pack(fill="both", expand=True, pady=(6, 0))
        self.backup_problems.configure(state="disabled")
        return f

    def _make_tray(self):
        if pystray is None:
            return None
        menu = pystray.Menu(
            pystray.MenuItem("Open DualLink", lambda: self.events.put(("show",)), default=True),
            pystray.MenuItem("Live", lambda: self.events.put(("mode", LIVE)),
                             checked=lambda _i: self.mode == LIVE, radio=True),
            pystray.MenuItem("Off-air", lambda: self.events.put(("mode", OFFAIR)),
                             checked=lambda _i: self.mode == OFFAIR, radio=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.events.put(("quit",))),
        )
        icon = pystray.Icon("DualLink", _tray_image(OFFAIR), "DualLink", menu)
        icon.run_detached()
        return icon

    # ---------- actions ----------

    def set_mode(self, mode):
        if mode == self.mode:
            return
        self.mode = mode
        live = mode == LIVE
        self.downloads.set_allowed(not live)
        if live and self.backup_job and self.backup_job.is_alive():
            self.backup_job.stop()
            self.resume_backup = True
        elif not live and self.resume_backup:
            self.resume_backup = False
            self.start_backup()
        self.live_btn.configure(bg=LIVE_RED if live else IDLE_GREY,
                                fg="white" if live else "black")
        self.off_btn.configure(bg=OFFAIR_GREEN if not live else IDLE_GREY,
                               fg="white" if not live else "black")
        self.mode_text.configure(text=(
            "Live: OBS has ethernet to itself. Games and everything else use the phone "
            "tether. Downloads and backups wait until you're off-air."
            if live else
            "Off-air: downloads and backups use both links together."))
        if self.tray:
            self.tray.icon = _tray_image(mode)
            self.tray.title = f"DualLink - {'Live' if live else 'Off-air'}"
            self.tray.update_menu()
        self.cfg["mode"] = mode
        config.save(self.cfg)

    def set_unit(self, unit):
        self.cfg["speed_unit"] = unit if unit in SPEED_UNITS else "Mbps"
        config.save(self.cfg)

    def rate(self, bytes_per_s):
        return fmt_rate(bytes_per_s, self.cfg.get("speed_unit", "Mbps"))

    def add_download(self):
        url = self.url_var.get().strip()
        if not url.lower().startswith(("http://", "https://")):
            messagebox.showwarning("DualLink", "Paste a link that starts with http:// or https://")
            return
        self.downloads.add(url)
        self.url_var.set("")
        if self.mode == LIVE:
            self.notify("Download queued. It starts when you switch to Off-air.")

    def pick_download_dir(self):
        d = filedialog.askdirectory(initialdir=self.cfg["download_dir"])
        if d:
            self.cfg["download_dir"] = os.path.normpath(d)
            config.save(self.cfg)
            self.dir_label.configure(text=f"Save to: {self.cfg['download_dir']}")

    def _on_selected(self, fn):
        ids = set(self.dl_tree.selection())
        for d in list(self.downloads.items):
            if str(id(d)) in ids:
                fn(d)

    def delete_selected(self):
        ids = set(self.dl_tree.selection())
        chosen = [d for d in list(self.downloads.items) if str(id(d)) in ids]
        if not chosen:
            messagebox.showinfo("DualLink", "Select a download in the list first.")
            return
        names = "\n".join(d.name for d in chosen)
        if not messagebox.askyesno("Delete file", f"Delete from your PC?\n\n{names}\n\n"
                                                  "Unfinished downloads are stopped first. "
                                                  "This can't be undone.", icon="warning"):
            return
        errors = [e for e in (self.downloads.delete(d) for d in chosen) if e]
        self._drop_missing_rows()
        if errors:
            messagebox.showwarning("DualLink", "\n".join(errors))

    def clear_finished(self):
        self.downloads.clear_finished()
        self._drop_missing_rows()

    def _drop_missing_rows(self):
        keep = {str(id(d)) for d in self.downloads.items}
        for iid in self.dl_tree.get_children():
            if iid not in keep:
                self.dl_tree.delete(iid)

    def add_source(self):
        d = filedialog.askdirectory()
        if d and os.path.normpath(d) not in self.cfg["backup_sources"]:
            d = os.path.normpath(d)
            self.cfg["backup_sources"].append(d)
            self.src_list.insert("end", d)
            config.save(self.cfg)

    def remove_source(self):
        for i in reversed(self.src_list.curselection()):
            self.cfg["backup_sources"].remove(self.src_list.get(i))
            self.src_list.delete(i)
        config.save(self.cfg)

    def save_remote(self):
        value = self.remote_var.get().strip()
        if ":" not in value:
            messagebox.showwarning("DualLink", "Use the rclone form remote:folder, "
                                               "for example gdrive:PC Backup")
            return
        self.cfg["backup_remote"] = value
        config.save(self.cfg)

    def start_backup(self):
        if self.mode == LIVE:
            messagebox.showinfo("DualLink", "Backups don't run while you're live.")
            return
        if self.backup_job and self.backup_job.is_alive():
            return
        if not self.cfg["backup_sources"]:
            messagebox.showinfo("DualLink", "Add at least one folder to back up first.")
            return
        self.save_remote()
        self.backup_job = backup.BackupJob(self.cfg, self.monitor.ips)
        self.backup_meter = RateMeter()
        self.backup_job.start()

    def stop_backup(self):
        self.resume_backup = False
        if self.backup_job:
            self.backup_job.stop()

    def notify(self, text):
        if self.tray and self.tray.HAS_NOTIFICATION:
            try:
                self.tray.notify(text, "DualLink")
            except Exception:
                pass

    def hide(self):
        if not self.tray:
            self.quit()
            return
        self.root.withdraw()
        if not self.told_about_tray:
            self.told_about_tray = True
            self.notify("DualLink is still running. Right-click the tray icon to switch "
                        "modes or quit.")

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self):
        if self.backup_job:
            self.backup_job.stop()
        self.downloads.set_allowed(False)  # partial downloads resume next time
        self.monitor.stop()
        if self.control_routes:
            links.RouteControl(self.cfg).restore()
        if self.tray:
            self.tray.stop()
        self.root.destroy()

    # ---------- refresh ----------

    def tick(self):
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "show":
                self.show()
            elif ev[0] == "mode":
                self.set_mode(ev[1])
            elif ev[0] == "quit":
                self.quit()
                return
        snap, error = self.monitor.get()
        self._refresh_links(snap)
        self._refresh_notices(snap, error)
        self._refresh_downloads()
        self._refresh_backup()
        self.root.after(1000, self.tick)

    def _refresh_links(self, snap):
        for role, cells in self.link_cells.items():
            s = snap[role]
            up = bool(s["ip"])
            cells[1].configure(text=s["alias"] or "not found")
            cells[2].configure(text=s["ip"] or "offline")
            cells[3].configure(text=self.rate(s["down"]) if up else "-")
            cells[4].configure(text=self.rate(s["up"]) if up else "-")

    def _refresh_notices(self, snap, error):
        notes = []
        eth_ip, tether_ip = snap["ethernet"]["ip"], snap["tether"]["ip"]
        if not self.control_routes:
            notes.append("Running without admin rights, so DualLink can't make the phone "
                         "tether the main link. Restart it and accept the admin prompt.")
        if not tether_ip:
            notes.append("Phone tether isn't connected - everything is using ethernet."
                         + (" This will eat into your stream's bandwidth." if self.mode == LIVE else ""))
        if not eth_ip:
            notes.append("Ethernet is offline.")
        if self.tether_was_up and not tether_ip and self.mode == LIVE:
            self.notify("Phone tether dropped while live - games are now on ethernet with OBS.")
        self.tether_was_up = bool(tether_ip)

        now = time.monotonic()
        if now - self.obs_checked > 10:
            self.obs_checked = now
            self.obs_ip = links.obs_bind_ip()
        if eth_ip:
            if self.obs_ip is None:
                notes.append("OBS settings not found, so DualLink can't check that OBS uses ethernet.")
            elif self.obs_ip == "default":
                notes.append(f"OBS isn't pinned to ethernet yet. In OBS: Settings > Advanced > "
                             f"Network > Bind to IP, pick {eth_ip}.")
            elif self.obs_ip != eth_ip:
                notes.append(f"OBS is pinned to {self.obs_ip}, but ethernet's address is now "
                             f"{eth_ip}. Update OBS: Settings > Advanced > Network > Bind to IP.")
        if error:
            notes.append(error)
        self.notice.configure(text="\n".join(notes))

    def _refresh_downloads(self):
        now = time.monotonic()
        existing = set(self.dl_tree.get_children())
        for d in list(self.downloads.items):
            iid = str(id(d))
            prev = self.dl_prev.get(iid)
            rates = {}
            for role in links.ROLES:
                b = d.link_bytes[role]
                rates[role] = (b - prev[0][role]) / max(now - prev[1], 1e-3) if prev else 0.0
            self.dl_prev[iid] = (dict(d.link_bytes), now)
            running = d.status == downloader.RUNNING
            meter = self.dl_meters.setdefault(iid, RateMeter())
            if running:
                speed = meter.update(d.done_bytes, now)
                eta = fmt_eta(meter.eta(d.size - d.done_bytes)) if d.size else "-"
            else:
                self.dl_meters[iid] = RateMeter()  # start fresh after a pause
                speed, eta = None, ""
            pct = f"{100 * d.done_bytes / d.size:.0f}%" if d.size else "-"
            status = d.status
            if d.status == downloader.QUEUED and self.mode == LIVE:
                status = "Waiting for Off-air"
            elif d.status == downloader.FAILED:
                status = f"Failed: {d.error}"
            elif running and d.split is False:
                small = d.size is not None and d.size < 2 * d.chunk_size
                status = ("Downloading (small file, one link)" if small
                          else "Downloading (server allows one link only)")
            values = (fmt_bytes(d.size), pct,
                      self.rate(speed) if running else "", eta,
                      self.rate(rates["ethernet"]) if running else "",
                      self.rate(rates["tether"]) if running else "", status)
            if iid in existing:
                self.dl_tree.item(iid, text=d.name, values=values)
            else:
                self.dl_tree.insert("", "end", iid=iid, text=d.name, values=values)

    def _refresh_backup(self):
        job = self.backup_job
        running = bool(job and job.is_alive())
        self.backup_start.configure(state="disabled" if running or self.mode == LIVE else "normal")
        self.backup_stop.configure(state="normal" if running else "disabled")
        if not job:
            return
        status = job.status
        if self.resume_backup and not running:
            status = "Paused for Live - continues when you go Off-air"
        self.backup_status.configure(text=status)
        done = job.progress_bytes()
        if job.total_bytes:
            self.backup_bar["value"] = 1000 * done / job.total_bytes
            eta = ""
            if running and job.status == "Uploading":
                self.backup_meter.update(done, time.monotonic())
                eta = f" - about {fmt_eta(self.backup_meter.eta(job.total_bytes - done))} left"
            self.backup_detail.configure(text=(
                f"{fmt_bytes(done)} of {fmt_bytes(job.total_bytes)} - "
                f"{job.files_done} of {job.files_total} files{eta}\n"
                f"Ethernet {self.rate(job.speed['ethernet'])}   "
                f"Tether {self.rate(job.speed['tether'])}"))
        else:
            self.backup_bar["value"] = 1000 if job.status == "Up to date" else 0
            self.backup_detail.configure(text=job.phase)
        text = "\n".join(job.problems)
        if self.backup_problems.get("1.0", "end-1c") != text:
            self.backup_problems.configure(state="normal")
            self.backup_problems.delete("1.0", "end")
            self.backup_problems.insert("1.0", text)
            self.backup_problems.configure(state="disabled")

    def run(self):
        self.root.mainloop()
