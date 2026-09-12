# Human Read

My quick reference. Update at sign-off. Last updated **2026-09-12**.

## Where things stand

- **CrossDrop:** works end to end on the Pi. The server is done and `/v1` is frozen. What's left is clients.
- **Freethrow:** hand tracking and calibration are done (M0–M1.7). Nothing moves a window yet.
- **Integration:** decided, not started. Freethrow sends a URL or file to CrossDrop over HTTP. See [Freethrow PLAN.md](../Freethrow/docs/PLAN.md), under Open items.

## Next, in order

1. **CrossDrop M1:** a Chrome/Edge extension (right-click → Send to wall). First, spend 10 minutes checking the extension can call the Pi without CORS. See [NEXT-STEPS.md](NEXT-STEPS.md) M1.
2. **Freethrow checks:** produce a real spatial profile, test with two real hands, recalibrate the posture gate. M2 depends on all three.
3. **Freethrow M2:** move a window on one monitor.
4. **Throw to CrossDrop:** portal edge first, browser windows only.

## Owed

- [ ] Commit the Freethrow `docs/PLAN.md` change (uncommitted as of 2026-09-12)
- [ ] Run the CrossDrop checks that have never run on the Pi: rollback drill, smoke suite. See [NEXT-STEPS.md](NEXT-STEPS.md), "Never verified on hardware"
- [ ] Answer the four one-word decisions in PLAN §1 (A2, A4, A5, A6)

## Decided (don't re-argue)

- Send URLs and files to the wall, never streamed pixels.
- Convert documents (Office → PDF) on the PC, never on the Pi.
- No HTML, SVG or XML uploads.
- Add file types one at a time, only when a real use needs one.

## Commands

From `E:\Company\Github\CrossDrop`:

```powershell
pytest
python -m roomctl status
```

From `E:\Company\Github\Freethrow`:

```powershell
dotnet test tests\Freethrow.Core.Tests\Freethrow.Core.Tests.csproj
dotnet run --project demos\Freethrow.Demo.Preview -- --calibrate-grab
```

## Sign-off log

Newest first, one line each.

- **2026-09-12:** Weighed app options and the Freethrow integration. Recorded the plan in Freethrow's PLAN.md.
