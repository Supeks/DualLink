# DualLink

A Windows tray app that uses your ethernet line and your phone's USB tether
together: one link stays free for streaming, and both links combine for big
downloads and Google Drive backups.

| Mode | Ethernet | Phone tether |
|---|---|---|
| **Live** | OBS only | Games and everything else |
| **Off-air** | Shares downloads and Google Drive backups | Shares downloads and backups, plus everything else |

Downloads and backups never run while you're live. If one is going when you
click **Live**, it pauses and picks up again when you click **Off-air**.

## One-time setup

1. **Python libraries**

   ```bash
   python -m pip install -r requirements.txt
   ```

2. **rclone** (it does the Google Drive uploading)

   ```bash
   winget install Rclone.Rclone
   ```

   Then open a new terminal and connect your Google Drive:

   ```bash
   rclone config
   ```

   Choose `n` (new remote), name it `gdrive`, pick **Google Drive** from the
   list, press Enter through the client ID/secret questions, choose scope `1`
   (full access), and say yes to opening the browser so you can log in.

3. **OBS** — Settings > Advanced > Network > **Bind to IP**: pick your ethernet
   address. DualLink warns you if this stops matching, for example if your
   router hands out a new address.

## Using it

Double-click `DualLink.bat`. Windows asks for admin rights; say yes. That's
what lets DualLink make the phone tether the main link. A coloured dot appears
in the tray: red = Live, green = Off-air. Right-click it to switch modes or quit.
Closing the window just hides it to the tray.

- **Downloads tab:** paste a link and press Download. Big files come over both
  links at once; the Speed, Time left, Ethernet and Tether columns show how
  it's going. **Delete file** removes a download from disk (it asks first).
  Some servers only allow one connection; DualLink then uses the tether alone
  and says so.
- **Speed in:** top right, switch between Mbps and MB/s.
- **Google Drive backup tab:** add folders, then **Start backup**. Each folder
  goes to `PC Backup/<folder name>` on your Drive. Only new or changed files are
  uploaded, so running it again is quick.

## How downloads use both links

Each connection is tied to one link and streams a long stretch of the file.
When a connection finishes its stretch, it takes over the back half of the
biggest stretch still in progress, so the faster link ends up doing more and
everything finishes together. Long stretches matter because some servers wait
up to a second before answering each new request. If a server says "too many
connections", DualLink drops a connection and carries on with fewer.

The other apps on your PC (games, browser) use the tether because DualLink
gives it the lowest Windows interface metric. OBS stays on ethernet through its
own Bind to IP setting.

## Good to know

- **One file uses one link when uploading.** Backups go faster when there are
  many files; a single 50 GB video uploads at one link's speed.
- **Google Drive allows about 750 GB of uploads per day.**
- **If the tether drops,** Windows moves everything to ethernet so you stay
  online. If that happens while you're live, DualLink shows a warning, since
  games then share ethernet with OBS.
- **Your phone plan's hotspot limits still apply.** Watch for fair-use caps.
- **Your network settings go back to normal** when you quit DualLink. If the PC
  crashes, they're fixed the next time DualLink starts, or right away with:

  ```bash
  python -m duallink --restore
  ```

## Settings file

`%APPDATA%\DualLink\config.json`. Things you might change:

- `ethernet_alias` / `tether_alias`: adapter names, if auto-detection picks the
  wrong one (names as shown in Windows network settings).
- `download_workers`: connections per link (default 3 ethernet, 6 tether).
  More isn't always faster: some phone networks slow down with many connections.
- `speed_unit`: `Mbps` or `MB/s`.
- `backup_remote`: where backups go, e.g. `gdrive:PC Backup`.

## Tests

```bash
python -m pytest tests
```
