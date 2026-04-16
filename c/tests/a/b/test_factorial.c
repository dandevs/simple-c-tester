#include <stdio.h>

int factorial(int n) {
    if (n <= 1) return 1;
    return n * factorial(n - 1);
}

int main() {
    int result = factorial(5);
    if (result == 120) {
        printf("Factorial test passed: 5! = %d\n", result);
        return 0;
    }
    return 1;
}
