// A classic off-by-one: veripp/ESBMC should produce a counterexample.
#include "veripp/contracts.hpp"

int sum_array(const int* a, unsigned n) {
    int s = 0;
    for (unsigned i = 0; i <= n; ++i)  // BUG: <= reads one past the end
        s += a[i];
    return s;
}

#if defined(__ESBMC__)
int main() {
    unsigned n = VERIPP_NONDET_UINT();
    VERIPP_ASSUME(n >= 1 && n <= 4);
    int a[4];
    for (unsigned i = 0; i < n; ++i) a[i] = VERIPP_NONDET_INT();
    (void)sum_array(a, n);
    return 0;
}
#endif
