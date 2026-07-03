"""
AGENT_H.vector_verifier — RISC-V Vector extension "V" / RVV (T41)
=================================================================

Golden-reference checker for the RISC-V Vector extension (RVV 1.0) driven from
the commit log. RVV is the widest ISA gap in most verification flows because its
correctness hinges on *dynamic configuration state* (``vtype`` / ``vl``) that a
plain scalar tandem-diff never inspects.

What it checks (all sound, all conservatively gated)
----------------------------------------------------
1. **vset_vl**  (HIGH) — the most important RVV property. On a
   ``vsetvli/vsetivli/vsetvl`` the returned ``vl`` must obey the spec's
   application-vector-length rules for the configured SEW/LMUL/VLEN:
     * ``VLMAX = LMUL · VLEN / SEW`` (LMUL may be fractional: 1/8,1/4,1/2)
     * ``AVL ≤ VLMAX``          →  ``vl == AVL``
     * ``AVL ≥ 2·VLMAX``        →  ``vl == VLMAX``
     * ``VLMAX < AVL < 2·VLMAX`` →  ``ceil(AVL/2) ≤ vl ≤ VLMAX``  (impl-defined
       band — we only assert the spec-guaranteed bounds, never a point value,
       so a compliant design is never falsely flagged)
     * always ``vl ≤ VLMAX`` and ``vl ≥ 0``.
2. **vtype_vill**  (HIGH) — an unsupported ``vtype`` (reserved LMUL, SEW > ELEN,
   or VLMAX < 1) must set ``vill`` and force ``vl = 0``. A DUT that accepts an
   illegal config, or leaves ``vl ≠ 0`` while ``vill`` is set, is flagged.
3. **velem**  (HIGH) — for supported vector ALU ops, recompute each **active,
   unmasked** element with a golden SEW-width model and compare to the DUT's
   destination register. Masked/tail elements are skipped (agnostic policy is
   legal), so only architecturally-pinned elements are checked.
4. **vtail**  (MEDIUM) — when ``vta = 0`` (tail-undisturbed) the elements at
   ``[vl, VLMAX)`` of the destination must equal their prior value, if the log
   exposes the pre-state.

Metrics (never fail the run): vector-instr count, vset count, mean ``vl``,
SEW / LMUL histograms, masked-op count, active-element throughput.

Additive trace contract (all optional — the agent no-ops without it)
--------------------------------------------------------------------
```
record may carry:
  "vtype": {"sew":32,"lmul":1,"vill":false,"vta":1,"vma":1}   # or an encoded int/hex
  "vl":    <int>          resulting application vector length
  "avl":   <int>          requested AVL (for vset checks)
  "vlen":  <int>          hardware VLEN in bits (default 128)
  "vregs": {"v1":["0x..",...], "v2":[...], "v0":[...] }        # element arrays
  "vmask": [true,false,...]   # or taken from vregs["v0"] LSBs
  "vstate_prev": {"v1":[...]} # dest pre-state for the tail check
```

Stdlib-only, schema-v2.1.0 report, graceful degradation throughout.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_H.vector")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "vector_verifier"
DEFAULT_VLEN = 128
ELEN = 64  # max element width in bits

# vlmul field (3 bits) → LMUL multiplier (4 is reserved → vill)
_VLMUL = {0: 1.0, 1: 2.0, 2: 4.0, 3: 8.0, 5: 0.125, 6: 0.25, 7: 0.5}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SEW element arithmetic helpers
# ─────────────────────────────────────────────────────────────────────────────
def _umask(sew: int) -> int:
    return (1 << sew) - 1


def _u(v: int, sew: int) -> int:
    return v & _umask(sew)


def _s(v: int, sew: int) -> int:
    v &= _umask(sew)
    return v - (1 << sew) if (v >> (sew - 1)) & 1 else v


# ─────────────────────────────────────────────────────────────────────────────
# vtype decode + VLMAX
# ─────────────────────────────────────────────────────────────────────────────
def decode_vtype(vtype: Any) -> Optional[Dict[str, Any]]:
    """
    Normalise a vtype into {sew, lmul (float), vta, vma, vill}. Accepts a dict
    ({"sew":..,"lmul":..,...}) or an encoded int / "0x..".
    """
    if isinstance(vtype, dict):
        sew = _to_int(vtype.get("sew"))
        lmul = vtype.get("lmul")
        try:
            lmul = float(lmul) if lmul is not None else None
        except (TypeError, ValueError):
            lmul = None
        d = {
            "sew": sew,
            "lmul": lmul,
            "vta": _to_int(vtype.get("vta")) or 0,
            "vma": _to_int(vtype.get("vma")) or 0,
            "vill": bool(vtype.get("vill", False)),
        }
        # If sew/lmul known, recompute the *architectural* vill for the golden.
        d["golden_vill"] = _is_illegal(d["sew"], d["lmul"])
        return d
    enc = _to_int(vtype)
    if enc is None:
        return None
    vill = bool((enc >> 31) & 1) if enc >= 0 else True
    vlmul = enc & 0x7
    vsew = (enc >> 3) & 0x7
    vta = (enc >> 6) & 0x1
    vma = (enc >> 7) & 0x1
    sew = (8 << vsew) if vsew <= 3 else None       # vsew 4..7 reserved
    lmul = _VLMUL.get(vlmul)                        # vlmul 4 reserved → None
    d = {"sew": sew, "lmul": lmul, "vta": vta, "vma": vma, "vill": vill}
    d["golden_vill"] = _is_illegal(sew, lmul)
    return d


def _is_illegal(sew: Optional[int], lmul: Optional[float], vlen: int = DEFAULT_VLEN) -> bool:
    if sew is None or lmul is None:
        return True
    if sew > ELEN or sew not in (8, 16, 32, 64):
        return True
    # VLMAX must be >= 1: LMUL*VLEN/SEW >= 1  (and SEW <= LMUL*ELEN)
    if lmul * vlen < sew:
        return True
    if sew > lmul * ELEN:                            # SEW must fit an LMUL group
        return True
    return False


def vlmax(sew: int, lmul: float, vlen: int = DEFAULT_VLEN) -> int:
    return int(lmul * vlen / sew)


# ─────────────────────────────────────────────────────────────────────────────
# Vector ALU golden model
# ─────────────────────────────────────────────────────────────────────────────
_COMMUTE = {"vadd", "vand", "vor", "vxor", "vmul", "vmulh", "vmulhu",
            "vminu", "vmaxu", "vmin", "vmax"}


def velem_compute(base: str, sew: int, a: int, b: int) -> Optional[int]:
    """
    Golden element result for op ``base`` on SEW-width operands (a = vs2[i],
    b = vs1[i] / rs1 / imm). Returns unsigned SEW-wide result, or None if the op
    is not modelled (→ the checker skips it).
    """
    m = _umask(sew)
    sh = b & (sew - 1)
    if base == "vadd":
        return (a + b) & m
    if base == "vsub":
        return (a - b) & m
    if base == "vrsub":
        return (b - a) & m
    if base == "vand":
        return (a & b) & m
    if base == "vor":
        return (a | b) & m
    if base == "vxor":
        return (a ^ b) & m
    if base == "vsll":
        return (a << sh) & m
    if base == "vsrl":
        return (_u(a, sew) >> sh) & m
    if base == "vsra":
        return (_s(a, sew) >> sh) & m
    if base == "vmul":
        return (a * b) & m
    if base == "vmulhu":
        return ((_u(a, sew) * _u(b, sew)) >> sew) & m
    if base == "vmulh":
        return ((_s(a, sew) * _s(b, sew)) >> sew) & m
    if base == "vminu":
        return _u(a, sew) if _u(a, sew) <= _u(b, sew) else _u(b, sew)
    if base == "vmaxu":
        return _u(a, sew) if _u(a, sew) >= _u(b, sew) else _u(b, sew)
    if base == "vmin":
        return (a if _s(a, sew) <= _s(b, sew) else b) & m
    if base == "vmax":
        return (a if _s(a, sew) >= _s(b, sew) else b) & m
    if base in ("vmv", "vmv.v"):                     # vmv.v.* : copy operand
        return b & m
    if base == "vmerge":                              # handled with mask upstream
        return b & m
    return None


_MODELLED = {"vadd", "vsub", "vrsub", "vand", "vor", "vxor", "vsll", "vsrl",
             "vsra", "vmul", "vmulh", "vmulhu", "vminu", "vmaxu", "vmin",
             "vmax", "vmv", "vmerge"}


def decode_vector_alu(disasm: str) -> Optional[Dict[str, Any]]:
    """
    Parse a vector ALU disasm into {base, form, vd, vs2, src3, masked}.
    form ∈ {vv, vx, vi}. src3 is the vs1 name (vv), rs1 name (vx) or imm (vi).
    Returns None for anything not a modelled vector ALU op.
    """
    if not disasm or not isinstance(disasm, str):
        return None
    toks = disasm.replace(",", " ").split()
    if not toks:
        return None
    mnem = toks[0].lower()
    if "." not in mnem:
        return None
    base, _, suf = mnem.partition(".")
    # suffix like vv / vx / vi / vvm / vxm / vim (m = masked merge form) or
    # vmv.v.v etc. Normalise vmv.v.x → base vmv, form vx.
    if base == "vmv":
        # vmv.v.v / vmv.v.x / vmv.v.i
        parts = mnem.split(".")
        if len(parts) == 3 and parts[1] == "v":
            form = {"v": "vv", "x": "vx", "i": "vi"}.get(parts[2])
            base, suf = "vmv", parts[2]
        else:
            return None
    else:
        form = suf[:2] if suf[:2] in ("vv", "vx", "vi") else None
    if base not in _MODELLED or form not in ("vv", "vx", "vi"):
        return None
    ops = toks[1:]
    # strip a trailing mask operand ("v0.t") and note masking
    masked = any(o.lower().startswith("v0.t") or o.lower() == "v0" for o in ops[3:]) \
        or any("v0.t" in o.lower() for o in ops)
    ops = [o for o in ops if "v0.t" not in o.lower()]
    if base == "vmv":                               # vmv.v.* : vd, source (no vs2)
        if len(ops) < 2:
            return None
        return {"base": "vmv", "form": form, "vd": ops[0], "vs2": None,
                "src3": ops[1], "masked": masked}
    if len(ops) < 3:
        return None
    return {"base": base, "form": form, "vd": ops[0], "vs2": ops[1],
            "src3": ops[2], "masked": masked}


def _elem_list(rec: Dict[str, Any], name: str) -> Optional[List[int]]:
    vregs = rec.get("vregs")
    if not isinstance(vregs, dict):
        return None
    arr = vregs.get(name)
    if not isinstance(arr, list):
        return None
    out = []
    for e in arr:
        iv = _to_int(e)
        if iv is None:
            return None
        out.append(iv)
    return out


def _mask_bits(rec: Dict[str, Any], n: int) -> Optional[List[bool]]:
    vm = rec.get("vmask")
    if isinstance(vm, list):
        return [bool(_to_int(x)) for x in vm][:n]
    v0 = _elem_list(rec, "v0")
    if v0:
        # v0 element i's LSB is the mask bit for element i
        return [(v0[i] & 1) == 1 for i in range(min(n, len(v0)))]
    return None


def decode_vmem(disasm: str) -> Optional[Dict[str, Any]]:
    """
    Parse a vector load/store disasm into
    {op, mode, eew, index_eew, ff, ops}. mode ∈ {unit, strided, indexed}.
    ``eew`` is the data element width in bits (from the mnemonic) for
    unit/strided; for indexed the data width comes from SEW and ``index_eew`` is
    the offset width. Returns None for anything that isn't a vector mem op.
    """
    if not disasm or not isinstance(disasm, str):
        return None
    toks = disasm.replace(",", " ").split()
    if not toks:
        return None
    mnem = toks[0].lower()
    if not (mnem.startswith("vl") or mnem.startswith("vs")):
        return None
    bm = mnem.split(".")[0]
    digits = re.sub(r"\D", "", bm)
    ff = "ff" in bm
    if "ei" in bm:                                  # indexed: vl{u,o}xei / vs{u,o}xei
        mode, eew, index_eew = "indexed", None, (int(digits) if digits else None)
    elif bm.startswith("vlse") or bm.startswith("vsse"):
        mode, eew, index_eew = "strided", (int(digits) if digits else None), None
    elif re.match(r"v[ls]e\d+", bm):                # unit-stride: vle{N} / vse{N}
        mode, eew, index_eew = "unit", (int(digits) if digits else None), None
    else:
        return None
    op = "load" if mnem.startswith("vl") else "store"
    ops = [o for o in toks[1:] if "v0.t" not in o.lower()]
    return {"op": op, "mode": mode, "eew": eew, "index_eew": index_eew,
            "ff": ff, "ops": ops}


# ─────────────────────────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────────────────────────
_VSET = ("vsetvli", "vsetivli", "vsetvl")


class VectorVerifier:
    def __init__(self, rtl_log: Sequence[Dict[str, Any]],
                 iss_log: Optional[Sequence[Dict[str, Any]]] = None,
                 vlen: int = DEFAULT_VLEN):
        self.rtl = list(rtl_log or [])
        self.iss = list(iss_log or [])
        self.vlen = int(vlen) if vlen else DEFAULT_VLEN
        self.violations: List[Dict[str, Any]] = []
        self.metrics = {
            "vector_instrs": 0, "vset_ops": 0, "arith_ops": 0, "masked_ops": 0,
            "vl_sum": 0, "vl_count": 0, "active_elements": 0,
            "mem_ops": 0, "mem_loads": 0, "mem_stores": 0, "mem_elements": 0,
            "sew_hist": {}, "lmul_hist": {},
        }

    # -- violation helper -----------------------------------------------------
    def _v(self, seq: int, check: str, sev: str, detail: str, **extra) -> None:
        self.violations.append({"seq": seq, "check": check, "severity": sev,
                                "detail": detail, **extra})

    # -- vset check -----------------------------------------------------------
    def _check_vset(self, rec: Dict[str, Any], seq: int) -> None:
        vt = decode_vtype(rec.get("vtype"))
        vl = _to_int(rec.get("vl"))
        if vt is None or vl is None:
            return
        vlen = _to_int(rec.get("vlen")) or self.vlen
        self.metrics["vset_ops"] += 1

        # vill handling
        dut_vill = bool(rec.get("vill", vt.get("vill", False)))
        golden_vill = vt.get("golden_vill", False)
        if golden_vill and not dut_vill:
            self._v(seq, "vtype_vill", "HIGH",
                    f"illegal vtype (sew={vt['sew']}, lmul={vt['lmul']}) but vill not set")
            return
        if dut_vill:
            if vl != 0:
                self._v(seq, "vtype_vill", "HIGH",
                        f"vill set but vl={vl} (must be 0)")
            return
        if vt["sew"] is None or vt["lmul"] is None:
            return

        vmax = vlmax(vt["sew"], vt["lmul"], vlen)
        # metrics
        self.metrics["vl_sum"] += vl
        self.metrics["vl_count"] += 1
        self.metrics["sew_hist"][str(vt["sew"])] = \
            self.metrics["sew_hist"].get(str(vt["sew"]), 0) + 1
        self.metrics["lmul_hist"][str(vt["lmul"])] = \
            self.metrics["lmul_hist"].get(str(vt["lmul"]), 0) + 1

        if vl < 0 or vl > vmax:
            self._v(seq, "vset_vl", "HIGH",
                    f"vl={vl} outside [0, VLMAX={vmax}] (sew={vt['sew']}, lmul={vt['lmul']}, vlen={vlen})")
            return
        avl = _to_int(rec.get("avl"))
        if avl is None:
            return
        if avl <= vmax:
            if vl != avl:
                self._v(seq, "vset_vl", "HIGH",
                        f"AVL={avl} ≤ VLMAX={vmax} requires vl==AVL, got {vl}")
        elif avl >= 2 * vmax:
            if vl != vmax:
                self._v(seq, "vset_vl", "HIGH",
                        f"AVL={avl} ≥ 2·VLMAX requires vl==VLMAX={vmax}, got {vl}")
        else:  # impl-defined band: ceil(AVL/2) ≤ vl ≤ VLMAX
            lo = math.ceil(avl / 2)
            if not (lo <= vl <= vmax):
                self._v(seq, "vset_vl", "HIGH",
                        f"AVL={avl} in [VLMAX,2·VLMAX): vl must be in [{lo},{vmax}], got {vl}")

    # -- element arithmetic check --------------------------------------------
    def _check_arith(self, rec: Dict[str, Any], seq: int) -> None:
        dec = decode_vector_alu(rec.get("disasm", ""))
        if not dec:
            return
        self.metrics["arith_ops"] += 1
        if dec["masked"]:
            self.metrics["masked_ops"] += 1

        vt = decode_vtype(rec.get("vtype"))
        vl = _to_int(rec.get("vl"))
        if vt is None or vl is None or vt.get("sew") is None:
            return
        sew = vt["sew"]

        vs2 = _elem_list(rec, dec["vs2"]) if dec["vs2"] else None
        vd = _elem_list(rec, dec["vd"])
        if vd is None or (dec["base"] != "vmv" and vs2 is None):
            return

        # resolve third operand per form
        if dec["form"] == "vv":
            src = _elem_list(rec, dec["src3"])
            if src is None:
                return
            scalar = None
        elif dec["form"] == "vx":
            regs = rec.get("regs", {})
            scalar = _to_int(regs.get(dec["src3"])) if isinstance(regs, dict) else None
            if scalar is None:
                return
            src = None
        else:  # vi
            scalar = _to_int(dec["src3"])
            if scalar is None:
                return
            src = None

        mask = _mask_bits(rec, vl) if dec["masked"] else None
        lens = [vl, len(vd)]
        if vs2 is not None:
            lens.append(len(vs2))
        if src is not None:
            lens.append(len(src))
        n = min(lens)
        checked = 0
        for i in range(n):
            if mask is not None and i < len(mask) and not mask[i]:
                if dec["base"] == "vmerge" and vs2 is not None:
                    # merge: masked-off element takes vs2 (the "false" operand)
                    exp = vs2[i] & _umask(sew)
                    if (vd[i] & _umask(sew)) != exp:
                        self._v(seq, "velem", "HIGH",
                                f"{dec['base']} elem[{i}] merge-false: exp {hex(exp)} got {hex(vd[i] & _umask(sew))}")
                    checked += 1
                continue  # other masked-off elements: agnostic, skip
            b = src[i] if src is not None else scalar
            a = (vs2[i] & _umask(sew)) if vs2 is not None else 0
            exp = velem_compute(dec["base"], sew, a, b & _umask(sew))
            if exp is None:
                return
            got = vd[i] & _umask(sew)
            checked += 1
            if got != exp:
                self._v(seq, "velem", "HIGH",
                        f"{dec['base']}.{dec['form']} elem[{i}] sew{sew}: exp {hex(exp)} got {hex(got)}")
                if len([v for v in self.violations if v["check"] == "velem"]) > 64:
                    break
        self.metrics["active_elements"] += checked

        # tail-undisturbed check (vta == 0)
        if vt.get("vta", 1) == 0:
            prev = rec.get("vstate_prev", {})
            pv = prev.get(dec["vd"]) if isinstance(prev, dict) else None
            if isinstance(pv, list):
                vmaxv = vlmax(sew, vt["lmul"], _to_int(rec.get("vlen")) or self.vlen) \
                    if vt.get("lmul") else len(vd)
                for i in range(vl, min(vmaxv, len(vd), len(pv))):
                    if (vd[i] & _umask(sew)) != (_to_int(pv[i]) or 0) & _umask(sew):
                        self._v(seq, "vtail", "MEDIUM",
                                f"tail elem[{i}] disturbed under vta=0")
                        break

    # -- vector load/store check ---------------------------------------------
    def _check_vmem(self, rec: Dict[str, Any], seq: int) -> None:
        dec = decode_vmem(rec.get("disasm", ""))
        if not dec:
            return
        self.metrics["mem_ops"] += 1
        self.metrics["mem_loads" if dec["op"] == "load" else "mem_stores"] += 1

        vt = decode_vtype(rec.get("vtype"))
        vl = _to_int(rec.get("vl"))
        if vt is None or vl is None:
            return
        sew = vt.get("sew")
        data_eew = dec["eew"] or sew                 # indexed uses SEW for data
        if not data_eew or data_eew % 8:
            return
        ebytes = data_eew // 8
        regs = rec.get("regs", {}) if isinstance(rec.get("regs"), dict) else {}
        vmemf = rec.get("vmem") if isinstance(rec.get("vmem"), dict) else {}
        ops = dec["ops"]
        if not ops:
            return

        # base address + the extra operand (stride reg or index vreg)
        base = _to_int(vmemf.get("base"))
        extra = None
        for o in ops[1:]:
            o2 = o.strip()
            if o2.startswith("(") and o2.endswith(")"):
                if base is None:
                    base = _to_int(regs.get(o2[1:-1]))
            else:
                extra = o2
        if base is None:
            return

        masked = "v0.t" in rec.get("disasm", "").lower()
        mask = _mask_bits(rec, vl) if masked else None

        idx = None
        if dec["mode"] == "indexed":
            idx = _elem_list(rec, extra) if extra else None
            if idx is None:
                return
        stride = None
        if dec["mode"] == "strided":
            stride = _to_int(vmemf.get("stride"))
            if stride is None and extra:
                stride = _to_int(regs.get(extra))
            if stride is None:
                return

        # golden per-element addresses for active, unmasked elements
        golden: List[int] = []
        active_idx: List[int] = []
        imask = (1 << (dec["index_eew"] or data_eew)) - 1
        for i in range(vl):
            if mask is not None and i < len(mask) and not mask[i]:
                continue
            if dec["mode"] == "unit":
                a = base + i * ebytes
            elif dec["mode"] == "strided":
                a = base + i * stride
            else:  # indexed
                if i >= len(idx):
                    break
                a = base + (idx[i] & imask)
            golden.append(a)
            active_idx.append(i)
        self.metrics["mem_elements"] += len(golden)

        # DUT-observed access addresses (explicit list, else mem_reads/writes)
        acc = rec.get("mem_reads") if dec["op"] == "load" else rec.get("mem_writes")
        if isinstance(vmemf.get("addrs"), list):
            dut_addrs = [_to_int(x) for x in vmemf["addrs"]]
        elif isinstance(acc, list):
            dut_addrs = [_to_int(a.get("addr")) for a in acc if isinstance(a, dict)]
        else:
            dut_addrs = None
        if dut_addrs is None:
            return
        dut_addrs = [a for a in dut_addrs if a is not None]

        # active-element access count (no spurious / no missing accesses)
        if len(dut_addrs) != len(golden):
            self._v(seq, "vmem_count", "HIGH",
                    f"{dec['mode']} {dec['op']}: {len(dut_addrs)} accesses vs "
                    f"{len(golden)} active elements")
        # address correctness (order-independent set comparison)
        if set(dut_addrs) != set(golden):
            miss = sorted(set(golden) - set(dut_addrs))[:4]
            xtra = sorted(set(dut_addrs) - set(golden))[:4]
            self._v(seq, "vmem_addr", "HIGH",
                    f"{dec['mode']} {dec['op']} addr mismatch: "
                    f"missing {[hex(x) for x in miss]} extra {[hex(x) for x in xtra]}")

        # access-size (EEW) check
        if isinstance(acc, list):
            for a in acc:
                if isinstance(a, dict):
                    sz = _to_int(a.get("size"))
                    if sz is not None and sz != ebytes:
                        self._v(seq, "vmem_eew", "MEDIUM",
                                f"access size {sz} != EEW bytes {ebytes}")
                        break

        # value check: loaded/stored element vs memory value at that address
        vd = vmemf.get("vd") or ops[0]
        velems = _elem_list(rec, vd)
        if velems is not None and isinstance(acc, list):
            memmap: Dict[int, int] = {}
            for a in acc:
                if isinstance(a, dict):
                    ai, av = _to_int(a.get("addr")), _to_int(a.get("value"))
                    if ai is not None and av is not None:
                        memmap[ai] = av
            me = _umask(data_eew)
            for k, i in enumerate(active_idx):
                if i < len(velems) and golden[k] in memmap:
                    if (velems[i] & me) != (memmap[golden[k]] & me):
                        self._v(seq, "vmem_value", "HIGH",
                                f"{dec['op']} elem[{i}] @ {hex(golden[k])}: "
                                f"reg {hex(velems[i] & me)} mem {hex(memmap[golden[k]] & me)}")
                        break

    # -- driver ---------------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        started = _now()
        for seq, rec in enumerate(self.rtl):
            if not isinstance(rec, dict):
                continue
            dis = rec.get("disasm", "")
            mnem = dis.split()[0].lower() if isinstance(dis, str) and dis.split() else ""
            is_vec = mnem.startswith("v") and (mnem in _VSET or "." in mnem
                                               or rec.get("vtype") is not None)
            if is_vec:
                self.metrics["vector_instrs"] += 1
            if mnem in _VSET or (rec.get("vl") is not None and rec.get("avl") is not None):
                self._check_vset(rec, seq)
            if "." in mnem and mnem.startswith("v"):
                self._check_arith(rec, seq)
            if "." in mnem and mnem.startswith(("vl", "vs")):
                self._check_vmem(rec, seq)

        total = len(self.violations)
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        med = sum(1 for v in self.violations if v["severity"] == "MEDIUM")
        severity_score = high * 3 + med
        if self.metrics["vl_count"]:
            self.metrics["mean_vl"] = round(
                self.metrics["vl_sum"] / self.metrics["vl_count"], 3)
        else:
            self.metrics["mean_vl"] = 0.0
        band = ("CLEAN" if total == 0 else
                "CRITICAL" if high else
                "DEGRADED" if med else "MINOR")
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.rtl),
            "vector_active": self.metrics["vector_instrs"] > 0,
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "medium_violations": med,
            "severity_score": severity_score,
            "band": band,
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def run_from_manifest(manifest_path: str) -> int:
    """Standalone entry: load the RTL commit log, run the checker, write
    ``vector_report.json``. Returns 0 on pass / skip, 1 on HIGH violations."""
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("vector_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    outputs = manifest.get("outputs", {})
    rtl_name = outputs.get("rtl_commit_log", "rtl_commit.jsonl")
    rtl = _load_jsonl(run_dir / rtl_name)
    vlen = _to_int((manifest.get("metrics", {}) or {}).get("vlen")) or DEFAULT_VLEN
    rep = VectorVerifier(rtl, vlen=vlen).run()
    try:
        (run_dir / "vector_report.json").write_text(json.dumps(rep, indent=2),
                                                     encoding="utf-8")
    except OSError as exc:
        log.warning("vector_verifier: cannot write report: %s", exc)
    return 0 if rep["pass"] else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA RISC-V Vector (RVV) verifier")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
