import frappe
from frappe.utils import get_url

from erpnext.accounts.doctype.payment_request.payment_request import (
    PaymentRequest as OriginalPaymentRequest,
)


class PaymentRequest(OriginalPaymentRequest):
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
        # users do not have (e.g. read access on Sales Order/Purchase Invoice).
        # Temporarily set session user to Administrator — this only changes the
        # user identity for permission checks, keeping session data intact so
        # the user stays logged in after the payment completes.
        original_user = frappe.session.user
        frappe.session.user = "Administrator"
        frappe.local.user_perms = None
        frappe.local.role_permissions = {}
        try:
            self.set_as_paid()
        finally:
            frappe.session.user = original_user
            frappe.local.user_perms = None
            frappe.local.role_permissions = {}

        return redirect_to

    @staticmethod
    def get_gateway_details(args):
        if args.order_type != "Shopping Cart":
            return super().get_gateway_details(args)

        cart_settings = frappe.get_doc("Webshop Settings")
        gateway_account = cart_settings.payment_gateway_account
        return super().get_payment_gateway_account(gateway_account)
