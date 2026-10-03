# MacBookPro11,3 (A1398, GT 750M) on Linux — fixes for the 800 MHz CPU clamp, input lag and screen glitches

Field notes from making a **MacBookPro11,3 (15" Retina, GT 750M, Apple GMUX)** run properly on
Linux. Everything here was diagnosed with hard evidence on real hardware and verified after
reboot. If your dual-GPU MacBook pins the CPU at 800 MHz, lags on input, or glitches when the
discrete GPU drives external monitors, this may save you weeks.

## The machine

| | |
|---|---|
| Model | MacBookPro11,3 (A1398), i7-4870HQ, Iris Pro 5200 + GeForce GT 750M (GMUX), 85 W MagSafe 2 |
| OS | CachyOS (Arch-based), kernel `7.2.8-cachyos`, `intel_pstate=disable` (acpi-cpufreq + ondemand) |
| Compositor | Sway (wlroots): iGPU is primary (internal panel), nouveau is a secondary KMS device for the external DP outputs (`WLR_DRM_DEVICES=/dev/dri/intel-igpu:/dev/dri/nouveau-gpu`) |
| Boot | GRUB → `apple_set_os.efi` → Unified Kernel Image (UKI) with the cmdline embedded |

## TL;DR — the five fixes

| Symptom | Root cause | Fix |
|---|---|---|
| CPU pinned to ~800 MHz when external monitors are connected | The SMC writes the CPU limit in Apple's 30-state `APSS` index space; Linux applies it against the 10-state `_PSS` table | `processor.ignore_ppc=1` |
| Input lag / stutter | nouveau grabs DRM master at boot while `force_igd=1` splits the GMUX → i915 page flips hang | `nouveau.modeset=0` at boot + load nouveau with KMS after boot |
| Screen glitches/flicker while the dGPU is active | GPU pstate ping-pong (0a↔07 every 2 s) → encoder retrain on every change | Make the pstate floor match the baseline (reclocked `[caps]`) |
| mpv burns a full CPU core | `--vo=wlshm` upscales the video to the window size **on the CPU** (swscale) | `--sws-scaler=bilinear` + `fps=10` |
| Periodic input lag (every ~6 s) | CPU turbo → 90 °C → thermal guard asserts BD PROCHOT → 800 MHz for ~5 s → repeat | Disable turbo (sustained 2.5 GHz beats the 3.5/0.8 cycle) |

---

## 1. The 800 MHz CPU clamp (the big one)

### Symptom
With external monitors connected (dGPU active), the CPU drops to ~800 MHz under load and stays
there. Without monitors (iGPU only) it runs at 2.5–3.5 GHz.

### What it is NOT (all measured)
- **BD PROCHOT** (MSR `0x1FC` bit 0): cleared on all cores, CPU stays at 800. Not it.
- **Power deficit**: the 85 W adapter delivered ~45 W (19.65 V × 2.29 A); the battery was
  full/healthy (SMP, 6 cycles, 100 %). Not it.
- **Thermal**: temps were 66–82 °C. Not it.
- **A hardware cap**: see the P-state proof below. Not it.
- **NVRAM**: `BootCampProcessorPstates=9` is a Boot Camp leftover (values vary across Macs:
  9/11/15 — it is not a `_PSS` index). Not it.

### Root cause: an APSS vs _PSS index-space mismatch

The DSDT exposes a PLMT I/O region:

```asl
OperationRegion (PLMT, SystemIO, 0x0310, 0x0A)
Field (PLMT, WordAcc, Lock, Preserve)
{
    CPLT,   8,   // 0x310 — CPU Performance Limit (what _PPC returns)
    IGPS,   8,   // 0x311
    MPLT,   8,   // 0x312
    CFIL,   8,   // 0x313
    EGPS,   8    // 0x314 — External GPU P-state
}
```

`_PPC` simply returns `CPLT`, which the SMC writes. The catch:

- The SMC uses **APSS** (Apple's 30-state table, turbo included: `APSS[n] = 3700 − 100·n` MHz).
- Linux's `acpi-cpufreq` applies `_PPC` against **`_PSS`** (10 states: `0=2501 … 9=800` MHz).

| SMC writes `CPLT` | SMC intent (APSS) | Linux applies (_PSS) |
|---|---|---|
| 0 | 3700 (no cap) | no limit |
| 9 | 2800 | **800** |
| ≥ 10 | 2700–1200 | **ignored** — kernel does `if (ppc >= state_count) return 0;`, so the previous (stale) limit sticks |

Live proof (ports read via `/dev/port`):

```
CPLT oscillates 8..25 while bios_limit stays 800000
```

The kernel only "understands" values < 10 and misapplies them. When the SMC wants to cap at
2800 MHz it writes 9, and Linux clamps to `_PSS[9]` = 800 MHz.

### Proof the hardware is fine

Hold a 2.5 GHz P-state request (`PERF_CTL = 0x1900`, i.e. `ratio 25 << 8`) in a loop, under
load, with all monitors connected:

```
 t    ratio   MHz   PERF_STATUS  bios_limit  temp   power
10s  1.000  2500     0x1900       800000     77C   30.9W
25s  1.000  2500     0x1900       800000     82C   32.3W
```

2.5 GHz sustained for 30 s. The hardware does not block it; only the `_PPC`-derived QoS does.

> **Trap:** the IA32_PERF_CTL encoding is `ratio << 8`. `0x0` is the **minimum** (ratio 0),
> not "P0". A test that writes `0x0`, sees P8 and concludes "hardware cap" is wrong.
> 2.5 GHz = `0x1900`, 3.7 GHz = `0x2500`.

### Fix

```
processor.ignore_ppc=1
```

on the kernel cmdline. The kernel then ignores `_PPC`/`CPLT` entirely (it is a software
ceiling; the SMC keeps writing `CPLT` but it has no effect). Verified: `bios_limit = 2501000`
and the CPU runs at 2.5 GHz+ under load with the monitors connected.

For a UKI-based boot, add it to the cmdline file and rebuild the UKI (see `scripts/`).

---

## 2. Input lag — nouveau taking DRM master at boot vs the GMUX

### Symptom
Persistent input lag (sticky mouse/typing) while the external monitors are active.

### Root cause
On this machine the external DP/HDMI outputs are wired to the GT 750M, so nouveau must drive
them. But if nouveau initialises KMS **during boot** while `apple-gmux.force_igd=1` is active,
it splits the GMUX and the i915 page-flip path hangs:

- `kworker/i915_flip` stuck in `D` state (`i915_request_wait_timeout` /
  `drm_atomic_helper_wait_for_flip_done`),
- `i915_engine_info` shows `hungE … @ 60ms: sway` on `rcs0`,
- the compositor's main loop waits → input lag.

### Fix
1. `nouveau.modeset=0` on the cmdline → nouveau loads at boot **without** KMS (no DRM master,
   no GMUX split).
2. A systemd unit (`Before=display-manager.service`) reloads nouveau **with** KMS after boot:

```
modprobe -r nouveau 2>/dev/null || true
modprobe nouveau modeset=1
```

Verified: `hungE` dropped from 61 ms to 5–9 ms (within a frame budget).

---

## 3. Screen glitches — the GPU pstate ping-pong

### Symptom
Screen gets glitchy/flickery when an app that keeps the dGPU busy (e.g. an mpv window on the
external monitor) is focused.

### Root cause
The `reclocked` daemon's state machine oscillated between two pstates every 2 s:

```
deep_idle -> active  (WAKE-ACTIVITY)
active -> deep_idle  (CEILING)
```

because the **ceiling** (the configured baseline, `07`) was *below* the **floor** (a per-app
`[caps]` floor set to `0a`). Every pstate change with an active CRTC retrains the encoder →
visible glitch.

### Fix
Make the floor match the baseline. In `reclocked.conf`:

```ini
[caps]
mpv = floor=07, max=0e, busy-up=50
```

Verified: pstate stable at `07` with mpv focused, zero state transitions in the log.

> If you change `[dgpu-active] baseline`, audit every `[caps] floor` for values above it.

---

## 4. mpv / camera wall eats a CPU core

### Symptom
Opening the 6-camera RTSP wall spikes CPU usage massively.

### Root cause
`--vo=wlshm` upscales the composited video to the window size **on the CPU** with libswscale
(the default scaler is bicubic). Measured on a stable 2.5 GHz CPU:

| Variant | mpv CPU |
|---|---|
| baseline (`wlshm`) | 20 % |
| `--sws-scaler=fast-bilinear` | 20 % |
| `--sws-scaler=bilinear` | **13 %** |
| `fps=10` in the lavfi | **12 %** |
| `fps=10` + `bilinear` | 14 % |

> Measuring CPU % while the CPU is thermally clamped inflates everything (~3× at 800 MHz).
> Always compare at a stable frequency.

### Fix
`--sws-scaler=bilinear` and `fps=10` on the lavfi output (the cameras are 10 fps anyway).

> `--vo=gpu` moves the scaling to the GPU (package power stayed ~equal), it does not remove
> the work. If you want to keep it on the CPU, use the options above.

---

## 5. Periodic input lag — turbo vs the thermal guard

### Symptom
Input lag that comes and goes every few seconds.

### Root cause
With turbo enabled, a CPU-heavy workload cycles every ~6 s:

```
3.5 GHz → 90 °C → thermal guard asserts BD PROCHOT → 800 MHz for ~5 s → cools to 77 °C
→ guard clears → 3.5 GHz → repeat
```

During the 800 MHz window the compositor stutters → input lag.

### Fix
Disable turbo (sustained 2.5 GHz is *faster on average* than the 3.5/0.8 cycle, and ~10 °C
cooler):

```
echo 0 > /sys/devices/system/cpu/cpufreq/boost
```

persisted with a oneshot systemd unit (see `scripts/cpu-no-turbo.service`).

Verified: 2.5 GHz flat, 80–84 °C, zero thermal asserts.

---

## Diagnostic cheat sheet

### Intel i915 (Iris Pro) — PCI `0000:00:02.0`

| What | Command |
|---|---|
| Kernel log | `sudo dmesg \| grep -i i915` |
| Hung requests | `sudo grep hungE /sys/kernel/debug/dri/0000:00:02.0/i915_engine_info` |
| Error state (after a hang) | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/i915_error_state` |
| Display resets | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/intel_display_reset_count` |
| Power state | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/i915_runtime_pm_status` |
| Live usage | `sudo intel_gpu_top` |

### NVIDIA nouveau (GT 750M) — PCI `0000:01:00.0`

| What | Command |
|---|---|
| Kernel log | `sudo dmesg \| grep -i nouveau` |
| Current pstate | `sudo grep '\*' /sys/kernel/debug/dri/0000:01:00.0/pstate` |
| Clients | `sudo cat /sys/kernel/debug/dri/0000:01:00.0/clients` |
| Engine busy % | not in debugfs — read the PMU from BAR0 (or use reclocked) |

### The `_PPC` / `CPLT` value (the clamp)

```bash
# Read the PLMT I/O ports (CPLT is what _PPC returns)
sudo python3 -c "
with open('/dev/port','rb') as f:
    for name, port in [('CPLT',0x310),('IGPS',0x311),('MPLT',0x312),('CFIL',0x313),('EGPS',0x314)]:
        f.seek(port); print(name, '=', f.read(1)[0])
"
# Kernel view of the applied limit
cat /sys/devices/system/cpu/cpu0/cpufreq/bios_limit
```

### BD PROCHOT (MSR 0x1FC bit 0)

```bash
sudo python3 -c "
import glob, struct
for p in sorted(glob.glob('/dev/cpu/[0-9]*/msr')):
    with open(p,'rb') as f:
        f.seek(0x1FC); v=struct.unpack('<Q',f.read(8))[0]
    print(p, 'bit0=', v&1)
"
```

### Real system power (SMC DC-in)

```bash
# iD0R (current) and vD0R (voltage) are SMC floats, read by key index.
# Indices are stable per machine (discover once by iterating key_at_index).
sudo python3 -c "
import struct, time
def rd(i):
    open('/sys/devices/platform/applesmc.768/key_at_index','w').write(str(i))
    d=open('/sys/devices/platform/applesmc.768/key_at_index_data','rb').read()
    return struct.unpack('<f', d[:4])[0]
for _ in range(5):
    a=rd(638); v=rd(654)   # iD0R, vD0R on a MacBookPro11,3
    print(f'{v:.2f}V x {a:.3f}A = {v*a:.1f}W')
    time.sleep(1)
"
```

---

## Files in this repo

```
docs/DIAGNOSTICO-CLAMP-800MHZ.md   Full Spanish write-up with all the raw evidence
scripts/cpu-no-turbo.service       Disable turbo (fix #5)
scripts/retinaforge-gmux-nouveau.service   Late nouveau KMS load (fix #2)
scripts/cpu-bdprochot-guard        Thermal BD PROCHOT guard (thermal safety)
scripts/smc-dc-power.py            Read real system power from the SMC
```

## If you keep more than one kernel (main + LTS)

Apply every cmdline/UKI change to **all** installed kernels. A stale second UKI means
booting it brings back the 800 MHz clamp and the GMUX lag. On this machine each kernel has
its own UKI and pacman hook:

- main: `cachyos_intel_linux.efi` + `post-kernel-package.hook`
- LTS: `cachyos_intel_linux_lts.efi` + `post-kernel-lts.hook`

Both hooks call the same orchestrator with a kernel selector (`post-kernel-update.sh [main|lts]`),
and `apply-patched-nouveau.sh [kver]` / `rebuild-intel-uki.sh [kver]` pick the right source tree
and UKI by kernel version. Verify **both** after any change:

```bash
for u in cachyos_intel_linux.efi cachyos_intel_linux_lts.efi; do
  printf "%-30s: " "$u"
  sudo objcopy --dump-section .cmdline=/tmp/c.txt /boot/efi/EFI/Linux/$u && \
    tr '\0' '\n' < /tmp/c.txt | grep -oE "processor.ignore_ppc=1|nouveau.modeset=0" | tr '\n' ' '
  echo
done
# both must show: processor.ignore_ppc=1 nouveau.modeset=0
```

## References

- Linux: `drivers/acpi/processor_perflib.c` (`acpi_processor_ppc_notifier`,
  `acpi_processor_get_bios_limit`), `drivers/cpufreq/acpi-cpufreq.c`.
- ACPI: DSDT `PLMT` (SystemIO 0x310), SSDT `_PPC`/`_PSS`/`APSS` (Apple tables).
- [erikberglund/AppleNVRAM](https://github.com/erikberglund/AppleNVRAM) — Apple NVRAM variables.
- [tinyapps.org — MacBooks without batteries severely throttled](https://tinyapps.org/blog/201811150700_macbook_slow_no_battery.html)
  (a different, real mechanism — ruled out here because the battery was healthy).
- [retinaforge](https://github.com/mmdmcy/retinaforge) and the `reclocked` daemon for dual-GPU
  MacBook power management.

## License

MIT — use it, share it, improve it. If it saves your MacBook, pass it on.
