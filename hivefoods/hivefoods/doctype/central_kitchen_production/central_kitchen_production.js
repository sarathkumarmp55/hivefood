frappe.ui.form.on("Central Kitchen Production", {
	setup(frm) {
		frm.set_query("bom", () => ({ filters: { is_active: 1, docstatus: 1, company: frm.doc.company } }));
		frm.set_query("source_warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("target_warehouse", () => ({ filters: { company: frm.doc.company, is_group: 0 } }));
		frm.set_query("item_code", "outputs", () => ({ filters: { is_stock_item: 1, disabled: 0 } }));
	},

	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.stock_entry) {
			frm.add_custom_button(__("Stock Entry"), () => frappe.set_route("Form", "Stock Entry", frm.doc.stock_entry), __("View"));
		}
		if (frm.doc.docstatus === 0 && frm.doc.bom) {
			frm.add_custom_button(__("Get Raw Materials from Recipe"), () => hivefoods.ckp.get_materials(frm));
		}
	},

	onload(frm) {
		if (frm.is_new() && frm.doc.company && !frm.doc.source_warehouse) {
			hivefoods.ckp.set_default_warehouses(frm);
		}
	},

	company(frm) {
		hivefoods.ckp.set_default_warehouses(frm);
	},

	bom(frm) {
		if (!frm.doc.bom) return;
		frappe.db.get_doc("BOM", frm.doc.bom).then((bom) => {
			frm.set_value("product", bom.item);
			frm.set_value("bom_quantity", bom.quantity);
			frm.set_value("stock_uom", bom.uom);
			frm.set_value("produced_qty", flt(bom.quantity) * flt(frm.doc.batches || 1));
			hivefoods.ckp.get_materials(frm);
		});
	},

	batches(frm) {
		frm.set_value("produced_qty", flt(frm.doc.bom_quantity) * flt(frm.doc.batches));
		(frm.doc.raw_materials || []).forEach((row) => {
			if (row.required_qty && frm._bom_base) {
				row.required_qty = flt(frm._bom_base[row.item_code] || 0) * flt(frm.doc.batches);
				row.qty = row.required_qty;
			}
		});
		frm.refresh_field("raw_materials");
		hivefoods.ckp.refresh_rates(frm);
	},

	produced_qty(frm) { hivefoods.ckp.calc(frm); },
	source_warehouse(frm) { hivefoods.ckp.refresh_rates(frm); },
	posting_date(frm) { hivefoods.ckp.refresh_rates(frm); },
});

frappe.ui.form.on("Central Kitchen Production Item", {
	item_code(frm, cdt, cdn) { hivefoods.ckp.set_row_rate(frm, locals[cdt][cdn]); },
	qty(frm, cdt, cdn) { hivefoods.ckp.set_row_rate(frm, locals[cdt][cdn]); },
	raw_materials_remove(frm) { hivefoods.ckp.calc(frm); },
});

frappe.ui.form.on("Central Kitchen Production Output", {
	item_code(frm, cdt, cdn) {
		const row = locals[cdt][cdn];
		if (!row.item_code) return;
		frappe.db.get_value("Item", row.item_code, ["weight_per_unit", "stock_uom"]).then((r) => {
			frappe.model.set_value(cdt, cdn, "weight_per_unit", r.message.weight_per_unit);
			frappe.model.set_value(cdt, cdn, "uom", r.message.stock_uom);
			hivefoods.ckp.calc(frm);
		});
	},
	qty(frm) { hivefoods.ckp.calc(frm); },
	weight_per_unit(frm) { hivefoods.ckp.calc(frm); },
	outputs_remove(frm) { hivefoods.ckp.calc(frm); },
});

frappe.provide("hivefoods.ckp");

hivefoods.ckp.set_default_warehouses = function (frm) {
	if (!frm.doc.company) return;
	frappe.db.get_value("Company", frm.doc.company, "abbr").then((r) => {
		const abbr = r.message.abbr;
		if (!frm.doc.source_warehouse) frm.set_value("source_warehouse", `Stores - ${abbr}`);
		if (!frm.doc.target_warehouse) frm.set_value("target_warehouse", `Finished Goods - ${abbr}`);
	});
};

hivefoods.ckp.get_materials = function (frm) {
	frappe.call({
		method: "hivefoods.hivefoods.doctype.central_kitchen_production.central_kitchen_production.get_bom_materials",
		args: { bom: frm.doc.bom, batches: frm.doc.batches || 1 },
		callback(r) {
			frm.clear_table("raw_materials");
			frm._bom_base = {};
			(r.message || []).forEach((m) => {
				const d = frm.add_child("raw_materials", m);
				frm._bom_base[d.item_code] = flt(m.required_qty) / flt(frm.doc.batches || 1);
			});
			frm.refresh_field("raw_materials");
			hivefoods.ckp.refresh_rates(frm);
		},
	});
};

hivefoods.ckp.refresh_rates = function (frm) {
	(frm.doc.raw_materials || []).forEach((row) => hivefoods.ckp.set_row_rate(frm, row, true));
	setTimeout(() => hivefoods.ckp.calc(frm), 300);
};

hivefoods.ckp.set_row_rate = function (frm, row, silent) {
	if (!row.item_code || !frm.doc.source_warehouse) return;
	frappe.call({
		method: "hivefoods.hivefoods.doctype.central_kitchen_production.central_kitchen_production.get_rate",
		args: {
			item_code: row.item_code, warehouse: frm.doc.source_warehouse, posting_date: frm.doc.posting_date,
			posting_time: frm.doc.posting_time, company: frm.doc.company, qty: row.qty || 1,
		},
		callback(r) {
			row.rate = flt(r.message);
			row.amount = flt(row.qty) * row.rate;
			frm.refresh_field("raw_materials");
			if (!silent) hivefoods.ckp.calc(frm);
		},
	});
};

hivefoods.ckp.calc = function (frm) {
	let total_cost = 0;
	(frm.doc.raw_materials || []).forEach((r) => { r.amount = flt(r.qty) * flt(r.rate); total_cost += r.amount; });
	let total_weight = 0;
	(frm.doc.outputs || []).forEach((o) => { o.total_weight = flt(o.qty) * flt(o.weight_per_unit); total_weight += o.total_weight; });
	(frm.doc.outputs || []).forEach((o) => {
		o.amount = total_weight ? flt(total_cost * o.total_weight / total_weight, 3) : 0;
		o.rate = o.qty ? flt(o.amount / o.qty, 6) : 0;
	});
	frm.set_value("total_raw_cost", flt(total_cost, 3));
	frm.set_value("total_output_weight", flt(total_weight, 3));
	frm.set_value("weight_difference", flt(frm.doc.produced_qty) - flt(total_weight, 3));
	frm.refresh_field("raw_materials");
	frm.refresh_field("outputs");
};
