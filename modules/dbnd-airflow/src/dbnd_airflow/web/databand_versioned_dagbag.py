from __future__ import absolute_import

import logging

import pendulum

from airflow.models import DagBag, DagModel, DagPickle
from airflow.utils.db import provide_session

from dbnd_airflow.airflow_override import DbndAirflowTaskInstance


logger = logging.getLogger(__name__)


DAG_UNPICKABLE_PROPERTIES = (
    "_log",
    ("user_defined_macros", {}),
    ("user_defined_filters", {}),
    ("params", {}),
)


class DbndDagModel(DagModel):
    def get_dag(self, store_serialized_dags=False):
        # DBND PATCH
        # unwrap all old logic, as we have recursion call there that makes it not easy to patch

        dag = DagBag(
            dag_folder=self.fileloc, store_serialized_dags=store_serialized_dags
        ).get_dag(self.dag_id)

        if store_serialized_dags and dag is None:
            dag = DagBag(dag_folder=self.fileloc, store_serialized_dags=False).get_dag(
                self.dag_id
            )
        if dag:
            return dag

        from airflow.www_rbac.views import dagbag

        return dagbag.get_dag(dag_id=self.dag_id)


class DbndAirflowDagBag(DagBag):
    @provide_session
    def get_dag(self, dag_id, from_file_only=True, execution_date=None, session=None):
        """
        :param dag_id:
        :param execution_date: if provided, we'll try to find specifc version of dag (using pickle)
        :param session:
        :return:
        """
        from flask import has_request_context, request, session as flask_session

        # all legacy airflow code works just with dag_id, also, there are some calls that doesn't pass through execution_date
        if has_request_context():
            execution_date = execution_date or request.args.get("execution_date")

            # trick to store last execution date used for the next flask call
            if execution_date:
                logger.debug(
                    "Execution date saved to session: %s, %s", dag_id, execution_date
                )
                flask_session["ed_" + dag_id] = execution_date
            else:
                logger.debug("Execution date from previous session: %s", dag_id)
                execution_date = flask_session.get("ed_" + dag_id)

            if execution_date and execution_date != "undefined":
                # we are going to return most "active" dag
                dttm = pendulum.parse(execution_date)
                dag = self._get_pickled_dag_from_dagrun(
                    dag_id=dag_id, execution_date=dttm, session=session
                )
                if dag:
                    return dag

        # we don't have specific dag/execution date, we are trying to get in-memory version
        dag = super(DbndAirflowDagBag, self).get_dag(
            dag_id, from_file_only=from_file_only
        )
        if dag:
            return dag

        # let try to find it latest version in DB
        latest_execution = (
            session.query(DbndAirflowTaskInstance.execution_date)
            .filter(DbndAirflowTaskInstance.task_id == dag_id)
            .order_by(DbndAirflowTaskInstance.execution_date.desc())
            .first()
        )

        if latest_execution:
            return self._get_pickled_dag_from_dagrun(
                dag_id=dag_id,
                execution_date=latest_execution.execution_date,
                session=session,
            )

        return None

    @provide_session
    def _get_pickled_dag_from_dagrun(self, dag_id, execution_date, session=None):

        ti = (
            session.query(
                DbndAirflowTaskInstance.task_id, DbndAirflowTaskInstance.executor_config
            )
            .filter(
                DbndAirflowTaskInstance.dag_id == dag_id,
                DbndAirflowTaskInstance.execution_date == execution_date,
            )
            .first()
        )
        if not ti:
            logger.debug("Failed to find task instance %s %s", dag_id, execution_date)
            return None

        pickled_dag_id = ti.executor_config.get("DatabandExecutor", {}).get(
            "dag_pickle_id", None
        )
        if not pickled_dag_id:
            logger.debug(
                "No dbnd config at %s %s %s, no pickle_id",
                dag_id,
                execution_date,
                ti.task_id,
            )
            return None

        try:
            pickled_dag = (
                session.query(DagPickle)
                .filter(DagPickle.id == pickled_dag_id)
                .one_or_none()
            )
        except Exception as ex:
            logger.error("Error ocured during DAG retrieval from DB, %s", ex)
            return None

        if pickled_dag and pickled_dag.pickle:
            # we found pickled dag
            dag = pickled_dag.pickle
            dag.dag_version_execution_date = execution_date
            # let's add this dag into dags, there is a check that validates if this dag exists..
            self.dags[dag_id] = dag
            return dag
        else:
            logger.debug(
                "Failed to find pickled dag in DB for pickle_id=%s", pickled_dag_id
            )
            # failed to parse dag?
            return None
