#include <stdio.h>

int main() {
    int sum = 0;
    for (int i = 1; i <= 10; i++) {
        sum += i;
    }
    if (sum == 55) {
        printf("Sum test passed: sum of 1 to 10 = %d\n", sum);
        return 0;
    }
    return 1;
}
