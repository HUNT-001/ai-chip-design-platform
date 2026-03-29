"""
Test Writer Agent - Coverage-Driven Cocotb [web:101]
"""
import ollama

class TestWriterAgent:
    async def generate_test(self, dut_name: str, gaps: List[str]) -> str:
        prompt = f"""
Generate Cocotb test targeting gaps: {gaps}
DUT: {dut_name}

Full Python Cocotb testbench with assertions."""
        
        resp = await ollama.chat(model="qwen2.5-coder:32b", messages=[{"role": "user", "content": prompt}])
        return resp["message"]["content"]
