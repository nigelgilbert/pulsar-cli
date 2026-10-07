#!/usr/bin/env python3
"""Live video and control for a Pulsar Axion XM30F over its Wi-Fi hotspot."""

import argparse
import collections
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

HOST = "172.28.0.1"
CTL_PORT = 5005
# Port 5005 serves one client; 5006 takes commands when 5005 is busy.
SIDE_PORT = 5006
REC_STATES = {0: "idle", 1: "recording", 2: "stopping"}
ZOOM_MIN, ZOOM_MAX = 3.0, 12.0
# The scope stops sending RTP to open sessions when a setting such as zoom changes.
STALL_SECS = 1.0
FIRST_DATA_SECS = 10.0
RTSP_URL = f"rtsp://{HOST}/"
# Drop the bottom status bar and black out the REC timer so on-screen icons do not count as heat.
WATCH_FILTER = "crop=528:372:0:0,drawbox=x=0:y=0:w=90:h=65:color=black:t=fill"
WATCH_W, WATCH_H, WATCH_FPS = 264, 186, 10
WATCH_HITS = 3
PREROLL_SECS = 5.0
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
JOIN_SECS = 20.0


class Busy(RuntimeError):
    pass


class Scope:
    def __init__(self, host=HOST, port=CTL_PORT, timeout=3.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def cmd(self, line):
        # Send one CRLF-terminated command and return the parsed JSON reply.
        self.sock.sendall(line.encode() + b"\r\n")
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise Busy("scope closed the control connection")
            self.buf += chunk
        reply, self.buf = self.buf.split(b"\r\n", 1)
        if reply.startswith(b"rejected"):
            raise Busy(f"{line}: rejected by scope")
        data = json.loads(reply)
        result = data.get("result")
        # Errors arrive at the top level or nested inside result.
        err = data.get("error") or (result.get("error") if isinstance(result, dict) else None)
        if err:
            raise RuntimeError(f"{line}: {err.get('msg')} (code {err.get('code')})")
        return result

    def close(self):
        self.sock.close()


def relay(sink, stopped):
    # Copy the RTSP stream to sink as MPEG-TS and reconnect when video stops.
    t0 = time.monotonic()
    while not stopped():
        offset = time.monotonic() - t0
        # Skip probing because the SDP already carries the SPS and PPS.
        src = subprocess.Popen(["ffmpeg", "-v", "error", "-probesize", "32768", "-analyzeduration", "0",
                                *input_args(), "-c", "copy",
                                "-output_ts_offset", f"{offset:.3f}", "-flush_packets", "1",
                                "-f", "mpegts", "-"], stdout=subprocess.PIPE)
        got_data = False
        try:
            while not stopped():
                ready, _, _ = select.select([src.stdout], [], [],
                                            STALL_SECS if got_data else FIRST_DATA_SECS)
                if not ready:
                    break
                chunk = os.read(src.stdout.fileno(), 65536)
                if not chunk:
                    break
                got_data = True
                sink.write(chunk)
                sink.flush()
        finally:
            # Give ffmpeg a moment to send TEARDOWN, then kill it because a stalled read blocks exit.
            src.terminate()
            try:
                src.wait(timeout=0.3)
            except subprocess.TimeoutExpired:
                src.kill()
                src.wait()
        if not stopped():
            print("pulsar: video stopped, reconnecting", file=sys.stderr)
            if not got_data:
                time.sleep(0.5)


def run_stream(sink_argv=None):
    # Start the stream and relay it to a sink process, or to stdout without one.
    control_cmd("stream_start")
    sink = subprocess.Popen(sink_argv, stdin=subprocess.PIPE) if sink_argv else None
    out = sink.stdin if sink else sys.stdout.buffer
    stopped = (lambda: sink.poll() is not None) if sink else (lambda: False)
    try:
        relay(out, stopped)
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    finally:
        if sink:
            try:
                sink.stdin.close()
            except BrokenPipeError:
                pass
            sink.wait()
    return 0


def hot_blobs(frame, level, w=WATCH_W):
    # Return the pixel count of each 4-connected patch brighter than level, largest first.
    mask = frame.translate(bytes(v > level for v in range(256)))
    hot = set()
    i = mask.find(1)
    while i != -1:
        hot.add(i)
        i = mask.find(1, i + 1)
    # Skip frames where most of the picture is hot, such as during shutter calibration.
    if len(hot) > len(frame) // 4:
        return []
    areas = []
    while hot:
        stack, area = [hot.pop()], 0
        while stack:
            p = stack.pop()
            area += 1
            x = p % w
            for q in (p - w, p + w, p - 1 if x else -1, p + 1 if x < w - 1 else -1):
                if q in hot:
                    hot.remove(q)
                    stack.append(q)
        areas.append(area)
    return sorted(areas, reverse=True)


class MotionWatch:
    # Relay sink that saves a clip while a warm patch is in view.
    def __init__(self, duration, delta, min_area, outdir, verbose):
        self.duration, self.delta, self.min_area = duration, delta, min_area
        self.outdir, self.verbose = outdir, verbose
        self.dec = subprocess.Popen(["ffmpeg", "-v", "error", "-f", "mpegts", "-i", "-", "-vf",
                                     f"fps={WATCH_FPS},{WATCH_FILTER},scale={WATCH_W}:{WATCH_H},format=gray",
                                     "-f", "rawvideo", "-"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.preroll = collections.deque()
        self.rec = None
        self.until = 0.0
        threading.Thread(target=self.detect, daemon=True).start()

    def detect(self):
        n = WATCH_W * WATCH_H
        hits, last_log = 0, 0.0
        while len(frame := self.dec.stdout.read(n)) == n:
            median = sorted(frame)[n // 2]
            blobs = hot_blobs(frame, median + self.delta)
            hits = hits + 1 if blobs and blobs[0] >= self.min_area else 0
            now = time.monotonic()
            if hits >= WATCH_HITS:
                self.until = now + self.duration
            if self.verbose and now - last_log >= 1:
                print(f"pulsar: median {median} level {median + self.delta} patches {blobs[:5]}", file=sys.stderr)
                last_log = now

    def write(self, chunk):
        self.dec.stdin.write(chunk)
        now = time.monotonic()
        if now < self.until and not self.rec:
            path = os.path.join(self.outdir, time.strftime("motion-%Y%m%d-%H%M%S.mp4"))
            self.rec = subprocess.Popen(["ffmpeg", "-y", "-v", "fatal", "-f", "mpegts", "-i", "-",
                                         "-c", "copy", path], stdin=subprocess.PIPE)
            for _, c in self.preroll:
                self.rec.stdin.write(c)
            print(f"pulsar: warm patch in view, recording {path}", file=sys.stderr)
        if self.rec:
            self.rec.stdin.write(chunk)
            if now >= self.until:
                self.stop_rec()
        self.preroll.append((now, chunk))
        while self.preroll[0][0] < now - PREROLL_SECS:
            self.preroll.popleft()

    def flush(self):
        self.dec.stdin.flush()
        if self.rec:
            self.rec.stdin.flush()

    def stop_rec(self):
        self.rec.stdin.close()
        self.rec.wait()
        self.rec = None
        print("pulsar: clip saved", file=sys.stderr)

    def close(self):
        if self.rec:
            self.stop_rec()
        self.dec.stdin.close()
        self.dec.wait()


def run_watch(a):
    # Watch the stream and save a clip whenever a warm patch appears.
    os.makedirs(a.outdir, exist_ok=True)
    control_cmd("stream_start")
    watch = MotionWatch(a.duration, a.delta, a.min_area, a.outdir, a.verbose)
    try:
        relay(watch, lambda: False)
    except KeyboardInterrupt:
        pass
    finally:
        watch.close()
    return 0


def load_env(path=ENV_PATH):
    # Read KEY=VALUE lines from .env and let the process environment override them.
    env = {}
    try:
        with open(path) as f:
            for line in f:
                key, sep, value = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    env[key.strip()] = value.strip()
    except FileNotFoundError:
        pass
    return {**env, **os.environ}


def wifi_device():
    # Return the BSD name of the Wi-Fi interface, e.g. en0.
    out = subprocess.run(["networksetup", "-listallhardwareports"],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    for port, dev in zip(out, out[1:]):
        if port.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort"):
            return dev.split(":", 1)[1].strip()
    raise RuntimeError("no Wi-Fi interface found")


def run_connect():
    # Join the scope's hotspot and wait until the control port answers.
    env = load_env()
    ssid, password = env.get("PULSAR_SSID"), env.get("PULSAR_WIFI_PASSWORD")
    if not ssid or not password:
        raise RuntimeError(f"set PULSAR_SSID and PULSAR_WIFI_PASSWORD in {ENV_PATH}")
    dev = wifi_device()
    # networksetup exits 0 on failure and prints the error instead.
    out = subprocess.run(["networksetup", "-setairportnetwork", dev, ssid, password],
                         capture_output=True, text=True)
    msg = (out.stdout + out.stderr).strip()
    if out.returncode or msg:
        raise RuntimeError(f"could not join {ssid}: {msg}")
    # Wait for DHCP by pinging the scope.
    deadline = time.monotonic() + JOIN_SECS
    while subprocess.run(["ping", "-c", "1", "-t", "1", HOST], capture_output=True).returncode:
        if time.monotonic() > deadline:
            raise RuntimeError(f"joined {ssid} but {HOST} does not answer")
    addr = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True, text=True).stdout.strip()
    print(f"connected to {ssid} on {dev} as {addr}")
    return 0


def control_cmd(line):
    # Try port 5005 and fall back to 5006 when 5005 is busy or refuses the command.
    for port in (CTL_PORT, SIDE_PORT):
        scope = Scope(port=port)
        try:
            return scope.cmd(line)
        except Busy as e:
            err = e
        finally:
            scope.close()
    raise err


def control_call(name, arg):
    # Send a command with a JSON argument in the form name?<json>.
    return control_cmd(f"{name}?{json.dumps(arg, separators=(',', ':'))}")


def parse_value(text):
    # Pass numbers as JSON numbers and anything else as a string.
    try:
        return json.loads(text)
    except ValueError:
        return text


def input_args():
    return ["-rtsp_transport", "udp", "-timeout", "5000000", "-buffer_size", "4194304", "-i", RTSP_URL]


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("connect", help="join the scope's Wi-Fi hotspot using .env")
    sub.add_parser("info", help="show device model, serial and firmware")
    get = sub.add_parser("get", help="show current settings, or only the named ones")
    get.add_argument("codes", nargs="*")
    st = sub.add_parser("set", help="change a setting, e.g. set Brightness 12")
    st.add_argument("code")
    st.add_argument("value")
    zm = sub.add_parser("zoom", help=f"show or set zoom, {ZOOM_MIN:g}-{ZOOM_MAX:g}")
    zm.add_argument("level", nargs="?", type=float)
    rc = sub.add_parser("rec", help="start or stop recording on the scope, or show its state")
    rc.add_argument("op", choices=["start", "stop", "status"], nargs="?", default="status")
    sub.add_parser("schema", help="show settings with types and ranges")
    sub.add_parser("play", help="show the live stream in ffplay")
    rec = sub.add_parser("record", help="record the live stream to a file")
    rec.add_argument("output")
    rec.add_argument("-t", "--duration", help="stop after this many seconds")
    wt = sub.add_parser("watch", help="record a clip whenever a warm body comes into view")
    wt.add_argument("-t", "--duration", type=float, default=30,
                    help="seconds to keep recording after the last detection")
    wt.add_argument("--delta", type=int, default=60,
                    help="brightness above the frame median that counts as warm")
    wt.add_argument("--min-area", type=int, default=20,
                    help=f"smallest warm patch in pixels at {WATCH_W}x{WATCH_H}")
    wt.add_argument("-o", "--outdir", default="clips", help="folder for saved clips")
    wt.add_argument("-v", "--verbose", action="store_true",
                    help="print brightness and patch sizes every second")
    sub.add_parser("pipe", help="write the stream as MPEG-TS to stdout")
    sub.add_parser("url", help="start the stream and print the RTSP URL")
    sub.add_parser("stop", help="stop the stream")
    raw = sub.add_parser("raw", help="send a raw control command")
    raw.add_argument("line")
    a = ap.parse_args()

    if a.action == "connect":
        return run_connect()
    if a.action == "zoom":
        if a.level is None:
            result = control_call("get", ["Zoom"])
        else:
            if not ZOOM_MIN <= a.level <= ZOOM_MAX:
                raise RuntimeError(f"zoom must be between {ZOOM_MIN:g} and {ZOOM_MAX:g}")
            result = [control_call("set", {"code": "Zoom", "value": round(a.level * 10) * 100})]
        print(f"{result[0]['value'] / 1000:.1f}x")
        return 0
    if a.action == "rec":
        if a.op != "status":
            control_cmd("start_video" if a.op == "start" else "stop_video")
            # RecStatus takes a moment to follow the command.
            time.sleep(2.5)
        state = control_call("get", ["RecStatus"])[0]["value"]
        print(REC_STATES.get(state, f"state {state}"))
        return 0
    if a.action == "set":
        result = control_call("set", {"code": a.code, "value": parse_value(a.value)})
        print(f"{result['code']}: {result['value']}")
        return 0
    if a.action == "get" and a.codes:
        for item in control_call("get", a.codes):
            print(f"{item['code']}: {item['value']}")
        return 0

    if a.action in ("info", "get", "schema", "stop", "raw", "url"):
        line = {"info": "getdeviceinfo", "get": "get", "schema": "info",
                "stop": "stream_stop", "url": "stream_start"}.get(a.action) or a.line
        result = control_cmd(line)
        if a.action == "url":
            print(RTSP_URL)
            print(f"Open with: ffplay -rtsp_transport udp {RTSP_URL}", file=sys.stderr)
            print(f"Stop with: {sys.argv[0]} stop", file=sys.stderr)
        elif a.action == "get":
            for item in result:
                print(f"{item['code']}: {item['value']}")
        elif result is not None:
            print(json.dumps(result, indent=2))
        return 0

    pipe_in = ["-f", "mpegts", "-i", "-"]
    if a.action == "play":
        return run_stream(["ffplay", "-v", "fatal", "-fflags", "nobuffer", "-flags", "low_delay",
                           "-framedrop", "-window_title", "Pulsar XM30F", *pipe_in])
    if a.action == "record":
        dur = ["-t", a.duration] if a.duration else []
        return run_stream(["ffmpeg", "-y", "-v", "fatal", *pipe_in, *dur, "-c", "copy", a.output])
    if a.action == "pipe":
        return run_stream()
    if a.action == "watch":
        return run_watch(a)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as e:
        sys.exit(f"pulsar: {e}")
