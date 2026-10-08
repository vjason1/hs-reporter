# Troubleshooting

## Mount and run errors

| Message | Likely cause | What to do |
|---|---|---|
| Mounts fail, and the share is labeled **Anvil address** | The share mounts from its cluster's management address (the Anvil) | Edit the share and use a DSX data address: **Find DSX data addresses** lists them |
| `mount.nfs: access denied by server` (export open to everyone) | The export requires privileged source ports (`Insecure: false`), and the container's traffic arrives from other ports | Enable insecure ports on the share's export, or add an export rule for this machine's IP that allows them, or mount on the host |
| `mount.nfs: Operation not permitted` with `vers=4.2` | NFS 4.2 is limited to approved client kernels | Use NFSv3 (the default), or run on a host with an approved kernel |
| `rpc.statd is not running but is required for remote locking` | NFSv3 without `nolock` | Add `nolock` (the default options include it) |
| `mount: permission denied` from the container | The container can't mount | Keep `cap_add: SYS_ADMIN` and `apparmor:unconfined`, or use `privileged: true`, or mount on the host |
| `OSError: [Errno 116] Stale file handle` on SMB, every run | The SMB mount lacks `noserverino` | Remount; the defaults include it. Check the share's own options |
| `Stale file handle` on NFS, every run | Attribute caching is turned off | Remove `actimeo=0` / `noac` from the share's options |
| `Stale file handle` after the computer slept or the network changed | The mount dropped | **Remount** on the Shares page. Runs remount and retry once by themselves |
| Shares page says **Mounted, not Hammerspace** | The path isn't a direct Hammerspace mount | Mount the Hammerspace share itself, not a copy or re-export |
| `/path doesn't exist on <share>` | A folder in the report was moved or removed | Edit the report's folders |

The verbose mount output is shown in the error message, and each run's **What ran** section
has the exact `hs` command, its output and its error text.

## Running on a Mac

Docker on macOS runs containers in a small Linux virtual machine (Docker Desktop or Colima).
A few things work differently there:

- **Install Docker Compose** if `docker compose up -d` fails with
  `unknown shorthand flag: 'd' in -d`. With Homebrew:

  ```bash
  brew install docker-compose
  mkdir -p ~/.docker
  echo '{"cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]}' > ~/.docker/config.json
  ```

  (On Intel Macs the path is `/usr/local/lib/docker/cli-plugins`. Merge with an existing
  `config.json` rather than replacing it.)

- **Start the Docker engine** if you see
  `failed to connect to the docker API at unix:///var/run/docker.sock`. Open Docker Desktop,
  or with Colima:

  ```bash
  brew install colima
  colima start
  docker context use colima
  ```

  Colima stops when the Mac restarts; `brew services start colima` starts it at login.

- **Use NFSv3 or SMB.** The VM's kernel isn't approved for NFS 4.2.
- **Allow non-privileged ports** on NFS exports, since the VM's traffic leaves through the
  Mac with its source ports rewritten.
- **Let the container mount shares.** Shares mounted in macOS Finder aren't usable by hstk
  inside the container.
- **Expect stale mounts after sleep.** Use **Remount**, or let the next run remount on its own.

## Checking a mount by hand

To test a mount and hstk outside the GUI:

```bash
docker run --rm --privileged --entrypoint sh hs-reporter:latest -c '
  mkdir -p /mnt/t
  mount -v -t nfs -o vers=3,nolock SERVER:/EXPORT /mnt/t &&
  hs eval -e SPACE_USED /mnt/t && umount /mnt/t'
```

For SMB, use `mount -t cifs -o username=USER,password=PASS,vers=3.0,noserverino //SERVER/SHARE /mnt/t`.
