import sys
import time
import random
import numpy as np
from queue import Queue, Empty
from threading import Thread, Event
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore
from PyQt6 import QtWidgets
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton
from PyQt6.QtCore import QTimer

import multiprocessing as mp

import bfplot
from bfplot import BFPlot
from session_samples import Sample
from session_samples import SessionSamples

from bfcompute import CamCalib
from bfcompute import BFComputer
from bfcompute import BFValue
from bfcompute import ValueAvailable

indicate = True#enable console output indicating queue len

# Sentinel to signal thread completion
SENTINEL = None

""" """
class ProducerBase(Thread):
    def __init__(self, period_s, stop_event, q_max_size = 10000):
        super().__init__()
        self.period_s = period_s
        self.produced_event = Event()
        self.produced_event.clear()
        self.stop_event = stop_event
        self.q_max_size = q_max_size
        self.out_q = Queue(maxsize=self.q_max_size); 
"""Source of data stream mockup"""
class DataProducerMockup(ProducerBase):
    def __init__(self, count, period_s, stop_event, in_csv_path=None):
        super().__init__(period_s, stop_event)
        self.csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_125021_left_mask99.csv"
        self.sd = SessionSamples();
        self.count = self.sd.read_csv(self.csv_path)
        self.id = 0
    """Override"""
    def run(self):
        for i in range(self.count):
            if self.stop_event.is_set():
                break
            if False:
                # Simulate data generation
                v1 = np.float32(np.random.rand())
                v2 = np.float32(np.random.rand())
                v3 = np.float32(np.random.rand())
                v4 = np.float32(np.random.rand())
                data = {"id": i, "value1": v1, "value2": v2, "value3": v3, "value4": v4, "timestamp": time.time()}
            else:
                data = self.sd.get(self.id)
                self.id += 1
                self.id = self.id if self.id < self.count else 0
            self.out_q.put(data)
            self.produced_event.set()
            if indicate: print(f"* {self.out_q.qsize()}")
            time.sleep(self.period_s)
            QApplication.processEvents()
            pass
        self.out_q.put(SENTINEL) # Signal end
"""DataProcessor mockup"""
class DataProcessorMockup(Thread):
    def __init__(self, in_q, stop_event):
        super().__init__()
        self.in_q = in_q
        self.out_q_1 = Queue(maxsize=10000)#!!!
        self.out_q_mp = mp.Queue(maxsize=10000)
        self.produced_event = Event()
        self.produced_event.clear()
        self.stop_event = stop_event
        self.camcalib = CamCalib()
        self.computer = BFComputer(4)#!!!
    """Override"""
    def run(self):
        while not self.stop_event.is_set():
            try:
                sample = self.in_q.get(timeout=1)
                if sample is SENTINEL:
                    #self.out_q_1.put(SENTINEL)
                    #self.out_q_mp.put(SENTINEL)
                    break
                # Process data
                processed_sample = self.computer.compute(sample)
                if processed_sample.available:
                    v = processed_sample.value
                    processed = (v.timestamp, v.bfi, v.bvi, v.bfi, v.bvi)
                    self.out_q_1.put(processed)
                    self.out_q_mp.put(processed)
                    self.produced_event.set()
                    if indicate: print(f"= :{self.out_q_1.qsize()} :{self.out_q_mp.qsize()}")
                    QApplication.processEvents()
                self.in_q.task_done()
            except Empty: continue
    def process(self):
        #read:
        #from: cam_id,frame_id,timestamp_s, 0, ... 1023, temperature,sum,tcm,tcl,pdc
        #to: data, camera_inds, timept, temperature
        pass
""" """
class ConsumerBase(Thread):
    def __init__(self, in_q, produced_event, stop_event):
        super().__init__()
        self.in_q = in_q
        self.produced_event = produced_event
        self.stop_event = stop_event
"""DataStorage mockup"""
class DataStorageMockup(ConsumerBase):
    def __init__(self, in_q, produced_event, stop_event):
        super().__init__(in_q, produced_event, stop_event)
    """Override"""
    def run(self):
        with open("data.csv", "w") as f:
            while not self.stop_event.is_set():
                try:
                    self.produced_event.wait()
                    data = self.in_q.get(timeout=1)
                    if data is SENTINEL:
                        break
                    f.write(f"{data[0]},{data[1]}\n")
                    if indicate: print(">")
                    self.in_q.task_done()
                except Empty: continue
        pass
""" """
class DataPlotter(ConsumerBase):
    def __init__(self, layout, plot_x_size, in_q_mp):
        super().__init__(in_q_mp, None, None)
        self.plot_x_size = plot_x_size
        self.count = 0
        self.layout = layout
        self.left_plot = BFPlot("Left", layout, self.plot_x_size)
        self.right_plot = BFPlot("Right", layout, self.plot_x_size)
    """ """
    def update_plot(self):
        try:
            while True: # Process all available items
                #self.produced_event.wait()
                data = self.in_q.get_nowait()
                self.count += 1
                if data is SENTINEL:
                    #self.timer.stop()
                    #self.stop_event.set()
                    #break
                    pass
                if self.count %1 == 0:
                    plot = True 
                else: 
                    plot = False
                self.left_plot.update_plot_data(data[1], data[2], plot)
                self.right_plot.update_plot_data(data[3], data[4], plot)
                QApplication.processEvents()
                if indicate:
                   print("|")
                   sys.stdout.flush()
                self.in_q.task_done()

        except Exception as ex:
            pass
    """Override"""
    def run(self):
        while True:#not self.stop_event.is_set():
            self.update_plot()
        pass
"""Separate process function"""
def run_data_plot(q_mp):
    app = QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("BF BV plot proto")
    win.resize(800, 400)
    # Central widget and layout
    central_widget = QtWidgets.QWidget()
    win.setCentralWidget(central_widget)
    layout = QtWidgets.QVBoxLayout(central_widget)
    layout.setSpacing(0)
    plot_x_size = 500
    data_plotter = DataPlotter(layout, plot_x_size, q_mp)
    threads = [data_plotter]
    for t in threads: 
        t.start()
    #
    win.show()
    ex = app.exec()
    sys.exit(ex)
    #
    for t in threads: 
        t.join()
    pass

"""Demo"""
if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    win_main = QtWidgets.QMainWindow()
    win_main.setWindowTitle("BF BV plot proto")
    win_main.resize(800, 400)
    central_widget = QtWidgets.QWidget()
    win_main.setCentralWidget(central_widget)
    plot_widget = QtWidgets.QWidget()
    control_widget = QtWidgets.QWidget()

    central_layout = QtWidgets.QVBoxLayout(central_widget)
    central_layout.setSpacing(0)

    plot_layout = QtWidgets.QVBoxLayout()
    plot_layout.setSpacing(0)   
    plot_layout.addWidget(plot_widget)
    plot_layout.addWidget(QPushButton("__"))

    central_layout.addLayout(plot_layout)
    central_layout.addWidget(QPushButton("_"))

    #Data pipeline
    plot_x_size = 250
    count= 1000000000
    period_s = 0.025
    stop_event = Event()
    producer = DataProducerMockup(count, period_s, stop_event)
    processor = DataProcessorMockup(producer.out_q, stop_event)
    storage = DataStorageMockup(processor.out_q_1, processor.produced_event, stop_event)
    if False:#True:
        #Run plot separatelly
        threads = [producer, processor, storage]
        plot_process = mp.Process(target=run_data_plot, args=(processor.out_q_mp, ))
        plot_process.start()
    else:
        #Run plot in the main thread
         data_plotter = DataPlotter(plot_layout, plot_x_size, processor.out_q_mp)
         threads = [producer, processor, storage, data_plotter]
    pass
    for t in threads: 
        t.start()
    win_main.show()
    ex = app.exec()
    sys.exit(ex)

    for t in threads: 
        t.join()
    pass
