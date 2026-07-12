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
# T54 — Scalar Cryptography Checker (SHA-256/512, SM3)
# ─────────────────────────────────────────────────────────────────────────────

def _rotr(x, n, w):
    x &= (1 << w) - 1
    return ((x >> n) | (x << (w - n))) & ((1 << w) - 1)


def _rotl(x, n, w):
    x &= (1 << w) - 1
    return ((x << n) | (x >> (w - n))) & ((1 << w) - 1)


def _cinstr(dis, src_reg, src_val, rd_reg, rd_val):
    return [
        {"schema_version": "2.1.0", "seq": 0, "disasm": f"li x{src_reg},{hex(src_val)}",
         "regs": {f"x{src_reg}": hex(src_val)}},
        {"schema_version": "2.1.0", "seq": 1, "disasm": dis,
         "regs": {f"x{rd_reg}": hex(rd_val)}},
    ]


class TestCryptoVerifier:
    def _run(self, rs):
        from AGENT_H.crypto_verifier import CryptoVerifier
        return CryptoVerifier(rs).run()

    def test_golden_matches_independent_reference(self):
        from AGENT_H.crypto_verifier import crypto_golden
        x = 0x12345678
        # sha256sig0 = ROTR7 ^ ROTR18 ^ SHR3  (recomputed here independently)
        assert crypto_golden("sha256sig0", x) == (_rotr(x, 7, 32) ^ _rotr(x, 18, 32) ^ (x >> 3))
        # sm3p1 = x ^ ROTL15 ^ ROTL23
        assert crypto_golden("sm3p1", x) == ((x ^ _rotl(x, 15, 32) ^ _rotl(x, 23, 32)) & 0xFFFFFFFF)
        y = 0x0123456789ABCDEF
        assert crypto_golden("sha512sum1", y) == (
            _rotr(y, 14, 64) ^ _rotr(y, 18, 64) ^ _rotr(y, 41, 64))

    def test_sha256_clean_and_bug(self):
        from AGENT_H.crypto_verifier import crypto_golden
        x = 0xABCDEF01
        ok = _cinstr("sha256sig0 x5,x6", 6, x, 5, crypto_golden("sha256sig0", x))
        r = self._run(ok)
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["crypto_active"]
        bad = _cinstr("sha256sum0 x5,x6", 6, x, 5, 0xDEADBEEF)
        rb = self._run(bad)
        assert not rb["pass"] and any(v["check"] == "crypto_result"
                                      for v in rb["violations"])

    def test_sha512_and_sm3(self):
        from AGENT_H.crypto_verifier import crypto_golden
        y = 0xFEDCBA9876543210
        assert self._run(_cinstr("sha512sum1 x5,x6", 6, y, 5,
                                 crypto_golden("sha512sum1", y)))["pass"]
        assert not self._run(_cinstr("sha512sig0 x5,x6", 6, y, 5, 0x1))["pass"]
        x = 0x0F0F0F0F
        assert self._run(_cinstr("sm3p1 x5,x6", 6, x, 5,
                                 crypto_golden("sm3p1", x)))["pass"]

    def test_abi_names_and_metrics(self):
        from AGENT_H.crypto_verifier import crypto_golden
        x = 0x11223344
        rs = [{"schema_version": "2.1.0", "seq": 0, "disasm": "li a1,0x11223344",
               "regs": {"a1": hex(x)}},
              {"schema_version": "2.1.0", "seq": 1, "disasm": "sha256sig1 a0,a1",
               "regs": {"a0": hex(crypto_golden("sha256sig1", x))}}]
        r = self._run(rs)
        assert r["pass"] and r["metrics"]["by_op"]["sha256sig1"] == 1

    def test_noop_robustness_schema(self):
        r = self._run([{"schema_version": "2.1.0", "seq": 0,
                        "disasm": "add x1,x2,x3", "regs": {"x1": "0x5"}}])
        assert r["pass"] and r["crypto_active"] is False
        for rs in ([], [None, 5], [{}], [{"disasm": None}]):
            assert self._run(rs)["pass"]
        r2 = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r2
        assert r2["agent"] == "crypto_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.crypto_verifier import run_from_manifest
        rs = _cinstr("sha256sum0 x5,x6", 6, 0xABCDEF01, 5, 0xDEADBEEF)
        (tmp_path / "rtl_commit.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"rtl_commit_log": "rtl_commit.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "crypto_report.json").exists()

    # --- Zbkb / Zbkx bit-manipulation for cryptography ---
    def test_zbk_golden_matches_reference(self):
        from AGENT_H.crypto_verifier import zbk_one, zbk_two
        x = 0x12345678
        ref_brev8 = 0
        for b in range(4):
            byte = (x >> (b * 8)) & 0xFF
            ref_brev8 |= int(f"{byte:08b}"[::-1], 2) << (b * 8)
        assert zbk_one("brev8", x, 32) == ref_brev8
        # zip then unzip is identity
        assert zbk_one("unzip", zbk_one("zip", x, 32), 32) == x
        assert zbk_two("pack", 0xAAAA, 0xBBBB, 32) == (0xAAAA | (0xBBBB << 16))
        assert zbk_two("packh", 0xAB, 0xCD, 32) == (0xAB | (0xCD << 8))

    def test_zbk_pack_brev_xperm(self):
        from AGENT_H.crypto_verifier import zbk_one, zbk_two
        x = 0xDEADBEEF
        assert self._run(_cinstr("brev8 x5,x6", 6, x, 5,
                                 zbk_one("brev8", x, 32)))["pass"]
        assert not self._run(_cinstr("brev8 x5,x6", 6, x, 5, 0xDEAD))["pass"]
        # pack (two-source): build via a 3-record sequence
        a, b = 0x11112222, 0x33334444
        rs = [{"schema_version": "2.1.0", "seq": 0, "disasm": "li x6",
               "regs": {"x6": hex(a)}},
              {"schema_version": "2.1.0", "seq": 1, "disasm": "li x7",
               "regs": {"x7": hex(b)}},
              {"schema_version": "2.1.0", "seq": 2, "disasm": "pack x5,x6,x7",
               "regs": {"x5": hex(zbk_two("pack", a, b, 32))}}]
        assert self._run(rs)["pass"]
        # xperm8 reverse-byte-order permutation
        av, idx = 0x03020100, 0x00010203
        rs2 = [{"schema_version": "2.1.0", "seq": 0, "disasm": "li x6",
                "regs": {"x6": hex(av)}},
               {"schema_version": "2.1.0", "seq": 1, "disasm": "li x7",
                "regs": {"x7": hex(idx)}},
               {"schema_version": "2.1.0", "seq": 2, "disasm": "xperm8 x5,x6,x7",
                "regs": {"x5": hex(zbk_two("xperm8", av, idx, 32))}}]
        assert self._run(rs2)["pass"]


# ─────────────────────────────────────────────────────────────────────────────
# T55 — Zacas Compare-and-Swap Checker
# ─────────────────────────────────────────────────────────────────────────────

def _cas(compare, swap, mem_old, mem_new, rd, op="amocas.w", addr=0x40):
    return {"op": op, "addr": hex(addr), "compare": hex(compare),
            "swap": hex(swap), "mem_old": hex(mem_old), "mem_new": hex(mem_new),
            "rd": hex(rd)}


class TestCASVerifier:
    def _run(self, evs):
        from AGENT_H.cas_verifier import CASVerifier
        return CASVerifier(evs).run()

    def test_import_and_success_fail(self):
        from AGENT_H import cas_verifier as cv
        assert hasattr(cv, "CASVerifier")
        assert self._run([_cas(5, 9, 5, 9, 5)])["pass"]      # match → swap, rd=old
        assert self._run([_cas(5, 9, 7, 7, 7)])["pass"]      # mismatch → unchanged

    def test_return_success_fail_bugs(self):
        assert any(v["check"] == "cas_return" for v in
                   self._run([_cas(5, 9, 5, 9, 9)])["violations"])       # rd wrong
        assert any(v["check"] == "cas_success" for v in
                   self._run([_cas(5, 9, 5, 5, 5)])["violations"])       # no write
        assert any(v["check"] == "cas_fail" for v in
                   self._run([_cas(5, 9, 7, 9, 7)])["violations"])       # modified

    def test_width_mask_and_metrics(self):
        assert self._run([_cas(0x100000005, 9, 5, 9, 5, op="amocas.w")])["pass"]
        assert self._run([_cas(0x100000005, 9, 0x100000005, 9, 0x100000005,
                               op="amocas.d")])["pass"]
        mm = self._run([_cas(5, 9, 5, 9, 5), _cas(5, 9, 7, 7, 7)])["metrics"]
        assert mm["cas_ops"] == 2 and mm["successes"] == 1 and mm["failures"] == 1

    def test_noop_robustness_schema(self):
        r = self._run([{"op": "add"}])
        assert r["pass"] and r["cas_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "amocas.w"}]):
            assert self._run(evs)["pass"]
        r2 = self._run([])
        for k in ("schema_version", "agent", "metrics", "total_violations",
                  "pass", "violations", "band"):
            assert k in r2
        assert r2["agent"] == "cas_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.cas_verifier import run_from_manifest
        evs = [_cas(5, 9, 5, 5, 5)]                            # bug → rc 1
        (tmp_path / "cas_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"cas_trace": "cas_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "cas_report.json").exists()


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
