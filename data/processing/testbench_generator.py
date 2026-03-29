"""
Auto-generate baseline testbenches for RTL validation
"""
from typing import List, Dict
import random
from dataclasses import dataclass
from .rtl_parser import RTLModule

@dataclass
class TestCase:
    """Single test case"""
    name: str
    inputs: Dict[str, List[int]]
    expected_outputs: Dict[str, List[int]]
    description: str

class BaselineTestbenchGenerator:
    """Generates simple testbenches for validation"""
    
    def generate_testbench(self, module: RTLModule) -> Dict:
        """Generate testbench for RTL module"""
        testbench = {
            "module_name": module.name,
            "language": module.language,
            "test_cases": self._generate_test_cases(module),
            "clock_period": 10,  # ns
            "reset_cycles": 5,
            "total_cycles": 100
        }
        return testbench
    
    def _generate_test_cases(self, module: RTLModule) -> List[TestCase]:
        """Generate test cases based on ports"""
        test_cases = []
        
        # Reset test
        test_cases.append(TestCase(
            name="reset_test",
            inputs={p["name"]: [0] * 5 for p in module.ports if p["direction"] != "output"},
            expected_outputs={p["name"]: [0] * 5 for p in module.ports if p["direction"] == "output"},
            description="Verify reset behavior"
        ))
        
        # Random input tests
        for i in range(10):
            inputs = {}
            for port in module.ports:
                if port["direction"] != "output":
                    # Generate random inputs
                    width = int(port["width"].split(":")[0]) if ":" in port["width"] else 1
                    inputs[port["name"]] = [random.randint(0, (1 << width) - 1)]
            
            test_cases.append(TestCase(
                name=f"random_test_{i}",
                inputs=inputs,
                expected_outputs={},  # Reference model would compute this
                description="Random input stimulus"
            ))
        
        return test_cases


if __name__ == "__main__":
    from rtl_parser import RTLParser
    
    parser = RTLParser()
    generator = BaselineTestbenchGenerator()
    
    modules = parser.parse_directory(Path("data/datasets/riscv_cores"))
    for module in modules[:2]:
        tb = generator.generate_testbench(module)
        print(f"Generated testbench for {module.name}: {len(tb['test_cases'])} test cases")
