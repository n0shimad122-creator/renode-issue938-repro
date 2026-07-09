# Real-World Reproduction — Renode Issue #938 (STM32L552 / FreeRTOS)

This directory demonstrates that Renode issue #938 (Gate4 EXC_RETURN guard miss
in `process_interrupt`) triggers a HardFault in a real automotive application —
an open-source CAN testbed running FreeRTOS on STM32L552 (Cortex-M33 with FPU;
see [Related](#related) for the firmware project link).

## What Happens

On unpatched Renode v1.16.1, when the STM32L552 FreeRTOS scheduler exits an
exception via `BX LR` with `LR = 0xFFFFFFBC` (basic frame) or `0xFFFFFFAC`
(extended FPU frame), and a higher-priority timer interrupt (TIM7 IRQ50) expires
at that Translation Block boundary, `process_interrupt` is called with
`PC = 0xFFFFFFBC` or `0xFFFFFFAC`.

The Gate4 guard (`cmpl $0xffffffdf, PC; ja skip`) does **not** catch these values
because both are `< 0xFFFFFFE0`.  The CPU attempts to fetch an instruction from
that address, takes IACCVIOL, and enters HardFault.

**Observed**: CFSR = `0x00000001` (IACCVIOL), stacked PC at SP+24 =
`0xFFFFFFBC` (basic frame, LSPACT=0) or `0xFFFFFFAC` (extended FPU frame,
LSPACT=1).  IRQ50 (TIM7) remains stuck active in IABR1.

**Fix (D2)**: Replace the guard with `cmpb $0xff, PC[31:24]; je skip`, which
catches all `0xFF??????` EXC_RETURN values including the full
`0xFFFFFF00`–`0xFFFFFFDF` range currently missed.

## Reproduction Results

| Renode build | Trials | HardFault | stackedPC values |
|---|---|---|---|
| v1.16.1 **unpatched** | 5 | **5/5** | 0xFFFFFFBC×4, 0xFFFFFFAC×1 |
| v1.16.1 **Fix-D2 patched** | 5 | 0/5 | — (FreeRTOS runs normally) |

The fault is timing-dependent (a race between the exception return and the pending timer
interrupt): in this setup 4/5 trials hit it within ~0.1 s of FreeRTOS start and 1/5 took
~53 s of virtual time. The per-run hit rate varies with host speed, so a given run may need
to be repeated.

## Prerequisites

### 1. Build the firmware

Clone and build the upstream firmware project:

```sh
git clone https://github.com/ToyotaInfoTech/RAMN.git
cd RAMN/RAMNV1
# Follow the upstream project's build instructions to produce ECUA.hex – ECUD.hex
```

Alternatively, if you already have the firmware built, place ECUx.hex as described below.

Copy or symlink the four HEX files into this directory:

```sh
cp path/to/ECUA.hex path/to/ECUB.hex path/to/ECUC.hex path/to/ECUD.hex \
   ./   # repository root (same directory as ecu4.resc)
```

### 2. Renode v1.16.1

Download Renode v1.16.1 from https://github.com/renode/renode/releases

```sh
# Example (Linux portable):
wget https://github.com/renode/renode/releases/download/v1.16.1/renode-1.16.1.linux-portable.tar.gz
tar xf renode-1.16.1.linux-portable.tar.gz
# renode binary is at renode_1.16.1_portable/renode
```

### 3. Python 3 (for check.py)

No extra packages required.  `check.py` uses only the standard library.

## Run

All commands must be run from the repository root directory.

### Unpatched — expect HardFault within ~90 s of virtual time

```sh
# Run from the repository root:
renode --disable-xwt -P 1240 -e "i @ecu4.resc"
```

In a second terminal, run the checker:

```sh
python3 check.py --renode renode --port 1240
```

Or launch and check in one step (checker starts its own Renode process):

```sh
python3 check.py --renode /path/to/renode --timeout 90
```

Expected output:

```
[check] renode=...  resc=ecu4.resc  port=1240  timeout=90s
  ECU_A: SRAM[0]=0x55555555 running=True
  ECU_B: SRAM[0]=0x00000000 running=False
  ECU_C: SRAM[0]=0x00000000 running=False
  ECU_D: SRAM[0]=0x000001B8 running=True
[check] Polling CFSR for up to 90s ...

[REPRODUCED] t=0.1s  ECU=ECU_A
  CFSR      = 0x00000001  (expect 0x00000001 = IACCVIOL)
  stackedPC = 0xFFFFFFBC  (expect 0xFFFFFFBC or 0xFFFFFFAC)
  LSPACT    = 0  ISPR1=0x00060400  IABR1=0x00040000

RESULT: REPRODUCED (#938 confirmed)
```

### Patched (Fix-D2) — expect no HardFault

Apply Fix-D2 to your Renode build (see below), then:

```sh
python3 check.py --renode /path/to/renode_patched --timeout 90
```

Expected output: `RESULT: NOT REPRODUCED (patched or timeout)`

## Applying Fix-D2

The fix is a one-instruction change in `translate.c`
(`process_interrupt` function, the Gate4 guard):

```diff
-    /* Gate4: skip if PC is an EXC_RETURN magic value (>= 0xFFFFFFE0) */
-    if ((uint32_t)env->pc > 0xFFFFFFDFu)
+    /* Gate4: skip if PC has top byte 0xFF (covers all EXC_RETURN values) */
+    if (((uint32_t)env->pc >> 24) == 0xFFu)
         goto skip;
```

The equivalent assembly change in the JIT'd `translate-arm-m-le.so`:

```
# Before (unpatched):
cmpl  $0xffffffdf, <PC field offset>(%rdi)
ja    skip

# After (Fix-D2):
cmpb  $0xff, <PC field offset + 3>(%rdi)   # compare top byte only
je    skip
```

## Files in This Directory

| File | Origin | License |
|------|--------|---------|
| `stm32l552_repro.repl` | Original (extends Renode upstream stm32l552.repl) | MIT |
| `ecu4.resc` | Original | MIT |
| `check.py` | Original | MIT |
| `README.md` | Original | MIT |
| `LICENSE` | Original | MIT |
| `ECUA.hex` – `ECUD.hex` | **User-supplied** (build from the upstream firmware project) | See link below |

The `stm32l552_repro.repl` file extends the Renode infrastructure platform
description for STM32L552 (MIT License, Copyright (c) Antmicro;
https://github.com/renode/renode/blob/master/platforms/cpus/stm32l552.repl).
Only timer frequency
overrides and peripheral Tags are added; no Renode source code is copied.

**Firmware binaries are not included.**  Users must build them from the
upstream firmware source.  See `LICENSE` for the applicable licenses.

## Related

- Renode issue #938: https://github.com/renode/renode/issues/938
- Upstream firmware project: https://github.com/ToyotaInfoTech/RAMN
