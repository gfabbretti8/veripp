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
#define VERIPP_NONDET_FLOAT() __VERIFIER_nondet_float()
#define VERIPP_NONDET_DOUBLE() __VERIFIER_nondet_double()

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
