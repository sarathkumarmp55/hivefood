import frappe
from frappe import _
from frappe.model.document import Document


class HivefoodsSettings(Document):
	def validate(self):
		seen = set()
		for row in self.company_settings:
			if row.company in seen:
				frappe.throw(_("Row {0}: Company {1} is listed more than once").format(row.idx, row.company))
			seen.add(row.company)
			if frappe.db.get_value("Warehouse", row.receiving_warehouse, "company") != row.company:
				frappe.throw(
					_("Row {0}: Warehouse {1} does not belong to {2}").format(
						row.idx, row.receiving_warehouse, row.company
					)
				)
