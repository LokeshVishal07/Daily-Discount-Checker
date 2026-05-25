"""
app.py — Daily Discount Checker
Performance-optimised: every expensive operation is cached or stored in session state.
Nothing re-runs on a filter click or tab switch.
"""
from __future__ import annotations
from datetime import date
import hashlib
import pandas as pd
import streamlit as st

from settings import (
    REGIONS, REGION_MARKETPLACES, REGION_COLORS, SEVERITY_HEX,
)
from zecom_loader import (
    get_sheet_names, load_zecom_sheet, build_article_lookup,
    guess_article_col, guess_rrp_col, guess_srp_col,
    guess_remarks_col,
)
from content_loader  import load_content_file
from order_loader    import load_order_file
from discount_engine import (
    run_pipeline, apply_flags_with_open_pct,
    summary_by_marketplace, flagged_orders,
)
from exporter import build_report


# ── Helpers ───────────────────────────────────────────────────────────────────
def _idx(lst, val):
    try:
        return lst.index(val) if val and val in lst else 0
    except ValueError:
        return 0


def _file_hash(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()[:12]


# ── Cached loaders — only re-run when the file itself changes ─────────────────
@st.cache_data(show_spinner=False)
def _cached_content(file_hash: str, file_bytes: bytes):
    return load_content_file(file_bytes)


@st.cache_data(show_spinner=False)
def _cached_zecom_sheet(file_hash: str, region: str, file_bytes: bytes):
    return load_zecom_sheet(file_bytes, region)


@st.cache_data(show_spinner=False)
def _cached_order_file(file_hash: str, marketplace: str, region: str, file_bytes: bytes):
    return load_order_file(file_bytes, marketplace, region)


def _build_lookup_direct(df: pd.DataFrame, art_col: str, rrp_col: str,
                          srp_col: str, rmk_col: str):
    """Build lookup directly from df — no JSON round-trip, no Streamlit cache issues."""
    return build_article_lookup(
        df, art_col, rrp_col,
        None if srp_col == "(same as RRP)" else srp_col,
        rmk_col, None
    )


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
  div[data-testid="stExpander"]{border:1px solid #e0e0e0;border-radius:8px;}
</style>
""", unsafe_allow_html=True)

# ── Session state ─────────────────────────────────────────────────────────────
_defaults = {
    "content_df":    None,
    "zecom_data":    {},
    "zecom_bytes":   {},          # {region: bytes} for lookup rebuild
    "zecom_hash":    {},          # {region: hash}
    "mp_lookups":    {},
    "orders_df":     pd.DataFrame(),
    "result_df":     pd.DataFrame(),
    "open_pct_map":  {},
}
for k, v in _defaults.items():
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
    st.caption("EAN → Article Number mapping")
    c_file = st.file_uploader(
        "Content file", type=["xlsx","xls"],
        key="c_up", label_visibility="collapsed",
    )
    if c_file:
        c_bytes = c_file.read()
        c_hash  = _file_hash(c_bytes)
        if st.session_state.get("_content_hash") != c_hash:
            with st.spinner("Loading content file…"):
                cdf, cerr = _cached_content(c_hash, c_bytes)
            if cerr:
                st.error(cerr)
            else:
                st.session_state["content_df"]    = cdf
                st.session_state["_content_hash"] = c_hash
                st.success(f"✅ {len(cdf):,} EANs")
        else:
            cdf = st.session_state["content_df"]
            st.success(f"✅ {len(cdf):,} EANs (cached)")

    # ── ZeCom files ───────────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 📊 ZeCom Tracker(s)")
    z_files = st.file_uploader(
        "ZeCom file(s)", type=["xlsx","xls"],
        accept_multiple_files=True,
        key="z_up", label_visibility="collapsed",
    )

    if z_files:
        for zf in z_files:
            zf_bytes = zf.read()
            zf_hash  = _file_hash(zf_bytes)
            sheets   = get_sheet_names(zf_bytes)
            for region in active_regions:
                if region not in sheets:
                    continue
                if st.session_state["zecom_hash"].get(region) != zf_hash:
                    with st.spinner(f"Loading ZeCom {region}…"):
                        df_tab, num_cols, txt_cols, all_cols, err = _cached_zecom_sheet(
                            zf_hash, region, zf_bytes
                        )
                    if err:
                        st.error(f"ZeCom {region}: {err}")
                        continue
                    st.session_state["zecom_data"][region]  = {
                        "df": df_tab, "num_cols": num_cols,
                        "txt_cols": txt_cols, "all_cols": all_cols,
                    }
                    st.session_state["zecom_bytes"][region] = zf_bytes
                    st.session_state["zecom_hash"][region]  = zf_hash
                    st.success(f"✅ ZeCom {region} — {len(df_tab):,} rows")
                else:
                    st.success(f"✅ ZeCom {region} (cached)")

    # ── Per-marketplace column mapping ────────────────────────────────────────
    if st.session_state["zecom_data"]:
        st.divider()
        st.markdown("### 🗂️ Column Mapping")
        st.caption("Collapsed by default. Changes auto-rebuild the lookup.")

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
                        "Article / Style# column", options=ac,
                        index=_idx(ac, guess_article_col(ac)),
                        key=f"art_{mp_key}",
                    )
                    rrp_col = st.selectbox(
                        "RRP column", options=nc,
                        index=_idx(nc, guess_rrp_col(nc, region)),
                        key=f"rrp_{mp_key}",
                    )
                    srp_options = ["(same as RRP)"] + nc
                    srp_col = st.selectbox(
                        "SRP / MD Price column", options=srp_options,
                        index=_idx(srp_options, guess_srp_col(nc, region)),
                        key=f"srp_{mp_key}",
                    )
                    srp_col_val = None if srp_col == "(same as RRP)" else srp_col

                    rmk_col = st.selectbox(
                        "Exclusion / Remarks column", options=tc,
                        index=_idx(tc, guess_remarks_col(tc)),
                        key=f"rmk_{mp_key}",
                    )

                    apply_all = st.checkbox(
                        f"Apply to all {region} marketplaces",
                        key=f"apply_all_{mp_key}",
                    )

                    # Build lookup — store result in session_state keyed by
                    # file hash + column selections to avoid rebuilding on every render
                    zf_hash   = st.session_state["zecom_hash"].get(region, "")
                    lookup_key = f"{zf_hash}|{art_col}|{rrp_col}|{srp_col}|{rmk_col}"

                    if st.session_state.get(f"_lookup_key_{region}_{mp}") == lookup_key:
                        # Same file + same columns → reuse stored lookup
                        lookup = st.session_state["mp_lookups"].get((region, mp))
                        lerr   = "" if lookup is not None else "No cached lookup"
                    else:
                        # Columns changed or first run — rebuild lookup directly (no JSON)
                        try:
                            lookup, lerr = _build_lookup_direct(
                                zdata["df"], art_col, rrp_col, srp_col, rmk_col
                            )
                            if lookup is not None:
                                st.session_state[f"_lookup_key_{region}_{mp}"] = lookup_key
                        except Exception as e:
                            lookup, lerr = None, str(e)

                    if lerr:
                        st.error(f"Lookup error: {lerr}")
                    else:
                        st.session_state["mp_lookups"][(region, mp)] = lookup
                        n_rrp = lookup["RRP"].notna().sum()
                        n_rmk = (lookup["remark"] != "").sum()
                        st.caption(
                            f"{len(lookup):,} articles · {n_rrp:,} with RRP · "
                            f"{n_rmk:,} with remarks"
                        )

                        if apply_all:
                            other_mps = [m for m in marketplaces if m != mp]
                            for om in other_mps:
                                st.session_state["mp_lookups"][(region, om)] = lookup
                            if other_mps:
                                st.success(f"✅ Applied to {', '.join(other_mps)}")

    # ── OPEN remark max % ─────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🎟️ OPEN Remark — Max Allowed Voucher %")
    st.caption("Tolerance: <5% overshoot = OK · 5–10% = check · >10% = flagged red")
    open_pct_map = {}
    for region in active_regions:
        mps = REGION_MARKETPLACES.get(region, [])
        if mps:
            st.markdown(f"**{region}**")
            cols = st.columns(len(mps))
            for col_w, mp in zip(cols, mps):
                with col_w:
                    open_pct_map[(region, mp)] = st.number_input(
                        mp, min_value=0.0, max_value=100.0,
                        value=50.0, step=5.0,
                        key=f"open_pct_{region}_{mp}",
                    )
    st.session_state["open_pct_map"] = open_pct_map


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="title">🔍 Daily Discount Checker</div>', unsafe_allow_html=True)
region_labels = " · ".join(
    f"**{r}**" + (f" ({pic_names.get(r)})" if pic_names.get(r) else "")
    for r in active_regions
)
st.caption(f"Regions: {region_labels}  ·  {date.today().strftime('%d %b %Y')}")

s1, s2, s3 = st.columns(3)
content_ok = st.session_state["content_df"] is not None
loaded_mps = [f"{r}/{m}" for (r, m) in st.session_state["mp_lookups"].keys()
              if r in active_regions]
with s1:
    n = len(st.session_state["content_df"]) if content_ok else 0
    st.metric("Content File", f"✅ {n:,} EANs" if content_ok else "❌ Not uploaded")
with s2:
    st.metric("ZeCom Mapped", f"✅ {len(loaded_mps)} marketplace(s)" if loaded_mps else "❌ None")
with s3:
    st.metric("Orders", f"{len(st.session_state['orders_df']):,} rows")

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
        mps     = REGION_MARKETPLACES.get(region, [])
        mp_cols = st.columns(len(mps))
        for col_ui, mp in zip(mp_cols, mps):
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
                        uf_bytes = uf.read()
                        uf_hash  = _file_hash(uf_bytes)
                        df_ord, err = _cached_order_file(uf_hash, mp, region, uf_bytes)
                        if err:
                            st.error(f"❌ {uf.name}: {err}")
                        else:
                            df_ord = df_ord.copy()
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
mp_lookups = st.session_state["mp_lookups"]

can_run = not orders_df.empty and content_df is not None and len(mp_lookups) > 0
if not can_run:
    missing = []
    if orders_df.empty:    missing.append("order files")
    if content_df is None: missing.append("Content file")
    if not mp_lookups:     missing.append("ZeCom + column mapping")
    st.info(f"⬆️  Still waiting for: **{', '.join(missing)}**")
    st.stop()

st.divider()
if st.button("▶️  Run Discount Check", type="primary"):
    prog = st.progress(0, text="Starting…")
    frames = []
    groups = list(orders_df.groupby(["region","marketplace"]))
    for i, ((region, mp), grp_orders) in enumerate(groups):
        prog.progress((i) / max(len(groups),1), text=f"Processing {mp} {region}…")
        lookup = mp_lookups.get((region, mp))
        if lookup is None:
            lookup = next((v for (r,m),v in mp_lookups.items() if r==region), None)
        if lookup is None or grp_orders.empty:
            continue
        grp_result = run_pipeline(grp_orders, content_df, lookup)
        frames.append(grp_result)

    prog.progress(0.9, text="Applying discount rules…")
    if frames:
        result = pd.concat(frames, ignore_index=True)
        result = apply_flags_with_open_pct(result, st.session_state["open_pct_map"])
    else:
        result = pd.DataFrame()
    st.session_state["result_df"] = result
    prog.progress(1.0, text="Done!")
    prog.empty()
    st.success(f"✅ {len(result):,} orders processed.")

result_df = st.session_state.get("result_df", pd.DataFrame())
if result_df.empty:
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# FILTERS  — applied to already-computed result_df, fast
# ─────────────────────────────────────────────────────────────────────────────
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
    all_sevs = ["red","orange","amber","green","grey"]
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
# DASHBOARD TABS
# ─────────────────────────────────────────────────────────────────────────────
tab_excl, tab_flag, tab_mp, tab_all = st.tabs([
    "📋 Exclusion Rule Dashboard",
    "🚨 Flagged Orders",
    "🏪 Marketplace Summary",
    "🔎 Full Order Explorer",
])

# Pre-compute expensive aggregations ONCE, share across tabs
active_view = view[
    ~view.get("order_status", pd.Series("")).astype(str)
    .str.lower().str.contains("cancel", na=False)
].copy()

DISPLAY_COLS = ["Exclusion Remark","Rule","Orders","Sum of RRP",
                "SUM OF PAID PRICE","Disc % (Paid vs RRP)","Violations","Status"]
TABLE_FMT    = {"Sum of RRP":"{:,.2f}","SUM OF PAID PRICE":"{:,.2f}","Disc % (Paid vs RRP)":"{:.1f}%"}
SEV_BG       = {"red":"#ffe5e5","orange":"#fff3e0","amber":"#fffde7","green":"#e8f5e9","grey":"#f5f5f5"}


def _build_excl_table(df):
    rows = []
    grp_cols = [c for c in ["remark","rule_label","flag_severity"] if c in df.columns]
    if not grp_cols or df.empty:
        return pd.DataFrame()
    for keys, grp in df.groupby(grp_cols, dropna=False, observed=True):
        if not isinstance(keys, tuple): keys = (keys,)
        remark   = str(keys[0]) if len(keys)>0 else ""
        rule     = str(keys[1]) if len(keys)>1 else ""
        severity = str(keys[2]) if len(keys)>2 else "grey"
        sum_rrp  = grp["rrp_used"].sum()
        sum_paid = grp["paid_price"].sum()
        orders   = len(grp)
        flagged  = int(grp["flagged"].sum())
        amber_n  = int((grp["flag_severity"]=="amber").sum()) if "flag_severity" in grp.columns else 0
        disc_pct = round((sum_rrp - sum_paid)/sum_rrp*100, 1) if sum_rrp>0 else 0.0
        status   = "🚨 Violated" if flagged>0 else ("⚠️ Check" if amber_n>0 else "✅ OK")
        rows.append({
            "Exclusion Remark":   remark if remark not in ("","nan") else "(no remark)",
            "Rule":               rule,
            "Orders":             orders,
            "Sum of RRP":         round(sum_rrp,2),
            "SUM OF PAID PRICE":  round(sum_paid,2),
            "Disc % (Paid vs RRP)": disc_pct,
            "Violations":         flagged,
            "Status":             status,
            "_severity":          severity,
            "_flagged":           flagged,
        })
    if not rows:
        return pd.DataFrame()
    return (pd.DataFrame(rows)
            .sort_values(["_flagged","Disc % (Paid vs RRP)"],ascending=[False,False])
            .reset_index(drop=True))


def _show_excl_table(tbl):
    if tbl.empty:
        st.info("No data.")
        return
    display  = tbl[DISPLAY_COLS].copy()
    sev_map  = dict(zip(tbl.index, tbl["_severity"]))
    for col, fmt in TABLE_FMT.items():
        if col in display.columns:
            display[col] = display[col].apply(
                lambda v: fmt.format(v) if v is not None and str(v) not in ("","nan") else "—"
            )
    st.dataframe(display, hide_index=True)


# ════════ TAB 1 — Exclusion Rule Dashboard ═══════════════════════════════════
with tab_excl:
    st.markdown("### 📋 Exclusion Rule Dashboard")
    st.caption("Disc % = (Sum RRP − Sum Paid) / Sum RRP × 100 · Cancelled orders excluded")

    st.markdown("#### All Marketplaces Combined")
    combined_tbl = _build_excl_table(active_view)
    _show_excl_table(combined_tbl)

    st.markdown("#### By Marketplace")
    mp_list = sorted(active_view["marketplace"].unique().tolist()) if not active_view.empty else []
    if mp_list:
        mp_tabs = st.tabs(mp_list)
        for mptab, mp in zip(mp_tabs, mp_list):
            with mptab:
                mp_tbl = _build_excl_table(active_view[active_view["marketplace"]==mp])
                _show_excl_table(mp_tbl)
                if not mp_tbl.empty and len(mp_tbl)>1:
                    try:
                        import plotly.express as px
                        fig = px.bar(mp_tbl, x="Disc % (Paid vs RRP)", y="Exclusion Remark",
                                     orientation="h", color="_severity",
                                     color_discrete_map=SEVERITY_HEX,
                                     title=f"{mp} — Disc % by Exclusion Remark",
                                     template="plotly_white")
                        fig.update_layout(showlegend=False,
                                          height=max(260, len(mp_tbl)*48),
                                          margin=dict(l=0,r=20,t=35,b=0))
                        st.plotly_chart(fig)
                    except Exception:
                        pass

# ════════ TAB 2 — Flagged Orders ═════════════════════════════════════════════
with tab_flag:
    flagged_view = view[view["flagged"]==True]
    st.markdown(f"### 🚨 Flagged Orders ({len(flagged_view):,})")
    if flagged_view.empty:
        st.success("✅ No orders flagged today.")
    else:
        flag_cols = [c for c in [
            "region","marketplace","order_id","sku","Article Number","product_name",
            "order_status","rrp_used","srp_used",
            "seller_srp_disc_pct","seller_vc_disc_pct","seller_end_disc_pct",
            "paid_price","actual_total_disc_pct",
            "remark","rule_label","flag_reason","flag_severity",
        ] if c in flagged_view.columns]
        disp_flag = flagged_view[flag_cols].rename(columns={
            "seller_srp_disc_pct":  "Seller SRP Disc %",
            "seller_vc_disc_pct":   "Seller VC Disc % (Remark)",
            "seller_end_disc_pct":  "Seller END Disc %",
            "actual_total_disc_pct":"Cust Disc % from RRP",
            "rule_label":           "Rule",
        }).copy()
        for col, fmt in [("Seller SRP Disc %","{:.1f}%"),
                          ("Seller VC Disc % (Remark)","{:.1f}%"),
                          ("Seller END Disc %","{:.1f}%"),
                          ("Cust Disc % from RRP","{:.1f}%"),
                          ("rrp_used","{:.2f}"),("srp_used","{:.2f}"),
                          ("paid_price","{:.2f}")]:
            if col in disp_flag.columns:
                disp_flag[col] = disp_flag[col].apply(
                    lambda v: fmt.format(v) if v is not None and str(v) not in ("nan","") else "—"
                )
        st.dataframe(disp_flag, hide_index=True, height=420)

# ════════ TAB 3 — Marketplace Summary ════════════════════════════════════════
with tab_mp:
    st.markdown("### 🏪 Marketplace Summary")
    summary = summary_by_marketplace(view)
    st.dataframe(summary.style.format({
        "Avg_RRP":"{:.2f}","Sum_RRP":"{:,.2f}","Sum_Paid":"{:,.2f}",
        "Avg_Actual_Disc":"{:.1f}%","Avg_Auth_Disc":"{:.1f}%","Avg_Overshoot":"{:.1f}%",
    }, na_rep="—"), hide_index=True)
    try:
        import plotly.express as px
        fig = px.bar(summary, x="marketplace", y="Avg_Actual_Disc",
                     color="region", barmode="group",
                     title="Avg Customer Disc % by Marketplace & Region",
                     color_discrete_map=REGION_COLORS, template="plotly_white")
        st.plotly_chart(fig)
    except Exception:
        pass

# ════════ TAB 4 — Full Order Explorer ════════════════════════════════════════
with tab_all:
    st.markdown("### 🔎 Full Order Explorer")
    search = st.text_input("Search EAN / Article # / Order ID / Product", "")
    disp   = view.copy()
    if search:
        mask = pd.Series(False, index=disp.index)
        for col in ["sku","order_id","Article Number","product_name"]:
            if col in disp.columns:
                mask |= disp[col].astype(str).str.contains(search, case=False, na=False)
        disp = disp[mask]
    show = [c for c in [
        "region","marketplace","order_id","sku","Article Number","product_name",
        "order_status","rrp_used","srp_used",
        "seller_srp_disc_pct","seller_vc_disc_pct","seller_end_disc_pct",
        "paid_price","actual_total_disc_pct",
        "remark","rule_label","flagged","flag_severity","flag_reason",
    ] if c in disp.columns]
    st.dataframe(disp[show].rename(columns={
        "seller_srp_disc_pct":  "Seller SRP Disc %",
        "seller_vc_disc_pct":   "Seller VC Disc % (Remark)",
        "seller_end_disc_pct":  "Seller END Disc %",
        "actual_total_disc_pct":"Cust Disc % from RRP",
        "rule_label":           "Rule",
    }), hide_index=True)

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
        "📥 Full Excel Report (3 sheets)",
        data=build_report(view),
        file_name=f"Discount_Check_{region_str}_{today_str}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
with d2:
    fv = view[view["flagged"]==True]
    if not fv.empty:
        st.download_button(
            "🚨 Flagged Orders CSV",
            data=fv.to_csv(index=False).encode("utf-8"),
            file_name=f"Flagged_{region_str}_{today_str}.csv",
            mime="text/csv",
        )

st.divider()
st.caption(
    "Daily Discount Checker · EXCLUDE = sell at SRP · "
    "MAX X% = capped · OPEN = no restriction · "
    "Seller disc excludes all MP-funded rebates"
)
