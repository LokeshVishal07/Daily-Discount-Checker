"""
discount_engine.py — vectorised for speed

Key changes vs previous version:
  - _apply_flags is fully vectorised (no row-by-row loop)
  - _calc_discounts uses vectorised ops throughout
  - parse_remark stays per-row (string parsing) but only called once via .apply()
"""
from __future__ import annotations
import re, logging
import numpy as np
import pandas as pd
from settings import MARKETPLACE_COLUMNS

logger = logging.getLogger(__name__)

OVERSHOOT_OK    = 5.0
OVERSHOOT_CHECK = 10.0


def run_pipeline(orders: pd.DataFrame, content: pd.DataFrame,
                 zecom_lookup: pd.DataFrame) -> pd.DataFrame:
    merged     = _merge(orders, content, zecom_lookup)
    with_disc  = _calc_discounts(merged)
    return with_disc   # flags applied later once open_pct_map is known


# ── Merge ─────────────────────────────────────────────────────────────────────
def _merge(orders, content, zecom):
    """
    Merge path: order.sku (EAN) → content.EAN → Article Number → zecom lookup.
    SKU in order files must be the EAN barcode to match Content file.
    """
    orders  = orders.copy()
    orders["_sku"] = (orders["sku"].astype(str).str.strip()
                      .str.replace(r"\.0$", "", regex=True))
    content = content.copy()
    content["EAN"]            = content["EAN"].astype(str).str.strip()
    content["Article Number"] = content["Article Number"].astype(str).str.strip()
    zecom   = zecom.copy()
    zecom["Article Number"]   = zecom["Article Number"].astype(str).str.strip()
    merged = (orders
              .merge(content, left_on="_sku", right_on="EAN", how="left")
              .merge(zecom,   on="Article Number", how="left"))
    merged.drop(columns=["_sku"], inplace=True)
    return merged.reset_index(drop=True)


# ── Parse remark (vectorised via apply) ──────────────────────────────────────
def parse_remark(remark: str) -> dict:
    rm = str(remark).strip().lower()
    if not rm or rm in ("", "none", "nan", "0"):
        return {"rule_type": "unknown", "vc_pct": None, "rule_label": "(no remark)"}
    if "exclude" in rm:
        return {"rule_type": "exclude", "vc_pct": None, "rule_label": "EXCLUDED — sell at SRP only"}
    if "open for all" in rm or rm.startswith("open for"):
        return {"rule_type": "open",    "vc_pct": None, "rule_label": "OPEN — no restriction"}
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*vc\s*only", rm)
    if m:
        pct = float(m.group(1))
        return {"rule_type": "exact_vc", "vc_pct": pct, "rule_label": f"{int(pct)}% VC ONLY (on SRP)"}
    m = re.search(r"max\s+(\d+(?:\.\d+)?)\s*%", rm)
    if m:
        pct = float(m.group(1))
        return {"rule_type": "max_pct", "vc_pct": pct, "rule_label": f"MAX {int(pct)}%"}
    return {"rule_type": "unknown", "vc_pct": None, "rule_label": f"Remark: {remark[:50]}"}


# ── Calculate discounts (vectorised) ─────────────────────────────────────────
def _calc_discounts(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    rrp_raw = pd.to_numeric(df.get("RRP"),  errors="coerce")
    srp_raw = pd.to_numeric(df.get("SRP"),  errors="coerce")
    orig    = pd.to_numeric(df.get("original_price"), errors="coerce")
    paid    = pd.to_numeric(df.get("paid_price"),     errors="coerce")
    s_disc  = pd.to_numeric(df.get("seller_discount_amount",   0), errors="coerce").fillna(0)
    p_disc  = pd.to_numeric(df.get("platform_discount_amount", 0), errors="coerce").fillna(0)

    rrp_used = rrp_raw.where(rrp_raw.notna() & (rrp_raw > 0), orig)
    srp_used = srp_raw.where(srp_raw.notna() & (srp_raw > 0), np.nan)

    df["rrp_used"]   = rrp_used
    df["srp_used"]   = srp_used
    df["rrp_source"] = np.where(rrp_raw.notna() & (rrp_raw > 0), "ZeCom", "MP Listed Price")

    safe_rrp = rrp_used.replace(0, np.nan)
    safe_srp = srp_used.where(srp_used.notna() & (srp_used > 0), rrp_used).replace(0, np.nan)

    df["actual_total_disc_pct"] = ((safe_rrp - paid) / safe_rrp * 100).round(2)
    df["seller_disc_pct"]       = (s_disc / safe_rrp * 100).round(2)
    df["platform_disc_pct"]     = (p_disc / safe_rrp * 100).round(2)

    # Parse remarks in one pass
    remarks = df.get("remark", pd.Series([""] * len(df), index=df.index))
    parsed  = remarks.apply(parse_remark)

    df["rule_type"]  = parsed.apply(lambda x: x["rule_type"])
    df["rule_label"] = parsed.apply(lambda x: x["rule_label"])
    df["vc_pct"]     = parsed.apply(lambda x: x["vc_pct"])

    # Authorised floor (vectorised per rule type)
    srp_floor = safe_srp.fillna(safe_rrp)   # SRP if available, else RRP

    auth_floor = pd.Series(np.nan, index=df.index)
    rt = df["rule_type"]
    vc = df["vc_pct"].astype(float, errors="ignore")

    # EXCLUDE: floor = SRP
    mask_excl = rt == "exclude"
    auth_floor = auth_floor.where(~mask_excl, srp_floor)

    # EXACT VC: floor = SRP × (1 - vc/100)
    mask_vc = rt == "exact_vc"
    auth_floor = auth_floor.where(~mask_vc, srp_floor * (1 - vc / 100))

    # MAX %: floor = RRP × (1 - vc/100)
    mask_max = rt == "max_pct"
    auth_floor = auth_floor.where(~mask_max, safe_rrp * (1 - vc / 100))

    # OPEN / UNKNOWN: floor = 0 (handled in apply_flags_with_open_pct)
    auth_floor = auth_floor.fillna(0)

    df["authorised_floor"]    = auth_floor.round(2)
    df["authorised_disc_pct"] = ((safe_rrp - auth_floor) / safe_rrp * 100).where(
        safe_rrp.notna() & (auth_floor > 0)
    ).round(2)
    df["overshoot_pct"] = (df["actual_total_disc_pct"] - df["authorised_disc_pct"]).round(2)

    # ── Three new seller discount breakdown columns ────────────────────────────
    # Col 10: Seller SRP Discount % = (RRP - SRP) / RRP * 100
    # Where SRP == RRP (no markdown), this is 0%
    srp_for_calc = srp_used.where(srp_used.notna() & (srp_used > 0), safe_rrp)
    df["seller_srp_disc_pct"] = ((safe_rrp - srp_for_calc) / safe_rrp * 100).where(
        safe_rrp.notna() & (srp_for_calc < safe_rrp)   # only show when SRP < RRP
    ).fillna(0).round(2)

    # Col 11: Seller Voucher Discount % from Exclusion Remark
    # = vc_pct stated in remark (e.g. 10% VC ONLY → 10.0)
    # Calculated ON SRP: actual discount amount = SRP × vc_pct/100
    # Express as % of RRP for consistency: (SRP × vc_pct/100) / RRP × 100
    vc_series = pd.to_numeric(df["vc_pct"], errors="coerce").fillna(0)
    vc_amount = srp_for_calc * vc_series / 100            # VC discount amount
    df["seller_vc_disc_pct"] = (vc_amount / safe_rrp * 100).where(
        safe_rrp.notna() & (vc_series > 0)
    ).fillna(0).round(2)

    # Col 12: Seller END Discount % = SRP disc % + VC disc % = total authorised seller disc
    # = (RRP - authorised_floor) / RRP * 100  (same as authorised_disc_pct)
    # Using the two components explicitly for transparency
    df["seller_end_disc_pct"] = (df["seller_srp_disc_pct"] + df["seller_vc_disc_pct"]).round(2)

    return df


# ── Apply flags (vectorised) ──────────────────────────────────────────────────
def apply_flags_with_open_pct(df: pd.DataFrame,
                               open_pct_map: dict | None = None) -> pd.DataFrame:
    """
    Fully vectorised flag logic. open_pct_map = {(region, mp): float}.
    Also fills seller_vc_disc_pct and seller_end_disc_pct for OPEN rows
    using the sidebar-entered voucher % (since OPEN remarks have no % in text).
    """
    if open_pct_map is None:
        open_pct_map = {}

    df = df.copy()

    # ── Fill OPEN rows with sidebar VC % ─────────────────────────────────────
    # For OPEN rule_type rows: seller_vc_disc_pct = open_pct (expressed as % of RRP)
    # seller_end_disc_pct = seller_srp_disc_pct + seller_vc_disc_pct
    if "rule_type" in df.columns and open_pct_map:
        m_open = df["rule_type"] == "open"
        if m_open.any():
            # Map each OPEN row to its open_pct via (region, marketplace)
            def _open_pct_for_row(row):
                return open_pct_map.get((row.get("region",""), row.get("marketplace","")), 0.0)

            # Skip rows where paid_price=0 (cancelled/unpaid — no real transaction)
            paid_open = df.loc[m_open, "paid_price"].fillna(0)
            m_open = m_open & (paid_open > 0)

            # Recompute open_pct_series with the updated (smaller) m_open mask
            open_pct_series = df[m_open].apply(_open_pct_for_row, axis=1)

            # VC discount % for OPEN = open_pct applied to SRP (or RRP if no SRP)
            # expressed as % of RRP for consistency with other rules
            srp_for_open  = df.loc[m_open, "srp_used"].fillna(df.loc[m_open, "rrp_used"])
            safe_rrp_open = df.loc[m_open, "rrp_used"].replace(0, np.nan)
            vc_amount_open = srp_for_open * open_pct_series.values / 100
            vc_pct_of_rrp  = (vc_amount_open / safe_rrp_open * 100).round(2)

            df.loc[m_open, "seller_vc_disc_pct"]  = vc_pct_of_rrp.values
            df.loc[m_open, "vc_pct"]               = open_pct_series.values
            df.loc[m_open, "seller_end_disc_pct"]  = (
                df.loc[m_open, "seller_srp_disc_pct"].fillna(0) + vc_pct_of_rrp
            ).round(2).values

            # Also update authorised_floor and authorised_disc_pct for OPEN rows
            auth_floor_open  = srp_for_open * (1 - open_pct_series.values / 100)
            auth_disc_open   = ((safe_rrp_open - auth_floor_open) / safe_rrp_open * 100).round(2)
            df.loc[m_open, "authorised_floor"]    = auth_floor_open.values
            df.loc[m_open, "authorised_disc_pct"] = auth_disc_open.values
            df.loc[m_open, "overshoot_pct"]       = (
                df.loc[m_open, "actual_total_disc_pct"] - auth_disc_open
            ).round(2).values

    rt        = df["rule_type"]
    overshoot = df["overshoot_pct"]
    paid      = df["paid_price"]
    srp_floor = df["srp_used"].fillna(df["rrp_used"])

    flagged  = pd.Series(False,  index=df.index)
    severity = pd.Series("grey", index=df.index)
    reason   = pd.Series("",     index=df.index)

    # ── EXCLUDE ───────────────────────────────────────────────────────────────
    m_excl    = rt == "exclude"
    paid_valid = paid > 0   # ignore cancelled/unpaid orders (paid=0)
    below_srp  = paid_valid & (paid < (srp_floor - 0.5))
    flagged  = flagged  | (m_excl & below_srp)
    severity = severity.where(~m_excl, np.where(m_excl & below_srp, "red", "green"))
    reason   = reason.where(~m_excl,
        np.where(m_excl & below_srp,
                 "EXCLUDED — paid below SRP. 🚨",
                 "EXCLUDED — at or above SRP. ✅"))

    # ── OPEN: use sidebar % ───────────────────────────────────────────────────
    m_open = rt == "open"
    if m_open.any() and open_pct_map:
        # Map each row to its open_pct
        def _get_open_pct(row_idx):
            r = df.loc[row_idx, "region"]    if "region"      in df.columns else ""
            m = df.loc[row_idx, "marketplace"] if "marketplace" in df.columns else ""
            return open_pct_map.get((r, m), None)

        open_rows = df.index[m_open]
        for idx in open_rows:
            opct = _get_open_pct(idx)
            if opct is None:
                severity.iloc[idx] = "grey"
                reason.iloc[idx]   = "OPEN — no max % set in sidebar."
                continue
            safe_rrp_val = df.at[idx, "rrp_used"]
            paid_val     = df.at[idx, "paid_price"]
            if pd.isna(safe_rrp_val) or safe_rrp_val == 0:
                severity.iloc[idx] = "grey"
                reason.iloc[idx]   = "OPEN — missing RRP."
                continue
            if pd.isna(paid_val) or paid_val == 0:
                # Cancelled / unpaid order — no real transaction, do not flag
                severity.iloc[idx] = "grey"
                reason.iloc[idx]   = "OPEN — cancelled or unpaid order, skipped."
                continue
            actual_pct  = (safe_rrp_val - paid_val) / safe_rrp_val * 100
            os          = actual_pct - opct
            if os > OVERSHOOT_CHECK:
                flagged.iloc[idx]  = True
                severity.iloc[idx] = "red"
                reason.iloc[idx]   = f"OPEN overshoot {os:.1f}% > {OVERSHOOT_CHECK}%. 🚨"
            elif os > OVERSHOOT_OK:
                severity.iloc[idx] = "amber"
                reason.iloc[idx]   = f"OPEN overshoot {os:.1f}% (check). ⚠️"
            else:
                severity.iloc[idx] = "green"
                reason.iloc[idx]   = f"OPEN — within tolerance. ✅"
    elif m_open.any():
        severity = severity.where(~m_open, "grey")
        reason   = reason.where(~m_open, "OPEN — enter max % in sidebar.")

    # ── MAX / EXACT_VC: use overshoot ────────────────────────────────────────
    # Only flag when paid_price > 0 (skip cancelled/unpaid orders)
    m_rule = rt.isin(["max_pct", "exact_vc"]) & (paid > 0)
    m_red  = m_rule & (overshoot > OVERSHOOT_CHECK)
    m_amb  = m_rule & (overshoot > OVERSHOOT_OK) & ~m_red
    m_ok   = m_rule & (overshoot <= OVERSHOOT_OK)

    flagged  = flagged  | m_red
    severity = severity.where(~m_red, "red")
    severity = severity.where(~m_amb, "amber")
    severity = severity.where(~m_ok,  "green")
    reason   = reason.where(~m_red,
        "Overshoot " + overshoot.astype(str) + "% > 10%. 🚨")
    reason   = reason.where(~m_amb,
        "Overshoot " + overshoot.astype(str) + "% (5–10% check). ⚠️")
    reason   = reason.where(~m_ok,
        "Within tolerance. ✅")

    # ── UNKNOWN ───────────────────────────────────────────────────────────────
    m_unk = rt == "unknown"
    severity = severity.where(~m_unk, "grey")
    reason   = reason.where(~m_unk, "No recognised remark — manual review.")

    df["flagged"]       = flagged
    df["flag_severity"] = severity
    df["flag_reason"]   = reason
    return df


# ── Aggregation helpers ────────────────────────────────────────────────────────
def summary_by_marketplace(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(["region","marketplace"])
            .agg(Total_Orders         =("order_id",            "count"),
                 RRP_Matched          =("RRP",                 lambda x: x.notna().sum()),
                 Avg_RRP              =("rrp_used",             "mean"),
                 Sum_RRP              =("rrp_used",             "sum"),
                 Sum_Paid             =("paid_price",           "sum"),
                 Avg_Actual_Disc      =("actual_total_disc_pct","mean"),
                 Avg_Auth_Disc        =("authorised_disc_pct",  "mean"),
                 Avg_Overshoot        =("overshoot_pct",        "mean"),
                 Flagged              =("flagged",              "sum"))
            .round(2).reset_index())


def flagged_orders(df: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in [
        "region","marketplace","order_id","sku","Article Number","product_name",
        "order_status","rrp_used","srp_used",
        "seller_srp_disc_pct","seller_vc_disc_pct","seller_end_disc_pct",
        "paid_price","actual_total_disc_pct",
        "remark","rule_label","rule_type","flagged","flag_reason","flag_severity",
    ] if c in df.columns]
    return df[df["flagged"] == True][keep].reset_index(drop=True)


def _format_col_name(col: str) -> str:
    """Map internal column names to the exact display names from the format file."""
    mapping = {
        "region":               "Region",
        "marketplace":          "Marketplace",
        "order_id":             "Order Id",
        "sku":                  "Sku",
        "Article Number":       "Article Number",
        "product_name":         "Product Name",
        "order_status":         "Order Status",
        "rrp_used":             "Rrp Used",
        "srp_used":             "Srp Used",
        "seller_srp_disc_pct":  "Seller SRP Discount %",
        "seller_vc_disc_pct":   "SELLER Voucher Discount % Mentioned in Exclusion Remark",
        "seller_end_disc_pct":  "SELLER END DISCOUNT %",
        "paid_price":           "Customer PAID Price",
        "actual_total_disc_pct":"Calculate Discount % from Customer PAID PRICE From RRP",
        "remark":               "Remark",
        "rule_label":           "Rule Label",
        "rule_type":            "Rule Type",
        "flagged":              "Flagged",
        "flag_reason":          "Flag Reason",
        "flag_severity":        "Flag Severity",
    }
    return mapping.get(col, col.replace("_"," ").title())


def exclusion_summary(df: pd.DataFrame) -> pd.DataFrame:
    grp_cols = [c for c in ["region","marketplace","remark","rule_label","flag_severity"]
                if c in df.columns]
    if not grp_cols:
        return pd.DataFrame()
    grp = (df.groupby(grp_cols, dropna=False, observed=True)
             .agg(Orders              =("order_id",            "count"),
                  Flagged             =("flagged",             "sum"),
                  Sum_RRP             =("rrp_used",            "sum"),
                  Sum_Paid            =("paid_price",          "sum"),
                  Sum_Seller_Disc     =("seller_discount_amount","sum"),
                  Avg_Auth_Disc_Pct   =("authorised_disc_pct", "mean"),
                  Avg_Actual_Disc_Pct =("actual_total_disc_pct","mean"),
                  Avg_Overshoot_Pct   =("overshoot_pct",       "mean"),
                  Max_Overshoot_Pct   =("overshoot_pct",       "max"))
             .reset_index())
    grp["Seller_Disc_Pct"] = (grp["Sum_Seller_Disc"] / grp["Sum_RRP"] * 100).round(1)
    return grp.round(2)
