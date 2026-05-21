"""zecom_loader.py — Dynamic ZeCom file reader"""
from __future__ import annotations
import io, re, logging
from typing import Optional
import pandas as pd
import openpyxl
import openpyxl.utils as ou

logger = logging.getLogger(__name__)


def get_sheet_names(file_bytes: bytes) -> list[str]:
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True)
    return wb.sheetnames


def load_zecom_sheet(file_bytes: bytes, sheet_name: str):
    """
    Load one sheet. Returns (df, numeric_cols, text_cols, all_cols, error).
    Auto-detects header row — works even after bi-weekly column changes.
    """
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        if sheet_name not in wb.sheetnames:
            return None, [], [], [], f"Sheet '{sheet_name}' not found. Available: {wb.sheetnames}"

        ws   = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if len(rows) < 3:
            return None, [], [], [], "Sheet has too few rows."

        # Auto-detect header row = row with most non-null cells (first 6 rows)
        header_idx  = max(range(min(6, len(rows))),
                          key=lambda i: sum(1 for v in rows[i] if v is not None))
        raw_headers = list(rows[header_idx])
        data_rows   = [list(r) for r in rows[header_idx + 1:]]

        # Build unique column names: "Header Name [COL_LETTER]"
        col_names = []
        for i, h in enumerate(raw_headers):
            letter = ou.get_column_letter(i + 1)
            label  = re.sub(r"[\r\n]+", " ", str(h).strip()) if h else ""
            col_names.append(f"{label} [{letter}]" if label else f"[{letter}]")

        df = pd.DataFrame(data_rows, columns=col_names).dropna(how="all").reset_index(drop=True)

        numeric_cols, text_cols = [], []
        for col in df.columns:
            series    = df[col].dropna()
            converted = pd.to_numeric(series, errors="coerce")
            if converted.notna().sum() / max(len(series), 1) > 0.5 and converted.notna().sum() >= 3:
                numeric_cols.append(col)
            elif len(series) > 0:
                text_cols.append(col)

        return df, numeric_cols, text_cols, col_names, ""

    except Exception as exc:
        logger.exception("ZeCom load error")
        return None, [], [], [], str(exc)


def build_article_lookup(df, article_col, rrp_col, srp_col, remarks_col, platform_vc_col):
    """Build per-article lookup: Article Number | RRP | SRP | remark | platform_vc"""
    try:
        lookup = pd.DataFrame()
        lookup["Article Number"] = (
            df[article_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        )
        lookup["RRP"] = pd.to_numeric(df[rrp_col], errors="coerce")

        if srp_col and srp_col in df.columns:
            srp_raw = pd.to_numeric(df[srp_col], errors="coerce")
            lookup["SRP"] = srp_raw.where(srp_raw.notna() & (srp_raw > 0), lookup["RRP"])
        else:
            lookup["SRP"] = lookup["RRP"]

        if remarks_col and remarks_col in df.columns:
            lookup["remark"] = (df[remarks_col].astype(str).str.strip()
                                .replace({"None": "", "nan": "", "0": ""}))
        else:
            lookup["remark"] = ""

        if platform_vc_col and platform_vc_col in df.columns:
            lookup["platform_vc"] = df[platform_vc_col].astype(str).str.strip()
        else:
            lookup["platform_vc"] = ""

        lookup = lookup[
            lookup["Article Number"].notna() &
            (lookup["Article Number"] != "") &
            (lookup["Article Number"] != "nan") &
            (lookup["Article Number"].str.len() >= 3)
        ]
        return lookup.drop_duplicates("Article Number").reset_index(drop=True), ""

    except Exception as exc:
        return None, str(exc)


def guess_article_col(all_cols):
    keywords = ["style#", "article#", "pim article", "color_no", "colour_no", "articleno"]
    for col in all_cols:
        if any(k in col.lower() for k in keywords):
            return col
    return None


def guess_rrp_col(numeric_cols, region):
    patterns = [f"{region.upper()} RRP", "EC RRP", "RRP"]
    for pat in patterns:
        for col in numeric_cols:
            if pat.lower() in col.lower():
                return col
    return numeric_cols[0] if numeric_cols else None


def guess_srp_col(numeric_cols, region):
    for pat in ["ec srp", "md price", "srp ao", "srp"]:
        for col in numeric_cols:
            if pat.lower() in col.lower():
                return col
    return None


def guess_remarks_col(text_cols):
    for pat in ["mp promo rmk", "mp remarks", "exclusion", "promo rmk mp", "eoss promo rmk mp"]:
        for col in text_cols:
            if pat.lower() in col.lower():
                return col
    return None


def guess_platform_vc_col(text_cols):
    for pat in ["platform vc", "platform voucher"]:
        for col in text_cols:
            if pat.lower() in col.lower():
                return col
    return None
