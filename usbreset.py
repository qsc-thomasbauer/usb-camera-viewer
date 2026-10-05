"""Reset a USB device, the software equivalent of unplugging and replugging it."""
import fcntl
import os
import time

USBDEVFS_RESET = (ord("U") << 8) | 20  # _IO('U', 20)


def reset_usb(usb_devnode, usb_syspath):
    """Reset via USBDEVFS_RESET ioctl, falling back to toggling sysfs 'authorized'.

    Returns the method that worked; raises OSError if both fail. Needs root.
    """
    errors = []
    if usb_devnode:
        try:
            fd = os.open(usb_devnode, os.O_WRONLY)
            try:
                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
            finally:
                os.close(fd)
            return "ioctl"
        except OSError as e:
            errors.append(f"ioctl on {usb_devnode}: {e}")
    if usb_syspath:
        authorized = os.path.join(usb_syspath, "authorized")
        try:
            with open(authorized, "w") as f:
                f.write("0")
            time.sleep(1)
            with open(authorized, "w") as f:
                f.write("1")
            return "authorized"
        except OSError as e:
            errors.append(f"{authorized}: {e}")
    raise OSError("; ".join(errors) or "no USB device path known")
