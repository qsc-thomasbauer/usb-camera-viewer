"""Per-camera supervisor: probes modes, runs ffmpeg, watches for stalls, retries and resets."""
import collections
import ctypes
import logging
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

import usbreset

log = logging.getLogger("camera")


def _env(name, default, cast=float):
    return cast(os.environ.get(name, default))


MAX_FPS = _env("MAX_FPS", 30)
STALL_TIMEOUT = _env("STALL_TIMEOUT", 5)            # no new frame for this long -> restart
FIRST_FRAME_TIMEOUT = _env("FIRST_FRAME_TIMEOUT", 20)  # ffmpeg alone needs ~5s to start on a Pi 3
RESET_AFTER = _env("RESET_AFTER", 3, int)           # consecutive failures before a USB reset
RESET_MIN_INTERVAL = _env("RESET_MIN_INTERVAL", 60)  # at most one automatic reset per this many s
BACKOFF_MAX = _env("BACKOFF_MAX", 30)
STABLE_AFTER = _env("STABLE_AFTER", 30)             # streaming this long clears the failure count
NODE_TIMEOUT = _env("NODE_TIMEOUT", 10)             # UVC device without /dev/video node -> failure
RAW_MAX_PIXELS = _env("RAW_MAX_PIXELS", 640 * 480, int)  # cap for formats that must be transcoded
MAX_FRAME_BYTES = 16 * 1024 * 1024

# V4L2 fourcc -> ffmpeg v4l2 input_format
FFMPEG_FORMATS = {
    "MJPG": "mjpeg", "JPEG": "mjpeg",
    "YUYV": "yuyv422", "UYVY": "uyvy422", "NV12": "nv12", "NV21": "nv21",
    "YU12": "yuv420p", "GREY": "gray", "RGB3": "rgb24", "BGR3": "bgr24", "H264": "h264",
}

# Standard (JPEG Annex K) Huffman tables as one DHT segment. UVC cameras usually omit
# them from MJPEG frames; we insert them so every browser can decode the frames.
STANDARD_DHT = bytes.fromhex(
    "ffc401a20000010501010101010100000000000000000102030405060708090a0b0100030101010101010101"
    "010000000000000102030405060708090a0b100002010303020403050504040000017d010203000411051221"
    "31410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536"
    "3738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a"
    "92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9"
    "dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa1100020102040403040705040400010277000102031104"
    "052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728"
    "292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485"
    "868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4"
    "d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa"
)

_PR_SET_PDEATHSIG = 1
try:
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
except (OSError, AttributeError):
    _prctl = None


def _child_setup():
    # Kill ffmpeg if we die, so a stale process never keeps the device busy.
    if _prctl:
        _prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)


class ProbeError(Exception):
    pass


@dataclass(frozen=True)
class Mode:
    fourcc: str | None = None
    width: int = 0
    height: int = 0
    fps: float | None = None

    @property
    def input_format(self):
        return FFMPEG_FORMATS.get(self.fourcc)

    @property
    def passthrough(self):
        return self.input_format == "mjpeg"

    def __str__(self):
        if not self.fourcc:
            return "auto (transcoded)"
        s = f"{self.fourcc} {self.width}x{self.height}"
        if self.fps:
            s += f"@{self.fps:g}"
        if not self.passthrough:
            s += " (transcoded)"
        return s


# ---------------------------------------------------------------- mode probing

_FMT_RE = re.compile(r"\[\d+\]: '([^']*)'")
_SIZE_RE = re.compile(r"Size: (?:Discrete (\d+)x(\d+)|(?:Stepwise|Continuous) \d+x\d+ - (\d+)x(\d+))")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) fps")


def parse_formats(text):
    """Parse `v4l2-ctl --list-formats-ext` into {fourcc: {(w, h): {fps, ...}}}."""
    formats, sizes, size = {}, None, None
    for line in text.splitlines():
        if m := _FMT_RE.search(line):
            sizes = formats.setdefault(m.group(1).strip(), {})
            size = None
        elif sizes is not None and (m := _SIZE_RE.search(line)):
            w, h = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
            size = (int(w), int(h))
            sizes.setdefault(size, set())
        elif size is not None and "Interval" in line:
            sizes[size].update(float(f) for f in _FPS_RE.findall(line))
    return formats


def _pick_fps(rates):
    if not rates:
        return None
    ok = [r for r in rates if r <= MAX_FPS + 0.01]
    return max(ok) if ok else min(rates)


def choose_modes(formats):
    """Best-first list of modes: MJPEG largest first, then small raw modes as fallback."""
    modes = [Mode(fourcc, w, h, _pick_fps(rates))
             for fourcc, sizes in formats.items() if fourcc in ("MJPG", "JPEG")
             for (w, h), rates in sizes.items()]
    modes.sort(key=lambda m: (m.width * m.height, m.fps or 0), reverse=True)

    raw = []
    for fourcc, sizes in formats.items():
        if fourcc in ("MJPG", "JPEG") or fourcc not in FFMPEG_FORMATS:
            continue
        cand = sorted(sizes.items(), key=lambda kv: kv[0][0] * kv[0][1])
        small = [kv for kv in cand if kv[0][0] * kv[0][1] <= RAW_MAX_PIXELS] or cand[:1]
        raw += [Mode(fourcc, w, h, _pick_fps(rates)) for (w, h), rates in small]
    raw.sort(key=lambda m: (m.fourcc != "YUYV", -m.width * m.height))
    return modes + raw or [Mode()]


def probe_modes(devnode):
    try:
        res = subprocess.run(["v4l2-ctl", "-d", devnode, "--list-formats-ext"],
                             capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise ProbeError("v4l2-ctl timed out (device not responding)")
    if res.returncode != 0:
        lines = (res.stderr or res.stdout).strip().splitlines()
        raise ProbeError(lines[-1] if lines else f"v4l2-ctl exited {res.returncode}")
    return choose_modes(parse_formats(res.stdout))


def ffmpeg_cmd(devnode, mode):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-f", "v4l2"]
    if mode.input_format:
        cmd += ["-input_format", mode.input_format]
    if mode.width:
        cmd += ["-video_size", f"{mode.width}x{mode.height}"]
    if mode.fps:
        cmd += ["-framerate", f"{mode.fps:g}"]
    cmd += ["-i", devnode]
    cmd += ["-c:v", "copy"] if mode.passthrough else ["-c:v", "mjpeg", "-q:v", "7"]
    return cmd + ["-f", "mjpeg", "pipe:1"]


# ---------------------------------------------------------------- JPEG handling

def _parse_header(buf):
    """Walk JPEG segments from SOI at buf[0] up to SOS.

    Returns (sos_offset, scan_data_offset, has_dht), None if more data is needed,
    or False if the data is not a valid JPEG header.
    """
    i, n, has_dht = 2, len(buf), False
    while True:
        if i + 4 > n:
            return None
        if buf[i] != 0xFF:
            return False
        marker = buf[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker in (0xD8, 0xD9):
            return False
        seglen = (buf[i + 2] << 8) | buf[i + 3]
        if seglen < 2:
            return False
        if marker == 0xDA:
            if i + 2 + seglen > n:
                return None
            return i, i + 2 + seglen, has_dht
        if marker == 0xC4:
            has_dht = True
        i += 2 + seglen


# ---------------------------------------------------------------- supervisor

class Camera:
    def __init__(self, key, cam_id):
        self.key = key
        self.id = cam_id
        self.info = None
        self.present = False
        self.absent_since = None
        self.removed = False

        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self.frame_times = collections.deque(maxlen=30)
        self.session = 0  # bumps each time a new ffmpeg run delivers its first frame

        self.state = "starting"
        self.mode = None
        self.restarts = 0
        self.resets = 0
        self.failures = 0
        self.last_error = None
        self.last_error_time = None
        self.retry_at = None

        self._modes = None
        self._mode_idx = 0
        self._no_frame_fails = 0
        self._last_reset = None
        self._node_missing_since = None
        self._reconnected = False
        self._reprobe = False
        self._reset_requested = False
        self._attempt_first_frame = None
        self._attempt_last_frame = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"cam-{key}", daemon=True)

    # ----- called from other threads

    def start(self):
        self._thread.start()

    def stop(self, join=False):
        self._stop.set()
        self._wake.set()
        self.removed = True
        with self.cond:
            self.cond.notify_all()
        if join:
            self._thread.join(timeout=10)

    def update(self, info):
        """Discovery saw the device (again)."""
        with self.lock:
            old, was_present = self.info, self.present
            self.info, self.present, self.absent_since = info, True, None
            reconnect = not was_present or (old is not None and old.usb_devnode != info.usb_devnode)
            changed = reconnect or old.devnode != info.devnode
            if reconnect:
                self._reconnected = True
            elif changed:
                self._reprobe = True
        if reconnect:
            log.info("[%s] connected: %s (%s) %s", self.key, info.name, info.vidpid,
                     info.devnode or "(no video node yet)")
        elif changed:
            log.info("[%s] video node is now %s", self.key, info.devnode or "(none)")
        if changed:
            self._wake.set()

    def mark_absent(self):
        with self.lock:
            if not self.present:
                return
            self.present = False
            self.absent_since = time.monotonic()
        log.info("[%s] disconnected", self.key)
        self._wake.set()

    def request_reset(self):
        self._reset_requested = True
        self._wake.set()

    def wait_frame(self, last_id, timeout):
        with self.cond:
            if self.frame_id == last_id and not self.removed:
                self.cond.wait(timeout)
            return self.frame, self.frame_id

    def status(self):
        now = time.monotonic()
        times = list(self.frame_times)
        fps = None
        if len(times) >= 2 and now - times[-1] < 2 and times[-1] > times[0]:
            fps = round((len(times) - 1) / (times[-1] - times[0]), 1)
        info = self.info
        return {
            "id": self.id,
            "key": self.key,
            "name": info.name if info else None,
            "vidpid": info.vidpid if info else None,
            "devnode": info.devnode if info else None,
            "present": self.present,
            "state": self.state,
            "mode": str(self.mode) if self.mode else None,
            "fps": fps,
            "frame_age": round(now - times[-1], 1) if times else None,
            "session": self.session,
            "restarts": self.restarts,
            "resets": self.resets,
            "failures": self.failures,
            "last_error": self.last_error,
            "last_error_age": round(now - self.last_error_time) if self.last_error_time else None,
            "retry_in": round(max(0, self.retry_at - now), 1) if self.retry_at and self.state == "retrying" else None,
        }

    # ----- supervisor thread

    def _run(self):
        while not self._stop.is_set():
            try:
                self._step()
            except Exception as e:  # never let the supervisor die
                log.exception("[%s] internal error", self.key)
                self._fail(f"internal error: {e}")
        self.state = "stopped"

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _step(self):
        if self._reconnected:
            self._reconnected = self._reprobe = False
            self._modes, self.failures, self._no_frame_fails = None, 0, 0
        if self._reprobe:
            self._reprobe = False
            self._modes = None
        if self._reset_requested:
            self._reset_requested = False
            self.failures = 0
            self._reset(manual=True)
            return

        with self.lock:
            present, info = self.present, self.info
        if not present:
            self.state = "disconnected"
            self._sleep(5)
            return

        if not info.devnode:
            now = time.monotonic()
            if self._node_missing_since is None:
                self._node_missing_since = now
            if now - self._node_missing_since < NODE_TIMEOUT:
                self.state = "waiting for video node"
                self._sleep(1)
                return
            self._node_missing_since = None
            self._fail("USB video device present but no /dev/video node appeared (driver failed to bind?)")
            return
        self._node_missing_since = None

        if self._modes is None:
            self.state = "probing"
            try:
                self._modes = probe_modes(info.devnode)
            except ProbeError as e:
                self._fail(f"probing {info.devnode} failed: {e}")
                return
            self._mode_idx = 0
            log.info("[%s] modes: %s", self.key, ", ".join(map(str, self._modes)))

        self._stream(info, self._modes[self._mode_idx % len(self._modes)])

    def _stream(self, info, mode):
        devnode = info.devnode
        self.state, self.mode = "starting", mode
        log.info("[%s] starting %s on %s", self.key, mode, devnode)
        try:
            proc = subprocess.Popen(ffmpeg_cmd(devnode, mode), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    preexec_fn=_child_setup)
        except OSError as e:
            self._fail(f"cannot start ffmpeg: {e}")
            return

        err_tail = collections.deque(maxlen=20)
        fix_dht = mode.passthrough and bool(STANDARD_DHT)
        self._attempt_first_frame = self._attempt_last_frame = None
        t_out = threading.Thread(target=self._read_frames, args=(proc.stdout, fix_dht), daemon=True)
        t_err = threading.Thread(target=self._read_stderr, args=(proc.stderr, err_tail), daemon=True)
        t_out.start()
        t_err.start()

        start = time.monotonic()
        reason, stable = None, False
        try:
            while reason is None:
                try:
                    proc.wait(timeout=0.5)
                    reason = "exited"
                    break
                except subprocess.TimeoutExpired:
                    pass
                with self.lock:
                    gone = (not self.present or self.info.devnode != devnode
                            or self.info.usb_devnode != info.usb_devnode)
                if self._stop.is_set() or self._reset_requested or gone:
                    reason = "stop"
                    break
                now = time.monotonic()
                first, last = self._attempt_first_frame, self._attempt_last_frame
                if last is None:
                    if now - start > FIRST_FRAME_TIMEOUT:
                        reason = "no-frames"
                elif now - last > STALL_TIMEOUT:
                    reason = "stall"
                else:
                    self.state = "streaming"
                    if not stable and now - first >= STABLE_AFTER:
                        stable = True
                        self.failures = self._no_frame_fails = 0
        finally:
            self._kill(proc)
            t_out.join(timeout=2)
            t_err.join(timeout=2)

        if reason == "stop":
            return
        got_frames = self._attempt_first_frame is not None
        errors = [line for line in err_tail if line.strip()]
        detail = errors[-1] if errors else ""
        if reason == "exited":
            msg = f"ffmpeg exited with code {proc.returncode}"
        elif reason == "no-frames":
            msg = f"no frames within {FIRST_FRAME_TIMEOUT:g}s"
        else:
            msg = f"stream stalled (no frames for {STALL_TIMEOUT:g}s)"
        msg = f"{mode}: {msg}" + (f": {detail}" if detail else "")

        if any("No space left on device" in line for line in errors):
            # Not enough USB bandwidth for this mode (shared bus): step down right away.
            if self._mode_idx + 1 < len(self._modes):
                self._mode_idx += 1
                self.restarts += 1
                self._set_error(msg + " -> not enough USB bandwidth, trying a smaller mode")
                self._sleep(0.5)
                return
            self._mode_idx = 0
        elif not got_frames:
            self._no_frame_fails += 1
            if self._no_frame_fails >= 2 and len(self._modes) > 1:
                self._no_frame_fails = 0
                self._mode_idx = (self._mode_idx + 1) % len(self._modes)
                msg += f" -> next try: {self._modes[self._mode_idx]}"
        self._fail(msg)

    def _set_error(self, msg):
        self.last_error, self.last_error_time = msg, time.monotonic()
        log.warning("[%s] %s", self.key, msg)

    def _fail(self, msg):
        self.failures += 1
        self.restarts += 1
        self._set_error(msg)
        now = time.monotonic()
        if (self.failures >= RESET_AFTER and self.present
                and (self._last_reset is None or now - self._last_reset >= RESET_MIN_INTERVAL)):
            self._reset(manual=False)
        delay = min(BACKOFF_MAX, 2 ** min(self.failures - 1, 10))
        self.state, self.retry_at = "retrying", time.monotonic() + delay
        log.info("[%s] retrying in %gs (failure #%d)", self.key, delay, self.failures)
        self._sleep(delay)

    def _reset(self, manual):
        info = self.info
        if not self.present or info is None:
            return
        self.state = "resetting"
        self._last_reset = time.monotonic()
        self.resets += 1
        log.warning("[%s] %s USB reset of %s (%s)", self.key, "manual" if manual else "automatic",
                    info.usb_devnode, info.vidpid)
        try:
            method = usbreset.reset_usb(info.usb_devnode, info.usb_syspath)
            log.info("[%s] USB reset done via %s", self.key, method)
        except OSError as e:
            self._set_error(f"USB reset failed: {e}")
        self._modes = None
        self._sleep(3)  # give the device time to re-enumerate

    @staticmethod
    def _kill(proc):
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error("ffmpeg pid %d does not die (stuck in the kernel?); abandoning it", proc.pid)

    def _publish(self, frame):
        now = time.monotonic()
        with self.cond:
            self.frame = frame
            self.frame_id += 1
            self.frame_times.append(now)
            if self._attempt_first_frame is None:
                self._attempt_first_frame = now
                self.session += 1
            self._attempt_last_frame = now
            self.cond.notify_all()

    def _read_frames(self, pipe, fix_dht):
        """Split ffmpeg's MJPEG byte stream into individual JPEG frames."""
        buf = bytearray()
        in_frame, hdr, scan_from = False, None, 0
        while True:
            try:
                chunk = pipe.read1(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            buf += chunk
            while True:
                if not in_frame:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        del buf[:-1]  # keep a trailing 0xFF in case SOI is split
                        break
                    del buf[:start]
                    in_frame, hdr = True, None
                if hdr is None:
                    hdr = _parse_header(buf)
                    if hdr is None:
                        break
                    if hdr is False:  # garbage: resync on the next SOI
                        del buf[:2]
                        in_frame, hdr = False, None
                        continue
                    scan_from = hdr[1]
                end = buf.find(b"\xff\xd9", scan_from)
                if end < 0:
                    scan_from = max(scan_from, len(buf) - 1)
                    break
                sos, _, has_dht = hdr
                if fix_dht and not has_dht:
                    frame = bytes(buf[:sos]) + STANDARD_DHT + bytes(buf[sos:end + 2])
                else:
                    frame = bytes(buf[:end + 2])
                del buf[:end + 2]
                in_frame, hdr = False, None
                self._publish(frame)
            if len(buf) > MAX_FRAME_BYTES:
                buf.clear()
                in_frame, hdr = False, None

    @staticmethod
    def _read_stderr(pipe, tail):
        try:
            for line in iter(pipe.readline, b""):
                tail.append(line.decode(errors="replace").rstrip())
        except (OSError, ValueError):
            pass
