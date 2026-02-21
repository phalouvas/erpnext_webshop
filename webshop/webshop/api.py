# -*- coding: utf-8 -*-
# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.utils import cint

from erpnext.accounts.doctype.payment_request.payment_request import (
	ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST,
	cancel_old_payment_requests,
	get_amount,
	get_dummy_message,
	get_existing_payment_request_amount,
	get_gateway_details,
)
from erpnext.accounts.party import get_party_account, get_party_bank_account
from erpnext.accounts.utils import get_account_currency
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions

from webshop.webshop.product_data_engine.filters import ProductFiltersBuilder
from webshop.webshop.product_data_engine.query import ProductQuery
from webshop.webshop.doctype.override_doctype.item_group import get_child_groups_for_website


@frappe.whitelist(allow_guest=True)
def get_product_filter_data(query_args=None):
	"""
	Returns filtered products and discount filters.

	Args:
		query_args (dict): contains filters to get products list

	Query Args filters:
		search (str): Search Term.
		field_filters (dict): Keys include item_group, brand, etc.
		attribute_filters(dict): Keys include Color, Size, etc.
		start (int): Offset items by
		item_group (str): Valid Item Group
		from_filters (bool): Set as True to jump to page 1
	"""
	if isinstance(query_args, str):
		query_args = json.loads(query_args)

	query_args = frappe._dict(query_args or {})

	if query_args:
		search = query_args.get("search")
		field_filters = query_args.get("field_filters", {})
		attribute_filters = query_args.get("attribute_filters", {})
		start = cint(query_args.start) if query_args.get("start") else 0
		item_group = query_args.get("item_group")
		from_filters = query_args.get("from_filters")
	else:
		search, attribute_filters, item_group, from_filters = None, None, None, None
		field_filters = {}
		start = 0

	# if new filter is checked, reset start to show filtered items from page 1
	if from_filters:
		start = 0

	sub_categories = []
	if item_group:
		sub_categories = get_child_groups_for_website(item_group, immediate=True)

	engine = ProductQuery()

	try:
		result = engine.query(
			attribute_filters,
			field_filters,
			search_term=search,
			start=start,
			item_group=item_group,
		)
	except Exception:
		frappe.log_error("Product query with filter failed")
		return {"exc": "Something went wrong!"}

	# discount filter data
	filters = {}
	discounts = result["discounts"]

	if discounts:
		filter_engine = ProductFiltersBuilder()
		filters["discount_filters"] = filter_engine.get_discount_filters(discounts)

	return {
		"items": result["items"] or [],
		"filters": filters,
		"settings": engine.settings,
		"sub_categories": sub_categories,
		"items_count": result["items_count"],
	}


@frappe.whitelist(allow_guest=True)
def get_guest_redirect_on_action():
	return frappe.db.get_single_value("Webshop Settings", "redirect_on_action")


@frappe.whitelist(allow_guest=True)
def make_payment_request(**args):
	"""Create payment request for checkout and other supported flows.

	For shopping cart orders, website permission is used so guest/portal checkout
	continues to work even if backend DocType create permission is restricted.
	"""
	args = frappe._dict(args)

	if args.dt not in ALLOWED_DOCTYPES_FOR_PAYMENT_REQUEST:
		frappe.throw(_("Payment Requests cannot be created against: {0}").format(frappe.bold(args.dt)))

	if args.dn and not isinstance(args.dn, str):
		frappe.throw(_("Invalid parameter. 'dn' should be of type str"))

	ref_doc = args.ref_doc or frappe.get_doc(args.dt, args.dn)

	if args.order_type == "Shopping Cart":
		if not frappe.has_website_permission(ref_doc):
			frappe.throw(_("Not Permitted"), frappe.PermissionError)
	else:
		frappe.has_permission("Payment Request", "create", throw=True)
		frappe.has_permission(args.dt, "read", args.dn, throw=True)

	if not args.get("company"):
		args.company = ref_doc.company
	gateway_account = get_gateway_details(args) or frappe._dict()

	grand_total = get_amount(ref_doc, gateway_account.get("payment_account"))
	if not grand_total:
		frappe.throw(_("Payment Entry is already created"))

	if args.loyalty_points and ref_doc.doctype == "Sales Order":
		from erpnext.accounts.doctype.loyalty_program.loyalty_program import validate_loyalty_points

		loyalty_amount = validate_loyalty_points(ref_doc, int(args.loyalty_points))
		ref_doc.db_update()
		grand_total = grand_total - loyalty_amount

	existing_payment_request_amount = get_existing_payment_request_amount(ref_doc)

	def validate_and_calculate_grand_total(current_total, existing_total):
		current_total -= existing_total
		if not current_total:
			frappe.throw(_("Payment Request is already created"))
		return current_total

	if existing_payment_request_amount:
		if args.order_type == "Shopping Cart":
			if get_existing_payment_request_amount(
				ref_doc, ["Initiated", "Partially Paid", "Payment Ordered", "Paid"]
			):
				grand_total = validate_and_calculate_grand_total(grand_total, existing_payment_request_amount)
			else:
				cancel_old_payment_requests(ref_doc.doctype, ref_doc.name)
		else:
			grand_total = validate_and_calculate_grand_total(grand_total, existing_payment_request_amount)

	draft_payment_request = frappe.db.get_value(
		"Payment Request",
		{"reference_doctype": ref_doc.doctype, "reference_name": ref_doc.name, "docstatus": 0},
	)

	if draft_payment_request:
		frappe.db.set_value(
			"Payment Request", draft_payment_request, "grand_total", grand_total, update_modified=False
		)
		pr = frappe.get_doc("Payment Request", draft_payment_request)
	else:
		bank_account = (
			get_party_bank_account(args.get("party_type"), args.get("party"))
			if args.get("party_type")
			else ""
		)
		pr = frappe.new_doc("Payment Request")

		if not args.get("payment_request_type"):
			args["payment_request_type"] = (
				"Outward" if args.get("dt") in ["Purchase Order", "Purchase Invoice"] else "Inward"
			)

		party_type = args.get("party_type") or "Customer"
		party_account_currency = ref_doc.get("party_account_currency")

		if not party_account_currency:
			party_account = get_party_account(party_type, ref_doc.get(party_type.lower()), ref_doc.company)
			party_account_currency = get_account_currency(party_account)

		pr.update(
			{
				"payment_gateway_account": gateway_account.get("name"),
				"payment_gateway": gateway_account.get("payment_gateway"),
				"payment_account": gateway_account.get("payment_account"),
				"payment_channel": gateway_account.get("payment_channel"),
				"payment_request_type": args.get("payment_request_type"),
				"currency": ref_doc.currency,
				"party_account_currency": party_account_currency,
				"grand_total": grand_total,
				"mode_of_payment": args.mode_of_payment,
				"email_to": args.recipient_id or ref_doc.owner,
				"subject": _("Payment Request for {0}").format(args.dn),
				"message": gateway_account.get("message") or get_dummy_message(ref_doc),
				"reference_doctype": ref_doc.doctype,
				"reference_name": ref_doc.name,
				"company": ref_doc.get("company"),
				"party_type": party_type,
				"party": args.get("party") or ref_doc.get("customer"),
				"bank_account": bank_account,
				"party_name": args.get("party_name") or ref_doc.get("customer_name"),
				"make_sales_invoice": args.make_sales_invoice or args.order_type == "Shopping Cart",
				"mute_email": (
					args.mute_email
					or args.order_type == "Shopping Cart"
					or gateway_account.get("payment_channel", "Email") != "Email"
				),
				"phone_number": args.get("phone_number") if args.get("phone_number") else None,
			}
		)

		pr.update({"cost_center": ref_doc.get("cost_center"), "project": ref_doc.get("project")})
		for dimension in get_accounting_dimensions():
			pr.update({dimension: ref_doc.get(dimension)})

		if frappe.db.get_single_value("Accounts Settings", "create_pr_in_draft_status", cache=True):
			pr.insert(ignore_permissions=True)
		if args.submit_doc:
			if pr.get("__unsaved"):
				pr.insert(ignore_permissions=True)
			pr.submit()

	if args.order_type == "Shopping Cart":
		frappe.db.commit()
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = pr.get_payment_url()

	if args.return_doc:
		return pr

	return pr.as_dict()
