#include <stdio.h>

int main() {
    int sum = 0;
    for (int i = 1; i <= 10; i++) {
        sum += i;
    }
    if (sum == 55) {
        return 0;
    }
    return 1;
}
