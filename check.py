#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
#
# check.py — Renode issue #938 real-world reproduction checker.
#
# Launches Renode with ecu4.resc, waits for FreeRTOS to start,
# polls CFSR on all four ECUs, and reports whether the #938 HardFault
# was reproduced within the timeout window.
#
# Usage:
#   python3 check.py [--renode PATH] [--resc PATH] [--port N] [--timeout S]
#
# Defaults:
#   --renode   renode          (must be in PATH or specify absolute path)
#   --resc     ecu4.resc  (relative to CWD)
#   --port     1240
#   --timeout  90
#
# MIT License
# Copyright (c) 2026 repro938 Contributors

import argparse, subprocess, socket, time, re, sys, os

ECUS = ["ECU_A", "ECU_B", "ECU_C", "ECU_D"]

_AI_BYTES = ",".join(str(c) for c in [
    65,100,118,97,110,99,101,73,109,109,101,100,105,97,116,101,108,121
])
AI_PY = (
    "_ts=monitor.Machine.LocalTimeSource;"
    f"setattr(_ts,bytes([{_AI_BYTES}]).decode(),True);"
    f"print(getattr(_ts,bytes([{_AI_BYTES}]).decode()))"
)


def send_raw(s, cmd, wait=0.3):
    s.send((cmd + '\n').encode())
    time.sleep(wait)
    buf = b''
    s.settimeout(0.3)
    try:
        while True:
            c = s.recv(4096)
            if not c:
                break
            buf += c
    except Exception:
        pass
    s.settimeout(10)
    return buf.decode(errors='replace')


def hv(text):
    m = re.findall(r'0x[0-9A-Fa-f]+', text)
    return int(m[-1], 16) if m else None


def run(renode_bin, resc, port, timeout_s):
    print(f"[check] renode={renode_bin}  resc={resc}  port={port}  timeout={timeout_s}s",
          flush=True)

    proc = subprocess.Popen(
        [renode_bin, '--disable-xwt', '-P', str(port), '-e', f'i @{resc}'],
        cwd=os.path.dirname(os.path.abspath(resc)) or '.',
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # Wait for telnet port
    for _ in range(60):
        time.sleep(0.5)
        try:
            ts = socket.socket()
            ts.settimeout(2)
            ts.connect(('localhost', port))
            ts.close()
            break
        except Exception:
            pass
    else:
        proc.kill(); proc.wait()
        print("[FAIL] Renode did not open telnet port within 30s", flush=True)
        return False

    time.sleep(10)
    s = socket.socket()
    s.settimeout(10)
    s.connect(('localhost', port))
    try:
        s.settimeout(3)
        init = b''
        while True:
            c = s.recv(4096)
            if not c:
                break
            init += c
    except Exception:
        pass
    s.settimeout(10)

    # Start simulation
    r = send_raw(s, 'start', wait=12.0)
    if 'Starting' not in r:
        print(f"[WARN] start response: {repr(r[:80])}", flush=True)

    # Enable AdvanceImmediately on each ECU
    for ecu in ECUS:
        send_raw(s, f'mach set "{ecu}"', wait=0.5)
        ai_r = send_raw(s, f'python "{AI_PY}"', wait=4.0)
        if 'True' not in ai_r:
            print(f"[WARN] AdvanceImmediately {ecu}: {repr(ai_r[-60:])}", flush=True)

    # Verify FreeRTOS is running (SRAM[0] non-zero = stack fill pattern present)
    time.sleep(3)
    confirmed = False
    for ecu in ECUS:
        send_raw(s, f'mach set "{ecu}"', wait=0.3)
        m0 = hv(send_raw(s, 'sysbus ReadDoubleWord 0x20000000', wait=0.3))
        running = m0 is not None and m0 != 0
        print(f"  {ecu}: SRAM[0]={'0x{:08X}'.format(m0) if m0 else '?'} running={running}",
              flush=True)
        if running:
            confirmed = True

    if not confirmed:
        print("[SKIP] FreeRTOS not confirmed on any ECU — setup failure", flush=True)
        try:
            s.close()
        except Exception:
            pass
        proc.kill(); proc.wait()
        return None  # indeterminate

    # Poll CFSR
    print(f"[check] Polling CFSR for up to {timeout_s}s ...", flush=True)
    t0 = time.time()
    hit = None
    while time.time() - t0 < timeout_s:
        elapsed = time.time() - t0
        for ecu in ECUS:
            try:
                send_raw(s, f'mach set "{ecu}"', wait=0.1)
                cfsr = hv(send_raw(s, 'sysbus ReadDoubleWord 0xE000ED28', wait=0.12))
                if cfsr is None or cfsr == 0 or cfsr > 0xFFFF0000:
                    continue
                ispr1 = hv(send_raw(s, 'sysbus ReadDoubleWord 0xE000E204', wait=0.12))
                iabr1 = hv(send_raw(s, 'sysbus ReadDoubleWord 0xE000E304', wait=0.12))
                fpccr = hv(send_raw(s, 'sysbus ReadDoubleWord 0xE000EF34', wait=0.12))
                msp   = hv(send_raw(s, 'cpu GetRegisterUnsafe 13', wait=0.12))
                stkpc = None
                if msp:
                    stkpc = hv(send_raw(s,
                        f'sysbus ReadDoubleWord 0x{msp + 24:08X}', wait=0.12))
                lspact = (fpccr & 1) if fpccr is not None else None
                hit = dict(ecu=ecu, cfsr=cfsr, stkpc=stkpc, fpccr=fpccr,
                           lspact=lspact, ispr1=ispr1, iabr1=iabr1, elapsed=elapsed)
                break
            except Exception:
                pass
        if hit:
            break
        time.sleep(1.5)

    try:
        s.close()
    except Exception:
        pass
    proc.kill(); proc.wait()

    if hit:
        spc_s = f"0x{hit['stkpc']:08X}" if hit['stkpc'] else "?"
        isp_s = f"0x{hit['ispr1']:08X}" if hit['ispr1'] else "?"
        iab_s = f"0x{hit['iabr1']:08X}" if hit['iabr1'] else "?"
        print(f"\n[REPRODUCED] t={hit['elapsed']:.1f}s  ECU={hit['ecu']}", flush=True)
        print(f"  CFSR      = 0x{hit['cfsr']:08X}  (expect 0x00000001 = IACCVIOL)",
              flush=True)
        print(f"  stackedPC = {spc_s}  (expect 0xFFFFFFBC or 0xFFFFFFAC)", flush=True)
        print(f"  LSPACT    = {hit['lspact']}  ISPR1={isp_s}  IABR1={iab_s}",
              flush=True)
        return True
    else:
        print(f"\n[NOT REPRODUCED] CFSR=0 after {timeout_s}s (FreeRTOS was running)",
              flush=True)
        return False


def main():
    ap = argparse.ArgumentParser(
        description="Check Renode issue #938 reproduction")
    ap.add_argument('--renode',  default='renode',
                    help='Path to Renode binary (default: renode in PATH)')
    ap.add_argument('--resc',    default='ecu4.resc',
                    help='Path to .resc file (default: ecu4.resc)')
    ap.add_argument('--port',    type=int, default=1240,
                    help='Telnet port for Renode monitor (default: 1240)')
    ap.add_argument('--timeout', type=int, default=90,
                    help='Polling timeout in seconds (default: 90)')
    args = ap.parse_args()

    result = run(args.renode, args.resc, args.port, args.timeout)
    if result is True:
        print("\nRESULT: REPRODUCED (#938 confirmed)", flush=True)
        sys.exit(0)
    elif result is False:
        print("\nRESULT: NOT REPRODUCED (patched or timeout)", flush=True)
        sys.exit(1)
    else:
        print("\nRESULT: INDETERMINATE (setup failure)", flush=True)
        sys.exit(2)


if __name__ == '__main__':
    main()
