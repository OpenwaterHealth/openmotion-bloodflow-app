import sys
import numpy as np
#from PyQt6.QtWidgets import QApplication

from pyqtgraph.Qt import QtCore, QtWidgets
import pyqtgraph as pg
import random
from threading import Thread, Event

from PySide6.QtCore import QObject, QTimer, Slot, Signal, Property
from PySide6.QtWidgets import QApplication

class BFPlot():
    def __init__(self, mark, layout, window_size,  data1=None, data2=None):
        self.mark = mark
        self.layout = layout
        self.plot_widget = pg.PlotWidget()
        self.layout.addWidget(self.plot_widget)
        # create a new ViewBox, link the right axis to its coordinate system
        self.view_box_second = pg.ViewBox()
        self.plot_item = self.plot_widget.plotItem
        self.plot_item.showAxis('right')
        self.plot_item.scene().addItem(self.view_box_second)
        self.plot_item.getAxis('right').linkToView(self.view_box_second)
        self.view_box_second.setXLink(self.plot_item)
        self.plot_item.hideAxis('bottom')
        #
        self.plot_item.getAxis('left').setPen(color='r')  # Sets line and ticks to red
        self.plot_item.getAxis('left').setTextPen(color='r')  # Sets tick labels to red
        self.plot_item.getAxis('right').setPen(color='b')
        self.plot_item.getAxis('right').setTextPen(color='b')
        # Styling
        self.plot_widget.setBackground('k')  # Black background
        self.plot_widget.addLegend()
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.setLabel('left', f"{self.mark} BF", color='#ff0000')#, units='V')
        self.plot_widget.setLabel('right', f"{self.mark} BV", color='#0000ff')#, units='V')
        # --- Data Initialization ---
        self.window_size = window_size
        self.data1 = [0.0] * self.window_size
        self.data2 = [0.0] * self.window_size
        self.x = np.arange(self.window_size)
        # Create two plot lines
        # pen: (color, width)
        self.curve1 = self.plot_widget.plot(self.x, self.data1,  pen=pg.mkPen('r', width=2))
        self.curve2 = pg.PlotCurveItem(self.data2, pen='b')
        self.view_box_second.addItem(self.curve2)
        self.plot_item.vb.sigResized.connect(self.updateViews)
        self.updateViews()
        # --- Auto-Scaling ---
        # This ensures the plot adjusts to new data ranges automatically
        self.plot_widget.enableAutoRange(axis='y', enable=True)
        self.ssig = 0
        self.set_data_event = Event()
        self.set_data_timer = QtCore.QTimer()
        self.set_data_timer.timeout.connect(self.set_data)
        self.set_data_timer.start(10) 
        pass
    """Redraw the plot. To be executed in the GUI thread"""
    def set_data(self):
        if self.set_data_event.is_set():
            self.curve1.setData(self.x, self.data1)
            self.curve2.setData(self.x, self.data2)
        pass
    """ """
    def update_plot_data(self, d1, d2, plot = True):
        self.data1.append(d1)
        if len(self.data1) > self.window_size:
            self.data1.pop(0)
        self.data2.append(d2)
        if len(self.data2) > self.window_size:
            self.data2.pop(0)
        if plot:
            self.set_data_event.set()#trigger redraw
    """ """
    def init_plot_data(self, d1, d2):
        for i in range(len(self.data1)):
            self.data1[i] = d1
            self.data2[i] = d2
        pass
    """Simulate data"""
    def update_plot_sin(self):
        self.ssig += 1
        k = 1.0 + np.sin(np.pi/self.window_size * self.ssig)*0.01
        d0 = self.data1[0]
        self.data1[:-1] = self.data1[1:] * k
        self.data1[-1] = d0 *k
        self.curve1.setData(self.x, self.data1)
        d0 = self.data2[0]
        self.data2[:-1] = self.data2[1:] * k
        self.data2[-1] = d0 * k
        self.curve2.setData(self.x, self.data2)
        QApplication.processEvents()
    """ """
        ## Handle view resizing 
    def updateViews(self):
        ## view has resized; update auxiliary views to match
        self.view_box_second.setGeometry(self.plot_item.vb.sceneBoundingRect())
        ## need to re-update linked axes since this was called
        ## incorrectly while views had different shapes.
        ## (probably this should be handled in ViewBox.resizeEvent)
        self.view_box_second.linkedViewChanged(self.plot_item.vb, self.view_box_second.XAxis)
""" """
def update_plot():
    d1 = random.random() * 10.0
    d2 = random.random() * 10.0
    left_plot.update_plot_data(d1, d2)
    #right_plot.update_plot_data(d1, d2)

"""Demo"""
if __name__ == "__main__":
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
    left_plot = BFPlot("Left", layout, plot_x_size)
    #
    timer = QtCore.QTimer()
    timer.timeout.connect(update_plot)
    timer.start(25)  # Update every 25ms (40 FPS)
    #
    win.show()
    sys.exit(app.exec())
