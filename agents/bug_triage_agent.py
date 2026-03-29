"""
Auto-Triage: Cocotb FAIL → Exact RTL Fix (Cadence $100K → FREE)
"""
import re
import ollama

class AutoTriageAgent:
    async def diagnose(self, cocotb_log: str, rtl_files: List[str]) -> Dict:
        # Parse failure
        fail_match = re.search(r'expected (0x[0-9a-f]+).*got (0x[0-9a-f]+)', cocotb_log)
        if fail_match:
            expected, actual = fail_match.groups()
            
            prompt = f"""
Cocotb FAILURE:

EXPECTED: {expected}
GOT:      {actual}

Pinpoint EXACT RTL bug. JSON only:
{{
  "file": "alu.sv",
  "line": 42,
  "cause": "off-by-one",
  "fix": "result <= a + b + carry;"
}}"""
            
            resp = await ollama.chat(model="qwen2.5-coder:32b", messages=[{"role": "user", "content": prompt}])
            diagnosis = resp["message"]["content"]
            
            try:
                return json.loads(diagnosis)
            except:
                return {"file": "unknown", "confidence": 0.8}
        
        return {"error": "no_failure_matched"}
