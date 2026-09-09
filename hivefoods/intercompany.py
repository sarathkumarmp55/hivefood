"""Head-office to branch sales.

When a Sales Invoice to an internal customer that represents another company is submitted:
1. The matching Purchase Invoice (or Debit Note for a return) is created as a draft in the
   branch company.
2. For a normal sale, the branch selling price of each item is rolled: the current Item
   Price is closed the day before the invoice date and a new one is created with
   rate = purchase rate + profit % (Hivefoods Settings).

Failures never block the Sales Invoice; they are rolled back to a savepoint, logged in
Error Log and shown to the user so the Purchase Invoice can be created manually.
"""

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_link_to_form, getdate

SAVEPOINT = "hivefoods_intercompany"


# --------------------------------------------------------------------------- hooks


def on_sales_invoice_submit(doc, method=None):
	if not is_intercompany_sale(doc):
		return
	settings = frappe.get_cached_doc("Hivefoods Settings")
	if not settings.auto_create_purchase_invoice:
		return
	if frappe.db.exists(
		"Purchase Invoice", {"inter_company_invoice_reference": doc.name, "docstatus": ("<", 2)}
	):
		return

	frappe.db.savepoint(SAVEPOINT)
	try:
		pi = make_debit_note(doc, settings) if doc.is_return else make_purchase_invoice(doc, settings)
		if not pi:
			return
		if not doc.is_return and settings.update_selling_price:
			update_selling_prices(pi, settings)
		frappe.msgprint(
			_("Purchase Invoice {0} created as draft in {1}").format(
				get_link_to_form("Purchase Invoice", pi.name), frappe.bold(pi.company)
			),
			alert=True,
			indicator="green",
		)
	except Exception as e:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(
			title=f"Inter-company Purchase Invoice failed for {doc.name}",
			message=frappe.get_traceback(),
		)
		frappe.msgprint(
			_("The inter-company Purchase Invoice could not be created automatically: {0}").format(e),
			indicator="orange",
		)


def on_sales_invoice_cancel(doc, method=None):
	if not is_intercompany_sale(doc):
		return
	if not cint(frappe.db.get_single_value("Hivefoods Settings", "delete_draft_on_cancel")):
		return
	for name in frappe.get_all(
		"Purchase Invoice",
		filters={"inter_company_invoice_reference": doc.name, "docstatus": 0},
		pluck="name",
	):
		frappe.delete_doc("Purchase Invoice", name, ignore_permissions=True)
		frappe.msgprint(_("Draft Purchase Invoice {0} deleted").format(name), alert=True)


# --------------------------------------------------------------------------- helpers


def is_intercompany_sale(si):
	return bool(
		si.get("is_internal_customer") and si.represents_company and si.represents_company != si.company
	)


def get_company_setting(settings, company):
	for row in settings.company_settings:
		if row.company == company:
			return row
	return None


def get_receiving_warehouse(settings, company):
	row = get_company_setting(settings, company)
	if row and row.receiving_warehouse:
		return row.receiving_warehouse
	warehouse = frappe.db.get_value(
		"POS Profile", {"company": company, "disabled": 0}, "warehouse", order_by="name"
	)
	if warehouse:
		return warehouse
	default = frappe.db.get_single_value("Stock Settings", "default_warehouse")
	if default and frappe.db.get_value("Warehouse", default, "company") == company:
		return default
	frappe.throw(
		_("No receiving warehouse for {0}. Add it in Hivefoods Settings or create a POS Profile.").format(
			company
		)
	)


def get_selling_price_list(settings, company):
	row = get_company_setting(settings, company)
	return (row and row.selling_price_list) or "Standard Selling"


def _run_as_administrator(fn, *args, **kwargs):
	"""Branch users are restricted to their own company by User Permissions, so the
	mapping into the other company runs as Administrator."""
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		return fn(*args, **kwargs)
	finally:
		frappe.set_user(user)


def _finalise_and_insert(pi, si, settings, owner):
	pi.inter_company_invoice_reference = si.name
	pi.set_posting_time = 1
	pi.posting_date = si.posting_date
	pi.due_date = si.due_date or si.posting_date
	pi.bill_no = si.name
	pi.bill_date = si.posting_date
	pi.update_stock = si.update_stock
	pi.remarks = _("Auto-created from {0}").format(si.name)
	if pi.update_stock:
		warehouse = get_receiving_warehouse(settings, pi.company)
		for item in pi.items:
			item.warehouse = warehouse
	pi.flags.ignore_permissions = True
	pi.insert()
	if owner and owner != pi.owner:
		pi.db_set("owner", owner, update_modified=False)
	return pi


def make_purchase_invoice(si, settings):
	from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice

	owner = frappe.session.user

	def _make():
		pi = make_inter_company_purchase_invoice(si.name)
		return _finalise_and_insert(pi, si, settings, owner)

	return _run_as_administrator(_make)


def make_debit_note(si, settings):
	"""Credit Note in head office -> Debit Note against the original branch Purchase Invoice."""
	from erpnext.accounts.doctype.purchase_invoice.purchase_invoice import make_debit_note as _make_debit_note

	original_pi = si.return_against and frappe.db.get_value(
		"Purchase Invoice",
		{"inter_company_invoice_reference": si.return_against, "docstatus": 1},
		"name",
	)
	if not original_pi:
		frappe.msgprint(
			_("No submitted Purchase Invoice found for {0}; create the Debit Note manually.").format(
				si.return_against or si.name
			),
			indicator="orange",
		)
		return None

	return_qty, return_rate = {}, {}
	for it in si.items:
		return_qty[it.item_code] = return_qty.get(it.item_code, 0.0) + flt(it.qty)
		return_rate[it.item_code] = flt(it.rate)

	owner = frappe.session.user

	def _make():
		pi = _make_debit_note(original_pi)
		items = []
		for it in pi.items:
			qty = return_qty.pop(it.item_code, None)
			if not qty:
				continue
			it.qty = qty  # already negative on the credit note
			it.received_qty = qty
			it.rejected_qty = 0
			it.stock_qty = qty * flt(it.conversion_factor or 1)
			it.rate = return_rate[it.item_code]
			items.append(it)
		if not items:
			frappe.throw(_("None of the returned items exist on {0}").format(original_pi))
		pi.set("items", items)
		return _finalise_and_insert(pi, si, settings, owner)

	return _run_as_administrator(_make)


# --------------------------------------------------------------------------- selling price


def update_selling_prices(pi, settings):
	pct = flt(settings.profit_percentage)
	price_list = get_selling_price_list(settings, pi.company)
	currency = frappe.db.get_value("Price List", price_list, "currency") or pi.currency
	precision = cint(frappe.db.get_default("currency_precision")) or 3
	date = getdate(pi.posting_date)
	updated = []
	for item in pi.items:
		if not item.item_code or flt(item.rate) <= 0:
			continue
		new_rate = flt(flt(item.rate) * (1 + pct / 100.0), precision)
		set_item_price(item.item_code, item.uom, price_list, currency, new_rate, date)
		updated.append(f"{item.item_code}: {new_rate}")
	if updated:
		frappe.msgprint(
			_("Selling price updated in {0} ({1} + {2}%): {3}").format(
				price_list, _("purchase rate"), pct, ", ".join(updated)
			),
			alert=True,
		)


def set_item_price(item_code, uom, price_list, currency, rate, date):
	"""Close the running Item Price the day before `date` and start a new one on `date`."""
	filters = {
		"item_code": item_code,
		"price_list": price_list,
		"uom": uom,
		"selling": 1,
		"customer": ("is", "not set"),
		"batch_no": ("is", "not set"),
	}
	for p in frappe.get_all(
		"Item Price", filters=filters, fields=["name", "valid_from", "valid_upto", "price_list_rate"]
	):
		if p.valid_upto and getdate(p.valid_upto) < date:
			continue  # already closed
		if p.valid_from and getdate(p.valid_from) >= date:
			# a price already starts on (or after) this date: overwrite it instead of overlapping
			frappe.db.set_value(
				"Item Price", p.name, {"price_list_rate": rate, "valid_from": date, "valid_upto": None}
			)
			return p.name
		frappe.db.set_value("Item Price", p.name, "valid_upto", add_days(date, -1))

	doc = frappe.get_doc(
		{
			"doctype": "Item Price",
			"item_code": item_code,
			"uom": uom,
			"price_list": price_list,
			"selling": 1,
			"currency": currency,
			"price_list_rate": rate,
			"valid_from": date,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name
