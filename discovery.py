"""Find USB cameras through udev and keep one Camera supervisor per USB port."""
import logging
import re
import threading
import time
from dataclasses import dataclass

import pyudev

from camera import Camera

log = logging.getLogger("discovery")

RESCAN_INTERVAL = 5   # full rescan even without udev events
REMOVE_AFTER = 60     # drop a disconnected camera's tile after this many seconds


@dataclass(frozen=True)
class CameraInfo:
    key: str            # USB interface path, e.g. "1-1.2:1.0"; stable per physical port
    devnode: str | None  # /dev/videoN, None if the driver has not created one
    name: str
    vidpid: str
    usb_devnode: str | None  # /dev/bus/usb/BBB/DDD (changes on every re-enumeration)
    usb_syspath: str


def _attr(dev, name):
    try:
        return dev.attributes.asstring(name).strip()
    except (KeyError, UnicodeDecodeError, OSError):
        return None


def _info(key, devnode, usbdev, video=None):
    props = video.properties if video is not None else {}
    name = (_attr(usbdev, "product") or props.get("ID_V4L_PRODUCT")
            or props.get("ID_MODEL") or "USB camera")
    vidpid = f"{_attr(usbdev, 'idVendor') or '?'}:{_attr(usbdev, 'idProduct') or '?'}"
    return CameraInfo(key, devnode, name, vidpid, usbdev.device_node, usbdev.sys_path)


class Registry:
    def __init__(self):
        self._cams = {}
        self._lock = threading.Lock()

    def sync(self, found):
        now = time.monotonic()
        with self._lock:
            for key, info in found.items():
                cam = self._cams.get(key)
                if cam is None:
                    cam = Camera(key, re.sub(r"[^A-Za-z0-9._-]", "_", key))
                    self._cams[key] = cam
                    cam.update(info)
                    cam.start()
                else:
                    cam.update(info)
            for key, cam in list(self._cams.items()):
                if key in found:
                    continue
                cam.mark_absent()
                if cam.absent_since is not None and now - cam.absent_since > REMOVE_AFTER:
                    log.info("[%s] removed after %ds disconnected", key, REMOVE_AFTER)
                    cam.stop()
                    del self._cams[key]

    def get(self, cam_id):
        with self._lock:
            return next((c for c in self._cams.values() if c.id == cam_id), None)

    def statuses(self):
        with self._lock:
            cams = sorted(self._cams.values(), key=lambda c: c.key)
        return [c.status() for c in cams]

    def stop_all(self):
        with self._lock:
            cams = list(self._cams.values())
        for cam in cams:
            cam.stop()
        for cam in cams:
            cam.stop(join=True)


class Discovery:
    def __init__(self, registry):
        self.registry = registry
        self.context = pyudev.Context()
        self._ignored = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="discovery", daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                monitor = pyudev.Monitor.from_netlink(self.context)
                monitor.filter_by("video4linux")
                monitor.filter_by("usb")
                monitor.start()
                while not self._stop.is_set():
                    self.scan()
                    if monitor.poll(timeout=RESCAN_INTERVAL) is not None:
                        time.sleep(0.3)  # let the rest of the hotplug burst arrive
                        while monitor.poll(timeout=0) is not None:
                            pass
            except Exception:
                log.exception("udev monitoring failed; restarting it")
                self._stop.wait(2)

    def scan(self):
        found = {}
        for dev in self.context.list_devices(subsystem="video4linux"):
            try:
                info = self._video_info(dev)
            except Exception as e:  # device vanished mid-scan, odd attributes, ...
                log.debug("skipping %s: %s", dev.sys_path, e)
                continue
            if info:
                found[info.key] = info
        # UVC devices whose driver never produced a video node still get a tile,
        # so their supervisor can reset them.
        for iface in self.context.list_devices(subsystem="usb", DEVTYPE="usb_interface"):
            try:
                if (iface.sys_name not in found and _attr(iface, "bInterfaceClass") == "0e"
                        and _attr(iface, "bInterfaceSubClass") == "01"):
                    usbdev = iface.find_parent("usb", "usb_device")
                    if usbdev is not None:
                        found[iface.sys_name] = _info(iface.sys_name, None, usbdev)
            except Exception as e:
                log.debug("skipping %s: %s", iface.sys_path, e)
        self.registry.sync(found)

    def _video_info(self, dev):
        devnode = dev.device_node
        if not devnode:
            return None
        usbdev = dev.find_parent("usb", "usb_device")
        if usbdev is None:
            if devnode not in self._ignored:
                self._ignored.add(devnode)
                log.info("ignoring non-USB video device %s (%s)", devnode, _attr(dev, "name"))
            return None
        if _attr(dev, "index") not in (None, "0"):
            return None  # secondary node, e.g. UVC metadata
        caps = dev.properties.get("ID_V4L_CAPABILITIES")
        if caps is not None and ":capture:" not in caps:
            return None
        iface = dev.find_parent("usb", "usb_interface")
        key = iface.sys_name if iface is not None else usbdev.sys_name
        return _info(key, devnode, usbdev, dev)
