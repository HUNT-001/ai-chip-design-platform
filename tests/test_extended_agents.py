"""
Extended-tier agent tests — split out of test_agents.py (2026-07-09).

Holds the newest system-integration + virtualization + interrupt-architecture
agents (T50 Hypervisor, T51 AIA/IMSIC) plus the Phase-6 end-to-end integration
test. Split so no single test module exceeds the workspace mount's file-serving
cap and to keep the suite maintainable (ADR-001 action item #5). Runs with the
same command as the rest of the suite:

    pytest tests/ --import-mode=importlib -p no:cacheprovider -q
"""

import json

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# T50 — Hypervisor Two-Stage Translation Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestHypervisorVerifier:
    _CFG = {"op": "config",
            "vs_map": {"0x80": {"gpn": "0x120", "r": True, "w": True,
                                "x": True, "v": True}},
            "g_map": {"0x120": {"ppn": "0x300", "r": True, "w": True,
                                "x": True, "v": True}}}

    def _run(self, evs):
        from AGENT_H.hypervisor_verifier import HypervisorVerifier
        return HypervisorVerifier(evs).run()

    def test_import_and_clean(self):
        from AGENT_H import hypervisor_verifier as hv
        assert hasattr(hv, "HypervisorVerifier") and hasattr(hv, "TwoStageMMU")
        # gva 0x80abc → ppn 0x300 | 0xabc = 0x300abc
        assert self._run([self._CFG, {"op": "translate", "gva": "0x80abc",
                                      "access": "load", "pa": "0x300abc"}])["pass"]

    def test_vs_stage_page_fault(self):
        # vpn 0x99 unmapped → load page fault 13
        assert self._run([self._CFG, {"op": "translate", "gva": "0x99000",
                                      "access": "load", "fault": 13}])["pass"]
        assert not self._run([self._CFG, {"op": "translate", "gva": "0x99000",
                                          "access": "load", "pa": "0x1000"}])["pass"]

    def test_vs_perm_fault(self):
        cfg = {"op": "config",
               "vs_map": {"0x80": {"gpn": "0x120", "r": True, "w": False,
                                   "x": True, "v": True}},
               "g_map": {"0x120": {"ppn": "0x300", "r": True, "w": True,
                                   "x": True, "v": True}}}
        assert self._run([cfg, {"op": "translate", "gva": "0x80000",
                                "access": "store", "fault": 15}])["pass"]

    def test_g_stage_guest_page_fault(self):
        miss = {"op": "config",
                "vs_map": {"0x80": {"gpn": "0x999", "r": True, "w": True,
                                    "x": True, "v": True}},
                "g_map": {"0x120": {"ppn": "0x300", "r": True, "v": True}}}
        # gpn 0x999 unmapped in G-stage → guest page fault 21
        assert self._run([miss, {"op": "translate", "gva": "0x80000",
                                 "access": "load", "fault": 21}])["pass"]
        perm = {"op": "config",
                "vs_map": {"0x80": {"gpn": "0x120", "r": True, "w": True,
                                    "x": True, "v": True}},
                "g_map": {"0x120": {"ppn": "0x300", "r": True, "w": False,
                                    "v": True}}}
        # store where G-stage lacks W → guest page fault 23
        assert self._run([perm, {"op": "translate", "gva": "0x80000",
                                 "access": "store", "fault": 23}])["pass"]

    def test_exec_causes(self):
        cfg = {"op": "config",
               "vs_map": {"0x80": {"gpn": "0x120", "r": True, "x": False,
                                   "v": True}},
               "g_map": {"0x120": {"ppn": "0x300", "x": True, "v": True}}}
        # exec but VS lacks X → instruction page fault 12
        assert self._run([cfg, {"op": "translate", "gva": "0x80000",
                                "access": "exec", "fault": 12}])["pass"]

    def test_spurious_wrong_cause_wrong_pa(self):
        assert any(v["check"] == "htrans_fault" for v in self._run(
            [self._CFG, {"op": "translate", "gva": "0x80abc", "access": "load",
                         "fault": 13}])["violations"])                 # spurious
        assert any(v["check"] == "htrans_fault" for v in self._run(
            [self._CFG, {"op": "translate", "gva": "0x99000", "access": "load",
                         "fault": 21}])["violations"])                 # VS miss is 13 not 21
        assert any(v["check"] == "htrans_result" for v in self._run(
            [self._CFG, {"op": "translate", "gva": "0x80abc", "access": "load",
                         "pa": "0x999abc"}])["violations"])

    def test_robustness_schema(self):
        for evs in ([], [None, 5], [{}], [{"op": "bogus"}]):
            assert self._run(evs)["pass"]
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "hypervisor_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.hypervisor_verifier import run_from_manifest
        evs = [self._CFG, {"op": "translate", "gva": "0x80abc",
                           "access": "load", "pa": "0x999abc"}]
        (tmp_path / "hypervisor_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"hypervisor_trace": "hypervisor_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "hypervisor_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T51 — AIA / IMSIC Checker
# ─────────────────────────────────────────────────────────────────────────────

class TestAIAVerifier:
    def _run(self, evs):
        from AGENT_H.aia_verifier import AIAVerifier
        return AIAVerifier(evs).run()

    @staticmethod
    def _cfg(**kw):
        return {"op": "imsic_config", **kw}

    def test_import(self):
        from AGENT_H import aia_verifier as av
        assert hasattr(av, "AIAVerifier") and hasattr(av, "IMSICModel")

    def test_lowest_identity_wins_and_wrong(self):
        assert self._run([self._cfg(eidelivery=1, eie=[3, 7], eip=[3, 7]),
                          {"op": "imsic_topei", "result": 3}])["pass"]
        assert any(v["check"] == "imsic_topei" for v in self._run(
            [self._cfg(eidelivery=1, eie=[3, 7], eip=[3, 7]),
             {"op": "imsic_topei", "result": 7}])["violations"])

    def test_threshold_masks(self):
        assert self._run([self._cfg(eidelivery=1, eithreshold=5, eie=[3, 7],
                                    eip=[3, 7]),
                          {"op": "imsic_topei", "result": 3}])["pass"]
        r = self._run([self._cfg(eidelivery=1, eithreshold=5, eie=[7], eip=[7]),
                       {"op": "imsic_topei", "result": 7}])   # 7 ≥ 5 masked
        assert not r["pass"]
        assert any(v["check"] in ("imsic_topei", "imsic_threshold")
                   for v in r["violations"])

    def test_delivery_off(self):
        assert self._run([self._cfg(eidelivery=0, eie=[3], eip=[3]),
                          {"op": "imsic_topei", "result": 0}])["pass"]
        assert any(v["check"] in ("imsic_topei", "imsic_delivery") for v in self._run(
            [self._cfg(eidelivery=0, eie=[3], eip=[3]),
             {"op": "imsic_topei", "result": 3}])["violations"])

    def test_disabled_or_not_pending(self):
        assert not self._run([self._cfg(eidelivery=1, eie=[], eip=[3]),
                              {"op": "imsic_topei", "result": 3}])["pass"]
        assert not self._run([self._cfg(eidelivery=1, eie=[3, 5], eip=[5]),
                              {"op": "imsic_topei", "result": 3}])["pass"]
        assert self._run([self._cfg(eidelivery=1, eie=[3], eip=[]),
                          {"op": "imsic_topei", "result": 0}])["pass"]

    def test_robustness_schema(self):
        for evs in ([], [None, 5], [{}], [{"op": "bogus"}]):
            assert self._run(evs)["pass"]
        r = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r
        assert r["agent"] == "aia_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.aia_verifier import run_from_manifest
        evs = [self._cfg(eidelivery=1, eie=[3, 7], eip=[3, 7]),
               {"op": "imsic_topei", "result": 7}]
        (tmp_path / "aia_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"aia_trace": "aia_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "aia_report.json").exists()

    # --- APLIC (direct-delivery): smaller iprio = higher priority ---
    @staticmethod
    def _acfg(**kw):
        return {"op": "aplic_config", **kw}

    def test_aplic_arbitration(self):
        from AGENT_H import aia_verifier as av
        assert hasattr(av, "APLICModel")
        # lowest iprio wins (3@1 beats 5@4)
        assert self._run([self._acfg(idelivery=1, setip=[3, 5], setie=[3, 5],
                                     iprio={"3": 1, "5": 4}),
                          {"op": "aplic_topi", "result": 3}])["pass"]
        # tie iprio → lowest id
        assert self._run([self._acfg(idelivery=1, setip=[3, 5], setie=[3, 5],
                                     iprio={"3": 2, "5": 2}),
                          {"op": "aplic_topi", "result": 3}])["pass"]
        # wrong pick flagged
        assert any(v["check"] == "aplic_topi" for v in self._run(
            [self._acfg(idelivery=1, setip=[3, 5], setie=[3, 5],
                        iprio={"3": 1, "5": 4}),
             {"op": "aplic_topi", "result": 5}])["violations"])

    def test_aplic_threshold_and_inactive(self):
        # ithreshold 3 → iprio4 masked (must be < threshold)
        r = self._run([self._acfg(idelivery=1, ithreshold=3, setip=[5],
                                  setie=[5], iprio={"5": 4}),
                       {"op": "aplic_topi", "result": 5}])
        assert not r["pass"]
        assert any(v["check"] in ("aplic_topi", "aplic_threshold")
                   for v in r["violations"])
        # inactive source never delivered
        assert any(v["check"] in ("aplic_topi", "aplic_inactive") for v in self._run(
            [self._acfg(idelivery=1, setip=[3], setie=[3], iprio={"3": 1},
                        sourcecfg={"3": "inactive"}),
             {"op": "aplic_topi", "result": 3}])["violations"])
        # delegated source → not delivered in this domain
        assert self._run([self._acfg(idelivery=1, setip=[3], setie=[3],
                                     iprio={"3": 1}, sourcecfg={"3": "delegated"}),
                          {"op": "aplic_topi", "result": 0}])["pass"]

    def test_aplic_delivery_and_iprio0(self):
        assert self._run([self._acfg(idelivery=0, setip=[3], setie=[3],
                                     iprio={"3": 1}),
                          {"op": "aplic_topi", "result": 0}])["pass"]
        assert any(v["check"] in ("aplic_topi", "aplic_delivery") for v in self._run(
            [self._acfg(idelivery=0, setip=[3], setie=[3], iprio={"3": 1}),
             {"op": "aplic_topi", "result": 3}])["violations"])
        # iprio 0 is reserved → treated as 1 (highest), eligible under threshold 2
        assert self._run([self._acfg(idelivery=1, ithreshold=2, setip=[3],
                                     setie=[3], iprio={"3": 0}),
                          {"op": "aplic_topi", "result": 3}])["pass"]


# ─────────────────────────────────────────────────────────────────────────────
# T52 — Out-of-Order Scoreboard Checker
# ─────────────────────────────────────────────────────────────────────────────

def _orec(seq, dis, issue=None, complete=None, commit=None, **kw):
    o = {}
    for k, v in (("issue", issue), ("complete", complete), ("commit", commit)):
        if v is not None:
            o[k] = v
    o.update(kw)
    return {"schema_version": "2.1.0", "seq": seq, "pc": hex(0x80000000 + seq * 4),
            "disasm": dis, "regs": {}, "ooo": o}


class TestOOOVerifier:
    def _run(self, rs):
        from AGENT_H.ooo_verifier import OOOVerifier
        return OOOVerifier(rs).run()

    def test_import_and_clean(self):
        from AGENT_H import ooo_verifier as ov
        assert hasattr(ov, "OOOVerifier")
        rs = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=3),
              _orec(1, "add x8,x5,x9", issue=2, complete=3, commit=4)]
        r = self._run(rs)
        assert r["pass"] and r["ooo_active"]

    def test_commit_order_and_raw(self):
        ooo = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=4),
               _orec(1, "add x8,x1,x9", issue=1, complete=2, commit=3)]
        assert any(v["check"] == "ooo_commit_order"
                   for v in self._run(ooo)["violations"])
        raw = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=3),
               _orec(1, "add x8,x5,x9", issue=1, complete=3, commit=4)]  # issue<producer complete
        assert any(v["check"] == "ooo_raw_hazard"
                   for v in self._run(raw)["violations"])
        # forwarding: issue exactly at producer complete is fine
        ok = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=3),
              _orec(1, "add x8,x5,x9", issue=2, complete=3, commit=4)]
        assert self._run(ok)["pass"]

    def test_exec_timing(self):
        assert any(v["check"] == "ooo_exec_timing" for v in self._run(
            [_orec(0, "add x5,x6,x7", issue=3, complete=2, commit=4)])["violations"])
        assert any(v["check"] == "ooo_exec_timing" for v in self._run(
            [_orec(0, "add x5,x6,x7", issue=1, complete=5, commit=3)])["violations"])

    def test_rename_and_squash(self):
        reuse = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=5, pdst=40),
                 _orec(1, "add x8,x1,x9", issue=2, complete=3, commit=6, pdst=40)]
        assert any(v["check"] == "ooo_rename"
                   for v in self._run(reuse)["violations"])
        freed = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=3, pdst=40),
                 _orec(1, "add x8,x1,x9", issue=3, complete=4, commit=5, pdst=40)]
        assert self._run(freed)["pass"]
        squash = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=3,
                        squashed=True)]
        assert any(v["check"] == "ooo_squash"
                   for v in self._run(squash)["violations"])
        assert self._run([_orec(0, "add x5,x6,x7", issue=1, complete=2,
                                squashed=True)])["pass"]

    def test_metrics_and_disasm_srcs(self):
        m = self._run([_orec(0, "add x5,x6,x7", issue=1, complete=4, commit=5),
                       _orec(1, "add x8,x1,x9", issue=2, complete=3, commit=6)])["metrics"]
        assert m["max_inflight"] >= 2 and m["mean_latency"] > 0
        # sources parsed from disasm (no explicit src) → RAW still caught
        raw = [_orec(0, "add x5,x6,x7", issue=1, complete=3, commit=4),
               _orec(1, "sub x8,x5,x9", issue=1, complete=4, commit=5)]
        assert any(v["check"] == "ooo_raw_hazard"
                   for v in self._run(raw)["violations"])

    def test_noop_robustness_schema(self):
        r = self._run([{"seq": 0, "disasm": "add x5,x6,x7", "regs": {}}])
        assert r["pass"] and r["ooo_active"] is False
        for rs in ([], [None, 5], [{}]):
            assert self._run(rs)["pass"]
        r2 = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r2
        assert r2["agent"] == "ooo_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.ooo_verifier import run_from_manifest
        rs = [_orec(0, "add x5,x6,x7", issue=1, complete=2, commit=4),
              _orec(1, "add x8,x1,x9", issue=1, complete=2, commit=3)]
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(x) for x in rs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"ooo_trace": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "ooo_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T53 — Load/Store Queue Checker
# ─────────────────────────────────────────────────────────────────────────────

def _st(seq, addr, val, commit=None):
    r = {"schema_version": "2.1.0", "seq": seq, "disasm": "sw",
         "mem_writes": [{"addr": hex(addr), "value": hex(val)}]}
    if commit is not None:
        r["ooo"] = {"commit": commit}
    return r


def _ld(seq, addr, val):
    return {"schema_version": "2.1.0", "seq": seq, "disasm": "lw",
            "mem_reads": [{"addr": hex(addr), "value": hex(val)}]}


class TestLSQVerifier:
    def _run(self, rs):
        from AGENT_H.lsq_verifier import LSQVerifier
        return LSQVerifier(rs).run()

    def test_import_and_forwarding(self):
        from AGENT_H import lsq_verifier as lv
        assert hasattr(lv, "LSQVerifier")
        assert self._run([_st(0, 0x40, 5), _ld(1, 0x40, 5)])["pass"]      # forwards 5
        assert any(v["check"] == "lsq_forward" for v in                    # stale read
                   self._run([_st(0, 0x40, 5), _ld(1, 0x40, 0)])["violations"])

    def test_youngest_store_and_wrong_forward(self):
        assert self._run([_st(0, 0x40, 5), _st(1, 0x40, 9),
                          _ld(2, 0x40, 9)])["pass"]                        # youngest = 9
        assert not self._run([_st(0, 0x40, 5), _st(1, 0x40, 9),
                              _ld(2, 0x40, 5)])["pass"]                    # wrong (older)

    def test_soundness_skips(self):
        assert self._run([_ld(0, 0x40, 7)])["pass"]                       # no prior store
        assert self._run([_st(0, 0x40, 5), _ld(1, 0x80, 0)])["pass"]      # other address

    def test_store_drain_order(self):
        assert self._run([_st(0, 0x40, 5, commit=3),
                          _st(1, 0x40, 9, commit=5), _ld(2, 0x40, 9)])["pass"]
        assert any(v["check"] == "lsq_store_order" for v in self._run(
            [_st(0, 0x40, 5, commit=6), _st(1, 0x40, 9, commit=4)])["violations"])

    def test_simplified_stream_and_metrics(self):
        assert not self._run([{"seq": 0, "op": "store", "addr": "0x40", "value": "0x5"},
                              {"seq": 1, "op": "load", "addr": "0x40",
                               "value": "0x1"}])["pass"]
        mm = self._run([_st(0, 0x40, 5), _ld(1, 0x40, 5),
                        _ld(2, 0x40, 5)])["metrics"]
        assert mm["stores"] == 1 and mm["loads"] == 2 and mm["forwards_checked"] == 2

    def test_noop_robustness_schema(self):
        r = self._run([{"seq": 0, "disasm": "add x1,x2,x3", "regs": {}}])
        assert r["pass"] and r["lsq_active"] is False
        for rs in ([], [None, 5], [{}]):
            assert self._run(rs)["pass"]
        r2 = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r2
        assert r2["agent"] == "lsq_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.lsq_verifier import run_from_manifest
        rs = [_st(0, 0x40, 5), _ld(1, 0x40, 0)]
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(x) for x in rs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"lsq_trace": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "lsq_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# Phase-6 end-to-end integration — every extended agent fires on a demo run
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase6Integration:
    """Drives every extended-tier agent against a synthesized run (demo_traces),
    proving the tier fires end-to-end — not just standalone. Closes the
    trace-producer / integration gap from ADR-001."""

    def _demo_run(self, tmp_path):
        from AGENT_H.demo_traces import write_demo_run
        return write_demo_run(str(tmp_path))

    def test_manifest_agents_fire_and_pass(self, tmp_path):
        mpath = self._demo_run(tmp_path)
        from AGENT_H import (coherence_verifier, memory_model_verifier,
                             interrupt_verifier, debug_verifier,
                             hypervisor_verifier, aia_verifier, reset_verifier)
        agents = [
            (coherence_verifier, "coherence_report.json"),
            (memory_model_verifier, "memory_model_report.json"),
            (interrupt_verifier, "interrupt_report.json"),
            (debug_verifier, "debug_report.json"),
            (hypervisor_verifier, "hypervisor_report.json"),
            (aia_verifier, "aia_report.json"),
            (reset_verifier, "reset_report.json"),
        ]
        for mod, report in agents:
            rc = mod.run_from_manifest(mpath)
            rep = json.loads((tmp_path / report).read_text())
            assert rep.get("status") == "completed", f"{report} was skipped"
            assert rep["pass"] is True, f"{report}: {rep.get('violations')}"
            assert rc == 0

    def test_commit_log_agents_active(self, tmp_path):
        self._demo_run(tmp_path)
        rtl = [json.loads(l) for l in
               (tmp_path / "rtl_commit.jsonl").read_text().splitlines() if l.strip()]
        from AGENT_H.vector_verifier import VectorVerifier
        from AGENT_H.perf_counter_verifier import PerfCounterVerifier
        from AGENT_H.branch_predictor_verifier import BranchPredictorVerifier
        from AGENT_H.coverage_collector import CoverageCollector
        v = VectorVerifier(rtl).run()
        assert v["vector_active"] and v["pass"]
        p = PerfCounterVerifier(rtl).run()
        assert p["perf_active"] and p["pass"]
        b = BranchPredictorVerifier(rtl).run()
        assert b["metrics"]["branches"] >= 1 and b["pass"]
        c = CoverageCollector(rtl).collect()
        assert c["covered_bins"] and c["coverage_summary"]["covered_bins"]
