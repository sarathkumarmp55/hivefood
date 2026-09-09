// Supplier: Tax Category = Internal for internal suppliers, Standard otherwise.
frappe.ui.form.on("Supplier", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.tax_category) {
			frm.set_value("tax_category", frm.doc.is_internal_supplier ? "Internal" : "Standard");
		}
	},
	is_internal_supplier(frm) {
		frm.set_value("tax_category", frm.doc.is_internal_supplier ? "Internal" : "Standard");
	},
});
