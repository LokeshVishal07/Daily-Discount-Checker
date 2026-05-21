"""
app.py — Daily Discount Checker
All files flat in one folder — no subfolders required.
Run: streamlit run app.py
"""
from __future__ import annotations
from datetime import date
import pandas as pd
import streamlit as st

from settings import (
    REGIONS, REGION_MARKETPLACES, MARKETPLACE_COLORS, REGION_COLORS, SEVERITY_HEX,
)
from zecom_loader import (
    get_sheet_names, load_zecom_sheet, build_article_lookup,
    guess_article_col, guess_rrp_col, guess_srp_col,
    guess_remarks_col, guess_platform_vc_col,
)
from content_loader  import load_content_file
from order_loader    import load_order_file
from discount_engine import (
    run_pipeline, apply_flags_with_open_pct,
    summary_by_marketplace, exclusion_summary, flagged_orders,
)
from exporter import build_report


# ── Helper — defined at top so it's available everywhere ─────────────────────
def _idx(lst, val):
    try:
        return lst.index(val) if val and val in lst else 0
    except ValueError:
        return 0


def _row_bg(row):
    """Return proper CSS background-color strings for pandas Styler.apply(axis=1)."""
    colours = {
        "red":    "background-color: #ffe5e5",
        "orange": "background-color: #fff3e0",
        "amber":  "background-color: #fffde7",
        "green":  "background-color: #e8f5e9",
        "grey":   "background-color: #f5f5f5",
    }
    sev  = row.get("flag_severity", "") if hasattr(row, "get") else ""
    fill = colours.get(str(sev), "")
    return [fill] * len(row)


def _sev_cell(val):
    """Return CSS for a single severity cell (used with map())."""
    colours = {
        "red":    "background-color: #FF4B4B; color: white; font-weight: bold",
        "orange": "background-color: #FF8C00; color: white; font-weight: bold",
        "amber":  "background-color: #FFC300; color: white; font-weight: bold",
        "green":  "background-color: #2ECC71; color: white; font-weight: bold",
        "grey":   "background-color: #95A5A6; color: white; font-weight: bold",
    }
    return colours.get(str(val), "")


# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Daily Discount Checker",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .title{font-size:1.9rem;font-weight:800;
    background:linear-gradient(90deg,#1a73e8,#9b27af);
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
  .sec{font-size:1rem;font-weight:700;color:#1a73e8;
    border-bottom:2px solid #e8f0fe;padding-bottom:3px;margin:14px 0 8px 0;}
  .kpi{background:#f8f9fa;border-radius:10px;padding:.9rem 1.1rem;
    border-left:4px solid #1a73e8;margin-bottom:4px;}
  .kpi-v{font-size:1.6rem;font-weight:800;color:#1a1a1a;}
  .kpi-l{font-size:.74rem;color:#666;text-transform:uppercase;letter-spacing:.05em;}
  .red-kpi{border-left-color:#ff4b4b!important;background:#fff5f5!important;}
  .green-kpi{border-left-color:#2ecc71!important;background:#f0fff4!important;}
  div[data-testid="stExpander"]{border:1px solid #e0e0e0;border-radius:8px;}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
for k, v in {"content_df": None, "zecom_data": {}, "orders_df": pd.DataFrame(),
              "result_df": pd.DataFrame()}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("## 🔍 Daily Discount Checker")
    st.caption(f"📅 {date.today().strftime('%d %b %Y')}")

    # ── Region + PIC ──────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🌏 Your Region(s)")
    st.caption("Select the region(s) you are handling today.")
    active_regions = st.multiselect(
        "Regions", options=REGIONS, default=["MY"],
        label_visibility="collapsed",
    )
    if not active_regions:
        st.warning("Select at least one region.")
        st.stop()

    pic_names = {}
    for r in active_regions:
        pic_names[r] = st.text_input(
            f"{r} — PIC name", placeholder="e.g. Ahmad",
            key=f"pic_{r}",
        )

    # ── Content file ──────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📄 Content File")
    st.caption("EAN → Article Number mapping (upload once per session)")
    c_file = st.file_uploader(
        "Content file", type=["xlsx","xls"],
        key="c_up", label_visibility="collapsed",
    )
    if c_file:
        cdf, cerr = load_content_file(c_file.read())
        if cerr:
            st.error(cerr)
        else:
            st.session_state["content_df"] = cdf
            st.success(f"✅ {len(cdf):,} EANs loaded")

    # ── ZeCom files ───────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📊 ZeCom Tracker(s)")
    st.caption("Auto-detects header row — works after every bi-weekly update.")
    z_files = st.file_uploader(
        "ZeCom file(s)", type=["xlsx","xls"],
        accept_multiple_files=True,
        key="z_up", label_visibility="collapsed",
    )

    if z_files:
        for zf in z_files:
            zf_bytes = zf.read()
            sheets   = get_sheet_names(zf_bytes)
            for region in active_regions:
                if region not in sheets:
                    continue
                df_tab, num_cols, txt_cols, all_cols, err = load_zecom_sheet(zf_bytes, region)
                if err:
                    st.error(f"ZeCom {region}: {err}")
                    continue
                st.session_state["zecom_data"][region] = {
                    "df": df_tab, "num_cols": num_cols,
                    "txt_cols": txt_cols, "all_cols": all_cols,
                }
                st.success(f"✅ ZeCom {region} — {len(df_tab):,} rows")

    # ── Per-marketplace column mapping ──────────────────────────────────────────
    if st.session_state["zecom_data"]:
        st.divider()
        st.markdown("### 🗂️ Column Mapping")
        st.caption(
            "Select RRP, SRP and Remarks columns per marketplace. "
            "Collapsed by default — expand to change."
        )

        if "mp_lookups" not in st.session_state:
            st.session_state["mp_lookups"] = {}

        for region in active_regions:
            zdata = st.session_state["zecom_data"].get(region)
            if not zdata:
                continue
            nc, tc, ac = zdata["num_cols"], zdata["txt_cols"], zdata["all_cols"]
            marketplaces = REGION_MARKETPLACES.get(region, [])

            st.markdown(f"**{region}**")

            for mp in marketplaces:
                mp_key = f"{region}_{mp}"
                with st.expander(f"{mp}", expanded=False):

                    art_col = st.selectbox(
                        "Article / Style# column",
                        options=ac,
                        index=_idx(ac, guess_article_col(ac)),
                        key=f"art_{mp_key}",
                        help="Column holding the article or style number",
                    )

                    rrp_col = st.selectbox(
                        "RRP column",
                        options=nc,
                        index=_idx(nc, guess_rrp_col(nc, region)),
                        key=f"rrp_{mp_key}",
                        help="Retail Recommended Price — base for all discount calculations",
                    )

                    srp_options = ["(same as RRP)"] + nc
                    srp_col = st.selectbox(
                        "SRP / MD Price column",
                        options=srp_options,
                        index=_idx(srp_options, guess_srp_col(nc, region)),
                        key=f"srp_{mp_key}",
                        help="SRP or markdown price. Used as base for VC% and EXCLUDE ceiling.",
                    )
                    srp_col = None if srp_col == "(same as RRP)" else srp_col

                    rmk_col = st.selectbox(
                        "Exclusion / Remarks column",
                        options=tc,
                        index=_idx(tc, guess_remarks_col(tc)),
                        key=f"rmk_{mp_key}",
                        help="Column with EXCLUDED / MAX 30% / 10% VC ONLY remarks",
                    )

                    # Build per-marketplace lookup
                    lookup, lerr = build_article_lookup(
                        zdata["df"], art_col, rrp_col, srp_col, rmk_col, None,
                    )
                    if lerr:
                        st.error(f"Lookup error: {lerr}")
                    else:
                        st.session_state["mp_lookups"][(region, mp)] = lookup
                        n_art = len(lookup)
                        n_rrp = lookup["RRP"].notna().sum()
                        n_rmk = (lookup["remark"] != "").sum()
                        st.caption(
                            f"{n_art:,} articles · {n_rrp:,} with RRP · "
                            f"{n_rmk:,} with remarks  |  "
                            f"RRP: **{rrp_col.split('[')[1].rstrip(']') if '[' in rrp_col else rrp_col}** · "
                            f"Remarks: **{rmk_col.split('[')[1].rstrip(']') if '[' in rmk_col else rmk_col}**"
                        )
                        with st.expander("📋 Unique remarks in this file"):
                            rc = lookup["remark"].value_counts().reset_index()
                            rc.columns = ["Remark", "Count"]
                            rc = rc[rc["Remark"] != ""]
                            st.dataframe(rc, hide_index=True)

    # ── OPEN remark — Max Voucher % per marketplace ───────────────────────────
    st.divider()
    st.markdown("### 🎟️ OPEN Remark — Max Allowed Voucher %")
    st.caption(
        "For OPEN remarks, enter the maximum seller voucher % allowed per marketplace. "
        "Tolerance: up to 5% overshoot = OK · 5–10% = check (amber) · >10% = flagged red."
    )
    open_pct_map = {}
    for region in active_regions:
        mps = REGION_MARKETPLACES.get(region, [])
        if mps:
            st.markdown(f"**{region}**")
            cols = st.columns(len(mps))
            for col_w, mp in zip(cols, mps):
                with col_w:
                    open_pct_map[(region, mp)] = st.number_input(
                        mp,
                        min_value=0.0, max_value=100.0,
                        value=50.0, step=5.0,
                        key=f"open_pct_{region}_{mp}",
                    )

# ─────────────────────────────────────────────────────────────────────────────
# MAIN AREA
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="title">🔍 Daily Discount Checker</div>', unsafe_allow_html=True)
region_labels = " · ".join(
    f"**{r}**" + (f" ({pic_names.get(r)})" if pic_names.get(r) else "")
    for r in active_regions
)
st.caption(f"Regions: {region_labels}  ·  {date.today().strftime('%d %b %Y')}")

# ── Status row ────────────────────────────────────────────────────────────────
s1, s2, s3 = st.columns(3)
content_ok = st.session_state["content_df"] is not None
mp_lookups_now = st.session_state.get("mp_lookups", {})
loaded_mps = [f"{r}/{m}" for (r, m) in mp_lookups_now.keys() if r in active_regions]
with s1:
    n = len(st.session_state["content_df"]) if content_ok else 0
    st.metric("Content File", f"✅ {n:,} EANs" if content_ok else "❌ Not uploaded")
with s2:
    st.metric("ZeCom Mapped", f"✅ {len(loaded_mps)} marketplace(s)" if loaded_mps else "❌ None")
with s3:
    st.metric("Orders Loaded", f"{len(st.session_state['orders_df']):,} rows")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# UPLOAD ORDER FILES
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="sec">📂 Upload Today\'s Order Files</div>', unsafe_allow_html=True)
collected: list[pd.DataFrame] = []
region_tabs = st.tabs(active_regions)

for tab, region in zip(region_tabs, active_regions):
    with tab:
        label = f"**{region}**"
        if pic_names.get(region):
            label += f"  —  PIC: {pic_names[region]}"
        st.markdown(label)
        marketplaces = REGION_MARKETPLACES.get(region, [])
        mp_cols      = st.columns(len(marketplaces))

        for col_ui, mp in zip(mp_cols, marketplaces):
            with col_ui:
                st.markdown(f"**{mp}**")
                ups = st.file_uploader(
                    f"{mp} {region}", type=["xlsx","xls"],
                    accept_multiple_files=True,
                    key=f"ord_{region}_{mp}",
                    label_visibility="collapsed",
                )
                if ups:
                    for uf in ups:
                        df_ord, err = load_order_file(uf.read(), mp, region)
                        if err:
                            st.error(f"❌ {uf.name}: {err}")
                        else:
                            df_ord["pic"] = pic_names.get(region, "")
                            collected.append(df_ord)
                            st.success(f"✅ {len(df_ord):,} rows")

if collected:
    st.session_state["orders_df"] = pd.concat(collected, ignore_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# RUN CALCULATION
# ─────────────────────────────────────────────────────────────────────────────
orders_df  = st.session_state["orders_df"]
content_df = st.session_state["content_df"]
mp_lookups = st.session_state.get("mp_lookups", {})

can_run = not orders_df.empty and content_df is not None and len(mp_lookups) > 0
if not can_run:
    missing = []
    if orders_df.empty:    missing.append("order files")
    if content_df is None: missing.append("Content file")
    if not mp_lookups:     missing.append("ZeCom + per-marketplace column mapping")
    st.info(f"⬆️  Still waiting for: **{', '.join(missing)}**")
    st.stop()

st.divider()
if st.button("▶️  Run Discount Check", type="primary"):
    with st.spinner("Mapping EANs → Articles → RRP/SRP → Applying rules…"):
        # Build one lookup per (region, marketplace), then run pipeline per group
        # and recombine — so each marketplace uses its own RRP/Remarks columns
        frames = []
        for (region, mp), grp_orders in orders_df.groupby(["region","marketplace"]):
            lookup = mp_lookups.get((region, mp))
            if lookup is None:
                # Fallback: try any lookup for this region
                fallback = next(
                    (v for (r, m), v in mp_lookups.items() if r == region), None
                )
                lookup = fallback
            if lookup is None or grp_orders.empty:
                # No lookup available — still process without RRP mapping
                grp_orders = grp_orders.copy()
                grp_orders["Article Number"] = None
                grp_orders["RRP"] = None
                grp_orders["SRP"] = None
                grp_orders["remark"] = ""
                frames.append(grp_orders)
                continue
            grp_result = run_pipeline(grp_orders, content_df, lookup)
            frames.append(grp_result)

        result = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        result = apply_flags_with_open_pct(result, open_pct_map)
        st.session_state["result_df"] = result
    st.success(f"✅ {len(result):,} orders processed.")

result_df = st.session_state.get("result_df", pd.DataFrame())
if result_df.empty:
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.markdown('<div class="sec">🔍 Filters</div>', unsafe_allow_html=True)
f1, f2, f3, f4 = st.columns([2, 2, 2, 1])
with f1:
    rf = st.multiselect("Region", result_df["region"].unique().tolist(),
                        default=result_df["region"].unique().tolist())
with f2:
    mf = st.multiselect("Marketplace", result_df["marketplace"].unique().tolist(),
                        default=result_df["marketplace"].unique().tolist())
with f3:
    all_sevs = ["red", "orange", "amber", "green", "grey"]
    sevs     = [s for s in all_sevs if s in result_df.get("flag_severity", pd.Series()).values]
    sf       = st.multiselect("Severity", sevs, default=sevs)
with f4:
    fo = st.checkbox("🚨 Flagged only")

view = result_df[result_df["region"].isin(rf) & result_df["marketplace"].isin(mf)]
if sf: view = view[view["flag_severity"].isin(sf)]
if fo: view = view[view["flagged"] == True]

if view.empty:
    st.warning("No data matches filters.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# KPIs
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="sec">📈 Key Metrics</div>', unsafe_allow_html=True)
total          = len(view)
flagged_count  = int(view["flagged"].sum())
flag_pct       = flagged_count / total * 100 if total else 0
rrp_match      = view["RRP"].notna().mean() * 100 if "RRP" in view.columns else 0
sum_rrp        = view["rrp_used"].sum()
sum_paid       = view["paid_price"].sum()
total_disc_pct = (sum_rrp - sum_paid) / sum_rrp * 100 if sum_rrp else 0

k1, k2, k3, k4, k5, k6 = st.columns(6)

def _kpi(col, lbl, val, fmt="{:.1f}%", cls=""):
    with col:
        st.markdown(
            f'<div class="kpi {cls}">'
            f'<div class="kpi-v">{fmt.format(val)}</div>'
            f'<div class="kpi-l">{lbl}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

_kpi(k1, "Total Orders",   total,           "{:,}")
_kpi(k2, "Sum RRP",        sum_rrp,          "{:,.0f}")
_kpi(k3, "Sum Paid",       sum_paid,         "{:,.0f}")
_kpi(k4, "Overall Disc %", total_disc_pct,   "{:.1f}%")
_kpi(k5, "🚨 Flagged",     flagged_count,
     "{:,}" + f" ({flag_pct:.0f}%)",
     cls="red-kpi" if flagged_count > 0 else "green-kpi")
_kpi(k6, "RRP Match Rate", rrp_match,        "{:.1f}%")

st.markdown("")

# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD TABS
# ─────────────────────────────────────────────────────────────────────────────
tab_excl, tab_flag, tab_mp, tab_all = st.tabs([
    "📋 Exclusion Rule Dashboard",
    "🚨 Flagged Orders",
    "🏪 Marketplace Summary",
    "🔎 Full Order Explorer",
])

# ════════════════════════════════════════════════════════════════════════
# TAB 1 — Exclusion Rule Dashboard
# ════════════════════════════════════════════════════════════════════════
with tab_excl:
    st.markdown("### 📋 Exclusion Rule Dashboard")
    st.caption(
        "Per exclusion remark — Orders · Sum of RRP (ZeCom) · "
        "Sum of Seller Discount (seller-funded only, no MP rebates/vouchers) · "
        "Seller Disc % = Sum Seller Disc / Sum RRP × 100. Cancelled orders excluded."
    )

    # ── Exclude cancelled orders ──────────────────────────────────────────────
    active_view = view[
        ~view.get("order_status", pd.Series("")).astype(str)
        .str.lower().str.contains("cancel", na=False)
    ].copy()

    st.divider()

    # ── Build table ───────────────────────────────────────────────────────────
    DISPLAY_COLS = [
        "Exclusion Remark", "Rule", "Orders",
        "Sum of RRP", "Sum of Seller Disc", "Seller Disc %",
        "Authorised Disc %", "Avg Actual Disc %", "Avg Overshoot %",
        "Violations", "Status",
    ]
    TABLE_FMT = {
        "Sum of RRP":          "{:,.2f}",
        "Sum of Seller Disc":  "{:,.2f}",
        "Seller Disc %":       "{:.1f}%",
        "Authorised Disc %":   "{:.1f}%",
        "Avg Actual Disc %":   "{:.1f}%",
        "Avg Overshoot %":     "{:.1f}%",
    }
    SEV_BG = {
        "red":    "background-color: #ffe5e5",
        "orange": "background-color: #fff3e0",
        "amber":  "background-color: #fffde7",
        "green":  "background-color: #e8f5e9",
        "grey":   "background-color: #f5f5f5",
    }

    def _build_excl_table(df):
        rows = []
        grp_cols = [c for c in ["remark","rule_label","flag_severity"] if c in df.columns]
        if not grp_cols:
            return pd.DataFrame()
        for keys, grp in df.groupby(grp_cols, dropna=False, observed=True):
            if not isinstance(keys, tuple): keys = (keys,)
            remark     = str(keys[0]) if len(keys)>0 else ""
            rule_label = str(keys[1]) if len(keys)>1 else ""
            severity   = str(keys[2]) if len(keys)>2 else "grey"
            sum_rrp      = grp["rrp_used"].sum()
            sum_sel_disc = grp["seller_discount_amount"].sum()
            orders       = len(grp)
            flagged      = int(grp["flagged"].sum())
            amber_count  = int((grp["flag_severity"]=="amber").sum()) if "flag_severity" in grp.columns else 0
            seller_pct   = round(sum_sel_disc/sum_rrp*100,1) if sum_rrp>0 else 0.0
            def _r(col):
                v = grp[col].mean() if col in grp.columns else None
                return round(float(v),1) if v is not None and not pd.isna(v) else None
            status = "🚨 Violated" if flagged>0 else ("⚠️ Check" if amber_count>0 else "✅ OK")
            rows.append({
                "Exclusion Remark":  remark if remark not in ("","nan") else "(no remark)",
                "Rule":              rule_label,
                "Orders":            orders,
                "Sum of RRP":        round(sum_rrp,2),
                "Sum of Seller Disc":round(sum_sel_disc,2),
                "Seller Disc %":     seller_pct,
                "Authorised Disc %": _r("authorised_disc_pct"),
                "Avg Actual Disc %": _r("actual_total_disc_pct"),
                "Avg Overshoot %":   _r("overshoot_pct"),
                "Violations":        flagged,
                "Status":            status,
                "_severity":         severity,
                "_flagged":          flagged,
            })
        if not rows:
            return pd.DataFrame()
        out = pd.DataFrame(rows)
        sc = "Avg Overshoot %" if "Avg Overshoot %" in out.columns else "Seller Disc %"
        return out.sort_values(["_flagged",sc],ascending=[False,False]).reset_index(drop=True)

    def _show_excl_table(tbl):
        if tbl.empty:
            st.info("No data.")
            return
        display  = tbl[DISPLAY_COLS].copy()
        sev_map  = dict(zip(tbl.index, tbl["_severity"]))
        def row_bg(row):
            return [SEV_BG.get(sev_map.get(row.name, ""), "")] * len(row)
        st.dataframe(
            display.style.apply(row_bg, axis=1).format(TABLE_FMT, na_rep="—"),
            hide_index=True,
        )

    # ── All marketplaces combined ─────────────────────────────────────────────
    st.markdown("#### All Marketplaces Combined")
    combined_tbl = _build_excl_table(active_view)
    _show_excl_table(combined_tbl)

    # ── Per-marketplace tabs ───────────────────────────────────────────────────
    st.markdown("#### By Marketplace")
    mp_list_all = sorted(active_view["marketplace"].unique().tolist()) if not active_view.empty else []
    if mp_list_all:
        mp_tabs_all = st.tabs(mp_list_all)
        for mptab, mp in zip(mp_tabs_all, mp_list_all):
            with mptab:
                mp_df  = active_view[active_view["marketplace"] == mp]
                mp_tbl = _build_excl_table(mp_df)
                _show_excl_table(mp_tbl)
                if not mp_tbl.empty and len(mp_tbl) > 1:
                    try:
                        import plotly.express as px
                        fig = px.bar(
                            mp_tbl,
                            x="Avg Overshoot %", y="Exclusion Remark",
                            orientation="h",
                            color="_severity",
                            color_discrete_map=SEVERITY_HEX,
                            labels={"Avg Overshoot %": "Avg Overshoot %", "Exclusion Remark": ""},
                            title=f"{mp} — Avg Overshoot % by Exclusion Remark",
                            template="plotly_white",
                        )
                        fig.update_layout(
                            showlegend=False,
                            height=max(260, len(mp_tbl) * 48),
                            margin=dict(l=0, r=20, t=35, b=0),
                        )
                        st.plotly_chart(fig)
                    except Exception:
                        pass

# ════════════════════════════════════════════════════════════════════════
# TAB 2 — Flagged Orders
# ════════════════════════════════════════════════════════════════════════
with tab_flag:
    flagged_view = view[view["flagged"] == True]
    st.markdown(f"### 🚨 Flagged Orders ({len(flagged_view):,})")
    st.caption("Orders where seller discount has violated the exclusion rule from ZeCom.")

    if flagged_view.empty:
        st.success("✅ No orders flagged today.")
    else:
        flag_cols = [c for c in [
            "region", "marketplace", "order_id", "sku", "Article Number",
            "product_name", "order_status", "rrp_used", "srp_used", "paid_price",
            "seller_disc_pct", "remark", "allowed_rule", "flag_reason", "flag_severity",
        ] if c in flagged_view.columns]

        st.dataframe(
            flagged_view[flag_cols].style
            .map(_sev_cell, subset=["flag_severity"])
            .format({
                "seller_disc_pct": "{:.1f}%",
                "rrp_used":        "{:.2f}",
                "srp_used":        "{:.2f}",
                "paid_price":      "{:.2f}",
            }, na_rep="—"),
            hide_index=True,
            height=420,
        )

# ════════════════════════════════════════════════════════════════════════
# TAB 3 — Marketplace Summary
# ════════════════════════════════════════════════════════════════════════
with tab_mp:
    st.markdown("### 🏪 Marketplace Summary")
    summary = summary_by_marketplace(view)
    st.dataframe(
        summary.style.format({
            "Avg_RRP":          "{:.2f}",
            "Sum_RRP":          "{:,.2f}",
            "Sum_Paid":         "{:,.2f}",
            "Avg_Customer_Disc":"{:.1f}%",
            "Avg_Seller_Disc":  "{:.1f}%",
            "Avg_Platform_Disc":"{:.1f}%",
        }, na_rep="—"),
        hide_index=True,
    )
    try:
        import plotly.express as px
        fig = px.bar(
            summary, x="marketplace", y="Avg_Seller_Disc",
            color="region", barmode="group",
            title="Avg Seller Discount % by Marketplace & Region",
            color_discrete_map=REGION_COLORS, template="plotly_white",
        )
        st.plotly_chart(fig)
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════
# TAB 4 — Full Order Explorer
# ════════════════════════════════════════════════════════════════════════
with tab_all:
    st.markdown("### 🔎 Full Order Explorer")
    search = st.text_input("Search EAN / Article # / Order ID / Product", "")
    disp   = view.copy()
    if search:
        mask = pd.Series(False, index=disp.index)
        for col in ["sku", "order_id", "Article Number", "product_name"]:
            if col in disp.columns:
                mask |= disp[col].astype(str).str.contains(search, case=False, na=False)
        disp = disp[mask]

    show = [c for c in [
        "region", "marketplace", "order_id", "sku", "Article Number",
        "product_name", "order_status", "rrp_used", "paid_price",
        "seller_discount_amount", "platform_discount_amount",
        "seller_disc_pct", "remark", "allowed_rule", "flagged",
        "flag_severity", "flag_reason",
    ] if c in disp.columns]
    st.dataframe(disp[show], hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────
st.divider()
st.markdown('<div class="sec">⬇️  Download Report</div>', unsafe_allow_html=True)
d1, d2     = st.columns(2)
region_str = "_".join(active_regions)
today_str  = date.today().strftime("%Y%m%d")

with d1:
    st.download_button(
        "📥 Full Excel Report (4 sheets)",
        data=build_report(view),
        file_name=f"Discount_Check_{region_str}_{today_str}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
with d2:
    fv = view[view["flagged"] == True]
    if not fv.empty:
        st.download_button(
            "🚨 Flagged Orders CSV",
            data=fv.to_csv(index=False).encode("utf-8"),
            file_name=f"Flagged_{region_str}_{today_str}.csv",
            mime="text/csv",
        )

st.divider()
st.caption(
    "Daily Discount Checker · Seller discount excludes all MP-funded rebates & vouchers · "
    "EXCLUDE = sell at SRP · MAX X% = capped · OPEN = no restriction"
)
