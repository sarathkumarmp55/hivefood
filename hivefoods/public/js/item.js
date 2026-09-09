// Item: auto-fill VAT rows for every company, plus a button to switch template.
frappe.ui.form.on("Item", {
	refresh(frm) {
		frm.add_custom_button(
			__("Set VAT for all companies"),
			() => {
				frappe.prompt(
					{
						fieldname: "template_title",
						label: __("VAT Template"),
						fieldtype: "Select",
						options: ["Oman VAT 5%", "Oman VAT Zero Rated", "Oman VAT Exempt"],
						default: "Oman VAT 5%",
						reqd: 1,
					},
					(values) => hivefoods.set_item_taxes(frm, values.template_title, true),
					__("Item Tax Template for all companies")
				);
			},
			__("Actions")
		);

		if (frm.is_new() && !(frm.doc.taxes || []).length) {
			hivefoods.set_item_taxes(frm, "Oman VAT 5%", false);
		}
	},
});

frappe.provide("hivefoods");

hivefoods.set_item_taxes = function (frm, template_title, replace) {
	return frappe.call({
		method: "hivefoods.tax_defaults.get_item_tax_rows",
		args: { template_title },
		callback(r) {
			const rows = r.message || [];
			if (!rows.length) {
				frappe.show_alert({ message: __("No Item Tax Template found for {0}", [template_title]), indicator: "orange" });
				return;
			}
			if (replace) frm.clear_table("taxes");
			rows.forEach((row) => {
				const d = frm.add_child("taxes");
				d.item_tax_template = row.item_tax_template;
				d.tax_category = row.tax_category;
			});
			frm.refresh_field("taxes");
			frm.dirty();
			if (replace) {
				frappe.show_alert({ message: __("{0} set for {1} companies", [template_title, rows.length]), indicator: "green" });
			}
		},
	});
};
