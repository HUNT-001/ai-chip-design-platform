# vhs script — the AVA pipeline on a sample core.
#
# Deliberately uses the no-EDA-tools path so anyone who clones the repository
# can regenerate this without installing Verilator or Spike.
#
#   vhs ava-pipeline.tape   ->   ava-pipeline.gif

Output "ava-pipeline.gif"

Set Shell bash
Set FontSize 15
Set Width 1200
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 20

Hide
Type "cd ../.." Enter
Type "clear" Enter
Show

Type "# AVA — 6-phase pipeline, no EDA toolchain required"
Sleep 1s
Enter
Sleep 500ms

Type "cat > /tmp/demo_core.sv <<'EOF'" Enter
Type "module demo_core (" Enter
Type "    input  wire        clk, rst_n," Enter
Type "    input  wire [31:0] instr_i," Enter
Type "    output reg  [31:0] data_o" Enter
Type ");" Enter
Type "    reg [31:0] pc;" Enter
Type "endmodule" Enter
Type "EOF" Enter
Sleep 1s

Type "python3 ava_patched.py --rtl /tmp/demo_core.sv --microarch in_order --no-llm --seed 42"
Sleep 1s
Enter
Sleep 25s

Type "# Phases 1-5 are the core loop; phase 6 runs the 14 extended modules"
Sleep 2s
Enter
Sleep 3s
