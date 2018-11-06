# Copyright 2018 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

import os
import pyspark
from six.moves import queue
import sys
import threading

from horovod.spark import codec, host_hash, task_service, driver_service, network, timeout, safe_shell_exec


def task_fn(index, driver_addresses, num_proc, start_timeout_at):
    tmout = timeout.Timeout(start_timeout_at)
    task = task_service.TaskService(index)
    try:
        driver_client = driver_service.DriverClient(driver_addresses)
        driver_client.register(index, task.addresses(), host_hash.host_hash())
        task.wait_for_initial_registration(tmout)
        # Tasks ping each other in a circular fashion to determine interfaces reachable within
        # the cluster.
        next_task_index = (index + 1) % num_proc
        next_task_addresses = driver_client.all_task_addresses(next_task_index)
        next_task_client = task_service.TaskClient(next_task_index, next_task_addresses)
        driver_client.register_task_to_task_addresses(next_task_index, next_task_client.addresses())
        task_indices_on_this_host = driver_client.task_host_hash_indices(host_hash.host_hash())
        if task_indices_on_this_host[0] == index:
            # Task with first index will execute orted that will run mpirun_exec_fn for all tasks.
            task.wait_for_command_start(tmout)
            task.wait_for_command_termination()
        else:
            # The rest of tasks need to wait for the first task to finish.
            first_task_addresses = driver_client.all_task_addresses(task_indices_on_this_host[0])
            first_task_client = task_service.TaskClient(task_indices_on_this_host[0], first_task_addresses)
            first_task_client.wait_for_command_termination()
        return task.fn_result()
    except network.DrainError as e:
        raise Exception('Terminating due to an earlier error: %s' % str(e))
    finally:
        task.shutdown()


def _make_mapper(driver_addresses, num_proc, start_timeout_at):
    def _mapper(index, _):
        yield task_fn(index, driver_addresses, num_proc, start_timeout_at)
    return _mapper


def _make_barrier_mapper(driver_addresses, num_proc, start_timeout_at):
    def _mapper(_):
        ctx = pyspark.BarrierTaskContext.get()
        ctx.barrier()
        index = ctx.partitionId()
        yield task_fn(index, driver_addresses, num_proc, start_timeout_at)
    return _mapper


def _make_spark_thread(spark_context, num_proc, driver, start_timeout_at, result_queue):
    def run_spark():
        try:
            procs = spark_context.range(0, numSlices=num_proc)
            if hasattr(procs, 'barrier'):
                # Use .barrier() functionality if it's available.
                procs = procs.barrier()
                result = procs.mapPartitions(_make_barrier_mapper(driver.addresses(), num_proc, start_timeout_at)).collect()
            else:
                result = procs.mapPartitionsWithIndex(_make_mapper(driver.addresses(), num_proc, start_timeout_at)).collect()
            result_queue.put(result)
        except:
            driver.notify_spark_job_failed()
            raise

    spark_thread = threading.Thread(target=run_spark)
    spark_thread.start()
    return spark_thread


def run(fn, args=(), kwargs={}, num_proc=None, start_timeout=180):
    spark_context = pyspark.SparkContext._active_spark_context
    if spark_context is None:
        raise Exception('Could not find an active SparkContext, are you running in a PySpark session?')

    if num_proc is None:
        num_proc = spark_context.defaultParallelism

    result_queue = queue.Queue(1)
    start_timeout_at = timeout.timeout_at(start_timeout)
    tmout = timeout.Timeout(start_timeout_at)
    driver = driver_service.DriverService(num_proc, fn, args, kwargs)
    spark_thread = _make_spark_thread(spark_context, num_proc, driver, start_timeout_at, result_queue)
    try:
        driver.wait_for_initial_registration(tmout)
        task_clients = [task_service.TaskClient(index, driver.task_addresses_for_driver(index))
                        for index in range(num_proc)]
        for task_client in task_clients:
            task_client.notify_initial_registration_complete()
        driver.wait_for_task_to_task_address_updates(tmout)

        # Determine a set of common interfaces for task-to-task communication.
        common_intfs = set(driver.task_addresses_for_tasks(0).keys())
        for index in range(1, num_proc):
            common_intfs.intersection_update(driver.task_addresses_for_tasks(index).keys())
        if not common_intfs:
            raise Exception('Unable to find a set of common task-to-task communication interfaces: %s'
                            % [(index, driver.task_addresses_for_tasks(index)) for index in range(num_proc)])

        # Determine the index grouping based on host hashes.
        # Barrel shift until index 0 is in the first host.
        host_hashes = driver.task_host_hash_indices().keys()
        host_hashes.sort()
        while 0 not in driver.task_host_hash_indices()[host_hashes[0]]:
            host_hashes = host_hashes[1:] + host_hashes[:1]

        ranks_to_indices = []
        for host_hash in host_hashes:
            ranks_to_indices += driver.task_host_hash_indices()[host_hash]
        driver.set_ranks_to_indices(ranks_to_indices)

        exit_code = safe_shell_exec.execute(
            'mpirun --allow-run-as-root '
            '-np {num_proc} -H {hosts} '
            '-bind-to none -map-by slot '
            '-mca pml ob1 -mca btl ^openib -mca btl_tcp_if_include {common_intfs} '
            '-x NCCL_DEBUG=INFO -x NCCL_SOCKET_IFNAME={common_intfs} '
            '{env} '  # expect a lot of environment variables
            '-mca plm_rsh_agent "python -m horovod.spark.mpirun_rsh {encoded_driver_addresses}" '
            'python -m horovod.spark.mpirun_exec_fn {encoded_driver_addresses} '
            .format(num_proc=num_proc,
                    hosts=','.join('%s:%d' % (host_hash, len(driver.task_host_hash_indices()[host_hash]))
                                   for host_hash in host_hashes),
                    common_intfs=','.join(common_intfs),
                    env=' '.join('-x %s' % key for key in os.environ.keys()),
                    encoded_driver_addresses=codec.dumps_base64(driver.addresses())),
            env=os.environ)
        if exit_code != 0:
            raise Exception('mpirun exited with code %d, see the error above.' % exit_code)
    except:
        exc_type, exc_value, exc_traceback = sys.exc_info()

        # Schedule driver for shutdown, so tasks trying to connect due to Spark retries will fail fast.
        driver.drain(str(exc_value))

        # Interrupt waiting tasks.  This is useful if the main flow quickly terminated, e.g. due to mpirun error,
        # and tasks are still waiting for a command to be executed on them.  This request is best-effort and is
        # not required for the proper shutdown, it just speeds it up and provides clear error message.
        for index in driver.registered_task_indices():
            # We only need to do this housekeeping while Spark Job is in progress.  If Spark job has finished,
            # it means that all the tasks are already terminated.
            if spark_thread.is_alive():
                try:
                    task_client = task_service.TaskClient(index, driver.task_addresses_for_driver(index))
                    task_client.interrupt_waits(str(exc_value))
                except:
                    pass

        # Re-raise the error.
        raise exc_type, exc_value, exc_traceback
    finally:
        spark_thread.join()
        driver.shutdown()

    # Make sure Spark Job did not fail.
    driver.check_for_spark_job_failure()

    # If there's no exception, execution results are in this queue.
    results = result_queue.get_nowait()
    return [results[index] for index in ranks_to_indices]
