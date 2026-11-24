#!/usr/bin/env python3
"""Generic GA100 SW-IFF applier: install arbitrary WRITE_DW records into the SW-IFF SRAM buffer
and replay them at reset, WITHOUT stalling GFW.

The buffer is indexed by (row - IFF_RECORD.START) and does NOT fall through to the fuse array,
so EVERY slot in the window must be populated. This tool always appends verbatim copies of the
20 stock IFF records (read live from the array) after the caller's records, which is what keeps
GFW booting -- see logs/iff-phase1-card06-*/00-SUMMARY.md Addenda 6-7.

Zero OTP: IFF_SW_FUSING is a volatile SRAM write, not FUSECTRL CMD_WRITE. Undo is
EN_SW_OVERRIDE=0 + IFF_RECORD=stock, then an FLR.

  iff_apply.py <bdf> install 0x820250=0 0x82057c=0 ...   # arms; caller does the FLR
  iff_apply.py <bdf> show                                 # what the buffer holds now
  iff_apply.py <bdf> undo                                 # disarm (FLR afterwards to recover)
"""
import mmap, os, struct, sys, time

FUSECTRL, FUSEADDR, FUSERDATA, FUSEWDATA = 0x820000, 0x820004, 0x820008, 0x82000C
EN_SW_OVERRIDE, IFF_RECORD, IFF_SW_FUSING = 0x820040, 0x820050, 0x820088
DEBUGCTRL_PLM, FUSEWDATA_PLM = 0x8200E0, 0x8200F4
CMD_READ, STATE_IDLE = 1, 4
STOCK_LO, STOCK_HI, STOCK_IFF = 488, 507, 0x01FB01E8
STOCK_FIRST, STOCK_LAST = 0x000D56BD, 0xA0802007

enc = lambda a: (((a >> 2) << 6) | 0x3D) & 0xFFFFFFFF
dec = lambda w: (((w >> 6) << 2), w & 0x3F)


class Fuse:
    def __init__(self, bdf):
        os.system("setpci -s %s COMMAND=0x0006 >/dev/null 2>&1" % bdf[5:])
        p = "/sys/bus/pci/devices/%s/resource0" % bdf
        self.fd = os.open(p, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, min(os.stat(p).st_size, 32 << 20),
                            mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def rd(self, o): return struct.unpack_from("<I", self.mm, o)[0]
    def wr(self, o, v): struct.pack_into("<I", self.mm, o, v)

    def fidle(self, t=2.0):
        d = time.time() + t
        while ((self.rd(FUSECTRL) >> 16) & 0x1F) != STATE_IDLE:
            if time.time() > d:
                raise RuntimeError("FUSECTRL timeout")

    def sidle(self, t=2.0):
        d = time.time() + t
        while self.rd(IFF_SW_FUSING) & 1:
            if time.time() > d:
                return False
        return True

    def row(self, r):
        self.wr(FUSEADDR, r); self.wr(FUSECTRL, CMD_READ); self.fidle()
        return self.rd(FUSERDATA)

    def swwr(self, r, v):
        self.sidle(); self.wr(FUSEADDR, r); self.wr(FUSEWDATA, v)
        self.wr(IFF_SW_FUSING, 1); return self.sidle()

    def close(self):
        self.mm.close(); os.close(self.fd)


def main():
    bdf, mode = sys.argv[1], sys.argv[2]
    f = Fuse(bdf)
    try:
        if mode == "show":
            iffr = f.rd(IFF_RECORD)
            s, l = iffr & 0x1FF, (iffr >> 16) & 0x1FF
            print("EN_SW_OVERRIDE=0x%08x  IFF_RECORD=0x%08x (START=%d LAST=%d)"
                  % (f.rd(EN_SW_OVERRIDE), iffr, s, l))
            for r in range(s, l + 1):
                v = f.row(r)
                a, op = dec(v)
                tag = {0x3D: "WRITE_DW -> 0x%06X" % a, 0x3C: "MODIFY_DW -> 0x%06X" % a,
                       0x3E: "SET_FIELD", 0x3F: "INVALID", 0x00: "(data/null)"}.get(op, "op=0x%02x" % op)
                print("  row %3d off %2d = 0x%08x   %s" % (r, r - s, v, tag))
            return

        if mode == "undo":
            f.wr(IFF_SW_FUSING, 0); f.wr(IFF_RECORD, STOCK_IFF); f.wr(EN_SW_OVERRIDE, 0)
            print("disarmed: EN_SW_OVERRIDE=0x%08x IFF_RECORD=0x%08x  (FLR now to recover)"
                  % (f.rd(EN_SW_OVERRIDE), f.rd(IFF_RECORD)))
            return

        if mode != "install":
            sys.exit("unknown mode %s" % mode)

        # Normally both fuse PLMs must be L0-open. But opening 0x8200F4 is a known hazard: a chain-A
        # write drove it to 0x0000000c, and trap-31@L3 corrupted it on card 0f. Its stock value
        # 0xffffff8f is already write-L3, so the safe alternative is to run the whole install with
        # trap-31 armed -- that elevates host writes to L3 and needs no PLM change at all.
        # IFF_ALLOW_TRAP=1 asserts the caller has armed the trap.
        if os.environ.get("IFF_ALLOW_TRAP") == "1":
            print("IFF_ALLOW_TRAP=1: skipping PLM precondition "
                  "(writes must run at L3 under an armed trap-31)")
        elif not (((f.rd(DEBUGCTRL_PLM) >> 4) & 1) and ((f.rd(FUSEWDATA_PLM) >> 4) & 1)):
            sys.exit("ABORT: 0x8200E0 / 0x8200F4 not L0-open -- run chain-A + trap-31 first")

        pairs = []
        for spec in sys.argv[3:]:
            a, v = spec.split("=")
            pairs.append((int(a, 0), int(v, 0)))
        if not pairs:
            sys.exit("no records given")

        # always assert the override first, then the caller's writes
        recs = [(EN_SW_OVERRIDE, 1)] + pairs

        if f.rd(EN_SW_OVERRIDE) != 0:
            f.wr(EN_SW_OVERRIDE, 0); time.sleep(0.05)
        f.wr(IFF_RECORD, STOCK_IFF); time.sleep(0.05)
        stock = [f.row(r) for r in range(STOCK_LO, STOCK_HI + 1)]
        if stock[0] != STOCK_FIRST or stock[-1] != STOCK_LAST:
            sys.exit("ABORT: stock IFF stream not recognised: %08x..%08x" % (stock[0], stock[-1]))
        print("stock IFF stream captured and verified (%d rows)" % len(stock))

        n = len(recs) * 2
        start = STOCK_LO - n
        if start < 301:
            sys.exit("ABORT: window would run into used FF rows (start=%d)" % start)
        # IFF_STOCK_FIRST=1: emit the 20 stock records BEFORE the caller's payload, so the
        # stock stream (XP_PL_CYA_0, the PLMs, trap 13, PMC_ELPG_ENABLE) has already run when
        # the payload executes. Needed for any target outside the always-on fuse block: an
        # FBPA/MMU/WPR write issued while XBAR/L2/HUB are still power-gated appears to kill the
        # interpreter -- the stock records never run and GFW stalls at 0x01, indistinguishable
        # from an unpopulated window. EN_SW_OVERRIDE stays first in either ordering.
        stock_first = os.environ.get("IFF_STOCK_FIRST") == "1"
        words, payload = [], []
        for a, v in recs:
            dst = payload if (stock_first and a != EN_SW_OVERRIDE) else words
            dst.extend([enc(a), v & 0xFFFFFFFF])
        words = words + stock + payload if stock_first else words + stock

        f.wr(EN_SW_OVERRIDE, 1)
        f.wr(IFF_RECORD, (start & 0xFFFF) | ((STOCK_HI & 0xFFFF) << 16))
        print("armed: EN_SW_OVERRIDE=%d IFF_RECORD=0x%08x (START=%d LAST=%d, %d slots)"
              % (f.rd(EN_SW_OVERRIDE), f.rd(IFF_RECORD), start, STOCK_HI, len(words)))

        for i, w in enumerate(words):
            f.swwr(start + i, w)
        bad = [(start + i, w, f.row(start + i)) for i, w in enumerate(words)
               if f.row(start + i) != w]
        print("wrote %d slots; mismatches: %d" % (len(words), len(bad)))
        for r, w, g in bad[:8]:
            print("  row %d want 0x%08x got 0x%08x" % (r, w, g))
        for a, v in recs:
            print("  record: 0x%06X = 0x%08x    (live now: 0x%08x)" % (a, v, f.rd(a)))
        print("INSTALL=%s" % ("OK" if not bad else "FAILED"))
    finally:
        f.close()


main()