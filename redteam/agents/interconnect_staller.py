class InterconnectStaller:
    """
    Red-Team Saboteur: Creates livelocks via NoC starvation.
    Detects cache miss → flood dummy traffic.
    """
    def __init__(self, dut):
        self.dut = dut
        self.noc_credit_threshold = 2  # Starvation trigger
    
    @cocotb.test()
    async def stall_campaign(self, dut):
        clock = Clock(dut.clk, 2, units='ns')
        cocotb.start_soon(clock.start())
        
        while True:
            # Monitor global NoC utilization
            noc_util = self.dut.noc_util.value
            
            if noc_util > 0.9 and self.dut.any_core_cache_miss.value:
                # Flood dummy traffic (livelock attempt)
                await self._dummy_traffic_burst()
            
            await Timer(50, units='ns')
    
    async def _dummy_traffic_burst(self):
        """Generate 16 dummy NoC packets"""
        for i in range(16):
            self.dut.dummy_traffic_gen.value = 1
            await RisingEdge(self.dut.clk)
        self.dut.dummy_traffic_gen.value = 0
