# Diagnóstico: CPU clavado a ~800 MHz con monitores externos

**Máquina:** MacBookPro11,3 (A1398) con GT 750M mod — CachyOS, kernel `7.2.8-2-cachyos`
**Sesión:** Sway (iGPU primaria + dGPU KMS secundaria para DP-1/DP-2)
**Fecha:** 2026-10-03
**Estado:** ✅ **RESUELTO (2026-10-03)** — clamp de CPU, input lag y glitch de pantalla corregidos y verificados en vivo. Fixes: `processor.ignore_ppc=1` (§7), nouveau KMS tardío (lag GMUX), `[caps] mpv floor=07` (churn/glitch), `--sws-scaler=bilinear` + `fps=10` (consumo mpv), `cpu-no-turbo.service` (input lag térmico).

---

## 1. Resumen ejecutivo

Con los monitores externos conectados (dGPU activa), el CPU i7-4870HQ queda limitado
dinámicamente a ~800 MHz bajo carga sostenida. Sin monitores (solo iGPU), el CPU corre
libre a 2.5–3.5 GHz.

**Causa raíz:** un **mismatch de espacios de índices** entre la tabla `APSS`
(Apple, 30 estados, con turbo — la que usa el SMC/macOS) y la tabla `_PSS`
(estándar, 10 estados — la que usa Linux). El SMC escribe el límite del CPU
(`CPLT`, I/O port 0x310) como **índice de APSS**; el kernel Linux lo aplica contra
`_PSS`. Ejemplo: el SMC pide "2800 MHz" (APSS índice 9) y Linux lo interpreta como
"_PSS índice 9" = **800 MHz**.

**Fix:** `processor.ignore_ppc=1` en el cmdline del kernel (ya embebido en el UKI).
Hace que el kernel ignore el `_PPC` por completo. Probado: el hardware banca
**2.5 GHz sostenidos** con los 3 monitores.

---

## 2. El síntoma

| Estado | CPU |
|---|---|
| Sin monitores (solo iGPU) | Libre: 2.5–3.5 GHz |
| Con monitores (dGPU activa) | **798–800 MHz** bajo carga sostenida |
| Con monitores, en reposo/bursts | Hasta ~1.9 GHz (dinámico) |

- El clamp es **dinámico**: el SMC lo ajusta en vivo según su política de energía.
- Los % de CPU se ven "por los cielos" porque todo tarda ~3× más a 800 MHz.
- La dGPU en pstate `0a` agrava el latch; con `baseline=07` el SMC relaja más.

---

## 3. La cadena del mecanismo

```
SMC (firmware)
 └─ escribe CPLT (I/O port 0x310)          ← índice en espacio APSS (0–29)
     └─ ACPI _PPC (ssdt3) devuelve CPLT
         └─ kernel: acpi_processor_ppc_notifier (processor_perflib.c)
             └─ freq_qos_update_request(FREQ_QOS_MAX, ...)
                 └─ scaling_max_freq / bios_limit (acpi-cpufreq)
                     └─ governor ondemand escribe PERF_CTL (MSR 0x199)
```

### El registro PLMT (DSDT, línea ~6826)

```asl
OperationRegion (PLMT, SystemIO, 0x0310, 0x0A)
Field (PLMT, WordAcc, Lock, Preserve)
{
    CPLT,   8,   // 0x310 — CPU Performance Limit (lo que devuelve _PPC)
    IGPS,   8,   // 0x311 — iGPU P-state
    MPLT,   8,   // 0x312 — Memory P-state Limit
    CFIL,   8,   // 0x313
    EGPS,   8    // 0x314 — External GPU P-state
}
```

### El método _PPC (ssdt3)

```asl
Method (_PPC, 0, NotSerialized)
{
    Local0 = CPLT /* External reference */
    Return (Local0)
}
```

---

## 4. La causa raíz: mismatch APSS vs _PSS

### `_PSS` (10 estados) — la que usa Linux

| índice | freq (MHz) |
|---|---|
| 0 | 2501 |
| 1 | 2500 |
| 2 | 2300 |
| 3 | 2100 |
| 4 | 1900 |
| 5 | 1600 |
| 6 | 1400 |
| 7 | 1200 |
| 8 | 1000 |
| 9 | **800** |

### `APSS` (30 estados, con turbo) — la que usa macOS/SMC

`APSS[n] = 3700 − 100·n` MHz

| índice | freq | índice | freq | índice | freq |
|---|---|---|---|---|---|
| 0 | 3700 | 10 | 2700 | 21 | 1600 |
| 4 | 3300 | 12 | 2500 | 25 | 1200 |
| 8 | 2900 | 16 | 2100 | 29 | 800 |
| 9 | **2800** | 19 | 1800 | | |

### El código del kernel (`drivers/acpi/processor_perflib.c`)

```c
index = ppc;

if (pr->performance_platform_limit == index ||
    ppc >= pr->performance->state_count)
        return 0;          // ← si _PPC >= 10: IGNORADO (queda el límite viejo)

pr->performance_platform_limit = index;

if (index == 0)
        qos_value = FREQ_QOS_MAX_DEFAULT_VALUE;   // "sin límite"
else
        qos_value = pr->performance->states[index].core_frequency * 1000;
```

### Resultado del mismatch

| SMC escribe CPLT | Intención (APSS) | Linux aplica (_PSS) | Efecto |
|---|---|---|---|
| 0 | 3700 (sin cap) | sin límite | CPU libre |
| 9 | 2800 | **800** | **clamp** |
| 8 | 2900 | 1000 | clamp |
| ≥ 10 | 2700–1200 | **ignorado** | queda el límite anterior pegado |

**El SMC nunca puede "soltar" el clamp con valores ≥ 10** — el kernel los descarta y
el último límite válido (típicamente 800) persiste. Solo CPLT=0 (que el SMC usa
cuando la dGPU está apagada) libera el CPU.

---

## 5. Evidencia (tests realizados)

### 5.1 BD PROCHOT descartado

Con el clamp activo, MSR `0x1FC` bit0 = **0** en los 8 cores (limpio).
No es BD PROCHOT; el guard térmico no tiene nada que ver.

### 5.2 El hardware NO bloquea — test P0 (MSR 0x199)

Con el governor en `userspace`, se mantuvo escrito `PERF_CTL = 0x1900`
(ratio 25 = 2.5 GHz) en loop durante 30 s con 4 hilos de carga:

```
 t    ratio   MHz   PERF_STATUS  bios_limit  temp   power
 5s  0.982  2455     0x1900       800000     76C   30.3W
10s  1.000  2500     0x1900       800000     77C   30.9W
15s  0.993  2482     0x1900       800000     79C   31.4W
20s  1.007  2518     0x1900       800000     80C   32.3W
25s  1.000  2500     0x1900       800000     82C   32.3W
```

**El hardware sostiene 2.5 GHz.** El único límite es el `_PPC` aplicado por el kernel.

> ⚠️ **Corrección importante:** el test del 02/10 que concluyó "hardware puro"
> escribió `0x0` a PERF_CTL creyendo que era P0. El encoding correcto es
> `ratio << 8` — `0x0` es **ratio 0 = mínimo** (por eso quedaba en P8).
> El valor para 2.5 GHz es `0x1900`.

### 5.3 Monitoreo en vivo (CPLT vs bios_limit)

Lectura de los puertos PLMT cada 1 s (extracto):

```
 t        CPLT IGPS EGPS  bios_limit  scaling_max
12:53:28   25   16   16     800000      800000
12:54:22   10    0    0     800000      800000
12:54:23    8    0    0    1000000     1000000   ← APSS[8]=2900 → _PSS[8]=1000
12:54:24   12    0    0     800000      800000   ← ≥10 ignorado
12:54:25   19    9    8     800000      800000
12:54:32   22   13   13     800000      800000
```

El SMC oscila CPLT entre **8 y 25** (targets de 1200–2900 MHz en APSS), pero Linux
solo reacciona a los valores < 10 — malinterpretándolos.

### 5.4 No hay déficit de energía real

- Adaptador: `vD0R` = 19.65 V, `iD0R` = 2.285 A → **~45 W de 85 W** (40 W de margen)
- Batería: Full, `current_now=0`, fabricante **SMP** (genuina), **6 ciclos**, 100% salud
- CPU package a 800 MHz con carga: **18.2 W**
- Temperaturas: 66–82 °C (frío)
- `CPLT`/`bios_limit` no dependen de déficit real — es la política del SMC

### 5.5 Qué NO es (descartado con evidencia)

| Hipótesis | Prueba | Resultado |
|---|---|---|
| BD PROCHOT (MSR 0x1FC) | bit0=0 en 8 cores, sin efecto | ❌ |
| Déficit de cargador/batería | 45W/85W, batería 100% sana | ❌ |
| Límite térmico | temps 66–82 °C | ❌ |
| `ignore_ppc` mal aplicado (30/09) | ver §6 | confundido |
| Hardware hard-cap | P0 sostenido → 2.5 GHz | ❌ |
| NVRAM `BootCampProcessorPstates=9` | resto de Boot Camp; varía entre Macs (9/11/15), no es índice _PSS | ❌ |

---

## 6. Historia: qué se probó antes y por qué no cerró

| Fecha | Intento | Resultado |
|---|---|---|
| 2026-09-25 | reclocked `[profile default] max-pstate=0a`, `nvidia-pstate-cap` pin 07 | Parpadeo mitigado; clamp persiste |
| 2026-09-30 | `processor.ignore_ppc=1` en cmdline | El techo de software se liberó (2501) pero el CPU seguía en 798 — **la dGPU estaba en 0a y el guard térmico ciclaba BD PROCHOT**, confundiendo la medición |
| 2026-09-30 | Teoría "MacBook sin batería throttlea" (tinyapps) | Descartada: batería sana |
| 2026-10-02 | Test "P0 directo" (MSR 0x199 = 0x0) | Concluyó "hardware puro" — **INCORRECTO: 0x0 es el mínimo, no P0** |
| 2026-10-03 | `[dgpu-active] baseline=07` + guard v3 | Quita el latch agresivo; el SMC relaja a 1.9 en reposo |
| 2026-10-03 | Test P0 correcto (`0x1900` sostenido) | **2.5 GHz sostenidos** — el hardware no bloquea |
| 2026-10-03 | Lectura de PLMT + monitoreo | **Causa raíz confirmada: mismatch APSS/_PSS** |

---

## 7. El fix

### 7.1 `processor.ignore_ppc=1` (preparado)

- **Cmdline:** `/etc/mkinitcpio-intel-cmdline.txt`
  ```
  ... intel_pstate=disable processor.ignore_ppc=1
  ```
- **UKI reconstruido:** `/usr/local/sbin/rebuild-intel-uki.sh`
  → `/boot/efi/EFI/Linux/cachyos_intel_linux.efi`
- **Verificado** en la sección `.cmdline` del UKI (objcopy + strings)
- **Backups:** `~/.config/backups/20261003-ignoreppc/`
  (cmdline + UKI previo)
- **Efecto:** el kernel ignora `_PPC`/`CPLT` por completo → el governor usa todo el
  rango de `_PSS` (2501 + turbo hardware). El SMC sigue escribiendo CPLT pero no
  tiene efecto.

### 7.2 Cambios complementarios de la sesión

| Cambio | Archivo | Motivo |
|---|---|---|
| `[dgpu-active] baseline = 07` | `/etc/reclocked.conf` | Evita la promoción a 0a (que agravaba el latch y causaba parpadeo) |
| `[switch] enable = false` | `/etc/reclocked.conf` | Corregido bug de sed (se había activado solo) |
| Guard v3 solo-térmico | `/usr/local/sbin/cpu-bdprochot-guard` | Sacados 2 bugs: path de pstate inexistente + busy con /proc/stat acumulativo |
| `[dgpu-active] enable = true` | `/etc/reclocked.conf` | Con baseline 07: 07 para scanout, 0e solo con busy > 85% |

### 7.3 Verificación post-reboot

```bash
# 1. cmdline y límites
cat /proc/cmdline | tr ' ' '\n' | grep ignore_ppc          # → processor.ignore_ppc=1
cat /sys/devices/system/cpu/cpu0/cpufreq/bios_limit       # → 2501000
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_max_freq # → 2501000

# 2. CPU bajo carga (con los 3 monitores activos)
( for i in 1 2 3 4; do timeout 15 yes > /dev/null & done )
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq | sort -n | tail -1
# → ~2500000 (no 800000)

# 3. Monitores + dGPU
swaymsg -t get_outputs | grep '"name"'                  # → eDP-2, DP-1, DP-2
sudo grep '\*' /sys/kernel/debug/dri/0000:01:00.0/pstate # → 07
sudo cat /sys/kernel/debug/vgaswitcheroo/switch          # → DIS: :Pwr
```

### 7.4 Rollback (si hiciera falta)

```bash
# restaurar cmdline sin ignore_ppc
sudo cp ~/.config/backups/20261003-ignoreppc/mkinitcpio-intel-cmdline.txt \
       /etc/mkinitcpio-intel-cmdline.txt
sudo /usr/local/sbin/rebuild-intel-uki.sh
# (o restaurar el UKI completo del backup)
sudo cp ~/.config/backups/20261003-ignoreppc/cachyos_intel_linux.efi \
       /boot/efi/EFI/Linux/cachyos_intel_linux.efi
```

---

## 8. Anexo: comandos de diagnóstico usados

```bash
# Estado del clamp
cat /sys/devices/system/cpu/cpu0/cpufreq/{bios_limit,scaling_max_freq,scaling_cur_freq}

# BD PROCHOT (MSR 0x1FC bit 0)
sudo python3 -c "
import glob, struct
for p in sorted(glob.glob('/dev/cpu/[0-9]*/msr')):
    with open(p,'rb') as f:
        f.seek(0x1FC); v=struct.unpack('<Q',f.read(8))[0]
    print(p, 'bit0=', v&1)
"

# Registros PLMT (el CPLT que escribe el SMC)
sudo python3 -c "
with open('/dev/port','rb') as f:
    for name, port in [('CPLT',0x310),('IGPS',0x311),('MPLT',0x312),('CFIL',0x313),('EGPS',0x314)]:
        f.seek(port); print(name, '=', f.read(1)[0])
"

# Potencia real del adaptador (floats del SMC)
#   vD0R ≈ 19.65 V   iD0R ≈ 2.285 A

# P-states disponibles
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_frequencies

# Disassembly de ACPI
sudo acpidump -o /tmp/acpi/dump.txt && cd /tmp/acpi && acpixtract -a dump.txt
for f in *.dat; do iasl -d "$f"; done
grep -l "_PPC" *.dsl
```

---

## 9. Pendientes

- [ ] **Reboot + verificación** (§7.3) — el fix está embebido en el UKI, falta aplicar
- [ ] Confirmar que el SMC no re-asserta BD PROCHOT bajo carga sostenida a 2.5 GHz
      (el guard v3 lo limpia si pasa; monitorear `journalctl -u cpu-bdprochot-guard`)
- [ ] Considerar `exit-state = 07` en reclocked.conf (hoy es `0a`: al reiniciar el
      daemon deja la dGPU en 0a — con ignore_ppc ya no clampa el CPU, pero es más
      consistente con el baseline)
- [ ] Verificar el hook de rebuild del UKI en upgrades de kernel (que el parámetro
      persista — el script lee el cmdline file, que ya lo tiene)

---

## 10. Cómo leer los logs de las GPUs (cheat sheet)

Ambas GPUs loguean al **kernel log** (`dmesg` / `journalctl -k`); se filtran por driver.
Cada una expone debugfs distinto.

### Intel (i915 / Iris Pro) — PCI `0000:00:02.0`

| Qué | Comando |
|---|---|
| Log del kernel | `sudo dmesg \| grep -i i915` |
| Requests colgados | `sudo grep hungE /sys/kernel/debug/dri/0000:00:02.0/i915_engine_info` |
| Error state (tras hang) | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/i915_error_state` |
| Resets de display | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/intel_display_reset_count` |
| Estado de energía | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/i915_runtime_pm_status` |
| Uso en vivo | `sudo intel_gpu_top` |
| PSR | `sudo cat /sys/kernel/debug/dri/0000:00:02.0/i915_edp_psr_status` |

### NVIDIA (nouveau / GT 750M) — PCI `0000:01:00.0`

| Qué | Comando |
|---|---|
| Log del kernel | `sudo dmesg \| grep -i nouveau` |
| Pstate actual | `sudo grep '\*' /sys/kernel/debug/dri/0000:01:00.0/pstate` |
| Quién la usa | `sudo cat /sys/kernel/debug/dri/0000:01:00.0/clients` |
| Params del módulo | `ls /sys/module/nouveau/parameters/` |
| Busy % | ❌ no está en debugfs — lo lee `reclocked` directo del PMU (BAR0) |

Verbose extremo: `drm.debug=0x1e` en el cmdline (llena el log).

### Consumo real del sistema (SMC DC-in)

```bash
# iD0R (corriente) y vD0R (tensión) son floats del SMC, por índice.
# Los índices se descubren una vez (en esta máquina: iD0R=638, vD0R=654).
sudo python3 -c "
import struct, time
def rd(i):
    open('/sys/devices/platform/applesmc.768/key_at_index','w').write(str(i))
    d=open('/sys/devices/platform/applesmc.768/key_at_index_data','rb').read()
    return struct.unpack('<f', d[:4])[0]
for _ in range(5):
    a=rd(638); v=rd(654)
    print(f'{v:.2f}V x {a:.3f}A = {v*a:.1f}W')
    time.sleep(1)
"
```

---

## 11. Hallazgo 2026-10-03: consumo con mpv (cámaras) — NO es la nvidia

Con el visor de 6 cámaras (`camaras-6.sh`) en el monitor externo, el consumo sube
~15 W y el CPU entra en ciclo térmico. Medido con el SMC:

| Estado | Consumo SMC | CPU |
|---|---|---|
| Sin mpv | 48-65 W | 3.2 GHz |
| mpv en DP-2 (dGPU) | 59-69 W | 798 MHz (clamp térmico) |
| mpv en eDP-1 (iGPU) | 65-73 W | 798 MHz (clamp térmico) |
| mpv cerrado | 50-66 W | 3.5 GHz |

**Conclusiones:**
1. **El cruce a la nvidia NO es el costo** — en eDP-1 (iGPU, sin cruce) el consumo es
   igual o mayor (más píxeles: 2880x1800 vs 1920x1080).
2. **Con mpv el guard térmico asserta BD PROCHOT** (temp ≥ 90 °C, picos de 96 °C) →
   CPU a 798 MHz; y el consumo **sube igual** (la iGPU compositando el video + el
   scanout mandan, no el CPU).
3. **El pico de CPU es el `--vo=wlshm` de mpv**: escala el mosaico 960x360 → tamaño de
   ventana (1904x974) en CPU (swscale) frame a frame = **~112 % CPU**
   (medido: `vo=null` 7 %, `vo=gpu` 26 %, `wlshm` 119 %).
4. **`--vo=gpu` mueve la carga a la iGPU** (package igual: 24.2 vs 24.9 W) — no es
   win gratis.

**Aplicado 2026-10-03:** `--sws-scaler=bilinear` + `setsar=1,fps=10` en el lavfi de
`camaras-6.sh` (A/B controlado: mpv 20 % → 12-14 % CPU). Backup:
`~/.config/backups/20261003-mpv-opt/`.

**Descartado con medición:** `--vo=gpu` mueve la carga a la iGPU (package igual:
24.2 vs 24.9 W); el cruce a la nvidia no es el costo (en eDP-1 el consumo es
igual o mayor).

### Fix del churn/glitch (mismo día)

Con `baseline=07` + `[caps] mpv = floor=0a` (viejo, de cuando el baseline era 0a)
la máquina de estados de reclocked hacía ping-pong `WAKE (07→0a)` ↔ `CEILING (0a→07)`
cada 2 s → **retrain del encoder → pantalla glitcheada** (evidencia: pstate flip
0a↔07 + strace con writes al debugfs pstate).

**Fix:** `mpv = floor=07, max=0e, busy-up=50` en `/etc/reclocked.conf` — el floor
debe seguir al baseline. Backup: `~/.config/backups/20261003-churn-fix/`.
Verificado: pstate estable en 07 con mpv, cero transiciones.

### Fix del input lag — no-turbo (2026-10-03, aplicado)

**Causa raíz del lag:** con turbo, el CPU llegaba a 87-90 °C cada ~6 s → el guard
assertaba BD PROCHOT → 800 MHz por ~5 s → el compositor quedaba a 800 → **input lag
periódico** (evidencia: freq 3.5 GHz → 798 MHz → 3.5 GHz cada ~6 s + guard log
`aserto`/`limpio` en loop).

**Fix:** `cpu-no-turbo.service` → `echo 0 > /sys/devices/system/cpu/cpufreq/boost`.
Verificado: **2.5 GHz estables, 80-84 °C, cero asserts, cero cycling** (mejor promedio
que el ciclo 800↔3500, ~10 °C más frío, sin lag).

Rollback: `sudo systemctl disable --now cpu-no-turbo.service`.

---

## 12. Referencias

- Kernel: `drivers/acpi/processor_perflib.c` (`acpi_processor_ppc_notifier`,
  `acpi_processor_get_bios_limit`), `drivers/cpufreq/acpi-cpufreq.c`
- ACPI: DSDT `PLMT` (SystemIO 0x310), SSDT3 `_PPC`/`_PSS`/`APSS`
- MacManx86: explicación de APSS/APSN (Apple P-state tables)
- tinyapps.org: "MacBooks without batteries severely throttled" (descartado acá)
- erikberglund/AppleNVRAM: variables NVRAM Apple (GUID `7C436110-...`)
- Historial de sesiones opencode: 2026-09-30 (c7953b16), 2026-10-01/02/03
