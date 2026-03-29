class RedTeamDriver:
    """Connects Red-Team agents to RTL interfaces"""
    
    @cocotb.test()
    async def red_team_swarm(self, dut):
        """Orchestrates all Red-Team agents"""
        clock = Clock(dut.clk, 2, units='ns')
        cocotb.start_soon(clock.start())
        
        # Launch adversarial agents
        conflict = ConflictAgent(dut)
        staller = InterconnectStaller(dut)
        graph = DependencyGraph()
        
        # Parallel execution
        cocotb.start_soon(conflict.aggressor_campaign(dut))
        cocotb.start_soon(staller.stall_campaign(dut))
        
        # Monitor + Graph updates
        while True:
            bus_txn = await self._capture_bus_transaction(dut)
            alert = graph.update_from_bus_monitor(bus_txn)
            if alert:
                # Assertion failure + log
                assert False, f"Red-Team Alert: {alert}"
            await Timer(10, units='ns')
    
    async def _capture_bus_transaction(self, dut):
        """Bus monitor abstraction"""
        await RisingEdge(dut.clk)
        return (dut.src_id.value, dut.dst_id.value, dut.address.value)
