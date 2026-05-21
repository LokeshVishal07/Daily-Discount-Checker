"""utils/content_loader.py"""
from __future__ import annotations
import io, logging
from typing import Optional
import pandas as pd
import openpyxl

logger = logging.getLogger(__name__)


def load_content_file(file_bytes: bytes) -> tuple[pd.DataFrame | None, str]:
    try:
        wb  = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws  = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return None, "Content file is empty."

        raw_headers = [str(h).strip() if h else "" for h in rows[0]]

        # Build unique header names (PUMA file has two "Color_No" cols)
        headers, seen = [], {}
        for h in raw_headers:
            if h in seen:
                seen[h] += 1
                headers.append(f"{h}_{seen[h]}")
            else:
                seen[h] = 0
                headers.append(h)

        data = [list(r) for r in rows[1:]]
        df   = pd.DataFrame(data, columns=headers)

        # PUMA format: Color_No (col0) + EAN (col1)
        if headers[0].lower().startswith("color_no") or headers[0].lower().startswith("colour_no"):
            ean_col = headers[1]   # EAN
            art_col = headers[0]   # Color_No
        else:
            ean_col = _find(df, ["EAN", "ean", "GTIN", "barcode", "Barcode"])
            art_col = _find(df, ["Article Number", "article_number", "Color_No",
                                  "colour_no", "Style#", "StyleNo", "ArticleNo"])
            if not ean_col or not art_col:
                return None, f"Cannot find EAN/Article columns. Found: {df.columns.tolist()[:8]}"

        result = df[[ean_col, art_col]].copy()
        result.columns = ["EAN", "Article Number"]
        result["EAN"]            = result["EAN"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        result["Article Number"] = result["Article Number"].astype(str).str.strip()
        result = result[result["EAN"].str.len() >= 8].dropna()
        result = result.drop_duplicates("EAN").reset_index(drop=True)
        return result, ""
    except Exception as exc:
        logger.exception("Content load error")
        return None, str(exc)


def _find(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns: return c
    lower = {col.lower(): col for col in df.columns}
    for c in candidates:
        if c.lower() in lower: return lower[c.lower()]
    return None
