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
