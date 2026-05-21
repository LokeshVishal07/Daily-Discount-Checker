"""discount_engine.py — Discount calculation and flagging engine"""
from __future__ import annotations
import re, logging
import numpy as np
import pandas as pd
from settings import EXCLUSION_RULES

logger = logging.getLogger(__name__)


def run_pipeline(orders: pd.DataFrame, content: pd.DataFrame, zecom_lookup: pd.DataFrame) -> pd.DataFrame:
    merged     = _merge(orders, content, zecom_lookup)
    with_disc  = _calc_discounts(merged)
    with_flags = _apply_flags(with_disc)
    return with_flags


def _merge(orders, content, zecom):
    orders  = orders.copy()
    orders["_sku"] = orders["sku"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    content = content.copy()
    content["EAN"]            = content["EAN"].astype(str).str.strip()
    content["Article Number"] = content["Article Number"].astype(str).str.strip()
    zecom   = zecom.copy()
    zecom["Article Number"]   = zecom["Article Number"].astype(str).str.strip()
    merged = (orders
              .merge(content, left_on="_sku", right_on="EAN", how="left")
              .merge(zecom, on="Article Number", how="left"))
    merged.drop(columns=["_sku"], inplace=True)
    return merged.reset_index(drop=True)


def _calc_discounts(df):
    df      = df.copy()
    rrp_raw = pd.to_numeric(df.get("RRP"),  errors="coerce")
    srp_raw = pd.to_numeric(df.get("SRP"),  errors="coerce")
    orig    = pd.to_numeric(df.get("original_price"), errors="coerce")
    paid    = pd.to_numeric(df.get("paid_price"),     errors="coerce")
    s_disc  = pd.to_numeric(df.get("seller_discount_amount",   0), errors="coerce").fillna(0)
    p_disc  = pd.to_numeric(df.get("platform_discount_amount", 0), errors="coerce").fillna(0)

    rrp_used = rrp_raw.where(rrp_raw.notna() & (rrp_raw > 0), orig)
    srp_used = srp_raw.where(srp_raw.notna() & (srp_raw > 0), rrp_used)
    df["rrp_used"]              = rrp_used
    df["srp_used"]              = srp_used
    df["rrp_source"]            = np.where(rrp_raw.notna() & (rrp_raw > 0), "ZeCom", "MP Listed Price")

    safe_rrp = rrp_used.replace(0, np.nan)
    safe_srp = srp_used.replace(0, np.nan)
    df["customer_disc_pct"]     = ((safe_rrp - paid) / safe_rrp * 100).round(2)
    df["seller_disc_pct"]       = (s_disc / safe_rrp * 100).round(2)
    df["platform_disc_pct"]     = (p_disc / safe_rrp * 100).round(2)
    df["seller_disc_vs_srp_pct"]= ((safe_srp - paid) / safe_srp * 100).round(2)
    return df


def _apply_flags(df):
    df      = df.copy()
    results = [_eval_remark(r.get("remark", ""),
                            r.get("seller_disc_pct", 0) or 0,
                            r.get("seller_disc_vs_srp_pct", 0) or 0)
               for r in df.to_dict("records")]
    df["allowed_rule"]    = [r[0] for r in results]
    df["max_allowed_pct"] = [r[1] for r in results]
    df["flagged"]         = [r[2] for r in results]
    df["flag_reason"]     = [r[3] for r in results]
    df["flag_severity"]   = [r[4] for r in results]
    return df


def _eval_remark(remark, seller_pct, srp_pct):
    rm = str(remark).strip().lower()
    if not rm or rm in ("", "none", "nan", "0"):
        return ("No remark — manual review", None, False,
                "No exclusion remark in ZeCom. Manual review recommended.", "grey")

    for rule in EXCLUSION_RULES:
        pat = rule["pattern"].lower()
        if rule["rule_type"] == "exclude":
            if pat in rm:
                flagged = srp_pct > 0.5
                reason  = (f"EXCLUDED — must sell at SRP. Disc below SRP = {srp_pct:.1f}%."
                           + (" 🚨" if flagged else " ✅"))
                return (rule["label"], 0, flagged, reason, rule["severity"])
        elif rule["rule_type"] == "open":
            if pat in rm:
                return (rule["label"], None, False, "Open — no restriction.", "green")
        elif rule["rule_type"] == "exact_pct":
            m = re.search(rule["pattern"], rm)
            if m:
                target  = float(m.group(1))
                tol     = rule.get("tolerance_pp", 2)
                flagged = abs(seller_pct - target) > tol
                label   = rule["label"].replace("{pct}", str(int(target)))
                reason  = (f"{label}. Seller disc = {seller_pct:.1f}% (target {target:.0f}% ±{tol}pp)."
                           + (" 🚨" if flagged else " ✅"))
                return (label, target, flagged, reason, rule["severity"])
        elif rule["rule_type"] == "max_pct":
            m = re.search(rule["pattern"], rm)
            if m:
                cap     = float(m.group(1))
                flagged = seller_pct > cap
                label   = rule["label"].replace("{pct}", str(int(cap)))
                reason  = (f"{label}. Seller disc = {seller_pct:.1f}% (cap {cap:.0f}%)."
                           + (" 🚨" if flagged else " ✅"))
                return (label, cap, flagged, reason, rule["severity"])

    return (f"Remark: {remark[:60]}", None, False,
            f"Unrecognised remark — manual review.", "grey")


def summary_by_marketplace(df):
    return (df.groupby(["region", "marketplace"])
            .agg(Total_Orders=("order_id","count"),
                 RRP_Matched=("RRP", lambda x: x.notna().sum()),
                 Avg_RRP=("rrp_used","mean"), Sum_RRP=("rrp_used","sum"),
                 Sum_Paid=("paid_price","sum"),
                 Avg_Customer_Disc=("customer_disc_pct","mean"),
                 Avg_Seller_Disc=("seller_disc_pct","mean"),
                 Avg_Platform_Disc=("platform_disc_pct","mean"),
                 Flagged=("flagged","sum"))
            .round(2).reset_index())


def exclusion_summary(df):
    grp = (df.groupby(["region", "marketplace", "allowed_rule", "flag_severity"])
           .agg(Orders=("order_id","count"), Flagged=("flagged","sum"),
                Sum_RRP=("rrp_used","sum"), Sum_Paid=("paid_price","sum"),
                Sum_Seller_Disc=("seller_discount_amount","sum"),
                Avg_Seller_Disc_Pct=("seller_disc_pct","mean"),
                Max_Seller_Disc_Pct=("seller_disc_pct","max"))
           .reset_index())
    grp["Total_Discount_Amt"] = (grp["Sum_RRP"] - grp["Sum_Paid"]).round(2)
    grp["Total_Discount_Pct"] = ((grp["Total_Discount_Amt"] / grp["Sum_RRP"]) * 100).round(2)
    return grp.round(2)


def flagged_orders(df):
    keep = [c for c in ["region","marketplace","order_id","sku","Article Number",
                         "product_name","order_status","order_date",
                         "rrp_used","srp_used","paid_price",
                         "seller_discount_amount","platform_discount_amount",
                         "customer_disc_pct","seller_disc_pct","platform_disc_pct",
                         "remark","allowed_rule","max_allowed_pct",
                         "flag_reason","flag_severity"] if c in df.columns]
    return df[df["flagged"] == True][keep].reset_index(drop=True)
