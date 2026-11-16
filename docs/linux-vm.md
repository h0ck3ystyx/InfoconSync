# Linux VM on Proxmox

This deployment runs directly on a Debian 13 or Ubuntu 24.04 LTS VM, as one systemd
service. It listens on **0.0.0.0:8080** and uses password login. Use the browser UI to check upstream,
review plans, and update the NAS mirror. The existing `infocon-librarian` CLI check,
plan, sync, verify, and receipts commands remain placeholders and must not be used
for scheduled synchronization.

## 1. Create the VM

Start with 2 vCPUs, 4 GB RAM and a 32 GB local system disk. Bridge the VM onto your
LAN and reserve its IP in DHCP (examples below use `192.168.1.50`). Install Debian
13 or Ubuntu 24.04 LTS, SSH for administration, and the Proxmox guest agent if
desired. Archive media will live on the NAS; the VM disk holds Python, SQLite, logs and resume metadata.

Use the VM's local disk for `/var/lib/infocon-librarian`. The database uses SQLite
WAL and must not be on NFS or SMB. Backups of the VM do not back up the NAS mirror.

## 2. Mount the NAS inside the guest

Pick **one** protocol. Replace the sample server/share names with your own. Do not
mount this share on the Proxmox host for this deployment.

NFS example:

```bash
sudo apt-get update
sudo apt-get install -y nfs-common git
sudo mkdir -p /mnt/infocon
```

Add an `/etc/fstab` entry:

```fstab
192.168.1.10:/volume1/infocon /mnt/infocon nfs4 rw,hard,_netdev,nofail 0 0
```

For SMB instead, install `cifs-utils` and use a root-owned, mode `0600` credentials
file at `/etc/infocon-nas.credentials` containing `username=...` and `password=...`
on separate lines. After installing the application in step 3, find its numeric
UID/GID with `id infocon` and substitute them below:

```fstab
//192.168.1.10/infocon /mnt/infocon cifs credentials=/etc/infocon-nas.credentials,vers=3.1.1,uid=APP_UID,gid=APP_GID,file_mode=0600,dir_mode=0700,_netdev,nofail 0 0
```

Mount and inspect it:

```bash
sudo mount /mnt/infocon
findmnt --mountpoint /mnt/infocon -o TARGET,SOURCE,FSTYPE,OPTIONS
```

For NFS, grant the service user's numeric UID/GID write access in your NAS's export
and ACL configuration. Do not recursively change ownership of an existing mirror.
For SMB, both NAS account permissions and the guest's UID/GID mapping must permit
writes. The service never runs as root.

Keep the underlying **unmounted** directory owned by root and not writable by the
service user. Do not change ownership of that directory to the application user.
The installer establishes this when the share is not mounted.

## 3. Install the application

```bash
git clone https://github.com/h0ck3ystyx/InfoconSync.git
cd InfoconSync
sudo bash scripts/install-linux.sh
```

The installer supports Debian 13 and Ubuntu 24.04 LTS. It installs the distribution's
`python3-libtorrent` and creates an isolated release venv using system Python with `--system-site-packages`, installs the package with
tested runtime constraints, and verifies packaged UI assets. It creates:

- `/opt/infocon-librarian/releases/<release>` and a `current` symlink.
- Dedicated system user/group `infocon`.
- `/etc/infocon-librarian/server.toml` (root-owned; readable by the service).
- `/var/lib/infocon-librarian` (owned by the service; mode 0700).
- `/etc/systemd/system/infocon-librarian.service`.

It does **not** mount shares, initialize an archive, or start/enable the service.
It preserves existing configuration and service units on subsequent installs.

On Ubuntu, `python3-libtorrent` and `python3-venv` require the **Universe**
repository. If the installer reports it is unavailable, run
`sudo add-apt-repository universe` and retry. The installer uses `/usr/bin/python3`
so the venv matches the distribution's native libtorrent bindings, even if another
Python is installed in `/usr/local/bin`.

## 4. Configure the service and archive identity

```bash
/opt/infocon-librarian/current/bin/infocon-server archive-id
sudoedit /etc/infocon-librarian/server.toml
```

Set `archive_id` to the generated UUID. Set `archive_root`, `mount_path` and the
exact `mount_source` reported by `findmnt`. `mount_path` is the actual mount point;
`archive_root` can be a subdirectory within it. Set `allowed_hosts` to your VM's
actual IPv4 address and the hostnames you will use in the browser. Do not include
schemes, ports, wildcards, or `0.0.0.0` in `allowed_hosts`.

If you change the mount path, also update `RequiresMountsFor=` and `ReadWritePaths=`
in the systemd unit with `sudo systemctl edit --full infocon-librarian`.

Verify access and deliberately initialize the marker on the mounted archive:

```bash
sudo -u infocon test -w /mnt/infocon
sudo -u infocon /opt/infocon-librarian/current/bin/infocon-server init-archive
sudo -u infocon /opt/infocon-librarian/current/bin/infocon-server set-password
```

`init-archive` checks the configured mount/source and writes only
`.infocon-archive-id`. It refuses to overwrite an existing marker. Do not regenerate
or overwrite an existing identity during upgrades. The password command prompts
without echoing or putting passwords on the command line; it stores only a hash
and a session signing key in `/var/lib/infocon-librarian/auth.json` (mode 0600).

The session key persists across restarts. Password resets rotate it, invalidating
old sessions after the service restarts. Login is rate limited, mutation requests
require CSRF and same-origin checks, and unexpected Host headers are rejected.

## 5. Restrict LAN access and start

Allow inbound TCP 8080 from your trusted LAN subnet using the Proxmox VM firewall
or the guest firewall. Preserve administrative SSH access. Do not configure router
port forwarding for the UI. HTTP sends credentials and sessions without encryption;
this configuration is intended for your trusted LAN.

The engine listens separately on TCP/UDP 6881. Permit those ports if you want
inbound torrent peers; outgoing tracker, peer, HTTPS and DNS connections are also
needed. DHT/PEX/LSD remain disabled. No public router port forward is required to
try outgoing peer connections.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now infocon-librarian
sudo systemctl status infocon-librarian
sudo journalctl -u infocon-librarian -n 50 --no-pager
```

Open `http://192.168.1.50:8080`, sign in, and confirm the archive path/free space.
Start with one small collection. Check its destination and receipt before doing a
large update. Sign out using the UI control. Sessions expire after 12 hours of
inactivity.

## NAS failures and restarts

Startup requires an exact mounted source and matching archive marker. The service
checks them periodically and before API mutations and archive transfer operations.
If a check fails, it pauses the torrent session, interrupts workers, rejects new API
mutations with 503, and stays available to show degraded health. Partial data stays
on disk. Returning connectivity does not automatically resume work.

After restoring and checking the expected mount:

```bash
findmnt --mountpoint /mnt/infocon -o TARGET,SOURCE,FSTYPE
sudo systemctl restart infocon-librarian
```

Restart the interrupted plan from the UI. Completed plan items remain complete.
Torrent resume state is saved on interruption to the VM's local `jobs` directory;
if it cannot be saved, the next attempt rechecks existing files. HTTP keeps `.part`
and `.part.meta` files on the NAS; up to the last 4 MiB of unrecorded progress may
be fetched again, and a server that ignores Range requires a fresh download.

Monitoring cannot cancel kernel I/O already blocked in an NFS/SMB outage. Hard NFS
mounts favor data integrity and may remain blocked until the server returns.
Mount/source checks and the marker are safeguards, not an atomic guarantee against
an administrator replacing/unmounting a filesystem during an active write. Stop
the service before changing mounts; never force/lazy-unmount an active archive.
Systemd allows 90 seconds to stop before terminating remaining work; startup
recovery also handles a forced termination. A NAS fault latches work off until a
service restart, so inspect the mount before restarting.

## Updates, backup and rollback

Stop the application before taking a state backup or changing releases:

```bash
sudo systemctl stop infocon-librarian
sudo tar -C / -czf /root/infocon-state-backup.tar.gz \
  var/lib/infocon-librarian etc/infocon-librarian
# From your source checkout:
git pull --ff-only
sudo bash scripts/install-linux.sh
sudo systemctl start infocon-librarian
```

The backup includes authentication secrets; retain root-only access and protect
external copies. A stopped-service backup captures SQLite together with any WAL
files consistently. Schedule VM backups independently if desired; automatic mirror
updates are not part of this release.

The prior release path is saved in `/etc/infocon-librarian/previous-release`. To
roll back, stop the service, restore the matching state/config backup if schema
changes require it, repoint `current` to that recorded release, and start it again:

```bash
sudo systemctl stop infocon-librarian
previous=$(sudo cat /etc/infocon-librarian/previous-release)
sudo ln -s "$previous" /opt/infocon-librarian/.rollback
sudo mv -Tf /opt/infocon-librarian/.rollback /opt/infocon-librarian/current
sudo systemctl start infocon-librarian
```

To reset the password, stop the service, run the `set-password` command from step
4, and start it again. No archive data is removed by updates, rollback, password
resets, or uninstalling the service.

## Developer validation

```bash
python -m pytest tests/unit tests/integration tests/engine -q
python -m pytest tests/e2e -q
bash -n scripts/install-linux.sh
# Clean Linux environment for package/native-binding checks, not production hosting:
docker build -f deploy/linux/smoke.Dockerfile -t infocon-linux-smoke .
docker run --rm infocon-linux-smoke
# Ubuntu 24.04 uses the same test image definition:
docker build --build-arg BASE_IMAGE=ubuntu:24.04 \
  -f deploy/linux/smoke.Dockerfile -t infocon-ubuntu-smoke .
docker run --rm infocon-ubuntu-smoke
docker run --rm --mount type=volume,dst=/archive infocon-linux-smoke \
  python scripts/smoke-linux-service.py
```

Before first production use, test reboot/mount ordering on the actual VM, access
from another LAN machine, a small real transfer, NAS connectivity loss, and service
restart during both transfer methods. Container smoke tests do not substitute for
those NAS/protocol and systemd boot tests.
