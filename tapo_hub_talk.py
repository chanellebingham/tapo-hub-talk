#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tapo_hub_talk.py — two-way (talk) audio to a TP-Link Tapo camera that sits
behind a Tapo H200 hub, locally, with no cloud and no vendor app.

This is the case that does not work in existing open-source tooling. go2rtc's
`tapo://` backchannel produces no sound through an H200: it returns HTTP 200,
advertises a PCMA sender, streams bytes and closes cleanly, while the camera
stays silent (see AlexxIT/go2rtc#2013, open). pytapo has no talk path for hub
children at all. This script implements the vendor's actual behaviour and
produces audible speech from the camera's own speaker.

Verified working 3 October 2026 on a Tapo C420 behind an H200, firmware as
shipped, by a human listener in the room.

────────────────────────────────────────────────────────────────────────────
WHAT WAS DIFFERENT — four findings, each isolated by experiment
────────────────────────────────────────────────────────────────────────────

1. THE CONNECTION MUST BE OPENED *AS* A TALK CONNECTION.
   Hub children take the stream type in the URL query, not only in the JSON body:

       POST /stream?deviceId=<hex>&type=talk&mode=<mode>&playerId=<uuid4>

   plus an `X-Client-UUID: <playerId>` header. go2rtc sets
   `RawQuery = "deviceId=" + deviceId`, discarding `type`, `mode` and `playerId`,
   so it opens a preview-shaped child connection and then asks for talk in the
   body. The hub relays that JSON — the camera really does enter talk mode — and
   returns a session id, but no talk route to the child was ever established, so
   the audio is accepted and discarded.

2. ⭐ THE PAYLOAD MUST BE AES-ENCRYPTED.
   This is the single requirement that silently fails. A plaintext talk session
   is accepted, returns `error_code: 0`, and makes no sound. Isolated by
   experiment: identical runs differing only in encryption — encrypted audible,
   plaintext silent.

3. ⭐ THE FIRST TALK SESSION AFTER IDLE ONLY WAKES THE CAMERA; IT NEVER PLAYS.
   A single isolated session is silent 100% of the time. Two or more sessions in
   quick succession and the *later* ones are audible. These are battery cameras
   and the audio path sleeps. This script therefore opens a short throwaway
   session carrying silence, then the real one.

4. THE MPEG-TS FRAMING IS VENDOR-SPECIFIC AND ISO-ILLEGAL IN PLACES.
   ffmpeg-produced MPEG-TS is rejected. Each part is exactly 1504 bytes = 8 TS
   packets = PAT + PMT + one PES packet spread over 6 packets:
     * stream_type 0x90 (TP-Link private PCMA), PMT PID 66, ES PID 68, PCR PID 0x100
     * PSI CRC32 fields are literal zeros — the device does not validate them
     * PAT/PMT are repeated in EVERY group, not sent once
     * PES_packet_length is written as 960 (the payload size), i.e. 8 lower than
       ISO 13818-1 requires
     * continuity counters reset to 0 every group and never advance on PAT/PMT
     * 960 A-law bytes per group = 120 ms at 8 kHz; PTS advances exactly 10800
       (90 kHz clock); parts are paced at 120 ms, i.e. real time
   Do not "fix" these to match the standard; the camera accepts the vendor's
   output, not the spec.

NOTE ON WHAT IS *NOT* REQUIRED (both were plausible and both were falsified):
  * `mode` — `aec` and `half_duplex` both work. It is not the discriminator.
  * Holding the session open after the final frame — unnecessary. An isolated
    send with zero hold is heard perfectly.

⚠️ THE DEVICE REPORTS SUCCESS REGARDLESS. `error_code: 0` comes back for the
configuration that speaks and for every configuration that is silently
discarded. There is no return code, log or receipt anywhere in this stack that
distinguishes them, which is why these findings required a person listening in
the room rather than any amount of reading responses.

⚠️ ONE MEDIA SESSION AT A TIME — enforced by the camera, not by software. Stop
any preview/recording of the same camera first.

⚠️ MALFORMED SENDS CAN LOCK THE CAMERA until it is power-cycled. Use --dry-run
first; it muxes and reports without opening a connection.

────────────────────────────────────────────────────────────────────────────
USAGE
────────────────────────────────────────────────────────────────────────────
    pip install pytapo
    # ffmpeg must be on PATH (used only to transcode to A-law, never to mux)

    python tapo_hub_talk.py --hub 192.168.1.50 --device-id <CHILD_HEX> \
                            --password '<TP-Link cloud password>' --file hello.mp3

    python tapo_hub_talk.py ... --dry-run --file hello.mp3     # no network
    python tapo_hub_talk.py ... --file hello.mp3 --no-encrypt  # falsifier: silent

Find a child device id with pytapo:
    from pytapo import Tapo
    h = Tapo("<hub ip>", "admin", "<cloud pw>", cloudPassword="<cloud pw>")
    print(h.getChildDevices())

MIT licensed. Protocol details were derived from the vendor application's own
output and verified by experiment against hardware the author owns.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid

GROUP_BYTES = 1504          # 8 TS packets
ALAW_PER_GROUP = 960        # 120 ms @ 8 kHz
PTS_STEP = 10800            # 960 samples * 90000/8000
ALAW_SILENCE = 0xD5         # linear zero in A-law
PART_SECONDS = 0.120

# Byte-exact PSI. CRC32 fields are deliberately zero; the device ignores them.
PAT = bytes.fromhex("474000100000b00d0001c100000001e04200000000") + b"\xff" * 167
PMT = bytes.fromhex("474042100002b0120001c10000e100f00090e044f00000000000") + b"\xff" * 162
assert len(PAT) == 188 and len(PMT) == 188


def pts5(t: int) -> bytes:
    """5-byte MPEG PTS field, PTS-only form (flags '0010')."""
    t &= (1 << 33) - 1
    return bytes([
        0x20 | ((t >> 29) & 0x0E) | 1,
        (t >> 22) & 0xFF,
        ((t >> 14) & 0xFE) | 1,
        (t >> 7) & 0xFF,
        ((t << 1) & 0xFE) | 1,
    ])


def mux_group(alaw960: bytes, pts: int) -> bytes:
    """One 1504-byte part: PAT + PMT + one PES packet across 6 TS packets."""
    if len(alaw960) != ALAW_PER_GROUP:
        raise ValueError(f"need exactly {ALAW_PER_GROUP} A-law bytes, got {len(alaw960)}")
    pes_hdr = (b"\x00\x00\x01\xc0"
               + (960).to_bytes(2, "big")       # intentionally 8 low — vendor behaviour
               + b"\x80\x80\x05" + pts5(pts))
    p2 = b"\x47\x40\x44\x30" + b"\x01\x00" + pes_hdr + alaw960[:168]
    mids = b"".join(
        bytes([0x47, 0x00, 0x44, 0x10 | cc]) + alaw960[168 + 184 * (cc - 1):168 + 184 * cc]
        for cc in (1, 2, 3, 4)
    )
    p7 = b"\x47\x00\x44\x35" + b"\x7f\x00" + b"\xff" * 126 + alaw960[904:960]
    g = PAT + PMT + p2 + mids + p7
    assert len(g) == GROUP_BYTES
    return g


def mux_all(alaw: bytes, pts_start: int):
    groups, pts = [], pts_start
    for i in range(0, len(alaw), ALAW_PER_GROUP):
        chunk = alaw[i:i + ALAW_PER_GROUP]
        if len(chunk) < ALAW_PER_GROUP:
            chunk += bytes([ALAW_SILENCE]) * (ALAW_PER_GROUP - len(chunk))
        groups.append(mux_group(chunk, pts))
        pts += PTS_STEP
    return groups


def to_alaw(src: str, ffmpeg: str) -> bytes:
    """Transcode any audio to raw G.711 A-law 8 kHz mono.
    ffmpeg is used ONLY for this; its MPEG-TS output is rejected by the camera."""
    r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-i", src,
                        "-ac", "1", "-ar", "8000", "-f", "alaw", "-"],
                       capture_output=True)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode()[:300])
    return r.stdout


async def send_part(ses, data: bytes, session: int, encrypt: bool):
    """Write one part and return immediately.

    pytapo's transceive() is an async generator that blocks waiting for device
    responses after writing. The device never answers audio parts, so awaiting it
    stalls for the full timeout per 120 ms of audio. Headers match the vendor
    application: X-If-Encrypt is present only when encrypting, and a trailing
    CRLF follows the body.
    """
    if encrypt:
        data = ses._aes.encrypt(data)
    headers = {b"Content-Type": b"audio/mp2t"}
    if encrypt:
        headers[b"X-If-Encrypt"] = b"1"
    headers[b"X-Session-Id"] = str(session).encode()
    headers[b"Content-Length"] = str(len(data)).encode()
    await ses._send_http_request(b"--" + ses.client_boundary, headers)
    for i in range(0, len(data), 4096):
        ses._writer.write(data[i:i + 4096])
        await ses._writer.drain()
    ses._writer.write(b"\r\n")
    await ses._writer.drain()


async def talk(hub, port, device_id, password, secret_key, enc_method,
               groups, mode, encrypt, quiet=False):
    import json
    from pytapo.media_stream.session import HttpMediaSession

    qp = {"deviceId": device_id, "type": "talk", "mode": mode,
          "playerId": str(uuid.uuid4())}
    if not quiet:
        print("  POST /stream?" + "&".join(f"{k}={v}" for k, v in qp.items()))

    ses = HttpMediaSession(hub, password, secret_key, enc_method,
                           port=port, query_params=qp)
    await ses.start()
    sid = None
    try:
        gen = ses.transceive(
            data=json.dumps({"type": "request", "seq": 1,
                             "params": {"talk": {"mode": mode}, "method": "get"}}),
            mimetype="application/json", encrypt=encrypt)
        async for resp in gen:
            sid = resp.session
            if not quiet:
                print(f"  session {sid}")
            break
        if sid is None:
            return False
        for g in groups:
            await send_part(ses, g, sid, encrypt)
            await asyncio.sleep(PART_SECONDS)
        if not quiet:
            print(f"  sent {len(groups)} parts ({len(groups)*PART_SECONDS:.1f}s)")
    finally:
        try:
            gen2 = ses.transceive(
                data=json.dumps({"type": "request", "seq": 2,
                                 "params": {"stop": "null", "method": "do"}}),
                mimetype="application/json", encrypt=encrypt, session=sid)
            async for _ in gen2:
                break
        except Exception:
            pass
        try:
            await ses.close()
        except Exception:
            pass
    return True


def main():
    ap = argparse.ArgumentParser(description="Talk audio to a Tapo camera behind an H200 hub.")
    ap.add_argument("--hub", required=True, help="hub IP")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--control-port", type=int, default=443)
    ap.add_argument("--device-id", required=True, help="child device id (hex)")
    ap.add_argument("--password", default=os.environ.get("TAPO_CLOUD_PASSWORD", ""),
                    help="TP-Link cloud password (or $TAPO_CLOUD_PASSWORD)")
    ap.add_argument("--file", required=True, help="audio file to speak")
    ap.add_argument("--mode", default="aec", choices=["aec", "half_duplex", "vad"],
                    help="not a discriminator; aec and half_duplex both work")
    ap.add_argument("--no-encrypt", action="store_true",
                    help="falsifier: produces silence while still returning error_code 0")
    ap.add_argument("--no-wake", action="store_true",
                    help="falsifier: skip the wake session; a lone session is always silent")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--dry-run", action="store_true", help="mux only, open no connection")
    args = ap.parse_args()

    if not args.password and not args.dry_run:
        print("need --password or $TAPO_CLOUD_PASSWORD", file=sys.stderr)
        return 2

    alaw = to_alaw(args.file, args.ffmpeg)
    pts0 = int(time.time() * 1000) & ((1 << 33) - 1)
    groups = mux_all(alaw, pts0)
    print(f"  {len(alaw)} A-law bytes -> {len(groups)} parts "
          f"({len(groups)*GROUP_BYTES} B, {len(groups)*PART_SECONDS:.1f}s)")
    if args.dry_run:
        print("  --dry-run: nothing sent.")
        return 0

    # pytapo's Tapo() authenticates eagerly on its own event loop, so it must be
    # constructed outside asyncio.run().
    from pytapo import Tapo
    probe = Tapo(args.hub, "admin", args.password, cloudPassword=args.password,
                 childID=args.device_id, controlPort=args.control_port,
                 streamPort=args.port)
    secret_key, enc_method = probe.superSecretKey, probe.getEncryptionMethod()
    common = (args.hub, args.port, args.device_id, args.password, secret_key, enc_method)
    encrypt = not args.no_encrypt

    if not args.no_wake:
        print("  wake session (silent) — the first session after idle never plays")
        silence = mux_all(bytes([ALAW_SILENCE]) * (ALAW_PER_GROUP * 4), pts0)
        try:
            asyncio.run(talk(*common, silence, args.mode, encrypt, quiet=True))
        except Exception as e:
            print(f"  wake session failed, continuing: {str(e)[:120]}")
        time.sleep(1.0)

    ok = asyncio.run(talk(*common, groups, args.mode, encrypt))
    print("  transport complete — audibility is confirmed by listening, not by this output.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
