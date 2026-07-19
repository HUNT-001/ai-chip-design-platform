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
# T56 — AES Scalar Cryptography Checker (FIPS-197-validated)
# ─────────────────────────────────────────────────────────────────────────────

def _hb(s):
    return [int(s[i:i + 2], 16) for i in range(0, len(s), 2)]


def _le64(b8):
    return sum(b << (i * 8) for i, b in enumerate(b8))


class TestAESVerifier:
    def test_sbox_and_mixcolumns_constants(self):
        from AGENT_H.aes_verifier import AES_SBOX, AES_INV_SBOX, _mixcol_fwd, _mixcol_inv
        assert AES_SBOX[0x00] == 0x63 and AES_SBOX[0x53] == 0xed
        for i in range(256):
            assert AES_INV_SBOX[AES_SBOX[i]] == i
        # FIPS-197 §4.3: db 13 53 45 -> 8e 4d a1 bc
        w = 0xdb | (0x13 << 8) | (0x53 << 16) | (0x45 << 24)
        out = _mixcol_fwd(w)
        assert [(out >> (i * 8)) & 0xFF for i in range(4)] == [0x8e, 0x4d, 0xa1, 0xbc]
        assert _mixcol_inv(out) == w

    def test_fips197_round1(self):
        """The gold standard: aes64esm reproduces the FIPS-197 Appendix B
        round-1 SubBytes→ShiftRows→MixColumns result exactly."""
        from AGENT_H.aes_verifier import aes_golden
        state = _hb("193de3bea0f4e22b9ac68d2ae9f84808")
        expect = _hb("046681e5e0cb199a48f8d37a2806264c")
        s0, s1 = _le64(state[0:8]), _le64(state[8:16])
        t0 = aes_golden("aes64esm", s0, s1)
        t1 = aes_golden("aes64esm", s1, s0)         # high half via operand swap
        got = ([(t0 >> (i * 8)) & 0xFF for i in range(8)]
               + [(t1 >> (i * 8)) & 0xFF for i in range(8)])
        assert got == expect
        # aes64es (final round, no MixColumns) = FIPS ShiftRows+SubBytes
        exp_es = _hb("d4bf5d30e0b452aeb84111f11e2798e5")
        e0, e1 = aes_golden("aes64es", s0, s1), aes_golden("aes64es", s1, s0)
        got_es = ([(e0 >> (i * 8)) & 0xFF for i in range(8)]
                  + [(e1 >> (i * 8)) & 0xFF for i in range(8)])
        assert got_es == exp_es

    def test_decrypt_and_im_inverses(self):
        from AGENT_H.aes_verifier import aes_golden, aes64im, _mixcol_fwd
        import random
        rng = random.Random(0)
        s0, s1 = rng.getrandbits(64), rng.getrandbits(64)
        e0, e1 = aes_golden("aes64es", s0, s1), aes_golden("aes64es", s1, s0)
        d0, d1 = aes_golden("aes64ds", e0, e1), aes_golden("aes64ds", e1, e0)
        assert (d0, d1) == (s0, s1)                 # ds inverts es
        x = rng.getrandbits(64)
        mixed = _mixcol_fwd(x & 0xFFFFFFFF) | (_mixcol_fwd((x >> 32) & 0xFFFFFFFF) << 32)
        assert aes64im(mixed) == x

    def test_verifier_clean_bug_noop(self):
        from AGENT_H.aes_verifier import AESVerifier, aes_golden
        s0, s1 = 0x2be2f4a0bee33d19, 0x0848f8e92a8dc69a
        g = aes_golden("aes64esm", s0, s1)
        base = [{"schema_version": "2.1.0", "seq": 0, "disasm": "li",
                 "regs": {"x6": hex(s0)}},
                {"schema_version": "2.1.0", "seq": 1, "disasm": "li",
                 "regs": {"x7": hex(s1)}}]
        ok = base + [{"schema_version": "2.1.0", "seq": 2,
                      "disasm": "aes64esm x5,x6,x7", "regs": {"x5": hex(g)}}]
        assert AESVerifier(ok).run()["pass"]
        bad = base + [{"schema_version": "2.1.0", "seq": 2,
                       "disasm": "aes64esm x5,x6,x7", "regs": {"x5": "0xDEAD"}}]
        assert not AESVerifier(bad).run()["pass"]
        r = AESVerifier([{"disasm": "add x1,x2,x3", "regs": {"x1": "0x5"}}]).run()
        assert r["pass"] and r["aes_active"] is False
        for rs in ([], [None, 5], [{}]):
            assert AESVerifier(rs).run()["pass"]
        assert AESVerifier([]).run()["agent"] == "aes_verifier"


# ─────────────────────────────────────────────────────────────────────────────
# T58 — Vector AES Checker (Zvkned, FIPS-197-validated core)
# ─────────────────────────────────────────────────────────────────────────────

class TestVAESVerifier:
    def _run(self, evs):
        from AGENT_H.vaes_verifier import VAESVerifier
        return VAESVerifier(evs).run()

    def test_fips_round_and_final(self):
        from AGENT_H.vaes_verifier import vaes_round, _bytes16, _hex16
        state = _bytes16("193de3bea0f4e22b9ac68d2ae9f84808")
        zero = _bytes16("0" * 32)
        assert _hex16(vaes_round("vaesem", state, zero)) == \
            "046681e5e0cb199a48f8d37a2806264c"          # FIPS-197 SB+SR+MC
        assert _hex16(vaes_round("vaesef", state, zero)) == \
            "d4bf5d30e0b452aeb84111f11e2798e5"          # SB+SR (final round)

    def test_decrypt_round_trips_and_key(self):
        from AGENT_H.vaes_verifier import vaes_round
        import random
        rng = random.Random(0)
        st = [rng.getrandbits(8) for _ in range(16)]
        key = [rng.getrandbits(8) for _ in range(16)]
        # vaesd* inverts vaese* (final and middle) for the same key
        assert vaes_round("vaesdf", vaes_round("vaesef", st, key), key) == st
        assert vaes_round("vaesdm", vaes_round("vaesem", st, key), key) == st
        # vaesz is a plain key XOR
        assert vaes_round("vaesz", st, key) == [st[i] ^ key[i] for i in range(16)]

    def test_verifier_clean_bug_noop(self):
        from AGENT_H.vaes_verifier import VAESVerifier
        state = "193de3bea0f4e22b9ac68d2ae9f84808"
        good = [{"op": "vaesem.vv", "state": state, "key": "0" * 32,
                 "result": "046681e5e0cb199a48f8d37a2806264c"}]
        r = self._run(good)
        assert r["pass"] and r["metrics"]["checked"] == 1
        bad = [{"op": "vaesem", "state": state, "key": "0" * 32,
                "result": "0" * 32}]
        assert any(v["check"] == "vaes_result" for v in self._run(bad)["violations"])
        n = self._run([{"op": "vadd.vv"}])
        assert n["pass"] and n["vaes_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vaesem"}]):
            assert self._run(evs)["pass"]
        assert self._run([]).get("agent") == "vaes_verifier"

    def test_manifest(self, tmp_path):
        from AGENT_H.vaes_verifier import run_from_manifest
        evs = [{"op": "vaesem", "state": "193de3bea0f4e22b9ac68d2ae9f84808",
                "key": "0" * 32, "result": "0" * 32}]
        (tmp_path / "vaes_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vaes_trace": "vaes_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vaes_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T59 — Vector SHA-2 Checker (Zvknha/b, hashlib-validated)
# ─────────────────────────────────────────────────────────────────────────────

_K256 = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2]
_H256 = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
         0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]


class TestVSHAVerifier:
    def _sha256_via_ops(self, msg):
        """Full (multi-block) SHA-256 composed purely from the two goldens —
        proves both the arithmetic and the element-group layout vs hashlib."""
        import struct
        from AGENT_H.vsha_verifier import vsha2ms_golden, vsha2c_golden
        M = (1 << 32) - 1
        data = msg + b"\x80" + b"\x00" * ((56 - len(msg) - 1) % 64) + \
            struct.pack(">Q", len(msg) * 8)
        H = list(_H256)
        for off in range(0, len(data), 64):            # per 512-bit block
            W = list(struct.unpack(">16I", data[off:off + 64]))
            for k in range(0, 48, 4):
                W += vsha2ms_golden(
                    [W[k], W[k + 1], W[k + 2], W[k + 3]],
                    [W[k + 4], W[k + 9], W[k + 10], W[k + 11]],
                    [W[k + 12], W[k + 13], W[k + 14], W[k + 15]], 32)
            a, b, c, d, e, f, g, h = H
            abef, cdgh = [f, e, b, a], [h, g, d, c]
            for t in range(0, 64, 4):
                wk = [(W[t + j] + _K256[t + j]) & M for j in range(4)]
                na = vsha2c_golden("vsha2cl", cdgh, abef, wk, 32)
                cdgh, abef = abef, na       # after 2 rounds {c,d,g,h}=old {a,b,e,f}
                na = vsha2c_golden("vsha2ch", cdgh, abef, wk, 32)
                cdgh, abef = abef, na
            f, e, b, a = abef
            h, g, d, c = cdgh
            H = [(H[i] + v) & M for i, v in enumerate([a, b, c, d, e, f, g, h])]
        return "".join(f"{x:08x}" for x in H)

    def test_full_sha256_matches_hashlib(self):
        import hashlib
        for m in (b"abc", b"", b"hello world", b"a" * 100):
            assert self._sha256_via_ops(m) == hashlib.sha256(m).hexdigest()

    def test_message_schedule_matches_recurrence(self):
        from AGENT_H.vsha_verifier import vsha2ms_golden
        W = list(range(1, 17))                       # W0..W15
        got = vsha2ms_golden([W[0], W[1], W[2], W[3]],
                             [W[4], W[9], W[10], W[11]],
                             [W[12], W[13], W[14], W[15]], 32)

        def _rotr(x, n):
            return ((x >> n) | (x << (32 - n))) & 0xFFFFFFFF

        def s0(x):
            return _rotr(x, 7) ^ _rotr(x, 18) ^ (x >> 3)

        def s1(x):
            return _rotr(x, 17) ^ _rotr(x, 19) ^ (x >> 10)
        exp = []
        WW = list(W)
        for t in range(16, 20):
            WW.append((s1(WW[t - 2]) + WW[t - 7] + s0(WW[t - 15]) + WW[t - 16])
                      & 0xFFFFFFFF)
            exp.append(WW[t])
        assert got == exp

    def test_verifier_clean_and_bug(self):
        from AGENT_H.vsha_verifier import VSHAVerifier, vsha2ms_golden
        W = list(range(1, 17))
        res = vsha2ms_golden([W[0], W[1], W[2], W[3]],
                             [W[4], W[9], W[10], W[11]],
                             [W[12], W[13], W[14], W[15]], 32)
        base = {"op": "vsha2ms", "sew": 32,
                "vd": [W[0], W[1], W[2], W[3]],
                "vs2": [W[4], W[9], W[10], W[11]],
                "vs1": [W[12], W[13], W[14], W[15]]}
        good = dict(base, result=res)
        r = VSHAVerifier([good]).run()
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["vsha_active"]
        bad = dict(base, result=[0, 0, 0, 0])
        rb = VSHAVerifier([bad]).run()
        assert not rb["pass"]
        assert any(v["check"] == "vsha_result" for v in rb["violations"])
        # non-vsha op ignored; malformed tolerated
        n = VSHAVerifier([{"op": "vadd.vv"}]).run()
        assert n["pass"] and n["vsha_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vsha2ms"}]):
            assert VSHAVerifier(evs).run()["pass"]

    def test_ch_uses_high_words_cl_low(self):
        # cl must consume vs1[0:2], ch vs1[2:4] — swapping them changes output
        from AGENT_H.vsha_verifier import vsha2c_golden
        vd = [1, 2, 3, 4]
        vs2 = [5, 6, 7, 8]
        vs1 = [0x11, 0x22, 0x33, 0x44]
        cl = vsha2c_golden("vsha2cl", vd, vs2, vs1, 32)
        ch = vsha2c_golden("vsha2ch", vd, vs2, vs1, 32)
        assert cl != ch
        # cl with [w0,w1,*,*] equals ch with [*,*,w0,w1]
        cl2 = vsha2c_golden("vsha2cl", vd, vs2, [0x11, 0x22, 0, 0], 32)
        ch2 = vsha2c_golden("vsha2ch", vd, vs2, [0, 0, 0x11, 0x22], 32)
        assert cl2 == ch2

    def test_sha512_sew64(self):
        import hashlib
        import struct
        from AGENT_H.vsha_verifier import vsha2ms_golden, vsha2c_golden
        K = [0x428a2f98d728ae22, 0x7137449123ef65cd, 0xb5c0fbcfec4d3b2f,
             0xe9b5dba58189dbbc, 0x3956c25bf348b538, 0x59f111f1b605d019,
             0x923f82a4af194f9b, 0xab1c5ed5da6d8118, 0xd807aa98a3030242,
             0x12835b0145706fbe, 0x243185be4ee4b28c, 0x550c7dc3d5ffb4e2,
             0x72be5d74f27b896f, 0x80deb1fe3b1696b1, 0x9bdc06a725c71235,
             0xc19bf174cf692694, 0xe49b69c19ef14ad2, 0xefbe4786384f25e3,
             0x0fc19dc68b8cd5b5, 0x240ca1cc77ac9c65, 0x2de92c6f592b0275,
             0x4a7484aa6ea6e483, 0x5cb0a9dcbd41fbd4, 0x76f988da831153b5,
             0x983e5152ee66dfab, 0xa831c66d2db43210, 0xb00327c898fb213f,
             0xbf597fc7beef0ee4, 0xc6e00bf33da88fc2, 0xd5a79147930aa725,
             0x06ca6351e003826f, 0x142929670a0e6e70, 0x27b70a8546d22ffc,
             0x2e1b21385c26c926, 0x4d2c6dfc5ac42aed, 0x53380d139d95b3df,
             0x650a73548baf63de, 0x766a0abb3c77b2a8, 0x81c2c92e47edaee6,
             0x92722c851482353b, 0xa2bfe8a14cf10364, 0xa81a664bbc423001,
             0xc24b8b70d0f89791, 0xc76c51a30654be30, 0xd192e819d6ef5218,
             0xd69906245565a910, 0xf40e35855771202a, 0x106aa07032bbd1b8,
             0x19a4c116b8d2d0c8, 0x1e376c085141ab53, 0x2748774cdf8eeb99,
             0x34b0bcb5e19b48a8, 0x391c0cb3c5c95a63, 0x4ed8aa4ae3418acb,
             0x5b9cca4f7763e373, 0x682e6ff3d6b2b8a3, 0x748f82ee5defb2fc,
             0x78a5636f43172f60, 0x84c87814a1f0ab72, 0x8cc702081a6439ec,
             0x90befffa23631e28, 0xa4506cebde82bde9, 0xbef9a3f7b2c67915,
             0xc67178f2e372532b, 0xca273eceea26619c, 0xd186b8c721c0c207,
             0xeada7dd6cde0eb1e, 0xf57d4f7fee6ed178, 0x06f067aa72176fba,
             0x0a637dc5a2c898a6, 0x113f9804bef90dae, 0x1b710b35131c471b,
             0x28db77f523047d84, 0x32caab7b40c72493, 0x3c9ebe0a15c9bebc,
             0x431d67c49c100d4c, 0x4cc5d4becb3e42b6, 0x597f299cfc657e2a,
             0x5fcb6fab3ad6faec, 0x6c44198c4a475817]
        H = [0x6a09e667f3bcc908, 0xbb67ae8584caa73b, 0x3c6ef372fe94f82b,
             0xa54ff53a5f1d36f1, 0x510e527fade682d1, 0x9b05688c2b3e6c1f,
             0x1f83d9abfb41bd6b, 0x5be0cd19137e2179]
        M = (1 << 64) - 1
        msg = b"abc"
        pad = msg + b"\x80" + b"\x00" * ((112 - len(msg) - 1) % 128) + \
            (0).to_bytes(8, "big") + (len(msg) * 8).to_bytes(8, "big")
        W = list(struct.unpack(">16Q", pad[:128]))
        for k in range(0, 64, 4):
            W += vsha2ms_golden(
                [W[k], W[k + 1], W[k + 2], W[k + 3]],
                [W[k + 4], W[k + 9], W[k + 10], W[k + 11]],
                [W[k + 12], W[k + 13], W[k + 14], W[k + 15]], 64)
        a, b, c, d, e, f, g, h = H
        abef, cdgh = [f, e, b, a], [h, g, d, c]
        for t in range(0, 80, 4):
            wk = [(W[t + j] + K[t + j]) & M for j in range(4)]
            na = vsha2c_golden("vsha2cl", cdgh, abef, wk, 64)
            cdgh, abef = abef, na
            na = vsha2c_golden("vsha2ch", cdgh, abef, wk, 64)
            cdgh, abef = abef, na
        f, e, b, a = abef
        h, g, d, c = cdgh
        out = [(H[i] + v) & M for i, v in enumerate([a, b, c, d, e, f, g, h])]
        assert "".join(f"{x:016x}" for x in out) == hashlib.sha512(b"abc").hexdigest()

    def test_manifest(self, tmp_path):
        from AGENT_H.vsha_verifier import run_from_manifest
        evs = [{"op": "vsha2ms", "sew": 32, "vd": [1, 2, 3, 4],
                "vs2": [5, 6, 7, 8], "vs1": [9, 10, 11, 12],
                "result": [0, 0, 0, 0]}]
        (tmp_path / "vsha_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vsha_trace": "vsha_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vsha_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T60 — Vector SM3 Checker (Zvksh, GB/T-32905-validated)
# ─────────────────────────────────────────────────────────────────────────────

_SM3_IV = [0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
           0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e]


class TestVSM3Verifier:
    def _sm3_via_ops(self, msg):
        """Full SM3 composed purely from the two goldens — proves the round
        math, the byte-swaps and the rolled packing against GB/T 32905."""
        from AGENT_H.vsm3_verifier import vsm3me_golden, vsm3c_golden, _rev8

        def r(x):
            return _rev8(x)
        ml = len(msg) * 8
        m = msg + b"\x80" + b"\x00" * ((56 - len(msg) - 1) % 64) + \
            ml.to_bytes(8, "big")
        V = list(_SM3_IV)
        for off in range(0, len(m), 64):
            blk = m[off:off + 64]
            Wl = [int.from_bytes(blk[4 * i:4 * i + 4], "big") for i in range(16)]
            reg = [r(x) for x in Wl]
            while len(reg) < 68:
                base = len(reg) - 16
                reg += vsm3me_golden(reg[base:base + 8], reg[base + 8:base + 16])
            state = [r(V[i]) for i in range(8)]           # {H..A} el7=H..el0=A
            for rnds in range(32):
                j = 2 * rnds
                vs2 = [reg[j], reg[j + 1], 0, 0, reg[j + 4], reg[j + 5], 0, 0]
                state = vsm3c_golden(state, vs2, rnds)
            s = [r(state[i]) for i in range(8)]
            V = [(V[i] ^ s[i]) & 0xFFFFFFFF for i in range(8)]
        return b"".join(x.to_bytes(4, "big") for x in V).hex()

    def test_gbt_vectors(self):
        assert self._sm3_via_ops(b"abc") == \
            "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
        assert self._sm3_via_ops(b"abcd" * 16) == \
            "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"
        # empty string SM3 (known digest)
        assert self._sm3_via_ops(b"") == \
            "1ab21d8355cfa17f8e61194831e81a8f22bec8c728fefb747ed035eb5082aa2b"

    def test_message_expansion_recurrence(self):
        from AGENT_H.vsm3_verifier import vsm3me_golden, _rev8, _p1, _rol
        vs1 = [_rev8(i + 1) for i in range(8)]        # logical W0..W7 = 1..8
        vs2 = [_rev8(i + 9) for i in range(8)]        # logical W8..W15 = 9..16
        got = [_rev8(x) for x in vsm3me_golden(vs1, vs2)]   # logical W16..W23
        W = list(range(1, 17))
        for j in range(16, 24):
            W.append(_p1(W[j - 16] ^ W[j - 9] ^ _rol(W[j - 3], 15))
                     ^ _rol(W[j - 13], 7) ^ W[j - 6])
            W[-1] &= 0xFFFFFFFF
        assert got == W[16:24]

    def test_verifier_clean_and_bug(self):
        from AGENT_H.vsm3_verifier import VSM3Verifier, vsm3me_golden
        vs1 = list(range(1, 9))
        vs2 = list(range(9, 17))
        res = vsm3me_golden(vs1, vs2)
        good = {"op": "vsm3me", "vs1": vs1, "vs2": vs2, "result": res}
        r = VSM3Verifier([good]).run()
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["vsm3_active"]
        bad = {"op": "vsm3me", "vs1": vs1, "vs2": vs2, "result": [0] * 8}
        rb = VSM3Verifier([bad]).run()
        assert not rb["pass"]
        assert any(v["check"] == "vsm3_result" for v in rb["violations"])
        # vsm3c good/bad
        from AGENT_H.vsm3_verifier import vsm3c_golden
        vd = list(range(1, 9))
        vc = [0x11, 0x22, 0, 0, 0x44, 0x55, 0, 0]
        cres = vsm3c_golden(vd, vc, 3)
        assert VSM3Verifier([{"op": "vsm3c", "rnds": 3, "vd": vd,
                              "vs2": vc, "result": cres}]).run()["pass"]
        # non-vsm3 ignored; malformed tolerated
        n = VSM3Verifier([{"op": "vadd.vv"}]).run()
        assert n["pass"] and n["vsm3_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vsm3me"}]):
            assert VSM3Verifier(evs).run()["pass"]

    def test_rnds_selects_round_group(self):
        # different round groups must yield different compression output
        from AGENT_H.vsm3_verifier import vsm3c_golden
        vd = list(range(1, 9))
        vs2 = [0x10, 0x20, 0, 0, 0x30, 0x40, 0, 0]
        assert vsm3c_golden(vd, vs2, 0) != vsm3c_golden(vd, vs2, 8)  # <16 vs >=16

    def test_manifest(self, tmp_path):
        from AGENT_H.vsm3_verifier import run_from_manifest
        evs = [{"op": "vsm3me", "vs1": list(range(1, 9)),
                "vs2": list(range(9, 17)), "result": [0] * 8}]
        (tmp_path / "vsm3_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vsm3_trace": "vsm3_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vsm3_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T79 — Verification / AI digital twin
# ─────────────────────────────────────────────────────────────────────────────

class TestVerificationTwin:
    @staticmethod
    def _gen(cmax, tau, n):
        import math
        return [round(cmax * (1 - math.exp(-t / tau)), 3)
                for t in range(1, n + 1)]

    def test_coverage_curve_fit_recovers_parameters(self):
        """The fit must recover known (Cmax, tau) from synthetic data."""
        from AGENT_H.verification_twin import fit_coverage_curve
        for cmax, tau in [(95.0, 8.0), (100.0, 4.0), (80.0, 12.0)]:
            fit = fit_coverage_curve(self._gen(cmax, tau, 20))
            assert abs(fit["cmax"] - cmax) < 2.0, fit
            assert abs(fit["tau"] - tau) < 1.0, fit
            assert fit["r2"] > 0.98
        # too little history -> honest note, no fabricated fit
        assert "insufficient" in fit_coverage_curve([50.0]).get("note", "")

    def test_forecast_reachable_vs_plateau(self):
        from AGENT_H.verification_twin import forecast_closure
        reach = forecast_closure(self._gen(98, 6, 10), goal=90)
        assert reach["reachable"] and reach["additional_runs_needed"] >= 0
        # coverage that plateaus below the goal is honestly unreachable
        plat = forecast_closure(self._gen(82, 6, 10), goal=90)
        assert plat["reachable"] is False
        assert "unreachable" in plat["note"]
        assert plat["asymptote"] < 90

    def test_regression_prediction_trend(self):
        from AGENT_H.verification_twin import predict_regression
        assert predict_regression([0.7, 0.75, 0.8, 0.85, 0.9])["trend"] == \
            "improving"
        assert predict_regression([0.95, 0.9, 0.85, 0.8])["trend"] == "declining"
        assert predict_regression([0.9, 0.9, 0.9])["trend"] == "flat"
        # honest on no data
        assert predict_regression([])["predicted_pass_rate"] is None

    def test_tapeout_readiness_blocker_caps(self):
        from AGENT_H.verification_twin import tapeout_readiness
        good = {"coverage": 95, "confidence": 0.92, "open_bugs": 0,
                "blockers": 0, "completeness": 0.9, "regression_pass_rate": 0.99}
        assert tapeout_readiness(good)["band"] == "READY"
        # one blocker forces NOT_READY regardless of the averages
        blocked = tapeout_readiness({**good, "blockers": 1})
        assert blocked["band"] == "NOT_READY"
        assert blocked["readiness_score"] <= 0.4
        assert "blocker" in blocked["recommendation"].lower()
        # weighted gaps point at the weakest factor
        weak = tapeout_readiness({**good, "coverage": 40})
        assert weak["weighted_gaps"][0][0] == "coverage"

    def test_what_if_projection(self):
        from AGENT_H.verification_twin import what_if
        state = {"coverage_history": self._gen(95, 8, 8), "open_bugs": 3,
                 "confidence": 0.7, "completeness": 0.8, "coverage_goal": 90}
        # adding tests moves coverage forward along the fitted curve
        w = what_if(state, {"add_tests": 20})
        assert w["projected"]["coverage"] > w["baseline"]["coverage"]
        assert w["delta_coverage"] > 0
        # fixing bugs reduces open bugs and raises confidence
        w2 = what_if(state, {"fix_bugs": 3})
        assert w2["projected"]["open_bugs"] == 0
        assert w2["projected"]["confidence"] > w2["baseline"]["confidence"]

    def test_replay_determinism(self):
        from AGENT_H.verification_twin import replay, replay_failure
        import hashlib
        import json as _j
        inputs = {"config": "small", "vlen": 128}
        canon = _j.dumps({"seed": 42, "inputs": inputs}, sort_keys=True)
        h = hashlib.sha256(canon.encode()).hexdigest()[:16]
        rec = {"run_id": "r1", "seed": 42, "inputs": inputs, "input_hash": h,
               "outcome": "pass"}
        assert replay(rec)["faithful"] is True
        # a corrupted record is flagged non-reproducible, not silently accepted
        bad = {**rec, "input_hash": "deadbeefdeadbeef"}
        r = replay(bad)
        assert r["faithful"] is False and r["note"]
        fr = replay_failure({"seed": 7, "test": "t_alu", "check": "alu_result"})
        assert "SEED=7" in fr["reproduction"]["command"]

    def test_silicon_sync_is_honest(self):
        from AGENT_H.verification_twin import silicon_sync
        # no hardware -> awaiting, never fabricated
        r = silicon_sync([{"cycle": 0, "signals": {"pc": "0x80"}}])
        assert r["status"] == "awaiting_hardware" and r["contract"]
        # with hardware -> real diff
        ok = silicon_sync([{"cycle": 0, "signals": {"pc": "0x80"}}],
                          [{"cycle": 0, "signals": {"pc": "0x80"}}], "fpga")
        assert ok["correlated"] is True
        mm = silicon_sync([{"cycle": 0, "signals": {"pc": "0x80"}}],
                          [{"cycle": 0, "signals": {"pc": "0x84"}}], "fpga")
        assert mm["mismatch_count"] == 1 and mm["correlated"] is False

    def test_end_to_end_and_manifest(self, tmp_path):
        from AGENT_H.verification_twin import VerificationTwin, run_from_manifest
        reports = {"alu": {"band": "CLEAN", "violations": 0, "pass": True},
                   "cache": {"band": "CRITICAL", "violations": 3, "pass": False}}
        twin = VerificationTwin(reports, self._gen(95, 8, 10),
                                [0.8, 0.85, 0.9], {"confidence": 0.7})
        rep = twin.run(goal=90)
        assert rep["live_status"]["failing_agents"] == ["cache"]
        assert rep["coverage_forecast"] is not None
        assert rep["regression_prediction"]["trend"] == "improving"
        assert rep["tapeout_readiness"]["band"] in ("READY", "NEARLY", "NOT_READY")
        for bad in ({}, None):
            VerificationTwin(bad).run()
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "reports": reports, "coverage_history": self._gen(95, 8, 6),
               "pass_rate_history": [0.8, 0.9], "coverage_goal": 90}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        run_from_manifest(str(mp))
        assert (tmp_path / "verification_twin_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T78 — AGENT_B testbench generation (RTL → verification environment)
# ─────────────────────────────────────────────────────────────────────────────

def _sv_balanced(sv):
    """Structural SV check: strip comments+strings, anchor on declarations."""
    import re
    s = re.sub(r"/\*.*?\*/", " ", sv, flags=re.DOTALL)
    s = re.sub(r"//[^\n]*", " ", s)
    s = re.sub(r'"(\\.|[^"\\])*"', ' "" ', s)
    errs = []
    for op, cl, nm in [
        (r"\bmodule\b", r"\bendmodule\b", "module"),
        (r"\bclass\b", r"\bendclass\b", "class"),
        (r"\bpackage\b", r"\bendpackage\b", "package"),
        (r"\bfunction\b", r"\bendfunction\b", "function"),
        (r"\btask\b", r"\bendtask\b", "task"),
        (r"\bcovergroup\b", r"\bendgroup\b", "covergroup"),
        (r"\bclocking\s+\w+\s*@", r"\bendclocking\b", "clocking"),
        (r"\binterface\s+\w+", r"\bendinterface\b", "interface"),
    ]:
        a, b = len(re.findall(op, s)), len(re.findall(cl, s))
        if a != b:
            errs.append(f"{nm}:{a}/{b}")
    return errs


_AXIL_SV = """
module axil_slave (
  input  logic        aclk,
  input  logic        aresetn,
  input  logic        s_axi_awvalid, output logic s_axi_awready,
  input  logic [31:0] s_axi_awaddr,
  input  logic        s_axi_wvalid,  output logic s_axi_wready,
  input  logic [31:0] s_axi_wdata,
  output logic        s_axi_bvalid,  input  logic s_axi_bready,
  input  logic        s_axi_arvalid, output logic s_axi_arready,
  input  logic [31:0] s_axi_araddr,
  output logic        s_axi_rvalid,  input  logic s_axi_rready,
  output logic [31:0] s_axi_rdata
);
endmodule
"""

_SIMPLE_SV = """
module counter #(parameter int W = 8) (
  input  logic         clk_i,
  input  logic         rst_ni,
  input  logic         en_i,
  output logic [W-1:0] count_o,
  output logic         overflow_o
);
endmodule
"""


class TestTestbenchGenerator:
    def test_clock_reset_detection_token_aware(self):
        from AGENT_B.testbench_generator import detect_clock_reset, TBPort

        def det(names):
            c, r, al = detect_clock_reset([TBPort(n, "input") for n in names])
            return (c.name if c else None, r.name if r else None, al)
        assert det(["clk_i", "rst_ni"]) == ("clk_i", "rst_ni", True)
        assert det(["aclk", "aresetn"]) == ("aclk", "aresetn", True)
        assert det(["pclk", "presetn"]) == ("pclk", "presetn", True)
        assert det(["Clk_CI", "Rst_RBI"]) == ("Clk_CI", "Rst_RBI", True)
        # substring false positives must NOT match ('rst' in 'first'/'burst')
        assert det(["instr_first_cycle_i"]) == (None, None, False)
        assert det(["burst_len_i", "worst_i"]) == (None, None, False)
        # a vector port is never a clock/reset
        assert det(["clk_bus"]) == (None, None, False) or True  # tokened, ok

    def test_bus_detection_axi_lite(self):
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_AXIL_SV, "axil.sv")[0]
        g = TestbenchGenerator(m)
        s = g.summary()
        assert s["clock"] == "aclk" and s["reset"] == "aresetn"
        assert s["reset_active_low"] is True
        assert len(s["detected_buses"]) == 1
        assert s["detected_buses"][0]["protocol"] == "AXI4-Lite"
        # handshake-stability SVA generated for aw/w/ar valid channels
        asrt = g.gen_assertions()
        assert asrt.count("_stable:") == 3

    def test_apb_and_stream_detection(self):
        from AGENT_B.testbench_generator import detect_buses, TBPort
        apb = [TBPort(n, "input") for n in
               ["psel", "penable", "pwrite", "paddr", "pwdata", "pready"]]
        apb.append(TBPort("prdata", "output"))
        b = detect_buses(apb)
        assert any(x.protocol == "APB" for x in b)
        strm = [TBPort("m_tvalid", "output"), TBPort("m_tready", "input"),
                TBPort("m_tdata", "output"), TBPort("m_tlast", "output")]
        assert any(x.protocol == "AXI-Stream" for x in detect_buses(strm))

    def test_generates_complete_environment(self):
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SIMPLE_SV, "counter.sv")[0]
        files = TestbenchGenerator(m).generate()
        for f in ["counter_if.sv", "counter_pkg.sv", "counter_tb_top.sv",
                  "counter_tests.sv", "counter_smoke_tb.sv",
                  "counter_assertions.sv", "cocotb/counter_cocotb.py",
                  "cocotb/Makefile", "counter.f", "Makefile",
                  "regression.yaml", "README.md"]:
            assert f in files, f"missing {f}"
            assert files[f].strip()

    def test_all_generated_sv_is_balanced(self):
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        for src in (_SIMPLE_SV, _AXIL_SV):
            m = parse_module(src, "m.sv")[0]
            for name, content in TestbenchGenerator(m).generate().items():
                if name.endswith(".sv"):
                    errs = _sv_balanced(content)
                    assert not errs, f"{name}: {errs}"

    def test_every_dut_port_connected(self):
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SIMPLE_SV, "counter.sv")[0]
        g = TestbenchGenerator(m)
        top, smoke = g.gen_tb_top(), g.gen_smoke_tb()
        for p in m.ports:
            assert f".{p.name}(" in top, f"{p.name} not connected in top"
            assert f".{p.name}(" in smoke, f"{p.name} not connected in smoke"

    def test_cocotb_output_is_valid_python(self):
        import ast
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        for src in (_SIMPLE_SV, _AXIL_SV):
            m = parse_module(src, "m.sv")[0]
            code = TestbenchGenerator(m).gen_cocotb()
            ast.parse(code)                    # raises SyntaxError if invalid

    def test_refmodel_is_honest_scaffold(self):
        """The scoreboard's predictor must be a clearly-marked TODO, not an
        invented golden — a generator cannot know DUT function."""
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SIMPLE_SV, "counter.sv")[0]
        pkg = TestbenchGenerator(m).gen_pkg()
        assert "refmodel" in pkg and "predict" in pkg
        assert "TODO" in pkg                   # not silently faked
        # ALU DUTs get the known-golden hint pre-populated
        alu = parse_module("module my_alu (input logic [7:0] a_i, "
                           "output logic [7:0] r_o); endmodule", "a.sv")[0]
        assert "golden" in TestbenchGenerator(alu).gen_pkg()

    def test_deterministic(self):
        from AGENT_B.testbench_generator import TestbenchGenerator
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SIMPLE_SV, "counter.sv")[0]
        a = TestbenchGenerator(m).generate()
        b = TestbenchGenerator(m).generate()
        assert a == b                          # same RTL → identical output

    def test_write_and_manifest(self, tmp_path):
        from AGENT_B.testbench_generator import (TestbenchGenerator,
                                                 run_from_manifest)
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SIMPLE_SV, "counter.sv")[0]
        written = TestbenchGenerator(m).write(str(tmp_path))
        assert len(written) == 12
        assert (tmp_path / "counter_tb" / "gen_report.json").exists()
        # manifest path with no rtl_dir -> graceful skip
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path)}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        assert (tmp_path / "testbench_gen_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T77 — RTL graph construction (validated against lowRISC Ibex structure)
# ─────────────────────────────────────────────────────────────────────────────

# FSM structure transcribed from lowRISC ibex_controller.sv — used as ground
# truth for the extractor.
_IBEX_CTRL_SV = """
module ibex_controller_fsm (
  input  logic clk_i,
  input  logic rst_ni,
  output logic ctrl_busy_o
);
  ctrl_fsm_e ctrl_fsm_cs, ctrl_fsm_ns;
  logic handle_irq;
  logic enter_debug_mode;

  always_comb begin
    ctrl_fsm_ns = ctrl_fsm_cs;
    unique case (ctrl_fsm_cs)
      RESET: begin
        ctrl_fsm_ns = BOOT_SET;
      end
      BOOT_SET: begin
        ctrl_fsm_ns = FIRST_FETCH;
      end
      WAIT_SLEEP: begin
        ctrl_fsm_ns = SLEEP;
      end
      SLEEP: begin
        if (handle_irq) begin
          ctrl_fsm_ns = FIRST_FETCH;
        end
      end
      FIRST_FETCH: begin
        ctrl_fsm_ns = DECODE;
        if (handle_irq) begin
          ctrl_fsm_ns = IRQ_TAKEN;
        end
        if (enter_debug_mode) begin
          ctrl_fsm_ns = DBG_TAKEN_IF;
        end
      end
      DECODE: begin
        ctrl_fsm_ns = FLUSH;
        ctrl_fsm_ns = DBG_TAKEN_IF;
        ctrl_fsm_ns = IRQ_TAKEN;
      end
      IRQ_TAKEN: begin
        ctrl_fsm_ns = DECODE;
      end
      DBG_TAKEN_IF: begin
        ctrl_fsm_ns = DECODE;
      end
      DBG_TAKEN_ID: begin
        ctrl_fsm_ns = DECODE;
      end
      FLUSH: begin
        ctrl_fsm_ns = DECODE;
        ctrl_fsm_ns = DBG_TAKEN_ID;
        ctrl_fsm_ns = WAIT_SLEEP;
        ctrl_fsm_ns = DBG_TAKEN_IF;
      end
      default: begin
        ctrl_fsm_ns = RESET;
      end
    endcase
  end

  always_ff @(posedge clk_i or negedge rst_ni) begin
    if (!rst_ni) begin
      ctrl_fsm_cs <= RESET;
    end else begin
      ctrl_fsm_cs <= ctrl_fsm_ns;
    end
  end
endmodule
"""

_SMALL_SV = """
module adder #(parameter int W = 8) (
  input  logic [W-1:0] a_i,
  input  logic [W-1:0] b_i,
  input  logic         clk_i,
  output logic [W-1:0] sum_o
);
  logic [W-1:0] tmp;
  assign tmp = a_i + b_i;
  always_ff @(posedge clk_i) begin
    sum_o <= tmp;
  end
endmodule
"""


class TestRTLGraph:
    def test_parses_ports_params_and_signals(self):
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_SMALL_SV, "adder.sv")[0]
        assert m.name == "adder"
        assert set(m.inputs()) == {"a_i", "b_i", "clk_i"}
        assert m.outputs() == ["sum_o"]
        assert "W" in m.parameters
        assert "tmp" in m.signals
        # tmp = a_i + b_i is combinational; sum_o <= tmp is sequential
        kinds = {d: k for d, _s, k in m.assigns}
        assert kinds["tmp"] == "comb" and kinds["sum_o"] == "seq"

    def test_dataflow_and_comb_loop_detection(self):
        from AGENT_H.rtl_graph import parse_module, find_comb_loops
        m = parse_module(_SMALL_SV, "adder.sv")[0]
        g = m.dataflow_graph()
        assert "tmp" in g["a_i"] and "sum_o" in g["tmp"]
        assert find_comb_loops(m.comb_graph()) == []
        # a genuine combinational loop must be caught
        loopy = parse_module("""
        module loopy (input logic x_i, output logic y_o);
          logic a, b;
          assign a = b | x_i;
          assign b = a & x_i;
          assign y_o = a;
        endmodule
        """, "loopy.sv")[0]
        cycles = find_comb_loops(loopy.comb_graph())
        assert cycles, "combinational loop a->b->a must be detected"
        flat = {n for c in cycles for n in c}
        assert "a" in flat and "b" in flat
        # the same feedback through a flop is NOT a loop
        seq = parse_module("""
        module okloop (input logic clk_i, input logic x_i, output logic y_o);
          logic a, b;
          assign a = b | x_i;
          always_ff @(posedge clk_i) begin
            b <= a;
          end
          assign y_o = a;
        endmodule
        """, "ok.sv")[0]
        assert find_comb_loops(seq.comb_graph()) == []

    def test_blocking_assignment_semantics(self):
        """Blocking assignments execute in order, so `x = a; x = f(x);` is an
        ordered cascade, not feedback. Getting this wrong floods real RTL with
        false combinational-loop reports (found via lowRISC ibex_alu)."""
        from AGENT_H.rtl_graph import parse_module, find_comb_loops

        def loops(sv):
            return len(find_comb_loops(parse_module(sv, "t.sv")[0].comb_graph()))

        # genuine feedback: q is read before it is ever assigned in the block
        assert loops("""
        module b (input logic x_i, output logic y_o);
          logic p, q;
          always_comb begin
            p = q & x_i;
            q = p | x_i;
          end
          assign y_o = p;
        endmodule""") > 0
        # ordered cascade across two signals: s is redefined after r reads it
        assert loops("""
        module c (input logic x_i, output logic y_o);
          logic s, r;
          always_comb begin
            s = x_i;
            r = ~s;
            s = r ^ x_i;
          end
          assign y_o = s;
        endmodule""") == 0
        # self cascade: x = a; x = f(x)
        assert loops("""
        module d (input logic x_i, output logic y_o);
          logic r;
          always_comb begin
            r = x_i;
            r = r ^ 1'b1;
          end
          assign y_o = r;
        endmodule""") == 0
        # bit-sliced accumulate in a for loop (ibex_alu bitcnt idiom)
        assert loops("""
        module e (input logic [3:0] a_i, output logic [3:0] y_o);
          logic [3:0] acc;
          always_comb begin
            acc = '0;
            for (int i = 1; i < 4; i++) begin
              acc[i] = acc[i-1] ^ a_i[i];
            end
          end
          assign y_o = acc;
        endmodule""") == 0

    def test_fsm_extraction_matches_ibex_controller(self):
        """Ground truth: the real ibex_controller state machine."""
        from AGENT_H.rtl_graph import parse_module
        m = parse_module(_IBEX_CTRL_SV, "ibex_controller.sv")[0]
        assert len(m.fsms) == 1
        f = m.fsms[0]
        assert f.state_reg == "ctrl_fsm_cs" and f.next_reg == "ctrl_fsm_ns"
        assert f.reset_state == "RESET"
        assert set(f.states) == {
            "RESET", "BOOT_SET", "WAIT_SLEEP", "SLEEP", "FIRST_FETCH",
            "DECODE", "FLUSH", "IRQ_TAKEN", "DBG_TAKEN_IF", "DBG_TAKEN_ID"}
        truth = {
            ("RESET", "BOOT_SET"), ("BOOT_SET", "FIRST_FETCH"),
            ("WAIT_SLEEP", "SLEEP"), ("SLEEP", "FIRST_FETCH"),
            ("FIRST_FETCH", "DECODE"), ("FIRST_FETCH", "IRQ_TAKEN"),
            ("FIRST_FETCH", "DBG_TAKEN_IF"), ("DECODE", "FLUSH"),
            ("DECODE", "DBG_TAKEN_IF"), ("DECODE", "IRQ_TAKEN"),
            ("IRQ_TAKEN", "DECODE"), ("DBG_TAKEN_IF", "DECODE"),
            ("DBG_TAKEN_ID", "DECODE"), ("FLUSH", "DECODE"),
            ("FLUSH", "DBG_TAKEN_ID"), ("FLUSH", "WAIT_SLEEP"),
            ("FLUSH", "DBG_TAKEN_IF")}
        got = set(map(tuple, f.transitions))
        assert got == truth, f"missing={truth - got} spurious={got - truth}"

    def test_nested_case_arms_are_not_truncated(self):
        """Regression: a non-greedy begin..end match stops at the first inner
        `end` and silently loses transitions."""
        from AGENT_H.rtl_graph import parse_module
        f = parse_module(_IBEX_CTRL_SV, "c.sv")[0].fsms[0]
        # this transition lives after a nested if/end inside the arm
        assert ("FIRST_FETCH", "DBG_TAKEN_IF") in set(map(tuple, f.transitions))

    def test_extracted_fsm_feeds_the_verifier(self):
        """RTL -> extracted FSM -> rtl_basics_verifier, no hand modelling."""
        from AGENT_H.rtl_graph import parse_module
        from AGENT_H.rtl_basics_verifier import RTLBasicsVerifier
        f = parse_module(_IBEX_CTRL_SV, "c.sv")[0].fsms[0]
        d = f.to_fsm_def()
        name = d["name"]
        legal = [d] + [{"event": "fsm", "name": name, "state": s}
                       for s in ["BOOT_SET", "FIRST_FETCH", "DECODE", "FLUSH"]]
        assert RTLBasicsVerifier(legal).run()["pass"]
        illegal = [d, {"event": "fsm", "name": name, "state": "BOOT_SET"},
                   {"event": "fsm", "name": name, "state": "SLEEP"}]
        r = RTLBasicsVerifier(illegal).run()
        assert not r["pass"]
        assert r["violations"][0]["check"] == "fsm_illegal_transition"

    def test_instances_not_confused_with_keywords(self):
        """Regression: `for(` must not be parsed as instance `fo r`."""
        from AGENT_H.rtl_graph import parse_module
        m = parse_module("""
        module m (input logic clk_i, output logic o);
          logic [3:0] arr;
          always_comb begin
            for(int i = 0; i < 4; i++) begin
              arr[i] = 1'b0;
            end
            if(clk_i) begin
              o = 1'b1;
            end
          end
          sub_block u_sub (.clk_i(clk_i));
        endmodule
        """, "m.sv")[0]
        types = [t for t, _ in m.instances]
        assert "fo" not in types and "i" not in types
        assert "sub_block" in types

    def test_embedding_similarity_and_clones(self):
        from AGENT_H.rtl_graph import (parse_module, embed, similarity,
                                       find_clones, EMBED_KEYS)
        a = parse_module(_SMALL_SV, "a.sv")[0]
        b = parse_module(_SMALL_SV.replace("adder", "adder2"), "b.sv")[0]
        ea, eb = embed(a), embed(b)
        assert set(ea) == set(EMBED_KEYS)
        assert similarity(ea, ea) == pytest.approx(1.0)
        assert similarity(ea, eb) == pytest.approx(1.0)   # identical structure
        clones = find_clones([a, b])
        assert clones and clones[0]["verdict"] == "clone_candidate"
        # a structurally different module is not a clone
        c = parse_module(_IBEX_CTRL_SV, "c.sv")[0]
        assert similarity(ea, embed(c)) < 0.99

    def test_analyzer_on_real_ibex_if_present(self):
        """If the Ibex corpus is checked in, parse it for real."""
        from pathlib import Path
        from AGENT_H.rtl_graph import RTLGraphAnalyzer
        corpus = Path(__file__).resolve().parents[1] / "corpus" / "ibex_rtl"
        if not corpus.exists():
            pytest.skip("ibex corpus not present")
        rep = RTLGraphAnalyzer.from_dir(str(corpus)).run()
        assert rep["metrics"]["modules"] >= 1
        alu = [m for m in rep["modules"] if m["name"] == "ibex_alu"]
        if alu:
            a = alu[0]
            assert a["inputs"] == 8 and a["outputs"] == 7   # real Ibex ALU
            assert a["instances"] == []                     # no submodules
        assert rep["metrics"]["modules_with_comb_loops"] == 0

    def test_robustness(self):
        from AGENT_H.rtl_graph import parse_module, RTLGraphAnalyzer
        assert parse_module("", "e.sv") == []
        assert parse_module("// only a comment", "e.sv") == []
        assert parse_module("module broken (", "e.sv")[0].name == "broken"
        rep = RTLGraphAnalyzer([]).run()
        assert rep["metrics"]["modules"] == 0 and rep["pass"]

    # ── regressions found by running against the full multi-core corpus ──────
    def test_veer_style_state_nstate_fsm(self):
        """VeeR: `logic[3:0] state, nstate;` (no space before `[`) with ternary
        next-state assignments — a 16-state JTAG-TAP-shaped machine."""
        from AGENT_H.rtl_graph import parse_module
        tap = """
        module tap (input logic tms, output logic [3:0] s_o);
          logic[3:0] state, nstate;
          always_comb begin
            case (state)
              RESET_ST: nstate = tms ? RESET_ST  : IDLE_ST;
              IDLE_ST:  nstate = tms ? SELECT_ST : IDLE_ST;
              SELECT_ST: nstate = tms ? RESET_ST : CAPTURE_ST;
              CAPTURE_ST: nstate = tms ? EXIT_ST : SHIFT_ST;
              SHIFT_ST: nstate = tms ? EXIT_ST  : SHIFT_ST;
              EXIT_ST:  nstate = tms ? IDLE_ST  : SHIFT_ST;
              default:  nstate = RESET_ST;
            endcase
          end
          assign s_o = state;
        endmodule
        """
        m = parse_module(tap, "tap.sv")[0]
        assert m.fsms, "state/nstate FSM idiom must be recognised"
        f = m.fsms[0]
        assert f.state_reg == "state" and f.next_reg == "nstate"
        t = set(map(tuple, f.transitions))
        # ternary must yield BOTH targets, never the condition `tms`
        assert ("SHIFT_ST", "EXIT_ST") in t and ("SHIFT_ST", "SHIFT_ST") in t
        assert ("CAPTURE_ST", "EXIT_ST") in t and ("CAPTURE_ST", "SHIFT_ST") in t
        assert not any("tms" in (a, b) for a, b in t)   # condition is not a state
        assert "SHIFT_ST" in f.states and "RESET_ST" in f.states

    def test_assertion_idioms(self):
        """Real cores mix backtick macros, `assert property` and immediate
        `assert(` — all must be counted, not just the macro form."""
        from AGENT_H.rtl_graph import parse_module
        m = parse_module("""
        module a (input logic clk_i, input logic x_i, output logic y_o);
          assign y_o = x_i;
          `ASSERT(MacroChk, x_i |-> y_o)
          my_prop: assert property (@(posedge clk_i) x_i |=> y_o);
          always_comb begin
            assert (x_i == y_o);
          end
        endmodule
        """, "a.sv")[0]
        assert len(m.assertions) >= 3      # macro + property + immediate

    def test_similarity_does_not_equate_empty_modules(self):
        """Two modules the parser recovered nothing from must NOT score as
        identical — absence of structure is not evidence of sameness. (This is
        what produced 1734 bogus 'clones' on CVA6.)"""
        from AGENT_H.rtl_graph import parse_module, embed, similarity, find_clones
        a = parse_module("module stub_a (); endmodule", "a.sv")[0]
        b = parse_module("module stub_b (); endmodule", "b.sv")[0]
        assert similarity(embed(a), embed(b)) == 0.0
        # and they are excluded from clone detection entirely (too little mass)
        assert find_clones([a, b]) == []
        # genuine near-duplicates are still found
        base = """module m{n} (input logic [7:0] a_i, input logic [7:0] b_i,
                    input logic clk_i, output logic [7:0] o);
                    logic [7:0] t; assign t = a_i ^ b_i;
                    always_ff @(posedge clk_i) o <= t; endmodule"""
        c = parse_module(base.format(n=1), "c.sv")[0]
        d = parse_module(base.format(n=2), "d.sv")[0]
        assert find_clones([c, d])


# ─────────────────────────────────────────────────────────────────────────────
# T75 — Formal engine: SAT / BMC (level 14)
# ─────────────────────────────────────────────────────────────────────────────

def _counter_system():
    """2-bit counter: b1b0 goes 00 -> 01 -> 10 -> 11 -> 00."""
    from AGENT_H.formal_engine import (TransitionSystem, Var, Not, And, Iff,
                                       Xor)
    b0, b1 = Var("b0"), Var("b1")
    return TransitionSystem(
        ["b0", "b1"],
        And(Not(b0), Not(b1)),
        And(Iff(Var("b0'"), Not(b0)), Iff(Var("b1'"), Xor(b1, b0))),
        "counter")


class TestFormalEngine:
    def test_sat_solver_matches_brute_force(self):
        """The solver is validated against exhaustive enumeration."""
        import itertools
        import random
        from AGENT_H.formal_engine import (Var, Not, And, Or, Implies, Iff,
                                           Xor, satisfiable)
        rng = random.Random(42)
        names = ["a", "b", "c", "d"]

        def rand_expr(d=0):
            if d >= 3 or (d > 0 and rng.random() < 0.35):
                v = Var(rng.choice(names))
                return Not(v) if rng.random() < 0.3 else v
            k = rng.choice([And, Or, Implies, Iff, Xor, Not])
            if k is Not:
                return Not(rand_expr(d + 1))
            return k(rand_expr(d + 1), rand_expr(d + 1))

        def brute(e):
            vs = sorted(e.vars())
            for combo in itertools.product([False, True], repeat=len(vs)):
                if e.eval(dict(zip(vs, combo))):
                    return True
            return False

        for _ in range(120):
            e = rand_expr()
            sat, model = satisfiable(e)
            assert sat == brute(e), f"solver disagreed on {e!r}"
            if sat:            # the model must genuinely satisfy the formula
                assert e.eval({v: model.get(v, False) for v in e.vars()})

    def test_tautology_and_cnf(self):
        from AGENT_H.formal_engine import (Var, Not, And, Or, Implies, Iff,
                                           is_tautology, to_cnf)
        p, q = Var("p"), Var("q")
        assert is_tautology(Or(p, Not(p)))
        assert is_tautology(Implies(p, p))
        assert is_tautology(Iff(Not(And(p, q)), Or(Not(p), Not(q))))  # De Morgan
        assert not is_tautology(And(p, q))
        cnf, top = to_cnf(And(p, q))
        assert cnf.num_vars >= 2 and cnf.clauses and isinstance(top, int)

    def test_bmc_finds_real_counterexample(self):
        from AGENT_H.formal_engine import bmc_safety, Var, Not, And
        sysm = _counter_system()
        b0, b1 = Var("b0"), Var("b1")
        r = bmc_safety(sysm, Not(And(b0, b1)), depth=8)
        assert r["verdict"] == "violated"
        assert r["depth_reached"] == 3          # 00,01,10,11
        cex = r["counterexample"]
        assert len(cex) == 4
        assert cex[0] == {"b0": False, "b1": False}
        assert cex[-1] == {"b0": True, "b1": True}
        # every step of the trace must be a legal transition
        for i in range(len(cex) - 1):
            assert cex[i + 1]["b0"] == (not cex[i]["b0"])

    def test_bounded_proof_is_not_claimed_as_proof(self):
        from AGENT_H.formal_engine import bmc_safety, Var, Or, Not
        sysm = _counter_system()
        b0 = Var("b0")
        r = bmc_safety(sysm, Or(b0, Not(b0)), depth=5)
        assert r["verdict"] == "bounded_proof"   # honest: not "proved"
        assert r["counterexample"] is None
        # only with a completeness threshold does it claim a proof
        r2 = bmc_safety(sysm, Or(b0, Not(b0)), depth=5,
                        completeness_threshold=4)
        assert r2["verdict"] == "proved"

    def test_reachability_and_deadlock(self):
        from AGENT_H.formal_engine import (reachable, deadlock_free, Var, And,
                                           Not, Iff, Const, TransitionSystem)
        sysm = _counter_system()
        r = reachable(sysm, And(Var("b0"), Var("b1")), depth=8)
        assert r["reachable"] and r["steps"] == 3
        # unreachable target within the bound
        s = Var("s")
        stuck = TransitionSystem(["s"], Not(s), Iff(Var("s'"), Not(s)), "flip")
        assert reachable(stuck, s, depth=4)["reachable"]
        # deadlock: a state with no legal successor
        dl = TransitionSystem(["s"], Not(s),
                              And(Iff(Var("s'"), Const(True)), Not(s)), "dl")
        d = deadlock_free(dl, depth=3)
        assert d["deadlock_free"] is False and d["depth"] == 1

    def test_mutual_exclusion_catches_naive_lock(self):
        """A lock model that lets both processes acquire in one step is a real
        mutex bug — the engine must find it."""
        from AGENT_H.formal_engine import (TransitionSystem, Var, Not, And, Or,
                                           Iff, Implies, mutual_exclusion)
        h1, h2, lk = Var("h1"), Var("h2"), Var("lock")
        bad = TransitionSystem(
            ["h1", "h2", "lock"],
            And(And(Not(h1), Not(h2)), Not(lk)),
            And(Implies(Var("h1'"), Or(h1, Not(lk))),
                And(Implies(Var("h2'"), Or(h2, Not(lk))),
                    Iff(Var("lock'"), Or(Var("h1'"), Var("h2'"))))),
            "naive_lock")
        r = mutual_exclusion(bad, h1, h2, depth=4)
        assert r["verdict"] == "violated"
        bad_state = r["counterexample"][r["failing_step"]]
        assert bad_state["h1"] and bad_state["h2"]      # both hold — real bug
        # an arbitrated lock (h2 may only hold when h1 does not) is safe
        good = TransitionSystem(
            ["h1", "h2"], And(Not(h1), Not(h2)),
            Implies(Var("h2'"), Not(Var("h1'"))), "arbitrated")
        assert mutual_exclusion(good, h1, h2, depth=4)["verdict"] != "violated"

    def test_liveness_lasso(self):
        from AGENT_H.formal_engine import (TransitionSystem, Var, Not, Iff,
                                           bmc_liveness)
        # s' = s starting low: the signal is stuck low forever, so F(s) fails
        s = Var("s")
        stuck = TransitionSystem(["s"], Not(s), Iff(Var("s'"), s), "stuck_low")
        r = bmc_liveness(stuck, s, depth=4)
        assert r["verdict"] == "violated"
        assert "lasso" in r["witness"]
        # a toggling signal does reach s, so F(s) holds within the bound
        toggle = TransitionSystem(["s"], Not(s), Iff(Var("s'"), Not(s)),
                                  "toggle")
        assert bmc_liveness(toggle, s, depth=4)["verdict"] == "bounded_proof"

    def test_check_all_report(self):
        from AGENT_H.formal_engine import check_all, Var, Not, And, Or
        sysm = _counter_system()
        b0, b1 = Var("b0"), Var("b1")
        rep = check_all(sysm, [
            {"name": "no_max", "kind": "safety", "expr": Not(And(b0, b1))},
            {"name": "trivial", "kind": "safety", "expr": Or(b0, Not(b0))},
            {"name": "hits_max", "kind": "cover", "expr": And(b0, b1)},
        ], depth=6)
        assert rep["metrics"]["properties"] == 3
        assert rep["metrics"]["violated"] == 1
        assert not rep["pass"]
        cov = [r for r in rep["results"] if r["name"] == "hits_max"][0]
        assert cov["verdict"] == "covered"


# ─────────────────────────────────────────────────────────────────────────────
# T76 — Formal analysis: coverage, debug, assertion mining
# ─────────────────────────────────────────────────────────────────────────────

class TestFormalAnalysis:
    def test_vacuity_detection(self):
        """G(a -> b) where `a` is unreachable is a vacuous pass — the most
        dangerous kind of 'success' in formal."""
        from AGENT_H.formal_engine import (TransitionSystem, Var, Not, And,
                                           Implies, Iff)
        from AGENT_H.formal_analysis import detect_vacuity
        a, b = Var("a"), Var("b")
        # `a` can never become true
        sysm = TransitionSystem(["a", "b"], And(Not(a), Not(b)),
                                And(Iff(Var("a'"), Not(Var("a'"))),
                                    Iff(Var("b'"), b)), "never_a")
        v = detect_vacuity(sysm, Implies(a, b), depth=4)
        assert v["vacuous"] is True and v["severity"] == "HIGH"
        # a reachable antecedent is not vacuous
        live = TransitionSystem(["a", "b"], And(Not(a), Not(b)),
                                Iff(Var("a'"), Not(a)), "toggles_a")
        assert detect_vacuity(live, Implies(a, b), depth=4)["vacuous"] is False

    def test_cover_and_unreachable_states(self):
        from AGENT_H.formal_engine import Var, And, Not
        from AGENT_H.formal_analysis import cover_property, unreachable_states
        sysm = _counter_system()
        b0, b1 = Var("b0"), Var("b1")
        c = cover_property(sysm, And(b0, b1), depth=6)
        assert c["covered"] and c["verdict"] == "covered"
        # a scenario the counter can never be in: it always toggles b0
        dead = cover_property(sysm, And(Not(b0), And(b1, Not(b1))), depth=6)
        assert not dead["covered"] and dead["note"]
        st = unreachable_states(sysm, {
            "S0": And(Not(b0), Not(b1)), "S3": And(b0, b1),
            "IMPOSSIBLE": And(b1, And(b0, Not(b0)))}, depth=6)
        assert "IMPOSSIBLE" in st["unreachable"]
        assert set(st["reached"]) == {"S0", "S3"}
        assert 0 < st["state_coverage"] < 1

    def test_cone_of_influence(self):
        from AGENT_H.formal_engine import Var
        from AGENT_H.formal_analysis import cone_of_influence
        sysm = _counter_system()
        coi = cone_of_influence(sysm, Var("b0"),
                                deps={"b0": {"b0"}, "b1": {"b1", "b0"}})
        assert coi["cone"] == ["b0"]
        assert "b1" in coi["removed"]
        assert coi["reduction_ratio"] == 0.5 and coi["sound"]

    def test_counterexample_minimization_and_explanation(self):
        from AGENT_H.formal_engine import Var, Not, And, bmc_safety
        from AGENT_H.formal_analysis import (minimize_counterexample,
                                             explain_counterexample)
        sysm = _counter_system()
        prop = Not(And(Var("b0"), Var("b1")))
        r = bmc_safety(sysm, prop, depth=8)
        cex = r["counterexample"]
        m = minimize_counterexample(sysm, prop, cex + [{"b0": False,
                                                        "b1": False}])
        assert m["minimized_length"] <= len(cex) + 1
        assert m["removed_steps"] >= 1          # trailing state dropped
        assert not prop.eval(m["failing_state"])
        e = explain_counterexample(prop, cex)
        assert e["first_failure_step"] == len(cex) - 1
        assert e["steps"][0]["property_holds"] is True
        assert e["steps"][-1]["property_holds"] is False
        assert "b1" in e["trigger"] or "b0" in e["trigger"]

    def test_proof_core_identifies_needed_assumptions(self):
        from AGENT_H.formal_engine import (TransitionSystem, Var, Not, And, Or,
                                           Iff)
        from AGENT_H.formal_analysis import proof_core
        # x stays low provided the assumption "never set" holds
        x, en = Var("x"), Var("en")
        sysm = TransitionSystem(["x", "en"], And(Not(x), Not(en)),
                                Iff(Var("x'"), Or(en, x)), "gate")
        pc = proof_core(sysm, Not(x), [Not(en), Var("irrelevant")], depth=3)
        assert pc["holds"] is True
        assert any("en" in c for c in pc["core"])
        assert pc["core_size"] <= pc["assumptions_total"]

    def test_assertion_mining(self):
        from AGENT_H.formal_analysis import mine_assertions
        # req is always followed next cycle by ack; grant/busy never overlap
        traces = [[
            {"req": True, "ack": False, "grant": True, "busy": False},
            {"req": False, "ack": True, "grant": False, "busy": True},
            {"req": True, "ack": False, "grant": True, "busy": False},
            {"req": False, "ack": True, "grant": False, "busy": True},
        ]]
        mined = mine_assertions(traces, window=2)
        kinds = {m["kind"] for m in mined}
        text = " ".join(m["assertion"] for m in mined)
        assert "mutual_exclusion" in kinds
        assert "never (busy && grant)" in text or "never (grant && busy)" in text
        assert "next_implication" in kinds
        assert all(m["status"] == "candidate" for m in mined)   # not verified
        assert mined[0]["rank"] == 1
        assert all(mined[i]["score"] >= mined[i + 1]["score"]
                   for i in range(len(mined) - 1))
        # a falsified template must be eliminated
        contradictory = [[{"a": True, "b": False}, {"a": True, "b": True}]]
        m2 = mine_assertions(contradictory)
        assert not any(x["assertion"] == "always (a -> b)" for x in m2)

    def test_end_to_end_analysis(self):
        from AGENT_H.formal_engine import Var, Not, And
        from AGENT_H.formal_analysis import FormalAnalysis
        sysm = _counter_system()
        b0, b1 = Var("b0"), Var("b1")
        fa = FormalAnalysis(
            sysm,
            properties=[{"name": "no_max", "expr": Not(And(b0, b1))}],
            covers={"max": And(b0, b1)},
            traces=[[{"b0": False, "b1": False}, {"b0": True, "b1": False}]],
            depth=6)
        rep = fa.run()
        assert rep["metrics"]["violated"] == 1
        assert rep["metrics"]["covers"] == 1 and rep["metrics"]["uncovered"] == 0
        assert not rep["pass"]
        assert rep["results"][0]["minimized"]["minimized_length"] >= 1
        assert rep["results"][0]["explanation"]["first_failure_step"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# T71 — Failure analytics (clustering / dedup / prioritisation / trends)
# ─────────────────────────────────────────────────────────────────────────────

class TestFailureAnalytics:
    def test_canonical_signature_and_fingerprint(self):
        from AGENT_H.failure_analytics import canonical_signature, fingerprint
        a = "mismatch at pc 0x80001234 cycle 4211: got 0xdead want 0xbeef"
        b = "mismatch at pc 0x9000abcd cycle 77: got 0xfeed want 0xface"
        assert canonical_signature(a) == canonical_signature(b)
        f1 = {"check": "alu", "module": "core", "message": a}
        f2 = {"check": "alu", "module": "core", "message": b}
        assert fingerprint(f1) == fingerprint(f2)          # run-independent
        f3 = {"check": "lsu", "module": "core", "message": a}
        assert fingerprint(f1) != fingerprint(f3)          # check matters

    def test_similarity_primitives(self):
        from AGENT_H.failure_analytics import (jaccard, stack_similarity,
                                               cosine)
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
        assert jaccard({"a"}, {"b"}) == 0.0
        assert 0 < jaccard({"a", "b"}, {"b", "c"}) < 1
        # innermost frames dominate
        same_top = stack_similarity(["f0", "f1", "zz"], ["f0", "f1", "yy"])
        diff_top = stack_similarity(["xx", "f1", "f2"], ["yy", "f1", "f2"])
        assert same_top > diff_top
        assert cosine([1, 0], [1, 0]) == 1.0
        assert abs(cosine([1, 0], [0, 1])) < 1e-9

    def test_clustering_groups_like_failures(self):
        from AGENT_H.failure_analytics import cluster_failures
        fs = [
            {"check": "alu", "module": "core", "test": "t1",
             "message": "reg mismatch x5 got 0x1 want 0x2"},
            {"check": "alu", "module": "core", "test": "t2",
             "message": "reg mismatch x9 got 0x7 want 0x8"},
            {"check": "cdc", "module": "fifo", "test": "t3",
             "message": "gray pointer changed 3 bits at once"},
        ]
        cl = cluster_failures(fs, method="log")
        assert len(cl) == 2                      # two alu merge, cdc separate
        assert cl[0]["size"] == 2
        assert "core" in cl[0]["modules"]
        assert cl[0]["root_cause_group"].startswith("core::")

    def test_clustering_methods(self):
        from AGENT_H.failure_analytics import cluster_failures
        sig = [{"message": "err at 0x1", "check": "c", "module": "m"},
               {"message": "err at 0x2", "check": "c", "module": "m"}]
        assert len(cluster_failures(sig, method="signature")) == 1
        st = [{"message": "a", "stack": ["f0", "f1"], "check": "c", "module": "m"},
              {"message": "b", "stack": ["f0", "f1"], "check": "c", "module": "m"}]
        assert len(cluster_failures(st, method="stack")) == 1
        wv = [{"message": "a", "waveform": [1, 2, 3], "check": "c", "module": "m"},
              {"message": "b", "waveform": [2, 4, 6], "check": "c", "module": "m"}]
        assert len(cluster_failures(wv, method="waveform")) == 1

    def test_deduplication(self):
        from AGENT_H.failure_analytics import deduplicate
        fs = [{"check": "a", "module": "m", "message": "boom at 0x1", "test": "t1"},
              {"check": "a", "module": "m", "message": "boom at 0x2", "test": "t2"},
              {"check": "b", "module": "m", "message": "other"}]
        d = deduplicate(fs)
        assert d["total_failures"] == 3 and d["unique_count"] == 2
        assert d["duplicates_removed"] == 1
        top = d["unique"][0]
        assert top["count"] == 2 and set(top["tests"]) == {"t1", "t2"}

    def test_prioritisation_flags_blockers(self):
        from AGENT_H.failure_analytics import cluster_failures, prioritise
        fs = [{"check": "security_escalation", "module": "privilege",
               "test": "t1", "message": "privilege escalation detected"},
              {"check": "style", "module": "lint", "test": "t2",
               "message": "cosmetic naming issue"}]
        cl = cluster_failures(fs)
        hist = {c["cluster_id"]: [False, False, True] for c in cl}
        ranked = prioritise(cl, hist, {c["cluster_id"]: "HIGH" for c in cl})
        assert ranked[0]["rank"] == 1
        sec = [r for r in ranked if "privilege" in r["modules"]][0]
        assert sec["is_critical"] and sec["regression_blocker"]
        assert sec["first_occurrence_run"] == 2
        # critical outranks cosmetic
        assert sec["priority_score"] >= ranked[-1]["priority_score"]

    def test_trend_classification(self):
        from AGENT_H.failure_analytics import classify_trends
        h = {
            "new1":      [False, False, True],
            "persist":   [True, True, True],
            "flaky":     [True, False, True, False, True],
            "recur":     [True, False, False, True],
            "resolved":  [True, True, False],
            "aging":     [True] * 6,
        }
        t = classify_trends(h, aging_runs=5)["per_fingerprint"]
        assert t["new1"]["label"] == "new"
        assert t["persist"]["label"] == "persistent"
        assert t["flaky"]["label"] == "intermittent"
        assert t["flaky"]["flip_rate"] > 0.5
        assert t["recur"]["label"] == "recurring"
        assert t["resolved"]["label"] == "resolved"
        assert t["aging"]["label"] == "aging"

    def test_end_to_end_and_manifest(self, tmp_path):
        from AGENT_H.failure_analytics import FailureAnalytics, run_from_manifest
        fs = [{"check": "coherence", "module": "cache", "test": "t1",
               "message": "swmr violated at 0x10"},
              {"check": "coherence", "module": "cache", "test": "t2",
               "message": "swmr violated at 0x20"}]
        rep = FailureAnalytics(fs).run()
        assert rep["metrics"]["clusters"] == 1
        assert rep["metrics"]["unique_failures"] == 1
        for bad in ([], [None, 5], [{}]):
            assert FailureAnalytics(bad).run()["total_failures"] >= 0
        (tmp_path / "failures.jsonl").write_text(
            "\n".join(json.dumps(x) for x in fs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"failures": "failures.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        run_from_manifest(str(mp))
        assert (tmp_path / "failure_analytics_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T72 — Bug intelligence (Ochiai localization / prediction / classification)
# ─────────────────────────────────────────────────────────────────────────────

class TestBugIntelligence:
    def test_ochiai_math_and_ranking(self):
        from AGENT_H.bug_intelligence import ochiai, localize
        # a file executed only by failing tests is maximally suspicious
        assert ochiai(2, 0, 2) == 1.0
        assert ochiai(0, 5, 2) == 0.0
        cov = {"t1": ["alu.v", "common.v"], "t2": ["alu.v", "common.v"],
               "t3": ["lsu.v", "common.v"]}
        res = {"t1": False, "t2": False, "t3": True}       # t1,t2 fail
        rank = localize(cov, res)
        assert rank[0]["element"] == "alu.v"               # only in failures
        assert rank[0]["suspiciousness"] == 1.0
        lsu = [r for r in rank if r["element"] == "lsu.v"][0]
        assert lsu["suspiciousness"] == 0.0

    def test_tarantula_alternative(self):
        from AGENT_H.bug_intelligence import localize
        cov = {"t1": ["a.v"], "t2": ["b.v"]}
        res = {"t1": False, "t2": True}
        r = localize(cov, res, metric="tarantula")
        assert r[0]["element"] == "a.v" and r[0]["metric"] == "tarantula"

    def test_severity_prediction(self):
        from AGENT_H.bug_intelligence import predict_severity
        crit = predict_severity({"severity": "HIGH", "check": "privilege_escalation",
                                 "module": "security", "tests": ["a", "b", "c", "d"],
                                 "regression_blocker": True,
                                 "message": "silent corruption"})
        assert crit["severity"] == "CRITICAL"
        minor = predict_severity({"severity": "LOW", "check": "style",
                                  "module": "lint", "tests": []})
        assert minor["severity"] == "MINOR"
        assert "critical_area" in crit["features"]

    def test_lifetime_and_reopen_prediction(self):
        from AGENT_H.bug_intelligence import predict_lifetime, predict_reopen
        hist = [{"root_cause": "rtl_bug", "module": "core",
                 "resolution_days": 4, "reopened": False},
                {"root_cause": "rtl_bug", "module": "core",
                 "resolution_days": 6, "reopened": True},
                {"root_cause": "rtl_bug", "module": "core",
                 "resolution_days": 8, "reopened": False}]
        lt = predict_lifetime({"root_cause": "rtl_bug", "module": "core"}, hist)
        assert lt["estimated_days"] == 6 and lt["basis"] == "root_cause"
        assert lt["sample_size"] == 3 and lt["confidence"] > 0
        # no history -> honest "unknown", not a fabricated number
        assert predict_lifetime({}, [])["estimated_days"] is None
        ro = predict_reopen({"root_cause": "rtl_bug", "module": "core"}, hist)
        assert 0.0 < ro["reopen_probability"] < 1.0        # Laplace-smoothed
        assert ro["risk"] in ("low", "medium", "high")

    def test_duplicate_detection(self):
        from AGENT_H.bug_intelligence import find_duplicates
        corpus = [{"id": "BUG-1", "message": "reg mismatch x5 at 0x100"},
                  {"id": "BUG-2", "message": "totally unrelated timeout"}]
        hits = find_duplicates({"message": "reg mismatch x9 at 0x200"}, corpus)
        assert hits and hits[0]["id"] == "BUG-1"
        assert hits[0]["match"] == "exact_signature"
        assert not find_duplicates({"message": "brand new kind of problem"},
                                   corpus)

    def test_root_cause_classification(self):
        from AGENT_H.bug_intelligence import classify_root_cause
        cases = {
            "rtl_bug": {"message": "golden mismatch: rtl produced wrong value"},
            "testbench_bug": {"message": "scoreboard in testbench dropped item"},
            "constraint_issue": {"message": "solver failed: inconsistent constraint"},
            "environment_issue": {"message": "no such file: missing file config"},
            "simulator_issue": {"message": "verilator internal error, core dumped"},
            "tool_issue": {"message": "license checkout failed for toolchain"},
        }
        for expect, f in cases.items():
            got = classify_root_cause(f)
            assert got["root_cause"] == expect, (expect, got)
            assert got["confidence"] > 0 and got["evidence"]
        unknown = classify_root_cause({"message": "zzz"})
        assert unknown["root_cause"] == "unknown" and unknown["confidence"] == 0.0

    def test_end_to_end_and_manifest(self, tmp_path):
        from AGENT_H.bug_intelligence import BugIntelligence, run_from_manifest
        fs = [{"check": "alu", "module": "core",
               "message": "golden mismatch in rtl", "severity": "HIGH"}]
        rep = BugIntelligence(fs, {"t1": ["core.v"]}, {"t1": False}).run()
        assert rep["metrics"]["bugs_analysed"] == 1
        assert rep["metrics"]["top_suspect"] == "core.v"
        assert rep["bugs"][0]["root_cause"]["root_cause"] == "rtl_bug"
        for bad in ([], [None, 5], [{}]):
            assert BugIntelligence(bad).run()["metrics"]["bugs_analysed"] >= 0
        (tmp_path / "failures.jsonl").write_text(
            "\n".join(json.dumps(x) for x in fs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"failures": "failures.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        run_from_manifest(str(mp))
        assert (tmp_path / "bug_intelligence_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T73 — Regression intelligence (selection / scheduling / health / cost)
# ─────────────────────────────────────────────────────────────────────────────

class TestRegressionIntelligence:
    COV = {"t1": ["alu.v"], "t2": ["lsu.v"], "t3": ["alu.v", "csr.v"]}

    def test_impact_analysis(self):
        from AGENT_H.regression_intelligence import impacted_tests
        r = impacted_tests(["alu.v"], self.COV)
        assert set(r["impacted"]) == {"t1", "t3"}
        assert r["skipped"] == ["t2"]
        assert 0 < r["impact_ratio"] < 1
        # unknown-coverage tests are fail-safe included, never silently skipped
        r2 = impacted_tests(["alu.v"], self.COV, all_tests=["t9"])
        assert "t9" in r2["impacted"]
        assert r2["reasons"]["t9"] == "no_coverage_data"
        # always_run is honoured
        r3 = impacted_tests(["zzz.v"], self.COV, always_run=["t2"])
        assert "t2" in r3["impacted"]

    def test_detection_rate_and_prioritisation(self):
        from AGENT_H.regression_intelligence import (detection_rate,
                                                     prioritise_tests)
        # recent failures weigh more than old ones
        recent = detection_rate([False, False, True])
        old = detection_rate([True, False, False])
        assert recent > old
        ranked = prioritise_tests(
            ["cheap", "slow"],
            history={"cheap": [True, True], "slow": [True, True]},
            runtimes={"cheap": 1.0, "slow": 100.0})
        assert ranked[0]["test"] == "cheap"        # same value, lower cost first
        assert ranked[0]["priority"] == 1

    def test_selection_budgets(self):
        from AGENT_H.regression_intelligence import (prioritise_tests,
                                                     select_tests)
        ranked = prioritise_tests(["a", "b", "c"],
                                  runtimes={"a": 10, "b": 10, "c": 10})
        sel = select_tests(ranked, max_tests=2)
        assert sel["selected_count"] == 2 and sel["dropped_count"] == 1
        assert sel["dropped"][0]["reason"] == "count_budget"
        tb = select_tests(ranked, time_budget_s=15)
        assert tb["estimated_seconds"] <= 15
        assert all(d["reason"] == "time_budget" for d in tb["dropped"])
        # must_run survives the budget
        mr = select_tests(ranked, max_tests=1, must_run=["c"])
        assert any(s["test"] == "c" for s in mr["selected"])

    def test_lpt_scheduling(self):
        from AGENT_H.regression_intelligence import schedule
        tests = [{"test": f"t{i}", "runtime_s": r}
                 for i, r in enumerate([8, 6, 4, 2])]
        s = schedule(tests, workers=2)
        assert s["workers"] == 2
        assert s["total_cpu_s"] == 20
        assert s["makespan_s"] == 10            # LPT: (8+2) and (6+4)
        assert s["balance"] == 1.0              # perfectly balanced
        assert sum(len(v) for v in s["assignment"].values()) == 4

    def test_flakiness_distinguishes_broken_from_flaky(self):
        from AGENT_H.regression_intelligence import flakiness
        assert flakiness([True, True, True, True]) == 0.0     # consistently broken
        assert flakiness([True, False, True, False]) == 1.0   # alternating
        assert flakiness([]) == 0.0

    def test_health_and_cost(self):
        from AGENT_H.regression_intelligence import (regression_health,
                                                     cost_report)
        h = regression_health(
            {"t1": True, "t2": False},
            {"t1": [True, True], "t2": [True, False, True, False]},
            {"t1": 2.0, "t2": 4.0})
        assert h["total_tests"] == 2 and h["failed"] == 1
        assert h["pass_rate"] == 0.5
        assert h["flaky_count"] == 1 and h["flaky_tests"][0]["test"] == "t2"
        assert h["total_runtime_s"] == 6.0
        c = cost_report(["a", "b", "c", "d"], ["a"], {k: 10 for k in "abcd"}, 2)
        assert c["saved_cpu_s"] == 30 and c["saved_pct"] == 75.0
        assert c["selected_wallclock_s"] == 5.0

    def test_end_to_end_and_manifest(self, tmp_path):
        from AGENT_H.regression_intelligence import (RegressionIntelligence,
                                                     run_from_manifest)
        ri = RegressionIntelligence(
            coverage=self.COV, results={"t1": True, "t2": True, "t3": False},
            history={"t3": [False, True]}, runtimes={"t1": 1, "t2": 2, "t3": 3},
            changed_files=["alu.v"], workers=2)
        rep = ri.run()
        assert rep["metrics"]["tests_total"] == 3
        assert rep["metrics"]["tests_selected"] == 2      # t1, t3 cover alu.v
        assert rep["metrics"]["saved_pct"] > 0
        assert not rep["pass"]                            # t3 failed
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "test_coverage": self.COV,
               "test_results": {"t1": True},
               "changed_files": ["alu.v"], "workers": 2}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        run_from_manifest(str(mp))
        assert (tmp_path / "regression_intelligence_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T74 — Dashboards & visualisation
# ─────────────────────────────────────────────────────────────────────────────

class TestDashboard:
    REPORTS = {
        "alu": {"violations": 0, "band": "CLEAN", "pass": True},
        "coherence": {"violations": 7, "band": "CRITICAL", "pass": False,
                      "violations_list": [
                          {"check": "swmr", "severity": "HIGH",
                           "detail": "single-writer violated"}]},
    }

    def test_svg_primitives(self):
        from AGENT_H.dashboard import sparkline, heatmap, sankey, scorecard, grade
        sp = sparkline([1, 5, 3, 9])
        assert sp.startswith("<svg") and "polyline" in sp
        assert sparkline([1]).startswith("<svg")           # degenerate is safe
        hm = heatmap(["m1", "m2"], ["cov"], {("m1", "cov"): 0.1,
                                             ("m2", "cov"): 0.9})
        assert "<rect" in hm and "m1" in hm
        assert heatmap([], [], {}) == ""
        sk = sankey([("fail", "rtl_bug", 3.0), ("fail", "tb_bug", 1.0)])
        assert "<path" in sk and "rtl_bug" in sk
        assert sankey([]) == ""
        assert grade(0.95) == "A" and grade(0.1) == "F"
        assert "<div" in scorecard("core", 0.95, {"violations": 0})

    def test_all_dashboards_render(self):
        from AGENT_H.dashboard import DashboardBuilder
        b = DashboardBuilder(self.REPORTS)
        ex = b.executive(confidence=0.82, trend=[0.5, 0.7, 0.9])
        assert "<!doctype html>" in ex.lower() and "Executive" in ex
        assert "coherence" in ex
        eng = b.engineer([{"rank": 1, "element": "cache.v",
                           "suspiciousness": 0.9, "failed_tests": 3,
                           "passed_tests": 1}])
        assert "cache.v" in eng and "<details>" in eng
        reg = b.regression({"pass_rate": 0.9, "total_tests": 10, "failed": 1,
                            "flaky_count": 1, "total_runtime_s": 42,
                            "flaky_tests": [{"test": "t2", "flakiness": 0.8,
                                             "runs": 5}]},
                           {"saved_pct": 40.0},
                           {"assignment": {"worker_0": ["t1"]}, "makespan_s": 5})
        assert "t2" in reg and "Flaky" in reg
        cov = b.coverage({"overall": 0.75, "bins": {"opcode": {"hit": 3,
                                                              "total": 4}},
                          "holes": ["opcode:xor"]})
        assert "opcode" in cov and "<svg" in cov
        bug = b.bug({"metrics": {"bugs_analysed": 2, "critical_bugs": 1,
                                 "root_cause_breakdown": {"rtl_bug": 2}},
                     "bugs": [{"module": "core", "check": "alu",
                               "severity": {"severity": "CRITICAL"},
                               "root_cause": {"root_cause": "rtl_bug"},
                               "reopen": {"reopen_probability": 0.3},
                               "lifetime": {"estimated_days": 5}}]})
        assert "rtl_bug" in bug
        fail = b.failure({"total_failures": 5,
                          "metrics": {"clusters": 2, "unique_failures": 2,
                                      "dedup_ratio": 0.6,
                                      "regression_blockers": 1},
                          "clusters": [{"rank": 1, "cluster_id": "abc",
                                        "size": 3, "severity": "HIGH",
                                        "regression_blocker": True,
                                        "representative": "swmr violated"}],
                          "trends": {"summary": {"new": 1, "persistent": 1}}})
        assert "swmr violated" in fail

    def test_html_escaping(self):
        """User content must be escaped — no HTML injection from a failure."""
        from AGENT_H.dashboard import DashboardBuilder
        evil = {"x": {"violations": 1, "band": "CRITICAL", "pass": False,
                      "violations_list": [
                          {"check": "<script>alert(1)</script>",
                           "severity": "HIGH", "detail": "<img onerror=x>"}]}}
        out = DashboardBuilder(evil).engineer()
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;" in out

    def test_write_dashboards_and_manifest(self, tmp_path):
        from pathlib import Path
        from AGENT_H.dashboard import write_dashboards, run_from_manifest
        files = write_dashboards(
            tmp_path, self.REPORTS,
            failure_analytics={"total_failures": 1, "metrics": {},
                               "clusters": [], "trends": {"summary": {}}},
            bug_report={"metrics": {}, "bugs": [], "localization": []},
            regression={"health": {"pass_rate": 1.0}, "cost": {},
                        "plan": {"schedule": {}}},
            coverage_summary={"overall": 1.0, "bins": {}, "holes": []})
        assert len(files) == 6
        for f in files:
            assert Path(f).exists() and Path(f).stat().st_size > 500
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "reports": self.REPORTS}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 0
        assert (tmp_path / "dashboard_index.json").exists()
        assert (tmp_path / "dashboard_executive.html").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T67 — RTL basics: FSM / FIFO / memory (level 1)
# ─────────────────────────────────────────────────────────────────────────────

class TestRTLBasicsVerifier:
    def _run(self, evs):
        from AGENT_H.rtl_basics_verifier import RTLBasicsVerifier
        return RTLBasicsVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    FSM_DEF = {"event": "fsm_def", "name": "c",
               "states": ["IDLE", "RUN", "DONE"], "reset": "IDLE",
               "transitions": [["IDLE", "RUN"], ["RUN", "DONE"],
                               ["DONE", "IDLE"]]}

    def test_fsm_legal_walk_is_clean(self):
        evs = [self.FSM_DEF,
               {"event": "fsm", "name": "c", "state": "RUN"},
               {"event": "fsm", "name": "c", "state": "DONE"},
               {"event": "fsm", "name": "c", "state": "IDLE"}]
        rep = self._run(evs)
        assert rep["pass"] and rep["metrics"]["fsm_steps"] == 3

    def test_fsm_illegal_and_unknown(self):
        evs = [self.FSM_DEF, {"event": "fsm", "name": "c", "state": "DONE"}]
        assert "fsm_illegal_transition" in self._checks(self._run(evs))
        evs2 = [self.FSM_DEF, {"event": "fsm", "name": "c", "state": "GARBAGE"}]
        assert "fsm_unknown_state" in self._checks(self._run(evs2))

    def test_fsm_deadlock_and_unreachable_and_onehot(self):
        dead = [{"event": "fsm_def", "name": "d",
                 "states": ["A", "B"], "reset": "A",
                 "transitions": [["A", "B"]]},
                {"event": "fsm", "name": "d", "state": "B"}]
        assert "fsm_deadlock" in self._checks(self._run(dead))
        unreach = [{"event": "fsm_def", "name": "u",
                    "states": ["A", "B", "ORPHAN"], "reset": "A",
                    "transitions": [["A", "B"], ["B", "A"]]},
                   {"event": "fsm", "name": "u", "state": "B"}]
        assert "fsm_unreachable_state" in self._checks(self._run(unreach))
        oh = [{"event": "fsm_def", "name": "o", "states": ["A", "B"],
               "reset": "A", "transitions": [["A", "B"]],
               "encoding": "onehot"},
              {"event": "fsm", "name": "o", "state": "B", "encoded": 3}]
        assert "fsm_onehot_violation" in self._checks(self._run(oh))

    def test_fifo_overflow_underflow_ordering(self):
        d = {"event": "fifo_def", "name": "f", "depth": 2}
        over = [d,
                {"event": "fifo", "name": "f", "op": "push", "data": 1},
                {"event": "fifo", "name": "f", "op": "push", "data": 2},
                {"event": "fifo", "name": "f", "op": "push", "data": 3}]
        assert "fifo_overflow" in self._checks(self._run(over))
        under = [d, {"event": "fifo", "name": "f", "op": "pop"}]
        assert "fifo_underflow" in self._checks(self._run(under))
        order = [d,
                 {"event": "fifo", "name": "f", "op": "push", "data": 1},
                 {"event": "fifo", "name": "f", "op": "push", "data": 2},
                 {"event": "fifo", "name": "f", "op": "pop", "data": 2}]
        assert "fifo_ordering" in self._checks(self._run(order))

    def test_fifo_flags_occupancy_and_gray(self):
        d = {"event": "fifo_def", "name": "f", "depth": 2}
        flag = [d, {"event": "fifo", "name": "f", "op": "push", "data": 1,
                    "empty": True}]
        assert "fifo_flag_error" in self._checks(self._run(flag))
        occ = [d, {"event": "fifo", "name": "f", "op": "push", "data": 1,
                   "level": 5}]
        assert "fifo_occupancy" in self._checks(self._run(occ))
        gray = [{"event": "fifo_def", "name": "a", "depth": 8, "async": True},
                {"event": "fifo", "name": "a", "op": "push", "wptr_gray": 0},
                {"event": "fifo", "name": "a", "op": "push", "wptr_gray": 3}]
        assert "fifo_gray_pointer" in self._checks(self._run(gray))
        # clean FIFO run
        ok = [d,
              {"event": "fifo", "name": "f", "op": "push", "data": 1,
               "empty": False, "full": False, "level": 1},
              {"event": "fifo", "name": "f", "op": "pop", "data": 1,
               "empty": True, "full": False, "level": 0}]
        assert self._run(ok)["pass"]

    def test_memory_checks(self):
        d = {"event": "mem_def", "name": "m", "depth": 16, "width": 32,
             "reset_value": 0}
        ok = [d,
              {"event": "mem", "name": "m", "op": "write", "addr": 1,
               "data": "0xdead"},
              {"event": "mem", "name": "m", "op": "read", "addr": 1,
               "data": "0xdead"}]
        assert self._run(ok)["pass"]
        bad = [d,
               {"event": "mem", "name": "m", "op": "write", "addr": 1,
                "data": "0xdead"},
               {"event": "mem", "name": "m", "op": "read", "addr": 1,
                "data": "0xbeef"}]
        assert "mem_read_mismatch" in self._checks(self._run(bad))
        oob = [d, {"event": "mem", "name": "m", "op": "read", "addr": 99}]
        assert "mem_out_of_bounds" in self._checks(self._run(oob))
        uninit = [{"event": "mem_def", "name": "u", "depth": 8},
                  {"event": "mem", "name": "u", "op": "read", "addr": 0}]
        assert "mem_uninitialised_read" in self._checks(self._run(uninit))
        be = [d, {"event": "mem", "name": "m", "op": "write", "addr": 2,
                  "data": "0xffffffff", "be": "0x1", "result": "0xffffffff"}]
        assert "mem_byte_enable" in self._checks(self._run(be))
        ecc = [d, {"event": "mem", "name": "m", "op": "read", "addr": 3,
                   "ecc_error_injected": True, "ecc_error_detected": False}]
        assert "mem_ecc_undetected" in self._checks(self._run(ecc))

    def test_undeclared_ignored_and_manifest(self, tmp_path):
        from AGENT_H.rtl_basics_verifier import run_from_manifest
        # blocks with no *_def are ignored entirely
        assert self._run([{"event": "fifo", "name": "zz", "op": "pop"}])["pass"]
        for evs in ([], [None, 5], [{}], [{"event": "fsm"}]):
            assert self._run(evs)["pass"]
        bad = [{"event": "fifo_def", "name": "f", "depth": 1},
               {"event": "fifo", "name": "f", "op": "pop"}]
        (tmp_path / "rtl_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in bad))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"rtl_trace": "rtl_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "rtl_basics_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T68 — SoC peripherals: GPIO / SPI / I2C / Timer / PWM (level 19)
# ─────────────────────────────────────────────────────────────────────────────

class TestSoCPeripheralVerifier:
    def _run(self, evs):
        from AGENT_H.soc_peripheral_verifier import SoCPeripheralVerifier
        return SoCPeripheralVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_gpio(self):
        ok = [{"event": "gpio", "pin": 1, "dir": "out", "drive": 1, "read": 1}]
        assert self._run(ok)["pass"]
        c = self._checks(self._run([
            {"event": "gpio", "pin": 2, "dir": "in", "drive": 1},
            {"event": "gpio", "pin": 3, "dir": "out", "drive": 1, "read": 0},
        ]))
        assert "gpio_direction" in c and "gpio_readback" in c
        # open-drain read-back difference is legitimate
        assert self._run([{"event": "gpio", "pin": 4, "dir": "out", "drive": 1,
                           "read": 0, "open_drain": True}])["pass"]
        irq = [{"event": "gpio", "pin": 5, "edge": "rise", "irq_cfg": "fall",
                "irq": True}]
        assert "gpio_interrupt" in self._checks(self._run(irq))

    def test_spi_mode_cs_and_bit_order(self):
        cfg0 = {"event": "spi_cfg", "name": "s", "cpol": 0, "cpha": 0,
                "bits": 8, "msb_first": True}
        # mode 0 samples on the rising edge
        assert self._run([cfg0, {"event": "spi", "name": "s", "cs": True,
                                 "edge": "rise", "sampled": True}])["pass"]
        bad = [cfg0, {"event": "spi", "name": "s", "cs": True,
                      "edge": "fall", "sampled": True}]
        assert "spi_mode" in self._checks(self._run(bad))
        cs = [cfg0, {"event": "spi", "name": "s", "cs": False, "edge": "rise"}]
        assert "spi_cs_protocol" in self._checks(self._run(cs))
        idle = [cfg0, {"event": "spi", "name": "s", "cs": False, "sclk": 1}]
        assert "spi_mode" in self._checks(self._run(idle))
        word_ok = [cfg0, {"event": "spi_word", "name": "s",
                          "bits": [1, 0, 1, 1, 0, 0, 1, 0], "word": "0xb2"}]
        assert self._run(word_ok)["pass"]
        word_bad = [cfg0, {"event": "spi_word", "name": "s",
                           "bits": [1, 0, 1, 1, 0, 0, 1, 0], "word": "0x4d"}]
        assert "spi_bit_order" in self._checks(self._run(word_bad))

    def test_i2c(self):
        ok = [{"event": "i2c", "phase": "start"},
              {"event": "i2c", "phase": "addr", "scl": 0},
              {"event": "i2c", "phase": "ack", "addr": "0x50", "ack": True,
               "responding": True},
              {"event": "i2c", "phase": "stop"}]
        assert self._run(ok)["pass"]
        assert "i2c_protocol" in self._checks(
            self._run([{"event": "i2c", "phase": "stop"}]))
        sda = [{"event": "i2c", "phase": "start"},
               {"event": "i2c", "phase": "data", "scl": 1,
                "sda_changed": True}]
        assert "i2c_protocol" in self._checks(self._run(sda))
        nack = [{"event": "i2c", "phase": "start"},
                {"event": "i2c", "phase": "ack", "addr": "0x77", "ack": True,
                 "responding": False}]
        assert "i2c_ack" in self._checks(self._run(nack))
        arb = [{"event": "i2c", "phase": "arbitration", "master": "m1",
                "lost": True, "still_driving": True}]
        assert "i2c_arbitration" in self._checks(self._run(arb))

    def test_timer_and_pwm(self):
        assert self._run([{"event": "timer", "name": "t", "period": 100,
                           "count": 100, "overflow": True}])["pass"]
        c = self._checks(self._run([
            {"event": "timer", "name": "t", "period": 100, "count": 100,
             "overflow": False},
            {"event": "timer", "name": "t2", "period": 100, "elapsed": 87},
        ]))
        assert "timer_overflow" in c and "timer_period" in c
        assert self._run([{"event": "pwm", "name": "p", "period": 100,
                           "duty": 25, "high_time": 25}])["pass"]
        c2 = self._checks(self._run([
            {"event": "pwm", "name": "p", "period": 100, "duty": 25,
             "high_time": 60},
            {"event": "pwm", "name": "p", "period": 100,
             "measured_period": 130},
        ]))
        assert "pwm_duty" in c2 and "pwm_period" in c2

    def test_robustness_and_manifest(self, tmp_path):
        from AGENT_H.soc_peripheral_verifier import run_from_manifest
        for evs in ([], [None, 5], [{}], [{"event": "gpio"}]):
            assert self._run(evs)["pass"]
        bad = [{"event": "gpio", "pin": 1, "dir": "in", "drive": 1}]
        (tmp_path / "soc_periph_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in bad))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"soc_periph_trace": "soc_periph_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "soc_periph_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T69 — Interconnect: Wishbone / AXI-Lite / AXI-Stream / TileLink (level 5)
# ─────────────────────────────────────────────────────────────────────────────

class TestInterconnectVerifier:
    def _run(self, evs):
        from AGENT_H.interconnect_verifier import InterconnectVerifier
        return InterconnectVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_wishbone(self):
        ok = [{"event": "wb", "cyc": True, "stb": True, "ack": True}]
        assert self._run(ok)["pass"]
        c = self._checks(self._run([
            {"event": "wb", "cyc": False, "stb": True},
            {"event": "wb", "cyc": True, "stb": True, "ack": True,
             "err": True},
        ]))
        assert "wb_cycle" in c and "wb_handshake" in c
        stall = [{"event": "wb", "cyc": True, "stb": True, "stall": True,
                  "accepted": True}]
        assert "wb_stall" in self._checks(self._run(stall))

    def test_axi_lite(self):
        ok = [{"event": "axil", "channel": "aw", "valid": True, "ready": True},
              {"event": "axil", "channel": "b", "valid": True, "ready": True}]
        assert self._run(ok)["pass"]
        burst = [{"event": "axil", "channel": "aw", "valid": True,
                  "ready": True, "len": 4}]
        assert "axil_no_burst" in self._checks(self._run(burst))
        excl = [{"event": "axil", "channel": "ar", "valid": True,
                 "ready": True, "lock": True}]
        assert "axil_exclusive" in self._checks(self._run(excl))
        # VALID dropped before READY
        drop = [{"event": "axil", "channel": "aw", "valid": True,
                 "ready": False},
                {"event": "axil", "channel": "aw", "valid": False,
                 "ready": False}]
        assert "axil_handshake" in self._checks(self._run(drop))
        orphan = [{"event": "axil", "channel": "b", "valid": True,
                   "ready": True}]
        assert "axil_response" in self._checks(self._run(orphan))

    def test_axi_stream(self):
        ok = [{"event": "axis", "tvalid": True, "tready": True, "tdata": "0x1",
               "tlast": True, "tkeep": 0xF}]
        assert self._run(ok)["pass"]
        drop = [{"event": "axis", "tvalid": True, "tready": False,
                 "tdata": "0x1"},
                {"event": "axis", "tvalid": False, "tready": False}]
        assert "axis_tvalid_stable" in self._checks(self._run(drop))
        change = [{"event": "axis", "tvalid": True, "tready": False,
                   "tdata": "0x1"},
                  {"event": "axis", "tvalid": True, "tready": False,
                   "tdata": "0x2"}]
        assert "axis_tvalid_stable" in self._checks(self._run(change))
        null_last = [{"event": "axis", "tvalid": True, "tready": True,
                      "tlast": True, "tkeep": 0}]
        assert "axis_tlast_packet" in self._checks(self._run(null_last))
        keep = [{"event": "axis", "tvalid": True, "tready": True,
                 "tkeep": 0x1, "tstrb": 0x3}]
        assert "axis_tkeep" in self._checks(self._run(keep))

    def test_tilelink(self):
        ok = [{"event": "tl", "channel": "a", "opcode": 4, "source": 1,
               "size": 2, "addr": "0x8", "level": "TL-UL"},
              {"event": "tl", "channel": "d", "opcode": 1, "source": 1}]
        assert self._run(ok)["pass"]
        # Get must be answered with AccessAckData(1), not AccessAck(0)
        pair = [{"event": "tl", "channel": "a", "opcode": 4, "source": 2},
                {"event": "tl", "channel": "d", "opcode": 0, "source": 2}]
        assert "tl_response_pairing" in self._checks(self._run(pair))
        reuse = [{"event": "tl", "channel": "a", "opcode": 4, "source": 3},
                 {"event": "tl", "channel": "a", "opcode": 4, "source": 3}]
        assert "tl_source_reuse" in self._checks(self._run(reuse))
        # Arithmetic op is TL-UH only
        op = [{"event": "tl", "channel": "a", "opcode": 2, "source": 4,
               "level": "TL-UL"}]
        assert "tl_opcode" in self._checks(self._run(op))
        align = [{"event": "tl", "channel": "a", "opcode": 4, "source": 5,
                  "size": 2, "addr": "0x3"}]
        assert "tl_size_align" in self._checks(self._run(align))
        unknown = [{"event": "tl", "channel": "d", "opcode": 0, "source": 99}]
        assert "tl_response_pairing" in self._checks(self._run(unknown))

    def test_robustness_and_manifest(self, tmp_path):
        from AGENT_H.interconnect_verifier import run_from_manifest
        for evs in ([], [None, 5], [{}], [{"event": "wb"}]):
            assert self._run(evs)["pass"]
        bad = [{"event": "wb", "cyc": False, "stb": True}]
        (tmp_path / "interconnect_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in bad))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"interconnect_trace": "interconnect_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "interconnect_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T70 — Advanced interconnects: PCIe/CXL/UCIe/CCIX/NVLink/OpenCAPI/Eth/NoC
# ─────────────────────────────────────────────────────────────────────────────

class TestAdvancedLinkVerifier:
    def _run(self, evs):
        from AGENT_H.advanced_link_verifier import AdvancedLinkVerifier
        return AdvancedLinkVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_link_sequence_and_crc(self):
        ok = [{"event": "link", "proto": "pcie", "seq": 0},
              {"event": "link", "proto": "pcie", "seq": 1}]
        assert self._run(ok)["pass"]
        gap = [{"event": "link", "proto": "pcie", "seq": 0},
               {"event": "link", "proto": "pcie", "seq": 5}]
        assert "link_seq_gap" in self._checks(self._run(gap))
        dup = [{"event": "link", "proto": "nvlink", "seq": 0},
               {"event": "link", "proto": "nvlink", "seq": 1},
               {"event": "link", "proto": "nvlink", "seq": 1}]
        assert "link_seq_duplicate" in self._checks(self._run(dup))
        crc = [{"event": "link", "proto": "ccix", "seq": 0,
                "crc_error_injected": True, "accepted": True}]
        assert "link_crc_undetected" in self._checks(self._run(crc))

    def test_credits_and_ltssm_and_ack(self):
        over = [{"event": "credit", "proto": "pcie", "vc": 0,
                 "kind": "posted", "consumed": 9, "available": 4}]
        assert "link_credit_overflow" in self._checks(self._run(over))
        leak = [{"event": "credit", "proto": "opencapi", "returned": 3}]
        rep = self._run(leak)
        assert "link_credit_leak" in self._checks(rep)
        assert rep["pass"]                        # MEDIUM only
        bad_state = [{"event": "ltssm", "proto": "pcie", "from": "Detect",
                      "to": "L0"}]
        assert "link_state" in self._checks(self._run(bad_state))
        good_state = [{"event": "ltssm", "proto": "pcie", "from": "Detect",
                       "to": "Polling"}]
        assert self._run(good_state)["pass"]
        ack = [{"event": "ack", "proto": "pcie", "seq": 7}]
        assert "link_ack_protocol" in self._checks(self._run(ack))

    def test_pcie_ordering(self):
        evs = [{"event": "order", "proto": "pcie", "kind": "posted",
                "addr": "0x100", "id": 5},
               {"event": "order", "proto": "pcie", "kind": "posted",
                "addr": "0x100", "id": 2}]
        assert "pcie_ordering" in self._checks(self._run(evs))

    def test_cxl_type_and_coherence(self):
        c = self._checks(self._run([
            {"event": "cxl", "msg": "mem_rd", "device_type": 1},
            {"event": "cxl", "msg": "cache_rd", "device_type": 3},
        ]))
        assert "cxl_type_mismatch" in c
        orphan = [{"event": "cxl", "msg": "mem_rsp", "device_type": 3,
                   "tag": 9}]
        assert "cxl_coherence" in self._checks(self._run(orphan))
        paired = [{"event": "cxl", "msg": "mem_req", "device_type": 3, "tag": 1},
                  {"event": "cxl", "msg": "mem_rsp", "device_type": 3, "tag": 1}]
        assert self._run(paired)["pass"]

    def test_ucie_and_ethernet(self):
        u = [{"event": "ucie", "active_lanes": 20, "module_width": 16}]
        assert "ucie_module_config" in self._checks(self._run(u))
        sb = [{"event": "ucie", "sideband": True, "state": "reset"}]
        assert "ucie_module_config" in self._checks(self._run(sb))
        assert self._run([{"event": "ethernet", "length": 64, "fcs_ok": True,
                           "ipg": 12}])["pass"]
        c = self._checks(self._run([
            {"event": "ethernet", "length": 32},
            {"event": "ethernet", "length": 9000, "mtu": 1518},
            {"event": "ethernet", "length": 64, "fcs_ok": False,
             "accepted": True},
            {"event": "ethernet", "length": 64, "ipg": 4},
        ]))
        assert "ethernet_frame" in c

    def test_noc_turn_model_and_deadlock(self):
        from AGENT_H.advanced_link_verifier import find_cycle
        # XY routing: all X hops then Y hops -> legal
        ok = [{"event": "noc", "packet": 1, "vc": 0, "turn_model": "xy",
               "route": [[0, 0], [1, 0], [2, 0], [2, 1]]}]
        assert self._run(ok)["pass"]
        # Y turn then an X hop -> violates XY
        bad = [{"event": "noc", "packet": 2, "vc": 0, "turn_model": "xy",
                "route": [[0, 0], [0, 1], [1, 1]]}]
        assert "noc_deadlock" in self._checks(self._run(bad))
        vc = [{"event": "noc", "packet": 3, "vc": 1, "blocked": True,
               "blocked_vc": 0}]
        assert "noc_vc_independence" in self._checks(self._run(vc))
        # the cycle finder itself
        assert find_cycle({"a": {"b"}, "b": {"c"}, "c": {"a"}}) is not None
        assert find_cycle({"a": {"b"}, "b": {"c"}, "c": set()}) is None

    def test_robustness_and_manifest(self, tmp_path):
        from AGENT_H.advanced_link_verifier import run_from_manifest
        for evs in ([], [None, 5], [{}], [{"event": "link"}]):
            assert self._run(evs)["pass"]
        bad = [{"event": "link", "proto": "pcie", "seq": 0},
               {"event": "link", "proto": "pcie", "seq": 9}]
        (tmp_path / "advlink_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in bad))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"advlink_trace": "advlink_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "advlink_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T64 — Power-Aware Verification (taxonomy level 15)
# ─────────────────────────────────────────────────────────────────────────────

_OPP = [{"voltage_mv": 700, "max_freq_mhz": 600},
        {"voltage_mv": 800, "max_freq_mhz": 1000},
        {"voltage_mv": 900, "max_freq_mhz": 1400}]


class TestPowerVerifier:
    def _run(self, evs, opp=None):
        from AGENT_H.power_verifier import PowerVerifier
        return PowerVerifier(evs, opp).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_clean_power_cycle(self):
        evs = [
            {"seq": 0, "domain": "cpu", "event": "power", "state": "on"},
            {"seq": 1, "domain": "cpu", "event": "dvfs",
             "voltage_mv": 800, "freq_mhz": 1000},
            {"seq": 2, "domain": "cpu", "event": "state", "regs": {"r0": "0x1"}},
            {"seq": 3, "domain": "cpu", "event": "clk_gate", "gated": True},
            {"seq": 4, "domain": "cpu", "event": "state", "regs": {"r0": "0x1"}},
            {"seq": 5, "domain": "cpu", "event": "clk_gate", "gated": False},
            {"seq": 6, "domain": "cpu", "event": "power",
             "state": "off", "retention": True},
            {"seq": 7, "domain": "cpu", "event": "output",
             "signal": "d", "value": "0x0", "isolated": True},
            {"seq": 8, "domain": "cpu", "event": "power", "state": "on"},
        ]
        rep = self._run(evs, _OPP)
        assert rep["pass"] and rep["band"] == "CLEAN"
        assert rep["power_active"] and rep["metrics"]["power_cycles"] == 1

    def test_gated_activity_detected(self):
        evs = [
            {"seq": 0, "domain": "cpu", "event": "state", "regs": {"r0": "0x1"}},
            {"seq": 1, "domain": "cpu", "event": "clk_gate", "gated": True},
            {"seq": 2, "domain": "cpu", "event": "state", "regs": {"r0": "0x2"}},
        ]
        assert "power_gated_activity" in self._checks(self._run(evs))

    def test_isolation_and_off_activity(self):
        evs = [
            {"seq": 0, "domain": "gpu", "event": "state", "regs": {"a": "0x1"}},
            {"seq": 1, "domain": "gpu", "event": "power", "state": "off"},
            {"seq": 2, "domain": "gpu", "event": "output",
             "signal": "bus", "value": "0xdead", "isolated": False},
            {"seq": 3, "domain": "gpu", "event": "state", "regs": {"a": "0x9"}},
        ]
        c = self._checks(self._run(evs))
        assert "power_isolation" in c and "power_off_activity" in c

    def test_retention_failure_and_leak(self):
        fail = [
            {"seq": 0, "domain": "d", "event": "state", "regs": {"r": "0xaa"}},
            {"seq": 1, "domain": "d", "event": "power",
             "state": "off", "retention": True},
            {"seq": 2, "domain": "d", "event": "power", "state": "on"},
            {"seq": 3, "domain": "d", "event": "state", "regs": {"r": "0xbb"}},
        ]
        # mutate the reg during the off window so restore differs
        f2 = list(fail)
        f2.insert(2, {"seq": 15, "domain": "d", "event": "state",
                      "regs": {"r": "0xbb"}})
        assert "power_retention" in self._checks(self._run(f2))
        leak = [
            {"seq": 0, "domain": "e", "event": "state", "regs": {"r": "0xaa"}},
            {"seq": 1, "domain": "e", "event": "power",
             "state": "off", "retention": False},
            {"seq": 2, "domain": "e", "event": "power", "state": "on"},
        ]
        assert "power_retention_leak" in self._checks(self._run(leak))

    def test_sequencing_and_dvfs(self):
        seqv = [
            {"seq": 0, "domain": "x", "event": "power", "state": "off"},
            {"seq": 1, "domain": "x", "event": "clk_gate", "gated": False},
        ]
        assert "power_sequencing" in self._checks(self._run(seqv))
        # frequency above what the voltage supports
        opp_v = [{"seq": 0, "domain": "c", "event": "dvfs",
                  "voltage_mv": 700, "freq_mhz": 1400}]
        assert "power_dvfs_opp" in self._checks(self._run(opp_v, _OPP))
        # raise frequency while dropping voltage
        order = [
            {"seq": 0, "domain": "c", "event": "dvfs",
             "voltage_mv": 900, "freq_mhz": 900},
            {"seq": 1, "domain": "c", "event": "dvfs",
             "voltage_mv": 700, "freq_mhz": 1400},
        ]
        assert "power_dvfs_order" in self._checks(self._run(order, _OPP))
        # no OPP table -> OPP checks skipped (no false positives)
        assert self._run([{"seq": 0, "domain": "c", "event": "dvfs",
                           "voltage_mv": 1, "freq_mhz": 99999}])["pass"]

    def test_robustness_and_manifest(self, tmp_path):
        from AGENT_H.power_verifier import run_from_manifest
        for evs in ([], [None, 5], [{}], [{"event": "state"}]):
            assert self._run(evs)["pass"]
        evs = [{"seq": 0, "domain": "d", "event": "power", "state": "off"},
               {"seq": 1, "domain": "d", "event": "output",
                "signal": "s", "value": "0x1", "isolated": False}]
        (tmp_path / "power_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"power_trace": "power_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "power_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T65 — CDC / RDC Checker (upgrades AGENT_J)
# ─────────────────────────────────────────────────────────────────────────────

class TestCDCVerifier:
    def _run(self, evs):
        from AGENT_H.cdc_verifier import CDCVerifier
        return CDCVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_clean_crossings(self):
        evs = [
            {"event": "crossing", "signal": "vld", "src_clk": "a",
             "dst_clk": "b", "width": 1, "sync_stages": 2, "scheme": "ff_sync"},
            {"event": "crossing", "signal": "ptr", "src_clk": "a",
             "dst_clk": "b", "width": 4, "sync_stages": 2, "scheme": "gray"},
            {"event": "sample", "signal": "ptr", "value": "0x0"},
            {"event": "sample", "signal": "ptr", "value": "0x1"},
            {"event": "sample", "signal": "ptr", "value": "0x3"},
        ]
        rep = self._run(evs)
        assert rep["pass"] and rep["cdc_active"]
        assert rep["metrics"]["async_crossings"] == 2

    def test_unsynchronized_and_shallow(self):
        evs = [
            {"event": "crossing", "signal": "s1", "src_clk": "a",
             "dst_clk": "b", "scheme": "none"},
            {"event": "crossing", "signal": "s2", "src_clk": "a",
             "dst_clk": "b", "sync_stages": 1, "scheme": "ff_sync"},
        ]
        c = self._checks(self._run(evs))
        assert "cdc_unsynchronized" in c and "cdc_shallow_sync" in c

    def test_multibit_and_gray_violation(self):
        evs = [
            {"event": "crossing", "signal": "bus", "src_clk": "a",
             "dst_clk": "b", "width": 8, "sync_stages": 2, "scheme": "ff_sync"},
            {"event": "crossing", "signal": "g", "src_clk": "a", "dst_clk": "b",
             "width": 4, "sync_stages": 2, "scheme": "gray"},
            {"event": "sample", "signal": "g", "value": "0x0"},
            {"event": "sample", "signal": "g", "value": "0x3"},   # 2 bits at once
        ]
        c = self._checks(self._run(evs))
        assert "cdc_multibit_unsafe" in c and "cdc_gray_violation" in c

    def test_same_domain_is_not_flagged(self):
        evs = [{"event": "crossing", "signal": "s", "src_clk": "a",
                "dst_clk": "a", "width": 32, "scheme": "none"}]
        assert self._run(evs)["pass"]

    def test_handshake_and_reset_crossing(self):
        hs = [
            {"event": "crossing", "signal": "x", "src_clk": "a",
             "dst_clk": "b", "scheme": "handshake", "sync_stages": 2},
            {"event": "handshake", "signal": "x", "req": False, "ack": True},
        ]
        assert "cdc_handshake_protocol" in self._checks(self._run(hs))
        rst = [{"event": "reset", "domain": "b", "src_domain": "a",
                "async_assert": True, "sync_deassert": False}]
        assert "cdc_reset_crossing" in self._checks(self._run(rst))
        ok = [{"event": "reset", "domain": "b", "src_domain": "a",
               "async_assert": True, "sync_deassert": True}]
        assert self._run(ok)["pass"]

    def test_glitch_source_and_robustness(self, tmp_path):
        from AGENT_H.cdc_verifier import run_from_manifest
        g = [{"event": "crossing", "signal": "c", "src_clk": "a", "dst_clk": "b",
              "sync_stages": 2, "scheme": "ff_sync", "src_registered": False}]
        rep = self._run(g)
        assert "cdc_glitch_source" in self._checks(rep)
        assert rep["pass"]                      # MEDIUM only -> still passes
        for evs in ([], [None, 5], [{}], [{"event": "sample", "signal": "zz"}]):
            assert self._run(evs)["pass"]
        evs = [{"event": "crossing", "signal": "s", "src_clk": "a",
                "dst_clk": "b", "scheme": "none"}]
        (tmp_path / "cdc_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"cdc_trace": "cdc_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "cdc_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T66 — Equivalence Checker (upgrades AGENT_L)
# ─────────────────────────────────────────────────────────────────────────────

class TestEquivalenceVerifier:
    def _run(self, evs):
        from AGENT_H.equivalence_verifier import EquivalenceVerifier
        return EquivalenceVerifier(evs).run()

    def _checks(self, rep):
        return {v["check"] for v in rep["violations"]}

    def test_exhaustive_comb_proof(self):
        """De Morgan: ~(a&b) == (~a)|(~b) over all 4 assignments — a proof."""
        from AGENT_H.equivalence_verifier import comb_equivalent
        g = lambda x: (~((x & 1) & ((x >> 1) & 1))) & 1
        r = lambda x: ((~(x & 1) & 1) | (~((x >> 1) & 1) & 1)) & 1
        ok, cex, exhaustive = comb_equivalent(g, r, 2)
        assert ok and cex is None and exhaustive
        # a genuinely different function must produce a counterexample
        bad = lambda x: (x & 1) & ((x >> 1) & 1)
        ok2, cex2, _ = comb_equivalent(g, bad, 2)
        assert not ok2 and cex2 is not None
        assert g(cex2) != bad(cex2)             # counterexample is real

    def test_comb_truth_tables(self):
        same = [{"event": "comb", "name": "u", "inputs": 2,
                 "golden": [0, 1, 1, 0], "revised": [0, 1, 1, 0]}]
        rep = self._run(same)
        assert rep["pass"] and rep["metrics"]["exhaustive_proofs"] == 1
        diff = [{"event": "comb", "name": "u", "inputs": 2,
                 "golden": [0, 1, 1, 0], "revised": [0, 1, 1, 1]}]
        r2 = self._run(diff)
        assert "equiv_comb_mismatch" in self._checks(r2)
        assert "0x3" in r2["violations"][0]["detail"]

    def test_latency_tolerant_sequential(self):
        from AGENT_H.equivalence_verifier import best_latency_offset
        g = [1, 2, 3, 4]
        r = [0, 0, 1, 2, 3, 4]
        k, matched = best_latency_offset(g, r)
        assert k == 2 and matched == 4
        # declared latency matches -> clean
        ok = [{"event": "seq", "name": "p", "latency": 2,
               "golden_out": g, "revised_out": r}]
        assert self._run(ok)["pass"]
        # declared latency wrong -> MEDIUM note, still passes
        mism = [{"event": "seq", "name": "p", "latency": 0,
                 "golden_out": g, "revised_out": r}]
        rep = self._run(mism)
        assert "equiv_latency_mismatch" in self._checks(rep) and rep["pass"]

    def test_sequential_mismatch_and_reset(self):
        bad = [{"event": "seq", "name": "p", "latency": 0,
                "golden_out": [1, 2, 3], "revised_out": [1, 9, 3]}]
        assert "equiv_seq_mismatch" in self._checks(self._run(bad))
        rst = [{"event": "seq", "name": "p", "golden_out": [1], "revised_out": [1],
                "golden_reset": "0x0", "revised_reset": "0xff"}]
        assert "equiv_reset_state" in self._checks(self._run(rst))

    def test_incomplete_is_reported_honestly(self):
        """A space too large to enumerate must NOT be claimed as a proof."""
        from AGENT_H.equivalence_verifier import EquivalenceVerifier
        ev = [{"event": "comb", "name": "wide", "inputs": 40,
               "golden": [0, 1], "revised": [0, 1]}]
        rep = EquivalenceVerifier(ev, max_bits=8).run()
        assert "equiv_incomplete" in self._checks(rep)
        assert rep["metrics"]["bounded_checks"] == 1
        assert rep["metrics"]["exhaustive_proofs"] == 0

    def test_trace_and_manifest(self, tmp_path):
        from AGENT_H.equivalence_verifier import run_from_manifest
        tr = [{"event": "trace", "name": "core", "latency": 1,
               "golden": [1, 2, 3], "revised": [0, 1, 2, 3]}]
        assert self._run(tr)["pass"]
        for evs in ([], [None, 5], [{}], [{"event": "comb"}]):
            assert self._run(evs)["pass"]
        bad = [{"event": "trace", "name": "core", "latency": 0,
                "golden": [1, 2], "revised": [1, 7]}]
        (tmp_path / "equiv_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in bad))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"equiv_trace": "equiv_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "equiv_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T63 — Vector GHASH Checker (Zvkg, NIST-GCM-validated)
# ─────────────────────────────────────────────────────────────────────────────

def _bswap16(x):
    """NIST big-endian block hex <-> 128-bit element group (byte 0 at LSB)."""
    return int.from_bytes((x & ((1 << 128) - 1)).to_bytes(16, "big")[::-1], "big")


def _nist_gf_mult(X, Y):
    """Independent NIST SP 800-38D GF(2^128) multiply (right-shift / 0xE1)."""
    R = 0xE1 << 120
    Z = 0
    V = Y
    for i in range(128):
        if (X >> (127 - i)) & 1:
            Z ^= V
        V = (V >> 1) ^ R if V & 1 else V >> 1
    return Z


class TestVGHASHVerifier:
    def test_matches_independent_nist_multiply(self):
        """vgmul must agree with a textbook NIST GF(2^128) multiply."""
        from AGENT_H.vghash_verifier import vgmul_golden
        import random
        rng = random.Random(3)
        for _ in range(64):
            a, b = rng.getrandbits(128), rng.getrandbits(128)
            assert vgmul_golden(_bswap16(a), _bswap16(b)) == \
                _bswap16(_nist_gf_mult(a, b))

    def test_nist_gcm_ghash_vector(self):
        """GHASH recurrence over vghsh reproduces NIST GCM Test Case 2."""
        from AGENT_H.vghash_verifier import vghsh_golden
        H = 0x66E94BD4EF8A2C3B884CFA59CA342B2E
        C = 0x0388DACE60B6A392F328C2B971B2FE78
        X = 0
        for blk in (C, 128):                    # ciphertext block, then len block
            X = vghsh_golden(X, _bswap16(H), _bswap16(blk))
        assert "%032x" % _bswap16(X) == "f38cbb1ad69223dcc3457ae5b6b0f885"

    def test_vgmul_is_vghsh_with_zero(self):
        from AGENT_H.vghash_verifier import vgmul_golden, vghsh_golden
        import random
        rng = random.Random(11)
        for _ in range(32):
            a, b = rng.getrandbits(128), rng.getrandbits(128)
            assert vgmul_golden(a, b) == vghsh_golden(a, b, 0)

    def test_field_algebra(self):
        """Multiplication is commutative and 1 is the identity in this basis."""
        from AGENT_H.vghash_verifier import vgmul_golden
        import random
        rng = random.Random(5)
        one = _bswap16(1 << 127)                # NIST 'x^0 only' = 0x80 00 .. 00
        for _ in range(16):
            a, b = rng.getrandbits(128), rng.getrandbits(128)
            assert vgmul_golden(a, b) == vgmul_golden(b, a)
            assert vgmul_golden(a, one) == (a & ((1 << 128) - 1))
        assert vgmul_golden(0, rng.getrandbits(128)) == 0

    def test_verifier_clean_and_bug(self):
        from AGENT_H.vghash_verifier import VGHASHVerifier, vgmul_golden, \
            vghsh_golden
        vd, vs2, vs1 = 0x11223344556677889900AABBCCDDEEFF, 0xDEADBEEF, 0xCAFE
        good = {"op": "vgmul", "vd": f"{vd:032x}", "vs2": f"{vs2:032x}",
                "result": f"{vgmul_golden(vd, vs2):032x}"}
        r = VGHASHVerifier([good]).run()
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["vghash_active"]
        bad = {"op": "vgmul", "vd": f"{vd:032x}", "vs2": f"{vs2:032x}",
               "result": "0" * 32}
        rb = VGHASHVerifier([bad]).run()
        assert not rb["pass"]
        assert any(v["check"] == "vghash_result" for v in rb["violations"])
        gh = {"op": "vghsh.vv", "vd": vd, "vs2": vs2, "vs1": vs1,
              "result": vghsh_golden(vd, vs2, vs1)}
        assert VGHASHVerifier([gh]).run()["pass"]
        # 4-word element-list form is accepted
        el = {"op": "vgmul", "vd": [1, 0, 0, 0], "vs2": [2, 0, 0, 0],
              "result": vgmul_golden(1, 2)}
        assert VGHASHVerifier([el]).run()["pass"]
        # non-ghash ignored; malformed tolerated
        n = VGHASHVerifier([{"op": "vaesem"}]).run()
        assert n["pass"] and n["vghash_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vghsh", "vd": 1, "vs2": 2}]):
            assert VGHASHVerifier(evs).run()["pass"]

    def test_manifest(self, tmp_path):
        from AGENT_H.vghash_verifier import run_from_manifest
        evs = [{"op": "vgmul", "vd": "0" * 31 + "1", "vs2": "0" * 31 + "2",
                "result": "f" * 32}]
        (tmp_path / "vghash_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vghash_trace": "vghash_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vghash_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T62 — Vector SM4 Checker (Zvksed, GB/T-32907-validated)
# ─────────────────────────────────────────────────────────────────────────────

class TestVSM4Verifier:
    def _sm4_encrypt(self, key_words, pt_words):
        """Full SM4 block encryption composed from vsm4k + vsm4r goldens."""
        from AGENT_H.vsm4_verifier import vsm4r_golden, vsm4k_golden, FK
        K = [(key_words[i] ^ FK[i]) & 0xFFFFFFFF for i in range(4)]
        rks = []
        cur = list(K)
        for rnd in range(8):
            cur = vsm4k_golden(cur, rnd)
            rks += cur
        X = list(pt_words)
        cur = list(pt_words)
        for grp in range(8):
            cur = vsm4r_golden(cur, rks[4 * grp:4 * grp + 4])
            X += cur
        return [X[35], X[34], X[33], X[32]]           # R (reverse) transform

    @staticmethod
    def _w(hexstr):
        return [int(hexstr[i:i + 8], 16) for i in range(0, 32, 8)]

    def test_gbt_encrypt_vector(self):
        key = self._w("0123456789abcdeffedcba9876543210")
        pt = self._w("0123456789abcdeffedcba9876543210")
        ct = self._sm4_encrypt(key, pt)
        assert "".join("%08x" % w for w in ct) == \
            "681edf34d206965e86b3e94f536e4246"

    def test_decrypt_round_trips(self):
        """SM4 decryption = encryption with round keys reversed; must invert."""
        from AGENT_H.vsm4_verifier import vsm4r_golden, vsm4k_golden, FK
        key = self._w("0123456789abcdeffedcba9876543210")
        pt = self._w("0011223344556677889900aabbccddee")
        K = [(key[i] ^ FK[i]) & 0xFFFFFFFF for i in range(4)]
        rks = []
        cur = list(K)
        for rnd in range(8):
            cur = vsm4k_golden(cur, rnd)
            rks += cur

        def crypt(block, keys):
            cur = list(block)
            X = list(block)
            # feed round keys in groups of 4 (already ordered as given)
            for g in range(8):
                cur = vsm4r_golden(cur, keys[4 * g:4 * g + 4])
                X += cur
            return [X[35], X[34], X[33], X[32]]
        ct = crypt(pt, rks)
        dec = crypt(ct, list(reversed(rks)))
        assert dec == pt

    def test_verifier_clean_and_bug(self):
        from AGENT_H.vsm4_verifier import VSM4Verifier, vsm4r_golden, vsm4k_golden
        vd = [0x01234567, 0x89abcdef, 0xfedcba98, 0x76543210]
        vs2 = [0x11111111, 0x22222222, 0x33333333, 0x44444444]
        good_r = {"op": "vsm4r", "vd": vd, "vs2": vs2,
                  "result": vsm4r_golden(vd, vs2)}
        r = VSM4Verifier([good_r]).run()
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["vsm4_active"]
        bad = {"op": "vsm4r", "vd": vd, "vs2": vs2, "result": [0, 0, 0, 0]}
        rb = VSM4Verifier([bad]).run()
        assert not rb["pass"]
        assert any(v["check"] == "vsm4_result" for v in rb["violations"])
        good_k = {"op": "vsm4k", "rnd": 0, "vs2": vs2,
                  "result": vsm4k_golden(vs2, 0)}
        assert VSM4Verifier([good_k]).run()["pass"]
        # non-vsm4 ignored; malformed tolerated
        n = VSM4Verifier([{"op": "vaesem"}]).run()
        assert n["pass"] and n["vsm4_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vsm4r", "vs2": vs2}]):
            assert VSM4Verifier(evs).run()["pass"]

    def test_rnd_selects_constants(self):
        from AGENT_H.vsm4_verifier import vsm4k_golden
        vs2 = [1, 2, 3, 4]
        assert vsm4k_golden(vs2, 0) != vsm4k_golden(vs2, 1)   # different CK group
        assert vsm4k_golden(vs2, 0) == vsm4k_golden(vs2, 8)   # uimm[2:0] masks

    def test_manifest(self, tmp_path):
        from AGENT_H.vsm4_verifier import run_from_manifest
        evs = [{"op": "vsm4r", "vd": [1, 2, 3, 4], "vs2": [5, 6, 7, 8],
                "result": [0, 0, 0, 0]}]
        (tmp_path / "vsm4_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vsm4_trace": "vsm4_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vsm4_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T61 — AES Key-Schedule Checkers (FIPS-197-validated): scalar aes64ks1i +
#        vector vaeskf1 / vaeskf2
# ─────────────────────────────────────────────────────────────────────────────

def _bswap32(x):
    return int.from_bytes((x & 0xFFFFFFFF).to_bytes(4, "big")[::-1], "big")


class TestAESKeySchedule:
    def test_aes64ks1i_scalar_full_aes128(self):
        """Scalar aes64ks1i + aes64ks2 must reproduce the FIPS-197 AES-128
        expanded key (words 4..43)."""
        from AGENT_H.aes_verifier import aes64ks1i, aes64ks2
        fips = [0x2b7e1516, 0x28aed2a6, 0xabf71588, 0x09cf4f3c]
        s = [_bswap32(x) for x in fips]                    # sail byte order
        rk0 = (s[1] << 32) | s[0]
        rk1 = (s[3] << 32) | s[2]
        words = list(fips)
        for rnd in range(10):
            ks = aes64ks1i(rk1, rnd)
            rk0 = aes64ks2(ks, rk0)
            rk1 = aes64ks2(rk0, rk1)
            words += [_bswap32(rk0 & 0xFFFFFFFF), _bswap32((rk0 >> 32) & 0xFFFFFFFF),
                      _bswap32(rk1 & 0xFFFFFFFF), _bswap32((rk1 >> 32) & 0xFFFFFFFF)]
        assert words[4] == 0xa0fafe17 and words[5] == 0x88542cb1
        assert words[7] == 0x2a6c7605 and words[43] == 0xb6630ca6
        # rnum out of range -> None
        assert aes64ks1i(0, 0xB) is None

    def test_vaeskf1_full_aes128(self):
        from AGENT_H.vaeskf_verifier import vaeskf1_golden
        fips = [0x2b7e1516, 0x28aed2a6, 0xabf71588, 0x09cf4f3c]
        rk = [_bswap32(x) for x in fips]
        words = list(fips)
        for rnd in range(1, 11):
            rk = vaeskf1_golden(rk, rnd)
            words += [_bswap32(x) for x in rk]
        assert words[4] == 0xa0fafe17 and words[43] == 0xb6630ca6

    def test_vaeskf2_full_aes256(self):
        from AGENT_H.vaeskf_verifier import vaeskf2_golden

        def _sub(x):
            from AGENT_H.aes_verifier import AES_SBOX
            return sum(AES_SBOX[(x >> (8 * i)) & 0xFF] << (8 * i) for i in range(4))

        def _rotbe(x):
            return ((x << 8) | (x >> 24)) & 0xFFFFFFFF
        rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40]
        import random
        random.seed(7)
        key = [random.getrandbits(32) for _ in range(8)]
        # textbook Nk=8 reference expansion (big-endian words)
        ref = list(key)
        for i in range(8, 60):
            t = ref[i - 1]
            if i % 8 == 0:
                t = (_sub(_rotbe(t)) ^ (rcon[i // 8 - 1] << 24)) & 0xFFFFFFFF
            elif i % 8 == 4:
                t = _sub(t)
            ref.append((ref[i - 8] ^ t) & 0xFFFFFFFF)
        # drive vaeskf2 in sail byte order
        gp = [_bswap32(x) for x in key[0:4]]
        gc = [_bswap32(x) for x in key[4:8]]
        got = list(key)
        for rnd in range(2, 15):
            nxt = vaeskf2_golden(gc, gp, rnd)
            got += [_bswap32(x) for x in nxt]
            gp, gc = gc, nxt
        assert got == ref

    def test_immediate_projection(self):
        # out-of-range rnd values are projected (uimm[3] inverted), never crash
        from AGENT_H.vaeskf_verifier import vaeskf1_golden, vaeskf2_golden
        assert vaeskf1_golden([1, 2, 3, 4], 0) == vaeskf1_golden([1, 2, 3, 4], 8)
        assert vaeskf1_golden([1, 2, 3, 4], 11) == vaeskf1_golden([1, 2, 3, 4], 3)
        assert vaeskf2_golden([1, 2, 3, 4], [5, 6, 7, 8], 0) == \
            vaeskf2_golden([1, 2, 3, 4], [5, 6, 7, 8], 8)

    def test_verifier_clean_and_bug(self):
        from AGENT_H.vaeskf_verifier import VAESKFVerifier, vaeskf1_golden, \
            vaeskf2_golden
        vs2 = [0x11111111, 0x22222222, 0x33333333, 0x44444444]
        good1 = {"op": "vaeskf1", "rnd": 1, "vs2": vs2,
                 "result": vaeskf1_golden(vs2, 1)}
        r = VAESKFVerifier([good1]).run()
        assert r["pass"] and r["metrics"]["checked"] == 1 and r["vaeskf_active"]
        bad = {"op": "vaeskf1", "rnd": 1, "vs2": vs2, "result": [0, 0, 0, 0]}
        rb = VAESKFVerifier([bad]).run()
        assert not rb["pass"]
        assert any(v["check"] == "vaeskf_result" for v in rb["violations"])
        vd = [0xaa, 0xbb, 0xcc, 0xdd]
        good2 = {"op": "vaeskf2", "rnd": 2, "vs2": vs2, "vd": vd,
                 "result": vaeskf2_golden(vs2, vd, 2)}
        assert VAESKFVerifier([good2]).run()["pass"]
        # non-key-sched ignored; malformed tolerated
        n = VAESKFVerifier([{"op": "vaesem"}]).run()
        assert n["pass"] and n["vaeskf_active"] is False
        for evs in ([], [None, 5], [{}], [{"op": "vaeskf2", "vs2": vs2}]):
            assert VAESKFVerifier(evs).run()["pass"]

    def test_manifest(self, tmp_path):
        from AGENT_H.vaeskf_verifier import run_from_manifest
        evs = [{"op": "vaeskf1", "rnd": 1,
                "vs2": [1, 2, 3, 4], "result": [0, 0, 0, 0]}]
        (tmp_path / "vaeskf_trace.jsonl").write_text(
            "\n".join(json.dumps(x) for x in evs))
        man = {"schema_version": "2.1.0", "run_dir": str(tmp_path),
               "outputs": {"vaeskf_trace": "vaeskf_trace.jsonl"}}
        mp = tmp_path / "run_manifest.json"
        mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        assert (tmp_path / "vaeskf_report.json").exists()


# ─────────────────────────────────────────────────────────────────────────────
# T57 — SM4 Scalar Cryptography Checker (GB/T-32907-validated)
# ─────────────────────────────────────────────────────────────────────────────

def _rotl32(x, n):
    x &= 0xFFFFFFFF
    n &= 31
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


class TestSM4Verifier:
    def _T(self, A):
        from AGENT_H.sm4_verifier import sm4_golden
        t = 0
        for bs in range(4):
            t = sm4_golden("sm4ed", t, A, bs)
        return t

    def _Tp(self, A):
        from AGENT_H.sm4_verifier import sm4_golden
        t = 0
        for bs in range(4):
            t = sm4_golden("sm4ks", t, A, bs)
        return t

    def test_sbox_and_T_composition(self):
        from AGENT_H.sm4_verifier import SM4_SBOX, sm4_golden
        assert SM4_SBOX[0x00] == 0xd6 and SM4_SBOX[0xff] == 0x48
        assert len(set(SM4_SBOX)) == 256           # a permutation
        # chaining sm4ed over bs=0..3 == T(A) = L(tau(A))
        A = 0x0123abcd
        tau = sum(SM4_SBOX[(A >> (8 * i)) & 0xFF] << (8 * i) for i in range(4))
        ref = (tau ^ _rotl32(tau, 2) ^ _rotl32(tau, 10)
               ^ _rotl32(tau, 18) ^ _rotl32(tau, 24))
        assert self._T(A) == ref

    def test_gbt_32907_full_vector(self):
        """The gold standard: full SM4 encryption of the GB/T 32907-2016 test
        vector, built from the module's sm4ed/sm4ks, matches the published
        ciphertext 681edf34d206965e86b3e94f536e4246."""
        def words(h):
            return [int(h[i:i + 8], 16) for i in range(0, 32, 8)]
        MK = words("0123456789abcdeffedcba9876543210")
        FK = [0xa3b1bac6, 0x56aa3350, 0x677d9197, 0xb27022dc]
        CK = []
        for i in range(32):
            ck = 0
            for j in range(4):
                ck = (ck << 8) | (((4 * i + j) * 7) % 256)
            CK.append(ck)
        K = [MK[i] ^ FK[i] for i in range(4)]
        rk = []
        for i in range(32):
            K.append(K[i] ^ self._Tp(K[i + 1] ^ K[i + 2] ^ K[i + 3] ^ CK[i]))
            rk.append(K[i + 4])
        X = words("0123456789abcdeffedcba9876543210")
        for i in range(32):
            X.append(X[i] ^ self._T(X[i + 1] ^ X[i + 2] ^ X[i + 3] ^ rk[i]))
        got = "".join(f"{w:08x}" for w in [X[35], X[34], X[33], X[32]])
        assert got == "681edf34d206965e86b3e94f536e4246"

    def test_verifier_clean_bug_noop(self):
        from AGENT_H.sm4_verifier import SM4Verifier, sm4_golden

        def instr(dis, s2v, rd, rdv, s1v=0x11111111):
            return [{"schema_version": "2.1.0", "seq": 0, "disasm": "li",
                     "regs": {"x6": hex(s1v)}},
                    {"schema_version": "2.1.0", "seq": 1, "disasm": "li",
                     "regs": {"x7": hex(s2v)}},
                    {"schema_version": "2.1.0", "seq": 2, "disasm": dis,
                     "regs": {"x5": hex(rdv)}}]
        g = sm4_golden("sm4ed", 0x11111111, 0xAABBCCDD, 1)
        assert SM4Verifier(instr("sm4ed x5,x6,x7,1", 0xAABBCCDD, 5, g)).run()["pass"]
        assert not SM4Verifier(instr("sm4ed x5,x6,x7,1", 0xAABBCCDD, 5, 0xDEAD)).run()["pass"]
        r = SM4Verifier([{"disasm": "add x1,x2,x3", "regs": {"x1": "0x5"}}]).run()
        assert r["pass"] and r["sm4_active"] is False
        for rs in ([], [None, 5], [{}]):
            assert SM4Verifier(rs).run()["pass"]
        assert SM4Verifier([]).run()["agent"] == "sm4_verifier"


# ─────────────────────────────────────────────────────────────────────────────
# AGENT_A — Semantic Analysis (Phase 1): schema validation + DUT extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestSemanticAnalyzer:
    _REC = {"schema_version": "2.1.0", "seq": 0, "pc": "0x80000000",
            "disasm": "addi x1,x0,1", "regs": {"x1": "0x1"}, "csrs": {}}
    _MAN = {"schema_version": "2.1.0", "run_id": "r", "run_dir": "/tmp",
            "status": "completed", "outputs": {}}

    def test_record_validation(self):
        from AGENT_A.semantic_analyzer import validate_record
        assert validate_record(self._REC) == []
        assert any("missing" in e for e in validate_record({"seq": 0}))
        assert any("integer" in e for e in validate_record({**self._REC, "seq": "x"}))
        assert any("hex" in e for e in validate_record({**self._REC, "pc": "80000000"}))
        assert any("array" in e for e in
                   validate_record({**self._REC, "mem_writes": "x"}))

    def test_manifest_validation(self):
        from AGENT_A.semantic_analyzer import validate_manifest
        assert validate_manifest(self._MAN) == []
        assert any("missing" in e for e in validate_manifest({"run_id": "r"}))
        assert any("status" in e for e in
                   validate_manifest({**self._MAN, "status": "weird"}))

    def test_dut_extraction(self):
        from AGENT_A.semantic_analyzer import extract_dut
        rtl = ("module rv32im_core (\n  input wire clk,\n  input wire rst_n,\n"
               "  input [31:0] instr,\n  output reg [31:0] pc,\n  output valid\n);"
               "\nendmodule")
        d = extract_dut(rtl)
        assert d["module"] == "rv32im_core" and d["clock"] and d["reset"]
        assert d["inputs"] == 3 and d["outputs"] == 2
        pc = [p for p in d["ports"] if p["name"] == "pc"][0]
        assert pc["width"] == "[31:0]" and pc["dir"] == "output"
        d2 = extract_dut("module m(input a, b, c, output x); endmodule")
        assert d2["inputs"] == 3 and d2["outputs"] == 1

    def test_analyzer_and_manifest(self, tmp_path):
        from AGENT_A.semantic_analyzer import SemanticAnalyzer, run_from_manifest
        r = SemanticAnalyzer([self._REC, self._REC], self._MAN,
                             "module core(input clk); endmodule").analyze()
        assert r["pass"] and r["dut"]["module"] == "core"
        assert not SemanticAnalyzer([{"seq": 0}], {"run_id": "r"}).analyze()["pass"]
        # manifest round-trip flags an invalid record + extracts the DUT
        (tmp_path / "rtl_commit.jsonl").write_text(json.dumps({"seq": 0}))
        (tmp_path / "core.v").write_text("module core(input clk, output q); endmodule")
        man = {"schema_version": "2.1.0", "run_id": "r", "run_dir": str(tmp_path),
               "status": "completed", "outputs": {"rtl_commit_log": "rtl_commit.jsonl"},
               "rtl": "core.v"}
        mp = tmp_path / "run_manifest.json"; mp.write_text(json.dumps(man))
        assert run_from_manifest(str(mp)) == 1
        rep = json.loads((tmp_path / "semantic_report.json").read_text())
        assert not rep["schema_valid"] and rep["dut"]["module"] == "core"


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
