# data_processing.py
import csv
import os
import struct
import argparse
import numpy as np
from typing import Dict, Tuple, List, Optional
import queue
import threading
import logging

logger = logging.getLogger("openmotion.bloodflow-app.data_processing")


try:
    # Accelerated CRC implementation if available
    from omotion.utils import util_crc16 as _crc16
except ImportError:
    import binascii

    def _crc16(buf: memoryview) -> int:
        return binascii.crc_hqx(buf, 0xFFFF)


# ─── Constants ──────────────────────────────────────────────
HISTO_SIZE_WORDS = 1024
HISTO_BYTES = HISTO_SIZE_WORDS * 4
HISTO_BINS = np.arange(HISTO_SIZE_WORDS, dtype=np.float64)
PACKET_HEADER_SIZE = 6
PACKET_FOOTER_SIZE = 3
HISTO_BLOCK_SIZE = 1 + 1 + HISTO_BYTES + 4 + 1
TIMESTAMP_SIZE = 4
MIN_PACKET_SIZE = PACKET_HEADER_SIZE + PACKET_FOOTER_SIZE + HISTO_BLOCK_SIZE

SOF, SOH, EOH, EOF = 0xAA, 0xFF, 0xEE, 0xDD

# ─── Struct formats ─────────────────────────────────────────
_U32 = struct.Struct("<I")
_U16 = struct.Struct("<H")
_F32 = struct.Struct("<f")
_HDR = struct.Struct("<BBI")
_BLK_HEAD = struct.Struct("<BB")


def _get_u32(buf: memoryview, offset: int) -> int:
    return _U32.unpack_from(buf, offset)[0]


def _crc_matches(pkt: memoryview, crc_expected: int) -> bool:
    return _crc16(pkt) == crc_expected


class DataProcessor:
    """Parses raw histogram .bin files into CSV format."""

    def parse_histogram_packet(
        self, pkt: memoryview
    ) -> Tuple[
        Dict[int, np.ndarray], Dict[int, int], Dict[int, float], Optional[float], int
    ]:
        """Returns histograms, frame-ids, temperatures, optional timestamp (s), bytes_consumed."""

        if len(pkt) < MIN_PACKET_SIZE:
            raise ValueError("Packet too small")

        sof, pkt_type, pkt_len = _HDR.unpack_from(pkt, 0)
        if sof != SOF or pkt_type != 0x00:
            raise ValueError("Bad header")

        if pkt_len > len(pkt):
            raise ValueError("Truncated packet")

        payload_len = pkt_len - PACKET_HEADER_SIZE - PACKET_FOOTER_SIZE
        if payload_len < HISTO_BLOCK_SIZE:
            raise ValueError("Packet payload too small")

        has_timestamp = (payload_len % HISTO_BLOCK_SIZE) == TIMESTAMP_SIZE
        if not has_timestamp and (payload_len % HISTO_BLOCK_SIZE) != 0:
            raise ValueError("Packet length mismatch")

        payload_end = pkt_len - PACKET_FOOTER_SIZE
        off = PACKET_HEADER_SIZE

        hists: Dict[int, np.ndarray] = {}
        ids: Dict[int, int] = {}
        temps: Dict[int, float] = {}
        timestamp_sec: Optional[float] = None

        mv = pkt

        if has_timestamp:
            timestamp_ms = _U32.unpack_from(mv, off)[0]
            timestamp_sec = timestamp_ms / 1000.0
            off += TIMESTAMP_SIZE

        while off < payload_end:
            soh, cam_id = _BLK_HEAD.unpack_from(mv, off)
            if soh != SOH:
                raise ValueError("Missing SOH")
            off += _BLK_HEAD.size

            hist = np.frombuffer(
                mv, dtype=np.uint32, count=HISTO_SIZE_WORDS, offset=off
            )
            off += HISTO_BYTES

            temp = _F32.unpack_from(mv, off)[0]
            off += 4

            if mv[off] != EOH:
                raise ValueError("Missing EOH")
            off += 1

            last_word = hist[-1]
            frame_id = (last_word >> 24) & 0xFF
            hist = hist.copy()
            hist[-1] = last_word & 0x00_FFFF_FF

            hists[cam_id] = hist
            ids[cam_id] = frame_id
            temps[cam_id] = temp

        crc_expected = _U16.unpack_from(mv, off)[0]
        off += 2
        if mv[off] != EOF:
            raise ValueError("Missing EOF")

        if not _crc_matches(mv[: off - 3], crc_expected):
            raise ValueError("CRC mismatch")

        return hists, ids, temps, timestamp_sec, pkt_len

    def process_bin_file(
        self, src_bin: str, dst_csv: str, start_offset: int = 0, batch_rows: int = 4096
    ) -> None:
        """Convert binary → CSV."""
        with open(src_bin, "rb") as f:
            data = memoryview(f.read())

        total_bytes = len(data)
        off = start_offset
        packet_ok = packet_fail = crc_failure = other_fail = bad_header_fail = (
            error_count
        ) = 0
        bad_header_packets = []
        out_buf: List[List] = []

        with open(dst_csv, "w", newline="") as fcsv:
            wr = csv.writer(fcsv)
            wr.writerow(
                [
                    "cam_id",
                    "frame_id",
                    "timestamp_s",
                    *range(HISTO_SIZE_WORDS),
                    "temperature",
                    "sum",
                ]
            )

            while off + MIN_PACKET_SIZE <= len(data):
                try:
                    hists, ids, temps, timestamp_sec, consumed = (
                        self.parse_histogram_packet(data[off:])
                    )
                    off += consumed
                    packet_ok += 1

                    ts_val = timestamp_sec if timestamp_sec is not None else 0.0
                    for cam, hist in hists.items():
                        row_sum = int(hist.sum(dtype=np.uint64))
                        out_buf.append(
                            [cam, ids[cam], ts_val, *hist.tolist(), temps[cam], row_sum]
                        )

                    if len(out_buf) >= batch_rows:
                        wr.writerows(out_buf)
                        out_buf.clear()
                except Exception as exc:
                    error_count += 1
                    if exc.args and exc.args[0] == "CRC mismatch":
                        crc_failure += 1
                    elif exc.args and exc.args[0] == "Missing SOH":
                        packet_fail += 1
                    elif exc.args and exc.args[0] == "Bad header":
                        bad_header_fail += 1
                    else:
                        other_fail += 1

                    # Resync: search for next valid packet header
                    old_off = off
                    search_from = off + 1
                    found_sync = False
                    while search_from + MIN_PACKET_SIZE <= len(data):
                        nxt = data.obj.find(b"\xaa\x00", search_from)
                        if nxt == -1 or nxt + PACKET_HEADER_SIZE > len(data):
                            break
                        candidate_size = struct.unpack_from("<I", data, nxt + 2)[0]
                        if MIN_PACKET_SIZE <= candidate_size <= 32837:
                            off = nxt
                            bad_header_packets.append((old_off, off))
                            found_sync = True
                            break
                        search_from = nxt + 1
                    if found_sync:
                        continue
                    break

            if out_buf:
                wr.writerows(out_buf)

        total_packets = (
            packet_ok + packet_fail + crc_failure + other_fail + bad_header_fail
        )
        print(f"Parsed {total_packets} packets, {packet_ok} OK")

    def parse_stream_to_csv(
        self,
        q: queue.Queue,
        stop_evt: threading.Event,
        csv_writer,
        buffer_accumulator: bytearray,
        extra_cols_fn=None,
        on_row_fn=None,
    ):
        """
        Parse streaming binary data and write to CSV.
        This function is called to process data from the queue.
        Returns the number of rows written.
        """
        rows_written = 0
        pkts_parsed = 0
        parse_errors = 0
        pkts_received = 0

        while not stop_evt.is_set() or not q.empty():
            try:
                queue_size_before = q.qsize()
                data = q.get(timeout=0.100)
                if data:
                    buffer_accumulator.extend(data)
                    pkts_received += 1
                q.task_done()
            except queue.Empty:
                continue

            # Try to parse packets from the accumulated buffer
            offset = 0
            while offset + MIN_PACKET_SIZE <= len(buffer_accumulator):
                try:
                    pkt_view = memoryview(buffer_accumulator[offset:])
                    hists, ids, temps, timestamp_sec, consumed = (
                        self.parse_histogram_packet(pkt_view)
                    )
                    offset += consumed
                    pkts_parsed += 1
                    # Write CSV rows for each camera in this packet
                    ts_val = timestamp_sec if timestamp_sec is not None else 0.0
                    for cam_id, hist in hists.items():
                        row_sum = int(hist.sum(dtype=np.uint64))
                        extra_cols = extra_cols_fn() if extra_cols_fn else []
                        row = [
                            cam_id,
                            ids[cam_id],
                            ts_val,
                            *hist.tolist(),
                            temps[cam_id],
                            row_sum,
                            *extra_cols,
                        ]
                        csv_writer.writerow(row)
                        rows_written += 1
                        if on_row_fn:
                            on_row_fn(
                                cam_id,
                                ids[cam_id],
                                ts_val,
                                hist,
                                row_sum,
                                temps[cam_id],
                            )

                except ValueError as e:
                    parse_errors += 1
                    logger.warning(
                        f"[PARSER] error #{parse_errors} after {pkts_parsed} good pkts "
                        f"(buf={len(buffer_accumulator)}, offset={offset}): {e}"
                    )
                    # Try to resync: search for next valid packet header (SOF + TYPE_HISTO)
                    # and validate that the size field is plausible.
                    old_off = offset
                    search_from = offset + 1
                    found_sync = False
                    while search_from + MIN_PACKET_SIZE <= len(buffer_accumulator):
                        nxt = buffer_accumulator.find(b"\xaa\x00", search_from)
                        if nxt == -1 or nxt + PACKET_HEADER_SIZE > len(buffer_accumulator):
                            break
                        # Validate size field is plausible
                        candidate_size = struct.unpack_from("<I", buffer_accumulator, nxt + 2)[0]
                        if MIN_PACKET_SIZE <= candidate_size <= 32837:
                            offset = nxt
                            found_sync = True
                            logger.warning(
                                f"Parser error at offset {old_off}, resynced to {nxt} "
                                f"(skipped {nxt - old_off} bytes): {e}"
                            )
                            break
                        search_from = nxt + 1
                    if found_sync:
                        continue
                    else:
                        # Can't find next packet, wait for more data
                        break

            # Remove processed data from buffer
            if offset > 0:
                del buffer_accumulator[:offset]

        if pkts_received > 0:
            logger.info(
                f"[PARSER] Stream done: {pkts_received} pkts received, "
                f"{pkts_parsed} parsed OK, {parse_errors} errors, "
                f"{rows_written} CSV rows, "
                f"residual buffer={len(buffer_accumulator)} bytes"
            )

        return rows_written


def main():
    parser = argparse.ArgumentParser(description="Process histogram .bin to .csv")
    parser.add_argument("--file", "-f", required=True, help="Input .bin file")
    parser.add_argument("--output", "-o", help="Output CSV file (optional)")
    args = parser.parse_args()

    input_file = args.file
    output_file = (
        args.output if args.output else os.path.splitext(input_file)[0] + ".csv"
    )

    processor = DataProcessor()
    processor.process_bin_file(input_file, output_file)


if __name__ == "__main__":
    main()
