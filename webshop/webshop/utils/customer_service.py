"""Webshop adapter for Digital Subscriptions customer orchestration."""

import time

import frappe

logger = frappe.logger("webshop")


def get_or_create_customer_for_user(user=None, cart_settings=None):
    """
    Create or return a customer for website users via Digital Subscriptions.

    Args:
        user (str): Email address of user (default: current session user)
        cart_settings: Unused; kept for backward compatibility.

    Returns:
        Customer document or None if user is not a website user or not a customer role
    """
    if not user:
        user = frappe.session.user

    if frappe.db.get_value("User", user, "user_type") != "Website User":
        return None

    # Check if user should be a customer based on Portal Settings
    portal_settings = frappe.get_single("Portal Settings")
    if portal_settings.default_role != "Customer":
        # User is not configured to be a customer
        return None

    user_roles = frappe.get_roles(user)
    if portal_settings.default_role not in user_roles:
        # User doesn't have customer role
        return None

    # Acquire distributed lock for this user
    lock_key = f"customer_creation_lock:{user}"
    if not _acquire_lock(lock_key):
        # Wait and retry once, then check if customer was created by another process
        time.sleep(0.5)
        customer = get_customer_for_user(user)
        if customer:
            return customer
        # If still no customer after waiting, try one more time
        time.sleep(0.5)
        if not _acquire_lock(lock_key):
            # Could not acquire lock, log and return None to avoid blocking
            frappe.log_error(
                f"Could not acquire lock for customer creation for user {user}",
                "Customer Creation Lock Error"
            )
            return None

    try:
        # Double-check pattern: after acquiring lock, check if customer already exists
        customer = get_customer_for_user(user)
        if customer:
            logger.info(f"Customer already exists for user {user}: {customer.name}")
            return customer

        logger.info(f"Delegating customer creation to digital_subscriptions for user {user}")
        from digital_subscriptions.overrides import create_customer_or_supplier

        party = create_customer_or_supplier(user=user)
        if party and party.doctype == "Customer":
            logger.info(f"Customer created via digital_subscriptions for user {user}: {party.name}")
            return party

        return get_customer_for_user(user)
    finally:
        # Release lock
        _release_lock(lock_key)


def get_customer_for_user(user):
    """
    Find existing customer linked to user's contact.

    Returns:
        Customer document or None
    """
    customer_name = frappe.db.get_value(
        "Portal User",
        {"parenttype": "Customer", "user": user},
        "parent",
    )
    if customer_name and frappe.db.exists("Customer", customer_name):
        return frappe.get_doc("Customer", customer_name)

    return None


def _acquire_lock(lock_key, timeout=30):
    """
    Acquire distributed lock using frappe.cache().

    Returns:
        bool: True if lock acquired, False otherwise
    """
    try:
        # nx=True means set only if not exists, ex=timeout sets expiry in seconds
        return frappe.cache().set(lock_key, "1", ex=timeout, nx=True)
    except Exception:
        # If cache doesn't support nx parameter, fall back to simpler locking
        # This is less safe but better than nothing
        try:
            if frappe.cache().get(lock_key):
                return False
            frappe.cache().set(lock_key, "1", ex=timeout)
            return True
        except Exception:
            # Last resort: log and proceed without locking
            frappe.log_error(
                f"Cache locking failed for key {lock_key}",
                "Customer Service Lock Error"
            )
            return True  # Proceed anyway to avoid blocking


def _release_lock(lock_key):
    """
    Release distributed lock.
    """
    try:
        frappe.cache().delete(lock_key)
    except Exception:
        pass