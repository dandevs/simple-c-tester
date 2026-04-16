import argparse
import sys

from .models import Test, Suite

def parse_args():
    parser = argparse.ArgumentParser(description="Test runner")
    parser.add_argument(
        "--parallel", type=int, default=1, help="Number of parallel workers"
    )
    parser.add_argument("--watch", action="store_true", help="Watch for file changes")
    return parser.parse_args()


def main():
    args = parse_args()
    print(f"Parallel: {args.parallel}")
    print(f"Watch: {args.watch}")


if __name__ == "__main__":
    main()
