"""Mounting, status and directory browsing for Hammerspace shares.

hstk talks to Hammerspace through a shadow "gateway" file on the mounted share,
so every report path must live on a real Hammerspace NFS 4.2 or SMB mount.
Three share kinds are supported:
  nfs   - the container mounts server:/export itself (needs CAP_SYS_ADMIN)
  smb   - the container mounts //server/share with mount.cifs (needs CAP_SYS_ADMIN)
  local - a path already mounted on the host and bind-mounted under HSR_LOCAL_ROOT
"""
import asyncio
import errno
import os
import re
from pathlib import Path

from . import settings, store

MOUNT_ROOT = Path(os.environ.get("HSR_MOUNT_ROOT", "/mnt/hs"))
LOCAL_ROOT = Path(os.environ.get("HSR_LOCAL_ROOT", "/mnt/external"))
CRED_DIR = store.DATA_DIR / "creds"
# Default mount options are global settings (Settings page): NFS "vers=3,nolock" (Hammerspace
# limits NFS 4.2 to approved client kernels; nolock because the container runs no rpc.statd;
# keep attribute caching on), SMB "vers=3.0,noserverino,cache=none,actimeo=0" (noserverino is
# required: hstk's gateway file changes server-side ID between opens).


class ShareError(Exception):
    pass


def share_root(share: dict) -> Path:
    if share["kind"] == "local":
        p = Path(share["local_path"]).resolve()
        if not (p == LOCAL_ROOT or LOCAL_ROOT in p.parents):
            raise ShareError(f"Local paths must be under {LOCAL_ROOT}")
        return p
    return MOUNT_ROOT / share["id"]


def resolve_in_share(share: dict, subpath: str) -> Path:
    """Resolve a user-supplied subpath, refusing anything that escapes the share."""
    root = share_root(share).resolve()
    target = (root / (subpath or "").lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise ShareError("Path is outside the share")
    return target


def _in_mount_table(path: Path) -> bool:
    """Check /proc/mounts rather than stat(): a stale NFS mount can't be stat'ed,
    so os.path.ismount() wrongly reports it as unmounted."""
    target = str(path)
    try:
        with open("/proc/self/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) > 1 and parts[1].replace("\\040", " ") == target:
                    return True
    except OSError:
        return os.path.ismount(path)
    return False


def is_stale(share: dict) -> bool:
    """True when the share is in the mount table but the server rejects its handle."""
    if share["kind"] == "local" or not _in_mount_table(share_root(share)):
        return False
    try:
        os.stat(share_root(share))
        os.listdir(share_root(share))
    except OSError as e:
        return e.errno in (errno.ESTALE, errno.EIO, errno.ENOTCONN)
    return False


def is_mounted(share: dict) -> bool:
    """Mounted and usable."""
    root = share_root(share)
    if share["kind"] == "local":
        return root.is_dir()
    return _in_mount_table(root) and not is_stale(share)


def hammerspace_detected(share: dict) -> bool | None:
    """Best effort: the gateway shadow file resolves on Hammerspace mounts only."""
    if not is_mounted(share):
        return None
    try:
        os.stat(share_root(share) / ".fs_command_gateway")
        return True
    except OSError:
        return False


def status(share: dict) -> dict:
    stale = is_stale(share)
    mounted = not stale and is_mounted(share)
    return {
        "mounted": mounted,
        "stale": stale,
        "hammerspace": hammerspace_detected(share) if mounted else None,
        "root": str(share_root(share)),
    }


async def _run(*cmd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    return proc.returncode, out.decode(errors="replace").strip()


async def mount(share: dict) -> dict:
    if share["kind"] == "local":
        if not is_mounted(share):
            raise ShareError(f"{share_root(share)} does not exist inside the container")
        return status(share)
    if is_stale(share):
        await force_unmount(share)
    if is_mounted(share):
        return status(share)

    mp = share_root(share)
    mp.mkdir(parents=True, exist_ok=True)

    if share["kind"] == "nfs":
        opts = share.get("options") or settings.get()["nfs_options"]
        is_v3 = re.search(r"(^|,)(nfs)?vers=3(,|$)", opts)
        if is_v3 and not re.search(r"(^|,)(no)?lock(,|$)", opts):
            opts += ",nolock"  # no rpc.statd in the container
        cmd = ["mount", "-v", "-t", "nfs", "-o", opts,
               f"{share['server']}:{share['export']}", str(mp)]
    elif share["kind"] == "smb":
        CRED_DIR.mkdir(parents=True, exist_ok=True)
        cred = CRED_DIR / share["id"]
        lines = [f"username={share.get('username', '')}",
                 f"password={share.get('password', '')}"]
        if share.get("domain"):
            lines.append(f"domain={share['domain']}")
        cred.write_text("\n".join(lines) + "\n")
        os.chmod(cred, 0o600)
        user_opts = share.get("options") or settings.get()["smb_options"]
        # hstk's gateway file gets a new server-side ID between its write-open and
        # read-open; with server inode numbers the cifs client rejects the second
        # open as ESTALE. noserverino skips that check. Honor an explicit "serverino".
        if not re.search(r"(^|,)(no)?serverino(,|$)", user_opts):
            user_opts += ",noserverino"
        opts = ",".join(filter(None, [f"credentials={cred}", user_opts]))
        export = share["export"].lstrip("/")
        cmd = ["mount", "-t", "cifs", "-o", opts, f"//{share['server']}/{export}", str(mp)]
    else:
        raise ShareError(f"Unknown share kind {share['kind']}")

    rc, out = await _run(*cmd)
    if rc != 0:
        # With -v, the useful line is usually last; keep enough context to diagnose.
        msg = (out or f"mount exited with {rc}")[-1200:]
        if share["kind"] == "nfs" and "access denied" in msg.lower():
            msg += ("\nThe export may only accept connections from privileged ports (Insecure: false). "
                    "Enable insecure ports on the share's export, or add an export rule for the machine "
                    "running this app (the IP address the cluster sees it connect from) that allows them.")
        raise ShareError(msg)
    return status(share)


async def unmount(share: dict) -> dict:
    if share["kind"] == "local":
        return status(share)
    if is_stale(share):
        await force_unmount(share)
    elif _in_mount_table(share_root(share)):
        rc, out = await _run("umount", str(share_root(share)))
        if rc != 0:
            raise ShareError(out or f"umount exited with {rc}")
    return status(share)


async def force_unmount(share: dict) -> None:
    """Detach a stale or busy mount. -f aborts pending NFS calls, -l detaches it
    even if a hung hs process still holds files open."""
    root = str(share_root(share))
    rc, out = await _run("umount", "-f", "-l", root)
    if rc != 0 and _in_mount_table(share_root(share)):
        raise ShareError(out or f"umount -f -l exited with {rc}")


async def remount(share: dict) -> dict:
    if share["kind"] == "local":
        return status(share)
    if _in_mount_table(share_root(share)):
        await force_unmount(share)
    return await mount(share)


def browse(share: dict, subpath: str, limit: int = 500) -> dict:
    target = resolve_in_share(share, subpath)
    if not target.exists():
        raise ShareError("No such folder on this share")
    if not target.is_dir():
        raise ShareError("That's a file, not a folder")
    root = share_root(share).resolve()
    dirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                except OSError:
                    continue
                if len(dirs) >= limit:
                    break
    except PermissionError as e:
        raise ShareError(f"Permission denied: {e}")
    rel = "/" + str(target.relative_to(root)) if target != root else "/"
    return {"path": rel.replace("//", "/"), "dirs": sorted(dirs, key=str.lower),
            "truncated": len(dirs) >= limit}


async def automount_all() -> None:
    for share in store.shares.all():
        if share.get("auto_mount") and share["kind"] != "local":
            try:
                await mount(share)
            except Exception as e:  # keep starting even if one share is down
                print(f"[hs-reporter] auto-mount of {share['name']} failed: {e}")
