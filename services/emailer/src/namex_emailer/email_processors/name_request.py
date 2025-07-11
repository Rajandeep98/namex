# Copyright © 2020 Province of British Columbia
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
"""Email processing rules and actions for Name Request Payment Completion."""

from __future__ import annotations

import base64
from http import HTTPStatus

import requests
from flask import current_app, request
from gcp_queue.logging import structured_log
from jinja2 import Template

from namex_emailer.email_processors import get_main_template, substitute_template_parts
from namex_emailer.services.helpers import get_bearer_token, query_nr_number


def process(email_info: dict) -> dict:
    """Build the email for Name Request notification."""
    structured_log(request, "DEBUG", f"NR_notification: {email_info}")
    nr_number = email_info.data.get("request", {}).get("header", {}).get("nrNum", "")
    payment_token = email_info.data.get("request", {}).get("paymentToken", "")

    # get nr data
    nr_response = query_nr_number(nr_number)
    if nr_response.status_code != HTTPStatus.OK:
        structured_log(request, "ERROR", f"Failed to get nr info for name request: {nr_number}")
        return {}
    nr_data = nr_response.json()
    request_action = nr_data["request_action_cd"]

    template = get_main_template(request_action, "NR-PAID.html")
    filled_template = substitute_template_parts(template)
    # render template with vars
    mail_template = Template(filled_template, autoescape=True)
    html_out = mail_template.render(identifier=nr_number)

    # get attachments
    pdfs = _get_pdfs(nr_data["id"], payment_token)
    if not pdfs:
        return {}

    # get recipients
    recipients = nr_data["applicants"]["emailAddress"]
    if not recipients:
        return {}

    subject = f"{nr_number} - Receipt from Corporate Registry"

    return {
        "recipients": recipients,
        "requestBy": "BCRegistries@gov.bc.ca",
        "content": {"subject": subject, "body": f"{html_out}", "attachments": pdfs},
    }


def _get_pdfs(nr_id: str, payment_token: str) -> list:
    """Get the receipt for the name request application."""
    pdfs = []
    token = get_bearer_token()
    if not token or not nr_id or not payment_token:
        return []

    # get nr payments
    nr_payments = requests.get(
        f"{current_app.config.get('NAMEX_SVC_URL')}/payments/{nr_id}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    if nr_payments.status_code != HTTPStatus.OK:
        structured_log(request, "ERROR", f"Failed to get payment info for name request id: {nr_id}")
        return []

    # find specific payment corresponding to payment token
    payment_id = ""
    for payment in nr_payments.json():
        if payment_token == payment["token"]:
            payment_id = payment["id"]
    if not payment_id:
        structured_log(
            request,
            "ERROR",
            f"No matching payment info found for name request id: {nr_id}, payment token: {payment_token}",
        )
        return []

    # get receipt
    receipt = requests.post(
        f"{current_app.config.get('NAMEX_SVC_URL')}/payments/{payment_id}/receipt",
        json={},
        headers={"Accept": "application/pdf", "Authorization": f"Bearer {token}"},
    )
    if receipt.status_code != HTTPStatus.OK:
        structured_log(request, "ERROR", f"Failed to get receipt pdf for name request id: {nr_id}")
        return []

    # add receipt to pdfs
    receipt_encoded = base64.b64encode(receipt.content)
    pdfs.append(
        {
            "fileName": "Receipt.pdf",
            "fileBytes": receipt_encoded.decode("utf-8"),
            "fileUrl": "",
            "attachOrder": "1",
        }
    )
    return pdfs
