#include "ctest.h"

int main(void) {
    int result = 20 / 4;
    /* Intentionally wrong: 20 / 4 == 5, not 6. Exercises the wire format. */
    ASSERT_EQ(6, result);
    return 0;
}
