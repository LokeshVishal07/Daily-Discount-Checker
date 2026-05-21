"""
app.py — Daily Discount Checker
All files are in the same flat folder — no subfolders required.
Run: streamlit run app.py
"""
from __future__ import annotations
from datetime import date
import pandas as pd
import streamlit as st

# ── All imports are flat — no config. or utils. prefix ───────────────────────
from settings import (
    REGIONS, REGION_MARKETPLACES, MARKETPLACE_COLORS, REGION_COLORS, SEVERITY_HEX,
)
from zecom_loader import (
    get_sheet_names, load_zecom_sheet, build_article_lookup,
    guess_article_col, guess_rrp_col, guess_srp_col,
    guess_remarks_col, guess_platform_vc_col,
)
from content_loader import load_content_file
from order_loader   import load_order_file
from discount_engine import (
    run_pipeline, summary_by_marketplace, exclusion_summary, flagged_orders,
)
from exporter import build_report


# ── Helper — defined here so it's available everywhere in this file ───────────
def _idx(lst, val):
    try: return lst.index(val) if val and val in lst else 0
    except ValueError: return 0


# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Daily Discount Checker", page_icon="🔍",
                   layout="wide", initial_sidebar_state="expanded")

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
    c_file = st.file_uploader("Content file", type=["xlsx","xls"],
                               key="c_up", label_visibility="collapsed")
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
    z_files = st.file_uploader("ZeCom file(s)", type=["xlsx","xls"],
                                accept_multiple_files=True,
                                key="z_up", label_visibility="collapsed")

    if z_files:
        for zf in z_files:
            zf_bytes = zf.read()
            sheets   = get_sheet_names(zf_bytes)
            for region in active_regions:
                if region not in sheets:
                    continue
                df_tab, num_cols, txt_cols, all_cols, err = load_zecom_sheet(zf_bytes, region)
                if err:
                    st.error(f"ZeCom {region}: {err}"); continue
                st.session_state["zecom_data"][region] = {
                    "df": df_tab, "num_cols": num_cols,
                    "txt_cols": txt_cols, "all_cols": all_cols,
                }
                st.success(f"✅ ZeCom {region} — {len(df_tab):,} rows")

    # ── Per-region column mapping ─────────────────────────────────────────────
    if st.session_state["zecom_data"]:
        st.divider()
        st.markdown("### 🗂️ Column Mapping")
        st.caption("Auto-suggested each time. Change freely — no hardcoded columns.")

        for region in active_regions:
            zdata = st.session_state["zecom_data"].get(region)
            if not zdata:
                continue
            nc, tc, ac = zdata["num_cols"], zdata["txt_cols"], zdata["all_cols"]

            with st.expander(f"**{region}** column mapping", expanded=True):

                art_col = st.selectbox(
                    f"{region} — Article / Style# column", options=ac,
                    index=_idx(ac, guess_article_col(ac)), key=f"art_{region}",
                    help="Column with article/style numbers (e.g. Style#, PIM Article#)",
                )
                rrp_col = st.selectbox(
                    f"{region} — RRP column", options=nc,
                    index=_idx(nc, guess_rrp_col(nc, region)), key=f"rrp_{region}",
                    help="Retail Recommended Price column",
                )
                srp_col = st.selectbox(
                    f"{region} — SRP / MD Price column",
                    options=["(same as RRP)"] + nc,
                    index=_idx(["(same as RRP)"] + nc, guess_srp_col(nc, region)),
                    key=f"srp_{region}",
                    help="SRP or markdown price — ceiling for EXCLUDED products",
                )
                srp_col = None if srp_col == "(same as RRP)" else srp_col

                rmk_col = st.selectbox(
                    f"{region} — MP Remarks / Exclusion column", options=tc,
                    index=_idx(tc, guess_remarks_col(tc)), key=f"rmk_{region}",
                    help="Column with EXCLUDED FROM PROMOTION / MAX 30% / OPEN FOR ALL etc.",
                )
                vc_col = st.selectbox(
                    f"{region} — Platform VC column (optional)",
                    options=["(none)"] + tc,
                    index=_idx(["(none)"] + tc, guess_platform_vc_col(tc)),
                    key=f"vc_{region}",
                )
                vc_col = None if vc_col == "(none)" else vc_col

                lookup, lerr = build_article_lookup(
                    zdata["df"], art_col, rrp_col, srp_col, rmk_col, vc_col,
                )
                if lerr:
                    st.error(f"Lookup error: {lerr}")
                else:
                    zdata["lookup"] = lookup
                    st.caption(f"→ {len(lookup):,} articles · "
                               f"{lookup['RRP'].notna().sum():,} with RRP · "
                               f"{(lookup['remark']!='').sum():,} with remarks")
                    with st.expander("📋 Remark values in this ZeCom"):
                        rc = lookup["remark"].value_counts().reset_index()
                        rc.columns = ["Remark", "Count"]
                        st.dataframe(rc, hide_index=True, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="title">🔍 Daily Discount Checker</div>', unsafe_allow_html=True)
region_labels = " · ".join(
    f"**{r}**" + (f" ({pic_names.get(r)})" if pic_names.get(r) else "")
    for r in active_regions
)
st.caption(f"Regions: {region_labels}  ·  {date.today().strftime('%d %b %Y')}")

# Status
s1, s2, s3 = st.columns(3)
content_ok = st.session_state["content_df"] is not None
loaded     = [r for r in active_regions
              if "lookup" in st.session_state["zecom_data"].get(r, {})]
with s1:
    st.metric("Content File",
              f"✅ {len(st.session_state['content_df']):,} EANs" if content_ok else "❌ Not uploaded")
with s2:
    st.metric("ZeCom Loaded", f"✅ {', '.join(loaded)}" if loaded else "❌ None")
with s3:
    st.metric("Orders Loaded", f"{len(st.session_state['orders_df']):,} rows")

st.divider()

# ── Upload order files ────────────────────────────────────────────────────────
st.markdown('<div class="sec">📂 Upload Today\'s Order Files</div>', unsafe_allow_html=True)
collected: list[pd.DataFrame] = []
region_tabs = st.tabs(active_regions)

for tab, region in zip(region_tabs, active_regions):
    with tab:
        st.markdown(f"**{region}**" + (f"  —  PIC: {pic_names.get(region)}" if pic_names.get(region) else ""))
        marketplaces = REGION_MARKETPLACES.get(region, [])
        mp_cols      = st.columns(len(marketplaces))

        for col_ui, mp in zip(mp_cols, marketplaces):
            with col_ui:
                st.markdown(f"**{mp}**")
                ups = st.file_uploader(f"{mp} {region}", type=["xlsx","xls"],
                                       accept_multiple_files=True,
                                       key=f"ord_{region}_{mp}",
                                       label_visibility="collapsed")
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

# ── Run calculation ───────────────────────────────────────────────────────────
orders_df  = st.session_state["orders_df"]
content_df = st.session_state["content_df"]
zecom_data = st.session_state["zecom_data"]
lookups    = {r: d["lookup"] for r, d in zecom_data.items() if "lookup" in d}

can_run = not orders_df.empty and content_df is not None and len(lookups) > 0
if not can_run:
    missing = []
    if orders_df.empty:    missing.append("order files")
    if content_df is None: missing.append("Content file")
    if not lookups:        missing.append("ZeCom + column mapping")
    st.info(f"⬆️  Still waiting for: **{', '.join(missing)}**")
    st.stop()

st.divider()
if st.button("▶️  Run Discount Check", type="primary", use_container_width=True):
    with st.spinner("Mapping EANs → Articles → RRP/SRP → Applying rules…"):
        combined_lookup = (pd.concat(list(lookups.values()), ignore_index=True)
                           .drop_duplicates("Article Number"))
        result = run_pipeline(orders_df, content_df, combined_lookup)
        st.session_state["result_df"] = result
    st.success(f"✅ {len(result):,} orders processed.")

result_df = st.session_state.get("result_df", pd.DataFrame())
if result_df.empty:
    st.stop()

# ── Filters ───────────────────────────────────────────────────────────────────
st.divider()
st.markdown('<div class="sec">🔍 Filters</div>', unsafe_allow_html=True)
f1, f2, f3, f4 = st.columns([2,2,2,1])
with f1:
    rf = st.multiselect("Region", result_df["region"].unique().tolist(),
                        default=result_df["region"].unique().tolist())
with f2:
    mf = st.multiselect("Marketplace", result_df["marketplace"].unique().tolist(),
                        default=result_df["marketplace"].unique().tolist())
with f3:
    sevs = [s for s in ["red","orange","amber","green","grey"]
            if s in result_df.get("flag_severity", pd.Series()).values]
    sf   = st.multiselect("Severity", sevs, default=sevs)
with f4:
    fo = st.checkbox("🚨 Flagged only")

view = result_df[result_df["region"].isin(rf) & result_df["marketplace"].isin(mf)]
if sf: view = view[view["flag_severity"].isin(sf)]
if fo: view = view[view["flagged"] == True]

if view.empty:
    st.warning("No data matches filters.")
    st.stop()

# ── KPIs ──────────────────────────────────────────────────────────────────────
st.markdown('<div class="sec">📈 Key Metrics</div>', unsafe_allow_html=True)
total         = len(view)
flagged_count = int(view["flagged"].sum())
flag_pct      = flagged_count / total * 100 if total else 0
rrp_match     = view["RRP"].notna().mean() * 100 if "RRP" in view.columns else 0
sum_rrp       = view["rrp_used"].sum()
sum_paid      = view["paid_price"].sum()
total_disc_pct= (sum_rrp - sum_paid) / sum_rrp * 100 if sum_rrp else 0

k1,k2,k3,k4,k5,k6 = st.columns(6)
def _kpi(col, lbl, val, fmt="{:.1f}%", cls=""):
    with col:
        st.markdown(f'<div class="kpi {cls}"><div class="kpi-v">{fmt.format(val)}</div>'
                    f'<div class="kpi-l">{lbl}</div></div>', unsafe_allow_html=True)

_kpi(k1, "Total Orders",    total,           "{:,}")
_kpi(k2, "Sum RRP",         sum_rrp,          "{:,.0f}")
_kpi(k3, "Sum Paid",        sum_paid,         "{:,.0f}")
_kpi(k4, "Overall Disc %",  total_disc_pct,   "{:.1f}%")
_kpi(k5, "🚨 Flagged",      flagged_count,
     "{:,}" + f" ({flag_pct:.0f}%)",
     cls="red-kpi" if flagged_count > 0 else "green-kpi")
_kpi(k6, "RRP Match Rate",  rrp_match,        "{:.1f}%")

st.markdown("")

# ── Dashboard tabs ────────────────────────────────────────────────────────────
tab_excl, tab_flag, tab_mp, tab_all = st.tabs([
    "📋 Exclusion Rule Dashboard",
    "🚨 Flagged Orders",
    "🏪 Marketplace Summary",
    "🔎 Full Order Explorer",
])

# ════════ TAB 1 — Exclusion Rule Dashboard ════════════════════════════════════
with tab_excl:
    st.markdown("### Exclusion Rule Discount Dashboard")
    st.caption("Sum of RRP · Sum Paid · Total Discount Amount · Total Discount % — per exclusion rule across all marketplaces.")

    excl_df = exclusion_summary(view)

    def _row_colour(row):
        c = {"red":"#ffe5e5","orange":"#fff3e0","amber":"#fffde7","green":"#e8f5e9","grey":"#f5f5f5"}
        return [c.get(row["flag_severity"], "")] * len(row)

    # Overall summary
    st.markdown("#### All Regions Combined")
    overall = (excl_df.groupby(["allowed_rule","flag_severity"])
               .agg(Orders=("Orders","sum"), Flagged=("Flagged","sum"),
                    Sum_RRP=("Sum_RRP","sum"), Sum_Paid=("Sum_Paid","sum"),
                    Sum_Seller_Disc=("Sum_Seller_Disc","sum"),
                    Avg_Seller_Disc_Pct=("Avg_Seller_Disc_Pct","mean"),
                    Max_Seller_Disc_Pct=("Max_Seller_Disc_Pct","max"))
               .reset_index())
    overall["Total_Discount_Amt"] = (overall["Sum_RRP"] - overall["Sum_Paid"]).round(2)
    overall["Total_Discount_Pct"] = ((overall["Total_Discount_Amt"] / overall["Sum_RRP"]) * 100).round(2)
    overall = overall.sort_values("Total_Discount_Pct", ascending=False).round(2)

    st.dataframe(
        overall.style.apply(_row_colour, axis=1)
        .format({"Sum_RRP":"{:,.2f}","Sum_Paid":"{:,.2f}","Sum_Seller_Disc":"{:,.2f}",
                 "Total_Discount_Amt":"{:,.2f}","Total_Discount_Pct":"{:.1f}%",
                 "Avg_Seller_Disc_Pct":"{:.1f}%","Max_Seller_Disc_Pct":"{:.1f}%"}, na_rep="—"),
        use_container_width=True, hide_index=True,
    )

    # Per-region breakdown
    st.markdown("#### Per Region & Marketplace")
    for region in active_regions:
        region_excl = excl_df[excl_df["region"] == region]
        if region_excl.empty:
            continue
        pic = pic_names.get(region, "")
        st.markdown(f"**{region}**" + (f"  —  PIC: {pic}" if pic else ""))
        mp_list = region_excl["marketplace"].unique().tolist()
        mp_tabs = st.tabs(mp_list)

        for mptab, mp in zip(mp_tabs, mp_list):
            with mptab:
                mp_excl = region_excl[region_excl["marketplace"] == mp][[
                    "allowed_rule","flag_severity","Orders","Flagged",
                    "Sum_RRP","Sum_Paid","Sum_Seller_Disc",
                    "Total_Discount_Amt","Total_Discount_Pct",
                    "Avg_Seller_Disc_Pct","Max_Seller_Disc_Pct",
                ]].sort_values("Total_Discount_Pct", ascending=False)

                st.dataframe(
                    mp_excl.style.apply(_row_colour, axis=1)
                    .format({"Sum_RRP":"{:,.2f}","Sum_Paid":"{:,.2f}","Sum_Seller_Disc":"{:,.2f}",
                             "Total_Discount_Amt":"{:,.2f}","Total_Discount_Pct":"{:.1f}%",
                             "Avg_Seller_Disc_Pct":"{:.1f}%","Max_Seller_Disc_Pct":"{:.1f}%"}, na_rep="—"),
                    use_container_width=True, hide_index=True,
                )

                if len(mp_excl) > 1:
                    try:
                        import plotly.express as px
                        fig = px.bar(mp_excl, x="Total_Discount_Pct", y="allowed_rule",
                                     orientation="h", color="flag_severity",
                                     color_discrete_map=SEVERITY_HEX,
                                     labels={"Total_Discount_Pct":"Total Disc %","allowed_rule":"Rule"},
                                     title=f"{mp} — Total Discount % by Rule",
                                     template="plotly_white")
                        fig.update_layout(showlegend=False, height=260,
                                          margin=dict(l=0,r=0,t=30,b=0))
                        st.plotly_chart(fig, use_container_width=True)
                    except Exception:
                        pass

# ════════ TAB 2 — Flagged Orders ═════════════════════════════════════════════
with tab_flag:
    flagged_view = view[view["flagged"] == True]
    st.markdown(f"### 🚨 Flagged Orders ({len(flagged_view):,})")

    if flagged_view.empty:
        st.success("✅ No orders flagged today.")
    else:
        flag_cols = [c for c in [
            "region","marketplace","order_id","sku","Article Number","product_name",
            "rrp_used","srp_used","paid_price","seller_disc_pct","customer_disc_pct",
            "remark","allowed_rule","max_allowed_pct","flag_reason","flag_severity",
        ] if c in flagged_view.columns]

        def _sev(val):
            m = {"red":"#FF4B4B","orange":"#FF8C00","amber":"#FFC300",
                 "green":"#2ECC71","grey":"#95A5A6"}
            return f"background-color:{m.get(str(val),'#fff')};color:white;font-weight:bold;"

        st.dataframe(
            flagged_view[flag_cols].style.applymap(_sev, subset=["flag_severity"])
            .format({"seller_disc_pct":"{:.1f}%","customer_disc_pct":"{:.1f}%",
                     "rrp_used":"{:.2f}","srp_used":"{:.2f}","paid_price":"{:.2f}",
                     "max_allowed_pct":"{:.0f}%"}, na_rep="—"),
            use_container_width=True, hide_index=True, height=420,
        )

# ════════ TAB 3 — Marketplace Summary ════════════════════════════════════════
with tab_mp:
    st.markdown("### Marketplace Summary")
    summary = summary_by_marketplace(view)
    st.dataframe(
        summary.style.format({"Avg_RRP":"{:.2f}","Sum_RRP":"{:,.2f}","Sum_Paid":"{:,.2f}",
                              "Avg_Customer_Disc":"{:.1f}%","Avg_Seller_Disc":"{:.1f}%",
                              "Avg_Platform_Disc":"{:.1f}%"}, na_rep="—"),
        use_container_width=True, hide_index=True,
    )
    try:
        import plotly.express as px
        fig = px.bar(summary, x="marketplace", y="Avg_Seller_Disc", color="region",
                     barmode="group", title="Avg Seller Discount % by Marketplace & Region",
                     color_discrete_map=REGION_COLORS, template="plotly_white")
        st.plotly_chart(fig, use_container_width=True)
    except Exception:
        pass

# ════════ TAB 4 — Full Explorer ══════════════════════════════════════════════
with tab_all:
    st.markdown("### Full Order Explorer")
    search = st.text_input("Search EAN / Article # / Order ID / Product", "")
    disp   = view.copy()
    if search:
        mask = pd.Series(False, index=disp.index)
        for col in ["sku","order_id","Article Number","product_name"]:
            if col in disp.columns:
                mask |= disp[col].astype(str).str.contains(search, case=False, na=False)
        disp = disp[mask]
    show = [c for c in ["region","marketplace","order_id","sku","Article Number","product_name",
                         "order_status","rrp_used","srp_used","paid_price",
                         "customer_disc_pct","seller_disc_pct","platform_disc_pct",
                         "remark","allowed_rule","flagged","flag_severity","flag_reason"]
            if c in disp.columns]
    st.dataframe(disp[show], use_container_width=True, hide_index=True)

# ── Download ──────────────────────────────────────────────────────────────────
st.divider()
st.markdown('<div class="sec">⬇️  Download Report</div>', unsafe_allow_html=True)
d1, d2 = st.columns(2)
region_str = "_".join(active_regions)
today_str  = date.today().strftime("%Y%m%d")

with d1:
    st.download_button("📥 Full Excel Report (4 sheets)",
                       data=build_report(view),
                       file_name=f"Discount_Check_{region_str}_{today_str}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       use_container_width=True)
with d2:
    fv = view[view["flagged"]==True]
    if not fv.empty:
        st.download_button("🚨 Flagged Orders CSV",
                           data=fv.to_csv(index=False).encode("utf-8"),
                           file_name=f"Flagged_{region_str}_{today_str}.csv",
                           mime="text/csv", use_container_width=True)

st.divider()
st.caption("Daily Discount Checker · Seller discount excludes all MP-funded rebates & vouchers · "
           "EXCLUDE = sell at SRP · MAX X% = capped · OPEN = no restriction")
