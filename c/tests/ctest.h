#ifndef CTEST_H
#define CTEST_H

/*
 * ctest.h - lightweight assertions for the C Tester TUI.
 *
 * Drop this next to your tests (e.g. tests/ctest.h) and:
 *
 *     #include "ctest.h"
 *
 *     int main(void) {
 *         ASSERT_EQ(factorial(5), 120);          // fatal: returns 1 on fail
 *         ASSERT_STREQ(greet("Dan"), "Hello, Dan!");
 *         EXPECT_EQ(score, 100);                 // soft: keeps going, reports all
    *         return TEST_RESULT();                  // 1 if any EXPECT_* failed
 *     }
 *
 * On failure, each assertion prints one machine-readable line that the
 * C Tester TUI parses for inline diagnostics ("expected X, got Y") and a
 * "jump to assertion" target in the debugger:
 *
 *     [CTEST:1] FAIL tests/test_factorial.c:9 ASSERT_EQ(factorial(5), 120) expected=120 actual=60
 *
 * Two flavors:
 *   - ASSERT_*  aborts the test on failure (returns 1 from main immediately).
 *   - EXPECT_*  records the failure and continues; end main with
 *               `return TEST_RESULT();` to report every failure in one run.
 *
 * Requires C11 (_Generic).  gcc/clang default to gnu11+ so this is a non-issue
 * on any modern toolchain.
 *
 * =============================================================================
 * WIRE FORMAT - the runner parses this exactly. Do not change the sentinel.
 *   [<sentinel>:<version>] FAIL <file>:<line> <MACRO>(<args>) expected=<X> actual=<Y>
 * Sentinel: "[CTEST:"   Version: 1
 * =============================================================================
 */

#include <stdio.h>
#include <string.h>   /* ASSERT_STREQ / ASSERT_STRNE */

#define CTEST_PROTOCOL_VERSION 1
#define CTEST_VAL_BUF 64

/* -------------------------------------------------------------------------- */
/* Internal: per-test failure flag + emit + stringify helpers                  */
/* -------------------------------------------------------------------------- */

/* File-scope flag for EXPECT_*.  Each test is its own binary (one main() per
 * .c file), so this static never collides across tests.
 * Marked __attribute__((unused)) so ASSERT-only tests don't warn. */
static int _ctest_failed __attribute__((unused)) = 0;

/* Emit the structured failure line.  Static inline => no .c file needed. */
static inline void _ctest_emit(const char *file, int line,
                               const char *macro, const char *args,
                               const char *expected, const char *actual) {
    fprintf(stderr, "[CTEST:%d] FAIL %s:%d %s(%s) expected=%s actual=%s\n",
            CTEST_PROTOCOL_VERSION, file, line, macro, args, expected, actual);
}

/* Stringify helpers - one per value category.  _Generic (below) picks the
 * right one by type; the call promotes `v` to the helper's parameter type. */
static inline const char *_ctest_to_str_ll(long long v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "%lld", v);          return buf;
}
static inline const char *_ctest_to_str_ull(unsigned long long v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "%llu", v);          return buf;
}
static inline const char *_ctest_to_str_d(double v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "%g", v);            return buf;
}
static inline const char *_ctest_to_str_ld(long double v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "%.3Lg", v);         return buf;
}
static inline const char *_ctest_to_str_p(const void *v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "%p", v);            return buf;
}
static inline const char *_ctest_to_str_s(const char *v, char *buf) {
    snprintf(buf, CTEST_VAL_BUF, "\"%s\"", v ? v : "(null)"); return buf;
}

/* Type-dispatched stringification.  Yields a function pointer selected by the
 * static type of `v`; the subsequent call converts `v` to that function's
 * parameter type.  Strings are quoted; numerics are bare; pointers as %p. */
#define _ctest_strval(v, buf) _Generic((v), \
    _Bool:              _ctest_to_str_ll, \
    char:               _ctest_to_str_ll, \
    signed char:        _ctest_to_str_ll, \
    unsigned char:      _ctest_to_str_ull, \
    short:              _ctest_to_str_ll, \
    unsigned short:     _ctest_to_str_ull, \
    int:                _ctest_to_str_ll, \
    unsigned int:       _ctest_to_str_ull, \
    long:               _ctest_to_str_ll, \
    unsigned long:      _ctest_to_str_ull, \
    long long:          _ctest_to_str_ll, \
    unsigned long long: _ctest_to_str_ull, \
    float:              _ctest_to_str_d, \
    double:             _ctest_to_str_d, \
    long double:        _ctest_to_str_ld, \
    char *:             _ctest_to_str_s, \
    const char *:       _ctest_to_str_s, \
    default:            _ctest_to_str_p)((v), (buf))

/* -------------------------------------------------------------------------- */
/* Boolean assertions                                                          */
/* -------------------------------------------------------------------------- */

#define ASSERT_TRUE(cond) do { \
    if (!(cond)) { \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_TRUE", #cond, "true", "false"); \
        return 1; \
    } \
} while (0)

#define ASSERT_FALSE(cond) do { \
    if ((cond)) { \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_FALSE", #cond, "false", "true"); \
        return 1; \
    } \
} while (0)

/* -------------------------------------------------------------------------- */
/* Generic typed equality / inequality (works on any ==-comparable type)       */
/* NOTE: each argument is evaluated twice (once for the compare, once for      */
/* stringification).  Avoid side-effecting expressions as arguments.           */
/* -------------------------------------------------------------------------- */

#define ASSERT_EQ(expected, actual) do { \
    if (!((expected) == (actual))) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_EQ", #expected ", " #actual, \
                    _ctest_strval((expected), _ve), \
                    _ctest_strval((actual), _va)); \
        return 1; \
    } \
} while (0)

#define ASSERT_NE(expected, actual) do { \
    if ((expected) == (actual)) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_NE", #expected ", " #actual, \
                    _ctest_strval((expected), _ve), \
                    _ctest_strval((actual), _va)); \
        return 1; \
    } \
} while (0)

/* -------------------------------------------------------------------------- */
/* Numeric comparisons (work on any </<=-comparable type)                      */
/* -------------------------------------------------------------------------- */

#define ASSERT_GT(a, b) do { \
    if (!((a) > (b))) { \
        char _va[CTEST_VAL_BUF], _vb[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_GT", #a ", " #b, \
                    _ctest_strval((a), _va), _ctest_strval((b), _vb)); \
        return 1; \
    } \
} while (0)

#define ASSERT_GE(a, b) do { if (!((a) >= (b))) { \
    char _va[CTEST_VAL_BUF], _vb[CTEST_VAL_BUF]; \
    _ctest_emit(__FILE__, __LINE__, "ASSERT_GE", #a ", " #b, \
                _ctest_strval((a), _va), _ctest_strval((b), _vb)); return 1; } } while (0)

#define ASSERT_LT(a, b) do { if (!((a) < (b))) { \
    char _va[CTEST_VAL_BUF], _vb[CTEST_VAL_BUF]; \
    _ctest_emit(__FILE__, __LINE__, "ASSERT_LT", #a ", " #b, \
                _ctest_strval((a), _va), _ctest_strval((b), _vb)); return 1; } } while (0)

#define ASSERT_LE(a, b) do { if (!((a) <= (b))) { \
    char _va[CTEST_VAL_BUF], _vb[CTEST_VAL_BUF]; \
    _ctest_emit(__FILE__, __LINE__, "ASSERT_LE", #a ", " #b, \
                _ctest_strval((a), _va), _ctest_strval((b), _vb)); return 1; } } while (0)

/* -------------------------------------------------------------------------- */
/* Strings (content comparison, not pointer equality)                          */
/* -------------------------------------------------------------------------- */

#define ASSERT_STREQ(expected, actual) do { \
    if (strcmp((expected), (actual)) != 0) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_STREQ", #expected ", " #actual, \
                    _ctest_to_str_s((expected), _ve), \
                    _ctest_to_str_s((actual), _va)); \
        return 1; \
    } \
} while (0)

#define ASSERT_STRNE(expected, actual) do { \
    if (strcmp((expected), (actual)) == 0) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_STRNE", #expected ", " #actual, \
                    _ctest_to_str_s((expected), _ve), \
                    _ctest_to_str_s((actual), _va)); \
        return 1; \
    } \
} while (0)

/* -------------------------------------------------------------------------- */
/* Pointers                                                                    */
/* -------------------------------------------------------------------------- */

#define ASSERT_NULL(p) do { \
    if ((p) != NULL) { \
        char _vp[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_NULL", #p, "NULL", \
                    _ctest_strval((p), _vp)); \
        return 1; \
    } \
} while (0)

#define ASSERT_NOT_NULL(p) do { \
    if ((p) == NULL) { \
        _ctest_emit(__FILE__, __LINE__, "ASSERT_NOT_NULL", #p, "not NULL", "NULL"); \
        return 1; \
    } \
} while (0)

/* -------------------------------------------------------------------------- */
/* Unconditional failure ("should not reach here", custom logic)               */
/* -------------------------------------------------------------------------- */

#define TEST_FAIL(msg) do { \
    _ctest_emit(__FILE__, __LINE__, "TEST_FAIL", #msg, "pass", "fail"); \
    return 1; \
} while (0)

/* -------------------------------------------------------------------------- */
/* Soft assertions (EXPECT_*): record + continue, report all at once           */
/* -------------------------------------------------------------------------- */

#define EXPECT_TRUE(cond) do { \
    if (!(cond)) { \
        _ctest_emit(__FILE__, __LINE__, "EXPECT_TRUE", #cond, "true", "false"); \
        _ctest_failed = 1; \
    } \
} while (0)

#define EXPECT_FALSE(cond) do { \
    if ((cond)) { \
        _ctest_emit(__FILE__, __LINE__, "EXPECT_FALSE", #cond, "false", "true"); \
        _ctest_failed = 1; \
    } \
} while (0)

#define EXPECT_EQ(expected, actual) do { \
    if (!((expected) == (actual))) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "EXPECT_EQ", #expected ", " #actual, \
                    _ctest_strval((expected), _ve), \
                    _ctest_strval((actual), _va)); \
        _ctest_failed = 1; \
    } \
} while (0)

#define EXPECT_NE(expected, actual) do { \
    if ((expected) == (actual)) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "EXPECT_NE", #expected ", " #actual, \
                    _ctest_strval((expected), _ve), \
                    _ctest_strval((actual), _va)); \
        _ctest_failed = 1; \
    } \
} while (0)

#define EXPECT_STREQ(expected, actual) do { \
    if (strcmp((expected), (actual)) != 0) { \
        char _ve[CTEST_VAL_BUF], _va[CTEST_VAL_BUF]; \
        _ctest_emit(__FILE__, __LINE__, "EXPECT_STREQ", #expected ", " #actual, \
                    _ctest_to_str_s((expected), _ve), \
                    _ctest_to_str_s((actual), _va)); \
        _ctest_failed = 1; \
    } \
} while (0)

/* End main with this when using EXPECT_*:
 *     return TEST_RESULT();   */
#define TEST_RESULT() (_ctest_failed ? 1 : 0)

#endif /* CTEST_H */
