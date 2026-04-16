#include <stdio.h>

int main() {
    int result = 20 / 4;
    // This test will fail - intentionally wrong assertion
    if (result == 6) {
        return 0;
    }
    fprintf(stderr, "FAIL: Division test expected 6, got %d\n", result);
    return 1;
}
