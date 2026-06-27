import requests
import os
import threading
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

URL = os.environ.get("PROXY_URL", "http://localhost:9999")
latencies: list[float] = []

def worker():
    start_time = time.perf_counter()
    response = requests.get(URL)
    latency = (time.perf_counter() - start_time) * 1000
    latencies.append(latency)

def main():
    threads = []

    for _ in range(100):
        thread = threading.Thread(target=worker)

        thread.start()

        threads.append(thread)

    for thread in threads:
        thread.join()

    logging.info(f"Latency: {sum(latencies) / len(latencies):.2f}ms")

if __name__ == "__main__":
    main()