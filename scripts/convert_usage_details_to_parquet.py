"""Convert one or more Azure usage detail CSV files to a single Parquet file.

Usage
-----
    python -m scripts.convert_usage_details_to_parquet <input> <output.parquet>

``<input>`` may be a single CSV file, a directory containing CSV files, or a
glob pattern.

Why this does not call :func:`core.usage_data.load_usage_details`
-----------------------------------------------------------------
``load_usage_details`` reads the *entire* CSV into a pandas DataFrame before
anything is written. On very large exports (multi-GB usage details) that peak
allocation exceeds available RAM and raises ``MemoryError``.

This script instead **streams** the conversion: pyarrow reads the CSV in
bounded-size blocks and we flush Parquet row groups incrementally via
:class:`pyarrow.parquet.ParquetWriter`. Peak memory is therefore a function of
the block / row-group size, not of the total file size, so arbitrarily large
files convert in roughly constant memory.

Column dtypes (string / category / float / date) are taken from the canonical
EA / MCA schema maps in :mod:`core.usage_data`, so the resulting Parquet has
stable, correct types that :func:`load_usage_details` reads back natively.
"""

import argparse
import logging
import sys
from time import perf_counter

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from core.commons import build_rename_map, detect_contract_type, resolve_source_files
from core.usage_data import (
    EA_COLUMN_DTYPES,
    EA_COLUMN_SYNONYMS,
    EA_DATE_FORMAT,
    MCA_COLUMN_DTYPES,
    MCA_COLUMN_SYNONYMS,
    MCA_DATE_FORMAT,
    _PARSE_DATE_SENTINEL,
    _USAGE_EA_MARKER,
    _USAGE_MCA_MARKER,
)

# Null tokens to treat as missing, mirroring the pandas loader's na_values.
_NULL_VALUES = ["", "null", "NaN", "nan"]


def _read_csv_header(path: str) -> list[str]:
    """Return the column names from a CSV file's first (header) row.

    Uses pyarrow's CSV reader with a small block so only the first block is
    materialised — enough to expose the schema's field names without loading
    the whole file.
    """
    reader = pacsv.open_csv(
        path,
        read_options=pacsv.ReadOptions(block_size=1 * 1024 * 1024),
    )
    try:
        return reader.schema.names
    finally:
        # RecordBatchReader has no explicit close; drop the reference so the
        # underlying file handle is released promptly.
        del reader


def _arrow_type_for_dtype(dtype) -> pa.DataType:
    """Translate a single EA/MCA pandas dtype into its pyarrow column type."""
    if dtype == _PARSE_DATE_SENTINEL:
        return pa.timestamp("us")
    if dtype in ("str", "category"):
        # Parquet dictionary-encodes string columns on disk, so 'category'
        # needs no special in-stream handling.
        return pa.string()
    # Remaining schema entries are pd.Float64Dtype() numeric measures.
    return pa.float64()


def _build_arrow_column_types(
    headers: list[str], schema_map: dict, synonyms: dict[str, list[str]]
) -> dict[str, pa.DataType]:
    """Map each actual CSV header to an explicit pyarrow type.

    The schema map is keyed by canonical column names (PascalCase), but source
    exports may use a different casing (MCA headers are camelCase) or aliases.
    Each header is resolved to its canonical name via :func:`build_rename_map`
    (exact > synonym > case-insensitive) — the same resolution the loader uses —
    so numeric / date columns get their correct type regardless of source
    casing. Headers with no canonical match fall back to string, which keeps
    pyarrow from settling on the `null` type for columns that are all-empty in a
    leading block (e.g. `reservationId`).

    Forcing explicit types also keeps the streamed schema stable across every
    block and file (pyarrow otherwise infers per first block, which can disagree
    between files or flip on later blocks) and avoids costly re-inference.
    """
    # {source_name: canonical_name} for headers that differ from canonical.
    rename_map = build_rename_map(list(schema_map.keys()), headers, synonyms)
    types: dict[str, pa.DataType] = {}
    unresolved: list[str] = []
    for col in headers:
        canonical = rename_map.get(col, col)
        dtype = schema_map.get(canonical)
        if dtype is None:
            unresolved.append(col)
            types[col] = pa.string()
        else:
            types[col] = _arrow_type_for_dtype(dtype)
    if unresolved:
        logging.warning(
            "%d column(s) did not resolve to a canonical schema name and were "
            "typed as string: %s. Numeric/date columns typed this way will not "
            "aggregate correctly downstream — check for header renames or update "
            "the EA/MCA schema map.",
            len(unresolved), ", ".join(unresolved),
        )
    return types


def convert_usage_details_to_parquet(
    input_path: str,
    output_path: str,
    block_size_mb: int = 128,
    row_group_size: int = 1_000_000,
    compression: str | None = "snappy",
) -> None:
    source_files, actual_source_format, source_type = resolve_source_files(
        input_path, source_format="csv"
    )
    if actual_source_format != "csv":
        raise ValueError("This converter only accepts CSV input.")
    logging.debug(
        "Source resolved — type: %s | format: %s | %d file(s).",
        source_type, actual_source_format, len(source_files),
    )

    contract_type = detect_contract_type(
        source_files, actual_source_format, _USAGE_EA_MARKER, _USAGE_MCA_MARKER
    )
    if contract_type == "unknown":
        raise ValueError(
            f"Could not detect contract type. Expected exactly one of "
            f"{_USAGE_EA_MARKER!r} (EA) or {_USAGE_MCA_MARKER!r} (MCA) columns."
        )
    logging.debug("Contract type detected: %s.", contract_type)

    schema_map = EA_COLUMN_DTYPES if contract_type == "ea" else MCA_COLUMN_DTYPES
    date_format = EA_DATE_FORMAT if contract_type == "ea" else MCA_DATE_FORMAT
    synonyms = EA_COLUMN_SYNONYMS if contract_type == "ea" else MCA_COLUMN_SYNONYMS

    # Resolve every actual CSV header to its canonical dtype (handles MCA
    # camelCase headers and aliases); unknown columns fall back to string.
    column_types: dict[str, pa.DataType] = {}
    for f in source_files:
        column_types.update(
            _build_arrow_column_types(_read_csv_header(f), schema_map, synonyms)
        )

    read_options = pacsv.ReadOptions(block_size=block_size_mb * 1024 * 1024)
    convert_options = pacsv.ConvertOptions(
        column_types=column_types,
        timestamp_parsers=[date_format],
        null_values=_NULL_VALUES,
        strings_can_be_null=True,
    )

    writer: pq.ParquetWriter | None = None
    buffered: list[pa.RecordBatch] = []
    buffered_rows = 0
    total_rows = 0
    t0 = perf_counter()

    def _flush() -> None:
        nonlocal buffered, buffered_rows
        if not buffered:
            return
        table = pa.Table.from_batches(buffered, schema=writer.schema)
        writer.write_table(table)
        buffered = []
        buffered_rows = 0

    try:
        for f in source_files:
            logging.info("Streaming csv file: %s", f)
            reader = pacsv.open_csv(
                f, read_options=read_options, convert_options=convert_options
            )
            if writer is None:
                writer = pq.ParquetWriter(
                    output_path, reader.schema, compression=compression
                )
            elif reader.schema != writer.schema:
                raise ValueError(
                    f"Column schema mismatch: {f!r} differs from the first file."
                )

            for batch in reader:
                buffered.append(batch)
                buffered_rows += batch.num_rows
                total_rows += batch.num_rows
                if buffered_rows >= row_group_size:
                    _flush()
            _flush()
    finally:
        if writer is not None:
            writer.close()

    logging.info(
        "Wrote %d row(s) to %s in %.1fs", total_rows, output_path, perf_counter() - t0
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Azure usage detail CSV(s) to a single Parquet file "
        "(streamed; constant memory for arbitrarily large files).",
    )
    parser.add_argument("input", help="CSV file, directory of CSVs, or glob pattern.")
    parser.add_argument("output", help="Destination .parquet file path.")
    parser.add_argument(
        "--block-size-mb", type=int, default=128,
        help="CSV read block size in MiB. Lower it to reduce peak memory. Default: 128.",
    )
    parser.add_argument(
        "--row-group-size", type=int, default=1_000_000,
        help="Approx rows per Parquet row group. Default: 1,000,000.",
    )
    parser.add_argument(
        "--compression", default="snappy",
        choices=["snappy", "zstd", "gzip", "brotli", "lz4", "none"],
        help="Parquet compression codec. Default: snappy.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    convert_usage_details_to_parquet(
        args.input,
        args.output,
        block_size_mb=args.block_size_mb,
        row_group_size=args.row_group_size,
        compression=None if args.compression == "none" else args.compression,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
