vbf_stat.py module contains code for profiling operation used for blood flow acquisition , plotting and stoing.
All the profiling code is placed into the main_profile function.
To start MongoDB installed locally: 
C:\MongoDB\bin\mongod.exe --config C:\MongoDB\mongod.conf

Stream prototyping:
stream_proto.py - main
session_samples.py: simulation of stream by reading rows from csv file
bfcompute.py: simplified implementation of BF/BV calculation from histograms
