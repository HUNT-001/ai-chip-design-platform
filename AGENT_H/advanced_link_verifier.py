"""
AGENT_H.advanced_link_verifier — Advanced Interconnects (T70, level 3)
=======================================================================

Protocol checkers for the high-speed / coherent interconnects: **PCIe, CXL,
UCIe, CCIX, NVLink, OpenCAPI, Ethernet MAC and NoC**.

Scope — read this first
-----------------------
These are enormous specifications; this agent deliberately targets the
**link-layer and transaction-layer invariants that are protocol-defining and
checkable from a trace** — sequence numbering, credit/flow control, CRC/FEC
integrity, ordering rules, LTSSM/training state legality, virtual-channel
independence and coherent-message pairing. It is **not** a compliance suite: it
will not replace a PCIe-SIG or CXL compliance run, and it does not model
electrical/PHY behaviour, equalisation, or every optional feature. What it does
model, it models against the actual rules.

Common link-layer checks (PCIe / CXL / CCIX / NVLink / OpenCAPI / UCIe)
-----------------------------------------------------------------------
- **link_seq_gap** (HIGH) — TLP/flit sequence number skipped or went backwards
  (a lost or reordered packet).
- **link_seq_duplicate** (HIGH) — the same sequence number transmitted twice
  without an intervening replay.
- **link_crc_undetected** (HIGH) — a packet with an injected CRC/LCRC/FEC error
  was accepted as good.
- **link_credit_overflow** (HIGH) — the transmitter sent more than the available
  flow-control credits (posted / non-posted / completion, or VC credits).
- **link_credit_leak** (MEDIUM) — credits returned exceed credits consumed.
- **link_ack_protocol** (HIGH) — an ACK/NAK for a sequence never sent, or a NAK
  that did not trigger a replay.
- **link_state** (HIGH) — an illegal training/LTSSM state transition.

Protocol-specific
-----------------
- **pcie_ordering** (HIGH) — a posted write passed an earlier posted write to
  the same address, or a completion passed a posted write (PCIe producer/
  consumer ordering rules).
- **cxl_type_mismatch** (HIGH) — a CXL.cache/CXL.mem message on a device type
  that does not support it (Type-1 has no .mem, Type-3 has no .cache).
- **cxl_coherence** (HIGH) — a coherent response that does not pair with its
  request (e.g. a snoop with no matching outstanding request).
- **ucie_module_config** (HIGH) — a UCIe lane/module configuration outside the
  declared width, or a sideband message during an incompatible link state.
- **ethernet_frame** (HIGH) — frame shorter than 64B or longer than the declared
  MTU/jumbo limit, bad FCS accepted, or an inter-packet gap below the minimum.
- **noc_deadlock** (HIGH) — a cyclic dependency across virtual channels in the
  routing/turn graph (the classic NoC deadlock condition), or a packet whose
  route violates the declared turn model.
- **noc_vc_independence** (HIGH) — a blocked virtual channel stalled traffic on
  an independent VC (head-of-line blocking across VCs).

Trace contract — `advlink_trace.jsonl` (additive; skipped when absent)
----------------------------------------------------------------------
```
{"event":"link","proto":"pcie","seq":0,"crc_error_injected":false,"accepted":true}
{"event":"credit","proto":"pcie","vc":0,"kind":"posted","consumed":1,"available":4}
{"event":"ltssm","proto":"pcie","from":"Detect","to":"Polling"}
{"event":"order","proto":"pcie","kind":"posted","addr":"0x100","id":1}
{"event":"cxl","msg":"mem_rd","device_type":3}
{"event":"ethernet","length":64,"fcs_ok":true,"ipg":12}
{"event":"noc","packet":1,"route":[[0,0],[1,0],[1,1]],"vc":0,
 "turn_model":"xy"}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.advlink")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "advanced_link_verifier"

ETH_MIN_FRAME = 64
ETH_DEFAULT_MTU = 1518
ETH_MIN_IPG = 12                       # bytes of inter-packet gap

# PCIe LTSSM legal transitions (major states; a practical subset).
_LTSSM: Dict[str, Set[str]] = {
    "Detect": {"Detect", "Polling"},
    "Polling": {"Polling", "Configuration", "Detect", "Disabled"},
    "Configuration": {"Configuration", "L0", "Detect", "Recovery", "Disabled"},
    "L0": {"L0", "Recovery", "L0s", "L1", "L2", "Detect"},
    "L0s": {"L0s", "L0", "Recovery", "Detect"},
    "L1": {"L1", "Recovery", "L0", "L2", "Detect"},
    "L2": {"L2", "Detect"},
    "Recovery": {"Recovery", "L0", "Configuration", "Detect", "Disabled"},
    "Disabled": {"Disabled", "Detect"},
    "Loopback": {"Loopback", "Detect"},
    "HotReset": {"HotReset", "Detect"},
}


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


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def find_cycle(edges: Dict[Any, Set[Any]]) -> Optional[List[Any]]:
    """Return a cycle in a directed graph as a node list, or None. Used for the
    NoC channel-dependency deadlock check."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour: Dict[Any, int] = {n: WHITE for n in edges}
    parent: Dict[Any, Any] = {}

    def walk(u: Any) -> Optional[List[Any]]:
        colour[u] = GREY
        for v in edges.get(u, ()):  # noqa: B020
            if v not in colour:
                colour[v] = WHITE
            if colour[v] == WHITE:
                parent[v] = u
                got = walk(v)
                if got:
                    return got
            elif colour[v] == GREY:
                cyc = [v, u]
                x = u
                while x in parent and parent[x] != v:
                    x = parent[x]
                    cyc.append(x)
                cyc.append(v)
                return list(reversed(cyc))
        colour[u] = BLACK
        return None

    for n in list(edges):
        if colour.get(n, WHITE) == WHITE:
            got = walk(n)
            if got:
                return got
    return None


class AdvancedLinkVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.seq_seen: Dict[str, Set[int]] = {}
        self.seq_last: Dict[str, int] = {}
        self.sent_seq: Dict[str, Set[int]] = {}
        self.credits: Dict[Tuple[str, Any, str], Dict[str, int]] = {}
        self.ltssm: Dict[str, str] = {}
        self.posted: List[Dict[str, Any]] = []
        self.cxl_outstanding: Set[Any] = set()
        self.noc_edges: Dict[Any, Set[Any]] = {}
        self.metrics: Dict[str, Any] = {
            "link_packets": 0, "credit_events": 0, "ltssm_events": 0,
            "eth_frames": 0, "noc_packets": 0, "cxl_msgs": 0,
            "checked": 0, "advlink_active": False, "by_proto": {},
        }

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    def _bump(self, proto: str) -> None:
        self.metrics["advlink_active"] = True
        self.metrics["checked"] += 1
        self.metrics["by_proto"][proto] = \
            self.metrics["by_proto"].get(proto, 0) + 1

    # ── main ───────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            seq = e.get("seq")
            if kind == "link":
                self._link(e, seq)
            elif kind == "credit":
                self._credit(e, seq)
            elif kind in ("ltssm", "linkstate", "training"):
                self._ltssm(e, seq)
            elif kind == "ack":
                self._ack(e, seq)
            elif kind == "order":
                self._order(e, seq)
            elif kind == "cxl":
                self._cxl(e, seq)
            elif kind == "ucie":
                self._ucie(e, seq)
            elif kind in ("ethernet", "eth"):
                self._ethernet(e, seq)
            elif kind == "noc":
                self._noc(e, seq)
        self._noc_final()
        return self._report(started)

    # ── generic link layer ─────────────────────────────────────────────────
    def _link(self, e: Dict[str, Any], _s: Any) -> None:
        proto = str(e.get("proto", "link")).lower()
        self.metrics["link_packets"] += 1
        self._bump(proto)
        n = _to_int(e.get("seq"))
        if n is not None:
            seen = self.seq_seen.setdefault(proto, set())
            last = self.seq_last.get(proto)
            if n in seen and not _truthy(e.get("replay")):
                self._v(n, "link_seq_duplicate",
                        f"{proto}: sequence {n} transmitted twice with no replay")
            elif last is not None and n != last + 1 and not _truthy(e.get("replay")):
                if n < last:
                    self._v(n, "link_seq_gap",
                            f"{proto}: sequence went backwards {last} -> {n}")
                else:
                    self._v(n, "link_seq_gap",
                            f"{proto}: sequence gap {last} -> {n} "
                            f"({n - last - 1} packet(s) lost)")
            seen.add(n)
            self.sent_seq.setdefault(proto, set()).add(n)
            self.seq_last[proto] = n
        # CRC / FEC integrity
        if _truthy(e.get("crc_error_injected")) and _truthy(e.get("accepted")):
            self._v(n, "link_crc_undetected",
                    f"{proto}: packet with an injected CRC/FEC error was "
                    f"accepted as good")

    def _credit(self, e: Dict[str, Any], seq: Any) -> None:
        proto = str(e.get("proto", "link")).lower()
        self.metrics["credit_events"] += 1
        self._bump(proto)
        key = (proto, e.get("vc", 0), str(e.get("kind", "posted")))
        st = self.credits.setdefault(key, {"consumed": 0, "returned": 0})
        consumed = _to_int(e.get("consumed")) or 0
        returned = _to_int(e.get("returned")) or 0
        avail = _to_int(e.get("available"))
        st["consumed"] += consumed
        st["returned"] += returned
        outstanding = st["consumed"] - st["returned"]
        if avail is not None and consumed and outstanding > avail:
            self._v(seq, "link_credit_overflow",
                    f"{proto} VC{key[1]} {key[2]}: {outstanding} credits "
                    f"outstanding exceeds the {avail} advertised")
        if st["returned"] > st["consumed"]:
            self._v(seq, "link_credit_leak",
                    f"{proto} VC{key[1]} {key[2]}: returned {st['returned']} "
                    f"credits but only consumed {st['consumed']}",
                    severity="MEDIUM")

    def _ltssm(self, e: Dict[str, Any], seq: Any) -> None:
        proto = str(e.get("proto", "pcie")).lower()
        self.metrics["ltssm_events"] += 1
        self._bump(proto)
        frm = str(e.get("from", self.ltssm.get(proto, "")))
        to = str(e.get("to", ""))
        table = e.get("legal")
        if isinstance(table, dict):
            allowed = set(table.get(frm, []))
        else:
            allowed = _LTSSM.get(frm, set())
        if frm and to and allowed and to not in allowed:
            self._v(seq, "link_state",
                    f"{proto}: illegal link-state transition {frm} -> {to}")
        if to:
            self.ltssm[proto] = to

    def _ack(self, e: Dict[str, Any], seq: Any) -> None:
        proto = str(e.get("proto", "link")).lower()
        self._bump(proto)
        n = _to_int(e.get("seq"))
        sent = self.sent_seq.get(proto, set())
        if n is not None and n not in sent:
            self._v(seq, "link_ack_protocol",
                    f"{proto}: ACK/NAK for sequence {n} which was never sent")
        if _truthy(e.get("nak")) and not _truthy(e.get("replay_triggered"), True):
            self._v(seq, "link_ack_protocol",
                    f"{proto}: NAK for sequence {n} did not trigger a replay")

    # ── PCIe ordering ──────────────────────────────────────────────────────
    def _order(self, e: Dict[str, Any], seq: Any) -> None:
        proto = str(e.get("proto", "pcie")).lower()
        self._bump(proto)
        kind = str(e.get("kind", "")).lower()
        addr = e.get("addr")
        idx = _to_int(e.get("id"))
        if kind == "posted":
            for p in self.posted:
                if p["addr"] == addr and idx is not None and idx < p["id"]:
                    self._v(seq, "pcie_ordering",
                            f"posted write id {idx} passed earlier posted write "
                            f"id {p['id']} to the same address {addr}")
            self.posted.append({"addr": addr, "id": idx if idx is not None else 0})
        elif kind == "completion":
            for p in self.posted:
                if idx is not None and p["id"] > idx:
                    self._v(seq, "pcie_ordering",
                            f"completion id {idx} passed posted write id "
                            f"{p['id']} (violates producer/consumer ordering)")
                    break

    # ── CXL ────────────────────────────────────────────────────────────────
    def _cxl(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["cxl_msgs"] += 1
        self._bump("cxl")
        msg = str(e.get("msg", "")).lower()
        dtype = _to_int(e.get("device_type"))
        if dtype == 1 and msg.startswith("mem"):
            self._v(seq, "cxl_type_mismatch",
                    f"CXL Type-1 device issued a CXL.mem message '{msg}' "
                    f"(Type-1 supports .cache/.io only)")
        if dtype == 3 and msg.startswith("cache"):
            self._v(seq, "cxl_type_mismatch",
                    f"CXL Type-3 device issued a CXL.cache message '{msg}' "
                    f"(Type-3 supports .mem/.io only)")
        tag = e.get("tag")
        if msg.endswith("_req") and tag is not None:
            self.cxl_outstanding.add(tag)
        elif msg.endswith(("_rsp", "_data")) and tag is not None:
            if tag not in self.cxl_outstanding:
                self._v(seq, "cxl_coherence",
                        f"CXL response '{msg}' tag {tag} has no matching "
                        f"outstanding request")
            else:
                self.cxl_outstanding.discard(tag)
        elif msg.startswith("snoop") and tag is not None:
            if tag not in self.cxl_outstanding and not _truthy(e.get("unsolicited_ok")):
                self._v(seq, "cxl_coherence",
                        f"CXL snoop tag {tag} with no matching outstanding "
                        f"request")

    # ── UCIe ───────────────────────────────────────────────────────────────
    def _ucie(self, e: Dict[str, Any], seq: Any) -> None:
        self._bump("ucie")
        lanes = _to_int(e.get("active_lanes"))
        width = _to_int(e.get("module_width"))
        if lanes is not None and width is not None and lanes > width:
            self._v(seq, "ucie_module_config",
                    f"UCIe: {lanes} active lanes exceeds the module width "
                    f"{width}")
        if _truthy(e.get("sideband")) and \
                str(e.get("state", "")).lower() in ("reset", "off"):
            self._v(seq, "ucie_module_config",
                    f"UCIe: sideband message issued while the link is in "
                    f"'{e.get('state')}'")

    # ── Ethernet MAC ───────────────────────────────────────────────────────
    def _ethernet(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["eth_frames"] += 1
        self._bump("ethernet")
        ln = _to_int(e.get("length"))
        mtu = _to_int(e.get("mtu")) or ETH_DEFAULT_MTU
        if ln is not None:
            if ln < ETH_MIN_FRAME and not _truthy(e.get("runt_expected")):
                self._v(seq, "ethernet_frame",
                        f"frame length {ln} below the {ETH_MIN_FRAME}-byte "
                        f"minimum (runt)")
            if ln > mtu:
                self._v(seq, "ethernet_frame",
                        f"frame length {ln} exceeds the MTU {mtu} (giant)")
        if "fcs_ok" in e and not _truthy(e.get("fcs_ok")) and \
                _truthy(e.get("accepted")):
            self._v(seq, "ethernet_frame",
                    "frame with a bad FCS was accepted")
        ipg = _to_int(e.get("ipg"))
        if ipg is not None and ipg < ETH_MIN_IPG:
            self._v(seq, "ethernet_frame",
                    f"inter-packet gap {ipg} below the {ETH_MIN_IPG}-byte "
                    f"minimum")

    # ── NoC ────────────────────────────────────────────────────────────────
    def _noc(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["noc_packets"] += 1
        self._bump("noc")
        route = e.get("route")
        vc = e.get("vc", 0)
        model = str(e.get("turn_model", "")).lower()
        if isinstance(route, (list, tuple)) and len(route) >= 2:
            hops = [tuple(h) if isinstance(h, (list, tuple)) else h for h in route]
            # channel dependency edges (channel = (hop_pair, vc))
            for a, b in zip(hops, hops[1:]):
                self.noc_edges.setdefault((a, b, vc), set())
            for (a, b), (c, d) in zip(zip(hops, hops[1:]), zip(hops[1:], hops[2:])):
                self.noc_edges.setdefault((a, b, vc), set()).add((c, d, vc))
            # XY (dimension-ordered) turn model: all X movement before any Y
            if model in ("xy", "dor", "dimension_ordered"):
                seen_y = False
                for a, b in zip(hops, hops[1:]):
                    if not (isinstance(a, tuple) and isinstance(b, tuple)
                            and len(a) >= 2 and len(b) >= 2):
                        continue
                    dy = b[1] != a[1]
                    dx = b[0] != a[0]
                    if dy:
                        seen_y = True
                    elif dx and seen_y:
                        self._v(seq, "noc_deadlock",
                                f"packet {e.get('packet')} violates the XY turn "
                                f"model: X hop {a}->{b} after a Y turn")
                        break
        if _truthy(e.get("blocked")) and e.get("blocked_vc") is not None \
                and e.get("blocked_vc") != vc:
            self._v(seq, "noc_vc_independence",
                    f"packet on VC{vc} stalled by a block on VC"
                    f"{e.get('blocked_vc')} (VCs must be independent)")

    def _noc_final(self) -> None:
        if not self.noc_edges:
            return
        cyc = find_cycle(self.noc_edges)
        if cyc:
            self._v(None, "noc_deadlock",
                    f"cyclic channel dependency across the routing graph "
                    f"(deadlock): {' -> '.join(str(c) for c in cyc[:6])}"
                    f"{' ...' if len(cyc) > 6 else ''}")

    # ── report ─────────────────────────────────────────────────────────────
    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "advlink_active": self.metrics["advlink_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3 + (total - high),
            "band": "CLEAN" if total == 0 else ("CRITICAL" if high else "DEGRADED"),
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("advlink_trace",
                                                    "advlink_trace.jsonl")
    p = run_dir / name
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("advanced_link_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no advlink_trace", "pass": True}
    else:
        rep = AdvancedLinkVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "advlink_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("advanced_link_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA advanced-interconnect checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
