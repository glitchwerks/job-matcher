#!/usr/bin/env python3
"""
Benchmark: redirect_url index performance.

Measures the time to check for duplicate listings via redirect_url
with and without the index.

Run from the project root:
    python scripts/benchmark_index.py

Benchmark results (contributed by @Alm0stSurely, PR #138):
  Dataset: 10,000 listings, 500 existence queries
  Without index: ~0.45s   With index: ~0.003s   Speedup: ~150x
"""

import os
import tempfile
import time

import db


def benchmark_without_index(num_listings: int, num_checks: int) -> float:
    """Time listing_exists_by_url checks WITHOUT the index."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = db.get_connection(db_path)

        # Create table without index
        conn.execute("""
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                redirect_url TEXT,
                title TEXT
            )
        """)

        # Insert test data
        for i in range(num_listings):
            conn.execute(
                "INSERT INTO listings (source, source_id, redirect_url, title) VALUES (?, ?, ?, ?)",
                ("test", str(i), f"https://example.com/job/{i}", f"Job {i}")
            )
        conn.commit()

        # Benchmark checks
        start = time.perf_counter()
        for i in range(num_checks):
            url = f"https://example.com/job/{i % (num_listings * 2)}"  # 50% hit rate
            conn.execute("SELECT 1 FROM listings WHERE redirect_url = ?", (url,)).fetchone()
        elapsed = time.perf_counter() - start
        conn.close()
        return elapsed


def benchmark_with_index(num_listings: int, num_checks: int) -> float:
    """Time listing_exists_by_url checks WITH the index."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = db.get_connection(db_path)

        # Create table with index
        conn.execute("""
            CREATE TABLE listings (
                id INTEGER PRIMARY KEY,
                source TEXT,
                source_id TEXT,
                redirect_url TEXT,
                title TEXT
            )
        """)
        conn.execute("CREATE INDEX idx_listings_redirect_url ON listings (redirect_url)")

        # Insert test data
        for i in range(num_listings):
            conn.execute(
                "INSERT INTO listings (source, source_id, redirect_url, title) VALUES (?, ?, ?, ?)",
                ("test", str(i), f"https://example.com/job/{i}", f"Job {i}")
            )
        conn.commit()

        # Benchmark checks
        start = time.perf_counter()
        for i in range(num_checks):
            url = f"https://example.com/job/{i % (num_listings * 2)}"  # 50% hit rate
            conn.execute("SELECT 1 FROM listings WHERE redirect_url = ?", (url,)).fetchone()
        elapsed = time.perf_counter() - start
        conn.close()
        return elapsed


def main():
    print("=" * 60)
    print("Benchmark: redirect_url index performance")
    print("=" * 60)

    # Simulate: 10,000 stored listings, 500 checks per ingest run
    num_listings = 10_000
    num_checks = 500

    print(f"\nDataset: {num_listings:,} listings")
    print(f"Checks: {num_checks:,} existence queries")
    print()

    print("Running benchmark WITHOUT index...")
    time_without = benchmark_without_index(num_listings, num_checks)
    print(f"  Time: {time_without:.4f}s")

    print("Running benchmark WITH index...")
    time_with = benchmark_with_index(num_listings, num_checks)
    print(f"  Time: {time_with:.4f}s")

    print()
    print("-" * 60)
    speedup = time_without / time_with if time_with > 0 else float("inf")
    improvement = ((time_without - time_with) / time_without * 100) if time_without > 0 else 0
    print(f"Speedup: {speedup:.1f}x")
    print(f"Time reduction: {improvement:.1f}%")
    print("-" * 60)

    # Verify query planner uses the index
    print("\nQuery planner verification:")
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        conn = db.get_connection(db_path)
        conn.execute("""
            CREATE TABLE listings (id INTEGER PRIMARY KEY, redirect_url TEXT)
        """)
        conn.execute("CREATE INDEX idx_listings_redirect_url ON listings (redirect_url)")
        conn.execute("INSERT INTO listings (redirect_url) VALUES ('test')")
        conn.commit()

        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM listings WHERE redirect_url = ?",
            ("test",)
        ).fetchall()
        for row in plan:
            print(f"  {row['detail']}")
        conn.close()


if __name__ == "__main__":
    main()
