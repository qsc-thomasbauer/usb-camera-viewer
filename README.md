# USB Viewer

Shows every USB webcam plugged into the Pi on a web page: `http://<pi>:8080`.

Cameras are found through udev, using hotplug events plus a rescan every 5s. Each camera
gets its own supervised `ffmpeg` process:

- **Stall watchdog:** if no frame arrives for 5s, ffmpeg is killed and restarted. On the
  first start it waits up to 20s, because ffmpeg alone takes about 5s to start on a Pi 3.
- **Backoff:** retries wait 1, 2, 4 … up to 30s. After 30s of stable streaming the
  failure count is cleared.
- **USB reset:** after 3 failures in a row, the device is reset at the USB level, as if it
  were replugged. This happens at most once per 60s. Each tile also has a manual
  "Reset USB" button.
- **Driver doesn't bind:** if a UVC device shows up on USB but no `/dev/video` node
  appears, it is reset as well.
- **Stable tiles:** cameras are identified by their USB port, so a replugged camera gets
  its old tile back.
- **Modes:** each camera uses its best MJPEG mode, passed through without re-encoding. If a
  mode fails because there isn't enough USB bandwidth (several cameras on one hub), the
  viewer steps down to smaller modes. Cameras without MJPEG are transcoded at 640x480 or
  smaller.

## Install

    ./install.sh

This installs ffmpeg and v4l-utils with apt and the Python dependencies (`pyproject.toml`)
into `.venv` with uv. It then writes `/etc/systemd/system/usb-viewer.service`, which runs
as root so USB resets work, and enables and starts it. You can run it again safely after
changes. Set `PORT=9000 ./install.sh` to use another port.

    journalctl -u usb-viewer -f    # logs
    sudo .venv/bin/python app.py   # run in the foreground instead (stop the service first)

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `PORT` | 8080 | web port |
| `MAX_FPS` | 30 | highest frame rate requested from a camera |
| `STALL_TIMEOUT` | 5 | seconds without frames before restarting |
| `FIRST_FRAME_TIMEOUT` | 20 | seconds to wait for the first frame |
| `RESET_AFTER` | 3 | consecutive failures before a USB reset |
| `RESET_MIN_INTERVAL` | 60 | minimum seconds between automatic resets |
| `BACKOFF_MAX` | 30 | longest wait between retries |
| `RAW_MAX_PIXELS` | 307200 | size cap for transcoded (non-MJPEG) modes |

## API

- `GET /api/cameras`: status of every camera (state, mode, fps, restarts, last error)
- `GET /stream/<id>.mjpg`: MJPEG stream
- `GET /snapshot/<id>.jpg`: latest frame
- `POST /api/cameras/<id>/reset`: USB reset

Note: browsers allow about 6 connections per host, so one browser tab can show about 5
cameras at once.
