"""Record a tab to MP4 via the CDP screencast.

Chrome pushes a JPEG frame on every paint (Page.screencastFrame, each one
acked so the stream keeps flowing); the frames land in a temp directory and
ffmpeg assembles them into an MP4 honoring the real frame timestamps. Without
ffmpeg on PATH the frames and their concat manifest are kept and the exact
assemble command is printed instead.

Frames only arrive while Chrome rasters the tab, and an occluded tab is
throttled hard -- record a tab someone is looking at, or pass --activate to
focus it for the duration.
"""

import asyncio
import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..cdp import open_target
from ..discovery import list_targets, select_target
from ..output import console, die, emit_json

# Even dimensions for yuv420p, whatever size the screencast frames come in.
_SCALE = "scale=trunc(iw/2)*2:trunc(ih/2)*2"


async def run(args, conn):
    target = select_target(await list_targets(conn), args.target_id, args.url)
    out = Path(args.output or f"web-cli-{target['id'][:8].lower()}.mp4")

    frames: list[tuple[float, bytes]] = []
    async with open_target(conn, target) as (session, sid):
        if args.activate:
            await session.send(
                "Target.activateTarget", {"targetId": target["id"]}, session_id=sid
            )
        await session.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": args.quality,
                "maxWidth": args.max_width,
                "maxHeight": args.max_width,
                "everyNthFrame": 1,
            },
            session_id=sid,
        )
        try:
            async with asyncio.timeout(args.duration):
                async for msg in session.events():
                    if msg.get("method") != "Page.screencastFrame":
                        continue
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
        except TimeoutError:
            pass
        await session.send("Page.stopScreencast", session_id=sid)

    if not frames:
        die(
            "No frames arrived. Chrome only rasters visible tabs -- record a "
            "tab someone is looking at, or retry with --activate.",
            code=1,
        )

    row = _assemble(frames, out, keep_frames=args.keep_frames)
    row.update({"id": target["id"], "url": target.get("url", "")})
    if args.json:
        emit_json(row)
    else:
        where = row.get("path") or row["frames_dir"]
        console.print(f"{row['frames']} frames over {row['seconds']}s -> {where}")
        if hint := row.get("hint"):
            console.print(hint)


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
