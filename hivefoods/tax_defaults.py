"""Default VAT settings for Item, Customer and Supplier.

- Item: when the Taxes table is empty, add one "Oman VAT 5%" Item Tax Template row per
  company (Tax Category "Standard"). A button on the form lets the user switch all rows to
  Zero Rated / Exempt for items like basic food.
- Customer / Supplier: Tax Category defaults to "Standard"; internal parties get "Internal".

Everything is looked up by title / name, so nothing breaks if a template or category is
missing on a site; the field is simply left as it is.
"""

import frappe

DEFAULT_ITEM_TAX_TITLE = "Oman VAT 5%"
ITEM_TAX_TITLES = ["Oman VAT 5%", "Oman VAT Zero Rated", "Oman VAT Exempt"]
STANDARD_TAX_CATEGORY = "Standard"
INTERNAL_TAX_CATEGORY = "Internal"


def _tax_category_exists(name):
	return bool(name) and frappe.db.exists("Tax Category", name)


@frappe.whitelist()
def get_item_tax_rows(template_title=DEFAULT_ITEM_TAX_TITLE):
	"""Return one Item Tax row per company for the given template title."""
	templates = frappe.get_all(
		"Item Tax Template",
		filters={"title": template_title, "disabled": 0},
		fields=["name", "company"],
		order_by="company",
	)
	tax_category = STANDARD_TAX_CATEGORY if _tax_category_exists(STANDARD_TAX_CATEGORY) else None
	return [{"item_tax_template": t.name, "tax_category": tax_category} for t in templates]


def set_item_tax_defaults(doc, method=None):
	"""Item.validate: fill the Taxes table for all companies when it is empty."""
	if doc.get("taxes"):
		return
	for row in get_item_tax_rows():
		doc.append("taxes", row)


def set_party_tax_category(doc, method=None):
	"""Customer/Supplier.validate: Internal for internal parties, Standard when blank."""
	is_internal = doc.get("is_internal_customer") or doc.get("is_internal_supplier")
	if is_internal:
		if _tax_category_exists(INTERNAL_TAX_CATEGORY):
			doc.tax_category = INTERNAL_TAX_CATEGORY
	elif not doc.tax_category or doc.tax_category == INTERNAL_TAX_CATEGORY:
		if _tax_category_exists(STANDARD_TAX_CATEGORY):
			doc.tax_category = STANDARD_TAX_CATEGORY
