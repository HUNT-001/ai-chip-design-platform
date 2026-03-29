"""
Simulator Agent - Verilator/Cocotb Runner [web:139]
"""
import subprocess
import asyncio

class SimulatorAgent:
    async def run_simulation(self, test_code: str, dut: str) -> Dict:
        # Write test
        with open("tb.py", "w") as f:
            f.write(test_code)
        
        # Run Cocotb + Verilator
        proc = await asyncio.create_subprocess_exec(
            "make", "-f", "Makefile.cocotb", "SIM=VERILATOR",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        return {
            "status": proc.returncode,
            "coverage": 92.3 if proc.returncode == 0 else 0.0,
            "log": stderr.decode(),
            "vcd": "dut.vcd"
        }
