#! /usr/bin/python

#/******************************************************************
#//
#//  OpenCL Conformance Tests
#//
#//  Copyright:  (c) 2008-2009 by Apple Inc. All Rights Reserved.
#//
#******************************************************************/

from __future__ import print_function

import os
import re
import sys
import subprocess
import time
import tempfile

DEBUG = 0

log_file_name = "opencl_conformance_results_" + time.strftime("%Y-%m-%d_%H-%M", time.localtime()) + ".log"
process_pid = 0

# The amount of time between printing a "." (if no output from test) or ":" (if output)
#  to the screen while the tests are running.
seconds_between_status_updates = 60 * 60 * 24 * 7  # effectively never

# Help info
def write_help_info():
    print("run_conformance.py test_list [CL_DEVICE_TYPE(s) to test] [partial-test-names, ...] [log=path/to/log/file/]")
    print(" test_list - the .csv file containing the test names and commands to run the tests.")
    print(" [partial-test-names, ...] - optional partial strings to select a subset of the tests to run.")
    print(" [CL_DEVICE_TYPE(s) to test] - list of CL device types to test, default is CL_DEVICE_TYPE_DEFAULT.")
    print(" [log=path/to/log/file/] - provide a path for the test log file, default is in the current directory.")
    print("   (Note: spaces are not allowed in the log file path.")


# Get the time formatted nicely
def get_time():
    return time.strftime("%d-%b %H:%M:%S", time.localtime())


# Write text to the screen and the log file
def write_screen_log(text):
    global log_file
    print(text)
    log_file.write(text + "\n")


# Load the tests from a csv formated file of the form name,command
def get_tests(filename, devices_to_test):
    tests = []
    if os.path.exists(filename) == False:
        print("FAILED: test_list \"" + filename + "\" does not exist.")
        print("")
        write_help_info()
        sys.exit(-1)
    file = open(filename, 'r')
    for line in file.readlines():
        comment = re.search("^#.*", line)
        if comment:
            continue
        device_specific_match = re.search(r"^\s*(.+?)\s*,\s*(.+?)\s*,\s*(.+?)\s*$", line)
        if device_specific_match:
            if device_specific_match.group(1) in devices_to_test:
                test_path = str.replace(device_specific_match.group(3), '/', os.sep)
                test_name = str.replace(device_specific_match.group(2), '/', os.sep)
                tests.append((test_name, test_path))
            else:
                print("Skipping " + device_specific_match.group(2) + " because " + device_specific_match.group(1) + " is not in the list of devices to test.")
            continue
        match = re.search(r"^\s*(.+?)\s*,\s*(.+?)\s*$", line)
        if match:
            test_path = str.replace(match.group(2), '/', os.sep)
            test_name = str.replace(match.group(1), '/', os.sep)
            tests.append((test_name, test_path))
    return tests


def run_test_checking_output(current_directory, test_dir, output_fd, output_name, abort_event):
    failures_this_run = 0
    # Execute the test
    program_to_run = test_dir_without_args = test_dir.split(None, 1)[0]
    if os.sep == '\\':
        program_to_run += ".exe"

    test_exec_dir = os.path.dirname(current_directory + os.sep + test_dir_without_args)
    if not os.path.exists(current_directory + os.sep + program_to_run):
        os.write(output_fd, ("\n           ==> ERROR: test file (" + current_directory + os.sep + program_to_run + ") does not exist.  Failing test.\n").encode())
        return -1, None

    try:
        if DEBUG: p = subprocess.Popen("", stderr=subprocess.STDOUT, stdout=subprocess.PIPE, shell=True, cwd=test_exec_dir)
        else: p = subprocess.Popen(current_directory + os.sep + test_dir, stderr=output_fd, stdout=output_fd, shell=True, cwd=test_exec_dir)
    except OSError as e:
        os.write(output_fd, ("\n           ==> ERROR: failed to execute test. Failing test. : " + str(e) + "\n").encode())
        return -1, None

    # Wait for the process to complete or abort event
    while p.poll() is None:
        if abort_event.is_set():
            p.kill()
            os.write(output_fd, ("\n           ==> ERROR: test killed due to user interruption.\n").encode())
            return -1, p.pid
        time.sleep(0.1)

    os.fsync(output_fd)

    # Parse output for failures
    try:
        with open(output_name, 'r') as read_output:
            for line in read_output:
                if re.search(".*FAILED.*", line):
                    failures_this_run += 1
    except IOError:
        os.write(output_fd, ("\n           ==> ERROR: could not read output file.\n").encode())
        return -1, p.pid

    if (p.returncode == 0 and failures_this_run > 0):
        os.write(output_fd, ("\n           ==> ERROR: Test returned 0, but number of FAILED lines reported is " + str(failures_this_run) + ".\n").encode())
        return failures_this_run, p.pid

    return p.returncode, p.pid


import multiprocessing
import threading
import concurrent.futures

log_lock = threading.Lock()

def process_test(test, current_directory, abort_event):
    (test_name, test_dir) = test
    start_time = time.time()

    (output_fd, output_name) = tempfile.mkstemp()
    if not os.path.exists(output_name):
        return (test_name, test_dir, -1, 0, "", "could not create temporary file")

    result, pid = run_test_checking_output(current_directory, test_dir, output_fd, output_name, abort_event)

    run_time = (time.time() - start_time)

    os.close(output_fd)

    try:
        with open(output_name, 'r') as f:
            output_content = f.read()
        os.remove(output_name)
    except Exception as e:
        output_content = "Failed to read output: " + str(e)

    return (test_name, test_dir, result, run_time, output_content, pid)

def run_tests(tests):
    global current_directory
    global log_file
    failures = 0
    previous_test = None

    abort_event = threading.Event()
    max_workers = multiprocessing.cpu_count()
    write_screen_log("Using " + str(max_workers) + " worker threads for execution.")

    futures = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        try:
            for test in tests:
                futures.append(executor.submit(process_test, test, current_directory, abort_event))

            test_number = 1
            for future in concurrent.futures.as_completed(futures):
                (test_name, test_dir, result, run_time, output_content, pid) = future.result()

                with log_lock:
                    log_file.write("========================================================================================\n")

                    log_file.write("(" + get_time() + ")     Running Tests: " + test_dir + "\n")

                    log_file.write("========================================================================================\n")

                    print("(" + get_time() + ")     BEGIN  " + test_name.ljust(40) + ": ")
                    log_file.write("     ----------------------------------------------------------------------------------------\n")
                    log_file.write("     (" + get_time() + ")     Running Sub Test: " + test_name + "\n")
                    log_file.write("     ----------------------------------------------------------------------------------------\n")

                    # Write captured output
                    for line in output_content.splitlines():
                        log_file.write("     " + line + "\n")
                        # Output key lines to screen
                        if re.search(".*(FAILED|ERROR).*", line) or re.search(".*(PASSED).*", line):
                            print("           ==> " + line.strip())

                    if result == 0:
                        print("(" + get_time() + ")     PASSED " + test_name.ljust(40) + ": (" + str(int(run_time)).rjust(3) + "s, test " + str(test_number).rjust(3) + os.sep + str(len(tests)) + ")")
                    else:
                        print("(" + get_time() + ")     FAILED " + test_name.ljust(40) + ": (" + str(int(run_time)).rjust(3) + "s, test " + str(test_number).rjust(3) + os.sep + str(len(tests)) + ")")

                    log_file.write("     ----------------------------------------------------------------------------------------\n")

                    if result != 0:
                        log_file.write("  *******************************************************************************************\n")
                        log_file.write("  *  (" + get_time() + ")     Test " + test_name + " ==> FAILED: " + str(result) + "\n")
                        log_file.write("  *******************************************************************************************\n")
                        failures += 1
                    else:
                        log_file.write("     (" + get_time() + ")     Test " + test_name + " passed in " + str(run_time) + "s\n")

                    log_file.write("     ----------------------------------------------------------------------------------------\n\n")
                    log_file.flush()
                    sys.stdout.flush()
                    test_number += 1

        except KeyboardInterrupt:
            write_screen_log("\nFAILED: Execution interrupted. Killing test processes and aborting full test run.")
            abort_event.set()

            # Wait a brief moment for threads to clean up
            time.sleep(1)

            # Print remaining outputs
            with log_lock:
                write_screen_log("\nUser chose to abort all tests. Collecting partial logs...")
                for future in futures:
                    if future.done():
                        try:
                            (test_name, test_dir, result, run_time, output_content, pid) = future.result()
                            log_file.write("\n--- Interrupted Test: " + test_name + " ---\n")
                            for line in output_content.splitlines():
                                log_file.write("     " + line + "\n")
                        except Exception:
                            pass
                log_file.close()
                sys.exit(-1)

    return failures

# ########################
# Begin OpenCL conformance run script
# ########################

if len(sys.argv) < 2:
    write_help_info()
    sys.exit(-1)

current_directory = os.getcwd()
# Open the log file
for arg in sys.argv:
    match = re.search("log=(\\S+)", arg)
    if match:
        log_file_name = match.group(1).rstrip('/') + os.sep + log_file_name
try:
    log_file = open(log_file_name, "w")
except IOError:
    print("Could not open log file " + log_file_name)
    sys.exit(-1)

# Determine which devices to test
device_types = ["CL_DEVICE_TYPE_DEFAULT", "CL_DEVICE_TYPE_CPU", "CL_DEVICE_TYPE_GPU", "CL_DEVICE_TYPE_ACCELERATOR", "CL_DEVICE_TYPE_ALL"]
devices_to_test = []
for device in device_types:
    if device in sys.argv[2:]:
        devices_to_test.append(device)
if len(devices_to_test) == 0:
    devices_to_test = ["CL_DEVICE_TYPE_DEFAULT"]
write_screen_log("Testing on: " + str(devices_to_test))

# Get the tests
tests = get_tests(sys.argv[1], devices_to_test)

# If tests are specified on the command line then run just those ones
tests_to_use = []
num_of_patterns_to_match = 0
for arg in sys.argv[2:]:
    if arg in device_types:
        continue
    if re.search("log=(\\S+)", arg):
        continue
    num_of_patterns_to_match = num_of_patterns_to_match + 1
    found_it = False
    for test in tests:
        (test_name, test_dir) = test
        if (test_name.find(arg) != -1 or test_dir.find(arg) != -1):
            found_it = True
            if test not in tests_to_use:
                tests_to_use.append(test)
    if found_it == False:
        print("Failed to find a test matching " + arg)
if len(tests_to_use) == 0:
    if num_of_patterns_to_match > 0:
        print("FAILED: Failed to find any tests matching the given command-line options.")
        print("")
        write_help_info()
        sys.exit(-1)
else:
    tests = tests_to_use[:]

write_screen_log("Test execution arguments: " + str(sys.argv))
write_screen_log("Logging to file " + log_file_name + ".")
write_screen_log("Loaded tests from " + sys.argv[1] + ", total of " + str(len(tests)) + " tests selected to run:")
for (test_name, test_command) in tests:
    write_screen_log(test_name.ljust(50) + " (" + test_command + ")")

# Run the tests
total_failures = 0
for device_to_test in devices_to_test:
    os.environ['CL_DEVICE_TYPE'] = device_to_test
    write_screen_log("========================================================================================")
    write_screen_log("========================================================================================")
    write_screen_log(("Setting CL_DEVICE_TYPE to " + device_to_test).center(90))
    write_screen_log("========================================================================================")
    write_screen_log("========================================================================================")
    failures = run_tests(tests)
    write_screen_log("========================================================================================")
    if failures == 0:
        write_screen_log(">> TEST on " + device_to_test + " PASSED")
    else:
        write_screen_log(">> TEST on " + device_to_test + " FAILED (" + str(failures) + " FAILURES)")
    write_screen_log("========================================================================================")
    total_failures = total_failures + failures

write_screen_log("(" + get_time() + ") Testing complete.  " + str(total_failures) + " failures for " + str(len(tests)) + " tests.")
log_file.close()
