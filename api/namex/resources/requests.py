"""Requests used to support the namex API

TODO: Fill in a larger description once the API is defined for V1
"""

from datetime import datetime

from flask import current_app, g, jsonify, make_response, request
from flask_jwt_oidc import AuthError
from flask_restx import Namespace, Resource, cors, fields
from marshmallow import ValidationError
from pytz import timezone
from sqlalchemy import and_, exists, func, or_, text
from sqlalchemy.inspection import inspect
from sqlalchemy.orm import eagerload, lazyload, load_only
from sqlalchemy.orm.exc import NoResultFound

from namex import jwt
from namex.analytics import VALID_ANALYSIS as ANALYTICS_VALID_ANALYSIS
from namex.analytics import RestrictedWords, SolrQueries
from namex.constants import DATE_TIME_FORMAT_SQL, NameState
from namex.exceptions import BusinessException
from namex.models import (
    Applicant,
    ApplicantSchema,
    Comment,
    DecisionReason,
    Event,
    Name,
    NameCommentSchema,
    NameSchema,
    PartnerNameSystemSchema,
    RequestsHeaderSchema,
    RequestsSchema,
    RequestsSearchSchema,
    State,
    User,
    db,
)
from namex.models import Request as RequestDAO
from namex.models.request import AffiliationInvitationSearchDetails, RequestsAuthSearchSchema
from namex.services import EventRecorder, MessageServices, ServicesError
from namex.services.lookup import nr_filing_actions
from namex.services.name_request import NameRequestService
from namex.services.name_request.utils import check_ownership, get_or_create_user_by_jwt, valid_state_transition
from namex.utils import queue_util
from namex.utils.auth import cors_preflight
from namex.utils.common import convert_to_ascii, convert_to_utc_max_date_time, convert_to_utc_min_date_time

from .utils import DateUtils

# Register a local namespace for the requests
api = Namespace('Name Examination', description='Staff-facing name request operations for state, analysis, and name editing')

# Marshmallow schemas
request_schema = RequestsSchema(many=False)
request_schemas = RequestsSchema(many=True)
request_header_schema = RequestsHeaderSchema(many=False)
request_search_schemas = RequestsSearchSchema(many=True)
request_auth_search_schemas = RequestsAuthSearchSchema(many=True)

names_schema = NameSchema(many=False)
names_schemas = NameSchema(many=True)
nwpta_schema = PartnerNameSystemSchema(many=False)
name_comment_schema = NameCommentSchema(many=False)

applicant_schema = ApplicantSchema(many=False)


@api.errorhandler(AuthError)
def handle_auth_error(ex):
    response = jsonify(ex.error)
    response.status_code = ex.status_code
    return response


# noinspection PyUnresolvedReferences
@cors_preflight('GET')
@api.route('/echo', methods=['GET', 'OPTIONS'])
class Echo(Resource):
    """Helper method to echo back all your JWT token info"""

    @staticmethod
    @jwt.requires_auth
    @api.doc(
        description='Fetches the JWT token info for the current user',
        responses={
            200: 'Token info fetched successfully',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(*args, **kwargs):
        try:
            return make_response(jsonify(g.jwt_oidc_token_info), 200)
        except Exception as err:
            return {'error': '{}'.format(err)}, 500


#################### QUEUES #######################
@cors_preflight('GET')
@api.route('/queues/@me/oldest', methods=['GET', 'OPTIONS'])
class RequestsQueue(Resource):
    """Acting like a QUEUE this gets the next NR (just the NR number)
    and assigns it to your auth id, and marks it as INPROGRESS
    """

    @staticmethod
    @jwt.requires_roles([User.APPROVER])
    @api.doc(
        description='Fetches the next draft name request from the queue and assigns it to the current user. '
                    'If the user already has an in-progress NR, that one is returned instead.',
        params={'priorityQueue': 'Set to true to fetch from the priority queue'},
        responses={
            200: 'Name request assigned successfully',
            401: 'Unauthorized',
            403: 'Forbidden',
            404: 'No name requests found in the queue',
            500: 'Internal server error',
        },
    )
    def get():
        # GET existing or CREATE new user based on the JWT info
        try:
            user = get_or_create_user_by_jwt(g.jwt_oidc_token_info)
        except ServicesError as se:
            current_app.logger.error(se.with_traceback(None))
            return make_response(jsonify(message='unable to get ot create user, aborting operation'), 500)
        except Exception as unmanaged_error:
            current_app.logger.error(unmanaged_error.with_traceback(None))
            return make_response(jsonify(message='internal server error'), 500)

        # get the next NR assigned to the User
        try:
            priority_queue = request.args.get('priorityQueue')
            nr = RequestDAO.get_queued_oldest(user, priority_queue == 'true')
        except BusinessException as be:
            current_app.logger.error(be.with_traceback(None))
            return make_response(jsonify(message='There are no more requests in the {} Queue'.format(State.DRAFT)), 404)
        except Exception as unmanaged_error:
            current_app.logger.error(unmanaged_error.with_traceback(None))
            return make_response(jsonify(message='internal server error'), 500)
        current_app.logger.debug('got the nr:{}'.format(nr.nrNum))

        # if no NR returned
        if 'nr' not in locals() or not nr:
            return make_response(jsonify(message='No more NRs in Queue to process'), 200)

        EventRecorder.record(user, Event.GET, nr, {})

        return make_response(jsonify(nameRequest='{}'.format(nr.nrNum)), 200)


@cors_preflight('GET, POST')
@api.route('', methods=['GET', 'POST', 'OPTIONS'])
class Requests(Resource):
    a_request = api.model(
        'Request',
        {
            'submitter': fields.String('The submitter name'),
            'corpType': fields.String('The corporation type'),
            'reqType': fields.String('The name request type'),
        },
    )

    START = 0
    ROWS = 10

    @staticmethod
    @jwt.requires_auth
    @api.doc(
        description='Fetches name requests using various filters, with pagination and sorting support',
        params={
            'start': 'The result offset (default: 0)',
            'rows': 'Number of results to return (default: 10)',
            'queue': 'Comma-separated list of request states to filter by (e.g., DRAFT, INPROGRESS)',
            'order': 'Sort order in format "column:asc,column:desc" (default: submittedDate:desc,stateCd:desc)',
            'nrNum': 'Partial or full name request number to search for',
            'activeUser': 'Filter by active examiner username',
            'compName': 'Partial or full company name to search',
            'firstName': 'Applicant first name',
            'lastName': 'Applicant last name',
            'consentOption': 'Filter by consent flag (Yes, No, Received, Waived)',
            'ranking': 'Request priority (Standard or Priority)',
            'notification': 'Notification status (Notified or Not Notified)',
            'submittedInterval': 'Submitted in time range (Today, 7 days, 30 days, etc.)',
            'lastUpdateInterval': 'Last updated in time range (Today, Yesterday, 2 days, etc.)',
            'submittedStartDate': 'Start date for submission date range (format: YYYY-MM-DD)',
            'submittedEndDate': 'End date for submission date range (format: YYYY-MM-DD)',
            'hour': 'Client-local hour offset used for relative date filters',
        },
        responses={
            200: 'Fetch successful',
            400: 'Invalid input or parameter combination',
            401: 'Unauthorized',
            403: 'Forbidden',
            406: 'Unacceptable parameter type',
            500: 'Internal server error',
        },
    )
    def get(*args, **kwargs):
        # validate row & start params
        start = request.args.get('start', Requests.START)
        rows = request.args.get('rows', Requests.ROWS)
        try:
            start = int(start)
            rows = int(rows)
        except Exception as err:
            current_app.logger.info('start or rows not an int, err: {}'.format(err))
            return make_response(jsonify({'message': 'paging parameters were not integers'}), 406)

        # queue must be a list of states
        queue = request.args.get('queue', None)
        if queue:
            if queue == 'COMPLETED':
                queue = 'COMPLETED'
            queue = queue.upper().split(',')
            for q in queue:
                if q not in State.VALID_STATES:
                    return make_response(jsonify({'message': "'{}' is not a valid queue".format(queue)}), 406)

        # order must be a string of 'column:asc,column:desc'
        order = request.args.get('order', 'submittedDate:desc,stateCd:desc')
        # order=dict((x.split(":")) for x in order.split(',')) // con't pass as a dict as the order is lost

        # create the order by txt, looping through Request Attributes and mapping to column names
        # TODO: this is fragile across joins, fix it up if queries are going to sort across joins
        cols = inspect(RequestDAO).columns
        col_keys = cols.keys()
        sort_by = ''
        order_list = ''
        for k, v in ((x.split(':')) for x in order.split(',')):
            vl = v.lower()
            if (k in col_keys) and (vl == 'asc' or vl == 'desc'):
                if len(sort_by) > 0:
                    sort_by = sort_by + ', '
                    order_list = order_list + ', '
                sort_by = sort_by + '{columns} {direction} NULLS LAST'.format(columns=cols[k], direction=vl)
                order_list = order_list + '{attribute} {direction} NULLS LAST'.format(attribute=k, direction=vl)

        # Assemble the query
        nrNum = request.args.get('nrNum', None)
        activeUser = request.args.get('activeUser', None)
        compName = request.args.get('compName', None)
        firstName = request.args.get('firstName', None)
        lastName = request.args.get('lastName', None)
        consentOption = request.args.get('consentOption', None)
        priority = request.args.get('ranking', None)
        notification = request.args.get('notification', None)
        submittedInterval = request.args.get('submittedInterval', None)
        lastUpdateInterval = request.args.get('lastUpdateInterval', None)
        current_hour = int(request.args.get('hour', 0))
        submittedStartDate = request.args.get('submittedStartDate', None)
        submittedEndDate = request.args.get('submittedEndDate', None)

        q = RequestDAO.query.filter()
        if queue:
            q = q.filter(RequestDAO.stateCd.in_(queue))

        q = q.filter(RequestDAO.nrNum.notlike('NR L%'))
        if nrNum:
            # set any variation of mixed case 'nr' to 'NR'
            nrNum = nrNum.upper().strip()
            # remove spaces within string
            nrNum = nrNum.replace(' ', '')
            # add single space between NR and number
            nrNum = nrNum.replace('NR', 'NR ')
            nrNum = '%' + nrNum + '%'
            q = q.filter(RequestDAO.nrNum.like(nrNum))
        if activeUser:
            q = q.join(RequestDAO.activeUser).filter(User.username.ilike('%' + activeUser + '%'))

        if compName:
            compName = compName.strip().replace(' ', '%')
            # nameSearch column is populated like: '|1<name 1>|2<name 2>|3<name 3>
            # to ensure we don't get a match that spans over a single name
            compName1 = '%|1%' + compName + '%1|%'
            compName2 = '%|2%' + compName + '%2|%'
            compName3 = '%|3%' + compName + '%3|%'
            q = q.filter(
                or_(
                    RequestDAO.nameSearch.ilike(compName1),
                    RequestDAO.nameSearch.ilike(compName2),
                    RequestDAO.nameSearch.ilike(compName3),
                )
            )

        if firstName:
            firstName = firstName.strip().replace(' ', '%')
            q = q.join(RequestDAO.applicants).filter(Applicant.firstName.ilike('%' + firstName + '%'))

        if lastName:
            lastName = lastName.strip().replace(' ', '%')
            q = q.join(RequestDAO.applicants).filter(Applicant.lastName.ilike('%' + lastName + '%'))

        if consentOption == 'Received':
            q = q.filter(RequestDAO.consentFlag == 'R')
        if consentOption == 'Yes':
            q = q.filter(RequestDAO.consentFlag == 'Y')
        elif consentOption == 'Waived':
            q = q.filter(RequestDAO.consentFlag == 'N')
        elif consentOption == 'No':
            q = q.filter(RequestDAO.consentFlag.is_(None))

        if priority == 'Standard':
            q = q.filter(RequestDAO.priorityCd != 'Y')
        elif priority == 'Priority':
            q = q.filter(RequestDAO.priorityCd != 'N')

        if notification == 'Notified':
            q = q.filter(RequestDAO.furnished != 'N')
        elif notification == 'Not Notified':
            q = q.filter(RequestDAO.furnished != 'Y')

        if submittedInterval == 'Today':
            q = q.filter(
                RequestDAO.submittedDate
                > text("NOW() - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour))
            )
        elif submittedInterval == '7 days':
            q = q.filter(
                RequestDAO.submittedDate
                > text("NOW() - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour + 24 * 6))
            )
        elif submittedInterval == '30 days':
            q = q.filter(
                RequestDAO.submittedDate
                > text("NOW() - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour + 24 * 29))
            )
        elif submittedInterval == '90 days':
            q = q.filter(
                RequestDAO.submittedDate
                > text("NOW() - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour + 24 * 89))
            )
        elif submittedInterval == '1 year':
            q = q.filter(RequestDAO.submittedDate > text("NOW() - INTERVAL '1 YEARS'"))
        elif submittedInterval == '3 years':
            q = q.filter(RequestDAO.submittedDate > text("NOW() - INTERVAL '3 YEARS'"))
        elif submittedInterval == '5 years':
            q = q.filter(RequestDAO.submittedDate > text("NOW() - INTERVAL '5 YEARS'"))

        if lastUpdateInterval == 'Today':
            q = q.filter(
                RequestDAO.lastUpdate
                > text("(now() at time zone 'utc') - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour))
            )
        if lastUpdateInterval == 'Yesterday':
            today_offset = current_hour
            yesterday_offset = today_offset + 24
            q = q.filter(
                RequestDAO.lastUpdate
                < text("(now() at time zone 'utc') - INTERVAL '{today_offset} HOURS'".format(today_offset=today_offset))
            )
            q = q.filter(
                RequestDAO.lastUpdate
                > text(
                    "(now() at time zone 'utc') - INTERVAL '{yesterday_offset} HOURS'".format(
                        yesterday_offset=yesterday_offset
                    )
                )
            )
        elif lastUpdateInterval == '2 days':
            q = q.filter(
                RequestDAO.lastUpdate
                > text(
                    "(now() at time zone 'utc') - INTERVAL '{hour_offset} HOURS'".format(hour_offset=current_hour + 24)
                )
            )
        elif lastUpdateInterval == '7 days':
            q = q.filter(
                RequestDAO.lastUpdate
                > text(
                    "(now() at time zone 'utc') - INTERVAL '{hour_offset} HOURS'".format(
                        hour_offset=current_hour + 24 * 6
                    )
                )
            )
        elif lastUpdateInterval == '30 days':
            q = q.filter(
                RequestDAO.lastUpdate
                > text(
                    "(now() at time zone 'utc') - INTERVAL '{hour_offset} HOURS'".format(
                        hour_offset=current_hour + 24 * 29
                    )
                )
            )

        if submittedInterval and (submittedStartDate or submittedEndDate):
            return make_response(
                jsonify(
                    {
                        'message': 'submittedInterval cannot be used in conjuction with submittedStartDate and submittedEndDate'
                    }
                ),
                400,
            )

        submittedStartDateTimeUtcObj = None
        submittedEndDateTimeUtcObj = None

        if submittedStartDate:
            try:
                submittedStartDateTimeUtcObj = convert_to_utc_min_date_time(submittedStartDate)
                # convert date to format db expects
                submittedStartDateTimeUtc = submittedStartDateTimeUtcObj.strftime(DATE_TIME_FORMAT_SQL)
                q = q.filter(
                    RequestDAO.submittedDate
                    >= text("'{submittedStartDateTimeUtc}'".format(submittedStartDateTimeUtc=submittedStartDateTimeUtc))
                )
            except ValueError:
                return make_response(
                    jsonify(
                        {
                            'message': 'Invalid submittedStartDate: {}.  Must be of date format %Y-%m-%d'.format(
                                submittedStartDate
                            )
                        }
                    ),
                    400,
                )

        if submittedEndDate:
            try:
                submittedEndDateTimeUtcObj = convert_to_utc_max_date_time(submittedEndDate)
                # convert date to format db expects
                submittedEndDateTimeUtc = submittedEndDateTimeUtcObj.strftime(DATE_TIME_FORMAT_SQL)
                q = q.filter(
                    RequestDAO.submittedDate
                    <= text("'{submittedEndDateTimeUtc}'".format(submittedEndDateTimeUtc=submittedEndDateTimeUtc))
                )
            except ValueError:
                return make_response(
                    jsonify(
                        {
                            'message': 'Invalid submittedEndDate: {}.  Must be of date format %Y-%m-%d'.format(
                                submittedEndDate
                            )
                        }
                    ),
                    400,
                )

        if (
            submittedStartDateTimeUtcObj and submittedEndDateTimeUtcObj
        ) and submittedEndDateTimeUtcObj < submittedStartDateTimeUtcObj:
            return make_response(jsonify({'message': 'submittedEndDate must be after submittedStartDate'}), 400)

        q = q.order_by(text(sort_by))

        # get a count of the full set size, this ignore the offset & limit settings
        count_q = q.statement.with_only_columns([func.count()]).order_by(None)
        count = db.session.execute(count_q).scalar()

        # Add the paging
        q = q.offset(start)
        q = q.limit(rows)

        # create the response
        rep = {
            'response': {
                'start': start,
                'rows': rows,
                'numFound': count,
                'numPriorities': 0,
                'numUpdatedToday': 0,
                'queue': queue,
                'order': order_list,
            },
            'nameRequests': [request_search_schemas.dump(q.all()), {}],
        }

        return make_response(jsonify(rep), 200)

    # @api.errorhandler(AuthError)
    # def handle_auth_error(ex):
    #     response = jsonify(ex.error)
    #     response.status_code = ex.status_code
    # return response, 401
    # return {}, 401

    # noinspection PyUnusedLocal,PyUnusedLocal
    @api.hide
    @api.expect(a_request)
    @jwt.requires_auth
    def post(self, *args, **kwargs):
        current_app.logger.info('Someone is trying to post a new request')
        return make_response(jsonify({'message': 'Not Implemented'}), 501)


# For sbc-auth - My Business Registry page.


@cors_preflight('GET, POST')
@api.route('/search', methods=['GET', 'POST', 'OPTIONS'])
class RequestSearch(Resource):
    """Search for NR's by NR number or associated name."""

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Searches name requests by partially matching NR number or business name using query parameters',
        params={
            'query': 'NR number or business name to search (e.g., "NR1234567" or "abcd")',
            'start': 'Result offset for pagination (default: 0)',
            'rows': 'Number of results to return (default: 10)',
        },
        responses={
            200: 'Search results fetched successfully',
            400: 'Invalid search parameters',
            500: 'Internal server error',
        },
    )
    def get():
        data = []
        start = request.args.get('start', 0, type=int)
        rows = request.args.get('rows', 10, type=int)
        query = request.args.get('query', '')
        if not query:
            return make_response(jsonify(data), 200)

        try:
            solr_query, nr_number, nr_name = SolrQueries.get_parsed_query_name_nr_search(query)
            condition = ''
            if nr_number:
                condition = f"requests.nr_num ILIKE '%{nr_number}%'"
            if nr_name:
                nr_name = nr_name.replace("'", "''")
                if condition:
                    condition += ' OR '
                name_condition = "requests.name_search ILIKE '%"
                name_condition += "%' AND requests.name_search ILIKE '%".join(nr_name.split())
                name_condition += "%'"

                condition += f'({name_condition})'

            results = (
                RequestDAO.query.filter(
                    RequestDAO.stateCd.in_([State.DRAFT, State.INPROGRESS, State.REFUND_REQUESTED]),
                    text(f'({condition})'),
                )
                .options(
                    lazyload('*'),
                    eagerload(RequestDAO.names).load_only(Name.name),
                    load_only(RequestDAO.id, RequestDAO.nrNum),
                )
                .order_by(RequestDAO.submittedDate.desc())
                .limit(rows)
                .all()
            )

            data.extend(
                [
                    {
                        # 'id': nr.id,
                        'nrNum': nr.nrNum,
                        'names': [n.name for n in nr.names],
                    }
                    for nr in results
                ]
            )

            while len(data) < rows:
                if start < rows:
                    # Check if the search length is less than 7 digits. If so, patch it with zero at the end to increase rows number.
                    # After this, the search cycles to solr will be reduced a lot.
                    # Otherwise, the rows is too small and it will take long time (many times solr calling) to search in solr and get timeout exception.
                    # So the less of search length, the bigger of rows will be.
                    temp_rows = str(rows)
                    if nr_number and len(nr_number) < 7:
                        temp_rows = str(rows).ljust(9 - len(nr_number), '0')
                    if nr_name and len(nr_name) < 7:
                        temp_rows = str(rows).ljust(9 - len(nr_name), '0')

                    rows = int(temp_rows)

                nr_data, have_more_data = RequestSearch._get_next_set_from_solr(solr_query, start, rows)
                nr_data = nr_data[: (rows - len(data))]
                data.extend(
                    [
                        {
                            # 'id': nr.id,
                            'nrNum': nr.nrNum,
                            'names': [n.name for n in nr.names],
                        }
                        for nr in nr_data
                    ]
                )

                if not have_more_data:
                    break  # no more data in solr
                start += rows

            return make_response(jsonify(data), 200)
        except Exception as e:
            current_app.logger.error(f'Error in /search, {e}')
            return make_response(jsonify({'message': 'Internal server error'}), 500)

    @staticmethod
    def _get_next_set_from_solr(solr_query, start, rows):
        results, msg, code = SolrQueries.get_name_nr_search_results(solr_query, start, rows)
        if code:
            raise Exception(msg)
        elif len(results['names']) > 0:
            have_more_data = results['response']['numFound'] > (start + rows)
            identifiers = [name['nr_num'] for name in results['names']]
            return RequestDAO.query.filter(
                RequestDAO.nrNum.in_(identifiers),
                RequestDAO.stateCd != State.CANCELLED,
                or_(
                    RequestDAO.stateCd != State.EXPIRED,
                    text(
                        f"(requests.state_cd = '{State.EXPIRED}' AND CAST(requests.expiration_date AS DATE) + "
                        "interval '60 day' >= CAST(now() AS DATE))"
                    ),
                ),
            ).options(
                lazyload('*'),
                eagerload(RequestDAO.names).load_only(Name.name),
                load_only(RequestDAO.id, RequestDAO.nrNum),
            ).all(), have_more_data

        return [], False

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.SYSTEM])
    @api.expect(api.model(
        'AffiliationInvitationSearch',
        {
            'identifiers': fields.List(fields.String, description='List of NR identifiers to search'),
            'identifier': fields.String(description='Search for a specific NR number'),
            'status': fields.List(fields.String, description='Filter by status (e.g. DRAFT, INPROGRESS)'),
            'name': fields.String(description='Partial name to search'),
            'type': fields.List(fields.String, description='Request types to filter'),
            'page': fields.Integer(description='Page number for pagination'),
            'limit': fields.Integer(description='Limit the number of results per page'),
        },
    ))
    @api.doc(
        description='Searches name requests by partially matching NR number or business name using a JSON payload',
        responses={
            200: 'Search results fetched successfully',
            400: 'Invalid input provided',
            401: 'Unauthorized',
            403: 'Forbidden',
            500: 'Internal server error',
        },
    )
    def post():
        search = request.get_json()
        identifiers = search.get('identifiers', [])
        search_details = AffiliationInvitationSearchDetails.from_request_args(search)

        # Only names and applicants are needed for this query, we want this query to be lighting fast
        # to prevent putting a load on namex-api.
        # Base query with the common identifier filter
        q = RequestDAO.query.filter(RequestDAO.nrNum.in_(identifiers))

        if search_details.identifier:
            q = q.filter(
                func.replace(RequestDAO.nrNum, ' ', '').ilike(f'%{search_details.identifier.replace(" ", "")}%')
            )
        # Add the state filter if 'state' is provided
        if search_details.status:
            normalized_status = {s.strip().upper() for s in search_details.status}
            base_statuses = normalized_status & set(State.ALL_STATES)
            # Handle Invalid Statuses such 'Active'
            if not base_statuses and NameState.NOT_EXAMINED.value not in normalized_status:
                return jsonify([])
            conditions = [RequestDAO.stateCd.in_(base_statuses)] if base_statuses else []

            if NameState.NOT_EXAMINED.value in normalized_status:
                conditions.append(
                    and_(
                        RequestDAO.stateCd.in_({State.DRAFT, State.HOLD}),
                        exists().where(
                            and_(
                                Name.nrId == RequestDAO.id,
                                Name.state == NameState.NOT_EXAMINED.value
                            )
                        )
                    )
                )

            if conditions:
                q = q.filter(or_(*conditions))

        # Add the nr_name filter if 'nr_name' is provided
        if search_details.name:
            q = q.filter(RequestDAO.nameSearch.ilike(f'%{search_details.name}%'))

        if search_details.type and 'NR' not in [t.strip().upper() for t in search_details.type]:
            request_typecd = nr_filing_actions.get_request_type_array(search_details.type)
            flattened_request_types = [item for sublist in request_typecd.values() for item in sublist]
            action_codes = nr_filing_actions.get_request_type_action(search_details.type)
            if action_codes:
                q = q.filter(RequestDAO._request_action_cd.in_(action_codes))
            q = q.filter(RequestDAO.requestTypeCd.in_(flattened_request_types))

        q = q.options(
            lazyload('*'),
            eagerload(RequestDAO.names).load_only(Name.state, Name.name),
            eagerload(RequestDAO.applicants).load_only(Applicant.emailAddress, Applicant.phoneNumber),
            load_only(
                RequestDAO.id,
                RequestDAO.nrNum,
                RequestDAO.stateCd,
                RequestDAO.requestTypeCd,
                RequestDAO.natureBusinessInfo,
                RequestDAO._entity_type_cd,
                RequestDAO.expirationDate,
                RequestDAO.consentFlag,
                RequestDAO._request_action_cd,
            ),
        )
        if (
            search_details.page is not None
            and search_details.page > 1
            and search_details.limit is not None
            and search_details.limit > 0
        ):
            q = q.offset((search_details.page - 1) * search_details.limit).limit(search_details.limit+1)
        q = q.offset((search_details.page - 1) * search_details.limit).limit(search_details.limit+1)
        requests = request_auth_search_schemas.dump(q.all())
        has_more = len(requests)> search_details.limit
        actions_array = [
            nr_filing_actions.get_actions(r['requestTypeCd'], r['entity_type_cd'], r['request_action_cd'])
            for r in requests[:search_details.limit]
        ]
        for r, additional_fields in zip(requests, actions_array):
            if additional_fields:
                r.update(additional_fields)
        requests = requests or []
        return jsonify({'requests': requests[:search_details.limit], 'hasMore': has_more})

# noinspection PyUnresolvedReferences
@cors_preflight('GET, PATCH, PUT, DELETE')
@api.route('/<string:nr>', methods=['GET', 'PATCH', 'PUT', 'DELETE', 'OPTIONS'])
class Request(Resource):
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR, User.VIEWONLY])
    @api.doc(
        description='Fetches a specific name request by NR number',
        params={'nr': 'NR number'},
        responses={
            200: 'Name request fetched successfully',
            401: 'Unauthorized',
            404: 'Name request not found',
            500: 'Internal server error',
        },
    )
    def get(nr):
        # return make_response(jsonify(request_schema.dump(RequestDAO.query.filter_by(nr=nr.upper()).first_or_404()))
        return jsonify(RequestDAO.query.filter_by(nrNum=nr.upper()).first_or_404().json())

    @staticmethod
    # @cors.crossdomain(origin='*')
    @api.hide
    @jwt.requires_roles([User.APPROVER, User.EDITOR])
    def delete(nr):
        return '', 501  # not implemented
        # nrd = RequestDAO.find_by_nr(nr)
        # even if not found we still return a 204, which is expected spec behaviour
        # if nrd:
        #     nrd.stateCd = State.CANCELLED
        #     nrd.save_to_db()
        #
        # return '', 204

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR, User.SYSTEM])
    @api.expect(api.model('PatchNRPayload', {
        'state': fields.String(description='New state to apply to the Name Request'),
        'previousStateCd': fields.String(description='Optional previous state code'),
        'corpNum': fields.String(description='Corporation number (required if consuming name)'),
        'comments': fields.List(fields.Nested(api.model('PatchNRComment', {
            'comment': fields.String(required=True, description='Comment text'),
            'id': fields.Integer(description='Set to 0 or omit for new comments')
        })))
    }))
    @api.doc(
        description=(
            "Updates a name request's state, records the previous state, optionally adds comments, assigns a corpNum if consumption state, "
            "and calculates expiration if approval state. Only users with APPROVER, EDITOR, or SYSTEM roles may update state, "
            "and certain transitions may be restricted based on role or current state."
        ),
        params={'nr': 'NR number'},
        responses={
            200: 'Name request patched successfully',
            206: 'Name request patched with warnings',
            400: 'Missing or invalid request body',
            401: 'Unauthorized',
            404: 'Name Request not found',
            406: 'Validation error or unsupported state transition',
            500: 'Internal server error',
        },
    )
    def patch(nr, *args, **kwargs):
        # do the cheap check first before the more expensive ones
        # check states
        # some nr requested from Legancy application includes %20 after NR. e.g. 'NR%209288253', which should be 'NR 9288253'
        nr = nr.replace('%20', ' ')
        current_app.logger.debug('NR: {0}'.format(nr))

        json_input = request.get_json()
        if not json_input:
            return make_response(jsonify({'message': 'No input data provided'}), 400)

        # find NR
        try:
            user = get_or_create_user_by_jwt(g.jwt_oidc_token_info)
            nrd = RequestDAO.find_by_nr(nr)
            if not nrd:
                return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
            start_state = nrd.stateCd
        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
        except Exception as err:
            current_app.logger.error('Error when patching NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        try:
            ### STATE ###

            # all these checks to get removed to marshmallow
            state = json_input.get('state', None)
            if state:
                if state not in State.VALID_STATES:
                    return make_response(jsonify({'message': 'not a valid state'}), 406)

                if not nrd:
                    return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)

                if not valid_state_transition(user, nrd, state):
                    return make_response(jsonify(message='Name Request state transition validation failed.'), 401)

                # if the user has an existing (different) INPROGRESS NR, revert to previous state (default to HOLD)
                existing_nr = RequestDAO.get_inprogress(user)
                if existing_nr:
                    if existing_nr.previousStateCd:
                        existing_nr.stateCd = existing_nr.previousStateCd
                        existing_nr.previousStateCd = None
                    else:
                        existing_nr.stateCd = State.HOLD
                    existing_nr.save_to_db()

                nrd.stateCd = state
                nrd.userId = user.id

                # if our state wasn't INPROGRESS and it is now, ensure the furnished flag is N
                if start_state in locals() and start_state != State.INPROGRESS and nrd.stateCd == State.INPROGRESS:
                    # set / reset the furnished flag to N
                    nrd.furnished = 'N'

                # if we're changing to a completed or cancelled state, clear reset flag on NR record
                if state in State.COMPLETED_STATE + [State.CANCELLED]:
                    nrd.hasBeenReset = False
                    if nrd.stateCd == State.CONDITIONAL and nrd.consentFlag is None:
                        nrd.consentFlag = 'Y'

                ### COMMENTS ###
                # we only add new comments, we do not change existing comments
                # - we can find new comments in json as those with no ID

                if json_input.get('comments', None):
                    for in_comment in json_input['comments']:
                        is_new_comment = False
                        try:
                            if in_comment['id'] is None or in_comment['id'] == 0:
                                is_new_comment = True
                        except KeyError:
                            is_new_comment = True
                        if is_new_comment and in_comment['comment'] is not None:
                            new_comment = Comment()
                            new_comment.comment = convert_to_ascii(in_comment['comment'])
                            new_comment.examiner = user
                            new_comment.nrId = nrd.id

                ### END comments ###

            ### PREVIOUS STATE ###
            # - None (null) is a valid value for Previous State
            if 'previousStateCd' in json_input.keys():
                nrd.previousStateCd = json_input.get('previousStateCd', None)

            # calculate and update expiration date
            if (
                nrd.stateCd in (State.APPROVED, State.REJECTED, State.CONDITIONAL)
                and nrd.furnished == 'N'
                and nrd.expirationDate is None
            ):
                expiry_days = NameRequestService.get_expiry_days(nrd.request_action_cd, nrd.requestTypeCd)
                nrd.expirationDate = NameRequestService.create_expiry_date(datetime.utcnow(), expiry_days)
                json_input['expirationDate'] = nrd.expirationDate.isoformat()

                nrd.furnished = 'Y'

            def consumeName(nrd, json_input):
                if not json_input.get('corpNum'):
                    return False, '"corpNum" is required and cannot be empty.'

                consumed = False
                for nrd_name in nrd.names:
                    if nrd_name.state in (Name.APPROVED, Name.CONDITION):
                        nrd_name.consumptionDate = datetime.utcnow()
                        nrd_name.corpNum = json_input.get('corpNum')
                        consumed = True

                if not consumed:
                    return False, 'Cannot find an Approved or Condition name to be consumed.'

                return True, None  # Return success and no error message

            if state == State.CONSUMED:
                success, error_message = consumeName(nrd, json_input)
                if not success:
                    return make_response(jsonify(message=error_message), 406)

            # save record
            nrd.save_to_db()
            EventRecorder.record(user, Event.PATCH, nrd, json_input)

        except Exception as err:
            current_app.logger.debug(err.with_traceback(None))
            return make_response(jsonify(message='Internal server error'), 500)

        if 'warnings' in locals() and warnings:  # noqa: F821
            return make_response(jsonify(message='Request:{} - patched'.format(nr), warnings=warnings), 206)  # noqa: F821

        if state in [State.APPROVED, State.CONDITIONAL, State.REJECTED]:
            queue_util.publish_email_notification(nrd.nrNum, state)

        return make_response(jsonify(message='Request:{} - patched'.format(nr)), 200)

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR])
    @api.doc(
        description='Fully replaces an existing name request with new data, including names, applicants, and state transitions',
        params={'nr': 'NR number'},
        responses={
            200: 'Name request replaced successfully',
            206: 'Replaced with warnings',
            400: 'Invalid input data or validation error',
            401: 'Unauthorized',
            404: 'Name request not found',
            406: 'Invalid or missing state',
            500: 'Internal server error',
        },
    )
    def put(nr, *args, **kwargs):
        # do the cheap check first before the more expensive ones
        json_input = request.get_json()
        if not json_input:
            return make_response(jsonify(message='No input data provided'), 400)
        current_app.logger.debug(json_input)

        nr_num = json_input.get('nrNum', None)
        if nr_num and nr_num != nr:
            return make_response(jsonify(message='Data contains a different NR# than this resource'), 400)

        state = json_input.get('state', None)
        if not state:
            return make_response(jsonify({'message': 'state not set'}), 406)

        if state not in State.VALID_STATES:
            return make_response(jsonify({'message': 'not a valid state'}), 406)

        try:
            user = get_or_create_user_by_jwt(g.jwt_oidc_token_info)
            nrd = RequestDAO.find_by_nr(nr)
            if not nrd:
                return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
            orig_nrd = nrd.json()
        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
        except Exception as err:
            current_app.logger.error('Error when patching NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        if not valid_state_transition(user, nrd, state):
            return make_response(jsonify(message='you are not authorized to make these changes'), 401)

        name_choice_exists = {1: False, 2: False, 3: False}
        for name in json_input.get('names', None):
            if name['name'] and name['name'] != '':
                name_choice_exists[name['choice']] = True
        if not name_choice_exists[1]:
            return make_response(jsonify(message='Data does not include a name choice 1'), 400)
        if not name_choice_exists[2] and name_choice_exists[3]:
            return make_response(jsonify(message='Data contains a name choice 3 without a name choice 2'), 400)

        try:
            existing_nr = RequestDAO.get_inprogress(user)
            if existing_nr:
                existing_nr.stateCd = State.HOLD
                existing_nr.save_to_db()

            if json_input.get('consent_dt', None):
                consentDateStr = json_input['consent_dt']
                json_input['consent_dt'] = DateUtils.parse_date_string(consentDateStr, '%d %b %Y %H:%M:%S %Z')

            # convert Submitted Date to correct format
            if json_input.get('submittedDate', None):
                submittedDateStr = json_input['submittedDate']
                json_input['submittedDate'] = DateUtils.parse_date_string(submittedDateStr, '%d %b %Y %H:%M:%S %Z')

            # convert Expiration Date to correct format
            if json_input.get('expirationDate', None):
                try:
                    expirationDateStr = json_input['expirationDate']
                    expirationDate = DateUtils.parse_date(expirationDateStr)
                    # Convert the UTC datetime object to the end of day in pacific time without milliseconds
                    pacific_time = expirationDate.astimezone(timezone('US/Pacific'))
                    end_of_day_pacific = pacific_time.replace(hour=23, minute=59, second=0, microsecond=0)
                    json_input['expirationDate'] = end_of_day_pacific.strftime('%Y-%m-%d %H:%M:%S%z')
                except Exception as e:
                    current_app.logger.debug(f'Error parsing expirationDate: {str(e)}')
                    pass

            # convert NWPTA dates to correct format
            if json_input.get('nwpta', None):
                for region in json_input['nwpta']:
                    try:
                        if region['partnerNameDate'] == '':
                            region['partnerNameDate'] = None
                        if region['partnerNameDate']:
                            partnerNameDateStr = region['partnerNameDate']
                            region['partnerNameDate'] = DateUtils.parse_date_string(partnerNameDateStr, '%d-%m-%Y')
                    except ValueError:
                        pass
                        # pass on this error and catch it when trying to add to record, to be returned

            # update request header

            reset = False
            if nrd.furnished == RequestDAO.REQUEST_FURNISHED and json_input.get('furnished', None) == 'N':
                reset = True

            nrd.additionalInfo = convert_to_ascii(json_input.get('additionalInfo', None))
            nrd.consentFlag = json_input.get('consentFlag', None)
            nrd.consent_dt = json_input.get('consent_dt', None)
            nrd.corpNum = json_input.get('corpNum', None)
            nrd.checkedOutBy = json_input.get('checkedOutBy', None)
            nrd.checkedOutDt = json_input.get('checkedOutDt', None)
            nrd.entity_type_cd = json_input.get('entity_type_cd', None)
            nrd.expirationDate = json_input.get('expirationDate', None)
            nrd.furnished = json_input.get('furnished', 'N')
            nrd.hasBeenReset = json_input.get('hasBeenReset', None)
            nrd.homeJurisNum = json_input.get('homeJurisNum', None)
            nrd.natureBusinessInfo = convert_to_ascii(json_input.get('natureBusinessInfo', None))
            nrd.previousNr = json_input.get('previousNr', None)
            nrd.previousRequestId = json_input.get('previousRequestId', None)
            nrd.priorityCd = json_input.get('priorityCd', None)
            nrd.priorityDate = json_input.get('priorityDate', None)
            nrd.requestTypeCd = json_input.get('requestTypeCd', None)
            nrd.request_action_cd = json_input.get('request_action_cd', None)
            nrd.stateCd = state
            nrd.tradeMark = json_input.get('tradeMark', None)
            nrd.userId = user.id
            nrd.xproJurisdiction = json_input.get('xproJurisdiction', None)

            if reset:
                # set the flag indicating that the NR has been reset
                nrd.hasBeenReset = True

                # add a generated comment re. this NR being reset
                json_input['comments'].append({'comment': 'This NR was RESET.'})

                # send the event to the namex emailer, to cancel the in-flight task, if there is one
                queue_util.publish_email_notification(nrd.nrNum, 'RESET')

            try:
                previousNr = json_input['previousNr']
                if previousNr:
                    nrd.previousRequestId = RequestDAO.find_by_nr(previousNr).requestId
            except AttributeError:
                nrd.previousRequestId = None
            except KeyError:
                nrd.previousRequestId = None

            # if we're changing to a completed or cancelled state, clear reset flag on NR record
            if state in State.COMPLETED_STATE + [State.CANCELLED]:
                nrd.hasBeenReset = False

            # check if any of the Oracle db fields have changed, so we can send them back
            is_changed__request = False
            is_changed__previous_request = False
            is_changed__request_state = False
            is_changed_consent = False
            if nrd.requestTypeCd != orig_nrd['requestTypeCd']:
                is_changed__request = True
            if nrd.expirationDate != orig_nrd['expirationDate']:
                is_changed__request = True
            if nrd.xproJurisdiction != orig_nrd['xproJurisdiction']:
                is_changed__request = True
            if nrd.additionalInfo != orig_nrd['additionalInfo']:
                is_changed__request = True
            if nrd.natureBusinessInfo != orig_nrd['natureBusinessInfo']:
                is_changed__request = True
            if nrd.previousRequestId != orig_nrd['previousRequestId']:
                is_changed__previous_request = True
            if nrd.stateCd != orig_nrd['state']:
                is_changed__request_state = True
            if nrd.consentFlag != orig_nrd['consentFlag']:
                is_changed_consent = True
                if nrd.consentFlag == 'R':
                    queue_util.publish_email_notification(nrd.nrNum, 'CONSENT_RECEIVED')

            # Need this for a re-open
            if nrd.stateCd != State.CONDITIONAL and is_changed__request_state:
                nrd.consentFlag = None
                nrd.consent_dt = None

            ### END request header ###

            ### APPLICANTS ###
            is_changed__applicant = False
            is_changed__address = False

            if nrd.applicants:
                applicants_d = nrd.applicants[0]
                orig_applicant = applicants_d.as_dict()
                appl = json_input.get('applicants', None)
                if appl:
                    errm = applicant_schema.validate(appl, partial=True)
                    if errm:
                        # return make_response(jsonify(errm), 400
                        MessageServices.add_message(MessageServices.ERROR, 'applicants_validation', errm)

                    # convert data to ascii, removing data that won't save to Oracle
                    applicants_d.lastName = convert_to_ascii(appl.get('lastName', None))
                    applicants_d.firstName = convert_to_ascii(appl.get('firstName', None))
                    applicants_d.middleName = convert_to_ascii(appl.get('middleName', None))
                    applicants_d.phoneNumber = convert_to_ascii(appl.get('phoneNumber', None))
                    applicants_d.faxNumber = convert_to_ascii(appl.get('faxNumber', None))
                    applicants_d.emailAddress = convert_to_ascii(appl.get('emailAddress', None))
                    applicants_d.contact = convert_to_ascii(appl.get('contact', None))
                    applicants_d.clientFirstName = convert_to_ascii(appl.get('clientFirstName', None))
                    applicants_d.clientLastName = convert_to_ascii(appl.get('clientLastName', None))
                    applicants_d.addrLine1 = convert_to_ascii(appl.get('addrLine1', None))
                    applicants_d.addrLine2 = convert_to_ascii(appl.get('addrLine2', None))
                    applicants_d.addrLine3 = convert_to_ascii(appl.get('addrLine3', None))
                    applicants_d.city = convert_to_ascii(appl.get('city', None))
                    applicants_d.postalCd = convert_to_ascii(appl.get('postalCd', None))
                    applicants_d.stateProvinceCd = convert_to_ascii(appl.get('stateProvinceCd', None))
                    applicants_d.countryTypeCd = convert_to_ascii(appl.get('countryTypeCd', None))

                    # check if any of the Oracle db fields have changed, so we can send them back
                    if applicants_d.lastName != orig_applicant['lastName']:
                        is_changed__applicant = True
                    if applicants_d.firstName != orig_applicant['firstName']:
                        is_changed__applicant = True
                    if applicants_d.middleName != orig_applicant['middleName']:
                        is_changed__applicant = True
                    if applicants_d.phoneNumber != orig_applicant['phoneNumber']:
                        is_changed__applicant = True
                    if applicants_d.faxNumber != orig_applicant['faxNumber']:
                        is_changed__applicant = True
                    if applicants_d.emailAddress != orig_applicant['emailAddress']:
                        is_changed__applicant = True
                    if applicants_d.contact != orig_applicant['contact']:
                        is_changed__applicant = True
                    if applicants_d.clientFirstName != orig_applicant['clientFirstName']:
                        is_changed__applicant = True
                    if applicants_d.clientLastName != orig_applicant['clientLastName']:
                        is_changed__applicant = True
                    if applicants_d.declineNotificationInd != orig_applicant['declineNotificationInd']:
                        is_changed__applicant = True
                    if applicants_d.addrLine1 != orig_applicant['addrLine1']:
                        is_changed__address = True
                    if applicants_d.addrLine2 != orig_applicant['addrLine2']:
                        is_changed__address = True
                    if applicants_d.addrLine3 != orig_applicant['addrLine3']:
                        is_changed__address = True
                    if applicants_d.city != orig_applicant['city']:
                        is_changed__address = True
                    if applicants_d.postalCd != orig_applicant['postalCd']:
                        is_changed__address = True
                    if applicants_d.stateProvinceCd != orig_applicant['stateProvinceCd']:
                        is_changed__address = True
                    if applicants_d.countryTypeCd != orig_applicant['countryTypeCd']:
                        is_changed__address = True

                else:
                    applicants_d.delete_from_db()
                    is_changed__applicant = True
                    is_changed__address = True

            ### END applicants ###

            ### NAMES ###
            # TODO: set consumptionDate not working -- breaks changing name values

            is_changed__name1 = False
            is_changed__name2 = False
            is_changed__name3 = False
            deleted_names = [False] * 3

            if len(nrd.names) == 0:
                new_name_choice = Name()
                new_name_choice.nrId = nrd.id

                # convert data to ascii, removing data that won't save to Oracle
                new_name_choice.name = convert_to_ascii(new_name_choice.name)

                nrd.names.append(new_name_choice)

            for nrd_name in nrd.names:
                orig_name = nrd_name.as_dict()

                for in_name in json_input.get('names', []):
                    if len(nrd.names) < in_name['choice']:
                        errors = names_schema.validate(in_name, partial=False)
                        if errors:
                            MessageServices.add_message(MessageServices.ERROR, 'names_validation', errors)
                            # return make_response(jsonify(errors), 400

                        # don't save if the name is blank
                        if in_name.get('name') and in_name.get('name') != '':
                            new_name_choice = Name()
                            new_name_choice.nrId = nrd.id
                            new_name_choice.choice = in_name.get('choice')
                            new_name_choice.conflict1 = in_name.get('conflict1')
                            new_name_choice.conflict2 = in_name.get('conflict2')
                            new_name_choice.conflict3 = in_name.get('conflict3')
                            new_name_choice.conflict1_num = in_name.get('conflict1_num')
                            new_name_choice.conflict2_num = in_name.get('conflict2_num')
                            new_name_choice.conflict3_num = in_name.get('conflict3_num')
                            new_name_choice.consumptionDate = in_name.get('consumptionDate')
                            new_name_choice.corpNum = in_name.get('corpNum')
                            new_name_choice.decision_text = in_name.get('decision_text')
                            new_name_choice.designation = in_name.get('designation')
                            new_name_choice.name_type_cd = in_name.get('name_type_cd')
                            new_name_choice.name = in_name.get('name')
                            new_name_choice.state = in_name.get('state')
                            new_name_choice.name = convert_to_ascii(new_name_choice.name.upper())

                            nrd.names.append(new_name_choice)

                            if new_name_choice.choice == 2:
                                is_changed__name2 = True
                            if new_name_choice.choice == 3:
                                is_changed__name3 = True

                    elif nrd_name.choice == in_name['choice']:
                        errors = names_schema.validate(in_name, partial=False)
                        if errors:
                            MessageServices.add_message(MessageServices.ERROR, 'names_validation', errors)
                            # return make_response(jsonify(errors), 400

                        nrd_name.choice = in_name.get('choice')
                        nrd_name.conflict1 = in_name.get('conflict1')
                        nrd_name.conflict2 = in_name.get('conflict2')
                        nrd_name.conflict3 = in_name.get('conflict3')
                        nrd_name.conflict1_num = in_name.get('conflict1_num')
                        nrd_name.conflict2_num = in_name.get('conflict2_num')
                        nrd_name.conflict3_num = in_name.get('conflict3_num')
                        nrd_name.consumptionDate = in_name.get('consumptionDate')
                        nrd_name.corpNum = in_name.get('corpNum')
                        nrd_name.decision_text = in_name.get('decision_text')
                        nrd_name.designation = in_name.get('designation')
                        nrd_name.name_type_cd = in_name.get('name_type_cd')
                        nrd_name.name = in_name.get('name')
                        nrd_name.state = in_name.get('state')
                        nrd_name.name = convert_to_ascii(nrd_name.name.upper())

                        # set comments (existing or cleared)
                        if in_name.get('comment', None) is not None:
                            # if there is a comment ID in data, just set it
                            if in_name['comment'].get('id', None) is not None:
                                nrd_name.commentId = in_name['comment'].get('id')

                            # if no comment id, it's a new comment, so add it
                            else:
                                # no business case for this at this point - this code will never run
                                pass

                        else:
                            nrd_name.comment = None

                        # convert data to ascii, removing data that won't save to Oracle
                        # - also force uppercase
                        nrd_name.name = convert_to_ascii(nrd_name.name)
                        if nrd_name.name is not None:
                            nrd_name.name = nrd_name.name.upper()

                        # check if any of the Oracle db fields have changed, so we can send them back
                        # - this is only for editing a name from the Edit NR section, NOT making a decision
                        if nrd_name.name != orig_name['name']:
                            if nrd_name.choice == 1:
                                is_changed__name1 = True
                                json_input['comments'].append(
                                    {
                                        'comment': 'Name choice 1 changed from {0} to {1}'.format(
                                            orig_name['name'], nrd_name.name
                                        )
                                    }
                                )
                            if nrd_name.choice == 2:
                                is_changed__name2 = True
                                if not nrd_name.name:
                                    deleted_names[nrd_name.choice - 1] = True
                                json_input['comments'].append(
                                    {
                                        'comment': 'Name choice 2 changed from {0} to {1}'.format(
                                            orig_name['name'], nrd_name.name
                                        )
                                    }
                                )
                            if nrd_name.choice == 3:
                                is_changed__name3 = True
                                if not nrd_name.name:
                                    deleted_names[nrd_name.choice - 1] = True
                                json_input['comments'].append(
                                    {
                                        'comment': 'Name choice 3 changed from {0} to {1}'.format(
                                            orig_name['name'], nrd_name.name
                                        )
                                    }
                                )
            ### END names ###

            ### COMMENTS ###

            # we only add new comments, we do not change existing comments
            # - we can find new comments in json as those with no ID
            # - This must come after names section above, to handle comments re. changed names.

            for in_comment in json_input['comments']:
                is_new_comment = False
                try:
                    if in_comment['id'] is None or in_comment['id'] == 0:
                        is_new_comment = True
                except KeyError:
                    is_new_comment = True
                if is_new_comment and in_comment['comment'] is not None:
                    new_comment = Comment()
                    new_comment.comment = convert_to_ascii(in_comment['comment'])
                    new_comment.examiner = user
                    new_comment.nrId = nrd.id

            ### END comments ###

            ### NWPTA ###

            is_changed__nwpta_ab = False
            is_changed__nwpta_sk = False

            if nrd.partnerNS.count() > 0:
                for nrd_nwpta in nrd.partnerNS.all():
                    orig_nwpta = nrd_nwpta.as_dict()

                    for in_nwpta in json_input['nwpta']:
                        if nrd_nwpta.partnerJurisdictionTypeCd == in_nwpta['partnerJurisdictionTypeCd']:
                            errors = nwpta_schema.validate(in_nwpta, partial=False)
                            if errors:
                                MessageServices.add_message(MessageServices.ERROR, 'nwpta_validation', errors)
                                # return make_response(jsonify(errors), 400

                            nwpta_schema.load(in_nwpta, instance=nrd_nwpta, partial=False)

                            # convert data to ascii, removing data that won't save to Oracle
                            nrd_nwpta.partnerName = convert_to_ascii(in_nwpta.get('partnerName'))
                            nrd_nwpta.partnerNameNumber = convert_to_ascii(in_nwpta.get('partnerNameNumber'))

                            # check if any of the Oracle db fields have changed, so we can send them back
                            tmp_is_changed = False
                            if nrd_nwpta.partnerNameTypeCd != orig_nwpta['partnerNameTypeCd']:
                                tmp_is_changed = True
                            if nrd_nwpta.partnerNameNumber != orig_nwpta['partnerNameNumber']:
                                tmp_is_changed = True
                            if nrd_nwpta.partnerNameDate != orig_nwpta['partnerNameDate']:
                                tmp_is_changed = True
                            if nrd_nwpta.partnerName != orig_nwpta['partnerName']:
                                tmp_is_changed = True
                            if tmp_is_changed:
                                if nrd_nwpta.partnerJurisdictionTypeCd == 'AB':
                                    is_changed__nwpta_ab = True
                                if nrd_nwpta.partnerJurisdictionTypeCd == 'SK':
                                    is_changed__nwpta_sk = True

            ### END nwpta ###

            # if there were errors, abandon changes and return the set of errors
            warning_and_errors = MessageServices.get_all_messages()
            if warning_and_errors:
                for we in warning_and_errors:
                    if we['type'] == MessageServices.ERROR:
                        return make_response(jsonify(errors=warning_and_errors), 400)
            if reset:
                nrd.expirationDate = None
                nrd.consentFlag = None
                nrd.consent_dt = None
                is_changed__request = True
                is_changed_consent = True

            else:
                change_flags = {
                    'is_changed__request': is_changed__request,
                    'is_changed__previous_request': is_changed__previous_request,
                    'is_changed__applicant': is_changed__applicant,
                    'is_changed__address': is_changed__address,
                    'is_changed__name1': is_changed__name1,
                    'is_changed__name2': is_changed__name2,
                    'is_changed__name3': is_changed__name3,
                    'is_changed__nwpta_ab': is_changed__nwpta_ab,
                    'is_changed__nwpta_sk': is_changed__nwpta_sk,
                    'is_changed__request_state': is_changed__request_state,
                    'is_changed_consent': is_changed_consent,
                }

                if any(value is True for value in change_flags.values()):
                    nrd.save_to_db()

                    # Delete any names that were blanked out
                    for nrd_name in nrd.names:
                        if deleted_names[nrd_name.choice - 1]:
                            nrd_name.delete_from_db()

            # if there were errors, return the set of errors
            warning_and_errors = MessageServices.get_all_messages()
            if warning_and_errors:
                for we in warning_and_errors:
                    if we['type'] == MessageServices.ERROR:
                        return make_response(jsonify(errors=warning_and_errors), 400)

            # Finally save the entire graph
            nrd.save_to_db()

            EventRecorder.record(user, Event.PUT, nrd, json_input)

        except ValidationError as ve:
            return make_response(jsonify(ve.messages), 400)

        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify(message='Request:{} not found'.format(nr)), 404)

        except Exception as err:
            current_app.logger.error('Error when replacing NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify(message='NR had an internal error'), 500)

        # if we're here, messaging only contains warnings
        warning_and_errors = MessageServices.get_all_messages()
        if warning_and_errors:
            current_app.logger.debug(nrd.json(), warning_and_errors)
            return make_response(jsonify(nameRequest=nrd.json(), warnings=warning_and_errors), 206)

        current_app.logger.debug(nrd.json())
        return make_response(jsonify(nrd.json()), 200)


@cors_preflight('GET')
@api.route('/<string:nr>/analysis/<int:choice>/<string:analysis_type>', methods=['GET', 'OPTIONS'])
class RequestsAnalysis(Resource):
    """Acting like a QUEUE this gets the next NR (just the NR number)
    and assigns it to your auth id

        :param nr (str): NameRequest Number in the format of 'NR 000000000'
        :param choice (int): name choice number (1..3)
        :param args: start: number of hits to start from, default is 0
        :param args: names_per_page: number of names to return per page, default is 50
        :param kwargs: __futures__
        :return: 200 - success; 40X for errors
    """

    START = 0
    ROWS = 50

    # @auth_services.requires_auth
    # noinspection PyUnusedLocal,PyUnusedLocal
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Performs name analysis for the given NR and name choice using the specified analysis type',
        params={
            'nr': 'NR number',
            'choice': 'Name choice number (1, 2, or 3)',
            'analysis_type': 'Type of analysis to perform (e.g., conflicts, histories, trademarks, restricted_words)',
            'start': 'Offset for results pagination (default: 0)',
            'rows': 'Number of results per page (default: 50)',
        },
        responses={
            200: 'Analysis results returned successfully',
            404: 'NR, name choice, or analysis type not found',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(nr, choice, analysis_type, *args, **kwargs):
        start = request.args.get('start', RequestsAnalysis.START)
        rows = request.args.get('rows', RequestsAnalysis.ROWS)

        if analysis_type not in ANALYTICS_VALID_ANALYSIS:
            return make_response(
                jsonify(
                    message='{analysis_type} is not a valid analysis type for that name choice'.format(
                        analysis_type=analysis_type
                    )
                ),
                404,
            )

        nrd = RequestDAO.find_by_nr(nr)

        if not nrd:
            return make_response(jsonify(message='{nr} not found'.format(nr=nr)), 404)

        nrd_name = next((name for name in nrd.names if name.choice == choice), None)

        if not nrd_name:
            return make_response(
                jsonify(message='Name choice:{choice} not found for {nr}'.format(nr=nr, choice=choice)), 404
            )

        if analysis_type in RestrictedWords.RESTRICTED_WORDS:
            results, msg, code = RestrictedWords.get_restricted_words_conditions(nrd_name.name)

        else:
            results, msg, code = SolrQueries.get_results(analysis_type, nrd_name.name, start=start, rows=rows)

        if code:
            return make_response(jsonify(message=msg), code)
        return make_response(jsonify(results), 200)


@cors_preflight('GET')
@api.route('/synonymbucket/<string:name>/<string:advanced_search>', methods=['GET', 'OPTIONS'])
class SynonymBucket(Resource):
    START = 0
    ROWS = 1000

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Fetches potential synonym conflicts for the given name using an optional advanced search filter',
        params={
            'name': 'The name to analyze for synonym conflicts',
            'advanced_search': 'Optional phrase to refine the conflict search (use * for no filter)',
            'start': 'Offset for pagination (default: 0)',
            'rows': 'Number of results to return (default: 1000)',
        },
        responses={
            200: 'Conflict results fetched successfully',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(name, advanced_search, *args, **kwargs):
        start = request.args.get('start', SynonymBucket.START)
        rows = request.args.get('rows', SynonymBucket.ROWS)
        exact_phrase = '' if advanced_search == '*' else advanced_search
        results, msg, code = SolrQueries.get_conflict_results(
            name.upper(), bucket='synonym', exact_phrase=exact_phrase, start=start, rows=rows
        )
        if code:
            return make_response(jsonify(message=msg), code)
        return make_response(jsonify(results), 200)


@cors_preflight('GET')
@api.route('/cobrsphonetics/<string:name>/<string:advanced_search>', methods=['GET', 'OPTIONS'])
class CobrsPhoneticBucket(Resource):
    START = 0
    ROWS = 500

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Fetches potential COBRS phonetic conflicts for the given name using an optional advanced search filter',
        params={
            'name': 'The name to analyze for phonetic conflict',
            'advanced_search': 'Optional phrase to refine the search (use * for no filter)',
            'start': 'Offset for pagination (default: 0)',
            'rows': 'Number of results to return (default: 500)',
        },
        responses={
            200: 'Conflict results fetched successfully',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(name, advanced_search, *args, **kwargs):
        start = request.args.get('start', CobrsPhoneticBucket.START)
        rows = request.args.get('rows', CobrsPhoneticBucket.ROWS)
        name = '' if name == '*' else name
        exact_phrase = '' if advanced_search == '*' else advanced_search
        results, msg, code = SolrQueries.get_conflict_results(
            name.upper(), bucket='cobrs_phonetic', exact_phrase=exact_phrase, start=start, rows=rows
        )
        if code:
            return make_response(jsonify(message=msg), code)
        return make_response(jsonify(results), 200)


@cors_preflight('GET')
@api.route('/phonetics/<string:name>/<string:advanced_search>', methods=['GET', 'OPTIONS'])
class PhoneticBucket(Resource):
    START = 0
    ROWS = 100000

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Fetches potential phonetic conflicts for the given name using an optional advanced search filter',
        params={
            'name': 'The name to analyze for phonetic conflicts',
            'advanced_search': 'Optional phrase to refine the search (use * for no filter)',
            'start': 'Offset for pagination (default: 0)',
            'rows': 'Number of results to return (default: 100000)',
        },
        responses={
            200: 'Conflict results fetched successfully',
            401: 'Unauthorized',
            500: 'Internal server error',
        },
    )
    def get(name, advanced_search, *args, **kwargs):
        start = request.args.get('start', PhoneticBucket.START)
        rows = request.args.get('rows', PhoneticBucket.ROWS)
        name = '' if name == '*' else name
        exact_phrase = '' if advanced_search == '*' else advanced_search
        results, msg, code = SolrQueries.get_conflict_results(
            name.upper(), bucket='phonetic', exact_phrase=exact_phrase, start=start, rows=rows
        )
        if code:
            return make_response(jsonify(message=msg), code)
        return make_response(jsonify(results), 200)


@cors_preflight('GET, PUT, PATCH')
@api.route('/<string:nr>/names/<int:choice>', methods=['GET', 'PUT', 'PATCH', 'OPTIONS'])
class NRNames(Resource):
    @staticmethod
    def common(nr, choice):
        """:returns: object, code, msg"""
        if not RequestDAO.validNRFormat(nr):
            return None, None, jsonify({'message': "NR is not a valid format 'NR 9999999'"}), 400

        nrd = RequestDAO.find_by_nr(nr)
        if not nrd:
            return None, None, jsonify({'message': '{nr} not found'.format(nr=nr)}), 404

        name = next((name for name in nrd.names if name.choice == choice), None)
        if not name:
            return (
                None,
                None,
                jsonify({'message': 'Choice {choice} for {nr} not found'.format(choice=choice, nr=nr)}),
                404,
            )

        return nrd, name, None, 200

    # noinspection PyUnusedLocal,PyUnusedLocal
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Fetches the name record for the specified name request and choice number',
        params={
            'nr': 'NR number',
            'choice': 'Choice number (1, 2, or 3)'
        },
        responses={
            200: 'Name record fetched successfully',
            400: 'Invalid NR format',
            404: 'Name Request or name choice not found',
            401: 'Unauthorized',
        },
    )
    def get(nr, choice, *args, **kwargs):
        nrd, nrd_name, msg, code = NRNames.common(nr, choice)
        if not nrd:
            return msg, code

        return names_schema.dumps(nrd_name).data, 200

    name_model = api.model('NameModel', {
        'choice': fields.Integer(description='Name choice number (1, 2, or 3)', example=1),
        'conflict1': fields.String(description='First conflict name'),
        'conflict2': fields.String(description='Second conflict name'),
        'conflict3': fields.String(description='Third conflict name'),
        'conflict1_num': fields.String(description='First conflict NR number'),
        'conflict2_num': fields.String(description='Second conflict NR number'),
        'conflict3_num': fields.String(description='Third conflict NR number'),
        'consumptionDate': fields.String(description='Consumption date in ISO format'),
        'corpNum': fields.String(description='Corporation number if consumed'),
        'decision_text': fields.String(description='Decision rationale or notes'),
        'designation': fields.String(description='Designation like INC, LTD, etc.'),
        'name_type_cd': fields.String(description='Name type code (e.g., CR, XPRO)'),
        'name': fields.String(description='The business name'),
        'state': fields.String(description='State of the name (e.g., APPROVED, REJECTED)'),
        'comment': fields.Nested(api.model('Comment', {
            'comment': fields.String(description='Comment about the name decision')
        }), description='Optional comment on the decision')
    })

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.expect(name_model)
    @api.doc(
        description='Replaces the entire name record for the specified Name Request and choice number. All fields must be supplied.',
        params={
            'nr': 'NR number',
            'choice': 'Choice number (1, 2, or 3)',
        },
        responses={
            200: 'Name updated successfully',
            400: 'Validation errors or missing input data',
            401: 'Unauthorized',
            403: 'User is not the active editor or NR is not in INPROGRESS',
            404: 'Name Request or name choice not found',
            500: 'Internal server error',
        },
    )
    def put(nr, choice, *args, **kwargs):
        json_data = request.get_json()
        if not json_data:
            return make_response(jsonify({'message': 'No input data provided'}), 400)

        errors = names_schema.validate(json_data, partial=False)
        if errors:
            return make_response(jsonify(errors), 400)

        if json_data['comment']:
            errors = name_comment_schema.validate(json_data['comment'], partial=True)
            if errors:
                return make_response(jsonify(errors), 400)

        nrd, nrd_name, msg, code = NRNames.common(nr, choice)
        if not nrd:
            return msg, code

        user = User.find_by_jwtToken(g.jwt_oidc_token_info)
        if not check_ownership(nrd, user):
            return make_response(jsonify({'message': 'You must be the active editor and it must be INPROGRESS'}), 403)

        nrd_name.choice = json_data.get('choice')
        nrd_name.conflict1 = json_data.get('conflict1')
        nrd_name.conflict2 = json_data.get('conflict2')
        nrd_name.conflict3 = json_data.get('conflict3')
        nrd_name.conflict1_num = json_data.get('conflict1_num')
        nrd_name.conflict2_num = json_data.get('conflict2_num')
        nrd_name.conflict3_num = json_data.get('conflict3_num')
        nrd_name.consumptionDate = json_data.get('consumptionDate')
        nrd_name.corpNum = json_data.get('corpNum')
        nrd_name.decision_text = json_data.get('decision_text')
        nrd_name.designation = json_data.get('designation')
        nrd_name.name_type_cd = json_data.get('name_type_cd')
        nrd_name.name = json_data.get('name')
        nrd_name.state = json_data.get('state')
        nrd_name.name = convert_to_ascii(nrd_name.name.upper())

        if json_data['comment'] is not None and json_data['comment']['comment'] is not None:
            comment_instance = Comment()
            name_comment_schema.load(json_data['comment'], instance=comment_instance, partial=True)
            comment_instance.examinerId = user.id
            comment_instance.nrId = nrd_name.nrId

            comment_instance.save_to_db()
            nrd_name.commentId = comment_instance.id
        else:
            nrd_name.comment = None

        try:
            nrd_name.save_to_db()
        except Exception as error:
            current_app.logger.error('Error on nrd_name update, Error:{0}'.format(error))
            return make_response(jsonify({'message': 'Error on name update, saving to the db.'}), 500)

        EventRecorder.record(user, Event.PUT, nrd, json_data)

        return make_response(
            jsonify(
                {'message': 'Replace {nr} choice:{choice} with {json}'.format(nr=nr, choice=choice, json=json_data)}
            ),
            200,
        )

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.expect(name_model)
    @api.doc(
        description='Partially updates the name record for the specified Name Request and choice number. Only the provided fields will be modified.',
        params={
            'nr': 'NR number',
            'choice': 'Choice number (1, 2, or 3)',
        },
        responses={
            200: 'Name patched successfully',
            400: 'Validation errors or missing input data',
            401: 'Unauthorized',
            403: 'User is not the active editor or NR is not in INPROGRESS',
            404: 'Name Request or name choice not found',
            500: 'Internal server error',
        },
    )
    def patch(nr, choice, *args, **kwargs):
        json_data = request.get_json()
        if not json_data:
            return make_response(jsonify({'message': 'No input data provided'}), 400)

        errors = names_schema.validate(json_data, partial=True)
        if errors:
            return make_response(jsonify(errors), 400)

        nrd, nrd_name, msg, code = NRNames.common(nr, choice)
        if not nrd:
            return msg, code

        user = User.find_by_jwtToken(g.jwt_oidc_token_info)
        if not check_ownership(nrd, user):
            return make_response(jsonify({'message': 'You must be the active editor and it must be INPROGRESS'}), 403)

        nrd_name.choice = json_data.get('choice')
        nrd_name.conflict1 = json_data.get('conflict1')
        nrd_name.conflict2 = json_data.get('conflict2')
        nrd_name.conflict3 = json_data.get('conflict3')
        nrd_name.conflict1_num = json_data.get('conflict1_num')
        nrd_name.conflict2_num = json_data.get('conflict2_num')
        nrd_name.conflict3_num = json_data.get('conflict3_num')
        nrd_name.consumptionDate = json_data.get('consumptionDate')
        nrd_name.corpNum = json_data.get('corpNum')
        nrd_name.decision_text = json_data.get('decision_text')
        nrd_name.designation = json_data.get('designation')
        nrd_name.name_type_cd = json_data.get('name_type_cd')
        nrd_name.name = json_data.get('name')
        nrd_name.state = json_data.get('state')
        nrd_name.name = convert_to_ascii(nrd_name.name.upper())
        nrd_name.save_to_db()

        EventRecorder.record(user, Event.PATCH, nrd, json_data)

        return make_response(jsonify({'message': 'Patched {nr} - {json}'.format(nr=nr, json=json_data)}), 200)


# TODO: This should be in it's own file, not in the requests
@cors_preflight('GET')
@api.route('/decisionreasons', methods=['GET', 'OPTIONS'])
class DecisionReasons(Resource):
    @staticmethod
    @cors.crossdomain(origin='*')
    @api.doc(
        description='Fetches the list of predefined decision reasons used when analyzing or approving name requests',
        responses={
            200: 'List of decision reasons fetched successfully',
            500: 'Internal server error',
        },
    )
    def get():
        response = []
        for reason in DecisionReason.query.order_by(DecisionReason.name).all():
            response.append(reason.json())
        return make_response(jsonify(response), 200)


@cors_preflight('GET')
@api.route('/<string:nr>/syncnr', methods=['GET', 'OPTIONS'])
class SyncNR(Resource):
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR])
    @api.doc(
        description='Fetches and syncs the name request data for the specified NR',
        params={'nr': 'NR number'},
        responses={
            200: 'Name Request synced and returned successfully',
            401: 'Unauthorized',
            404: 'Name Request not found',
            500: 'Internal server error',
        },
    )
    def get(nr):
        try:
            get_or_create_user_by_jwt(g.jwt_oidc_token_info)
            nrd = RequestDAO.find_by_nr(nr)
        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
        except Exception as err:
            current_app.logger.error('Error when patching NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        if not nrd:
            return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)

        return jsonify(RequestDAO.query.filter_by(nrNum=nr.upper()).first_or_404().json())


@cors_preflight('GET')
@api.route('/stats', methods=['GET', 'OPTIONS'])
class Stats(Resource):
    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.requires_auth
    @api.doc(
        description='Fetches name request stats completed in the past [timespan] hours, optionally filtered by user',
        params={
            'myStats': 'Set to true to filter results to current user only',
            'timespan': 'Time range in hours to look back (default: 1)',
            'currentpage': 'Pagination: current page number (default: 1)',
            'perpage': 'Pagination: number of results per page (default: 50)',
        },
        responses={
            200: 'Statistics returned successfully',
            401: 'Unauthorized',
            406: 'Invalid pagination parameters',
            500: 'Internal server error',
        },
    )
    def get(*args, **kwargs):
        user = None
        if bool(request.args.get('myStats', False)):
            user = get_or_create_user_by_jwt(g.jwt_oidc_token_info)

        # default is last 1 hour, but can be sent as parameter
        timespan = int(request.args.get('timespan', 1))

        # validate row & start params
        start = request.args.get('currentpage', 1)
        rows = request.args.get('perpage', 50)

        try:
            rows = int(rows)
            start = (int(start) - 1) * rows
        except Exception as err:
            current_app.logger.info('start or rows not an int, err: {}'.format(err))
            return make_response(jsonify({'message': 'paging parameters were not integers'}), 406)

        q = RequestDAO.query.filter(RequestDAO.stateCd.in_(State.COMPLETED_STATE)).filter(
            RequestDAO.lastUpdate
            >= text("(now() at time zone 'utc') - INTERVAL '{delay} HOURS'".format(delay=timespan))
        )
        if user:
            q = q.filter(RequestDAO.userId == user.id)
        q = q.order_by(RequestDAO.lastUpdate.desc())

        count_q = q.statement.with_only_columns([func.count()]).order_by(None)
        count = db.session.execute(count_q).scalar()

        q = q.offset(start)
        q = q.limit(rows)

        # current_app.logger.debug(str(q.statement.compile(
        #     dialect=postgresql.dialect(),
        #     compile_kwargs={"literal_binds": True}))
        # )

        requests = q.all()
        rep = {'numRecords': count, 'nameRequests': request_search_schemas.dump(requests)}
        return jsonify(rep)


@cors_preflight('POST')
@api.route('/<string:nr>/comments', methods=['POST', 'OPTIONS'])
class NRComment(Resource):
    @staticmethod
    def common(nr):
        """:returns: object, code, msg"""
        if not RequestDAO.validNRFormat(nr):
            return None, jsonify({'message': "NR is not a valid format 'NR 9999999'"}), 400

        nrd = RequestDAO.find_by_nr(nr)
        if not nrd:
            return None, jsonify({'message': '{nr} not found'.format(nr=nr)}), 404

        return nrd, None, 200

    @staticmethod
    @cors.crossdomain(origin='*')
    @jwt.has_one_of_roles([User.APPROVER, User.EDITOR])
    @api.expect(api.model('CommentInput', {'comment': fields.String(description='The comment text')}))
    @api.doc(
        description='Adds a comment to a name request',
        params={'nr': 'NR number'},
        responses={
            200: 'Comment successfully added',
            400: 'Missing or invalid input data',
            401: 'Unauthorized',
            404: 'Name Request not found or user not found',
            500: 'Internal server error',
        },
    )
    def post(nr, *args, **kwargs):
        json_data = request.get_json()

        if not json_data:
            return make_response(jsonify({'message': 'No input data provided'}), 400)

        nrd, msg, code = NRComment.common(nr)

        if not nrd:
            return msg, code

        errors = name_comment_schema.validate(json_data, partial=False)
        if errors:
            return make_response(jsonify(errors), 400)

        # find NR
        try:
            nrd = RequestDAO.find_by_nr(nr)
            if not nrd:
                return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)

        except NoResultFound:
            # not an error we need to track in the log
            return make_response(jsonify({'message': 'Request:{} not found'.format(nr)}), 404)
        except Exception as err:
            current_app.logger.error('Error when trying to post a comment NR:{0} Err:{1}'.format(nr, err))
            return make_response(jsonify({'message': 'NR had an internal error'}), 404)

        nr_id = nrd.id
        user = User.find_by_jwtToken(g.jwt_oidc_token_info)
        if user is None:
            return make_response(jsonify({'message': 'No User'}), 404)

        if json_data.get('comment') is None:
            return make_response(jsonify({'message': 'No comment supplied'}), 400)

        comment_instance = Comment()
        comment_instance.examinerId = user.id
        comment_instance.nrId = nr_id
        comment_instance.comment = convert_to_ascii(json_data.get('comment'))

        comment_instance.save_to_db()

        EventRecorder.record(user, Event.POST, nrd, json_data)
        return make_response(jsonify(comment_instance.as_dict()), 200)
