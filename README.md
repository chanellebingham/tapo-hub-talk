# tapo-hub-talk

Local two-way (talk) audio to a TP-Link Tapo camera that sits **behind a Tapo H200 hub** — no cloud,
no vendor app.

This is the case that does not work in existing open-source tooling. Verified working on a Tapo C420
behind an H200 on 3 October 2026.

## The problem

Tapo cameras that connect **directly to wifi** have working two-way audio in several open-source
tools. Cameras that connect **through an H200 hub** do not, anywhere:

- **go2rtc** — its `tapo://` backchannel returns HTTP 200, advertises a PCMA sender, streams bytes and
  closes cleanly, while the camera stays silent. Tracked as
  [go2rtc#2013 "Tapo D230 No 2-Way Audio"](https://github.com/AlexxIT/go2rtc/issues/2013) — open at
  time of writing, same topology (camera behind H200, video fine, outbound audio dead).
- **pytapo** — no talk path for hub children at all; its only occurrence of "talk" is the error string
  `TALK_IS_USED`.
- **Home Assistant Tapo integration** — does not implement two-way audio itself.

## What was different

Four findings, each isolated by changing one variable at a time and having a person in the room
report what they heard.

### 1. The connection must be opened *as* a talk connection

Hub children take the stream type in the **URL query**, not only in the JSON body:

```
POST /stream?deviceId=<hex>&type=talk&mode=<mode>&playerId=<uuid4>
X-Client-UUID: <playerId>
```

go2rtc sets `req.URL.RawQuery = "deviceId=" + deviceId`, discarding `type`, `mode` and `playerId`. It
therefore opens a *preview-shaped* child connection and then asks for talk in the body. The hub
relays that JSON — the camera genuinely enters talk mode — and returns a session id, but no talk
route to the child was ever established, so the audio is accepted and discarded.

*(Measured corroboration: recording the camera's own microphone during a go2rtc push shows the mic
level drop ~21 dB for the duration and recover afterwards. The camera arms itself to speak. Only the
payload never lands. Note this is also why the camera's mic is **not** a valid witness for testing —
these cameras are half-duplex and suppress the mic whenever a talk session is open.)*

### 2. ⭐ The payload must be AES-encrypted

This is the requirement that fails silently. A plaintext talk session is accepted, returns
`error_code: 0`, and makes no sound. Isolated by experiment: two runs differing **only** in
encryption — encrypted audible, plaintext silent.

### 3. ⭐ The first talk session after idle only wakes the camera; it never plays

A single isolated session is silent, 100% of the time, with or without delays. Two or more sessions
in quick succession and the later ones are audible. These are battery cameras and the audio path
sleeps. This tool opens a short throwaway session carrying silence, then the real one.

*How this was found: firing N sends back-to-back, the listener heard only the last one every time
(#2 of 2; #3 of 3). A lone send with a generous hold before teardown was still silent — which ruled
out "my teardown truncates playback" and left "the first session is consumed waking the device".*

### 4. The MPEG-TS framing is vendor-specific and ISO-illegal in places

ffmpeg-produced MPEG-TS is rejected. Each part is exactly **1504 bytes = 8 TS packets** =
PAT + PMT + one PES packet spread over six packets:

| detail | value |
|---|---|
| stream_type | **0x90** (TP-Link private PCMA) |
| PMT PID / ES PID / PCR PID | 66 (0x042) / 68 (0x044) / 0x100 |
| PSI CRC32 | **literal zeros** — the device does not validate them |
| PSI cadence | PAT+PMT repeated in **every** group, not sent once |
| PES_packet_length | written as 960 (the payload size) — 8 **lower** than ISO 13818-1 requires |
| continuity counters | reset to 0 every group; never advance on PAT/PMT |
| payload | 960 A-law bytes per group = 120 ms @ 8 kHz |
| PTS | 90 kHz clock, advances exactly 10800 per group |
| pacing | one part per 120 ms, i.e. real time |

Do not "fix" these to match the standard. The camera accepts the vendor's output, not the spec.

## What is *not* required

Both of these were plausible, and both were falsified by experiment — worth stating so nobody
re-derives them:

- **`mode`** — `aec` and `half_duplex` both work. It is not the discriminator.
- **Holding the session open after the final audio frame** — unnecessary. An isolated send with zero
  hold is heard perfectly.

## ⚠️ The device reports success regardless

`error_code: 0` comes back for the configuration that speaks **and** for every configuration that is
silently discarded. There is no return code, log, or receipt anywhere in this stack that
distinguishes them.

This matters more than any individual finding above: it means the only usable instrument is a person
listening in the room, and the only efficient experiment design is numbered variants fired in
sequence so that **one listen discriminates between several hypotheses**. It also means anyone
debugging this by reading responses will conclude everything is fine.

*(Corollary learned the hard way: multiplexing only discriminates over the axis you multiplexed. The
first sweep here varied **configuration** while the live variable was **position in the sequence** —
so the first result answered a question that wasn't being asked, and looked like an answer to the one
that was.)*

## Install

```
pip install pytapo
```
`ffmpeg` must be on `PATH` — it is used **only** to transcode audio to A-law, never to mux.

## Usage

```bash
# find the child device id
python -c "from pytapo import Tapo; h=Tapo('<hub ip>','admin','<cloud pw>',cloudPassword='<cloud pw>'); print(h.getChildDevices())"

# speak
python tapo_hub_talk.py --hub 192.168.1.50 --device-id <CHILD_HEX> \
                        --password '<TP-Link cloud password>' --file hello.mp3

# mux and report without opening a connection
python tapo_hub_talk.py ... --dry-run --file hello.mp3

# falsifiers — both should be SILENT while still reporting error_code 0
python tapo_hub_talk.py ... --file hello.mp3 --no-encrypt
python tapo_hub_talk.py ... --file hello.mp3 --no-wake
```

The two falsifier flags are deliberate: each reproduces one of the failure modes above on demand, so
the findings can be checked rather than taken on trust.

## Warnings

- **One media session at a time** — enforced by the camera, not by software. Stop any preview or
  recording of the same camera first.
- **Malformed sends can lock the camera** until it is power-cycled. Use `--dry-run` first.
- Battery cameras drain faster when woken repeatedly.

## Credit and provenance

Found and verified by **Chanelle Bingham**, 3 October 2026, on hardware she owns. The protocol
framing was derived from the vendor application's own output; everything in "What was different" was
then confirmed or falsified by direct experiment against her own camera, with a human listener as the
witness.

MIT licensed.
