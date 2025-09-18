import random
import time


def main():
    while True:
        random_number = random.randint(0, 100)
        print(f"Hello, World! {random_number}", flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main()
