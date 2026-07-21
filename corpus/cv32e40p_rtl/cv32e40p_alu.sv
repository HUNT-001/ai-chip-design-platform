// PLACEHOLDER — not real cv32e40p source.
//
// A copy step in the corpus setup accidentally wrote ibex_alu.sv to this path.
// The file has been blanked rather than left mislabelled, because a corpus file
// whose name does not match its contents silently corrupts any similarity,
// clone-detection or per-core comparison built on top of it. (It would, for
// example, report similarity(ibex_alu, cv32e40p_alu) = 1.0000 — which is
// exactly the false result that surfaced during setup.)
//
// To populate this directory for real, place the CV32E40P RTL here:
//   corpus/cv32e40p_rtl/   <- contents of cv32e40p/rtl/*.sv
//
// See docs/DATA_AND_HARDWARE_REQUIREMENTS.md for the full corpus layout.
