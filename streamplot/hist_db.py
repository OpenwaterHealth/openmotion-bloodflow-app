from pickle import NONE
import sqlite3
from types import NoneType
import zlib
import os
import sys
import time
import gzip
import io
import pandas as pd
from pymongo import MongoClient
from datetime import datetime
import zstandard as zstd
import io
import numpy as np
import sys

def csv_to_binary_dataframe(csv_path, binary_path):
    """
    Reads a CSV file into a DataFrame and saves it as a binary file.
    """
    try:
        # Read CSV into DataFrame
        df = pd.read_csv(csv_path)
        print(f"CSV loaded successfully with {len(df)} rows and {len(df.columns)} columns.")

        # Save DataFrame to binary (Pickle format)
        df.to_pickle(binary_path)
        print(f"DataFrame saved to binary file: {binary_path}")

    except FileNotFoundError:
        print(f"Error: CSV file '{csv_path}' not found.")
    except pd.errors.EmptyDataError:
        print("Error: CSV file is empty.")
    except Exception as e:
        print(f"Unexpected error: {e}")

def binary_to_csv(binary_path, output_csv_path):
    """
    Reads a binary DataFrame file and saves it back to CSV.
    """
    try:
        # Load DataFrame from binary
        df = pd.read_pickle(binary_path)
        print(f"Binary file loaded successfully with {len(df)} rows and {len(df.columns)} columns.")

        # Save DataFrame to CSV
        df.to_csv(output_csv_path, index=False)
        print(f"DataFrame saved back to CSV: {output_csv_path}")

    except FileNotFoundError:
        print(f"Error: Binary file '{binary_path}' not found.")
    except Exception as e:
        print(f"Unexpected error: {e}")
"""
CREATE TABLE samples (
    ts INTEGER NOT NULL,
    sensor_id INTEGER NOT NULL,
    is_keyframe INTEGER NOT NULL,
    hist BLOB NOT NULL
);
"""
def compress_csv_file_to_blob(csv_path: str, level=9) -> bytes:
    if not os.path.isfile(csv_path):
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    #
    with open(csv_path, "rb") as f:
        csv_data = f.read()
    #
    compressed_data = zlib.compress(csv_data, level)
    return compressed_data
""" """
def compress_zlib(csv_data, level) -> bytes:
    compressed_data = zlib.compress(csv_data, level)
    return compressed_data
""" """
def init_sqlite_db(db_path: str):
    """
    Initializes SQLite DB in WAL mode and ensures the table exists.
    """
    conn=None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL;")  # Enable WAL mode
        conn.execute("""
            CREATE TABLE IF NOT EXISTS samples (
                ts INTEGER NOT NULL,
                sensor_id INTEGER NOT NULL,
                is_keyframe INTEGER NOT NULL,
                hist BLOB NOT NULL
            );
        """)
        conn.commit()
    except sqlite3.Error as e:
        print(f"[DB ERROR] {e}")
        if conn is not None: 
            conn.close()
    return conn

def csv_to_blob(csv_data):
    """convert csv to binary content."""

    return csv_data
""" """
def blob_to_csv(blob_data):
    encoding="utf-8"
    ret = blob_data.decode(encoding)
    return ret

""" """
def insert_csv_data_sqlite(conn, ts, sensor_id, is_keyframe, csv_data):
    """Insert CSV data as BLOB into the database."""
    try:
        blob_data = csv_to_blob(csv_data)
        conn.execute(
            "INSERT INTO samples (ts, sensor_id, is_keyframe, hist) VALUES (?, ?, ?, ?)",
            (ts, sensor_id, is_keyframe, blob_data)
        )
        conn.commit()
    except Exception as e:
        print(f"[INSERT ERROR] {e}")
""" """
def retrieve_csv_data_sqlite(conn, ts, sensor_id):
    ret = None
    try:
        cursor = conn.execute(
            "SELECT hist FROM samples WHERE ts=? AND sensor_id=?",
            (ts, sensor_id)
        )
        row = cursor.fetchone()
        if row is None:
            print("[INFO] No matching record found.")
    except Exception as e:
        print(f"[RETRIEVE ERROR] {e}")
    return row[0] if row else None
""" """
def insert_blob_sqlite(conn, ts: int, sensor_id: int, is_keyframe: int, hist_blob: bytes):
    """
    Inserts blob into the samples table.
    """
    if not isinstance(hist_blob, (bytes, bytearray)):
        raise TypeError("hist_blob must be bytes or bytearray")
    
    conn.execute(
        "INSERT INTO samples (ts, sensor_id, is_keyframe, hist) VALUES (?, ?, ?, ?)",
        (ts, sensor_id, is_keyframe, hist_blob)
    )
    conn.commit()
""" """
def retrieve_blob_sqlite(conn, ts, sensor_id):
    """Retrieve BLOB for a given sensor_id."""
    #cursor = conn.execute(
    #    "SELECT ts, sensor_id, is_keyframe, hist FROM samples WHERE sensor_id = ? ORDER BY ts DESC LIMIT 1",
    #    (sensor_id,)
    #)
    conn.row_factory = None  # Return raw tuples
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT hist
        FROM samples
        WHERE ts = ? AND sensor_id = ?
        LIMIT 1
        """,
        (ts, sensor_id)
    )
    row = cursor.fetchone()
    if row:
        blob = row
        return blob
    else:
        raise ValueError(f"No data found for sensor_id={sensor_id}")
""" """
def decompress_csv_zlib(blob_data):
    """Decompress BLOB data back to CSV string."""
    try:
        csv_str = zlib.decompress(blob_data).decode("utf-8")
        return csv_str
    except zlib.error as e:
        raise ValueError(f"Error decompressing CSV data: {e}")
""" """
def write_csv_file(path, csv_data):
    with open(path, "wb") as f:
            f.write(csv_data)
    f.close()
""" """
def read_csv_file(path):
    with open(path, "rb") as f:
        ret = f.read()
    return ret
""" """
def init_mongodb():
    client = MongoClient("mongodb://localhost:27017/")  # Change if needed # powershell: mongod --config "C:\MongoDB\mongod.conf"
    db = client["test_db"]
    collection = db["samples"]
    return collection
""" """
def csv_to_gzip_blob(csv_data) -> bytes:
    try:
        return gzip.compress(csv_data)
    except Exception as e:
        print(e)
""" """
def gzip_blob_to_csv(blob: bytes) -> pd.DataFrame:
    try:
        decompressed = gzip.decompress(blob)
        return pd.read_csv(io.BytesIO(decompressed))
    except Exception as e:
        raise ValueError(f"Failed to decompress or parse CSV: {e}")
""" """
def store_csv_blob_to_mongo(collection, ts: int, sensor_id: int, is_keyframe: int, blob):
    doc = {
        "ts": ts,
        "sensor_id": sensor_id,
        "is_keyframe": is_keyframe,
        "hist": blob
    }
    result = collection.insert_one(doc)
""" """
def retrieve_csv_blob_from_mongo(collection, ts: int, sensor_id: int):
    doc = collection.find_one({"ts": ts, "sensor_id": sensor_id})
    if not doc:
        print("No matching document found.")
        return None
    blob = doc["hist"]
    return blob
""" """
def zstd_compress(data: bytes, compression_level: int = 3) -> bytes:
    """
    Compress data using Zstandard.
    
    :param data: Data to compress (bytes).
    :param compression_level: Compression level (1-22, higher = better compression but slower).
    :return: Compressed data as bytes.
    """
    if not isinstance(data, bytes):
        raise TypeError("Input data must be of type 'bytes'.")
    if not (1 <= compression_level <= 22):
        raise ValueError("Compression level must be between 1 and 22.")

    compressor = zstd.ZstdCompressor(level=compression_level)
    return compressor.compress(data)
""" """
def zstd_decompress(compressed_data: bytes) -> bytes:
    """
    Decompress Zstandard-compressed data.
    
    :param compressed_data: Compressed data (bytes).
    :return: Decompressed data as bytes.
    """
    if not isinstance(compressed_data, bytes):
        raise TypeError("Compressed data must be of type 'bytes'.")

    decompressor = zstd.ZstdDecompressor()
    return decompressor.decompress(compressed_data)

def delta_encode(df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply delta encoding to numeric columns of a DataFrame.
    Non-numeric columns are left unchanged.
    """
    encoded_df = df.copy()
    for col in encoded_df.select_dtypes(include=[np.number]).columns:
        encoded_df[col] = encoded_df[col].diff().fillna(encoded_df[col])
    return encoded_df

def delta_decode(df: pd.DataFrame) -> pd.DataFrame:
    """
    Reverse delta encoding for numeric columns.
    """
    decoded_df = df.copy()
    for col in decoded_df.select_dtypes(include=[np.number]).columns:
        decoded_df[col] = decoded_df[col].cumsum()
    return decoded_df
""" """
def compress_csv_file_with_delta(input_csv_path: str, output_bin_path: str):
    """
    Reads a CSV, applies delta encoding, compresses with Zstandard, and saves to a binary file.
    """
    try:
        df = pd.read_csv(input_csv_path)
    except Exception as e:
        print(f"Error reading CSV: {e}")
        sys.exit(1)
    # Apply delta encoding
    delta_df = delta_encode(df)
    # Convert DataFrame to CSV bytes
    csv_bytes = delta_df.to_csv(index=False).encode("utf-8")
    # Compress with Zstandard
    compressor = zstd.ZstdCompressor(level=10)  # Higher level = better compression
    compressed = compressor.compress(csv_bytes)
    # Save compressed data
    with open(output_bin_path, "wb") as f:
        f.write(compressed)
    print(f"Compressed CSV saved to {output_bin_path}")

def decompress_csv_file_with_delta(input_bin_path: str, output_csv_path: str):
    """
    Reads a compressed binary file, decompresses with Zstandard, applies delta decoding, and saves as CSV.
    """
    try:
        with open(input_bin_path, "rb") as f:
            compressed_data = f.read()
    except Exception as e:
        print(f"Error reading binary file: {e}")
        sys.exit(1)
    # Decompress
    decompressor = zstd.ZstdDecompressor()
    try:
        csv_bytes = decompressor.decompress(compressed_data)
    except zstd.ZstdError as e:
        print(f"Decompression failed: {e}")
        sys.exit(1)
    # Load into DataFrame
    delta_df = pd.read_csv(io.BytesIO(csv_bytes))
    # Apply delta decoding
    decoded_df = delta_decode(delta_df)
    # Save to CSV
    decoded_df.to_csv(output_csv_path, index=False)
    print(f"Decompressed CSV saved to {output_csv_path}")

    # Compress with Zstandard
def compress_zstd(blob_in, level):
    compressor = zstd.ZstdCompressor(level)  # Higher level = better compression
    compressed = compressor.compress(blob_in)
    return compressed

# Example usage:
if __name__ == "__main__":
    # Paths for demonstration
    original_csv = "sample.csv"
    compressed_file = "sample_compressed.zst"
    decompressed_csv = "sample_decompressed.csv"
    # Compress
    compress_csv_file_with_delta(original_csv, compressed_file)
    # Decompress
    decompress_csv_file_with_delta(compressed_file, decompressed_csv)

""" """
def main():
    if len(sys.argv) != 6:
        print(f"Usage: {sys.argv[0]} <db_path> <csv_path> <sensor_id> <is_keyframe> <timestamp>")
        print("Example: python store_csv_blob.py data.db readings.csv 101 1 1700000000")
        sys.exit(1)
    db_path = sys.argv[1]
    csv_path = sys.argv[2]
    sensor_id = int(sys.argv[3])
    is_keyframe = int(sys.argv[4])
    ts = int(sys.argv[5])
    try:
        # Compress CSV
        compressed_blob = compress_zlib(csv_path)
        # Initialize DB
        conn = init_sqlite_db(db_path)
        # Insert record
        insert_blob_sqlite(conn, ts, sensor_id, is_keyframe, compressed_blob)
        print(f"✅ CSV '{csv_path}' compressed and stored in '{db_path}' successfully.")

    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    main()