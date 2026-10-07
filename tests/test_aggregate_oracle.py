"""
QA oracle for the research plot's Aggregate view (issue #621) — unit,
no app launch, no hardware.

Aggregate folds each sensor module's 8 cameras into 4 plots by averaging
the mirrored camera pairs 1+8, 2+7, 3+6, 4+5. Per the issue, each sample
is the NaN-aware mean of the pair's cameras for that capture: an unlit
camera drops out of the mean, a capture where both are unlit is a gap,
and a pair with only one enabled camera shows that camera. It is display
only — scans.db and the History CSV export are unchanged, which is what
makes the export usable as ground truth.

This oracle is QA-owned and written straight from the #621 spec; it does
not import the app's derivation. ``test_aggregate_view.py`` (HIL) imports
it to check what the app displays. On its own it:

  1. checks the pair rule on synthetic samples,
  2. loads the latest History CSV export (or AGGREGATE_EXPORT_CSV),
  3. asserts every exported capture obeys the invariants (mean bounded
     by its two cameras, one-camera captures pass that camera through,
     both-unlit captures are gaps), and
  4. writes the expected pair table to test_logs/<export>_aggregate_expected.csv
     for manual spot checks against the plot's value labels.

Run:
    pytest test_aggregate_oracle.py
"""

from __future__ import annotations

import csv
import math
import os
from pathlib import Path

import pytest

from conftest import log

pytestmark = pytest.mark.unit

# ─────────────────────────────────────────────
# Spec constants (issue #621)
# ─────────────────────────────────────────────
PAIRS: tuple[tuple[int, int], ...] = ((1, 8), (2, 7), (3, 6), (4, 5))
SIDES = ("LEFT", "RIGHT")
METRICS = ("bfi", "bvi")

# Test log folder (same one pytest.ini's junit/log paths use).
TEST_LOGS = Path(__file__).resolve().parent / "test_logs"

STATE: dict = {}


def finite(v) -> bool:
    return isinstance(v, float) and math.isfinite(v)


def expected_pair_value(a: float, b: float) -> float:
    """NaN-aware mean of one capture's two mirrored cameras."""
    fa, fb = finite(a), finite(b)
    if fa and fb:
        return (a + b) / 2.0
    if fa:
        return a
    if fb:
        return b
    return math.nan


def _cell(row: dict, key: str) -> float:
    raw = (row.get(key) or "").strip()
    if not raw:
        return math.nan
    try:
        return float(raw)
    except ValueError:
        return math.nan


def find_export_csv(session_label: str | None = None) -> Path | None:
    """The History CSV export to check against. AGGREGATE_EXPORT_CSV wins;
    otherwise the newest ``*_export.csv`` in an installed build's data
    folder (preferring one whose name carries ``session_label``; None if
    a label is given and no export matches it).

    Installed builds live at Documents\\OpenMotion\\Open-Motion-<ver>\\
    Open-Motion\\data. The glob is fixed-depth on purpose: the OpenMotion
    folder also holds years of logs and scan DBs, and a recursive glob
    over it takes minutes."""
    pinned = os.environ.get("AGGREGATE_EXPORT_CSV")
    if pinned:
        p = Path(pinned)
        return p if p.is_file() else None
    roots = [Path.home() / "Documents" / "OpenMotion",
             Path.home() / "Documents" / "Open-Motion"]
    found: list[Path] = []
    for root in roots:
        if root.is_dir():
            found.extend(root.glob("*/Open-Motion/data/*_export.csv"))
            found.extend(root.glob("*/data/*_export.csv"))
    if not found:
        return None
    if session_label:
        tagged = [p for p in found if session_label in p.name]
        return max(tagged, key=lambda p: p.stat().st_mtime) if tagged else None
    return max(found, key=lambda p: p.stat().st_mtime)


def load_export(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "bfi_l1" not in rows[0]:
        raise AssertionError(
            f"{path.name} is not a per-camera scan export (no bfi_l1 column)"
        )
    return rows


def sides_with_data(rows: list[dict]) -> list[str]:
    """Sides ("LEFT"/"RIGHT") with at least one finite BFI sample."""
    sides = []
    for side in SIDES:
        s = side[0].lower()
        if any(finite(_cell(r, f"bfi_{s}{i}")) for r in rows for i in range(1, 9)):
            sides.append(side)
    return sides


def expected_pair_table(rows: list[dict]) -> list[dict]:
    """One output row per capture: frame_id, timestamp_s and, for every
    side/pair/metric, the two camera values and the expected pair value
    under ``exp_<metric>_<s><a>+<b>`` (e.g. ``exp_bfi_l1+8``)."""
    out = []
    for row in rows:
        rec = {"frame_id": row.get("frame_id"), "timestamp_s": row.get("timestamp_s")}
        for side in SIDES:
            s = side[0].lower()
            for a, b in PAIRS:
                for m in METRICS:
                    va, vb = _cell(row, f"{m}_{s}{a}"), _cell(row, f"{m}_{s}{b}")
                    rec[f"{m}_{s}{a}"] = va
                    rec[f"{m}_{s}{b}"] = vb
                    rec[f"exp_{m}_{s}{a}+{b}"] = expected_pair_value(va, vb)
        out.append(rec)
    return out


def write_expected_table(table: list[dict], out: Path) -> None:
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(table[0].keys()))
        w.writeheader()
        for rec in table:
            w.writerow({k: ("" if isinstance(v, float) and math.isnan(v) else v)
                        for k, v in rec.items()})


# ─────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────
class TestAggregateOracle:
    """Spec oracle + invariants on the latest History CSV export."""

    def test_01_pair_rule_on_synthetic_samples(self):
        """Both lit → mean; one unlit → the lit camera; both unlit → gap."""
        assert expected_pair_value(2.0, 4.0) == pytest.approx(3.0)
        assert expected_pair_value(-0.2, 0.1) == pytest.approx(-0.05)
        assert expected_pair_value(math.nan, 6.0) == pytest.approx(6.0)
        assert expected_pair_value(6.0, math.nan) == pytest.approx(6.0)
        assert math.isnan(expected_pair_value(math.nan, math.nan))
        assert PAIRS == ((1, 8), (2, 7), (3, 6), (4, 5))

    def test_02_export_csv_available(self):
        path = find_export_csv()
        if path is None:
            pytest.skip("No History CSV export found; export a research "
                        "scan from History or set AGGREGATE_EXPORT_CSV.")
        rows = load_export(path)
        STATE["csv"] = path
        STATE["rows"] = rows
        STATE["sides"] = sides_with_data(rows)
        log.info(f"  oracle export: {path} ({len(rows)} captures, "
                 f"sides with data: {STATE['sides']})")
        assert rows, f"{path.name} has no captures"
        assert STATE["sides"], f"{path.name} has no finite BFI on either side"

    def test_03_invariants_hold_on_every_capture(self):
        """Every capture obeys the #621 rule for every pair; the expected
        table is written to test_logs for spot checks against the plot."""
        rows = STATE.get("rows")
        if rows is None:
            pytest.skip("no export loaded")
        table = expected_pair_table(rows)
        counts = {"both": 0, "one": 0, "gap": 0}
        for rec in table:
            for side in STATE["sides"]:
                s = side[0].lower()
                for a, b in PAIRS:
                    for m in METRICS:
                        va, vb = rec[f"{m}_{s}{a}"], rec[f"{m}_{s}{b}"]
                        ev = rec[f"exp_{m}_{s}{a}+{b}"]
                        fa, fb = finite(va), finite(vb)
                        tag = f"frame {rec['frame_id']} {side} {a}+{b} {m}"
                        if fa and fb:
                            counts["both"] += 1
                            assert min(va, vb) - 1e-9 <= ev <= max(va, vb) + 1e-9, \
                                f"{tag}: mean {ev} outside [{va}, {vb}]"
                        elif fa or fb:
                            counts["one"] += 1
                            assert ev == (va if fa else vb), \
                                f"{tag}: one-camera capture should pass through"
                        else:
                            counts["gap"] += 1
                            assert math.isnan(ev), f"{tag}: both unlit must be a gap"
        assert counts["both"] > 0, "export has no capture with both pair cameras lit"
        log.info(f"  captures checked — both lit: {counts['both']}, "
                 f"one camera: {counts['one']}, gaps: {counts['gap']}")
        out = TEST_LOGS / f"{STATE['csv'].stem}_aggregate_expected.csv"
        write_expected_table(table, out)
        log.info(f"  expected pair table -> {out}")

    def test_04_app_derivation_matches_oracle(self):
        """The app's own pair derivation, fed the same export, agrees with
        this oracle on every capture.

        The plot's value labels are plain QML Text and never reach UI
        Automation on the 1.5.x builds, so the numbers on screen can't be
        read by the HIL test. What the cells show is value_at() over the
        buffers this derivation produces, so checking the derivation
        against an independent oracle is the closest automated check of
        the displayed numbers. System under test: ``data_sources`` from
        the app checkout at OPENMOTION_APP_SRC (default: the ``next``
        clone beside this repo). Skips if that module doesn't import."""
        rows = STATE.get("rows")
        if rows is None:
            pytest.skip("no export loaded")
        import sys
        src = Path(os.environ.get(
            "OPENMOTION_APP_SRC",
            str(Path(__file__).resolve().parents[3] / "openmotion-bloodflow-app-next")))
        if not (src / "data_sources.py").is_file():
            pytest.skip(f"app source not found at {src}; set OPENMOTION_APP_SRC")
        sys.path.insert(0, str(src))
        try:
            import data_sources as ds
        except Exception as e:  # pragma: no cover - environment dependent
            pytest.skip(f"data_sources import failed from {src}: {e}")
        finally:
            sys.path.remove(str(src))
        assert tuple((a - 1, b - 1) for a, b in PAIRS) == tuple(ds.AGGREGATE_PAIRS), (
            f"app pairs {ds.AGGREGATE_PAIRS} (0-based) differ from spec {PAIRS}"
        )
        buffers = ds.load_csv_scan_buffers(str(STATE["csv"]), derive_side_average=True)
        assert buffers, "app loader returned no buffers for the export"
        expected = {rec["frame_id"]: rec for rec in expected_pair_table(rows)}
        compared = 0
        for side in STATE["sides"]:
            s = side[0].lower()
            for p, (a, b) in enumerate(PAIRS):
                for m in METRICS:
                    buf = buffers.get((side.lower(), ds.AGGREGATE_CAM_BASE + p, m))
                    if buf is None:
                        # Legit only when neither camera recorded anything.
                        have = any(finite(rec[f"{m}_{s}{a}"]) or finite(rec[f"{m}_{s}{b}"])
                                   for rec in expected.values())
                        assert not have, f"{side} {a}+{b} {m}: app derived no pair buffer"
                        continue
                    n = buf.n
                    fids, vals = buf.frame_id[:n], buf.v[:n]
                    for fid, v in zip(fids, vals):
                        rec = expected.get(str(int(fid)))
                        assert rec is not None, f"{side} {a}+{b} {m}: app frame {fid} not in export"
                        ev = rec[f"exp_{m}_{s}{a}+{b}"]
                        v = float(v)
                        if not finite(ev):
                            assert not math.isfinite(v), \
                                f"{side} {a}+{b} {m} frame {fid}: app {v}, oracle gap"
                        else:
                            # buffers store float32
                            assert math.isfinite(v) and abs(v - ev) <= 1e-5 * max(1.0, abs(ev)), \
                                f"{side} {a}+{b} {m} frame {fid}: app {v}, oracle {ev}"
                        compared += 1
        assert compared > 0, "nothing compared"
        log.info(f"  app derivation vs oracle: {compared} samples agree "
                 f"(source: {src})")
