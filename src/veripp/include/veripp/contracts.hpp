// veripp/contracts.hpp
//
// Dual-use contract macros:
//   - Under ESBMC (__ESBMC__ defined): expand to assume/assert so the
//     verifier can use preconditions and must prove postconditions.
//   - Under normal builds with VERIPP_RUNTIME_CHECKS: cheap runtime asserts.
//   - Otherwise: compiled away entirely.
//
// Migration path: these map 1:1 onto C++26 contract syntax (pre/post),
// so annotated code has a future in standard C++.

#pragma once

#if defined(__ESBMC__)

// This header is included from both C and C++ harnesses: veripp generates a
// harness in the language of the file under test, because C code that assigns
// malloc's void* without a cast is not valid C++.
#if defined(__cplusplus)
#define VERIPP_BOOL bool
extern "C" {
#else
#define VERIPP_BOOL _Bool
#endif

void __ESBMC_assume(VERIPP_BOOL);
void __ESBMC_assert(VERIPP_BOOL, const char*);

#define VERIPP_REQUIRES(cond) __ESBMC_assume(cond)
#define VERIPP_ENSURES(cond) __ESBMC_assert((cond), "postcondition: " #cond)
#define VERIPP_ASSERT(cond) __ESBMC_assert((cond), "assertion: " #cond)
#define VERIPP_ASSUME(cond) __ESBMC_assume(cond)

// Nondeterministic value constructors for harnesses.
int __VERIFIER_nondet_int();
short __VERIFIER_nondet_short();
unsigned __VERIFIER_nondet_uint();
long __VERIFIER_nondet_long();
unsigned long __VERIFIER_nondet_ulong();
char __VERIFIER_nondet_char();
VERIPP_BOOL __VERIFIER_nondet_bool();
float __VERIFIER_nondet_float();
double __VERIFIER_nondet_double();

#if defined(__cplusplus)
}  // extern "C"
#endif

#define VERIPP_NONDET_INT() __VERIFIER_nondet_int()
#define VERIPP_NONDET_UINT() __VERIFIER_nondet_uint()
#define VERIPP_NONDET_LONG() __VERIFIER_nondet_long()
#define VERIPP_NONDET_ULONG() __VERIFIER_nondet_ulong()
#define VERIPP_NONDET_SIZE() ((unsigned long)__VERIFIER_nondet_ulong())
#define VERIPP_NONDET_CHAR() __VERIFIER_nondet_char()
#define VERIPP_NONDET_BOOL() __VERIFIER_nondet_bool()
// Floating-point inputs are constrained to finite values. Without this an
// unconstrained double may be an infinity or a NaN, and then a/b is NaN for
// inf/inf -- so a NaN check reports every floating-point division in correct
// code. Constrained, the same check becomes useful: quiet on code that
// guards its divisor, and still catching a genuine 0.0/0.0.
//
// This is an assumption, and it is listed with every result that relies on
// it: veripp is checking the function for finite inputs, not for infinities
// a caller could in principle pass.
#define VERIPP_NONDET_FLOAT() (veripp_finite_float())
#define VERIPP_NONDET_DOUBLE() (veripp_finite_double())

static inline float veripp_finite_float(void) {
    float value = __VERIFIER_nondet_float();
    __ESBMC_assume(value == value);                    // not NaN
    __ESBMC_assume(value < __FLT_MAX__ && value > -__FLT_MAX__);
    return value;
}

static inline double veripp_finite_double(void) {
    double value = __VERIFIER_nondet_double();
    __ESBMC_assume(value == value);                    // not NaN
    __ESBMC_assume(value < __DBL_MAX__ && value > -__DBL_MAX__);
    return value;
}

// Guard for a demo/self-test main() living inside a verified source file.
// veripp defines VERIPP_GENERATED_HARNESS in the harness it generates for a
// specific function and #includes the source from it, so the file's own main
// must step aside there; when ESBMC checks the file directly, it is used.
#if !defined(VERIPP_GENERATED_HARNESS)
#define VERIPP_HAS_OWN_MAIN 1
#endif

#elif defined(VERIPP_RUNTIME_CHECKS)

#include <cassert>
#define VERIPP_REQUIRES(cond) assert((cond) && "precondition")
#define VERIPP_ENSURES(cond) assert((cond) && "postcondition")
#define VERIPP_ASSERT(cond) assert(cond)
#define VERIPP_ASSUME(cond) ((void)0)

#else

#define VERIPP_REQUIRES(cond) ((void)0)
#define VERIPP_ENSURES(cond) ((void)0)
#define VERIPP_ASSERT(cond) ((void)0)
#define VERIPP_ASSUME(cond) ((void)0)

#endif
