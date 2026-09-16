"""Central Kitchen Production: a one-screen replacement for Work Order + Stock Entries.

Pick a recipe (BOM), enter batches / actual produced qty and the packed output items.
On submit one Repack Stock Entry is created: raw materials go out of the raw material
warehouse, finished items come into the finished goods warehouse. Raw material cost is
split across the outputs by weight (qty x weight per unit).
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, get_link_to_form, getdate

from erpnext.stock.utils import get_incoming_rate


class CentralKitchenProduction(Document):
	# ----------------------------------------------------------------- validate
	def validate(self):
		self.validate_warehouses()
		if not self.raw_materials:
			frappe.throw(_("Add at least one raw material or bulk item to consume"))
		if not self.bom and not self.outputs:
			frappe.throw(_("Without a recipe, add the packed items under Output"))
		if not self.produced_qty:
			self.produced_qty = flt(self.batches) * flt(self.bom_quantity)
		if not self.produced_qty and not self.bom:
			# packing entry: produced qty = weight of bulk consumed
			self.produced_qty = sum(
				flt(r.qty) * flt(frappe.get_cached_value("Item", r.item_code, "weight_per_unit") or 1)
				for r in self.raw_materials
			)
		self.set_required_qty()
		self.set_raw_material_rates()
		self.set_additional_costs()
		self.set_default_output()
		self.validate_outputs()
		self.allocate_output_cost()

	def validate_warehouses(self):
		for field in ("source_warehouse", "target_warehouse"):
			wh_company = frappe.db.get_value("Warehouse", self.get(field), "company")
			if wh_company != self.company:
				frappe.throw(
					_("{0} {1} does not belong to {2}").format(
						self.meta.get_label(field), self.get(field), self.company
					)
				)

	def set_required_qty(self):
		if not self.bom:
			return
		bom_qty = {}
		for row in get_bom_materials(self.bom, self.batches):
			bom_qty[row["item_code"]] = bom_qty.get(row["item_code"], 0) + row["required_qty"]
		for row in self.raw_materials:
			if row.item_code in bom_qty:
				row.required_qty = bom_qty[row.item_code]
			if not row.qty and row.required_qty:
				row.qty = row.required_qty

	def set_raw_material_rates(self):
		self.total_raw_cost = 0
		for row in self.raw_materials:
			row.uom = frappe.get_cached_value("Item", row.item_code, "stock_uom")
			if flt(row.qty) <= 0:
				frappe.throw(_("Row {0}: Actual Qty must be greater than 0").format(row.idx))
			picked = pick_batches(row.item_code, self.source_warehouse, row.qty, self.posting_date, self.posting_time, self.company)
			if picked:
				row.amount = flt(sum(b["qty"] * b["rate"] for b in picked))
				row.rate = flt(row.amount / flt(row.qty), 6)
				row.batch_no = ", ".join(f"{b['batch_no']} ({b['qty']})" for b in picked)
			else:
				row.rate = get_valuation_rate(
					row.item_code, self.source_warehouse, self.posting_date, self.posting_time, self.company, row.qty
				)
				row.amount = flt(row.qty) * flt(row.rate)
				row.batch_no = None
			self.total_raw_cost += row.amount
		self.total_raw_cost = flt(self.total_raw_cost, self.precision("total_raw_cost"))

	def set_additional_costs(self):
		self.total_additional_cost = 0
		for row in self.additional_costs:
			if flt(row.amount) <= 0:
				frappe.throw(_("Additional cost row {0}: Amount must be greater than 0").format(row.idx))
			if frappe.db.get_value("Account", row.expense_account, "company") != self.company:
				frappe.throw(_("Additional cost row {0}: account does not belong to {1}").format(row.idx, self.company))
			self.total_additional_cost += flt(row.amount)
		self.total_additional_cost = flt(self.total_additional_cost, self.precision("total_additional_cost"))
		self.total_cost = flt(flt(self.total_raw_cost) + self.total_additional_cost, self.precision("total_cost"))

	def set_default_output(self):
		"""No packed outputs entered: the bulk product itself is received."""
		if self.outputs or not self.product:
			return
		self.append(
			"outputs",
			{
				"item_code": self.product,
				"qty": self.produced_qty,
				"weight_per_unit": frappe.db.get_value("Item", self.product, "weight_per_unit") or 1,
			},
		)

	def validate_outputs(self):
		self.total_output_weight = 0
		for row in self.outputs:
			if flt(row.qty) <= 0:
				frappe.throw(_("Output row {0}: Qty must be greater than 0").format(row.idx))
			if not row.weight_per_unit:
				row.weight_per_unit = frappe.db.get_value("Item", row.item_code, "weight_per_unit")
			if flt(row.weight_per_unit) <= 0:
				frappe.throw(
					_("Output row {0}: set Weight / Unit for {1} (needed to split the cost). "
					  "You can also set it once on the Item as Weight Per Unit.").format(
						row.idx, row.item_code
					)
				)
			row.total_weight = flt(row.qty) * flt(row.weight_per_unit)
			self.total_output_weight += row.total_weight
			self.set_expiry(row)
		self.weight_difference = flt(self.produced_qty) - flt(self.total_output_weight)
		if self.total_output_weight > flt(self.produced_qty) * 1.05:
			frappe.throw(
				_("Packed weight {0} Kg is more than produced qty {1}. Check the output quantities.").format(
					self.total_output_weight, self.produced_qty
				)
			)

	def set_expiry(self, row):
		has_batch, has_expiry, shelf_life = frappe.get_cached_value(
			"Item", row.item_code, ["has_batch_no", "has_expiry_date", "shelf_life_in_days"]
		)
		if not (has_batch and has_expiry):
			row.expiry_date = None
			return
		if not row.expiry_date:
			if not shelf_life:
				frappe.throw(
					_("Output row {0}: enter Expiry Date, or set Shelf Life in Days on Item {1}").format(
						row.idx, row.item_code
					)
				)
			row.expiry_date = add_days(self.posting_date, cint(shelf_life))
		elif getdate(row.expiry_date) <= getdate(self.posting_date):
			frappe.throw(_("Output row {0}: Expiry Date must be after the production date").format(row.idx))

	def make_batch(self, row):
		"""New batch for a batch-tracked finished item; the id comes from the Item's batch series."""
		if not frappe.get_cached_value("Item", row.item_code, "has_batch_no"):
			return None
		batch = frappe.get_doc(
			{
				"doctype": "Batch",
				"item": row.item_code,
				"manufacturing_date": self.posting_date,
				"expiry_date": row.expiry_date,
				"reference_doctype": self.doctype,
				"reference_name": self.name,
			}
		)
		batch.flags.ignore_permissions = True
		batch.insert()
		row.db_set("batch_no", batch.name, update_modified=False)
		return batch.name

	def allocate_output_cost(self):
		"""Split raw cost (posted as basic rate) and total cost (shown to the user) by weight."""
		total_weight = flt(self.total_output_weight)
		precision = self.precision("total_raw_cost")
		allocated_raw = allocated_total = 0
		for i, row in enumerate(self.outputs):
			if i == len(self.outputs) - 1:
				row.raw_amount = flt(flt(self.total_raw_cost) - allocated_raw, precision)
				row.amount = flt(flt(self.total_cost) - allocated_total, precision)
			else:
				share = flt(row.total_weight) / total_weight if total_weight else 0
				row.raw_amount = flt(flt(self.total_raw_cost) * share, precision)
				row.amount = flt(flt(self.total_cost) * share, precision)
				allocated_raw += row.raw_amount
				allocated_total += row.amount
			row.rate = flt(row.amount / flt(row.qty), 6) if row.qty else 0

	# ----------------------------------------------------------------- submit / cancel
	def on_submit(self):
		se = self.make_stock_entry()
		self.db_set("stock_entry", se.name)
		frappe.msgprint(
			_("Stock Entry {0} submitted").format(get_link_to_form("Stock Entry", se.name)), alert=True
		)

	def on_cancel(self):
		self.ignore_linked_doctypes = ("Stock Entry", "Stock Ledger Entry", "GL Entry", "Repost Item Valuation")
		if self.stock_entry and frappe.db.get_value("Stock Entry", self.stock_entry, "docstatus") == 1:
			se = frappe.get_doc("Stock Entry", self.stock_entry)
			se.flags.ignore_permissions = True
			se.cancel()

	def make_stock_entry(self):
		se = frappe.new_doc("Stock Entry")
		se.purpose = se.stock_entry_type = "Repack"
		se.company = self.company
		se.set_posting_time = 1
		se.posting_date = self.posting_date
		se.posting_time = self.posting_time
		se.remarks = _("Central Kitchen Production {0}").format(self.name)
		for row in self.raw_materials:
			picked = pick_batches(row.item_code, self.source_warehouse, row.qty, self.posting_date, self.posting_time, self.company)
			for part in picked or [{"batch_no": None, "qty": row.qty}]:
				se.append(
					"items",
					{
						"item_code": row.item_code,
						"qty": part["qty"],
						"uom": row.uom,
						"s_warehouse": self.source_warehouse,
						"batch_no": part["batch_no"],
						"use_serial_batch_fields": 1 if part["batch_no"] else 0,
						"conversion_factor": 1,
					},
				)
		for row in self.outputs:
			batch_no = self.make_batch(row)
			se.append(
				"items",
				{
					"item_code": row.item_code,
					"qty": row.qty,
					"uom": row.uom,
					"t_warehouse": self.target_warehouse,
					"batch_no": batch_no,
					"use_serial_batch_fields": 1 if batch_no else 0,
					"is_finished_item": 1,
					"set_basic_rate_manually": 1,
					"basic_rate": flt(row.raw_amount / flt(row.qty), 6) if row.qty else 0,
					"basic_amount": row.raw_amount,
					"amount": row.raw_amount,
					"conversion_factor": 1,
				},
			)
		for row in self.additional_costs:
			se.append(
				"additional_costs",
				{
					"expense_account": row.expense_account,
					"description": row.description or row.expense_account,
					"amount": row.amount,
				},
			)
		se.flags.ignore_permissions = True
		se.insert()
		self.sync_actual_cost(se)
		se.submit()
		return se

	def sync_actual_cost(self, se):
		"""ERPNext values the outgoing rows on insert (batch/FIFO specific), which can differ
		from the estimate shown on the form. Re-split the actual raw cost across the outputs
		before submitting so the finished goods carry exactly what was consumed."""
		out_rows = [d for d in se.items if d.s_warehouse]
		fg_rows = [d for d in se.items if d.t_warehouse and not d.s_warehouse]
		actual_raw = flt(sum(flt(d.basic_amount) for d in out_rows), self.precision("total_raw_cost"))
		precision = self.precision("total_raw_cost")
		total_weight = flt(self.total_output_weight)
		allocated = 0
		for i, (row, d) in enumerate(zip(self.outputs, fg_rows)):
			if i == len(fg_rows) - 1:
				raw_amount = flt(actual_raw - allocated, precision)
			else:
				raw_amount = flt(actual_raw * flt(row.total_weight) / total_weight, precision) if total_weight else 0
				allocated += raw_amount
			d.basic_rate = flt(raw_amount / flt(d.qty), 6) if d.qty else 0
			d.basic_amount = d.amount = raw_amount
			row.db_set({"raw_amount": raw_amount, "amount": flt(raw_amount + (flt(self.total_additional_cost) * flt(row.total_weight) / total_weight if total_weight else 0), precision)}, update_modified=False)
			row.db_set("rate", flt(row.amount / flt(row.qty), 6) if row.qty else 0, update_modified=False)
		by_item = {}
		for d in out_rows:
			by_item.setdefault(d.item_code, []).append(d)
		for row in self.raw_materials:
			parts = by_item.get(row.item_code) or []
			amount = flt(sum(flt(d.basic_amount) for d in parts), precision)
			row.db_set(
				{
					"rate": flt(amount / flt(row.qty), 6) if row.qty else 0,
					"amount": amount,
					"batch_no": ", ".join(f"{d.batch_no} ({d.qty})" for d in parts if d.batch_no) or None,
				},
				update_modified=False,
			)
		self.db_set({"total_raw_cost": actual_raw, "total_cost": flt(actual_raw + flt(self.total_additional_cost), precision)}, update_modified=False)
		se.save()


# --------------------------------------------------------------------- api


def get_valuation_rate(item_code, warehouse, posting_date, posting_time, company, qty=1):
	return flt(
		get_incoming_rate(
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_date": posting_date,
				"posting_time": posting_time,
				"qty": -1 * flt(qty),
				"company": company,
				"voucher_type": "Stock Entry",
				"allow_zero_valuation": 1,
			},
			raise_error_if_no_rate=False,
		)
	)


def pick_batches(item_code, warehouse, qty, posting_date, posting_time, company):
	"""FEFO/FIFO batch split (Stock Settings basis) with the batch-wise valuation rate.
	Returns [] for items that are not batch tracked."""
	if not frappe.get_cached_value("Item", item_code, "has_batch_no"):
		return []
	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import get_auto_batch_nos

	based_on = frappe.db.get_single_value("Stock Settings", "pick_serial_and_batch_based_on") or "FIFO"
	batches = get_auto_batch_nos(
		frappe._dict(
			item_code=item_code,
			warehouse=warehouse,
			qty=flt(qty),
			based_on=based_on,
			posting_date=posting_date,
			posting_time=posting_time,
			company=company,
		)
	)
	picked = []
	for b in batches or []:
		if flt(b.qty) <= 0:
			continue
		rate = flt(
			get_incoming_rate(
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"posting_date": posting_date,
					"posting_time": posting_time,
					"qty": -1 * flt(b.qty),
					"batch_no": b.batch_no,
					"company": company,
					"voucher_type": "Stock Entry",
					"allow_zero_valuation": 1,
				},
				raise_error_if_no_rate=False,
			)
		)
		picked.append({"batch_no": b.batch_no, "qty": flt(b.qty), "rate": rate})
	total = sum(p["qty"] for p in picked)
	if picked and total < flt(qty):
		frappe.throw(
			_("Only {0} {1} available in batches of {2} at {3}; {4} needed").format(
				total, frappe.get_cached_value("Item", item_code, "stock_uom"), item_code, warehouse, qty
			)
		)
	return picked


@frappe.whitelist()
def get_bom_materials(bom, batches=1):
	"""Raw materials of the BOM scaled by batches (exploded, so sub-recipes are included)."""
	bom_doc = frappe.get_cached_doc("BOM", bom)
	factor = flt(batches) or 1
	rows = []
	for d in bom_doc.get("exploded_items") or bom_doc.get("items"):
		rows.append(
			{
				"item_code": d.item_code,
				"item_name": d.item_name,
				"uom": frappe.get_cached_value("Item", d.item_code, "stock_uom"),
				"required_qty": flt(d.stock_qty) * factor,
				"qty": flt(d.stock_qty) * factor,
			}
		)
	return rows


@frappe.whitelist()
def get_rate(item_code, warehouse, posting_date, posting_time, company, qty=1):
	return get_valuation_rate(item_code, warehouse, posting_date, posting_time, company, qty)
