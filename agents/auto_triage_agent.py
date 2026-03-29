"""
Auto-Triage: Cocotb failure → Exact RTL line + Fix (FREE)
"""
import re
import asyncio
from typing import List, Dict
import json

class AutoTriageAgent:
    async def diagnose_failure(self, cocotb_log: str, rtl_files: List[str]) -> Dict:
        # Parse failure pattern
        failure_match = re.search(r'AssertionError: expected (0x[0-9a-f]+), got (0x[0-9a-f]+)', cocotb_log)
        if failure_match:
            expected, actual = failure_match.groups()
            
            prompt = f"""
Cocotb FAILURE ANALYSIS:

EXPECTED: {expected}
ACTUAL:   {actual}
CYCLE:    {self._extract_cycle(cocotb_log)}

RTL FILES:
{chr(10).join(rtl_files)}

Pinpoint EXACT line causing mismatch. Output JSON only:
{{
  "file": "alu.sv",
  "line": 42,
  "cause": "off-by-one adder",
  "fix": "y <= x + carry;",
  "confidence": 0.97
}}"""
            
            diagnosis = await ollama_client.generate(prompt)
            return json.loads(diagnosis)
        
        return {"error": "No failure pattern found"}
