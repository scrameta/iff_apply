I didn't write this tool, someone shared it and asked me to pop it on the wiki. I thought that uploading here was best for that.

# `iff_apply.py` — GA100 SW-IFF record applier (admin guide)

| | |
|---|---|
| Audience | Debian/Ubuntu admin with root on the card host |
| Card model | NVIDIA CMP 170HX (GA100 `10de:2082`), but the mechanism applies to GA100-class fuse hardware in general |
| Guarantee | **Zero OTP** — no `FUSECTRL CMD_WRITE`, no RIR rows, nothing permanent. Every action is a register/SRAM write that a power-cycle clears |
| Status | field-proven by its author's fleet (phase-1 logs), but new to THIS workspace: it depends on your card's stock IFF stream, which the tool verifies live on every install |

---

## 1. What problem it solves

GA100 boots through the **fuse block read path**: at reset, the GFW (GPU Front-end micro/
firmware block) walks the MAIN fuse array under the direction of `IFF_RECORD` ("instruction
figure pointer", register `0x820050`). That pointer selects a **window of rows** which are
interpreted as a small program (**SW-IFF = software IFF**): paired 32-bit words of
`encoded-address, value` written as structured record rows. The interpreter executes them
during devinit — *before* the driver, before BAR0 optimization, before HBM.

Two ways exist to change fuse-derived boot values:

1. **Volatile OPT/`_CP` register override** (what `ga100_rir --sw-write` does): take effect
   immediately, die at the next reset. Fine for anything the runtime read path honors, but
   the value is *re-derived from fuses at every boot* — the override never covers devinit
   itself.
2. **SW-IFF replay records** (this tool): install records into the SRAM merge buffer so that
   **the GFW itself re-applies them at every reset**, as part of normal boot. The override
   now covers devinit and everything that reads fuses *after* the reset, every time.

That second layer is exactly what the PCIe **Gen3/Gen4 boot fuses** (`0x820250`,
`0x820580`) need: they are consumed too early in boot for a runtime register write to
matter at the next probe. It is also — for the same pre-devinit timing reason — the only
OTP-free path to things like HBM training parameters (the reason one community card
reached the full 48 GiB).

Nothing in this tool is manufacturer-locked: it leverages a legit boot-time "software
fusing" facility, gated on the card's `disable_sw_override` switch (same gate the
`ga100_rir` unlock already opened).

## 2. Registers it touches (all in the `NV_FUSE` BAR0 window around 0x820xxx)

| Offset | Register | Role in this tool |
|---|---|---|
| `0x820000/04/08` | FUSECTRL / FUSEADDR / FUSERDATA | standard fuse-row read engine (state machine: wait for `STATE_IDLE=4<<16`, then read). **Used only to READ rows** |
| `0x82000C` | FUSEWDATA | data port for the SRAM-write engine |
| `0x820040` | EN_SW_OVERRIDE | master enable for SW-fusing access. The install asserts it (`=1`); `undo` clears it (`=0`) |
| `0x820050` | IFF_RECORD | "program counter" into the SW-IFF SRAM: `START` on bits [8:0], `LAST` on bits [24:16]. Stock value `0x01FB01E8` = window rows 488–507 |
| `0x820088` | IFF_SW_FUSING | trigger/status of the SRAM write engine (`=1` start, bit0 polls busy) |
| `0x8200E0` | (DEBUG)CTRL PLM aux | PLM state the install checks (must be L0-open, see §5) |
| `0x8200F4` | FUSEWDATA PLM | ditto — *do not open it directly; that path has a burn history* (§5) |

Encode/decode of a record row:

```text
address word  = ((phys_addr >> 2) << 6) | op ^ op: 0x3D = WRITE_DW (32-bit overwrite)
                                                     0x3C = MODIFY_DW, 0x3E = SET_FIELD (+value), 0x3F = none/invalid
value word    = straight 32-bit payload
```

A record *occupies two rows* (address word + value word).

## 3. The critical mechanic: the window does NOT fall through

The interpreter reads rows `START..LAST` only. If any slot in the window holds 0/INVALID,
the interpreter **stops there** — and because the window overlay is *not* backed by the
fuse array, every position you want executed must be written. This is why the tool:

- computes the payload length and places its records **just below the stock stream**
  (`start = 488 − 2×(records+override)`; aborts if that would collide with used rows
  below 301),
- extends `LAST` to keep 488–507 inside the window, and
- (default) **appends verbatim copies of the 20 stock records read live from the card**
  behind the caller's records.

Both facts are the "GFW must not stall" insurance documented in the author's phase-1
summaries: an aborted/stalled interpreter leaves the GPU dead at boot; copying the stock
program *into the same window* keeps the boot path complete no matter what the caller
wanted.

Optional ordering knob — `IFF_STOCK_FIRST=1` (env): places the stock stream **before**
the payload, so the platform-set records it performs (XP_PL_CYA_0, the PLMs, trap 13,
PMC_ELPG_ENABLE…) run *before* your payload executes. This is required whenever the
payload touches registers of blocks that are still power-gated at that point (FBPA/MMU
writes before XBAR/L2 came up) — a record written against a powered-down block is what
"kills the interpreter" (GFW stalls, boot dies at status `0x01`). Leave off for
simple, always-mapped targets (e.g. the Gen3 fuse regs), turn on for FBPA-area payloads.

`EN_SW_OVERRIDE` is always emitted **first** in the record stream in both orderings.

## 4. Modes

```bash
sudo python3 iff_apply.py <BDF> install 0xADDR=0xVAL 0xADDR=0xVAL ...   # arm records; caller does the FLR
sudo python3 iff_apply.py <BDF> show                                    # read-only: what the window holds now
sudo python3 iff_apply.py <BDF> undo                                    # disarm (stock pointer, override off) + FLR hint
```

- `install` re-reads the stock stream rows 488–507 and hard-compares their first and last
  words against the documented values (`0x000D56BD`, `0xA0802007`) — on mismatch it aborts
  ("stock IFF stream not recognised") rather than program a wrong window.
- `undo` writes `IFF_SW_FUSING=0`, restores `IFF_RECORD` to stock, and sets
  `EN_SW_OVERRIDE=0`. A subsequent FLR re-reads the fuse array directly and behaves stock.
- `show` decodes each row it displays with its operation tag (WRITE_DW → address,
  MODIFY_DW, SET_FIELD, INVALID…), so an admin can inspect an armed window at any time.

## 5. PLM preconditions (this is the risky part of the whole design)

Client privilege levels (PLM) gate writes into the fuse block. Two of the four PLMs
normally must be L0-open (i.e. writable from host level) for the SW write engine to work.
The author's fleet history recorded what happens if you open `0x8200F4` **directly**:

- a chain-A write drove it to `0x0000000c`, and
- a trap-31@L3 event corrupted it on one card.

Consequence: the safe way is **not to touch the 0x8200F4 PLM at all**, and instead arm
**trap-31** (a deliberate vector that elevates host writes to L3) so the whole install
runs at L3 with no PLM mutation. In that configuration, run the tool with:

```bash
IFF_ALLOW_TRAP=1 sudo python3 iff_apply.py <BDF> install ...
```

`IFF_ALLOW_TRAP=1` also disables the PLM precondition check — you are asserting you armed
the trap yourself. Without that env, the tool simply aborts when either PLM is not
L0-open. **If neither route is available on your card, do not proceed with raw `iff_apply`
— use the harness wrapper (`170hx_sweep.py iff-...`), which refuses unsupported states
rather than improvising.**

## 6. Undo semantics and why it is not "permanent"

| Question | Answer |
|---|---|
| Is anything burned? | No. The main fuse array is untouched; only the SRAM overlay + `IFF_RECORD` + `EN_SW_OVERRIDE` change |
| Does it survive FLR? | **Yes** — that is the entire point: the payload re-applies itself at every reset |
| Does it survive power cycle? | No — SRAM overlay dies, `IFF_RECORD` reverts to power-on default |
| Explicit revert path | `undo` (+FLR), or `EN_SW_OVERRIDE=0` + `IFF_RECORD=stock` + FLR, or just power-cycle + FLR |
| Interaction with `ga100_rir` overrides | An armed IFF window re-applies at every reset — including resets performed by   the *other* tool's workflows. Those runtime writes layer on top for their session; conflicts   resolve per-register by write order in the boot flow |

## 7. The harness wrapper (recommended way to use it)

`170hx_sweep.py` wraps this tool with the same safety rails as the rest of the sweep:

```bash
sudo python3 170hx_sweep.py iff-show                                          # read-only peek
sudo python3 170hx_sweep.py iff-install 0x820250=0 0x820580=0                 # Phase G0 Gen3 pair
sudo python3 170hx_sweep.py iff-undo                                          # disarm + FLR + rebind
```

- Record targets are **whitelisted** to the registers the workspace knows (no arbitrary
  WRITE_DW is driven blindly).
- It refuses to install over an already-armed, non-stock window (re-run disarm first).
- `run`/`discover` of the sweep **hard-abort** while a window is armed: the sweep's "any
  failure → FLR → stock" rollback model would replay your payload instead of restoring
  stock. `--allow-iff` overrides deliberately (only ever with `iff-show` verified state).
- The harness passes your environment through, so `IFF_ALLOW_TRAP=1` and
  `IFF_STOCK_FIRST=1` work the same as the raw tool.
- Location: sibling `iff_apply.py`, or `SWEEP_IFF=/path/to/iff_apply.py`.

When to use **raw** vs **wrapper**: raw for ad-hoc experimentation on a bench card where
you exactly understand the target register; wrapper for anything that ends up recorded in
the workspace (history, intent trail, and re-arming guard all included).

## 8. Known payload recipes

Across this workspace, completed and validated IFF use-cases are documented in
`info/SW-IFF_recipe.md` (concrete record tables per die, including the full 126-SM and 48-GiB
payloads). This tool *executes* such payloads; *which* records to send remains a
per-die decision — do not transplant record tables across dies (`info/SW-IFF_recipe.md`
precondition 3).

## 9. Troubleshooting quick answers

| Symptom | Meaning / action |
|---|---|
| `ABORT: 0x8200E0 / 0x8200F4 not L0-open` | the host cannot yet write the SRAM engine. Do NOT open `0x8200F4` directly (burn history, §5). Arm trap-31 and retry with `IFF_ALLOW_TRAP=1`, or restore an earlier PLM configuration the author's chain-A produces |
| `ABORT: stock IFF stream not recognised: …` | the stock records at 488–507 do not match the known-good pattern — your card's boot stream differs. Capture the actual rows (`bar0_read.py <BDF> 0x8201F8 …` window row dump) and treat this card as NOT eligible for a blind install |
| `INSTALL=FAILED` with row mismatches | the SRAM write engine faulted mid-stream (PLM timing or a losing write). The window is inconsistent — do **not** FLR with this window active; run `undo`, FLR, re-check with `iff-show` |
| GPU fails to boot at all after an install | a payload wrote a powered-gated block before it came awake (missing `IFF_STOCK_FIRST=1`), or the window left an unpopulated slot. Power-cycle (SRAM dies) → back to stock; if it still fails, priority target is `IFF_RECORD` restore (`bar0_write.py <BDF> 0x820050 0x01FB01E8`), then escalate (§10) |
| Driver kills/`dmesg` fused-value fallout | the payload re-applies at *every* reset — `iff-undo` + FLR before you continue experimenting |
| `iff-show` shows `INVALID` rows inside your window | an unpopulated slot — the interpreter will stop there at next boot. Re-`install` (freshly reads stock rows); verify with `show` before the FLR |

## 10. Kill switch (escalation ladder)

1. `sudo python3 iff_apply.py <BDF> undo` + FLR.
2. `bar0_write.py <BDF> 0x820050 0x01FB01E8` (restore the stock pointer directly),
   then FLR.
3. Hard power-off — SRAM overlay dies with power; stock boot (be sure the fuse PLMs are
   not in a weird state, else consult `info/rir-unlock-log.md` — the original gate-unlock
   history documents the PLM states the tooling produced).
4. Whatever happens, the `<BDF> --sw-write` layer from `ga100_rir` remains independent:
   runtime overrides remain usable even if the IFF path is misbehaving (or vice versa) —
   the two layers write different things.

## 11. Lines of caution worth repeating

1. The tool ABORTS rather than improvises at gate/PLM/stock-stream-fingerprint failures —
   treat an abort as "card state not understood", not "retry harder".
2. Compounding matters: an armed window is active across EVERY reset, including those you
   trigger inadvertently (watchdog, random driver fault, common use of `nvidia-smi -r`).
   For experimenting with sensitive payloads (memory), arm windows only while a
   session/plan is explicitly documented in the state logs.
3. The record target whitelist lives in the harness. Raw usage accepts ANY address a
   WRITE_DW can encode — includes registers the boot code may consider sacred. Cross-check
   a payload against `info/SW-IFF_recipe.md` before running raw.
4. Nothing here bypasses the `disable_sw_override` switch; the original RIR/unlock work
   (`info/rir-unlock-log.md`) is what keeps this whole layer possible.
