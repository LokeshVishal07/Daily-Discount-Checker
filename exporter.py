"""
exporter.py — Excel report builder
Fixed for pandas 3.x / openpyxl 3.1.5:
  - to_excel() uses sheet_name= keyword (not positional)
  - No Styler.apply() — plain dataframe with background_map via openpyxl directly
  - groupby on columns that actually exist in the new discount_engine output
"""
from __future__ import annotations
import io
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

WHITE_BOLD  = Font(color="FFFFFF", bold=True, size=10)
DARK_FILL   = PatternFill("solid", fgColor="1F3864")
RED_FILL    = PatternFill("solid", fgColor="C00000")
ALT_FILL    = PatternFill("solid", fgColor="F2F2F2")
CENTER      = Alignment(horizontal="center", wrap_text=True)
LEFT        = Alignment(horizontal="left")

SEV_COLORS = {
    "red":    "FFCCCC",
    "orange": "FFE5CC",
    "amber":  "FFF5CC",
    "green":  "CCFFDD",
    "grey":   "EEEEEE",
}


def build_report(result_df: pd.DataFrame) -> bytes:
    from discount_engine import flagged_orders

    buf = io.BytesIO()

    # Sheet 1: Exclusion Summary (simple, no Styler)
    excl = _build_excl_simple(result_df)

    # Sheet 2: Full Detail
    detail_cols = [c for c in [
        "region", "marketplace", "order_id", "sku", "Article Number",
        "product_name", "order_status", "rrp_used", "srp_used",
        "authorised_floor", "authorised_disc_pct", "paid_price",
        "actual_total_disc_pct", "overshoot_pct",
        "seller_discount_amount", "platform_discount_amount",
        "seller_disc_pct", "remark", "rule_label", "rule_type",
        "flagged", "flag_reason", "flag_severity",
    ] if c in result_df.columns]
    detail   = result_df[detail_cols].copy()
    sev_col  = result_df["flag_severity"] if "flag_severity" in result_df.columns else pd.Series(dtype=str)

    # Sheet 3: Flagged
    flagged  = flagged_orders(result_df)
    flag_sev = flagged["flag_severity"] if "flag_severity" in flagged.columns else pd.Series(dtype=str)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        excl.to_excel(writer, sheet_name="Exclusion Summary", index=False)
        _style_ws(writer.sheets["Exclusion Summary"], excl, pd.Series(dtype=str))

        detail.rename(columns=_pm).to_excel(writer, sheet_name="Full Detail", index=False)
        _style_ws(writer.sheets["Full Detail"], detail, sev_col)

        if not flagged.empty:
            flagged.rename(columns=_pm).to_excel(writer, sheet_name="Flagged Orders", index=False)
            _style_ws(writer.sheets["Flagged Orders"], flagged, flag_sev, header_fill=RED_FILL)
        else:
            pd.DataFrame([{"Note": "No flagged orders ✅"}]).to_excel(
                writer, sheet_name="Flagged Orders", index=False)
            _style_ws(writer.sheets["Flagged Orders"], pd.DataFrame(), pd.Series(dtype=str))

    return buf.getvalue()


def _build_excl_simple(df: pd.DataFrame) -> pd.DataFrame:
    """Build exclusion summary — uses rule_label (new engine column name)."""
    # Cancel filter
    status = df.get("order_status", pd.Series("", index=df.index))
    active = df[~status.astype(str).str.lower().str.contains("cancel", na=False)].copy()

    # Use rule_label (discount_engine output), fall back gracefully
    remark_col = "remark"       if "remark"     in active.columns else None
    rule_col   = "rule_label"   if "rule_label"  in active.columns else None
    sev_col    = "flag_severity" if "flag_severity" in active.columns else None

    rows = []
    grp_keys = [c for c in [remark_col, rule_col, sev_col, "region", "marketplace"] if c]

    if not grp_keys or active.empty:
        return pd.DataFrame(columns=["Region","Marketplace","Exclusion Remark","Rule",
                                      "Orders","Sum of RRP","Sum of Seller Disc",
                                      "Seller Disc %","Violations","Status"])

    for keys, grp in active.groupby(grp_keys, dropna=False, observed=True):
        if not isinstance(keys, tuple):
            keys = (keys,)
        key_map = dict(zip(grp_keys, keys))
        remark   = str(key_map.get(remark_col, ""))
        rule     = str(key_map.get(rule_col,   ""))
        severity = str(key_map.get(sev_col,    "grey"))
        region   = str(key_map.get("region",   ""))
        mp       = str(key_map.get("marketplace", ""))

        sum_rrp  = grp["rrp_used"].sum()              if "rrp_used"              in grp.columns else 0
        sum_sd   = grp["seller_discount_amount"].sum() if "seller_discount_amount" in grp.columns else 0
        orders   = len(grp)
        flagged  = int(grp["flagged"].sum())           if "flagged"               in grp.columns else 0
        sd_pct   = round(sum_sd / sum_rrp * 100, 1)   if sum_rrp > 0 else 0.0

        rows.append({
            "Region":          region,
            "Marketplace":     mp,
            "Exclusion Remark":remark if remark not in ("", "nan") else "(no remark)",
            "Rule":            rule,
            "Orders":          orders,
            "Sum of RRP":      round(sum_rrp, 2),
            "Sum of Seller Disc": round(sum_sd, 2),
            "Seller Disc %":   sd_pct,
            "Violations":      flagged,
            "Status":          "🚨 Violated" if flagged > 0 else "✅ OK",
            "_severity":       severity,
        })

    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values(["Violations","Seller Disc %"], ascending=[False,False])
            .reset_index(drop=True))


def _style_ws(ws, df: pd.DataFrame, severities: pd.Series, header_fill=None):
    hf = header_fill or DARK_FILL
    for cell in ws[1]:
        cell.fill = hf
        cell.font = WHITE_BOLD
        cell.alignment = CENTER
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 45)

    sev_list = list(severities.reset_index(drop=True)) if len(severities) > 0 else []
    for ri, row in enumerate(ws.iter_rows(min_row=2)):
        sev  = sev_list[ri] if ri < len(sev_list) else None
        fgc  = SEV_COLORS.get(str(sev), "")
        fill = PatternFill("solid", fgColor=fgc) if fgc else (ALT_FILL if ri % 2 == 0 else PatternFill())
        for cell in row:
            cell.fill      = fill
            cell.alignment = LEFT


def _pm(col):
    return col.replace("_", " ").title()
