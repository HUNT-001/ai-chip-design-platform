"""
Red-Team Multi-Core RISC-V Verification Suite
Targets: Cache coherency Heisenbugs, NoC deadlocks
"""
import cocotb
from cocotb.triggers import Timer
from redteam.driver.redteam_driver import RedTeamDriver
from redteam.agents.conflict_agent import ConflictAgent
from redteam.graph.dependency_graph import DependencyGraph

@cocotb.test()
async def red_team_multi_core(dut):
    """Full Red-Team swarm against multi-core NoC"""
    driver = RedTeamDriver(dut)
    await driver.red_team_swarm(dut)

@cocotb.test()
async def conflict_agent_only(dut):
    """Isolate Conflict Agent testing"""
    agent = ConflictAgent(dut)
    await agent.aggressor_campaign(dut)
