# vhs script — the comparator catching a real divergence.
#
# Shows the thing that matters most about AGENT_D: it is the sole arbiter of
# pass/fail, so what it cannot see is a silent false PASS. This demo runs the
# canonical-schema regression tests added for issue #1, which are exactly the
# cases the comparator used to miss.
#
#   vhs bug-detect.tape   ->   bug-detect.gif

Output "bug-detect.gif"

Set Shell bash
Set FontSize 15
Set Width 1200
Set Height 720
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 20

Hide
Type "cd ../.." Enter
Type "clear" Enter
Show

Type "# The comparator is the sole arbiter of pass/fail (interfaces.md P1)."
Sleep 1500ms
Enter
Type "# So anything it cannot read is a silent PASS on broken RTL."
Sleep 2s
Enter
Enter
Sleep 500ms

Type "python3 - <<'EOF'" Enter
Type "import sys; sys.path.insert(0, 'AGENT_D')" Enter
Type "from compare_commitlogs import CommitEntry, _FieldComparator, CompareConfig" Enter
Type "" Enter
Type "# same instruction, different store data: RTL corrupted it" Enter
Type "base = dict(schema_version='2.1.0', hart=0, seq=2, pc='0x80000008'," Enter
Type "            instr='0x00a12023', priv='M', trap=None, regs={}, csrs={})" Enter
Type "rtl = dict(base, mem_writes=[{'addr':'0x80001000','size':4,'data':'0xbadbad00'}])" Enter
Type "iss = dict(base, mem_writes=[{'addr':'0x80001000','size':4,'data':'0x0000002a'}])" Enter
Type "" Enter
Type "a = CommitEntry.from_dict(rtl, xlen=32, lineno=1, source='rtl.jsonl')" Enter
Type "b = CommitEntry.from_dict(iss, xlen=32, lineno=1, source='iss.jsonl')" Enter
Type "issues = _FieldComparator(CompareConfig()).compare(a, b)" Enter
Type "print(f'issues found: {len(issues)}')" Enter
Type "for i in issues: print('   ', i)" Enter
Type "EOF" Enter
Sleep 6s

Enter
Type "# Before the fix for #1 this printed 0 — a hard PASS on corrupted store data."
Sleep 2500ms
Enter
Enter

Type "python3 -m pytest AGENT_D/test_comparator.py -q --tb=no" Enter
Sleep 8s
Sleep 3s
