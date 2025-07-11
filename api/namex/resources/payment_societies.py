import copy

from flask import current_app, jsonify, make_response, request
from flask_jwt_oidc.exceptions import AuthError
from flask_restx import Namespace, cors, fields
from sqlalchemy.orm.exc import NoResultFound

from namex import jwt
from namex.models import PaymentSociety as PaymentSocietyDAO
from namex.models import Request as RequestDAO
from namex.models import State, User
from namex.resources.name_requests.abstract_nr_resource import AbstractNameRequestResource
from namex.utils.auth import cors_preflight

# Register a local namespace for the payment_society
api = Namespace('Payment Society', description='Manage payment records for societies')

@api.errorhandler(AuthError)
def handle_auth_error(ex):
    return {'message': 'Unauthorized', 'details': ex.error.get('description') or 'Invalid or missing token'}, 401

# Swagger input model for POST payload
payment_society_payload = api.model('PaymentSocietyPayload', {
    'nrNum': fields.String(required=True, description='Name Request number (e.g., NR1234567)'),
    'corpNum': fields.String(required=False, description='Corporation number'),
    'paymentCompletionDate': fields.DateTime(required=False, description='Payment completion timestamp (ISO format)'),
    'paymentStatusCode': fields.String(required=False, description='Status code for payment'),
    'paymentFeeCode': fields.String(required=False, description='Fee code used'),
    'paymentType': fields.String(required=False, description='Type of payment'),
    'paymentAmount': fields.Float(required=False, description='Payment amount in dollars'),
    'paymentJson': fields.Raw(required=False, description='Raw payment metadata (JSON object)'),
    'paymentAction': fields.String(required=False, description='Action taken (e.g., create, refund)')
})


@cors_preflight('GET')
@api.route('/<string:nr>', methods=['GET', 'OPTIONS'])
class PaymentSocietiesSearch(AbstractNameRequestResource):
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR, User.SYSTEM])
    @api.doc(
        description='Fetch payment transaction history for a society name request',
        params={'nr': 'Name Request number'},
        responses={
            200: 'Payment history fetched successfully',
            401: 'Unauthorized',
            404: 'Name request or payment record not found',
            500: 'Internal server error',
        },
    )
    def get(nr):
        try:
            current_app.logger.debug(nr)
            nrd = RequestDAO.query.filter_by(nrNum=nr).first()
            if not nrd:
                return make_response(jsonify({'message': 'Request: {} not found in requests table'.format(nr)}), 404)
        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request: {} not found in requests table'.format(nr)}), 404)
        except Exception as err:
            current_app.logger.error('Error when getting NR: {0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        try:
            psd = PaymentSocietyDAO.query.filter_by(nrNum=nr).first()
            if not psd:
                return make_response(
                    jsonify({'message': 'Request: {} not found in payment_societies table'.format(nr)}), 404
                )
        except NoResultFound as nrf:
            # not an error we need to track in the log
            return make_response(
                jsonify({'message': 'Request: {0} not found in payment_societies table, Err:{1}'.format(nr, nrf)}), 404
            )
        except Exception as err:
            current_app.logger.error('Error when patching NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        paymentSociety_results = PaymentSocietyDAO.query.filter_by(nrNum=nr).order_by('id').all()

        # info needed for each payment_society
        nr_payment_society_info = {}
        payment_society_txn_history = []

        for ps in paymentSociety_results:
            nr_payment_society_info['id'] = ps.id
            nr_payment_society_info['nr_num'] = ps.nrNum
            nr_payment_society_info['corp_num'] = ps.corpNum
            nr_payment_society_info['payment_completion_date'] = ps.paymentCompletionDate
            nr_payment_society_info['payment_status_code'] = ps.paymentStatusCode
            nr_payment_society_info['payment_fee_code'] = ps.paymentFeeCode
            nr_payment_society_info['payment_type'] = ps.paymentType
            nr_payment_society_info['payment_amount'] = ps.paymentAmount
            nr_payment_society_info['payment_json'] = ps.paymentJson
            nr_payment_society_info['payment_action'] = ps.paymentAction

            payment_society_txn_history.insert(0, copy.deepcopy(nr_payment_society_info))
        if len(payment_society_txn_history) == 0:
            return make_response(jsonify({'message': f'No valid payment societies for {nr} found'}), 404)

        resp = {'response': {'count': len(payment_society_txn_history)}, 'transactions': payment_society_txn_history}

        return make_response(jsonify(resp), 200)


@cors_preflight('POST')
@api.route('', methods=['POST', 'OPTIONS'])
class PaymentSocieties(AbstractNameRequestResource):
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR, User.SYSTEM])
    @api.expect(payment_society_payload)
    @api.doc(
        description='Creates a payment record for a society name request',
        responses={
            200: 'Payment record created successfully',
            400: 'Invalid request payload',
            401: 'Unauthorized',
            404: 'Name request not found',
            406: 'Missing NR number in request',
            500: 'Internal server error',
        },
    )
    def post(self):
        # do the cheap check first before the more expensive ones
        try:
            json_input = request.get_json()
            if not json_input:
                return make_response(jsonify({'message': 'No input data provided'}), 400)
            current_app.logger.debug(f'Request Json: {json_input}')

            nr_num = json_input.get('nrNum', None)
            if not nr_num:
                return make_response(jsonify({'message': 'nr_num not set in json input'}), 406)

            nrd = RequestDAO.find_by_nr(nr_num)
            if not nrd:
                return make_response(
                    jsonify({'message': 'Request: {} not found in requests table'.format(nr_num)}), 404
                )

            # replacing temp NR number to a formal NR number if needed.
            nrd = self.add_new_nr_number(nrd, False)
            current_app.logger.debug(f'Formal NR nubmer is: {nrd.nrNum}')
        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request: {} not found'.format(nr_num)}), 404)
        except Exception as err:
            current_app.logger.error(
                'Error when posting NR: {0} Err:{1} Please double check the json input file format'.format(nr_num, err)
            )
            return make_response(
                jsonify({'message': 'NR had an internal error. Please double check the json input file format'}), 404
            )

        ps_instance = PaymentSocietyDAO()
        ps_instance.nrNum = nrd.nrNum
        ps_instance.corpNum = json_input.get('corpNum', None)
        ps_instance.paymentCompletionDate = json_input.get('paymentCompletionDate', None)
        ps_instance.paymentStatusCode = json_input.get('paymentStatusCode', None)
        ps_instance.paymentFeeCode = json_input.get('paymentFeeCode', None)
        ps_instance.paymentType = json_input.get('paymentType', None)
        ps_instance.paymentAmount = json_input.get('paymentAmount', None)
        ps_instance.paymentJson = json_input.get('paymentJson', None)
        ps_instance.paymentAction = json_input.get('paymentAction', None)

        ps_instance.save_to_db()
        current_app.logger.debug('ps_instance saved...')

        if nrd.stateCd == State.PENDING_PAYMENT:
            nrd.stateCd = 'DRAFT'
        nrd.save_to_db()
        current_app.logger.debug('nrd saved...')

        return make_response(jsonify(ps_instance.json()), 200)
