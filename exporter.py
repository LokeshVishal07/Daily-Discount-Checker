"""exporter.py — Excel report builder"""
from __future__ import annotations
import io
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment
from openpyxl.utils import get_column_letter

SEV_FILL = {
    "red":    PatternFill("solid", fgColor="FF4B4B"),
    "orange": PatternFill("solid", fgColor="FF8C00"),
    "amber":  PatternFill("solid", fgColor="FFC300"),
    "green":  PatternFill("solid", fgColor="2ECC71"),
    "grey":   PatternFill("solid", fgColor="BDC3C7"),
}
WHITE_BOLD = Font(color="FFFFFF", bold=True, size=10)
DARK_FILL  = PatternFill("solid", fgColor="1F3864")
RED_FILL   = PatternFill("solid", fgColor="C00000")
ALT_FILL   = PatternFill("solid", fgColor="F2F2F2")
CENTER     = Alignment(horizontal="center", wrap_text=True)
LEFT       = Alignment(horizontal="left")


def build_report(result_df: pd.DataFrame) -> bytes:
    from discount_engine import summary_by_marketplace, exclusion_summary, flagged_orders

    buf = io.BytesIO()
    detail_cols = [c for c in [
        "region","marketplace","order_id","sku","Article Number","product_name",
        "order_status","order_date","original_price","rrp_used","srp_used",
        "rrp_source","paid_price","seller_discount_amount","platform_discount_amount",
        "customer_disc_pct","seller_disc_pct","platform_disc_pct",
        "remark","allowed_rule","max_allowed_pct","flagged","flag_reason","flag_severity",
    ] if c in result_df.columns]

    detail   = result_df[detail_cols].copy()
    summary  = summary_by_marketplace(result_df)
    excl_sum = exclusion_summary(result_df)
    flagged  = flagged_orders(result_df)

    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        detail.rename(columns=_pm(detail.columns)).to_excel(writer, "Full Detail", index=False)
        _style(writer.sheets["Full Detail"],
               result_df.get("flag_severity", pd.Series(dtype=str)))

        summary.to_excel(writer, "MP Summary", index=False)
        _hdr(writer.sheets["MP Summary"])

        excl_sum.to_excel(writer, "Exclusion Summary", index=False)
        _hdr(writer.sheets["Exclusion Summary"])

        if not flagged.empty:
            flagged.rename(columns=_pm(flagged.columns)).to_excel(writer, "Flagged Orders", index=False)
            _style(writer.sheets["Flagged Orders"],
                   flagged.get("flag_severity", pd.Series(dtype=str)), hf=RED_FILL)
        else:
            pd.DataFrame([{"Note":"No flagged orders ✅"}]).to_excel(writer, "Flagged Orders", index=False)

    return buf.getvalue()


def _style(ws, severities, hf=None):
    for cell in ws[1]:
        cell.fill = hf or DARK_FILL; cell.font = WHITE_BOLD; cell.alignment = CENTER
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"
    for col in ws.columns:
        w = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(w + 2, 42)
    sevs = severities.reset_index(drop=True).tolist()
    for ri, row in enumerate(ws.iter_rows(min_row=2), start=0):
        sev  = sevs[ri] if ri < len(sevs) else None
        fill = SEV_FILL.get(str(sev), ALT_FILL if ri % 2 == 0 else PatternFill())
        for cell in row:
            cell.fill = fill; cell.alignment = LEFT


def _hdr(ws):
    for cell in ws[1]:
        cell.fill = DARK_FILL; cell.font = WHITE_BOLD; cell.alignment = CENTER
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"
    for col in ws.columns:
        w = max((len(str(c.value or "")) for c in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(w + 2, 28)


def _pm(cols):
    return {c: c.replace("_", " ").title() for c in cols}
