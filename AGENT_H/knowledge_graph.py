"""
AGENT_H/knowledge_graph.py
===========================
T7 — Verification Knowledge Graph

A SQLite-backed graph that stores every bug found by the AVA pipeline and
enables:

  1. Bug deduplication — new bugs are compared against known bugs by
     (mismatch_class, affected_module, instruction_type) signature.
  2. Similarity clustering — bugs with the same error-code family are
     grouped so a single root cause can be traced across runs.
  3. Automated follow-up campaign proposals — for each new bug the graph
     proposes targeted test campaigns that exercise similar corner cases.

Schema
------
Tables:
  bugs           — one row per unique verified bug
  campaigns      — one row per verification campaign (run_id group)
  bug_campaigns  — many-to-many join
  signals        — signals implicated in each bug
  instruction_types — instruction classes involved in each bug
  similar_bugs   — pre-computed similarity edges

Usage
-----
  from AGENT_H.knowledge_graph import KnowledgeGraph

  kg = KnowledgeGraph("ava_knowledge.db")
  kg.record_bug(bug_report, manifest)
  proposals = kg.propose_campaigns(bug_id)
  similar   = kg.find_similar(bug_id, top_k=5)
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"

# ─────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────

@dataclass
class BugRecord:
    """Represents one verified bug in the knowledge graph."""
    bug_id:          str           # SHA-256 of (mismatch_class, run_id, seq)
    run_id:          str
    mismatch_class:  str           # AVA error code: REG_MISMATCH, PC_MISMATCH, etc.
    first_divergence_seq: int
    affected_module: Optional[str]  # RTL module name (from root-cause analysis)
    instruction_types: List[str]   # e.g. ["MUL","DIV"] from disasm context
    signals:         List[str]     # implicated RTL signals (from root-cause)
    rtl_context:     Optional[List[dict]]  # commit-log context window at divergence
    iss_context:     Optional[List[dict]]
    repro_cmd:       Optional[str]
    fix_applied:     Optional[str]  # human-entered fix description
    campaign_id:     Optional[str]
    recorded_at:     str            # ISO-8601


@dataclass
class CampaignProposal:
    """A proposed follow-up verification campaign."""
    trigger_bug_id:   str
    campaign_type:    str    # "stress", "directed", "litmus", "formal"
    priority:         str    # "P0" / "P1" / "P2"
    description:      str
    suggested_config: Dict[str, Any]   # partial manifest fields


# ─────────────────────────────────────────────────────────
# Bug signature and ID
# ─────────────────────────────────────────────────────────

def _bug_signature(mismatch_class: str, run_id: str, seq: int) -> str:
    raw = f"{mismatch_class}|{run_id}|{seq}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _instr_types_from_context(context: Optional[List[dict]]) -> List[str]:
    """
    Extract likely instruction types from the commit-log context window.
    Uses disasm strings to identify instruction classes.
    """
    if not context:
        return []
    types = set()
    prefixes = {
        "mul": "MUL", "div": "DIV", "rem": "REM",
        "lw": "LOAD", "lh": "LOAD", "lb": "LOAD",
        "sw": "STORE", "sh": "STORE", "sb": "STORE",
        "add": "ALU", "sub": "ALU", "and": "ALU", "or": "ALU", "xor": "ALU",
        "sll": "SHIFT", "srl": "SHIFT", "sra": "SHIFT",
        "beq": "BRANCH", "bne": "BRANCH", "blt": "BRANCH", "bge": "BRANCH",
        "jal": "JUMP", "jalr": "JUMP",
        "csrr": "CSR", "csrw": "CSR", "csrrs": "CSR",
        "ecall": "TRAP", "ebreak": "TRAP", "mret": "TRAP",
        "fence": "FENCE", "lr": "AMO", "sc": "AMO", "amo": "AMO",
    }
    for rec in context:
        disasm = (rec.get("disasm") or "").strip().lower().split()[0]
        for prefix, itype in prefixes.items():
            if disasm.startswith(prefix):
                types.add(itype)
                break
    return sorted(types)


# ─────────────────────────────────────────────────────────
# Knowledge Graph
# ─────────────────────────────────────────────────────────

class KnowledgeGraph:
    """
    SQLite-backed verification knowledge graph.

    Parameters
    ----------
    db_path : path to SQLite database file (created if absent)
    """

    def __init__(self, db_path: str | Path = "ava_knowledge.db") -> None:
        self.db_path = Path(db_path)
        self._conn   = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        logger.info("KnowledgeGraph opened: %s", self.db_path)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "KnowledgeGraph":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ── Schema initialisation ─────────────────────────────────────────────────

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS bugs (
                bug_id           TEXT PRIMARY KEY,
                run_id           TEXT NOT NULL,
                mismatch_class   TEXT NOT NULL,
                first_divergence_seq INTEGER,
                affected_module  TEXT,
                repro_cmd        TEXT,
                fix_applied      TEXT,
                campaign_id      TEXT,
                rtl_context_json TEXT,
                iss_context_json TEXT,
                recorded_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_id   TEXT PRIMARY KEY,
                description   TEXT,
                created_at    TEXT NOT NULL,
                total_runs    INTEGER DEFAULT 0,
                total_bugs    INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS bug_campaigns (
                bug_id      TEXT REFERENCES bugs(bug_id),
                campaign_id TEXT REFERENCES campaigns(campaign_id),
                PRIMARY KEY (bug_id, campaign_id)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                bug_id  TEXT REFERENCES bugs(bug_id),
                signal  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS instruction_types (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                bug_id TEXT REFERENCES bugs(bug_id),
                itype  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS similar_bugs (
                bug_id_a   TEXT REFERENCES bugs(bug_id),
                bug_id_b   TEXT REFERENCES bugs(bug_id),
                similarity REAL NOT NULL,   -- 0.0 to 1.0
                PRIMARY KEY (bug_id_a, bug_id_b)
            );

            CREATE INDEX IF NOT EXISTS idx_bugs_mismatch ON bugs(mismatch_class);
            CREATE INDEX IF NOT EXISTS idx_bugs_campaign ON bugs(campaign_id);
            CREATE INDEX IF NOT EXISTS idx_signals_bug   ON signals(bug_id);
            CREATE INDEX IF NOT EXISTS idx_itypes_bug    ON instruction_types(bug_id);
        """)
        self._conn.commit()

    # ── Record a new bug ──────────────────────────────────────────────────────

    def record_bug(
        self,
        bug_report: Dict[str, Any],
        manifest:   Optional[Dict[str, Any]] = None,
        signals:    Optional[List[str]] = None,
        fix_applied: Optional[str] = None,
    ) -> str:
        """
        Insert a bug from a bug_report.json dict.

        Returns the bug_id (16-char hex).
        """
        run_id         = bug_report.get("run_id", "unknown")
        mismatch_class = bug_report.get("mismatch_class", "UNKNOWN")
        seq            = bug_report.get("first_divergence_seq", 0)
        rtl_ctx        = bug_report.get("rtl_context")
        iss_ctx        = bug_report.get("iss_context")
        repro_cmd      = bug_report.get("repro_cmd")
        campaign_id    = (manifest or {}).get("campaign_id")

        bug_id = _bug_signature(mismatch_class, run_id, seq)
        itypes = _instr_types_from_context(rtl_ctx)
        now    = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        cur = self._conn.cursor()
        cur.execute("""
            INSERT OR IGNORE INTO bugs
              (bug_id, run_id, mismatch_class, first_divergence_seq,
               repro_cmd, fix_applied, campaign_id,
               rtl_context_json, iss_context_json, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bug_id, run_id, mismatch_class, seq,
            repro_cmd, fix_applied, campaign_id,
            json.dumps(rtl_ctx) if rtl_ctx else None,
            json.dumps(iss_ctx) if iss_ctx else None,
            now,
        ))

        # Insert instruction types
        for itype in itypes:
            cur.execute(
                "INSERT OR IGNORE INTO instruction_types (bug_id, itype) VALUES (?, ?)",
                (bug_id, itype),
            )

        # Insert implicated signals
        for sig in (signals or []):
            cur.execute(
                "INSERT INTO signals (bug_id, signal) VALUES (?, ?)",
                (bug_id, sig),
            )

        # Link to campaign
        if campaign_id:
            cur.execute(
                "INSERT OR IGNORE INTO campaigns (campaign_id, description, created_at) VALUES (?, ?, ?)",
                (campaign_id, "", now),
            )
            cur.execute(
                "INSERT OR IGNORE INTO bug_campaigns (bug_id, campaign_id) VALUES (?, ?)",
                (bug_id, campaign_id),
            )
            cur.execute(
                "UPDATE campaigns SET total_bugs = total_bugs + 1 WHERE campaign_id = ?",
                (campaign_id,),
            )

        self._conn.commit()

        # Update similarity index
        self._update_similarity(bug_id, mismatch_class, itypes)
        logger.info("Recorded bug %s: %s (run=%s, seq=%d)", bug_id, mismatch_class, run_id, seq)
        return bug_id

    # ── Similarity computation ────────────────────────────────────────────────

    def _update_similarity(
        self,
        new_id:         str,
        mismatch_class: str,
        itypes:         List[str],
    ) -> None:
        """
        Compute similarity between new_id and all existing bugs.
        Similarity = weighted Jaccard over (mismatch_class, instruction_types).
        """
        cur = self._conn.cursor()
        existing = cur.execute(
            "SELECT bug_id, mismatch_class FROM bugs WHERE bug_id != ?",
            (new_id,)
        ).fetchall()

        for row in existing:
            other_id    = row["bug_id"]
            other_class = row["mismatch_class"]

            # Same mismatch class → base score 0.5
            class_score = 0.5 if mismatch_class == other_class else 0.0

            # Instruction type Jaccard
            other_itypes = set(
                r["itype"] for r in cur.execute(
                    "SELECT itype FROM instruction_types WHERE bug_id = ?", (other_id,)
                ).fetchall()
            )
            mine = set(itypes)
            if mine | other_itypes:
                itype_score = 0.5 * len(mine & other_itypes) / len(mine | other_itypes)
            else:
                itype_score = 0.0

            similarity = round(class_score + itype_score, 4)
            if similarity > 0:
                cur.execute("""
                    INSERT OR REPLACE INTO similar_bugs (bug_id_a, bug_id_b, similarity)
                    VALUES (?, ?, ?)
                """, (new_id, other_id, similarity))
                cur.execute("""
                    INSERT OR REPLACE INTO similar_bugs (bug_id_a, bug_id_b, similarity)
                    VALUES (?, ?, ?)
                """, (other_id, new_id, similarity))

        self._conn.commit()

    # ── Query: find similar bugs ──────────────────────────────────────────────

    def find_similar(
        self,
        bug_id: str,
        top_k:  int = 5,
        min_similarity: float = 0.3,
    ) -> List[Dict[str, Any]]:
        """
        Return the top-k most similar bugs to bug_id.

        Each result: {"bug_id", "similarity", "mismatch_class", "run_id", "recorded_at"}
        """
        cur = self._conn.cursor()
        rows = cur.execute("""
            SELECT sb.bug_id_b AS similar_id, sb.similarity,
                   b.mismatch_class, b.run_id, b.recorded_at
            FROM similar_bugs sb
            JOIN bugs b ON b.bug_id = sb.bug_id_b
            WHERE sb.bug_id_a = ? AND sb.similarity >= ?
            ORDER BY sb.similarity DESC
            LIMIT ?
        """, (bug_id, min_similarity, top_k)).fetchall()

        return [dict(r) for r in rows]

    # ── Query: propose follow-up campaigns ───────────────────────────────────

    def propose_campaigns(self, bug_id: str) -> List[CampaignProposal]:
        """
        Generate follow-up campaign proposals based on the bug's mismatch class
        and instruction types.
        """
        cur = self._conn.cursor()
        bug = cur.execute(
            "SELECT * FROM bugs WHERE bug_id = ?", (bug_id,)
        ).fetchone()
        if bug is None:
            return []

        itypes = [r["itype"] for r in cur.execute(
            "SELECT itype FROM instruction_types WHERE bug_id = ?", (bug_id,)
        ).fetchall()]

        proposals: List[CampaignProposal] = []
        mc = bug["mismatch_class"]

        # REG_MISMATCH on MUL/DIV → stress the M-extension
        if mc == "REG_MISMATCH" and ("MUL" in itypes or "DIV" in itypes):
            proposals.append(CampaignProposal(
                trigger_bug_id=bug_id,
                campaign_type="stress",
                priority="P0",
                description="M-extension ALU stress: heavy MUL/DIV/REM sequences with corner cases",
                suggested_config={
                    "run_type": "coverage_directed",
                    "isa": {"extensions": ["I","M","Zicsr"]},
                    "agent_config": {
                        "agent_g_bias": {"MUL": 0.4, "DIV": 0.4, "REM": 0.2}
                    },
                    "tags": ["ext:M", "stress:alu", f"trigger:{bug_id}"],
                },
            ))

        # MEM_MISMATCH → RVWMO litmus campaign
        if mc == "MEM_MISMATCH" or "AMO" in itypes or "LOAD" in itypes:
            proposals.append(CampaignProposal(
                trigger_bug_id=bug_id,
                campaign_type="litmus",
                priority="P0",
                description="RVWMO litmus: store-load, fence, and release-acquire ordering",
                suggested_config={
                    "run_type": "coverage_directed",
                    "agent_config": {
                        "agent_i": {
                            "litmus_patterns": ["store_load","fence","release_acquire","amo_ordering"],
                            "max_litmus_tests": 128,
                        }
                    },
                    "tags": ["litmus:rvwmo", f"trigger:{bug_id}"],
                },
            ))

        # TRAP_MISMATCH → trap-sequence directed campaign
        if mc == "TRAP_MISMATCH" or "TRAP" in itypes:
            proposals.append(CampaignProposal(
                trigger_bug_id=bug_id,
                campaign_type="directed",
                priority="P1",
                description="Trap-sequence stress: nested traps, mret, illegal instructions",
                suggested_config={
                    "run_type": "coverage_directed",
                    "tags": ["trap:stress", f"trigger:{bug_id}"],
                },
            ))

        # CSR_MISMATCH → CSR-write directed campaign
        if mc == "CSR_MISMATCH" or "CSR" in itypes:
            proposals.append(CampaignProposal(
                trigger_bug_id=bug_id,
                campaign_type="directed",
                priority="P1",
                description="CSR write stress: rapid mstatus/mtvec/mepc updates",
                suggested_config={
                    "run_type": "coverage_directed",
                    "tags": ["csr:stress", f"trigger:{bug_id}"],
                },
            ))

        # Any mismatch → formal follow-up
        proposals.append(CampaignProposal(
            trigger_bug_id=bug_id,
            campaign_type="formal",
            priority="P2",
            description=f"Formal verification: prove absence of {mc} by design",
            suggested_config={
                "run_type": "formal",
                "formal": {
                    "tool": "symbiyosys",
                    "depth": 30,
                },
                "tags": [f"formal:{mc.lower()}", f"trigger:{bug_id}"],
            },
        ))

        return proposals

    # ── Statistics ────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Return high-level graph statistics."""
        cur = self._conn.cursor()
        total_bugs   = cur.execute("SELECT COUNT(*) FROM bugs").fetchone()[0]
        by_class     = cur.execute(
            "SELECT mismatch_class, COUNT(*) AS cnt FROM bugs GROUP BY mismatch_class ORDER BY cnt DESC"
        ).fetchall()
        total_sims   = cur.execute("SELECT COUNT(*) FROM similar_bugs").fetchone()[0]
        top_itypes   = cur.execute(
            "SELECT itype, COUNT(*) AS cnt FROM instruction_types GROUP BY itype ORDER BY cnt DESC LIMIT 5"
        ).fetchall()

        return {
            "total_bugs":    total_bugs,
            "by_mismatch_class": {r["mismatch_class"]: r["cnt"] for r in by_class},
            "similarity_edges":  total_sims,
            "top_instruction_types": {r["itype"]: r["cnt"] for r in top_itypes},
        }

    # ── Export ────────────────────────────────────────────────────────────────

    def export_json(self, output_path: Path) -> None:
        """Export entire knowledge graph as JSON (for dashboards / external tools)."""
        cur = self._conn.cursor()
        bugs = [dict(r) for r in cur.execute("SELECT * FROM bugs").fetchall()]
        for bug in bugs:
            for key in ("rtl_context_json", "iss_context_json"):
                if bug.get(key):
                    bug[key.replace("_json", "")] = json.loads(bug.pop(key))
                else:
                    bug.pop(key, None)
            bug["instruction_types"] = [
                r["itype"] for r in cur.execute(
                    "SELECT itype FROM instruction_types WHERE bug_id = ?", (bug["bug_id"],)
                ).fetchall()
            ]
            bug["signals"] = [
                r["signal"] for r in cur.execute(
                    "SELECT signal FROM signals WHERE bug_id = ?", (bug["bug_id"],)
                ).fetchall()
            ]

        export = {
            "schema_version": SCHEMA_VERSION,
            "exported_at":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stats":          self.stats(),
            "bugs":           bugs,
        }
        with open(output_path, "w") as f:
            json.dump(export, f, indent=2)
            f.write("\n")
        logger.info("Knowledge graph exported to %s (%d bugs)", output_path, len(bugs))
