from dataclasses import dataclass, field
from typing import Optional, Tuple, List
import os
import numpy as np  
import pandas as pd
import numpy as np
import numpy.typing as npt
from typing import NamedTuple

""" """
class Sample(NamedTuple):
    side: np.uint32
    cam_id: np.uint32
    frame_id: np.uint32
    timestamp: np.float32
    hist: npt.NDArray[np.uint32]
    temp: np.float32
    summ:  np.uint64
""" """
class SessionSamples():
    def __init__(self):
        self.rows = None
        self.ncams = None
        pass
    """ """
    def read_csv(self, csv_path: str, side = 0) :
        #2592 rows of 1032 bytes in a row
        #cam_id, frame_id, timestamp_s, 0,...1023, temperature, sum, tcm, tcl, pdc
        self.side = side
        self.rows = np.array(pd.read_csv(csv_path, dtype=np.float32))
        camera = self.rows[:, 0]
        camera_inds = np.unique(camera)
        self.ncams = len(camera_inds)
        return self.size()
    def size(self):
        return self.rows.shape[0]
    """ """
    def get(self, i)-> Sample:
        cid = np.uint32(self.rows[i, 0])
        fid = np.uint32(self.rows[i, 1])
        ts = self.rows[i, 2]
        h = np.uint32(self.rows[i, 3:1027])
        t = self.rows[i,1027]
        sm = np.uint64(self.rows[i,1028])
        #tcm = rows[i,1030]
        #tcl = rows[i,1031]
        #pdc = rows[i,1032]
        sam = Sample(self.side, cid,fid,ts,h,t,sm)
        return sam
    """ """
def main():
    csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_125021_left_mask99.csv"
    sd = SessionSamples();
    n = sd.read_csv(csv_path)
    for i in range(n):
        s = sd.get(i)
        pass
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
