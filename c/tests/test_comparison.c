#include <stdio.h>

int main() {
    int a = 20;
    int b = 20;
    // This test will fail - intentionally wrong comparison
    if (a > b) {
        return 0;
    }
    fprintf(stderr, "FAIL: Comparison test failed - %d is not greater than %d\n", a, b);
    return 1;
}
