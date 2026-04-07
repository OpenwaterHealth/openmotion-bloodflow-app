from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import os
import numpy as np  
import pandas as pd
import numpy as np
import numpy.typing as npt
from typing import NamedTuple
import scipy
from scipy.signal import medfilt

from api.session_samples import Sample
from api.session_samples import SessionSamples

import compute.profile_hist as profile_hist
from compute.profile_hist import ExecutionTimer

""" """
class ValueAvailable():
    def __init__(self, available=False, value=None):
        self.available = available
        self.value=value
    def set(self, value):
        self.value = value
        self.available = True
""" """
class BFValue(NamedTuple):
    side:      str            # 'left' or 'right'
    cam_id:    np.uint32
    frame_id:  np.uint32
    timestamp: np.float32
    temp:      np.float32
    summ:      np.uint64
    mean:      np.float32
    contrast:  np.float32
    bfi:       np.float32
    bvi:       np.float32
""" """
class CamCalib():
    def __init__(self, cmin=None, cmax=None, imin=None, imax=None):
        self.cmin = cmin # npt.NDArray[np.float32]
        self.cmax = cmax # npt.NDArray[np.float32]
        self.imin = imin # npt.NDArray[np.float32]
        self.imax = imax# npt.NDArray[np.float32]
""" """
class Estimator():
    def __init__(self, ncams):
        self.buf=[]
        self.ncams=ncams
    def add(self,v)->ValueAvailable:
        ret = ValueAvailable()
        self.buf.append(v)
        if len(self.buf) >= self.ncams:
            ret.available = True
            ret.value = np.mean(self.buf) 
            self.buf = []
        return ret
""" """
class BFComputer():
    def __init__(self, ncams, calib=None):
        self.ncams = ncams
        if calib is not None:
            self.calib = calib
        else:
            self.calib = CamCalib()
        self.bf_estimator = Estimator(self.ncams)
        self.bv_estimator = Estimator(self.ncams)
        pass
    """ """
    def estimate_bf_from_all_cameras(self, bf,bv)->ValueAvailable:
        ret = ValueAvailable()
        bf_ret = self.bf_estimator.add(bf)
        bv_ret = self.bv_estimator.add(bv)
        if bf_ret.available & bv_ret.available:
            ret.set([bf,bv])
        pass
        return ret
    """ """
    def compute(self, sample) -> ValueAvailable:
        """
        Computation for one sample.
        """
        ret = ValueAvailable()
        bin_nums = np.arange(1024, dtype=np.float32)
        bin_nums2 = bin_nums * bin_nums
        if sample.summ > 0:
            mean_val = float(np.dot(sample.hist, bin_nums) / sample.summ)
        else:
            mean_val = 0.0
        if sample.summ > 0 and mean_val > 0:
            mean2 = float(np.dot(sample.hist, bin_nums2) / sample.summ)
            var = max(0.0, mean2 - (mean_val * mean_val))
            std = np.sqrt(var)
            contrast = float(std / mean_val) if mean_val > 0 else 0.0
        else:
            contrast = 0.0
        #!!!
        bf_val = contrast * 10.0
        bv_val = mean_val * 10.0
        #
        if False:
            module_idx = 0 if side == "left" else 1
            cam_pos = int(sample.cam_id) % 8
            if module_idx >= bfi_c_min.shape[0] or cam_pos >= bfi_c_min.shape[1]:
                bfi_val = contrast * 10.0
            else:
                cmin = float(bfi_c_min[module_idx, cam_pos])
                cmax = float(bfi_c_max[module_idx, cam_pos])
                cden = (cmax - cmin) or 1.0
                bfi_val = (1.0 - ((contrast - cmin) / cden)) * 10.0
            if module_idx >= bfi_i_min.shape[0] or cam_pos >= bfi_i_min.shape[1]:
                bvi_val = mean_val * 10.0
            else:
                imin = float(bfi_i_min[module_idx, cam_pos])
                imax = float(bfi_i_max[module_idx, cam_pos])
                iden = (imax - imin) or 1.0
                bvi_val = (1.0 - ((mean_val - imin) / iden)) * 10.0
            timestamp = float(timestamp_s) if timestamp_s else time.time()
        #
        ev = self.estimate_bf_from_all_cameras(bf_val,bv_val)
        if ev.available:
            ret_val = BFValue(
            side=sample.side,
            cam_id=sample.cam_id,
            frame_id=sample.frame_id,
            timestamp=sample.timestamp,
            summ=sample.summ,
            temp=sample.temp,
            mean=mean_val,
            contrast=contrast,
            bfi=bf_val,
            bvi=bv_val,
            )
            ret.set(ret_val)
        return ret
""" """
def main():
    csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_125021_left_mask99.csv"
    sd = SessionSamples();
    n = sd.read_csv(csv_path)
    c = CamCalib()
    comp = BFComputer(sd.ncams)
    print("Compute=====================")
    extimer = ExecutionTimer()
    ncycles = 100
    print_distribution = True
    try:
        for i in range(n):
            s = sd.get(i)
            for i in range(ncycles):
                r = extimer.time_function(comp.compute, s)
                pass
        extimer.hist.save_out("../../../tmp/bf_compute.txt", print_distribution)
    except Exception as e:
        print(f"❌ Error: {e}")
    pass

""" """
if __name__ == "__main__":
    if True:
        main()
    else:
        buffer = io.StringIO()
        # Redirect stdout to the buffer
        with redirect_stdout(buffer):
            main_profile()
        captured_output = buffer.getvalue()
        print(captured_output)
        with open("../../../tmp/profile.txt", "w") as f:
            f.write(captured_output)
    pass