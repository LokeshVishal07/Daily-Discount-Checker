"""config/settings.py"""

REGIONS = ["MY", "SG", "PH"]

REGION_MARKETPLACES = {
    "MY": ["Lazada", "Shopee", "Zalora", "TikTok"],
    "SG": ["Lazada", "Shopee", "Zalora"],
    "PH": ["Lazada", "Shopee", "Zalora"],
}

# ── Marketplace order-file column mappings ─────────────────────────────────
MARKETPLACE_COLUMNS = {
    "Lazada": {
        "order_id":               "orderNumber",
        "sku":                    "sellerSku",
        "product_name":           None,
        "order_status":           None,
        "order_date":             "createTime",
        "original_price":         "unitPrice",
        "paid_price":             "paidPrice",
        "quantity":               None,
        "seller_discount_cols":   ["sellerDiscountTotal"],
        "platform_discount_cols": ["platformDiscountTotal"],
        "discount_sign":          "negative",   # stored as negative numbers
    },
    "Shopee": {
        "order_id":               "Order ID",
        "sku":                    "SKU Reference No.",
        "product_name":           "Product Name",
        "order_status":           "Order Status",
        "order_date":             "Order Creation Date",
        "original_price":         "Original Price",
        "paid_price":             "Total Amount",
        "quantity":               "Quantity",
        "seller_discount_cols":   [
            "Seller Discount",
            "Seller Rebate",
            "Discount Voucher Amount Sponsored by Seller",
            "Coin Cashback Voucher Amount Sponsored by Seller",
            "Seller Bundle Discount",
        ],
        "platform_discount_cols": [
            "Shopee Rebate",
            "Discount Voucher Amount Sponsored by Shopee",
            "Coin Cashback Voucher Amount Sponsored by Shopee",
            "Shopee Bundle Discount",
            "Credit Card Discount Total",
        ],
        "discount_sign": "positive",
    },
    "Zalora": {
        "order_id":               "Order Number",
        "sku":                    "Seller SKU",
        "product_name":           "Item Name",
        "order_status":           "Payment Method",
        "order_date":             "Created at",
        "original_price":         "Unit Price",
        "paid_price":             "Paid Price",
        "quantity":               None,
        "seller_discount_cols":   [],
        "platform_discount_cols": [],
        "discount_sign":          "positive",
        "derive_seller_disc":     True,
    },
    "TikTok": {
        "order_id":               "Order ID",
        "sku":                    "Seller SKU",
        "product_name":           "Product Name",
        "order_status":           "Order Status",
        "order_date":             "Created Time",
        "original_price":         "SKU Unit Original Price",
        "paid_price":             "SKU Subtotal After Discount",
        "quantity":               "Quantity",
        "seller_discount_cols":   ["SKU Seller Discount", "Shipping Fee Seller Discount"],
        "platform_discount_cols": ["SKU Platform Discount", "Shipping Fee Platform Discount",
                                   "Payment platform discount"],
        "discount_sign":          "positive",
        "tiktok_skip_desc_row":   True,
    },
}

# ── Exclusion flag rules (checked in order, first match wins) ──────────────
# pattern is matched case-insensitively against the remarks text
EXCLUSION_RULES = [
    {
        "pattern":     "exclude",
        "rule_type":   "exclude",
        "label":       "EXCLUDED — sell at SRP only",
        "severity":    "red",
    },
    {
        "pattern":     "open for all",
        "rule_type":   "open",
        "label":       "OPEN — no restriction",
        "severity":    "green",
    },
    {
        "pattern":     "open for",
        "rule_type":   "open",
        "label":       "OPEN — no restriction",
        "severity":    "green",
    },
    {
        # e.g. "10% VC ONLY", "50% VC ONLY - Shopee exclusive"
        "pattern":     r"(\d+)%\s*vc\s*only",
        "rule_type":   "exact_pct",
        "label":       "EXACT {pct}% VC ONLY",
        "severity":    "orange",
        "tolerance_pp": 2,
    },
    {
        # e.g. "MAX 20%", "MAX 30% DISC", "MAX 20% DISC - EOSS"
        "pattern":     r"max\s+(\d+)%",
        "rule_type":   "max_pct",
        "label":       "MAX {pct}%",
        "severity":    "amber",
    },
]

MARKETPLACE_COLORS = {
    "Lazada": "#FF6600",
    "Shopee": "#EE4D2D",
    "TikTok": "#010101",
    "Zalora": "#DDAA00",
}

REGION_COLORS = {"MY": "#1a73e8", "SG": "#27ae60", "PH": "#9b27af"}

SEVERITY_HEX = {
    "red":    "#FF4B4B",
    "orange": "#FF8C00",
    "amber":  "#FFC300",
    "green":  "#2ECC71",
    "grey":   "#95A5A6",
}
