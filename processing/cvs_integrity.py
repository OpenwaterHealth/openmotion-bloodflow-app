# processing/csv_integrity.py
import argparse
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import pandas as pd


@dataclass
class CheckConfig:
    expected_sum: int = 2_457_606  # your default
    max_frame_id: int = 255  # rollover at 255 -> 0


@dataclass
class CheckResult:
    passed: bool
    error_counts: Dict[str, int]
    cam_hist_counts: Dict[int, int]
    skipped_percentage: float
    details: Dict[str, object]


class CSVIntegrityChecker:
    def __init__(self, config: Optional[CheckConfig] = None):
        self.cfg = config or CheckConfig()

    def check(self, csv_path: str) -> CheckResult:
        df = pd.read_csv(csv_path)

        # Normalize dtypes
        for col in ("frame_id", "cam_id", "sum"):
            if col in df.columns:
                df[col] = df[col].astype(int)
            else:
                raise ValueError(f"Missing required column '{col}' in CSV")

        errors_found = False
        error_counts = {
            "bad_sum": 0,
            "frame_id_skipped": 0,
            "bad_frame_cam_count": 0,
        }
        expected_fids: List[Tuple[str, str]] = []

        # --- Derive frame_cycle based on rollover detection ---
        frame_cycles = []
        last_frame_id = None
        cycle = 0
        for fid in df["frame_id"]:
            if last_frame_id is not None and fid < last_frame_id:
                cycle += 1
            frame_cycles.append(cycle)
            last_frame_id = fid
        df["frame_cycle"] = frame_cycles

        # --- 1) Check sums ---
        bad_sums = df[df["sum"] != self.cfg.expected_sum]
        if not bad_sums.empty:
            error_counts["bad_sum"] = len(bad_sums)
            errors_found = True

        # --- 2) Verify frame_id sequencing + 3) cam count per frame ---
        grouped = df.groupby(["frame_cycle", "frame_id"], sort=True)
        expected_fid = None
        expected_cam_count = None

        for (cycle, fid), group in grouped:
            row_idx = int(group.index.min())
            cam_count = int(len(group))

            if expected_cam_count is None:
                expected_cam_count = cam_count  # learn once
            elif cam_count != expected_cam_count:
                error_counts["bad_frame_cam_count"] += 1
                errors_found = True

            if expected_fid is None:
                expected_fid = fid
            elif fid != expected_fid:
                # how many IDs were skipped, modulo rollover
                num_skipped = (fid - expected_fid) % (self.cfg.max_frame_id + 1)
                if num_skipped:
                    error_counts["frame_id_skipped"] += num_skipped
                    expected_fids.append((str(cycle), str(expected_fid)))
                    errors_found = True
                expected_fid = fid

            expected_fid = (expected_fid + 1) % (self.cfg.max_frame_id + 1)

        # Summary stats
        cam_counts = df["cam_id"].value_counts().sort_index().to_dict()

        skipped_percentage = 0.0
        # Use cam_id 0 if present as baseline; otherwise average counts
        denom = None
        if 0 in cam_counts:
            denom = cam_counts[0] + error_counts["frame_id_skipped"]
        elif cam_counts:
            avg = sum(cam_counts.values()) / len(cam_counts)
            denom = avg + error_counts["frame_id_skipped"]
        if denom and denom > 0:
            skipped_percentage = (error_counts["frame_id_skipped"] / denom) * 100.0

        return CheckResult(
            passed=not errors_found,
            error_counts=error_counts,
            cam_hist_counts={int(k): int(v) for k, v in cam_counts.items()},
            skipped_percentage=skipped_percentage,
            details={
                "expected_cam_count": expected_cam_count
                if expected_cam_count is not None
                else 0,
                "bad_sum_rows": int(error_counts["bad_sum"]),
                "skipped_expected_fids": expected_fids,  # optional list of (cycle, expected_fid)
            },
        )


# ---------------- CLI ----------------
def _parse_args():
    p = argparse.ArgumentParser(description="Check histogram CSV integrity")
    p.add_argument("--csv", required=True, help="Path to input CSV file")
    p.add_argument(
        "--expected-sum",
        type=int,
        default=CheckConfig.expected_sum,
        help=f"Expected histogram sum per row (default: {CheckConfig.expected_sum})",
    )
    p.add_argument(
        "--max-frame-id",
        type=int,
        default=CheckConfig.max_frame_id,
        help="Max frame id before rollover (default: 255)",
    )
    return p.parse_args()


def main():
    args = _parse_args()
    cfg = CheckConfig(expected_sum=args.expected_sum, max_frame_id=args.max_frame_id)
    checker = CSVIntegrityChecker(cfg)
    res = checker.check(args.csv)

    # Pretty print result (kept close to your original output style)
    print("\n[INFO] Histogram count per cam_id:")
    for cam, cnt in sorted(res.cam_hist_counts.items()):
        print(f"  cam {cam}: {cnt}")

    print("\n[INFO] Error type counts:")
    for k, v in res.error_counts.items():
        print(f"  {k}: {v}")

    if res.skipped_percentage:
        print(
            f"\n[INFO] Percentage of skipped frame_ids: {res.skipped_percentage:.2f}%"
        )

    print(
        "\n[PASS] CSV passed all integrity checks."
        if res.passed
        else "\n[FAIL] One or more checks failed."
    )


if __name__ == "__main__":
    main()
