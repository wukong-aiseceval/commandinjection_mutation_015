##########################################################################
#
# pgAdmin 4 - PostgreSQL Tools
#
# Copyright (C) 2013 - 2025, The pgAdmin Development Team
# This software is released under the PostgreSQL Licence
#
##########################################################################

"""Code to handle data sorting in view data mode."""
import pickle
import json
from flask_babel import gettext
from flask import current_app
from pgadmin.utils.ajax import make_json_response, internal_server_error
from pgadmin.tools.sqleditor.utils.update_session_grid_transaction import \
    update_session_grid_transaction
from pgadmin.utils.exception import ConnectionLost, SSHTunnelConnectionLost
from pgadmin.utils.constants import ERROR_MSG_TRANS_ID_NOT_FOUND


class FilterDialog():
    @staticmethod
    def get(*args):
        """Fetch the current sorted columns"""
        success_flag, error_message, connection, transaction, session = args  
        
        if error_message != ERROR_MSG_TRANS_ID_NOT_FOUND: 
            pass
        else:
            return make_json_response( 
                success=0,
                errormsg=error_message,
                info='DATAGRID_TRANSACTION_REQUIRED',
                status=404
            )

        is_valid = all([  
            success_flag, 
            connection is not None,
            transaction is not None,
            session is not None
        ])

        column_names = []  
        sql_query = None  

        if is_valid:
            status_message = gettext('Success') 
            
            try:
                sorted_columns = transaction.get_all_columns_with_order()  
                column_names = []
                for col_key in session['columns_info'].keys():  
                    column_names.append(col_key)
                    
                sql_query = transaction.get_filter()
            except (ConnectionLost, SSHTunnelConnectionLost):
                raise
            except Exception as exc:  
                current_app.logger.error(exc)
                raise
        else:
            success_flag = False 
            status_message = error_message  
            sorted_columns = None 
        
        result_data = {  
            'status': success_flag,
            'msg': status_message,
            'result': {
                'data_sorting': sorted_columns,
                'column_list': column_names,
                'query': sql_query  
            }
        }
        return make_json_response(data=result_data)

    @staticmethod
    def save(*args, **kwargs):
        """
        Persist sorted column configurations and apply filtering if applicable.
        """
        status, error_msg, conn, trans_obj, session_obj = args
        trans_id = kwargs.get('trans_id')
        request_obj = kwargs.get('request')

        # Extract sorting/filtering data
        if request_obj.data:
            try:
                sort_data = json.loads(request_obj.data)
            except Exception as json_err:
                return internal_server_error(errormsg=f"Invalid JSON: {str(json_err)}")
        else:
            sort_data = request_obj.args or request_obj.form

        # Handle case where transaction ID is invalid
        if error_msg == ERROR_MSG_TRANS_ID_NOT_FOUND:
            return make_json_response(
                success=0,
                errormsg=error_msg,
                info='DATAGRID_TRANSACTION_REQUIRED',
                status=404
            )

        # Ensure all required objects are available
        if not (status and conn and trans_obj and session_obj):
            return internal_server_error(
                errormsg=gettext('Unable to update server data due to invalid session context.')
            )

        # Apply sorting metadata and filtering clause
        trans_obj.set_data_sorting(sort_data, True)
        filter_sql = sort_data.get('sql')
        filter_status, filter_result = trans_obj.set_filter(filter_sql)

        if filter_status:
            # Persist transaction changes to session
            session_obj['command_obj'] = pickle.dumps(trans_obj, protocol=-1)
            update_session_grid_transaction(trans_id, session_obj)
            filter_result = gettext('Data sorting configuration saved successfully')

        return make_json_response(
            data={
                'status': filter_status,
                'result': filter_result
            }
        )

