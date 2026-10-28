#!/usr/bin/env python3
"""USB webcam viewer: web UI and MJPEG streams for every USB camera plugged into this Pi."""
import logging
import os
import signal
import sys
import time

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from discovery import Discovery, Registry

PORT = int(os.environ.get("PORT", 8080))
STREAM_IDLE_CLOSE = 10  # end a stream after this long without new frames; the UI reconnects
KEEPALIVE = 2           # resend the last frame this often while waiting

log = logging.getLogger("app")
here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(here, "static"))
registry = Registry()


def _camera_or_404(cam_id):
    cam = registry.get(cam_id)
    if cam is None:
        abort(404)
    return cam


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/cameras")
def cameras():
    return jsonify(registry.statuses())


@app.post("/api/cameras/<cam_id>/reset")
def reset(cam_id):
    _camera_or_404(cam_id).request_reset()
    return jsonify(ok=True)


@app.get("/snapshot/<cam_id>.jpg")
def snapshot(cam_id):
    frame = _camera_or_404(cam_id).frame
    if frame is None:
        abort(503)
    headers = {"Cache-Control": "no-store"}
    if request.args.get("download"):
        # The frame is sent byte for byte as the camera delivered it (no re-encoding).
        name = f"{cam_id}_{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return Response(frame, mimetype="image/jpeg", headers=headers)


@app.get("/stream/<cam_id>.mjpg")
def stream(cam_id):
    cam = _camera_or_404(cam_id)

    def frames():
        last_id, last_sent, last_new = -1, 0.0, time.monotonic()
        while not cam.removed:
            # Always send the newest frame; slow clients simply skip frames.
            frame, fid = cam.wait_frame(last_id, timeout=1.0)
            now = time.monotonic()
            if fid != last_id:
                last_id, last_new = fid, now
            elif now - last_new > STREAM_IDLE_CLOSE:
                return
            elif now - last_sent < KEEPALIVE:
                continue
            if frame is None:
                continue
            last_sent = now
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")

    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if os.geteuid() != 0:
        log.warning("not running as root: automatic USB resets will fail")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    discovery = Discovery(registry)
    discovery.start()
    log.info("serving on http://0.0.0.0:%d", PORT)
    try:
        app.run(host="0.0.0.0", port=PORT, threaded=True)
    finally:
        log.info("shutting down")
        discovery.stop()
        registry.stop_all()


if __name__ == "__main__":
    main()
