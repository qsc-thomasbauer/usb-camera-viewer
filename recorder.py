"""Records a camera's frames to H.264 MP4 files with a separate ffmpeg process."""
import collections
import logging
import os
import shlex
import subprocess
import threading
import time

from camera import _child_setup

log = logging.getLogger("recorder")

here = os.path.dirname(os.path.abspath(__file__))
REC_DIR = os.environ.get("REC_DIR", os.path.join(here, "recordings"))
REC_ENCODER = os.environ.get("REC_ENCODER", "libx264")
REC_ENCODER_ARGS = shlex.split(os.environ.get(
    "REC_ENCODER_ARGS", "-preset veryfast -crf 23" if REC_ENCODER == "libx264" else "-b:v 4M"))
FINISH_TIMEOUT = 60  # the encoder may still have buffered frames to flush when we stop


def jpeg_size(frame):
    """(width, height) from the SOF segment of a JPEG, or None."""
    i, n = 2, len(frame)
    while i + 9 <= n:
        if frame[i] != 0xFF:
            return None
        marker = frame[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (frame[i + 7] << 8) | frame[i + 8], (frame[i + 5] << 8) | frame[i + 6]
        if marker == 0xDA:
            return None
        i += 2 + ((frame[i + 2] << 8) | frame[i + 3])
    return None


def ffmpeg_cmd(path, width, height):
    # Frames are timestamped as they arrive, so the video keeps real time even when the
    # camera's frame rate varies or frames are skipped. The fixed scale keeps the output
    # size constant if the camera switches modes mid-recording.
    return (["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
             "-use_wallclock_as_timestamps", "1", "-f", "mjpeg", "-i", "pipe:0",
             "-vf", f"scale={width}:{height},format=yuv420p", "-c:v", REC_ENCODER]
            + REC_ENCODER_ARGS
            + ["-vsync", "vfr", "-enc_time_base", "1:1000",
               "-movflags", "+faststart", "-f", "mp4", path])


class Recording:
    def __init__(self, cam, first_frame):
        size = jpeg_size(first_frame)
        if size is None:
            raise ValueError("cannot read the frame size")
        width, height = size[0] & ~1, size[1] & ~1  # yuv420p needs even dimensions
        os.makedirs(REC_DIR, exist_ok=True)
        self.cam = cam
        self.name = f"{cam.id}_{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        self.path = os.path.join(REC_DIR, self.name)
        self._part = self.path + ".part"  # renamed once ffmpeg has finished the file
        self.started = time.monotonic()
        self.frames = 0
        self.error = None
        self.done = threading.Event()
        self._stop = threading.Event()
        self._err_tail = collections.deque(maxlen=20)
        self._proc = None
        self._start_error = None
        ready = threading.Event()
        threading.Thread(target=self._run, args=(width, height, ready),
                         name=f"rec-{cam.key}", daemon=True).start()
        ready.wait()
        if self._start_error:
            raise self._start_error
        log.info("[%s] recording %dx%d to %s with %s", cam.key, width, height, self.path, REC_ENCODER)

    @property
    def active(self):
        return not self.done.is_set()

    def stop(self):
        """Stop and wait until the file is complete. Returns True if it was saved."""
        self._stop.set()
        self.done.wait(FINISH_TIMEOUT + 5)
        return self.error is None

    def _run(self, width, height, ready):
        # ffmpeg must be started from this thread: PR_SET_PDEATHSIG kills it when the thread
        # that started it exits, and the HTTP request thread that created us ends right away.
        try:
            self._proc = subprocess.Popen(ffmpeg_cmd(self._part, width, height),
                                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.PIPE, preexec_fn=_child_setup)
        except OSError as e:
            self._start_error = e
            self.done.set()
            return
        finally:
            ready.set()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()
        last_id = -1
        try:
            while not self._stop.is_set() and not self.cam.removed:
                # Always take the newest frame; if the encoder is too slow, frames are skipped.
                frame, fid = self.cam.wait_frame(last_id, timeout=1.0)
                if fid == last_id or frame is None:
                    continue
                last_id = fid
                self._proc.stdin.write(frame)
                self._proc.stdin.flush()
                self.frames += 1
        except (OSError, ValueError):
            pass  # ffmpeg died; reported below
        finally:
            self._finish()

    def _finish(self):
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        try:
            rc = self._proc.wait(timeout=FINISH_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            rc = self._proc.wait()
        self._stderr_thread.join(timeout=2)
        errors = [line for line in self._err_tail if line.strip()]
        if rc == 0 and self.frames:
            os.replace(self._part, self.path)
            log.info("[%s] recording saved: %s (%d frames, %ds)", self.cam.key, self.path,
                     self.frames, time.monotonic() - self.started)
        else:
            self.error = f"ffmpeg exited with code {rc}" + (f": {errors[-1]}" if errors else "")
            if rc == 0:
                self.error = "no frames recorded"
            log.warning("[%s] recording failed: %s", self.cam.key, self.error)
            try:
                os.remove(self._part)
            except OSError:
                pass
        self.done.set()

    def _read_stderr(self):
        try:
            for line in iter(self._proc.stderr.readline, b""):
                self._err_tail.append(line.decode(errors="replace").rstrip())
        except (OSError, ValueError):
            pass


class Recorder:
    """Keeps at most one recording per camera, plus the last finished one for status."""

    def __init__(self):
        self._recs = {}
        self._lock = threading.Lock()

    def start(self, cam):
        with self._lock:
            rec = self._recs.get(cam.id)
            if rec and rec.active:
                return rec
            frame = cam.frame
            if frame is None:
                raise ValueError("camera has no frames yet")
            rec = self._recs[cam.id] = Recording(cam, frame)
            return rec

    def stop(self, cam_id):
        with self._lock:
            rec = self._recs.get(cam_id)
        if rec:
            rec.stop()
        return rec

    def status(self, cam_id):
        rec = self._recs.get(cam_id)
        if rec is None:
            return {"recording": False}
        return {
            "recording": rec.active,
            "rec_elapsed": round(time.monotonic() - rec.started) if rec.active else None,
            "rec_file": rec.name,
            "rec_error": rec.error,
        }

    def stop_all(self):
        with self._lock:
            recs = [r for r in self._recs.values() if r.active]
        for rec in recs:
            rec._stop.set()
        for rec in recs:
            rec.stop()
