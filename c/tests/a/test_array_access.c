#include <stdio.h>

int main() {
    int arr[] = {10, 20, 30, 40, 50};
    if (arr[2] == 30) {
        printf("Array access test passed: arr[2] = %d\n", arr[2]);
        return 0;
    }
    return 1;
}
