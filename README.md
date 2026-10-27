# DualLink

A Windows tray app that uses your ethernet line together with one or more
phones (USB tethering or a Wi-Fi hotspot): ethernet stays free for streaming,
and every link combines for big downloads and Google Drive backups.

| Mode | Ethernet | Main phone link | Extra phone links |
|---|---|---|---|
| **Live** | OBS only | Games and everything else | Idle |
| **Off-air** | Downloads and backups | Downloads and backups, plus everything else | Downloads and backups |

Downloads and backups never run while you're live. If one is going when you
click **Live**, it pauses and picks up again when you click **Off-air**.

## Which links it uses

- **Ethernet:** your wired connection (for OBS).
- **Phone USB tethers:** any number. The first one is the **main link** that
  games and apps use; the others only carry downloads and backups. Plugging a
  phone in mid-download adds it to that download within a second.
- **Wi-Fi:** used when it's connected to a phone hotspot. If it's connected to
  the same router as ethernet it adds no speed, so DualLink leaves it out and
  says so.

**USB bandwidth:** USB 2.0 tethering tops out around 440 Mbps *per USB
controller*, and phones on the same controller share it. For two USB phones,
use ports on different controllers (Device Manager → View → Devices by
connection shows which ports share one). A Wi-Fi hotspot avoids USB entirely.

## One-time setup

1. **Python libraries**

   ```bash
   python -m pip install -r requirements.txt
   ```

2. **Google Drive** — in DualLink's **Google Drive backup** tab, click **Set up
   Google Drive**. DualLink downloads rclone (the uploader it uses, about
   32 MB, from rclone.org, checked against rclone's published checksum), then
   your browser opens: sign in to Google and click **Allow**. The tab then
   shows "Connected" and how much space is free.

3. **OBS** — Settings > Advanced > Network > **Bind to IP**: pick your ethernet
   address. DualLink warns you if this stops matching, for example if your
   router hands out a new address.

## Using it

Double-click `DualLink.bat`. Windows asks for admin rights; say yes. That's
what lets DualLink make the main phone link the one Windows prefers. A coloured
dot appears in the tray: red = Live, green = Off-air. Right-click it to switch
modes or quit. Closing the window just hides it to the tray.

- **Links list:** every link, its speed, and what it's used for right now.
- **Downloads tab:** paste a link and press Download. Big files come over all
  links at once; Speed, Time left and Per link show how it's going. **Delete
  file** removes a download from disk (it asks first). Some servers only allow
  one connection; DualLink then uses a phone link alone and says so.
- **Speed in:** top right, switch between Mbps and MB/s.
- **Google Drive backup tab:** add folders, then **Start backup**. Each folder
  goes to `PC Backup/<folder name>` on your Drive. Only new or changed files are
  uploaded, so running it again is quick.

## How downloads use several links

Each connection is tied to one link and streams a long stretch of the file.
When a connection finishes its stretch, it takes over the back half of the
biggest stretch still in progress, so faster links end up doing more and
everything finishes together. Long stretches matter because some servers wait
up to a second before answering each new request. If a server says "too many
connections", DualLink drops a connection and carries on with fewer.

Games and other apps use the main phone link because DualLink gives it the
lowest Windows interface metric. OBS stays on ethernet through its own Bind to
IP setting.

## How Google Drive backup works

1. **Compare:** for each folder you added, DualLink lists the files on your
   PC and the files already in `PC Backup/<folder name>` on Drive, and picks
   the ones that are new or changed (different size or modified time).
2. **Batch:** those files are grouped into batches (up to 512 MB or 200 files),
   biggest first.
3. **Upload over every link:** each link runs its own rclone upload, tied to
   that link's address, and takes the next batch when it finishes one, so
   faster links upload more.
4. **Next time:** only new or changed files go up. Nothing on Drive is ever
   deleted, even if you delete it on your PC.

Signing in gives rclone a Google token stored in `%APPDATA%\rclone\rclone.conf`;
DualLink never sees your password. **Reconnect** signs in again, for example to
switch Google accounts.

## Good to know

- **One file uses one link when uploading.** Backups go faster when there are
  many files; a single 50 GB video uploads at one link's speed.
- **Google Drive allows about 750 GB of uploads per day.**
- **If the main phone link drops,** games move to the next phone link, or to
  ethernet if there isn't one. If that happens while you're live, DualLink
  shows a warning.
- **Phones on the same carrier** share the same cell tower and often the same
  public address, so a second phone on a *different* carrier usually adds more.
- **Your phone plan's hotspot limits still apply.** Watch for fair-use caps,
  and keep phones charging and cool.
- **Your network settings go back to normal** when you quit DualLink. If the PC
  crashes, they're fixed the next time DualLink starts, or right away with:

  ```bash
  python -m duallink --restore
  ```

## Settings file

`%APPDATA%\DualLink\config.json`. Things you might change:

- `ethernet_alias`: ethernet's adapter name, if auto-detection picks the wrong
  one (names as shown in Windows network settings).
- `main_alias`: which phone tether or Wi-Fi adapter games and apps should use.
  Blank = the first phone tether found.
- `download_workers`: connections per link type (default ethernet 3, tether 6,
  wifi 4), or per link, e.g. `"tether 2": 4`. More isn't always faster: some
  phone networks slow down with many connections.
- `speed_unit`: `Mbps` or `MB/s`.
- `backup_remote`: where backups go, e.g. `gdrive:PC Backup`.

## Tests

```bash
python -m pytest tests
```
