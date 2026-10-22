"""Convert one or more Azure pricesheet CSV files to a single Parquet file.

Usage
-----
    python -m scripts.convert_pricesheet_to_parquet <input> <output.parquet>

``<input>`` may be a single CSV file, a directory containing CSV files, or a
glob pattern. All loading, schema normalization and contract-type detection
is delegated to :func:`core.pricesheet.load_pricesheet`.
"""

import argparse
import logging
import sys
from time import perf_counter

from core.pricesheet import load_pricesheet


def convert_pricesheet_to_parquet(input_path: str, output_path: str) -> None:
    t0 = perf_counter()
    df = load_pricesheet(
        path=input_path,
        source_format="csv",
        cache_path_directory=None,
    )
    logging.info("Loaded %d row(s) from %s in %.1fs",
                 len(df), input_path, perf_counter() - t0)

    t1 = perf_counter()
    df.to_parquet(output_path, index=False)
    logging.info("Wrote %s in %.1fs", output_path, perf_counter() - t1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Azure pricesheet CSV(s) to a single Parquet file.",
    )
    parser.add_argument("input", help="CSV file, directory of CSVs, or glob pattern.")
    parser.add_argument("output", help="Destination .parquet file path.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    convert_pricesheet_to_parquet(args.input, args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
