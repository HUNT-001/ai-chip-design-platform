# One corrupted store: RTL wrote 0xbadbad00 where the ISS wrote 0x0000002a.
# AGENT_D's field comparator must catch it (before the #1 fix it returned 0).
import sys; sys.path.insert(0, 'AGENT_D')
from compare_commitlogs import CommitEntry, _FieldComparator, CompareConfig
base = dict(schema_version='2.1.0', hart=0, seq=2, pc='0x80000008',
            instr='0x00a12023', priv='M', trap=None, regs={}, csrs={})
rtl = dict(base, mem_writes=[{'addr':'0x80001000','size':4,'data':'0xbadbad00'}])
iss = dict(base, mem_writes=[{'addr':'0x80001000','size':4,'data':'0x0000002a'}])
a = CommitEntry.from_dict(rtl, xlen=32, lineno=1, source='rtl.jsonl')
b = CommitEntry.from_dict(iss, xlen=32, lineno=1, source='iss.jsonl')
issues = _FieldComparator(CompareConfig()).compare(a, b)
print(f'issues found: {len(issues)}')
for i in issues: print('   ', i)
