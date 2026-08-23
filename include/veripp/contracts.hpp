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

extern "C" {
void __ESBMC_assume(bool);
void __ESBMC_assert(bool, const char*);
}

#define VERIPP_REQUIRES(cond) __ESBMC_assume(cond)
#define VERIPP_ENSURES(cond) __ESBMC_assert((cond), "postcondition: " #cond)
#define VERIPP_ASSERT(cond) __ESBMC_assert((cond), "assertion: " #cond)
#define VERIPP_ASSUME(cond) __ESBMC_assume(cond)

// Nondeterministic value constructors for harnesses.
extern "C" {
int __VERIFIER_nondet_int();
unsigned __VERIFIER_nondet_uint();
long __VERIFIER_nondet_long();
unsigned long __VERIFIER_nondet_ulong();
char __VERIFIER_nondet_char();
bool __VERIFIER_nondet_bool();
float __VERIFIER_nondet_float();
double __VERIFIER_nondet_double();
}

#define VERIPP_NONDET_INT() __VERIFIER_nondet_int()
#define VERIPP_NONDET_UINT() __VERIFIER_nondet_uint()
#define VERIPP_NONDET_SIZE() ((unsigned long)__VERIFIER_nondet_ulong())
#define VERIPP_NONDET_BOOL() __VERIFIER_nondet_bool()
#define VERIPP_NONDET_DOUBLE() __VERIFIER_nondet_double()

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
