"""Record a tab to MP4 via the CDP screencast.

Chrome pushes a JPEG frame on every paint (Page.screencastFrame, each one
acked so the stream keeps flowing); the frames land in a temp directory and
ffmpeg assembles them into an MP4 honoring the real frame timestamps. Without
ffmpeg on PATH the frames and their concat manifest are kept and the exact
assemble command is printed instead.

Frames only arrive while Chrome rasters the tab, and an occluded tab is
throttled hard -- record a tab someone is looking at, or pass --activate to
focus it for the duration.

Chrome silently drops the screencast on a cross-document navigation, so the
event pump watches Page.frameNavigated and re-arms it -- a recording keeps
rolling across page loads.

Driving the page while recording is what --run is for: the script evaluates
over the recording's own CDP session, because a second web-cli process
attaching to the tab mid-recording stalls the frame stream. With --run the
recording ends shortly after the script settles (--duration stays the cap),
so scripts don't need to be timed against the recording length.
"""

import asyncio
import base64
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..cdp import CDPError, open_target
from ..discovery import list_targets, select_target
from ..output import console, die, emit_json

# Even dimensions for yuv420p, whatever size the screencast frames come in.
_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

# Closing beat after a --run script settles, so its final paint makes the cut.
_RUN_TAIL = 1.5

# Longer beat when the script ended by hard-navigating: its execution context
# died mid-await, but the point was to film the page it navigated to.
_NAV_TAIL = 5.0


async def run(args, conn):
    target = select_target(await list_targets(conn), args.target_id, args.url)
    out = Path(args.output or f"web-cli-{target['id'][:8].lower()}.mp4")

    cast_params = {
        "format": "jpeg",
        "quality": args.quality,
        "maxWidth": args.max_width,
        "maxHeight": args.max_width,
        "everyNthFrame": 1,
    }
    frames: list[tuple[float, bytes]] = []
    run_row = {}
    async with open_target(conn, target) as (session, sid):
        if args.activate:
            await session.send(
                "Target.activateTarget", {"targetId": target["id"]}, session_id=sid
            )
        # Screencast frames arrive without it, but Page.frameNavigated needs
        # the Page domain enabled.
        await session.send("Page.enable", session_id=sid)
        await session.send("Page.startScreencast", cast_params, session_id=sid)

        import os, sys, time  # TEMP DEBUG
        _dbg = os.environ.get("WEB_CLI_DEBUG")  # TEMP DEBUG

        async def pump():
            with contextlib.suppress(CDPError):
                async for msg in session.events():
                    method = msg.get("method")
                    if _dbg:  # TEMP DEBUG
                        print(f"{time.monotonic():.2f} {method}", file=sys.stderr)
                    if method == "Page.screencastFrame":
                        params = msg["params"]
                        frames.append(
                            (
                                params["metadata"]["timestamp"],
                                base64.b64decode(params["data"]),
                            )
                        )
                        await session.send(
                            "Page.screencastFrameAck",
                            {"sessionId": params["sessionId"]},
                            session_id=sid,
                        )
                    elif (
                        method == "Page.frameNavigated"
                        and "parentId" not in msg["params"]["frame"]
                    ):
                        # A cross-document navigation drops the screencast;
                        # re-arm it so the recording rolls on across page loads.
                        await session.send(
                            "Page.startScreencast", cast_params, session_id=sid
                        )

        pump_task = asyncio.create_task(pump())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + args.duration
        try:
            if args.run:
                run_row = await _drive(session, sid, args.run, deadline)
                tail = _NAV_TAIL if run_row.get("run") == "navigated" else _RUN_TAIL
                await asyncio.sleep(min(tail, max(deadline - loop.time(), 0)))
            else:
                await asyncio.sleep(args.duration)
        finally:
            pump_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump_task
            with contextlib.suppress(CDPError):
                await session.send("Page.stopScreencast", session_id=sid)

    if not frames:
        die(
            "No frames arrived. Chrome only rasters visible tabs -- record a "
            "tab someone is looking at, or retry with --activate."
            + (f" (--run script: {run_row['run_error']})" if run_row.get("run_error") else ""),
            code=1,
        )

    row = _assemble(frames, out, keep_frames=args.keep_frames)
    row.update({"id": target["id"], "url": target.get("url", "")})
    row.update(run_row)
    if args.json:
        emit_json(row)
    else:
        where = row.get("path") or row["frames_dir"]
        console.print(f"{row['frames']} frames over {row['seconds']}s -> {where}")
        if run_error := row.get("run_error"):
            console.print(f"--run script did not settle cleanly: {run_error}")
        if hint := row.get("hint"):
            console.print(hint)


async def _drive(session, sid, js, deadline):
    """Evaluate the --run script on the recording's session; never raise.

    Returns row fields: run = ok | error | timeout, plus run_result / run_error.
    A hard navigation destroys the script's execution context and surfaces here
    as an error -- the recording itself keeps going either way.
    """
    loop = asyncio.get_running_loop()
    budget = max(deadline - loop.time(), 0.1)
    try:
        async with asyncio.timeout(budget):
            res = await session.send(
                "Runtime.evaluate",
                {
                    "expression": js,
                    "awaitPromise": True,
                    "returnByValue": True,
                    "userGesture": True,
                },
                session_id=sid,
                # The outer timeout is the real cap; keep send()'s own from
                # firing first (it dies hard, which would drop the frames).
                timeout=budget + 60,
            )
    except TimeoutError:
        return {
            "run": "timeout",
            "run_error": "script still pending at the --duration cap",
        }
    except CDPError as e:
        # A hard navigation destroys the script's execution context mid-await.
        # That's a script that ended by navigating, not a failure -- report it
        # as such so the caller records a landing beat instead of bailing.
        if "navigated" in e.message or "context was destroyed" in e.message:
            return {"run": "navigated"}
        return {"run": "error", "run_error": e.message}
    if "exceptionDetails" in res:
        exc = res["exceptionDetails"]
        detail = exc.get("exception", {}).get("description") or exc.get("text", "")
        return {"run": "error", "run_error": detail.splitlines()[0][:300]}
    return {"run": "ok", "run_result": res.get("result", {}).get("value")}


def _assemble(frames, out, *, keep_frames):
    """Write the frames and their timing manifest; build the MP4 if ffmpeg exists."""
    tmp = Path(tempfile.mkdtemp(prefix="web-cli-record-"))
    lines = []
    for i, (ts, data) in enumerate(frames):
        name = f"{i:05d}.jpg"
        (tmp / name).write_bytes(data)
        # Each frame shows until the next one; the last gets a closing beat.
        seconds = (frames[i + 1][0] - ts) if i + 1 < len(frames) else 0.5
        lines.append(f"file '{name}'\nduration {max(seconds, 0.001):.3f}")
    # ffmpeg's concat demuxer wants the final file repeated after its duration.
    lines.append(f"file '{len(frames) - 1:05d}.jpg'")
    manifest = tmp / "frames.txt"
    manifest.write_text("\n".join(lines) + "\n")

    span = round(frames[-1][0] - frames[0][0], 1)
    ffmpeg_cmd = [
        "ffmpeg", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(manifest),
        "-fps_mode", "vfr", "-vf", _SCALE, "-pix_fmt", "yuv420p", str(out),
    ]

    if not shutil.which("ffmpeg"):
        return {
            "frames_dir": str(tmp),
            "frames": len(frames),
            "seconds": span,
            "hint": f"ffmpeg not found; assemble with: {' '.join(ffmpeg_cmd)}",
        }

    try:
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        die(f"ffmpeg failed: {e.stderr.strip()[:300]} (frames kept in {tmp})", code=1)
    if not keep_frames:
        shutil.rmtree(tmp, ignore_errors=True)
    row = {"path": str(out), "frames": len(frames), "seconds": span}
    if keep_frames:
        row["frames_dir"] = str(tmp)
    return row
