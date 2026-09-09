// Customer: Tax Category = Internal for internal customers, Standard otherwise.
frappe.ui.form.on("Customer", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.tax_category) {
			frm.set_value("tax_category", frm.doc.is_internal_customer ? "Internal" : "Standard");
		}
	},
	is_internal_customer(frm) {
		frm.set_value("tax_category", frm.doc.is_internal_customer ? "Internal" : "Standard");
	},
});
