# -*- coding: utf-8 -*-
# Copyright (c) 2020, Aakvatech and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
from frappe import _
from erpnext.healthcare.doctype.patient_appointment.patient_appointment import get_appointment_item, check_is_new_patient
from erpnext.healthcare.doctype.healthcare_settings.healthcare_settings import get_receivable_account
from erpnext.healthcare.utils import check_fee_validity, get_service_item_and_practitioner_charge
from frappe.utils import getdate
from frappe.model.mapper import get_mapped_doc
from csf_tz.nhif.api.token import get_nhifservice_token
from erpnext import get_company_currency, get_default_company
import json
import requests
from time import sleep
from csf_tz.nhif.doctype.nhif_product.nhif_product import add_product
from csf_tz.nhif.doctype.nhif_scheme.nhif_scheme import add_scheme
# from csf_tz import console



@frappe.whitelist()
def get_paid_amount(insurance_subscription, billing_item, company):
    paid_amount = 0
    healthcare_insurance_coverage_plan = frappe.get_value(
        "Healthcare Insurance Subscription", insurance_subscription, "healthcare_insurance_coverage_plan")
    pric_list = frappe.get_value(
        "Healthcare Insurance Coverage Plan", healthcare_insurance_coverage_plan, "price_list")
    company_currency = frappe.get_value("Company", company, "default_currency")

    item_prices_data = frappe.get_all("Item Price",
                                      fields=[
                                          "item_code", "price_list_rate", "currency"],
                                      filters={
                                          'price_list': pric_list, 'item_code': billing_item, 'currency': company_currency},
                                      order_by="valid_from desc")
    if len(item_prices_data):
        paid_amount = item_prices_data[0].price_list_rate

    return paid_amount


@frappe.whitelist()
def invoice_appointment(name):
    appointment_doc = frappe.get_doc("Patient Appointment", name)
    automate_invoicing = frappe.db.get_single_value(
        'Healthcare Settings', 'automate_appointment_invoicing')
    appointment_invoiced = frappe.db.get_value(
        'Patient Appointment', appointment_doc.name, 'invoiced')
    enable_free_follow_ups = frappe.db.get_single_value(
        'Healthcare Settings', 'enable_free_follow_ups')
    if enable_free_follow_ups:
        fee_validity = check_fee_validity(appointment_doc)
        if fee_validity and fee_validity.status == 'Completed':
            fee_validity = None
        elif not fee_validity:
            if frappe.db.exists('Fee Validity Reference', {'appointment': appointment_doc.name}):
                return
            if check_is_new_patient(appointment_doc.patient, appointment_doc.name):
                return
    else:
        fee_validity = None

    if not automate_invoicing and not appointment_doc.insurance_subscription and appointment_doc.mode_of_payment and not appointment_invoiced and not fee_validity:
        sales_invoice = frappe.new_doc('Sales Invoice')
        sales_invoice.patient = appointment_doc.patient
        sales_invoice.customer = frappe.get_value(
            'Patient', appointment_doc.patient, 'customer')
        sales_invoice.appointment = appointment_doc.name
        sales_invoice.due_date = getdate()
        sales_invoice.company = appointment_doc.company
        sales_invoice.debit_to = get_receivable_account(
            appointment_doc.company)

        item = sales_invoice.append('items', {})
        item = get_appointment_item(appointment_doc, item)

        # Add payments if payment details are supplied else proceed to create invoice as Unpaid
        if appointment_doc.mode_of_payment and appointment_doc.paid_amount:
            sales_invoice.is_pos = 1
            payment = sales_invoice.append('payments', {})
            payment.mode_of_payment = appointment_doc.mode_of_payment
            payment.amount = appointment_doc.paid_amount

        sales_invoice.set_missing_values(for_validate=True)
        sales_invoice.flags.ignore_mandatory = True
        sales_invoice.save(ignore_permissions=True)
        sales_invoice.submit()
        frappe.msgprint(_('Sales Invoice {0} created'.format(
            sales_invoice.name)), alert=True)
        appointment_doc = frappe.get_doc(
            "Patient Appointment", appointment_doc.name)
        appointment_doc.ref_sales_invoice = sales_invoice.name
        appointment_doc.invoiced = 1
        appointment_doc.save()
        return "true"


@frappe.whitelist()
def get_consulting_charge_item(appointment_type, practitioner):
    charge_item = ""
    is_inpatient = frappe.get_value("Appointment Type", appointment_type, "ip")
    field_name = "inpatient_visit_charge_item" if is_inpatient else "op_consulting_charge_item"
    charge_item = frappe.get_value(
        "Healthcare Practitioner", practitioner, field_name)
    return charge_item


@frappe.whitelist()
def get_consulting_charge_amount(appointment_type, practitioner):
    charge_amount = ""
    is_inpatient = frappe.get_value("Appointment Type", appointment_type, "ip")
    field_name = "inpatient_visit_charge" if is_inpatient else "op_consulting_charge"
    charge_amount = frappe.get_value(
        "Healthcare Practitioner", practitioner, field_name)
    return charge_amount    


def make_vital(appointment_doc, method):
    if not appointment_doc.ref_vital_signs and (appointment_doc.invoiced or appointment_doc.insurance_claim):
        vital_doc = frappe.get_doc(dict(
            doctype="Vital Signs",
            patient=appointment_doc.patient,
            appointment=appointment_doc.name,
            company=appointment_doc.company,
        ))
        vital_doc.save(ignore_permissions=True)
        appointment_doc.ref_vital_signs = vital_doc.name
        # console(vital_doc)
        frappe.msgprint(_('Vital Signs {0} created'.format(
            vital_doc.name)), alert=True)


def make_encounter(vital_doc, method):
    source_name = vital_doc.appointment
    target_doc = None
    appointment_doc = get_mapped_doc('Patient Appointment', source_name, {
        'Patient Appointment': {
            'doctype': 'Patient Encounter',
            'field_map': [
                ['appointment', 'name'],
                ['patient', 'patient'],
                ['practitioner', 'practitioner'],
                ['medical_department', 'department'],
                ['patient_sex', 'patient_sex'],
                ['invoiced', 'invoiced'],
                ['company', 'company'],
                ['appointment_type', 'appointment_type']
            ]
        }
    }, target_doc)

    appointment_doc.save(ignore_permissions=True)
    frappe.msgprint(_('Patient Encounter {0} created'.format(
        appointment_doc.name)), alert=True)


# def update_paid_amount(appointment_doc, method):
#     if appointment_doc.insurance_subscription:
#         if not appointment_doc.insurance_subscription or not appointment_doc.billing_item:
#             return
#         appointment_doc.paid_amount = get_paid_amount(
#             appointment_doc.insurance_subscription, appointment_doc.billing_item, appointment_doc.company)
#     elif appointment_doc.mode_of_payment:
#         if not appointment_doc.practitioner or not appointment_doc.insurance_subscription:
#             return
#         appointment_doc.paid_amount = (
#             appointment_doc.appointment_type, appointment_doc.practitioner)

@frappe.whitelist()
def get_authorization_num(insurance_subscription, company, appointment_type, referral_no=""):
    enable_nhif_api = frappe.get_value("Company NHIF Settings", company, "enable")
    if not enable_nhif_api:
        frappe.msgprint(_("Company {0} not enabled for NHIF Integration".format(company)), alert=True)
        return
    card_no = frappe.get_value("Healthcare Insurance Subscription", insurance_subscription, "coverage_plan_card_number")
    if not card_no:
        frappe.msgprint(_("Please set Card No in Healthcare Insurance Subscription {0}".format(insurance_subscription)))
        return
    card_no = "CardNo=" + str(card_no)
    visit_type_id = "&VisitTypeID=" + frappe.get_value("Appointment Type", appointment_type, "visit_type_id")[:1]
    referral_no = "&ReferralNo=" + str(referral_no) 
    # remarks = "&Remarks=" + ""
    token = get_nhifservice_token(company)
    
    nhifservice_url = frappe.get_value("Company NHIF Settings", company, "nhifservice_url")
    headers = {
        "Authorization" : "Bearer " + token
    }
    url = str(nhifservice_url) + "/nhifservice/breeze/verification/AuthorizeCard?" + card_no + visit_type_id + referral_no # + remarks
    for i in range(3):
        try:
            r = requests.get(url, headers = headers, timeout=5)
            r.raise_for_status()
            frappe.logger().debug({"webhook_success": r.text})
            if json.loads(r.text):
                card = json.loads(r.text)
                # console(card)
                add_scheme(card.get("SchemeID"), card.get("SchemeName"))
                add_product(card.get("ProductCode"), card.get("ProductName"))
                frappe.msgprint(_(card["Remarks"]), alert=True)
                return card["AuthorizationNo"]
            else:
                frappe.throw(json.loads(r.text))
        except Exception as e:
            frappe.logger().debug({"webhook_error": e, "try": i + 1})
            sleep(3 * i + 1)
            if i != 2:
                continue
            else:
                raise e
