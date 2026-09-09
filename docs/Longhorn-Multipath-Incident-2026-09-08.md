# Longhorn Multipath Incident — September 8, 2026

## Summary

A Longhorn-backed Pi-hole volume could not be formatted or mounted because the Ubuntu node's `multipath` subsystem had claimed the Longhorn iSCSI block device before Longhorn CSI could use it.

The affected Pi-hole pod remained in `ContainerCreating` even though the PVC was bound and the Longhorn volume was attached.

The decisive kubelet error was:

```text
MountVolume.MountDevice failed for volume "pvc-7d309d18-dc25-4ac7-97f5-6eeeb8d00faf"
...
/dev/longhorn/pvc-7d309d18-dc25-4ac7-97f5-6eeeb8d00faf is apparently in use by the system; will not make a filesystem here!
```

This was the same failure mode previously observed with the MoniKey Postgres Longhorn volume.

## Impact

Affected workload:

```text
Namespace: dns
Deployment: pihole-dns
PVC: pihole-dns
Longhorn volume: pvc-7d309d18-dc25-4ac7-97f5-6eeeb8d00faf
Size: 5 GiB
```

Observed pod state:

```text
0/1   ContainerCreating
```

The Pi-hole container never started because kubelet could not complete the Longhorn CSI mount operation.

## Root Cause

Longhorn exposed the PVC through iSCSI as a raw block device:

```text
/dev/longhorn/pvc-7d309d18-dc25-4ac7-97f5-6eeeb8d00faf
  -> /dev/sdg
```

The host's multipath subsystem then claimed `/dev/sdg` and created:

```text
mpatha (360000000000000000e00000000010001) dm-0 IET,VIRTUAL-DISK
size=5.0G
`- sdg 8:96 active ready running
```

The device reported:

```text
ID_VENDOR=IET
ID_MODEL=VIRTUAL-DISK
```

`lsblk` showed the conflicting relationship:

```text
sdg  disk  5G  mpath_
└─mpatha
     mpath 5G
```

Because `/dev/sdg` had an active device-mapper holder, Longhorn CSI's `mkfs.ext4` operation refused to format the volume:

```text
is apparently in use by the system
```

The failure path was:

```text
Longhorn attaches volume through iSCSI
  -> Linux exposes /dev/sdX
  -> multipath claims the IET VIRTUAL-DISK device
  -> /dev/mapper/mpathX is created
  -> Longhorn CSI attempts mkfs.ext4
  -> kernel reports the raw device is in use
  -> MountVolume.MountDevice fails
  -> pod remains ContainerCreating
```

## Important Service Roles

### `iscsid.service`

Longhorn requires iSCSI support. Keep it enabled and running.

```bash
sudo systemctl enable --now iscsid.service
systemctl is-active iscsid.service
```

Expected:

```text
active
```

Do **not** disable `iscsid` as part of this remediation.

### `multipathd.service`

This homelab node does not intentionally use SAN multipath storage. `multipathd` must not claim Longhorn iSCSI devices.

The node was configured with:

```bash
sudo systemctl disable --now multipathd.service
sudo systemctl mask multipathd.service
sudo systemctl mask multipathd.socket 2>/dev/null || true
```

Verification:

```bash
systemctl is-active multipathd.service
systemctl is-enabled multipathd.service
```

Expected:

```text
inactive
masked
```

## Recovery Procedure

### 1. Identify multipath claims

```bash
sudo multipath -ll
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
```

For this incident the affected mapping was:

```text
mpatha -> /dev/sdg -> pvc-7d309d18-dc25-4ac7-97f5-6eeeb8d00faf
```

A previous MoniKey Postgres incident used:

```text
mpathb -> /dev/sdh -> pvc-9d9972fe-4041-4838-9f07-49847b1755ef
```

### 2. Confirm the mapping is not mounted

Before removing a device-mapper mapping, verify that neither the raw device nor the mapper device is mounted:

```bash
findmnt /dev/sdg
findmnt /dev/mapper/mpatha
```

For this incident both commands returned no mount.

### 3. Keep iSCSI enabled

```bash
sudo systemctl enable --now iscsid.service
```

### 4. Stop and mask multipath

```bash
sudo systemctl disable --now multipathd.service
sudo systemctl mask multipathd.service
sudo systemctl mask multipathd.socket 2>/dev/null || true
```

### 5. Remove only the stale affected mapping

```bash
sudo multipath -f mpatha
sudo udevadm settle
```

Do **not** run:

```bash
sudo multipath -F
```

`-F` attempts to flush every multipath map on the host and is unsafe when other storage may be active.

Also do not use forced device-mapper removal unless the storage state has been fully investigated:

```text
dmsetup remove --force
```

### 6. Verify the stale mapper is gone

```bash
sudo multipath -ll
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
ls -l /sys/class/block/sdg/holders/
sudo dmsetup ls --tree
```

After the successful cleanup, `mpatha` no longer existed beneath `/dev/sdg`.

`lsblk` could still display `mpath_` as the detected filesystem/signature type on the raw block device. That display alone is not proof of an active multipath claim. The important checks are that:

- there is no `mpatha` child under `/dev/sdg`;
- `/sys/class/block/sdg/holders/` does not contain the stale mapper;
- `dmsetup` no longer shows the mapping;
- `multipath -ll` no longer shows `mpatha`.

Do not wipe or manually format the device based only on the `mpath_` text shown by `lsblk`.

### 7. Recreate the affected workload pod

```bash
sudo kubectl -n dns delete pod -l app=pihole
sudo kubectl -n dns get pods -w
```

Expected progression:

```text
ContainerCreating
Running
```

The Pi-hole pod successfully reached `Running` after the stale `mpatha` mapping was removed.

## Verification

Verify Pi-hole:

```bash
sudo kubectl -n dns get pods
sudo kubectl -n dns get pvc
sudo kubectl -n dns describe pod -l app=pihole
```

There should be no new event containing:

```text
is apparently in use by the system
```

Verify Longhorn/iSCSI host state:

```bash
systemctl is-active iscsid.service
systemctl is-active multipathd.service
systemctl is-enabled multipathd.service
sudo multipath -ll
```

Expected host state:

```text
iscsid.service:     active
multipathd.service: inactive
multipathd.service: masked
```

## Known-Good Evidence After Recovery

The previously affected MoniKey Postgres volume was observed mounted normally after multipath was disabled:

```text
/dev/sdh
  -> Longhorn CSI globalmount
  -> postgres pod mount
```

The Pi-hole volume then recovered after its stale `mpatha` mapping was flushed and the pod was recreated.

This confirms that the Longhorn volumes themselves were healthy; the failure was caused by the host device-mapper ownership conflict.

## Guardrails

Do not perform any of the following as a first response to this incident:

- delete the PVC;
- delete the Longhorn volume;
- run `mkfs` manually on `/dev/longhorn/*` or `/dev/sdX`;
- disable or stop `iscsid`;
- run `multipath -F` on a node with unknown storage state;
- force-remove device-mapper mappings without confirming they are unused.

The correct recovery is to remove only the stale multipath mapping after confirming it is not mounted, while keeping the Longhorn iSCSI path intact.

## Fresh-Install Prevention

This incident is a host prerequisite problem, not an application-manifest problem. A fresh K3s/Longhorn node should ensure the following before Longhorn workloads are deployed:

```text
open-iscsi installed
  -> iscsid enabled
  -> multipathd disabled/masked when SAN multipath is not required
  -> K3s installed
  -> Argo CD installed
  -> Longhorn reconciled
```

The bootstrap should eventually enforce or validate this host state so a clean Git clone + bootstrap cannot reproduce the issue.

## Additional Host Warning Observed

During service changes, systemd printed:

```text
Failed to allocate directory watch: Too many open files
```

This warning did not prevent the required service states from being applied (`iscsid` remained active and `multipathd` remained masked), but it should be investigated separately as a host resource/inotify limit issue.

It was not the root cause of the Longhorn mount failure.
