from contextlib import contextmanager

import frappe
from frappe.utils import get_url

from erpnext.accounts.doctype.payment_request.payment_request import (
    PaymentRequest as OriginalPaymentRequest,
)


@contextmanager
def elevated_permissions():
    """Temporarily evaluate permission checks as Administrator.

    Only the session user identity used for permission checks is changed; the
    session data itself is left intact so the visitor stays logged in.
    """
    original_user = frappe.session.user
    if original_user == "Administrator":
        yield
        return

    frappe.session.user = "Administrator"
    frappe.local.user_perms = None
    frappe.local.role_permissions = {}
    try:
        yield
    finally:
        frappe.session.user = original_user
        frappe.local.user_perms = None
        frappe.local.role_permissions = {}


class PaymentRequest(OriginalPaymentRequest):
    def validate(self):
        # ERPNext now checks `read` permission on the reference document here.
        # Website/portal checkout has already been authorised through
        # `frappe.has_website_permission`, so honour that decision instead of
        # failing on backend role permissions the customer will never have.
        if not self.flags.get("ignore_reference_permission"):
            return super().validate()

        with elevated_permissions():
            return super().validate()

    def on_payment_authorized(self, status=None):
        if not status:
            return

        if status not in ("Authorized", "Completed"):
            return

        if not hasattr(frappe.local, "session"):
            return

        if frappe.local.session.user == "Guest":
            return

        cart_settings = frappe.get_doc("Webshop Settings")

        if not cart_settings.enabled:
            return

        success_url = cart_settings.payment_success_url
        redirect_to = get_url("/orders/{0}".format(self.reference_name))

        if success_url:
            redirect_to = (
                {
                    "Orders": "/orders",
                    "Invoices": "/invoices",
                    "My Account": "/me",
                }
            ).get(success_url, "/me")

        # Payment finalization requires backend permissions that website/portal
        # users do not have (e.g. read access on Sales Order/Purchase Invoice),
        # so run it with an elevated session user.
        with elevated_permissions():
            self.set_as_paid()

        return redirect_to

    @staticmethod
    def get_gateway_details(args):
        if args.order_type != "Shopping Cart":
            return super().get_gateway_details(args)

        cart_settings = frappe.get_doc("Webshop Settings")
        gateway_account = cart_settings.payment_gateway_account
        return super().get_payment_gateway_account(gateway_account)
