#include "ctest.h"

int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}

int main(void) {
    ASSERT_EQ(120, factorial(5));
    return 0;
}
