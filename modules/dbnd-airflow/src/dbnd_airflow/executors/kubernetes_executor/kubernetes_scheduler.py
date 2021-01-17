# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.


import logging
import time

from airflow.contrib.executors.kubernetes_executor import AirflowKubernetesScheduler
from airflow.utils.db import provide_session
from airflow.utils.state import State

from dbnd._core.constants import TaskRunState
from dbnd._core.current import try_get_databand_run
from dbnd._core.errors.friendly_error.executor_k8s import KubernetesImageNotFoundError
from dbnd_airflow.airflow_extensions.dal import (
    get_airflow_task_instance,
    get_airflow_task_instance_state,
)
from dbnd_airflow.executors.kubernetes_executor.kubernetes_watcher import (
    DbndKubernetesJobWatcher,
)
from dbnd_airflow.executors.kubernetes_executor.utils import mgr_init
from dbnd_airflow_contrib.kubernetes_metrics_logger import KubernetesMetricsLogger
from dbnd_docker.kubernetes.kube_dbnd_client import (
    PodPhase,
    PodRetryReason,
    _get_status_log_safe,
    _try_get_pod_exit_code,
)


logger = logging.getLogger(__name__)


class DbndKubernetesScheduler(AirflowKubernetesScheduler):
    """
    Very serious override of AirflowKubernetesScheduler
    1. we want better visability on errors, so we proceed Failures with much more info
    2. handling of disappeared pods
    """

    def __init__(
        self, kube_config, task_queue, result_queue, kube_client, worker_uuid, kube_dbnd
    ):
        super(DbndKubernetesScheduler, self).__init__(
            kube_config, task_queue, result_queue, kube_client, worker_uuid
        )
        self.kube_dbnd = kube_dbnd

        # PATCH watcher communication manager
        # we want to wait for stop, instead of "exit" inplace, so we can get all "not" received messages
        from multiprocessing.managers import SyncManager

        # Scheduler <-> (via _manager) KubeWatcher
        # if _manager dies inplace, we will not get any "info" from KubeWatcher until shutdown
        self._manager = SyncManager()
        self._manager.start(mgr_init)

        self.watcher_queue = self._manager.Queue()
        self.current_resource_version = 0
        self.kube_watcher = self._make_kube_watcher_dbnd()

        # pod to airflow key (dag_id, task_id, execution_date)
        self.running_pods = {}
        self.pod_to_task_run = {}

        self.metrics_logger = KubernetesMetricsLogger()

        # disappeared pods mechanism
        self.last_disappeared_pods = {}
        self.current_iteration = 1

    def _make_kube_watcher(self):
        # prevent storing in db of the kubernetes resource version, because the kubernetes db model only stores a single value
        # of the resource version while we need to store a sperate value for every kubernetes executor (because even in a basic flow
        # we can have two Kubernets executors running at once, the one that launched the driver and the one inside the driver).
        #
        # the resource version is the position inside the event stream of the kubernetes cluster and is used by the watcher to poll
        # Kubernets for events. It's probably fine to not store this because by default Kubernetes will returns "the evens currently in cache"
        # https://github.com/kubernetes-client/python/blob/master/kubernetes/docs/CoreV1Api.md#list_namespaced_pod
        return None

    def _make_kube_watcher_dbnd(self):
        watcher = DbndKubernetesJobWatcher(
            namespace=self.namespace,
            watcher_queue=self.watcher_queue,
            resource_version=self.current_resource_version,
            worker_uuid=self.worker_uuid,
            kube_config=self.kube_config,
            kube_dbnd=self.kube_dbnd,
        )
        watcher.start()
        return watcher

    @staticmethod
    def _create_pod_id(dag_id, task_id):
        task_run = try_get_databand_run().get_task_run(task_id)
        return task_run.job_id__dns1123

    def _health_check_kube_watcher(self):
        if self.kube_watcher.is_alive():
            pass
        else:
            self.log.error(
                "Error while health checking kube watcher process. "
                "Process died for unknown reasons"
            )
            self.kube_watcher = self._make_kube_watcher_dbnd()

    def run_next(self, next_job):
        """

        The run_next command will check the task_queue for any un-run jobs.
        It will then create a unique job-id, launch that job in the cluster,
        and store relevant info in the current_jobs map so we can track the job's
        status
        """
        key, command, kube_executor_config = next_job
        dag_id, task_id, execution_date, try_number = key
        self.log.debug(
            "Kube POD to submit: image=%s with %s",
            self.kube_config.kube_image,
            str(next_job),
        )

        dr = try_get_databand_run()
        task_run = dr.get_task_run_by_af_id(task_id)
        pod_command = [str(c) for c in command]
        task_engine = task_run.task_engine  # type: KubernetesEngineConfig
        pod = task_engine.build_pod(
            task_run=task_run,
            cmds=pod_command,
            labels={
                "airflow-worker": self.worker_uuid,
                "dag_id": self._make_safe_label_value(dag_id),
                "task_id": self._make_safe_label_value(task_run.task_af_id),
                "execution_date": self._datetime_to_label_safe_datestring(
                    execution_date
                ),
                "try_number": str(try_number),
            },
            try_number=try_number,
            include_system_secrets=True,
        )

        pod_ctrl = self.kube_dbnd.get_pod_ctrl_for_pod(pod)
        self.running_pods[pod.name] = key
        self.pod_to_task_run[pod.name] = task_run

        pod_ctrl.run_pod(pod=pod, task_run=task_run, detach_run=True)
        self.metrics_logger.log_pod_started(task_run.task)

    def delete_pod(self, pod_id):
        # we will try to delete pod only once

        self.running_pods.pop(pod_id, None)
        task_run = self.pod_to_task_run.pop(pod_id, None)
        if not task_run:
            return

        try:
            self.metrics_logger.log_pod_finished(task_run.task)
        except Exception:
            # Catch all exceptions to prevent any delete loops, best effort
            logger.exception("Failed to save pod finish info: pod_name=%s.!", pod_id)

        try:
            result = self.kube_dbnd.delete_pod(pod_id, self.namespace)
            return result
        except Exception:
            # Catch all exceptions to prevent any delete loops, best effort
            logger.exception(
                "Exception raised when trying to delete pod: pod_name=%s.", pod_id
            )

    def terminate(self):
        # we kill watcher and communication channel first

        # prevent watcher bug of being stacked on termination during event processing
        try:
            self.kube_watcher.safe_terminate()
            super(DbndKubernetesScheduler, self).terminate()
        finally:
            self._terminate_all_running_pods()

    def _terminate_all_running_pods(self):
        """
        Clean up of all running pods on terminate:
        """
        # now we need to clean after the run
        pods_to_delete = sorted(list(self.pod_to_task_run.items()))
        if not pods_to_delete:
            return

        logger.info(
            "Terminating run, deleting all %d submitted pods that are still running.",
            len(pods_to_delete),
        )
        for pod_name, task_run in pods_to_delete:
            try:
                self.delete_pod(pod_name)
            except Exception:
                logger.exception("Failed to terminate pod %s", pod_name)

        # Wait for pods to be deleted and execute their own state management
        logger.info("Scheduler: Setting all running pods to cancelled in 10 seconds...")
        time.sleep(10)
        try:
            for pod_name, task_run in pods_to_delete:
                self._dbnd_set_task_cancelled_on_termination(pod_name, task_run)
        except Exception:
            logger.exception("Could not set pods to cancelled!")

    def _dbnd_set_task_cancelled_on_termination(self, pod_name, task_run):
        if task_run.task_run_state in TaskRunState.final_states():
            logger.info(
                "pod %s was %s, not setting to cancelled",
                pod_name,
                task_run.task_run_state,
            )
            return
        task_run.set_task_run_state(TaskRunState.CANCELLED)

    def process_watcher_task(self, task):
        """Process the task by watcher."""
        pod_id, state, labels, resource_version = task
        pod_name = pod_id
        self.log.debug(
            "k8s scheduler: Attempting to process pod; pod_name: %s; state: %s; labels: %s",
            pod_id,
            state,
            labels,
        )
        key = self._labels_to_key(labels=labels)
        if not key:
            logger.info(
                "k8s scheduler: Can't find a key for event from %s - %s from labels %s, skipping",
                pod_name,
                state,
                labels,
            )
            return

        task_run = self.pod_to_task_run.get(pod_name)
        if not task_run:
            logger.info(
                "k8s scheduler: Can't find a task run for event from %s - %s, skipping",
                pod_name,
                state,
            )
            return

        self.log.debug(
            "k8s scheduler: Attempting to process pod; pod_name: %s; state: %s; labels: %s",
            pod_id,
            state,
            labels,
        )

        if state == State.RUNNING:
            logger.info("k8s scheduler: event: %s is Running", pod_name)
            self._dbnd_set_task_running(task_run, pod_name=pod_name)
            # we will not send event to executor (otherwise it will delete the running pod)
        elif state is None:
            # simple case, pod has success - will be proceed by airflow main scheduler (Job)
            self._dbnd_set_task_success(pod_name=pod_name, task_run=task_run)

            self.result_queue.put((key, state, pod_name, resource_version))
        elif state == State.FAILED:
            self._dbnd_set_task_failed(pod_name=pod_name, task_run=task_run)
            self.result_queue.put((key, state, pod_id, resource_version))
        else:
            self.log.debug(
                "k8s scheduler: finishing job %s - %s (%s)", key, state, pod_id
            )
            self.result_queue.put((key, state, pod_id, resource_version))

    def _dbnd_set_task_running(self, task_run, pod_name):

        pod_data = self.get_pod_status(pod_name)
        if not pod_data:
            logger.error(
                "Failed to proceed Running event for %s: can't find pod info", pod_name
            )
            return
        node_name = pod_data.spec.node_name
        if not node_name:
            return

        self.metrics_logger.log_pod_running(
            task_run.task, pod_name, node_name=node_name
        )

    def _dbnd_set_task_success(self, task_run, pod_name):
        logger.debug("Getting task run")

        if task_run.task_run_state == TaskRunState.SUCCESS:
            logger.info("Skipping 'success' event from %s", pod_name)
            return

        # we print success message to the screen
        # we will not send it to databand tracking store
        task_run.set_task_run_state(TaskRunState.SUCCESS, track=False)
        logger.info(
            "Task %s has been completed at pod '%s'!"
            % (task_run.task.task_name, pod_name)
        )

    def _dbnd_set_task_failed(self, task_run, pod_name):

        if task_run.task_run_state == TaskRunState.FAILED:
            logger.info("Skipping 'failure' event from %s", pod_name)
            return

        task_id = task_run.task_af_id
        pod_data = self.get_pod_status(pod_name)
        pod_ctrl = self.kube_dbnd.get_pod_ctrl(pod_name, self.namespace)
        error_msg_header = "Pod %s at %s has failed!" % (pod_name, self.namespace)

        failure_reason, failure_message = self._find_pod_failure_reason(
            task_run=task_run, pod_data=pod_data, pod_name=pod_name
        )
        if failure_reason:
            error_msg_header += "%s: %s." % (failure_reason, failure_message)

        pod_logs = []
        if pod_data:
            pod_status_log = _get_status_log_safe(pod_data)
            pod_phase = pod_data.status.phase
            if pod_phase != "Pending":
                pod_logs = pod_ctrl.get_pod_logs()
        else:
            pod_status_log = "POD NOT FOUND"

        error_msg_desc = "Please see full pod log for more details\n%s" % pod_status_log

        from dbnd._core.task_run.task_run_error import TaskRunError

        ti_state = get_airflow_task_instance_state(task_run=task_run)
        logger.info(
            "k8s scheduler: current task airflow state: %s %s ", task_id, ti_state
        )

        if pod_logs:
            error_msg_desc += "\nPod logs:\n%s\n" % "\n".join(
                ["out: %s" % l for l in pod_logs[-20:]]
            )
        error = TaskRunError.build_from_message(
            task_run=task_run, msg=error_msg_header, help_msg=error_msg_desc,
        )

        if ti_state == State.FAILED:
            # let just notify the error, so we can show it in summary it
            # we will not send it to databand tracking store
            task_run.set_task_run_state(TaskRunState.FAILED, track=False, error=error)
            logger.info(
                "%s",
                task_run.task.ctrl.banner(
                    "Task %s has failed at pod '%s'!"
                    % (task_run.task.task_name, pod_name),
                    color="red",
                    task_run=task_run,
                ),
            )
            return True
        logger.info("k8s scheduler: got crashed pod %s for %s", pod_name, task_id)
        # we got State.Failed from watcher, but at DB airflow instance in different state
        # that means the task has failed in the middle
        # (all kind of errors and exit codes)

        task_run_log = error_msg_header
        task_run_log += pod_status_log
        task_run_log += "Airflow state at DB:%s\n" % ti_state
        if pod_logs:
            # let's upload it logs - we don't know what happen
            task_run_log += "\nPod logs:\n\n%s\n\n" % "\n".join(pod_logs)
        task_run.tracker.save_task_run_log(error_msg_header)

        retry_config = self.kube_dbnd.engine_config.pod_retry_config
        retry_count = retry_config.get_retry_count(failure_reason)

        # update retry for the latest values (we don't have
        task_run.task.task_retries = retry_count

        error_msg = "%s failed with %s: %s." % (
            pod_name,
            failure_reason,
            failure_message,
        )
        af_state = self._handle_crashed_task_failure(error=error_msg, task_run=task_run)
        if af_state == State.UP_FOR_RETRY:
            task_run.set_task_run_state(
                TaskRunState.UP_FOR_RETRY, track=True, error=error
            )
        else:
            task_run.set_task_run_state(TaskRunState.FAILED, track=True, error=error)

    def _find_pod_failure_reason(
        self, task_run, pod_name, pod_data,
    ):
        if not pod_data:
            return (
                PodRetryReason.err_pod_deleted,
                "Pod %s probably has been deleted (can not be found)" % pod_name,
            )

        pod_phase = pod_data.status.phase
        pod_ctrl = self.kube_dbnd.get_pod_ctrl(name=pod_name)

        if pod_phase == "Pending":
            logger.info(
                "Got pod %s at Pending state which is failing: looking for the reason..",
                pod_name,
            )
            try:
                pod_ctrl.check_deploy_errors(pod_data)
            except KubernetesImageNotFoundError as ex:
                return PodRetryReason.err_image_pull, str(ex)
            except Exception as ex:
                pass
            return None, None

        if pod_data.metadata.deletion_timestamp:
            return (
                PodRetryReason.err_pod_deleted,
                "Pod %s has been deleted at %s"
                % (pod_name, pod_data.metadata.deletion_timestamp),
            )

        pod_exit_code = _try_get_pod_exit_code(pod_data)
        if pod_exit_code:
            logger.info("Found pod exit code %d for pod %s", pod_exit_code, pod_name)
            pod_exit_code = str(pod_exit_code)
            return pod_exit_code, "Pod exit code %s" % pod_exit_code
        return None, None

    @provide_session
    def _handle_crashed_task_failure(self, task_run, error, session=None):
        task_instance = get_airflow_task_instance(task_run, session=session)
        task_instance.task = task_run.task.ctrl.airflow_op
        task_instance.task.retries = task_run.task.task_retries
        task_instance.max_tries = task_run.task.task_retries

        logger.info(
            "k8s scheduler: retries %s  task: %s",
            task_instance.max_tries,
            task_instance.task.retries,
        )
        # retry condition: self.task.retries and self.try_number <= self.max_tries
        increase_try_number = False

        if task_instance.state == State.QUEUED:
            # Special case - no airflow code has been run in the pod at all.
            # Must increment try number
            increase_try_number = True

        task_instance.handle_failure(error, session=session)

        if increase_try_number:
            task_instance._try_number += 1
            logger.info(
                "k8s scheduler: increasing try number for %s to %s",
                task_instance.task_id,
                task_instance._try_number,
            )
            session.merge(task_instance)
            session.commit()
        return task_instance.state

    def sync(self):
        super(DbndKubernetesScheduler, self).sync()

        # DBND-AIRFLOW: doing more validations during the sync
        # we do it after kube_scheduler sync, so all "finished pods" are removed already
        # but no new pod requests are submitted
        self.current_iteration += 1

        if self.current_iteration % 10 == 0:
            try:
                self.handle_disappeared_pods()
            except Exception:
                logger.exception("Failed to find disappeared pods")

    #######
    # HANDLE DISAPPEARED PODS
    #######
    def handle_disappeared_pods(self):
        pods = self.__find_disappeared_pods()
        if not pods:
            return
        logger.info(
            "Pods %s can not be found for the last 2 iterations of disappeared pods recovery. "
            "Trying to recover..",
            pods,
        )
        for pod_name in pods:
            task_run = self.pod_to_task_run.get(pod_name)
            key = self.running_pods.get(pod_name)
            self._dbnd_set_task_failed(task_run=task_run, pod_name=pod_name)
            self.result_queue.put((key, State.FAILED, pod_name, None))

    def __find_disappeared_pods(self):
        """
        We will want to check on pod status.
        K8s may have pods disappeared from it because of preemptable/spot nodes
         without proper event sent to KubernetesJobWatcher
        We will do a check on all running pods every 10th iteration
        :return:
        """
        if not self.running_pods:
            self.last_disappeared_pods = {}
            logger.info(
                "Skipping on checking on disappeared pods - no pods are running"
            )
            return

        logger.info(
            "Checking on disappeared pods for currently %s running tasks",
            len(self.running_pods),
        )

        previously_disappeared_pods = self.last_disappeared_pods
        currently_disappeared_pods = {}
        running_pods = list(
            self.running_pods.items()
        )  # need a copy, will be modified on delete
        disapeared_pods = []
        for pod_name, pod_key in running_pods:
            from kubernetes.client.rest import ApiException

            try:
                self.kube_client.read_namespaced_pod(
                    name=pod_name, namespace=self.namespace
                )
            except ApiException as e:
                # If the pod can not be found
                if e.status == 404:
                    logger.info("Pod %s has disappeared...", pod_name)
                    if pod_name in previously_disappeared_pods:
                        disapeared_pods.append(pod_name)
                    currently_disappeared_pods[pod_name] = pod_key
            except Exception:
                logger.exception("Failed to get status of pod_name=%s", pod_name)
        self.last_disappeared_pods = currently_disappeared_pods

        if disapeared_pods:
            logger.info("Disappeared pods: %s ", disapeared_pods)
        return disapeared_pods

    def get_pod_status(self, pod_name):
        pod_ctrl = self.kube_dbnd.get_pod_ctrl(name=pod_name)
        return pod_ctrl.get_pod_status_v1()