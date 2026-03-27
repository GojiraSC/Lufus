import shutil
import subprocess
import os
import glob
import tempfile
import contextlib
import re
import time
from lufus.drives import states
from lufus.lufus_logging import get_logger
from lufus.writing.partition_scheme import PartitionScheme

log = get_logger(__name__)


def run(cmd):
    """Wrapper for subprocess.run with logging and error checking."""
    log.debug("run: %s", cmd)
    subprocess.run(cmd, check=True)

def stats(msg:str):
    log.info(msg)
    print(msg)

def _get_wim_size(data_mount: str) -> int:
    """Return size in bytes of install.wim/esd, or 0 if not found."""
    sources_dir = os.path.join(data_mount, "sources")
    for entry in glob.glob(os.path.join(sources_dir, "*")):
        if os.path.basename(entry).lower() in ("install.wim", "install.esd"):
            size = os.path.getsize(entry)
            log.info("Found %s (%d bytes / %.2f GiB)", entry, size, size / (1024**3))
            return size
    log.warning("install.wim/install.esd not found in data partition sources/")
    return 0


def _find_path_case_insensitive(base, *parts):
    current = [base]
    for part in parts:
        next_level = []
        for c in current:
            next_level += [
                p
                for p in glob.glob(os.path.join(c, "*"))
                if os.path.basename(p).lower() == part.lower()
            ]
        current = next_level
    result = current[0] if current else None
    return result


def _fix_efi_bootloader(efi_mount):
    """
    Ensure /EFI/BOOT/BOOTX64.EFI exists - required by UEFI spec.
    Windows ISOs put the bootloader at efi/microsoft/boot/efisys.bin
    but UEFI firmware looks for /EFI/BOOT/BOOTX64.EFI as fallback.
    """
    log.info("EFI bootloader fix: checking %s", efi_mount)
    found_boot_dir = _find_path_case_insensitive(efi_mount, "EFI", "BOOT")
    boot_dir = found_boot_dir or os.path.join(efi_mount, "EFI", "BOOT")
    existing_bootx64 = _find_path_case_insensitive(
        efi_mount, "EFI", "BOOT", "BOOTX64.EFI"
    )
    if existing_bootx64:
        log.info("EFI bootloader fix: BOOTX64.EFI already present at %s", existing_bootx64)
        return

    log.info(
        "EFI bootloader fix: BOOTX64.EFI not found, will attempt to create at %s", boot_dir
    )
    bootx64 = os.path.join(boot_dir, "BOOTX64.EFI")
    run(["sudo", "mkdir", "-p", boot_dir])
    log.info("EFI bootloader fix: created directory %s", boot_dir)

    src = _find_path_case_insensitive(
        efi_mount, "EFI", "Microsoft", "Boot", "bootmgfw.efi"
    )
    if src:
        run(["sudo", "cp", src, bootx64])
        log.info("EFI bootloader fix: copied %s -> %s", src, bootx64)
        return

    log.warning(
        "EFI bootloader fix: could not find bootmgfw.efi, UEFI boot may fail"
    )




def _copy_tree_with_progress(
    src_items: list[str],
    dst: str,
    total_bytes: int,
    status_cb=None,
    progress_cb=None,
    base_pct: int = 60,
    end_pct: int = 75,
) -> None:
    """
    Copy a list of files/directories into dst using shutil, reporting
    per-file progress through status_cb and progress_cb.

    Args:
        src_items:  List of absolute paths (files or dirs) to copy into dst.
        dst:        Destination directory. Must already exist.
        total_bytes: Pre-computed total size of all src_items in bytes.
                     Used to calculate percentage progress. Pass 0 to skip
                     percentage tracking (status messages still fire).
        status_cb:  Optional callable(str) for human-readable status lines.
        progress_cb: Optional callable(int) for overall 0-100 progress.
                     Interpolates between base_pct and end_pct as bytes
                     are copied.
        base_pct:   Progress value at the start of the copy (default 60).
        end_pct:    Progress value when copy completes (default 75).

    Raises:
        OSError:  If a file cannot be read or written.
        shutil.Error: If one or more files failed during copytree
                      (collected and re-raised by shutil).
    """
    copied_bytes = 0

    def _copy_file(src: str, dst: str) -> str:
        """
        copy_function passed to shutil.copytree. Copies one file,
        updates copied_bytes, and fires callbacks.
        """
        nonlocal copied_bytes

        size = os.path.getsize(src)
        name = os.path.relpath(src)

        if status_cb:
            status_cb(f"Copying {name} ({size / 1024**2:.1f} MiB)")

        shutil.copy2(src, dst)   # preserves timestamps, like cp -p
        copied_bytes += size

        if progress_cb and total_bytes > 0:
            pct = base_pct + int((copied_bytes / total_bytes) * (end_pct - base_pct))
            progress_cb(min(pct, end_pct))

        return dst

    for item in src_items:
        item_name = os.path.basename(item)
        dest_path = os.path.join(dst, item_name)
        if os.path.isdir(item):
            shutil.copytree(
                item,
                dest_path,
                copy_function=_copy_file,
                dirs_exist_ok=True,
            )
        else:
            _copy_file(item, dest_path)
            
def _find_ntfs_tool(status_cb=None) -> str | None:
    """Find mkfs.ntfs/mkntfs, installing ntfs-3g if needed. Returns command name or None."""
    for candidate in ["mkfs.ntfs", "mkntfs"]:
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate

    if status_cb:
        status_cb("ntfs-3g not found, attempting to install...")
    pkg_managers = [
        ["apt-get", "install", "-y", "ntfs-3g"],
        ["dnf", "install", "-y", "ntfs-3g"],
        ["pacman", "-S", "--noconfirm", "ntfs-3g"],
        ["zypper", "install", "-y", "ntfs-3g"],
    ]
    for pm_cmd in pkg_managers:
        if subprocess.run(["which", pm_cmd[0]], capture_output=True).returncode == 0:
            run(["sudo"] + pm_cmd)
            break

    for candidate in ["mkfs.ntfs", "mkntfs"]:
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return candidate
    return None
            
def _ensure_wimlib(status_cb=None) -> None:
    """Install wimlib-imagex if not present. Raises FileNotFoundError if it can't be found after install."""
    if subprocess.run(["which", "wimlib-imagex"], capture_output=True).returncode == 0:
        return
    if status_cb:
        status_cb("wimlib-imagex not found, attempting to install...")
    pkg_managers = [
        ["apt-get", "install", "-y", "wimtools"],
        ["dnf",     "install", "-y", "wimlib-utils"],
        ["pacman",  "-S", "--noconfirm", "wimlib"],
        ["zypper",  "install", "-y", "wimtools"],
    ]
    for pm_cmd in pkg_managers:
        if subprocess.run(["which", pm_cmd[0]], capture_output=True).returncode == 0:
            run(["sudo"] + pm_cmd)
            break
    if subprocess.run(["which", "wimlib-imagex"], capture_output=True).returncode != 0:
        raise FileNotFoundError(
            "wimlib-imagex not found. Install manually: "
            "sudo pacman -S wimlib  /  sudo apt install wimtools"
        )
            
def flash_windows(device: str, iso: str, partition_scheme: PartitionScheme, progress_cb=None, status_cb=None) -> bool:
    if not re.match(r"^/dev/(sd[a-z]+|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)$", device):
        raise ValueError(f"Invalid device path: {device}")

    def _emit(pct):
        if progress_cb:
            progress_cb(pct)

    def _status(msg):
        log.info(msg)
        if status_cb:
            status_cb(msg)

    _status(f"flash_windows: starting for device={device}, iso={iso}")

    iso_mount = None

    try:
        # --- Step 1: Mount ISO directly (no extraction needed) ---
        _status(f"Mounting ISO {iso}...")
        iso_mount = mount_iso(iso)
        if iso_mount is None:
            _status("flash_windows: failed to mount ISO")
            return False
        _status(f"ISO mounted at {iso_mount}")
        _emit(8)

        # --- Step 2: Partition the drive ---
        _status(f"Wiping existing partition table on {device}...")
        run(["sudo", "wipefs", "-a", device])

        _status(f"Creating partitions on {device} with scheme {partition_scheme.name}...")
        partitions = create_partitions(device, partition_scheme)
        if not partitions:
            _status("flash_windows: partitioning failed")
            return False

        efi_part = next((p["path"] for p in partitions if p["role"] == "efi"), None)
        data_part = next((p["path"] for p in partitions if p["role"] == "data"), None)

        if not data_part:
            _status("flash_windows: no data partition found after partitioning")
            return False

        _status(f"Partitions: EFI={efi_part}, data={data_part}")
        run(["sudo", "udevadm", "settle"])
        _emit(15)

        # --- Step 3: Format partitions ---
        if efi_part:
            _status(f"Formatting {efi_part} as FAT32 with label BOOT...")
            run(["sudo", "mkfs.vfat", "-F32", "-n", "BOOT", efi_part])

        _status(f"Formatting {data_part} as {partition_scheme.name}...")
        if partition_scheme == PartitionScheme.WINDOWS_NTFS:
            ntfs_cmd = _find_ntfs_tool(status_cb=_status)
            if ntfs_cmd is None:
                raise FileNotFoundError("mkfs.ntfs / mkntfs not found. Install ntfs-3g.")
            run(["sudo", ntfs_cmd, "-f", "-L", "WINDOWS", data_part])
        elif partition_scheme == PartitionScheme.WINDOWS_EXFAT:
            run(["sudo", "mkfs.exfat", "-n", "WINDOWS", data_part])
        elif partition_scheme == PartitionScheme.SIMPLE_FAT32:
            run(["sudo", "mkfs.vfat", "-F32", "-n", "WINDOWS", data_part])
        _emit(22)

        # --- Step 4: Mount target partitions and copy files ---
        with (
            tempfile.TemporaryDirectory() as mount_efi,
            tempfile.TemporaryDirectory() as mount_data,
        ):
            if efi_part:
                _status(f"Mounting {efi_part} -> {mount_efi}")
                run(["sudo", "mount", efi_part, mount_efi])

            _status(f"Mounting {data_part} -> {mount_data}")
            run(["sudo", "mount", data_part, mount_data])

            try:
                # --- Step 5: Copy ISO contents ---
                _status(f"Copying ISO contents from {iso_mount} to {mount_data}...")

                extract_used = sum(
                    os.path.getsize(os.path.join(dp, f))
                    for dp, _, files in os.walk(iso_mount)
                    for f in files
                )
                data_free = shutil.disk_usage(mount_data).free
                log.info(
                    "Space check: ISO content %.2f GiB, data partition free %.2f GiB",
                    extract_used / 1024**3, data_free / 1024**3,
                )
                if data_free < extract_used * 1.02:
                    raise OSError(
                        f"Data partition too small: need {extract_used / 1024**3:.2f} GiB, "
                        f"only {data_free / 1024**3:.2f} GiB free."
                    )

                wim_size = _get_wim_size(iso_mount)
                FAT32_LIMIT = 4 * 1024**3  # 4 GiB

                needs_split = (
                    partition_scheme == PartitionScheme.SIMPLE_FAT32
                    and wim_size > FAT32_LIMIT
                )

                if needs_split:
                    _status(
                        f"install.wim is {wim_size / 1024**3:.2f} GiB — exceeds FAT32 4 GiB limit, "
                        f"will split with wimlib-imagex"
    )
                    # Copy everything except install.wim directly from the loop mount
                    top_level_items = [
        i for i in os.listdir(iso_mount)
        if i.lower() != "sources"
    ]
                    items = [os.path.join(iso_mount, i) for i in top_level_items]
                    _copy_tree_with_progress(
        src_items=items,
        dst=mount_data,
        total_bytes=extract_used,  # slight overcount, acceptable for progress
        status_cb=_status,
        progress_cb=_emit,
        base_pct=22,
        end_pct=60,
    )

                    # Copy sources/ minus install.wim
                    src_sources = _find_path_case_insensitive(iso_mount, "sources")
                    dst_sources = os.path.join(mount_data, "sources")
                    os.makedirs(dst_sources, exist_ok=True)
                    non_wim_sources = [
        os.path.join(src_sources, f)
        for f in os.listdir(src_sources)
        if f.lower() not in ("install.wim", "install.esd")
    ]
                    _copy_tree_with_progress(
        src_items=non_wim_sources,
        dst=dst_sources,
        total_bytes=extract_used,
        status_cb=_status,
        progress_cb=_emit,
        base_pct=60,
        end_pct=70,
    )

                    # Split install.wim into sources/ on data partition
                    wim_src = _find_path_case_insensitive(iso_mount, "sources", "install.wim") \
                           or _find_path_case_insensitive(iso_mount, "sources", "install.esd")
                    wim_dst = os.path.join(dst_sources, "install.swm")
                    _status(f"Splitting {wim_src} -> {wim_dst} (max 3.8 GiB chunks)...")
                    _ensure_wimlib(status_cb=_status)
                    run([
        "wimlib-imagex", "split",
        wim_src, wim_dst,
        str(int(3.8 * 1024)),  # max chunk size in MiB
    ])
                    _status("WIM split complete")
                    _emit(75)

                else:
                    # No split needed — copy everything straight from the loop mount
                    top_level_items = os.listdir(iso_mount)
                    items = [os.path.join(iso_mount, i) for i in top_level_items]
                    _copy_tree_with_progress(
        src_items=items,
        dst=mount_data,
        total_bytes=extract_used,
        status_cb=_status,
        progress_cb=_emit,
        base_pct=22,
        end_pct=75,
    )
                    _status("Copy to data partition complete")
                    _emit(75)

                _status(f"install.wim/esd on data partition: {wim_size / (1024**3):.2f} GiB")
                # --- Step 6: Copy EFI boot files ---
                if efi_part:
                    _status("Copying EFI boot files to EFI partition...")

                    efi_src = _find_path_case_insensitive(iso_mount, "EFI")
                    if efi_src:
                        efi_items = os.listdir(efi_src)
                        _status(f"Found EFI/ with {len(efi_items)} items: {efi_items}")
                        run(
                            ["sudo", "cp", "-r"]
                            + [os.path.join(efi_src, i) for i in efi_items]
                            + [mount_efi]
                        )
                        _status("Copied EFI/ tree to EFI partition")
                    else:
                        _status("WARNING: No EFI directory found in ISO - drive may not be UEFI bootable")

                    boot_src = _find_path_case_insensitive(iso_mount, "boot")
                    if boot_src:
                        boot_items = os.listdir(boot_src)
                        _status(f"Found boot/ with {len(boot_items)} items")
                        run(
                            ["sudo", "cp", "-r"]
                            + [os.path.join(boot_src, i) for i in boot_items]
                            + [mount_efi]
                        )
                        _status("Copied boot/ tree to EFI partition")

                    for fname in ["bootmgr", "bootmgr.efi"]:
                        src = _find_path_case_insensitive(iso_mount, fname)
                        if src:
                            run(["sudo", "cp", src, f"{mount_efi}/{fname}"])
                            _status(f"Copied {fname} to EFI partition root")

                    _fix_efi_bootloader(mount_efi)
                _emit(88)

                # --- Step 7: Sync ---
                _status("Syncing all writes to disk...")
                run(["sudo", "sync"])
                _emit(97)
                _status("Sync complete")

            except Exception as e:
                log.error("flash_windows: ERROR - %s: %s", type(e).__name__, e)
                _status(f"flash_windows: ERROR - {type(e).__name__}: {e}")
                raise
            finally:
                _status("Unmounting target partitions...")
                if efi_part:
                    subprocess.run(["sudo", "umount", mount_efi], capture_output=True)
                subprocess.run(["sudo", "umount", mount_data], capture_output=True)
                _status("Unmount complete")

        _status("flash_windows: finished successfully, Windows USB is ready")
        _emit(100)
        return True

    except (OSError, subprocess.CalledProcessError) as e:
        log.error("flash_windows: failed: %s", e)
        _status(f"flash_windows: failed: {e}")
        return False

    finally:
        # Always unmount the ISO
        if iso_mount and os.path.ismount(iso_mount):
            _status(f"Unmounting ISO from {iso_mount}...")
            subprocess.run(["sudo", "umount", iso_mount], capture_output=True)
            
            
# ---new---
def mount_iso(iso_path:str)->str|None:
    """This function mounts an iso file at /mnt/iso/ and returns the location if mount is successfull
    
    command:`sudo mount -o loop iso_path /mnt/iso/{iso name without extension}

    Args:
        iso_path (str): The location of the iso file

    Returns:
        str: the path where it's mounted
        None: if mounting fails return None
    """
    mount_base="/mnt/iso"
    basename=os.path.basename(iso_path)
    iso_name_without_extension=os.path.splitext(basename)[0]
    iso_mount_location=os.path.join(mount_base,iso_name_without_extension)
    
    try:
        os.makedirs(iso_mount_location,exist_ok=True)
        stats(f"Mounting {iso_path} in {iso_mount_location}")
        result=subprocess.run(["sudo","mount","-o","loop",iso_path,iso_mount_location],
                              capture_output=True,
                              text=True)
        
        if result.returncode==0:
            stats(f"Success: Mounted {iso_path} to {iso_mount_location} successfully!")
            return iso_mount_location
        else:
            stats(f"Failed: Failed to mount {iso_path} to {iso_mount_location} successfully!")

            return None
    except Exception as e:
        stats(f"An error occured during mounting iso: {e}")
        return None
def create_partitions(drive: str, scheme: PartitionScheme) -> list[dict[str, str]]:
    """
    Unified function to partition a drive based on a selected PartitionScheme.
    Returns a list of created partition paths and their roles.
    """
    # Define the sfdisk scripts for each enum case
    scripts = {
    PartitionScheme.WINDOWS_NTFS: (
        "size=-2G, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"  # NTFS Data (leaves 2G at end)
        "size=1G, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"   # EFI
    ),
    PartitionScheme.WINDOWS_EXFAT: (
        "size=-2G, type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"  # exFAT Data
        "size=1G, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B\n"   # EFI
    ),
    PartitionScheme.SIMPLE_FAT32: (
        "type=EBD0A0A2-B9E5-4433-87C0-68B6B72699C7\n"            # Single FAT32 (unchanged)
    )
}

    script = scripts.get(scheme)
    if not script:
        raise ValueError(f"Invalid partition scheme: {scheme}")

    try:
        # Apply the GPT table and script
        subprocess.run(["sudo", "sfdisk", "--label", "gpt", drive], 
                       input=script, text=True, check=True)
        
        # Refresh kernel table
        subprocess.run(["sudo", "partprobe", drive], check=True)
        time.sleep(0.5)

        # Build the return list
        separator = "p" if drive[-1].isdigit() else ""
        num_parts = len(script.strip().split('\n'))
        
        if num_parts > 1:
            return [
                    {"role": "data", "path": f"{drive}{separator}1"},
                    {"role": "efi",  "path": f"{drive}{separator}2"}
            ]
        else:
            return [{"role": "data", "path": f"{drive}{separator}1"}]

    except subprocess.CalledProcessError as e:
        print(f"Error partitioning {drive}: {e}")
        return []
