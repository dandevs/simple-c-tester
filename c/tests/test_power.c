#include <stdio.h>

int power(int base, int exp) {
    int result = 1;
    for (int i = 0; i < exp; i++) {
        result *= base;
    }
    return result;
}

int main() {
    int result = power(2, 8);
    if (result == 256) {
        printf("Power test passed: 2^8 = %d\n", result);
        return 0;
    }
    return 1;
}
