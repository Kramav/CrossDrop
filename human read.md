# Human Read

My quick reference. Update at sign-off. Last updated **2026-11-24**.

## Where things stand

- **CrossDrop:** works end to end on the Pi. The server is done and `/v1` is frozen. The M1 extension is built and tested headless, but not yet tried in a real browser.
- **CrossDrop streaming (v3):** built and passing end to end against real Chrome on this PC. Not yet tried on the Pi, with Max, or from Firefox.
- **Freethrow:** hand tracking and calibration are done (M0–M1.7). Nothing moves a window yet. This PC has no .NET SDK.
- **Integration:** decided, not started. Freethrow sends a URL or file to CrossDrop over HTTP. See [Freethrow PLAN.md](../Freethrow/docs/PLAN.md), under Open items.

## Next, in order

1. **Try streaming:** turn on HTTPS certificates in Tailscale, re-run `setup.sh` on the Pi, open the https address, press 📺. Check: Max shows picture (not black), sound arrives through Stereo Mix, Pi CPU stays under 80%. See README "Streaming".
2. **Accept CrossDrop M1:** load `extension\` unpacked, save the Pi in the popup, right-click a page, and check the wall shows it. See [extension/README.md](extension/README.md).
3. **Freethrow checks:** follow [Freethrow docs/hardware-checks.md](../Freethrow/docs/hardware-checks.md): install the SDK, calibrate, test two hands, test the posture gate. M2 depends on it.
4. **Freethrow M2:** move a window on one monitor.
5. **Throw to CrossDrop:** portal edge first, browser windows only.

## Owed

- [ ] Commit both repos: CrossDrop (extension, tests, CI, docs, this file) and Freethrow (PLAN, runbook, `install.ps1` fix)
- [ ] Run the CrossDrop checks that have never run on the Pi: rollback drill, smoke suite. See [NEXT-STEPS.md](NEXT-STEPS.md), "Never verified on hardware"
- [ ] Answer the four one-word decisions in PLAN §1 (A2, A4, A5, A6)

## Decided (don't re-argue)

- Send URLs and files to the wall by default. A PC may stream a window to the Pi (v3); the Pi never captures itself.
- Convert documents (Office → PDF) on the PC, never on the Pi.
- No HTML, SVG or XML uploads.
- Add file types one at a time, only when a real use needs one.

## Commands

From `E:\Company\Github\CrossDrop`:

```powershell
.venv\Scripts\python -m pytest
python -m roomctl status
python -m roomctl stream stop
```

From `E:\Company\Github\Freethrow`:

```powershell
dotnet test tests\Freethrow.Core.Tests\Freethrow.Core.Tests.csproj
dotnet run --project demos\Freethrow.Demo.Preview -- --calibrate-grab
```

## Sign-off log

Newest first, one line each.

- **2026-11-24:** Built streaming from a PC to the wall (WebRTC from the browser, no OBS). Reversed "never streamed pixels" to "the Pi never captures itself".

- **2026-09-12:** Weighed app options and the Freethrow integration. Recorded the plan in Freethrow's PLAN.md.
