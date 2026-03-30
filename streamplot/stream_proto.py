import sys
import time
import random
import numpy as np
from queue import Queue, Empty
from threading import Thread, Event
from PySide6.QtCore import QObject, QTimer, Slot, Signal, Property
from PySide6 import QtWidgets
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QPushButton
from PySide6.QtQuick import QQuickView
from PySide6.QtQml import QQmlApplicationEngine

import pyqtgraph as pg
#from pyqtgraph.Qt import QtCore
#from PyQt6 import QtWidgets
#from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton
#from PyQt6.QtWidgets import QMainWindow, QPushButton

import PySide6
from PySide6 import QtCore
from PySide6.QtCore import QObject
from PySide6.QtCore import QTimer
from PySide6.QtCore import Slot
from PySide6.QtCore import Signal
from PySide6.QtCore import Property

from PySide6.QtCore import QObject
from PySide6.QtCore import QTimer
from PySide6.QtCore import Slot
from PySide6.QtCore import Signal
from PySide6.QtCore import Property

from PySide6.QtWidgets import QApplication
from PySide6.QtQuick import QQuickView
from PySide6.QtQml import QQmlApplicationEngine



import multiprocessing as mp
import csv

import uuid

# Generate a random UUID (version 4)
#random_guid = uuid.uuid4()
# Convert the UUID object to a string format with hyphens
#guid_string = str(random_guid)

import bfplot
from bfplot import BFPlot
from session_samples import Sample
from session_samples import SessionSamples

from bfcompute import CamCalib
from bfcompute import BFComputer
from bfcompute import BFValue
from bfcompute import ValueAvailable

from bfstorage import SamplesDBsqlite

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
        #self.csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_125021_left_mask99.csv"
        #self.csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_124944_left_mask99.csv"
        self.csv_path = "C:/Users/ethan/Projects/ow-bloodflow-app/scan_data/scan_owTM2GXS_20260214_124553_left_mask99.csv"
        self.sd = SessionSamples();
        self.count = self.sd.read_csv(self.csv_path)
        self.id = 0
        self.out_q_raw_store = Queue(maxsize=self.q_max_size); 
    """Override"""
    def run(self):
        pass
        try:
            for i in range(self.count):
                if self.stop_event.is_set():
                    break
                data = self.sd.get(self.id)
                self.id += 1
                self.id = self.id if self.id < self.count else 0
                self.out_q.put(data)
                self.out_q_raw_store.put(data)
                self.produced_event.set()
                if indicate: print(f"* {self.out_q.qsize()}")
                time.sleep(self.period_s)
                QApplication.processEvents()
                pass
            self.out_q.put(SENTINEL) # Signal end
            self.out_q_raw_store.put(SENTINEL) # Signal end
        except Exception as e:
            print(e)
            pass
"""DataProcessor mockup"""
class DataProcessorMockup(Thread):
    def __init__(self, in_q, stop_event):
        super().__init__()
        self.in_q = in_q
        self.out_q_1 = Queue(maxsize=10000)#!!!
        self.out_q_mp = mp.Queue(maxsize=10000)
        self.out_q_qml = mp.Queue(maxsize=10000)
        self.produced_event = Event()
        self.produced_event.clear()
        self.stop_event = stop_event
        self.camcalib = CamCalib()
        self.computer = BFComputer(4)#!!!
    """Override"""
    def run(self):
        pass
        try:
            while not self.stop_event.is_set():
                try:
                    sample = self.in_q.get(timeout=1)
                    if sample is SENTINEL:
                        self.out_q_1.put(SENTINEL)
                        self.out_q_mp.put(SENTINEL)
                        self.out_q_qml.put(SENTINEL)
                        break
                    # Process data
                    processed_sample = self.computer.compute(sample)
                    if processed_sample.available:
                        v = processed_sample.value
                        processed = (v.timestamp, v.bfi, v.bvi, v.bfi, v.bvi)
                        self.out_q_1.put(processed)
                        self.out_q_mp.put(processed)
                        self.out_q_qml.put(processed)
                        self.produced_event.set()
                        if indicate: print(f"= :{self.out_q_1.qsize()} :{self.out_q_mp.qsize()} :{self.out_q_qml.qsize()}")
                        QApplication.processEvents()
                    self.in_q.task_done()
                except Empty: continue
        except Exception as e:
            print(e)
        pass
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
"""DataStorage mockups"""
class RawDataStorageMockup(ConsumerBase):
    def __init__(self,uid, session_id, in_q, produced_event, stop_event):
        super().__init__(in_q, produced_event, stop_event)
        self.uid = uid
        self.session_id = session_id
        self.raw_data = []
    """Override"""
    def run(self):
        self.raw_data = []
        try:
            while not self.stop_event.is_set():
                try:
                    self.produced_event.wait()
                    sample = self.in_q.get(timeout=1)
                    if sample is SENTINEL:
                        break
                    self.raw_data.append(sample)
                    if indicate: print(">>")
                    self.in_q.task_done()
                except Empty:
                   continue
            self.store()
            pass
        except Exception as e:
            print(e)
        pass
    """ """
    def store(self):
        #store collected raw_data
        db = "raw_db.sqlite"
        sdb = SamplesDBsqlite(db, self.uid)
        sdb.insert(self.session_id, self.raw_data)
        print("Stored raw session data")
        #sdb.view_content()
        pass
    """ """
    def get_record(self, id):
        try:
            self.export_data = []
            pass
        except Exception as e:
            pass
        pass
    """ """
    def export2csv(self, id):
        with open(f"{id}_raw_data.csv", "w",  newline='') as f:
            #get record from db
            self.get_record(id)
            writer = csv.writer(f)
            # Write the header row using field names
            writer.writerow(Sample._fields)
            writer.writerows(self.export_data)
        pass
""" """
class ProcessedDataStorageMockup(ConsumerBase):
    def __init__(self, uid, session_id, in_q, produced_event, stop_event):
        super().__init__(in_q, produced_event, stop_event)
        self.uid = uid
        self.session_id = session_id
    """Override"""
    def run(self):
        pass
        try:
            with open("processed_data.csv", "w") as f:
                writer = csv.writer(f)
                # Write the header row using field names
                writer.writerow(BFValue._fields)
                while not self.stop_event.is_set():
                    try:
                        self.produced_event.wait()
                        data = self.in_q.get(timeout=1)
                        if data is SENTINEL:
                            break
                        writer.writerow(data)
                        if indicate: print(">")
                        self.in_q.task_done()
                    except Empty: continue
            pass
        except Exception as e:
            print(e)
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
                    break
                    pass
                if self.count >= 20:#!!!
                    if self.count == 20:
                        self.left_plot.init_plot_data(data[1], data[2])
                        self.right_plot.init_plot_data(data[3], data[4])
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

""" """
class BFProducer(QObject):
    bfUpdated = Signal(float, float, float, float, float)
    """ """
    def __init__(self):
        super().__init__()
        self.x = 0
    """ """
    @Slot()
    def update_bf(self, x, d1, d2, d3, d4):
        self.x += 1
        self.bfUpdated.emit(self.x, d1, d2, d3, d4)
""" """
class DataPlotterQML(ConsumerBase):
    def __init__(self, layout, plot_x_size, in_q_mp):
        super().__init__(in_q_mp, None, None)
        self.plot_x_size = plot_x_size
        self.count = 0
        self.layout = layout
        self.x = 0
        self.bfProducer = BFProducer()
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
                    break
                    pass
                if self.count >= 20:#!!!
                    #if self.count == 20:
                    #    self.left_plot.init_plot_data(data[1], data[2])
                    #    self.right_plot.init_plot_data(data[3], data[4])
                    plot = True 
                else: 
                    plot = False
                self.x += 1        
                self.bfProducer.update_bf(self.x, data[1], data[2], data[3], data[4])
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
    win_main.setWindowTitle("BF BV plot PyQtgraph")
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

    #subject
    uid = uuid.uuid4()
    #Session
    session_id = 1
    #Data pipeline
    plot_x_size = 250
    count= 1000000000
    period_s = 0.010
    stop_event = Event()
    #produce
    producer = DataProducerMockup(count, period_s, stop_event)
    #store raw
    raw_storage = RawDataStorageMockup(uid, session_id, producer.out_q_raw_store, producer.produced_event, stop_event)
    #process
    processor = DataProcessorMockup(producer.out_q, stop_event)
    #store processed
    storage = ProcessedDataStorageMockup(uid, session_id, processor.out_q_1, processor.produced_event, stop_event)
    #plot
    data_plotter = DataPlotter(plot_layout, plot_x_size, processor.out_q_mp)
    #plot QML
    data_plotter_qml = DataPlotterQML(plot_layout, plot_x_size, processor.out_q_qml)
    #threads
    threads = [producer, raw_storage, processor, storage, data_plotter, data_plotter_qml]
    for t in threads: 
        print(t)
        t.start()
    #UI
    win_main.show()

    #QML
    engine = QQmlApplicationEngine()
    # Expose Python object to QML
    engine.rootContext().setContextProperty("bfSystem", data_plotter_qml.bfProducer)
    engine.load("bfplot_qml_app.qml")
    if not engine.rootObjects():
        sys.exit(-1)
    #
    ex = app.exec()
    sys.exit(ex)

    for t in threads: 
        t.join()
    pass
