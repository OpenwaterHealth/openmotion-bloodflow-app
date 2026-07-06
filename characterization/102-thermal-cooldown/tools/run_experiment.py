"""Run one drift experiment from a manifest JSON.

Manifest schema (experiments/NNN_name/manifest.json):
{
  "name": "010_baseline",
  "duration_sec": 1200,
  "left_camera_mask": 255, "right_camera_mask": 255,
  "register_overrides": {            # optional; applied AFTER connect+configure
     "left":  { "0": [[16385, 42]], ... }   # cam -> [[addr, val], ...] (ints)
  },
  "notes": "why this experiment exists"
}

Flow: connect -> (optional configure) -> apply overrides (verified) ->
register dump per camera (pre) -> scan via run_collection_scan with a
DriftCollector -> register dump (post) -> save means CSV + dumps + result.json.

Usage:
    python run_experiment.py <manifest.json> [--configure] [--no-laser]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import owcam  # noqa: E402
from drift_collector import DriftCollector  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("campaign.run")


def mask_cams(mask: int) -> list[int]:
    return [i for i in range(8) if mask & (1 << i)]


def do_configure(iface, left_mask: int, right_mask: int, timeout_s: float = 300.0):
    """Run the SDK configure workflow (camera power + FPGA program + register
    table). Blocks until complete."""
    from omotion.ScanWorkflow import ConfigureRequest
    wf = get_workflow(iface)
    done: list = []

    def on_complete(res):
        done.append(res)

    started = wf.start_configure_camera_sensors(
        ConfigureRequest(left_camera_mask=left_mask, right_camera_mask=right_mask),
        on_log_fn=lambda m: logger.info("configure: %s", m),
        on_complete_fn=on_complete,
    )
    if not started:
        raise RuntimeError("configure refused (already running?)")
    t0 = time.time()
    while not done and time.time() - t0 < timeout_s:
        time.sleep(0.5)
    if not done:
        raise TimeoutError("camera configure timed out")
    res = done[0]
    if hasattr(res, "ok") and not res.ok:
        raise RuntimeError(f"configure failed: {res.error}")
    logger.info("configure complete")


def get_workflow(iface):
    from omotion.ScanWorkflow import ScanWorkflow
    wf = getattr(iface, "_scan_workflow", None)
    if wf is None:
        wf = ScanWorkflow(iface)
        iface._scan_workflow = wf
    return wf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--configure", action="store_true",
                    help="run FPGA program + camera register configure first")
    ap.add_argument("--no-laser", action="store_true")
    ap.add_argument("--skip-overrides", action="store_true")
    args = ap.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as fh:
        man = json.load(fh)
    exp_dir = os.path.dirname(os.path.abspath(args.manifest))
    name = man["name"]
    lmask = int(man.get("left_camera_mask", 0xFF))
    rmask = int(man.get("right_camera_mask", 0xFF))

    result: dict = {"name": name, "started_utc": time.time(), "ok": False}

    iface = owcam.connect()
    try:
        # Quiesce: clear any orphaned scan state from a previously killed run
        # (trigger running, sensors still capturing). Mirrors the scan
        # teardown order: stop_trigger -> disable_camera -> fsin off.
        try:
            iface.console.stop_trigger()
        except Exception:
            logger.warning("initial stop_trigger failed (continuing)")
        time.sleep(0.5)
        for side in ("left", "right"):
            try:
                sensor = owcam.sensor_for(iface, side)
                sensor.disable_camera(0xFF)
                sensor.disable_camera_fsin_ext()
            except Exception:
                logger.warning("quiesce %s failed (continuing)", side)
        time.sleep(0.5)

        if args.configure:
            do_configure(iface, lmask, rmask)

        # Laser baseline load — identical to app connect path.
        if not args.no_laser and not man.get("disable_laser", False):
            ok = iface.apply_laser_power()
            result["apply_laser_power"] = bool(ok)
            logger.info("apply_laser_power -> %s", ok)

        # Baseline-restore preamble on every active camera. VERIFIED FINDING
        # (test_configure_reset.py, 2026-07-02): OW_CAMERA_SET_CONFIG skips
        # re-init on cameras it considers already configured, so runtime
        # register changes silently survive "reconfigure" until a camera
        # power-cycle. Every register this campaign ever touches is therefore
        # explicitly restored to the deployed value here, then manifest
        # overrides apply on top.
        GAIN_LADDER = {0: 0x10, 1: 0x04, 2: 0x02, 3: 0x01,
                       4: 0x01, 5: 0x02, 6: 0x04, 7: 0x10}
        pre_mism: list[str] = []
        for side, mask in (("left", lmask), ("right", rmask)):
            sensor = owcam.sensor_for(iface, side)
            for cam in mask_cams(mask):
                preamble = [
                    (0x5100, 0x00), (0x5000, 0x34),          # test pattern off
                    (0x3501, 0x00), (0x3502, 0x48),          # exposure 72 rows (648 us, stock since PR #72)
                    (0x3508, GAIN_LADDER[cam]), (0x3509, 0x00),  # gain ladder
                    (0x4000, 0xF9), (0x4001, 0x23),          # BLC policy
                    (0x4032, 0x3E),                          # kcoef manual en
                    (0x4034, 0x00), (0x4035, 0x49),          # kcoef = 0x049
                    (0x4036, 0x00), (0x4037, 0x49),
                    (0x4038, 0x00), (0x4039, 0x49),
                    (0x403A, 0x00), (0x403B, 0x49),
                ]
                try:
                    pre_mism.extend(owcam.apply_writes(sensor, cam, preamble))
                except IOError as e:
                    logger.warning("preamble %s%d failed: %s", side, cam, e)
        result["preamble_mismatches"] = pre_mism

        # Apply register overrides (verified).
        overrides = {} if args.skip_overrides else man.get("register_overrides", {})
        mism_all: list[str] = []
        for side, cams in overrides.items():
            sensor = owcam.sensor_for(iface, side)
            for cam_str, writes in cams.items():
                cam = int(cam_str)
                writes = [(int(a), int(v)) for a, v in writes]
                mism = owcam.apply_writes(sensor, cam, writes)
                mism_all.extend(mism)
        result["override_mismatches"] = mism_all
        if mism_all:
            logger.warning("override mismatches: %s", mism_all)

        # Pre-scan register dumps + temps. A thermally-latched camera is
        # I2C-unreachable — skip it rather than abort the experiment.
        dumps_pre = {}
        unreachable: list[str] = []
        for side, mask in (("left", lmask), ("right", rmask)):
            sensor = owcam.sensor_for(iface, side)
            for cam in mask_cams(mask):
                try:
                    d = owcam.dump_registers(sensor, cam)
                except IOError as e:
                    logger.warning("pre-dump %s%d unreachable: %s", side, cam, e)
                    unreachable.append(f"{side}{cam}")
                    continue
                dumps_pre[f"{side}{cam}"] = d
                with open(os.path.join(exp_dir, f"regdump_pre_{side}{cam}.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(owcam.dump_to_text(d))
        result["unreachable_pre"] = unreachable

        # Scan. Built directly (not run_collection_scan) so the manifest can
        # override trigger_config — e.g. {"TriggerStatus": 1} = fsync on,
        # laser off, which is the correct "dark scan" (disable_laser=True
        # skips enable_camera_fsin_ext and yields zero frames).
        from omotion.ScanWorkflow import ScanRequest
        wf = get_workflow(iface)
        collector = DriftCollector(
            spill_path=os.path.join(exp_dir, "means.partial.csv"))
        duration = int(man["duration_sec"])
        request = ScanRequest(
            subject_id=name,
            duration_sec=duration,
            left_camera_mask=lmask,
            right_camera_mask=rmask,
            disable_laser=bool(man.get("disable_laser", False)),
            trigger_config=man.get("trigger_config"),
            sinks=[collector],
            skip_default_storage=True,
        )
        t0 = time.time()
        if not wf.start_scan(request):
            raise RuntimeError("ScanWorkflow refused start_scan.")
        wf.await_complete(timeout_sec=duration + 30)
        if wf.last_scan_error:
            raise RuntimeError(f"scan failed: {wf.last_scan_error}")
        ok = not wf.last_scan_canceled
        result["scan_ok"] = bool(ok)
        result["scan_wall_s"] = time.time() - t0

        # Save collected means.
        means_path = os.path.join(exp_dir, "means.csv")
        n = collector.save(means_path)
        result["rows"] = n
        logger.info("saved %d rows -> %s", n, means_path)

        # Post-scan dumps + diffs (tolerate cameras that died mid-scan).
        reg_drift: dict[str, list[str]] = {}
        for side, mask in (("left", lmask), ("right", rmask)):
            sensor = owcam.sensor_for(iface, side)
            for cam in mask_cams(mask):
                key = f"{side}{cam}"
                try:
                    d = owcam.dump_registers(sensor, cam)
                except IOError as e:
                    logger.warning("post-dump %s unreachable: %s", key, e)
                    reg_drift[key] = ["UNREACHABLE post-scan"]
                    continue
                with open(os.path.join(exp_dir, f"regdump_post_{side}{cam}.txt"),
                          "w", encoding="utf-8") as fh:
                    fh.write(owcam.dump_to_text(d))
                if key in dumps_pre:
                    diffs = owcam.diff_dumps(dumps_pre[key], d)
                    if diffs:
                        reg_drift[key] = diffs
        result["register_changes_during_scan"] = reg_drift
        result["ok"] = bool(ok) and n > 0
    finally:
        result["finished_utc"] = time.time()
        with open(os.path.join(exp_dir, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        try:
            iface.stop()
        except Exception:
            pass

    print(json.dumps({k: v for k, v in result.items()
                      if k != "register_changes_during_scan"}, indent=2))
    print("register_changes_during_scan:",
          json.dumps(result["register_changes_during_scan"], indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
