"""order_loader.py — Marketplace order file reader"""
from __future__ import annotations
import io, logging
import numpy as np
import pandas as pd
import openpyxl
from settings import MARKETPLACE_COLUMNS

logger = logging.getLogger(__name__)


def load_order_file(file_bytes: bytes, marketplace: str, region: str):
    cfg = MARKETPLACE_COLUMNS.get(marketplace)
    if not cfg:
        return None, f"Unknown marketplace: {marketplace}"
    try:
        if cfg.get("tiktok_skip_desc_row"):
            raw = _read_tiktok(file_bytes)
        else:
            raw = pd.read_excel(io.BytesIO(file_bytes))
        if raw is None or raw.empty:
            return None, "File is empty."

        # Auto-detect Shopee PH format by checking PH-specific column names
        # PH has "(PHP)" suffix on discount columns — MY/SG do not
        if marketplace == "Shopee":
            ph_indicators = [
                "Products' Price Paid by Buyer (PHP)",
                "Price Discount(from Seller)(PHP)",
                "Shopee Rebate(PHP)",
                "Seller Voucher(PHP)",
            ]
            is_ph = any(col in raw.columns for col in ph_indicators)
            if is_ph:
                cfg = MARKETPLACE_COLUMNS.get("Shopee_PH", cfg)

        normalised = _normalise(raw, marketplace, region, cfg)
        
        # Safety check: if paid_price is all null/zero for Shopee and PH region,
        # it means auto-detect failed — force Shopee_PH config and retry
        if (marketplace == "Shopee" and region == "PH" and
                normalised["paid_price"].isna().all()):
            cfg_ph = MARKETPLACE_COLUMNS.get("Shopee_PH", cfg)
            normalised = _normalise(raw, marketplace, region, cfg_ph)
        
        return normalised, ""
    except Exception as exc:
        logger.exception("Order load error %s", marketplace)
        return None, str(exc)


def _read_tiktok(file_bytes):
    wb   = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=False, data_only=True)
    ws   = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 3:
        return pd.DataFrame()
    headers = [str(v).strip() if v is not None else "" for v in rows[0]]
    return pd.DataFrame([list(r) for r in rows[2:]], columns=headers)


def _normalise(raw, marketplace, region, cfg):
    df   = raw.copy()
    df.columns = df.columns.str.strip()
    n    = len(df)
    sign = cfg.get("discount_sign", "positive")

    out = pd.DataFrame()
    out["marketplace"]   = [marketplace] * n
    out["region"]        = [region] * n
    out["order_id"]      = _col(df, cfg, "order_id",      "").astype(str).str.strip()
    out["sku"]           = _col(df, cfg, "sku",           "").astype(str).str.strip()
    out["product_name"]  = _col(df, cfg, "product_name",  "")
    out["order_status"]  = _col(df, cfg, "order_status",  "")
    out["order_date"]    = pd.to_datetime(_col(df, cfg, "order_date", pd.NaT), errors="coerce")
    out["original_price"]= pd.to_numeric(_col(df, cfg, "original_price"), errors="coerce")
    out["paid_price"]    = pd.to_numeric(_col(df, cfg, "paid_price"),      errors="coerce")
    out["quantity"]      = pd.to_numeric(_col(df, cfg, "quantity", 1),     errors="coerce").fillna(1)

    s_disc = _sum_cols(df, cfg.get("seller_discount_cols",  []))
    p_disc = _sum_cols(df, cfg.get("platform_discount_cols",[]))
    if sign == "negative":
        s_disc, p_disc = s_disc.abs(), p_disc.abs()

    if cfg.get("derive_seller_disc"):
        unit  = pd.to_numeric(df.get("Unit Price",  pd.Series(dtype=float)), errors="coerce")
        paid  = pd.to_numeric(df.get("Paid Price",  pd.Series(dtype=float)), errors="coerce")
        s_disc = (unit - paid).clip(lower=0)
        out["original_price"] = unit

    if cfg.get("tiktok_skip_desc_row"):
        out["paid_price"] = out["paid_price"] / out["quantity"].replace(0, 1)

    out["seller_discount_amount"]   = s_disc.values
    out["platform_discount_amount"] = p_disc.values
    return out.reset_index(drop=True)


def _col(df, cfg, key, default=np.nan):
    name = cfg.get(key)
    return df[name] if name and name in df.columns else pd.Series([default] * len(df), index=df.index)


def _sum_cols(df, cols):
    total = pd.Series(0.0, index=df.index)
    for c in cols:
        if c in df.columns:
            total += pd.to_numeric(df[c], errors="coerce").fillna(0)
    return total
