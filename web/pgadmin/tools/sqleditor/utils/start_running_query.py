##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2025, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""Start executing the query in async mode."""

import pickle
import secrets
from threading import Thread
from flask import Response, current_app, copy_current_request_context
from flask_babel import gettext

from config import PG_DEFAULT_DRIVER
from pgadmin.tools.sqleditor.utils.apply_explain_plan_wrapper import \
    apply_explain_plan_wrapper_if_needed
from pgadmin.tools.sqleditor.utils.constant_definition import TX_STATUS_IDLE, \
    TX_STATUS_INERROR
from pgadmin.tools.sqleditor.utils.is_begin_required import is_begin_required
from pgadmin.tools.sqleditor.utils.update_session_grid_transaction import \
    update_session_grid_transaction
from pgadmin.utils.ajax import make_json_response, internal_server_error
from pgadmin.utils.driver import get_driver
from pgadmin.utils.exception import ConnectionLost, SSHTunnelConnectionLost,\
    CryptKeyMissing
from pgadmin.utils.constants import ERROR_MSG_TRANS_ID_NOT_FOUND


class StartRunningQuery:

    def __init__(self, blueprint_object, logger):
        self.http_session = None
        self.blueprint_object = blueprint_object
        self.connection_id = str(secrets.choice(range(1, 9999999)))
        self.logger = logger

    def execute(self, sql_text, trans_id, http_session, connect=False):
        """
        Execute a SQL query in the context of a specific session and transaction.

        Args:
            sql_text (str): The SQL query string.
            trans_id (int): Transaction identifier.
            http_session (Session): HTTP session object.
            connect (bool): Whether to connect if not already connected.

        Returns:
            Response: A JSON response with execution result, editability, notifies, etc.
        """
        # Retrieve session data from the cache/store
        sess = StartRunningQuery.retrieve_session_information(http_session, trans_id)
        if isinstance(sess, Response):
            return sess

        # Purge previous PK/OID metadata if present
        sess.pop('primary_keys', None)
        sess.pop('oids', None)

        # Load the transaction state from pickled command object
        txn_obj = pickle.loads(sess['command_obj'])

        # Defaults
        editable = False
        filterable = False
        server_notifies = None
        query_status = -1
        query_result = None

        if txn_obj is not None and sess is not None:
            # Reset row counter on fresh execution
            txn_obj.update_fetched_row_cnt(0)
            self.__retrieve_connection_id(txn_obj)

            try:
                conn_mgr = get_driver(PG_DEFAULT_DRIVER).connection_manager(txn_obj.sid)
                conn_obj = conn_mgr.connection(
                    did=txn_obj.did,
                    conn_id=self.connection_id,
                    auto_reconnect=False,
                    use_binary_placeholder=True,
                    array_to_string=True,
                    **({"database": txn_obj.dbname} if hasattr(txn_obj, 'dbname') else {})  
                )
            except (ConnectionLost, SSHTunnelConnectionLost, CryptKeyMissing):
                raise
            except Exception as ex:
                self.logger.error(ex)
                return internal_server_error(errormsg=str(ex))

            # Connect if necessary
            if connect and not conn_obj.connected():
                from pgadmin.tools.sqleditor.utils import query_tool_connection_check

                _, _, _, _, _, reconnect_resp = query_tool_connection_check(trans_id)  
                if reconnect_resp is not None:
                    return reconnect_resp

            # Apply explain plan if needed
            wrapped_sql = apply_explain_plan_wrapper_if_needed(conn_mgr, sql_text) 

            # Perform actual execution
            self.__execute_query(
                conn_obj,
                sess,
                wrapped_sql,
                trans_id,
                txn_obj
            )

            editable = txn_obj.can_edit()
            filterable = txn_obj.can_filter()
            server_notifies = conn_obj.get_notifies()

        else:
            query_status = False
            query_result = gettext('Either transaction object or session object not found.')

        return make_json_response(
            data={
                'status': query_status,
                'result': query_result,
                'can_edit': editable,
                'can_filter': filterable,
                'notifies': server_notifies,
            }
    )


    def __retrieve_connection_id(self, trans_obj):
        conn_id = trans_obj.conn_id
        # if conn_id is None then we will have to create a new connection
        if conn_id is not None:
            self.connection_id = conn_id

    def __execute_query(self, conn, session_obj, sql, trans_id, trans_obj):
        """
        Internal method to manage the execution of an SQL statement
        within a tracked transaction context.
        """

        # Assign connection ID to the transaction object if supported
        if hasattr(trans_obj, 'set_connection_id'):
            trans_obj.set_connection_id(self.connection_id)

        # Store the transaction context in the session
        StartRunningQuery.save_transaction_in_session(
            session_obj, trans_id, trans_obj
        )

        # Evaluate whether a BEGIN is required before execution
        if StartRunningQuery.is_begin_required_for_sql_query(trans_obj, conn, sql):
            conn.execute_void("BEGIN;")

        # Determine whether a rollback is necessary after execution
        rollback_required = StartRunningQuery.is_rollback_statement_required(trans_obj, conn)

        # Inner function for executing the query asynchronously within app context
        @copy_current_request_context
        def async_execute(conn, sql_text, txn_obj, rollback_flag, flask_app):
            with flask_app.app_context():
                try:
                    _, _ = conn.execute_async(sql_text)
                    if rollback_flag:
                        conn.execute_void("ROLLBACK;")
                except Exception as execution_err:
                    self.logger.error(execution_err)
                    return internal_server_error(errormsg=str(execution_err))

        # Launch query in a new thread
        thread = QueryThread(
            target=async_execute,
            args=(
                conn,
                sql,
                trans_obj,
                rollback_required,
                current_app._get_current_object()
            )
        )
        thread.start()

        thread_id = getattr(thread, 'native_id', thread.ident)
        trans_obj.set_thread_native_id(thread_id)

        # Persist transaction object again to record thread ID
        StartRunningQuery.save_transaction_in_session(
            session_obj, trans_id, trans_obj
        )

    @staticmethod
    def is_begin_required_for_sql_query(trans_obj, conn, sql):
        return (not trans_obj.auto_commit and
                conn.transaction_status() == TX_STATUS_IDLE and
                is_begin_required(sql)
                )

    @staticmethod
    def is_rollback_statement_required(trans_obj, conn):
        return (
            conn.transaction_status() == TX_STATUS_INERROR and
            trans_obj.auto_rollback
        )

    @staticmethod
    def save_transaction_in_session(session, transaction_id, transaction):
        # As we changed the transaction object we need to
        # restore it and update the session variable.
        session['command_obj'] = pickle.dumps(transaction, -1)
        update_session_grid_transaction(transaction_id, session)

    @staticmethod
    def retrieve_session_information(http_session, transaction_id):
        if 'gridData' not in http_session:
            return make_json_response(
                success=0,
                errormsg=ERROR_MSG_TRANS_ID_NOT_FOUND,
                info='DATAGRID_TRANSACTION_REQUIRED', status=404
            )
        grid_data = http_session['gridData']
        # Return from the function if transaction id not found
        if str(transaction_id) not in grid_data:
            return make_json_response(
                success=0,
                errormsg=ERROR_MSG_TRANS_ID_NOT_FOUND,
                info='DATAGRID_TRANSACTION_REQUIRED',
                status=404
            )
        # Fetch the object for the specified transaction id.
        # Use pickle.loads function to get the command object
        return grid_data[str(transaction_id)]


class QueryThread(Thread):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.app = current_app._get_current_object()

    def run(self):
        with self.app.app_context():
            super().run()
