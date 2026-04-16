#include <stdio.h>
#include <string.h>

int main() {
    char str1[] = "Hello";
    char str2[] = "World";
    printf("String concatenation test: %s %s\n", str1, str2);
    // This test will fail - intentionally checking wrong condition
    if (strcmp(str1, str2) == 0) {
        return 0;
    }
    fprintf(stderr, "FAIL: String compare test - '%s' != '%s'\n", str1, str2);
    return 1;
}
