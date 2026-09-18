# vhs script — VOE refusing to report a result.
#
# This is the demo worth having. Most tools demonstrate success; this one shows
# the system declining to produce a number, because the board it was given
# cannot exhibit the phenomenon the experiment intends to measure.
#
# Runs in --mock on purpose: mock returns a fixed verdict by construction, so
# the board is degenerate and the eligibility gate fires. That is not a
# workaround for the demo -- it is the honest behaviour, and the script says so.
#
#   vhs voe-eligibility.tape   ->   voe-eligibility.gif

Output "voe-eligibility.gif"

Set Shell bash
Set FontSize 15
Set Width 1200
Set Height 760
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 20

Hide
Type "cd ../../voe_bench" Enter
Type "clear" Enter
Show

Type "# Every VOE experiment commits its criteria BEFORE collecting data,"
Sleep 1500ms
Enter
Type "# and checks whether the board can exhibit the effect at all."
Sleep 2s
Enter
Enter
Sleep 500ms

Type "python3 run_coupled_noise.py --mock --seeds 8"
Sleep 800ms
Enter
Sleep 12s

Enter
Type "# NOT EVALUABLE is a third verdict, not a weak NOT MET:"
Sleep 2s
Enter
Type "#   NOT MET       -> the effect is absent or too small"
Sleep 1500ms
Enter
Type "#   NOT EVALUABLE -> the question was never asked"
Sleep 2500ms
Enter
Enter

Type "# The calibration of the transfer statistic, no tools required:"
Sleep 1500ms
Enter
Type "python3 run_stoch_transfer.py --calibrate"
Sleep 800ms
Enter
Sleep 20s

Enter
Type "# Note the false-alarm column RISES with more seeds."
Sleep 2s
Enter
Type "# 'Run it with more seeds' is not an available response here."
Sleep 3s
Enter
