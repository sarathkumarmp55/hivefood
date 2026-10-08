"""Sales without stock (migration period).

When "Auto Material Receipt When Stock Is Short" is ON in Hivefoods Settings, a POS Invoice,
Sales Invoice (Update Stock) or Delivery Note that sells more than the warehouse holds gets a
Material Receipt Stock Entry for the shortfall, created and submitted just before the sale is
validated for submit. With the setting OFF nothing here runs and ERPNext blocks the sale as usual.
"""

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, get_link_to_form, getdate, nowtime
from datetime import timedelta

SETTING = "auto_receipt_on_shortfall"


def before_submit_check(doc, method=None):
	"""hooked on before_validate of POS Invoice / Sales Invoice / Delivery Note"""
	if doc.docstatus != 1 or doc.get("is_return"):
		return
	if doc.doctype == "Sales Invoice" and not doc.get("update_stock"):
		return
	settings = frappe.get_cached_doc("Hivefoods Settings")
	if not cint(settings.get(SETTING)):
		return

	shortfalls = get_shortfalls(doc)
	if not shortfalls:
		return
	se = make_receipt(doc, shortfalls, settings)
	frappe.msgprint(
		_("Stock was short for {0}; Material Receipt {1} was created automatically.").format(
			", ".join(f"{s['item_code']} ({s['qty']})" for s in shortfalls),
			get_link_to_form("Stock Entry", se.name),
		),
		alert=True,
		indicator="orange",
	)
	doc.add_comment("Comment", _("Auto Material Receipt {0} created for short stock").format(se.name))


def get_shortfalls(doc):
	"""[{item_code, warehouse, qty}] for stock items whose available qty < sold qty."""
	from erpnext.accounts.doctype.pos_invoice.pos_invoice import get_stock_availability

	need = {}
	for d in doc.get("items"):
		if not d.warehouse or not frappe.get_cached_value("Item", d.item_code, "is_stock_item"):
			continue
		key = (d.item_code, d.warehouse)
		need[key] = need.get(key, 0) + flt(d.get("stock_qty") or d.qty)

	out = []
	for (item_code, warehouse), qty in need.items():
		if doc.doctype == "POS Invoice":
			available, _is_stock, _neg = get_stock_availability(item_code, warehouse)
		else:
			available = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))
		short = flt(qty) - flt(available)
		if short > 0:
			out.append({"item_code": item_code, "warehouse": warehouse, "qty": short})
	return out


def get_receipt_rate(item_code, settings):
	basis = settings.get("auto_receipt_rate_basis") or "Item Valuation Rate"
	item = frappe.get_cached_value("Item", item_code, ["valuation_rate", "last_purchase_rate"], as_dict=True)
	order = {
		"Item Valuation Rate": [item.valuation_rate, item.last_purchase_rate],
		"Last Purchase Rate": [item.last_purchase_rate, item.valuation_rate],
		"Zero": [],
	}[basis]
	for rate in order:
		if flt(rate) > 0:
			return flt(rate)
	return 0


def make_receipt(doc, shortfalls, settings):
	se = frappe.new_doc("Stock Entry")
	se.purpose = se.stock_entry_type = "Material Receipt"
	se.company = doc.company
	se.set_posting_time = 1
	se.posting_date = doc.posting_date
	# one minute before the sale so the ledger order is receipt -> sale
	sale_dt = get_datetime(f"{getdate(doc.posting_date)} {doc.get('posting_time') or nowtime()}") - timedelta(minutes=1)
	se.posting_date = sale_dt.date()
	se.posting_time = sale_dt.strftime("%H:%M:%S")
	se.remarks = _("Auto receipt for short stock on {0} {1}").format(doc.doctype, doc.name)
	for s in shortfalls:
		rate = get_receipt_rate(s["item_code"], settings)
		row = {
			"item_code": s["item_code"],
			"qty": s["qty"],
			"t_warehouse": s["warehouse"],
			"basic_rate": rate,
			"allow_zero_valuation_rate": 1 if rate == 0 else 0,
			"use_serial_batch_fields": 1,
		}
		se.append("items", row)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se


def sync_pos_profiles(settings, method=None):
	"""Hivefoods Settings.on_update.

	POS Awesome (screen and its submit API) refuses a sale beyond available qty unless
	Stock Settings > Allow Negative Stock is on, whatever the profile flags say. So the switch
	also drives that setting: ON -> allow negative stock (the auto receipt keeps real stock from
	going negative on sales), OFF -> block negative stock again.
	"""
	on = cint(settings.get(SETTING))
	current = cint(frappe.db.get_single_value("Stock Settings", "allow_negative_stock"))
	if on != current:
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", on)
		frappe.msgprint(
			_("Stock Settings > Allow Negative Stock switched {0}").format(_("ON") if on else _("OFF")), alert=True
		)
	if not on:
		return
	fields = [f for f in ("validate_stock_on_save", "posa_block_sale_beyond_available_qty") if frappe.get_meta("POS Profile").has_field(f)]
	changed = []
	for name in frappe.get_all("POS Profile", filters={"disabled": 0}, pluck="name"):
		vals = frappe.db.get_value("POS Profile", name, fields, as_dict=True)
		if any(cint(vals.get(f)) for f in fields):
			frappe.db.set_value("POS Profile", name, {f: 0 for f in fields})
			changed.append(name)
	if changed:
		frappe.msgprint(_("POS Profiles updated to allow sale beyond available qty: {0}").format(", ".join(changed)), alert=True)
