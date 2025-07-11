from datetime import datetime
from uuid import uuid4

import requests
from flask import current_app, jsonify, make_response, request

from namex import db, jwt
from namex.constants import NameRequestPatchActions, NameRequestRollbackActions, PaymentState
from namex.models import Event, Payment, Request, State, User
from namex.services import EventRecorder
from namex.services.name_request.exceptions import InvalidInputError, NameRequestException, NameRequestIsInProgressError
from namex.services.name_request.name_request_state import (
    get_nr_state_actions,
    is_name_request_refundable,
    is_request_editable,
)
from namex.services.name_request.utils import get_mapped_entity_and_action_code
from namex.services.payment.payments import get_payment, refund_payment
from namex.services.statistics.wait_time_statistics import WaitTimeStatsService
from namex.utils.api_resource import handle_exception
from namex.utils.auth import cors_preflight, full_access_to_name_request
from namex.utils.queue_util import publish_email_notification

from .api_models import nr_request
from .api_namespace import api
from .base_nr_resource import BaseNameRequestResource
from .constants import contact_editable_states, request_editable_states

MSG_BAD_REQUEST_NO_JSON_BODY = 'No JSON data provided'
MSG_SERVER_ERROR = 'Server Error!'
MSG_NOT_FOUND = 'Resource not found'


@cors_preflight('GET, PUT')
@api.route('/<int:nr_id>', strict_slashes=False, methods=['GET', 'PUT', 'OPTIONS'])
class NameRequestResource(BaseNameRequestResource):
    """Name Request endpoint."""

    @jwt.requires_auth
    @api.doc(
        description='Fetch details and available actions for a name request',
        params={
            'nr_id': 'Internal ID of the name request',
            'org_id': 'Optional organization id',
        },
        responses={
            200: 'Name request fetched successfully',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(self, nr_id):
        try:
            if nr_model := Request.query.get(nr_id):
                org_id = request.args.get('org_id', None)

                headers = {'Authorization': f'Bearer {jwt.get_token_auth_header()}', 'Content-Type': 'application/json'}

                auth_svc_url = current_app.config.get('AUTH_SVC_URL')
                auth_url = f'{auth_svc_url}/orgs/{org_id}/affiliations/{nr_model.nrNum}'
                auth_response = requests.get(url=auth_url, headers=headers)

                if auth_response.status_code == 200:
                    if nr_model.requestTypeCd and (not nr_model.entity_type_cd or not nr_model.request_action_cd):
                        # If requestTypeCd is set, but a request_entity (entity_type_cd) and a request_action (request_action_cd)
                        # are not, use get_mapped_entity_and_action_code to map the values from the requestTypeCd
                        entity_type, request_action = get_mapped_entity_and_action_code(nr_model.requestTypeCd)
                        nr_model.entity_type_cd = entity_type
                        nr_model.request_action_cd = request_action

                    response_data = nr_model.json()

                    # If draft, get the wait time and oldest queued request
                    if nr_model.stateCd == 'DRAFT':
                        service = WaitTimeStatsService()
                        wait_time_response = service.get_waiting_time_dict()
                        response_data.update(wait_time_response)

                    # Add the list of valid Name Request actions for the given state to the response
                    response_data['actions'] = get_nr_state_actions(nr_model.stateCd, nr_model)
                    return make_response(jsonify(response_data), 200)
        except Exception as err:
            current_app.logger.error(repr(err))
            return handle_exception(err, 'Error retrieving the NR.', 500)

    # REST Method Handlers
    @api.expect(nr_request)
    @api.doc(
        description="Update a name request's state and key fields. This endpoint supports state transitions including: "
                    "DRAFT, COND_RESERVE, RESERVED, PENDING_PAYMENT, COND_RESERVE → CONDITIONAL, and RESERVED → APPROVED. "
                    "Use PATCH instead for partial updates or name-only changes. Requires full access to the name request.",
        params={'nr_id': 'Internal ID of the name request'},
        responses={
            200: 'Successfully updated name request',
            403: 'Forbidden',
            400: 'invalid update state or payload',
            500: 'Internal server error'
        },
    )
    def put(self, nr_id):
        try:
            if not full_access_to_name_request(request):
                return {'message': 'You do not have access to this NameRequest.'}, 403
            # Find the existing name request
            nr_model = Request.query.get(nr_id)

            # Creates a new NameRequestService, validates the app config, and sets request_data to the NameRequestService instance
            self.initialize()
            nr_svc = self.nr_service

            nr_svc.nr_num = nr_model.nrNum
            nr_svc.nr_id = nr_model.id

            valid_update_states = [State.DRAFT, State.COND_RESERVE, State.RESERVED, State.PENDING_PAYMENT]

            # This could be moved out, but it's fine here for now
            def validate_put_request(data):
                is_valid = False
                msg = ''
                if data.get('stateCd') in valid_update_states:
                    is_valid = True

                return is_valid, msg

            is_valid_put, validation_msg = validate_put_request(self.request_data)
            validation_msg = validation_msg if not len(validation_msg) > 0 else 'Invalid request for PUT'

            if not is_valid_put:
                raise InvalidInputError(message=validation_msg)

            if nr_model.stateCd in valid_update_states:
                nr_model = self.update_nr(nr_model, nr_model.stateCd, self.handle_nr_update)

                # Record the event
                EventRecorder.record(nr_svc.user, Event.PUT, nr_model, nr_svc.request_data)

            current_app.logger.debug(nr_model.json())
            response_data = nr_model.json()
            # Add the list of valid Name Request actions for the given state to the response
            response_data['actions'] = nr_svc.current_state_actions
            return make_response(jsonify(response_data), 200)
        except NameRequestException as err:
            return handle_exception(err, err.message, 500)
        except Exception as err:
            return handle_exception(err, repr(err), 500)


@cors_preflight('PATCH')
@api.route('/<int:nr_id>/<string:nr_action>', strict_slashes=False, methods=['PATCH', 'OPTIONS'])
class NameRequestFields(BaseNameRequestResource):
    @api.expect(nr_request)
    @api.doc(
        description=(
            'Perform an action or apply partial updates to a name request, depending on the `nr_action` path parameter. '
            'For `EDIT`, only the fields provided in the request body will be updated; all other fields will remain unchanged. '
            'Other actions trigger system-defined logic and may not require a request body:\n'
            '- `CANCEL`: Cancels the name request\n'
            '- `CHECKOUT`: Locks the name request for editing\n'
            '- `CHECKIN`: Unlocks the name request and clears its checkout state\n'
            '- `RESEND`: Resends applicable notifications\n'
            '- `REQUEST_REFUND`: Initiates a refund process if the request is eligible'
        ),
        params={
            'nr_id': 'Internal ID of the name request',
            'nr_action': 'Action to perform. One of: CHECKOUT, CHECKIN, EDIT, CANCEL, RESEND, REQUEST_REFUND',
        },
        responses={
            200: 'Action completed or fields updated successfully',
            400: 'Invalid payload, state transition, or unsupported action',
            403: 'Forbidden',
            423: 'Locked: name request is currently checked out by another user',
            500: 'Internal server error',
        },
    )
    def patch(self, nr_id, nr_action: str):
        try:
            if not full_access_to_name_request(request):
                return {'message': 'You do not have access to this NameRequest.'}, 403

            nr_action = str(
                nr_action
            ).upper()  # Convert to upper-case, just so we can support lower case action strings
            nr_action = (
                NameRequestPatchActions[nr_action].value
                if NameRequestPatchActions.has_value(nr_action)
                else NameRequestPatchActions.EDIT.value
            )

            # Find the existing name request
            nr_model = Request.query.get(nr_id)

            def initialize(_self):
                _self.validate_config(current_app)
                request_json = request.get_json()

                if nr_action:
                    _self.nr_action = nr_action

                if nr_action is NameRequestPatchActions.CHECKOUT.value:
                    # Make sure the NR isn't already checked out
                    if nr_model.checkedOutBy is None:
                        if not is_request_editable(nr_model.stateCd):
                            # the name is in examination
                            raise NameRequestIsInProgressError()
                    elif nr_model.checkedOutBy != request_json.get('checkedOutBy', None):
                        # checked out by another user
                        raise NameRequestIsInProgressError()

                    # set the user id of the request to name_request_service_account
                    service_account_user = User.find_by_username('name_request_service_account')
                    nr_model.userId = service_account_user.id

                    # The request payload will be empty when making this call, add them to the request
                    _self.request_data = {
                        # Doesn't have to be a UUID but this is easy and works for a pretty unique token
                        'checkedOutBy': str(uuid4()),
                        'checkedOutDt': datetime.now(),
                    }
                    # Set the request data to the service
                    _self.nr_service.request_data = self.request_data
                elif nr_action is NameRequestPatchActions.CHECKIN.value:
                    # The request payload will be empty when making this call, add them to the request
                    _self.request_data = {'checkedOutBy': None, 'checkedOutDt': None}
                    # Set the request data to the service
                    _self.nr_service.request_data = self.request_data
                elif nr_action is NameRequestPatchActions.REQUEST_REFUND.value and not is_name_request_refundable(
                    nr_model.stateCd
                ):
                    # the NR can be cancelled and refund when state_cd = DRAFT
                    raise NameRequestIsInProgressError()
                else:
                    super().initialize()

            initialize(self)

            nr_svc = self.nr_service
            nr_svc.nr_num = nr_model.nrNum
            nr_svc.nr_id = nr_model.id

            # This could be moved out, but it's fine here for now
            def validate_patch_request(data):
                # Use the NR model state as the default, as the state change may not be included in the PATCH request
                request_state = data.get('stateCd', nr_model.stateCd)
                is_valid = False
                msg = ''

                # Handles updates if the NR state is 'patchable'
                if request_state in request_editable_states:
                    is_valid = True
                elif request_state in contact_editable_states:
                    is_valid = True
                else:
                    msg = (
                        'Invalid state change requested - the Name Request state cannot be changed to ['
                        + data.get('stateCd', '')
                        + ']'
                    )

                # Check the action, make sure it's valid
                if not NameRequestPatchActions.has_value(nr_action):
                    is_valid = False
                    msg = (
                        'Invalid Name Request PATCH action, please use one of ['
                        + ', '.join([action.value for action in NameRequestPatchActions])
                        + ']'
                    )
                return is_valid, msg

            is_valid_patch, validation_msg = validate_patch_request(self.request_data)
            validation_msg = validation_msg if not len(validation_msg) > 0 else 'Invalid request for PATCH'

            if not is_valid_patch:
                raise InvalidInputError(message=validation_msg)

            def handle_patch_actions(action, model):
                return {
                    NameRequestPatchActions.CHECKOUT.value: self.handle_patch_checkout,
                    NameRequestPatchActions.CHECKIN.value: self.handle_patch_checkin,
                    NameRequestPatchActions.EDIT.value: self.handle_patch_edit,
                    NameRequestPatchActions.CANCEL.value: self.handle_patch_cancel,
                    NameRequestPatchActions.RESEND.value: self.handle_patch_resend,
                    NameRequestPatchActions.REQUEST_REFUND.value: self.handle_patch_request_refund,
                }.get(action)(model)

            # This handles updates if the NR state is 'patchable'
            nr_model = handle_patch_actions(nr_action, nr_model)

            current_app.logger.debug(nr_model.json())
            response_data = nr_model.json()

            # Don't return the whole response object if we're checking in or checking out
            if nr_action == NameRequestPatchActions.CHECKOUT.value:
                response_data = {
                    'id': nr_id,
                    'checkedOutBy': response_data.get('checkedOutBy'),
                    'checkedOutDt': response_data.get('checkedOutDt'),
                    'state': response_data.get('state', ''),
                    'stateCd': response_data.get('stateCd', ''),
                    'actions': nr_svc.current_state_actions,
                }
                return make_response(jsonify(response_data), 200)

            if nr_action == NameRequestPatchActions.CHECKIN.value:
                response_data = {
                    'id': nr_id,
                    'state': response_data.get('state', ''),
                    'stateCd': response_data.get('stateCd', ''),
                    'actions': nr_svc.current_state_actions,
                }
                return make_response(jsonify(response_data), 200)

            # Add the list of valid Name Request actions for the given state to the response
            response_data['actions'] = nr_svc.current_state_actions
            return make_response(jsonify(response_data), 200)

        except NameRequestIsInProgressError as err:
            # Might as well use the Mozilla WebDAV HTTP Locked status, it's pretty close
            return handle_exception(err, err.message, 423)
        except NameRequestException as err:
            return handle_exception(err, err.message, 500)
        except Exception as err:
            return handle_exception(err, repr(err), 500)

    def handle_patch_checkout(self, nr_model: Request):
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, State.INPROGRESS, self.handle_nr_patch)

        EventRecorder.record(nr_svc.user, Event.PATCH + ' [checkout]', nr_model, {})
        return nr_model

    def handle_patch_checkin(self, nr_model: Request):
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, State.DRAFT, self.handle_nr_patch)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [checkin]', nr_model, {})

        return nr_model

    def handle_patch_edit(self, nr_model: Request):
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, nr_model.stateCd, self.handle_nr_patch)
        # Commit the transaction to ensure changes are saved
        db.session.commit()

        # Refresh the nr_model to ensure the changes are reflected
        db.session.refresh(nr_model)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [edit]', nr_model, nr_svc.request_data)

        return nr_model

    def handle_patch_resend(self, nr_model: Request):
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, nr_model.stateCd, self.handle_nr_patch)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [re-send]', nr_model, nr_svc.request_data)

        return nr_model

    def handle_patch_cancel(self, nr_model: Request):
        """
        Cancel the Name Request.
        :param nr_model:
        :return:
        """
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, State.CANCELLED, self.handle_nr_patch)

        # This handles the updates for Solr, if necessary
        nr_model = self.update_solr(nr_model)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [cancel]', nr_model, nr_svc.request_data)

        return nr_model

    def handle_patch_request_refund(self, nr_model: Request):
        """
        Can the NR and request a refund for ALL associated Name Request payments.
        :param nr_model:
        :return:
        """
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, State.REFUND_REQUESTED, self.handle_nr_patch)

        # Handle the payments
        valid_states = [PaymentState.APPROVED.value, PaymentState.COMPLETED.value, PaymentState.PARTIAL.value]

        refund_value = 0

        # Check for NR that has been renewed - do not refund any payments.
        # UI should not order refund for an NR renewed/reapplied it.
        if not any(
            payment.payment_action == Payment.PaymentActions.REAPPLY.value for payment in nr_model.payments.all()
        ):
            # Try to refund all payments associated with the NR
            for payment in nr_model.payments.all():
                if payment.payment_status_code in valid_states:
                    payment_response = get_payment(payment.payment_token)

                    # Some refunds may fail. Some payment methods are not refundable and return HTTP 400 at the refund.
                    # The refund status is checked from the payment_response and a appropriate message is displayed by the UI.
                    # Skip REFUND for staff no fee payment.
                    if payment_response.total != 0:
                        refund_payment(payment.payment_token, {})
                    payment.payment_status_code = PaymentState.REFUND_REQUESTED.value
                    payment.save_to_db()
                    refund_value += (
                        payment_response.receipts[0]['receiptAmount'] if len(payment_response.receipts) else 0
                    )

        publish_email_notification(nr_model.nrNum, 'refund', '{:.2f}'.format(refund_value))

        # This handles the updates for Solr, if necessary
        nr_model = self.update_solr(nr_model)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [request-refund]', nr_model, nr_model.json())

        return nr_model


@cors_preflight('PATCH')
@api.route('/<int:nr_id>/rollback/<string:action>', strict_slashes=False, methods=['PATCH', 'OPTIONS'])
@api.doc(
    params={
        'nr_id': 'NR Number - This field is required',
    }
)
class NameRequestRollback(BaseNameRequestResource):
    @api.expect(nr_request)
    @api.doc(
        description=(
            'Rollback a name request to a stable, usable state after a frontend or processing error. '
            'This endpoint is intended for internal recovery workflows and should only be used when a name request '
            'has been left in an inconsistent or blocked state due to system failure or UI error.'
        ),
        params={
            'nr_id': 'Internal ID of the name request',
            'action': 'Rollback action to perform',
        },
        responses={
            200: 'Rollback completed successfully',
            400: 'Invalid rollback action or request payload',
            403: 'Forbidden',
            500: 'Internal server error',
        },
    )
    def patch(self, nr_id, action):
        try:
            if not full_access_to_name_request(request):
                return {'message': 'You do not have access to this NameRequest.'}, 403

            # Find the existing name request
            nr_model = Request.query.get(nr_id)

            # Creates a new NameRequestService, validates the app config, and sets request_data to the NameRequestService instance
            self.initialize()
            nr_svc = self.nr_service

            nr_svc.nr_num = nr_model.nrNum
            nr_svc.nr_id = nr_model.id

            # This could be moved out, but it's fine here for now
            def validate_patch_request(data):
                # TODO: Validate the data payload
                # Use the NR model state as the default, as the state change may not be included in the PATCH request
                is_valid = False
                msg = ''
                # This handles updates if the NR state is 'patchable'
                if NameRequestRollbackActions.has_value(action):
                    is_valid = True
                else:
                    msg = 'Invalid rollback action'

                return is_valid, msg

            is_valid_patch, validation_msg = validate_patch_request(self.request_data)
            validation_msg = validation_msg if not len(validation_msg) > 0 else 'Invalid request for PATCH'

            if not is_valid_patch:
                raise InvalidInputError(message=validation_msg)

            # This handles updates if the NR state is 'patchable'
            nr_model = self.handle_patch_rollback(nr_model, action)

            current_app.logger.debug(nr_model.json())
            response_data = nr_model.json()
            # Add the list of valid Name Request actions for the given state to the response
            response_data['actions'] = nr_svc.current_state_actions
            return make_response(jsonify(response_data), 200)

        except NameRequestException as err:
            return handle_exception(err, err.message, 500)
        except Exception as err:
            return handle_exception(err, repr(err), 500)

    def handle_patch_rollback(self, nr_model: Request, action: str):
        """
        Roll back the Name Request.
        :param nr_model:
        :param action:
        :return:
        """
        nr_svc = self.nr_service

        # This handles updates if the NR state is 'patchable'
        nr_model = self.update_nr(nr_model, State.CANCELLED, self.handle_nr_patch)

        # Delete in solr for temp or real NR because it is cancelled
        if nr_model.entity_type_cd in [
            'CR',
            'UL',
            'BC',
            'CP',
            'PA',
            'XCR',
            'XUL',
            'XCP',
            'CC',
            'FI',
            'XCR',
            'XUL',
            'XCP',
        ]:
            SOLR_CORE = 'possible.conflicts'
            self.delete_solr_doc(SOLR_CORE, nr_model.nrNum)

        # Record the event
        EventRecorder.record(nr_svc.user, Event.PATCH + ' [rollback]', nr_model, nr_model.json())

        return nr_model
