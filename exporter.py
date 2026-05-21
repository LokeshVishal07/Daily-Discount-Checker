"""exporter.py — Excel report builder (fixed for pandas/openpyxl compatibility)"""
from __future__ import annotations
import io
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

WHITE_BOLD = Font(color="FFFFFF", bold=True, size=10)
DARK_FILL  = PatternFill("solid", fgColor="1F3864")
RED_FILL   = PatternFill("solid", fgColor="C00000")
ALT_FILL   = PatternFill("solid", fgColor="F2F2F2")
CENTER     = Alignment(horizontal="center", wrap_text=True)
LEFT       = Alignment(horizontal="left")

SEV_FILL = {
    "red":    PatternFill("solid", fgColor="FFCCCC"),
    "orange": PatternFill("solid", fgColor="FFE5CC"),
    "amber":  PatternFill("solid", fgColor="FFF5CC"),
    "green":  PatternFill("solid", fgColor="CCFFDD"),
    "grey":   PatternFill("solid", fgColor="EEEEEE"),
}


def build_report(result_df: pd.DataFrame) -> bytes:
    from discount_engine import flagged_orders

    buf = io.BytesIO()

    # ── Sheet 1: Simple Exclusion Summary ─────────────────────────────────────
    excl_simple = _build_simple_excl(result_df)

    # ── Sheet 2: Full Detail ──────────────────────────────────────────────────
    detail_cols = [c for c in [
        "region","marketplace","order_id","sku","Article Number","product_name",
        "order_status","rrp_used","paid_price",
        "seller_discount_amount","platform_discount_amount",
        "seller_disc_pct","customer_disc_pct",
        "remark","allowed_rule","flagged","flag_reason","flag_severity",
    ] if c in result_df.columns]
    detail = result_df[detail_cols].copy()

    # ── Sheet 3: Flagged ──────────────────────────────────────────────────────
    flagged = flagged_orders(result_df)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        # Sheet 1
        excl_simple.to_excel(writer, sheet_name="Exclusion Summary", index=False)
        _style_ws(writer.sheets["Exclusion Summary"], len(excl_simple))

        # Sheet 2
        detail.rename(columns=_pm).to_excel(writer, sheet_name="Full Detail", index=False)
        _style_ws(writer.sheets["Full Detail"], len(detail),
                  severities=result_df.get("flag_severity", pd.Series(dtype=str)))

        # Sheet 3
        if not flagged.empty:
            flagged.rename(columns=_pm).to_excel(writer, sheet_name="Flagged Orders", index=False)
            _style_ws(writer.sheets["Flagged Orders"], len(flagged),
                      severities=flagged.get("flag_severity", pd.Series(dtype=str)),
                      header_fill=RED_FILL)
        else:
            pd.DataFrame([{"Note": "No flagged orders ✅"}]).to_excel(
                writer, sheet_name="Flagged Orders", index=False)
            _style_ws(writer.sheets["Flagged Orders"], 1)

    return buf.getvalue()


def _build_simple_excl(df: pd.DataFrame) -> pd.DataFrame:
    """Build the simple exclusion summary table."""
    # Exclude cancelled orders from totals
    active = df[~df.get("order_status", pd.Series("")).astype(str).str.lower().str.contains("cancel", na=False)]

    rows = []
    for (region, mp, remark, rule, severity), grp in active.groupby(
        ["region", "marketplace", "remark", "allowed_rule", "flag_severity"],
        dropna=False
    ):
        sum_rrp      = grp["rrp_used"].sum()
        sum_sel_disc = grp["seller_discount_amount"].sum()
        orders       = len(grp)
        flagged      = int(grp["flagged"].sum())
        sel_disc_pct = (sum_sel_disc / sum_rrp * 100) if sum_rrp > 0 else 0
        status       = "🚨 Violated" if flagged > 0 else "✅ OK"
        rows.append({
            "Region":              region,
            "Marketplace":         mp,
            "Exclusion Remark":    remark if remark else "(no remark)",
            "Rule":                rule,
            "Orders":              orders,
            "Sum of RRP":          round(sum_rrp, 2),
            "Sum of Seller Disc":  round(sum_sel_disc, 2),
            "Seller Disc %":       round(sel_disc_pct, 1),
            "Violations":          flagged,
            "Status":              status,
        })

    if not rows:
        return pd.DataFrame(columns=[
            "Region","Marketplace","Exclusion Remark","Rule",
            "Orders","Sum of RRP","Sum of Seller Disc","Seller Disc %","Violations","Status"
        ])
    return pd.DataFrame(rows).sort_values(
        ["Region","Marketplace","Seller Disc %"], ascending=[True, True, False]
    ).reset_index(drop=True)


def _style_ws(ws, nrows: int, severities=None, header_fill=None):
    hf = header_fill or DARK_FILL
    for cell in ws[1]:
        cell.fill = hf
        cell.font = WHITE_BOLD
        cell.alignment = CENTER
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    # Auto column width
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 3, 45)

    # Row shading
    sev_list = list(severities.reset_index(drop=True)) if severities is not None and len(severities) > 0 else []
    for ri, row in enumerate(ws.iter_rows(min_row=2)):
        sev  = sev_list[ri] if ri < len(sev_list) else None
        fill = SEV_FILL.get(str(sev), ALT_FILL if ri % 2 == 0 else PatternFill())
        for cell in row:
            cell.fill = fill
            cell.alignment = LEFT


def _pm(col):
    return col.replace("_", " ").title()
