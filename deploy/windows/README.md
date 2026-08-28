# roomtray — the display in the Windows tray

Copy a link or a file, double-click the tray icon, it's on the wall.

The web UI is still the full controller (scroll, saved links, drag-and-drop);
this covers the verbs you reach for twenty times a day without opening a tab, and
puts the display's state where you can see it without asking.

- **Icon colour is the status** — blue = awake, grey = asleep, red = can't reach
  the Pi. Hover for what the selected screen is showing. Polls every 30 s.
- **Double-click** — send whatever is on the clipboard. A copied file uploads; a
  copied link navigates. Files win over text, because copying a file in Explorer
  also puts its path on the clipboard.
- **Right-click** — pick the screen (only shown when the Pi has more than one),
  send a file, Home, Reload, Display off / Wake, or open the web UI.

The chosen screen is remembered in `%APPDATA%\roomtray\screen.txt`.

## Running it

It reads the same `roomctl/targets.toml` — `ROOMCTL_TARGETS` overrides the path,
`-Target <name>` picks a Pi other than the file's `default`.

```powershell
powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File deploy\windows\roomtray.ps1
```

At login: `Win+R` → `shell:startup` → new shortcut with that command line as its
target. Windows hides new tray icons by default — drag it out of the overflow
flyout, or Settings → Personalisation → Taskbar → *Other system tray icons*.

No install, no Python, no dependencies: Windows' own `NotifyIcon`, so the script
plus `targets.toml` is the whole thing on any desktop on the tailnet.

## Checks

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\selfcheck.ps1
```

Offline only — the TOML parse, the 63-char tooltip clamp, the icon, and the
error-detail extraction. Uploading needs a live agent and is not covered; it was
verified against one by sending 256 raw bytes and matching the SHA256 of what
landed in the agent's upload dir.

## Notes

- Uploads go through `System.Net.Http`, not `Invoke-RestMethod -Form`, which is
  PowerShell 7+. Hand-rolling multipart boundaries around binary file bytes in
  5.1 is how you corrupt PDFs.
- No global hotkey. It needs `RegisterHotKey` and a message pump, and
  double-clicking a tray icon is already two keystrokes' worth of effort.
- Clipboard *images* aren't sent — only text links and copied files. It would
  mean writing a temp PNG for a case the file picker already covers.
