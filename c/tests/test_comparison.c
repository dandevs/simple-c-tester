#include <stdio.h>

int main() {
    int a = 15;
    int b = 20;
    printf("Comparison test: %d < %d is ", a, b);
    // This test will fail - intentionally wrong comparison
    if (a > b) {
        printf("true\n");
        return 0;
    }
    printf("false\n");
    fprintf(stderr, "FAIL: Comparison test failed - %d is not greater than %d\n", a, b);
    return 1;
}
