// Input for the VERIFICATION UNKNOWN transcript: k-induction cannot prove
// this without an invariant, so it gives up.
extern "C" {
unsigned __VERIFIER_nondet_uint();
void __ESBMC_assert(bool, const char *);
}

int main() {
    unsigned n = __VERIFIER_nondet_uint();
    unsigned s = 0;
    for (unsigned i = 0; i < n; ++i) s += 1;
    __ESBMC_assert(s == n, "sum equals n");
    return 0;
}
