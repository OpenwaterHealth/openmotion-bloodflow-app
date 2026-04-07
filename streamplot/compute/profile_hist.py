import numpy as np
from streamhist import StreamHist
import time
import sys
#import pickle
import json
import os
import pathlib
from pathlib import Path
import util.format_num as format_num
from util.format_num import format_three_nonzero_decimals
from typing import Callable
from typing import Any

class ProfileHist():
    def __init__(self, nbins=100):
        self.nbins = nbins
        self.hist=StreamHist(self.nbins)
        pass
    def add(self, value):
        self.hist.update(value)
    """ """
    def prin(self):
        print(f"Histogram sum: {h1.sum()}")
        print(f"Histogram bins: {h1.bins}")

    def breaks_to_string(self, num=50):
        """Print a string reprentation of the histogram."""
        string = ""
        if self.hist.total <= 0:
            return string
        for c, b in zip(*self.hist.compute_breaks(num)):
            bar = str()
            try:
                for i in range(int(c/float(self.hist.total)*200)):
                    bar += "."
                string += f"{format_three_nonzero_decimals(b)}" + "\t" + bar + "\n"
            except Exception as e:
                get_tlog().logger.error(f"Exception while saving histogram breaks to string.")
        return string
    """ """
    def to_string(self):
        if self.hist.total <= 0:
            return ""
        title = f"--- Histogram Data ---\nTotal Samples: {self.hist.count()}\nRange: [{self.hist.min():.2f}, {self.hist.max():.2f}]\n"
        ndashes = len(title) + 8
        dashes = "-" * ndashes
        strout = dashes + "\n" + title + dashes + "\n"
        # Formatting bins into a string
        output = (f"{'Bin Range':<20} | {'Count':<10}\n")
        output += ("-" * 32)
        output += "\n"
        # Calculate bins and counts for display
        # streamhist stores bins in _bins, which is a list of [value, count]
        for b in self.hist.bins:
            bin_range = format_three_nonzero_decimals(b[0])#f"{b[0]:.2f}"
            count = b[1]#f"{b[1]:.2f}"
            output += (f"{bin_range:<20} | {count:<10}\n")
        pass
        return strout + output
    """ """
    def brief_json_string(self):
        """Generate various summary statistics."""
        jsondata = {
            "count": float(self.hist.count()),
            "mean": self.hist.mean(),
            "var": self.hist.var(),
            "min": self.hist.min(),
            "max": self.hist.max()
        }
        json_string = json.dumps(jsondata)
        return json_string

    def brief_string(self):
        ret = f"Count: {self.hist.count()} \
        Mean: {format_three_nonzero_decimals(self.hist.mean())} \
        Var: {format_three_nonzero_decimals(self.hist.var())} \
        MIN: {format_three_nonzero_decimals(self.hist.min())} \
        MAX: {format_three_nonzero_decimals(self.hist.max())}"
        return ret
    """ """
    def save_hist(self, filename):#*.pkl
        """Saves a StreamHist object to a file using pickle."""
        with open(filename, "wb") as f:
            pickle.dump(self.hist, f)
    """ """
    def load_hist(self, filename):#*.pkl
        """Loads a StreamHist object from a file using pickle."""
        if os.path.exists(filename):
            with open(filename, "rb") as f:
                self.hist = pickle.load(f)
                return loaded_histogram
        else:
            get_tlog().logger.error(f"Error: File '{filename}' not found.")
            return None
    """ """
    def save_out(self, file_path_template, print_hist):
        try:
            # Print the current state of the histogram bins
            file_path_hist = str(file_path_template) + "_hist.txt"
            file_path_data = str(file_path_template) + "_hist_data.txt"
            #
            brief = self.brief_string()
            print(brief)
            breaks = self.breaks_to_string(self.nbins)
            brief += "\n"
            brief += breaks
            if print_hist:
                print(breaks)
            with open(file_path_hist, 'w') as f:
                f.write(brief)
            #
            st = self.to_string()
            if print_hist:
                print(st)
            with open(file_path_data, 'w') as f:
                f.write(st)
        except Exception as e:
            print(f"Exception while saving out: {e}")
        pass
    """ """
""" """
class ExecutionTimer:
    """
    A class to measure, record, and analyze the execution times of functions.
    """
    def_nbins = 1000
    def __init__(self):
        self.durations = []
        self.hist = ProfileHist()
    """ """
    def time_function(self, func: Callable, *args, **kwargs) -> Any:
        """
        Executes a function and records its execution time.

        Args:
            func: The function to time.
            *args: Arguments for the function.
            **kwargs: Keyword arguments for the function.

        Returns:
            The result of the executed function.
        """
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        duration = end_time - start_time
        self.hist.add(duration)
        return result

