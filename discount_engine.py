"""
discount_engine.py

New discount logic:
  1. Parse ZeCom remark → extract rule type + authorised % (VC %, MAX %, etc.)
  2. Compute AUTHORISED FLOOR PRICE from RRP + SRP + remark rule
  3. Compare actual customer paid price to authorised floor
  4. Derive OVERSHOOT = actual_total_disc% - authorised_total_disc%
  5. Apply tolerance bands:
       overshoot < 5%   → OK (green)
       5% ≤ overshoot ≤ 10% → CHECK (amber)
       overshoot > 10%  → FLAGGED (red)
  EXCLUDE rule: any paid price below SRP = immediate flag regardless of overshoot
"""
from __future__ import annotations
import re, logging
import numpy as np
import pandas as pd
from settings import EXCLUSION_RULES, MARKETPLACE_COLUMNS

logger = logging.getLogger(__name__)

OVERSHOOT_OK    = 5.0    # below this = OK
OVERSHOOT_CHECK = 10.0   # above this = flagged red, between = amber


def run_pipeline(orders: pd.DataFrame, content: pd.DataFrame,
                 zecom_lookup: pd.DataFrame) -> pd.DataFrame:
    merged     = _merge(orders, content, zecom_lookup)
    with_disc  = _calc_discounts(merged)
    with_flags = _apply_flags(with_disc)
    return with_flags


# ── Merge ─────────────────────────────────────────────────────────────────────
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
              .merge(zecom,   on="Article Number", how="left"))
    merged.drop(columns=["_sku"], inplace=True)
    return merged.reset_index(drop=True)


# ── Parse remark → rule ───────────────────────────────────────────────────────
def parse_remark(remark: str) -> dict:
    """
    Returns dict with keys:
      rule_type : 'exclude' | 'exact_vc' | 'max_pct' | 'open' | 'unknown'
      vc_pct    : float | None   (the voucher % stated in remark)
      label     : str
    """
    rm = str(remark).strip().lower()

    if not rm or rm in ("", "none", "nan", "0"):
        return {"rule_type": "unknown", "vc_pct": None, "label": "(no remark)"}

    # EXCLUDE
    if "exclude" in rm:
        return {"rule_type": "exclude", "vc_pct": None, "label": "EXCLUDED — sell at SRP only"}

    # OPEN FOR ALL
    if "open for all" in rm or rm.startswith("open for"):
        return {"rule_type": "open", "vc_pct": None, "label": "OPEN — seller sets own VC %"}

    # X% VC ONLY  (e.g. "10% VC ONLY - NO bundle", "40% VC ONLY - NO bundle & OPEN platform VC")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*vc\s*only", rm)
    if m:
        pct = float(m.group(1))
        return {"rule_type": "exact_vc", "vc_pct": pct, "label": f"{int(pct)}% VC ONLY (on SRP/RRP)"}

    # MAX X%  (e.g. "MAX 30%", "MAX 50% DISC")
    m = re.search(r"max\s+(\d+(?:\.\d+)?)\s*%", rm)
    if m:
        pct = float(m.group(1))
        return {"rule_type": "max_pct", "vc_pct": pct, "label": f"MAX {int(pct)}%"}

    return {"rule_type": "unknown", "vc_pct": None, "label": f"Remark: {remark[:60]}"}


def compute_authorised_floor(rrp: float, srp: float, rule: dict) -> tuple[float, float]:
    """
    Returns (authorised_floor_price, authorised_total_disc_pct_from_rrp).
    srp: if 0 or NaN, fall back to rrp.
    """
    if not rrp or np.isnan(rrp) or rrp <= 0:
        return (np.nan, np.nan)

    # SRP fallback: use RRP if SRP is missing/zero
    base_srp = srp if (srp and not np.isnan(srp) and srp > 0) else rrp

    rule_type = rule.get("rule_type", "unknown")
    vc_pct    = rule.get("vc_pct")

    if rule_type == "exclude":
        # Floor = SRP (no discount below SRP allowed)
        floor = base_srp
    elif rule_type == "exact_vc":
        # Floor = SRP × (1 − vc_pct/100), or RRP × (1 − vc_pct/100) if no SRP
        floor = base_srp * (1 - vc_pct / 100)
    elif rule_type == "max_pct":
        # Floor = RRP × (1 − max_pct/100)
        floor = rrp * (1 - vc_pct / 100)
    elif rule_type == "open":
        # No authorised floor from ZeCom — use 0 (let OPEN % from sidebar handle it)
        floor = 0.0
    else:
        floor = 0.0

    auth_disc_pct = ((rrp - floor) / rrp * 100) if rrp > 0 and floor > 0 else np.nan
    return (floor, auth_disc_pct)


# ── Calculate discounts ────────────────────────────────────────────────────────
def _calc_discounts(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rrp_raw = pd.to_numeric(df.get("RRP"),  errors="coerce")
    srp_raw = pd.to_numeric(df.get("SRP"),  errors="coerce")
    orig    = pd.to_numeric(df.get("original_price"), errors="coerce")
    paid    = pd.to_numeric(df.get("paid_price"),      errors="coerce")
    s_disc  = pd.to_numeric(df.get("seller_discount_amount",   0), errors="coerce").fillna(0)
    p_disc  = pd.to_numeric(df.get("platform_discount_amount", 0), errors="coerce").fillna(0)

    # RRP: ZeCom first, fallback to marketplace listed price
    rrp_used = rrp_raw.where(rrp_raw.notna() & (rrp_raw > 0), orig)
    # SRP: ZeCom value; 0/NaN means use RRP as fallback (handled in compute_authorised_floor)
    srp_used = srp_raw.where(srp_raw.notna() & (srp_raw > 0), np.nan)

    df["rrp_used"]   = rrp_used
    df["srp_used"]   = srp_used
    df["rrp_source"] = np.where(rrp_raw.notna() & (rrp_raw > 0), "ZeCom", "MP Listed Price")

    safe_rrp = rrp_used.replace(0, np.nan)

    df["actual_total_disc_pct"] = ((safe_rrp - paid) / safe_rrp * 100).round(2)
    df["seller_disc_pct"]       = (s_disc / safe_rrp * 100).round(2)
    df["platform_disc_pct"]     = (p_disc / safe_rrp * 100).round(2)

    # Parse remark and compute authorised floor per row
    remarks = df.get("remark", pd.Series([""] * len(df)))
    rules_parsed = remarks.apply(parse_remark)

    auth_floors = []
    auth_discs  = []
    rule_labels = []
    rule_types  = []

    for i, row in df.iterrows():
        rrp  = row.get("rrp_used", np.nan)
        srp  = row.get("srp_used", np.nan)
        rule = rules_parsed.iloc[i]
        floor, auth_disc = compute_authorised_floor(
            float(rrp) if not np.isnan(float(rrp)) else np.nan,
            float(srp) if (srp is not None and not np.isnan(float(srp if srp else 0))) else np.nan,
            rule
        )
        auth_floors.append(floor)
        auth_discs.append(auth_disc)
        rule_labels.append(rule["label"])
        rule_types.append(rule["rule_type"])

    df["authorised_floor"]    = auth_floors
    df["authorised_disc_pct"] = auth_discs
    df["rule_label"]          = rule_labels
    df["rule_type"]           = rule_types

    # Overshoot = actual disc % − authorised disc %
    df["overshoot_pct"] = (df["actual_total_disc_pct"] - df["authorised_disc_pct"]).round(2)

    return df


# ── Apply tolerance bands ─────────────────────────────────────────────────────
def _apply_flags(df: pd.DataFrame, open_pct_map: dict | None = None) -> pd.DataFrame:
    """
    open_pct_map: {(region, marketplace): float} from sidebar inputs.
    Applied only to OPEN rule_type rows.
    """
    if open_pct_map is None:
        open_pct_map = {}

    df = df.copy()
    flagged_list, severity_list, reason_list = [], [], []

    for _, row in df.iterrows():
        rule_type    = row.get("rule_type", "unknown")
        overshoot    = row.get("overshoot_pct", np.nan)
        paid         = row.get("paid_price", np.nan)
        srp          = row.get("srp_used", np.nan)
        rrp          = row.get("rrp_used", np.nan)
        auth_floor   = row.get("authorised_floor", np.nan)
        auth_disc    = row.get("authorised_disc_pct", np.nan)
        actual_disc  = row.get("actual_total_disc_pct", np.nan)
        region       = row.get("region", "")
        mp           = row.get("marketplace", "")

        # ── EXCLUDE: flag if paid < SRP ───────────────────────────────────────
        if rule_type == "exclude":
            srp_floor = srp if (srp and not np.isnan(float(srp if srp is not None else 0)) and srp > 0) else rrp
            below_srp = (paid < srp_floor - 0.5) if (not np.isnan(paid) and srp_floor) else False
            if below_srp:
                flagged_list.append(True)
                severity_list.append("red")
                reason_list.append(f"EXCLUDED — paid {paid:.2f} is below SRP floor {srp_floor:.2f}. 🚨")
            else:
                flagged_list.append(False)
                severity_list.append("green")
                reason_list.append(f"EXCLUDED — selling at or above SRP. ✅")
            continue

        # ── OPEN: use sidebar-entered % to determine authorised floor ─────────
        if rule_type == "open":
            open_pct = open_pct_map.get((region, mp), None)
            if open_pct is not None:
                open_floor = rrp * (1 - open_pct / 100) if (rrp and not np.isnan(rrp)) else np.nan
                if open_floor and not np.isnan(open_floor):
                    actual_disc_pct = ((rrp - paid) / rrp * 100) if rrp else np.nan
                    auth_disc_from_open = open_pct
                    overshoot = (actual_disc_pct - auth_disc_from_open) if not np.isnan(actual_disc_pct) else np.nan
                else:
                    overshoot = np.nan
            else:
                # No sidebar input for this marketplace: treat as no cap
                flagged_list.append(False)
                severity_list.append("grey")
                reason_list.append("OPEN — no max % set in sidebar. Enter max % in sidebar to enable flagging.")
                continue

        # ── All other rules (exact_vc, max_pct, open with pct) ───────────────
        if np.isnan(overshoot) if (overshoot is None or (isinstance(overshoot, float) and np.isnan(overshoot))) else False:
            flagged_list.append(False)
            severity_list.append("grey")
            reason_list.append("Cannot calculate — missing RRP or paid price.")
            continue

        if overshoot > OVERSHOOT_CHECK:
            flagged_list.append(True)
            severity_list.append("red")
            reason_list.append(
                f"Overshoot {overshoot:.1f}% > {OVERSHOOT_CHECK}%. "
                f"Actual disc {actual_disc:.1f}% vs authorised {auth_disc:.1f}%. 🚨"
            )
        elif overshoot > OVERSHOOT_OK:
            flagged_list.append(False)
            severity_list.append("amber")
            reason_list.append(
                f"Overshoot {overshoot:.1f}% (5–10% range — needs check). "
                f"Actual disc {actual_disc:.1f}% vs authorised {auth_disc:.1f}%."
            )
        else:
            flagged_list.append(False)
            severity_list.append("green")
            reason_list.append(
                f"Within tolerance. Overshoot {overshoot:.1f}% < {OVERSHOOT_OK}%. ✅"
            )

    df["flagged"]       = flagged_list
    df["flag_severity"] = severity_list
    df["flag_reason"]   = reason_list
    return df


# ── Public helpers ────────────────────────────────────────────────────────────
def apply_flags_with_open_pct(df: pd.DataFrame, open_pct_map: dict) -> pd.DataFrame:
    """Called from app.py after sidebar inputs are known."""
    return _apply_flags(df, open_pct_map=open_pct_map)


def summary_by_marketplace(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(["region", "marketplace"])
            .agg(Total_Orders        =("order_id",           "count"),
                 RRP_Matched         =("RRP",                lambda x: x.notna().sum()),
                 Avg_RRP             =("rrp_used",            "mean"),
                 Sum_RRP             =("rrp_used",            "sum"),
                 Sum_Paid            =("paid_price",          "sum"),
                 Avg_Actual_Disc     =("actual_total_disc_pct","mean"),
                 Avg_Auth_Disc       =("authorised_disc_pct", "mean"),
                 Avg_Overshoot       =("overshoot_pct",       "mean"),
                 Flagged             =("flagged",             "sum"))
            .round(2).reset_index())


def flagged_orders(df: pd.DataFrame) -> pd.DataFrame:
    keep = [c for c in [
        "region","marketplace","order_id","sku","Article Number","product_name",
        "order_status","order_date","rrp_used","srp_used","authorised_floor",
        "authorised_disc_pct","paid_price","actual_total_disc_pct",
        "overshoot_pct","seller_discount_amount","platform_discount_amount",
        "seller_disc_pct","remark","rule_label","rule_type",
        "flag_reason","flag_severity",
    ] if c in df.columns]
    return df[df["flagged"] == True][keep].reset_index(drop=True)


def exclusion_summary(df: pd.DataFrame) -> pd.DataFrame:
    """For the Exclusion Rule Dashboard tab."""
    grp = (df.groupby(["region","marketplace","remark","rule_label","flag_severity"])
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
