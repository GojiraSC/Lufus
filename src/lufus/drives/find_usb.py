import psutil
import os
import subprocess
import getpass
from lufus.drives import states


def _media_directories() -> list:  # [ANNOTATION] Extract duplicated path-scanning logic into one helper used by both find_usb and find_DN.
    """Return a deduplicated list of candidate USB mount directories."""
    username = getpass.getuser()
    paths = ["/media", "/run/media", f"/media/{username}", f"/run/media/{username}"]

    seen = set()  # [ANNOTATION] Track seen paths to prevent duplicate directory entries from overlapping scan roots.
    directories = []
    for path in paths:
        if os.path.exists(path) and os.path.isdir(path):
            try:
                for entry in os.listdir(path):
                    full = os.path.join(path, entry)
                    if os.path.isdir(full) and full not in seen:  # [ANNOTATION] Deduplicate: skip path already added from an overlapping parent scan.
                        seen.add(full)
                        directories.append(full)
            except PermissionError:
                print(f"Permission denied accessing {path}")
            except Exception as err:
                print(f"Error accessing {path}: {err}")
    return directories


### USB RECOGNITION ###
def find_usb() -> dict:  # [ANNOTATION] Add return type hint; function always returns a dict.
    """Return a mapping of mount-path -> volume-label for detected USB drives."""
    usbdict = {}

    all_directories = _media_directories()  # [ANNOTATION] Delegate to shared helper instead of duplicating path-scan logic.
    dir_set = set(all_directories)  # [ANNOTATION] Use a set for O(1) membership test inside the partition loop.

    for part in psutil.disk_partitions(all=True):  # [ANNOTATION] Pass all=True to match check_file_sig usage and avoid missing bind-mounted USB volumes.
        if part.mountpoint not in dir_set:  # [ANNOTATION] Single O(1) set lookup replaces the inner for-loop over all_directories.
            continue
        mount_path = part.mountpoint
        device_node = part.device
        if device_node:
            try:
                label = subprocess.check_output(
                    ["lsblk", "-d", "-n", "-o", "LABEL", device_node],
                    text=True,
                    timeout=5,
                ).strip()
                if not label:
                    label = os.path.basename(mount_path)
                usbdict[mount_path] = label
                print(f"Found USB: {mount_path} -> {label}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                label = os.path.basename(mount_path)
                usbdict[mount_path] = label
                print(f"Found USB: {mount_path} -> {label}")

    return usbdict


### FOR DEVICE NODE ###
def find_DN() -> str | None:  # [ANNOTATION] Add return type hint to clarify the function returns a device string or None.
    """Return the device node for the first detected USB drive, or None."""
    all_directories = _media_directories()  # [ANNOTATION] Delegate to shared helper instead of duplicating path-scan logic.
    dir_set = set(all_directories)  # [ANNOTATION] Use a set for O(1) membership test inside the partition loop.

    for part in psutil.disk_partitions(all=True):  # [ANNOTATION] Pass all=True for consistency with find_usb and check_file_sig.
        if part.mountpoint not in dir_set:  # [ANNOTATION] Single O(1) set lookup replaces the inner for-loop over all_directories.
            continue
        device_node = part.device
        if device_node:  # [ANNOTATION] Guard against empty device string before mutating global state.
            states.DN = device_node
            return device_node

    return None
