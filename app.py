import streamlit as st
import pandas as pd
import importlib
import plotly.graph_objects as go
import battery_simulator

# Forceer hot-reloads van de backend rekenlogica
importlib.reload(battery_simulator)
from battery_simulator import simulate_battery_roi

# ==========================================
# 1. PAGE LAYOUT & DEEP BLACK CANVAS THEME
# ==========================================
st.set_page_config(page_title="Asset Payback Analytics", layout="wide")

st.markdown(
    """
    <style>
    .stApp, [data-testid="stSidebar"], div[data-testid="stSidebarCollapseButton"] {
        background-color: #0A0C10 !important;
    }
    h1, h2, h3, p, span, label, .stMarkdown, [data-testid="stMetricValue"] {
        color: #FFFFFF !important;
    }
    div.stButton > button:first-child {
        background-color: #ff7f0e !important;
        color: white !important;
        border: none !important;
    }
    code {
        background-color: #12161f !important;
        color: #00ff66 !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("🔋 Home Battery ROI Simulator")
st.markdown("Your local multi-year dataset has been mounted. Modify physical configurations below to inspect sensitivity matrix profiles.")

# Vaste lokale bestandspaden
solar_excel = "Solar panel power generation.xlsx"
fluvius_csvs = [
    "Verbruikshistoriek_elektriciteit_541448860004312189_20220912_20250902_kwartiertotalen-1.csv",
    "Verbruikshistoriek_elektriciteit_541448860004312189_20250908_20260713_kwartiertotalen-1.csv"
]

# ==========================================
# 2. CONFIGURATION MATRIX SLIDERS (SIDEBAR)
# ==========================================
st.sidebar.header("🔧 Structural Spec Parameters")
capacity = st.sidebar.slider("Net Battery Capacity (kWh)", min_value=0.0, max_value=20.0, value=9.0, step=0.5)
power = st.sidebar.slider("Max Inverter Power (kW)", min_value=2.0, max_value=12.0, value=7.5, step=0.5)

# Solar generation scale slider
solar_scale = st.sidebar.slider("Solar Panel Generation Scale (%)", min_value=50, max_value=250, value=100, step=5) / 100.0

formula_cost = 1850 * (capacity ** 0.62)
battery_cost = st.sidebar.number_input(
    f"Asset Acquisition Price (€) [Formula: €{formula_cost:.2f}]", 
    value=int(formula_cost), 
    step=100
)

st.sidebar.header("💶 Volumetric Tariff Rates")
price_offtake = st.sidebar.number_input("All-In Grid Purchase Fee (€/kWh)", value=0.39, step=0.01)
price_injection = st.sidebar.number_input("Surplus Compensation Rate (€/kWh)", value=0.04, step=0.01)

st.sidebar.header("⚡ System Efficiency Settings")
eff_charge = st.sidebar.slider("Charging Conversion (%)", min_value=80, max_value=100, value=96, step=1) / 100.0
eff_discharge = st.sidebar.slider("Discharging Conversion (%)", min_value=80, max_value=100, value=95, step=1) / 100.0

st.sidebar.header("📅 Historical Analytics Windows")
calc_start = st.sidebar.date_input("Calculation Horizon Start", value=pd.to_datetime('01-01-2023', dayfirst=True))
calc_end = st.sidebar.date_input("Calculation Horizon End", value=pd.to_datetime('31-12-2026', dayfirst=True))

# ==========================================
# 3. INTERACTIVE CALCULATION ENGINE TRACE
# ==========================================
if st.sidebar.button("🚀 Run System Analytics", use_container_width=True):
    with st.spinner("Processing asset configuration models over 15-minute intervals..."):
        try:
            # Baseline simulatie run
            base = simulate_battery_roi(
                solar_file=solar_excel, fluvius_files=fluvius_csvs,
                battery_capacity_kwh=capacity, max_power_kw=power,
                efficiency_charge=eff_charge, efficiency_discharge=eff_discharge,
                price_offtake=price_offtake, price_injection=price_injection,
                battery_cost=battery_cost,
                calc_start_date=calc_start.strftime('%d-%m-%Y'),
                calc_end_date=calc_end.strftime('%d-%m-%Y'),
                plot_start_date='01-06-2025', plot_end_date='01-07-2025',
                generate_graph=True,
                solar_scale_factor=solar_scale # Pass scaling factor to core function
            )
            
            # --- HOOFDRESULTATEN RENDER ---
            kpi1, kpi2 = st.columns(2)
            if base['payback_years'] == float('inf'):
                kpi1.metric("Asset Payback Horizon", "Infinite (No Savings)")
            else:
                kpi1.metric("Asset Payback Horizon", f"{base['payback_years']:.2f} Years")
            kpi2.metric("Total Invested Capital Base", f"€ {int(battery_cost):,}")
            
            st.subheader("Financial Performance Receipt Matrix")
            st.code(base["financial_receipt"], language="text")
            
            st.subheader("Granular 15-Minute Flow Profiles")
            st.plotly_chart(base["dashboard_figure"], use_container_width=True)
            
            # ==========================================
            # 4. SENSITIVITY ENGINE (OAT WITH CRASH PROTECTION)
            # ==========================================
            st.write("---")
            st.subheader("🔍 What happens if my assumptions change? (Sensitivity Analysis)")
            
            baseline_val = base['payback_years']
            
            # If payback is infinite (e.g. 0 kWh battery), skip OAT plot to prevent crashes
            if baseline_val == float('inf') or pd.isna(baseline_val):
                st.warning("⚠️ **Sensitivity Analysis is disabled** because the baseline payback period is infinite (e.g. when battery capacity is 0 kWh or there are no annual savings to evaluate). Increase battery capacity to view analysis.")
            else:
                st.markdown(
                    """
                    This chart maps how sensitive your financial return is to real-world changes. 
                    We simulated what happens if each key parameter fluctuates up or down by **20%**, completely independent of the others.
                    
                    **How to read this chart:**
                    * The **vertical center line** represents your current baseline payback period.
                    * **Longer bars** indicate that a parameter has a **huge impact** on your investment. If that variable shifts, your payback time changes drastically.
                    * **Shorter bars** mean your project is **stable**. Even if your assumption there is slightly wrong, your payback time remains mostly the same.
                    """
                )
                
                sens_vars = {
                    "Grid Purchase Price": {"var": "price_offtake", "low": price_offtake * 0.8, "high": price_offtake * 1.2},
                    "Surplus Injection Rate": {"var": "price_injection", "low": price_injection * 0.8, "high": price_injection * 1.2},
                    "Total Asset Capital Cost": {"var": "battery_cost", "low": battery_cost * 0.8, "high": battery_cost * 1.2},
                    "Roundtrip Efficiency": {"var": "efficiency", "low": 0.85, "high": 0.98},
                    "Net Battery Capacity": {"var": "battery_capacity_kwh", "low": capacity * 0.8, "high": capacity * 1.2},
                    "Solar Generation Scale": {"var": "solar_scale_factor", "low": solar_scale * 0.8, "high": solar_scale * 1.2}
                }
                
                oat_records = []
                
                for visual_name, bounds in sens_vars.items():
                    loop_args = {
                        "solar_file": solar_excel, "fluvius_files": fluvius_csvs,
                        "battery_capacity_kwh": capacity, "max_power_kw": power,
                        "efficiency_charge": eff_charge, "efficiency_discharge": eff_discharge,
                        "price_offtake": price_offtake, "price_injection": price_injection,
                        "battery_cost": battery_cost,
                        "calc_start_date": calc_start.strftime('%d-%m-%Y'),
                        "calc_end_date": calc_end.strftime('%d-%m-%Y'),
                        "generate_graph": False,
                        "solar_scale_factor": solar_scale 
                    }
                    
                    # Low Delta
                    if bounds["var"] == "efficiency":
                        loop_args["efficiency_charge"] = bounds["low"]
                        loop_args["efficiency_discharge"] = bounds["low"]
                    else:
                        loop_args[bounds["var"]] = bounds["low"]
                    low_payback = simulate_battery_roi(**loop_args)["payback_years"]
                    
                    # High Delta
                    if bounds["var"] == "efficiency":
                        loop_args["efficiency_charge"] = bounds["high"]
                        loop_args["efficiency_discharge"] = bounds["high"]
                    else:
                        loop_args[bounds["var"]] = bounds["high"]
                    high_payback = simulate_battery_roi(**loop_args)["payback_years"]
                    
                    oat_records.append({
                        "Parameter": visual_name,
                        "Low_Delta": low_payback - baseline_val,
                        "High_Delta": high_payback - baseline_val,
                        "Absolute_Spread": abs(high_payback - low_payback)
                    })
                
                df_oat = pd.DataFrame(oat_records).sort_values("Absolute_Spread", ascending=True)
                
                # Render Tornado Plot
                tornado = go.Figure()
                
                # Ergonomic Soft Blue
                tornado.add_trace(go.Bar(
                    y=df_oat["Parameter"], x=df_oat["Low_Delta"],
                    base=baseline_val, name="Parameter decreased by 20%", orientation='h',
                    marker_color='#4A90E2', hovertemplate="Payback: %{x:.2f} Years"
                ))
                
                # Ergonomic Warm Orange
                tornado.add_trace(go.Bar(
                    y=df_oat["Parameter"], x=df_oat["High_Delta"],
                    base=baseline_val, name="Parameter increased by 20%", orientation='h',
                    marker_color='#FF9F43', hovertemplate="Payback: %{x:.2f} Years"
                ))
                
                tornado.update_layout(
                    template="plotly_dark", barmode="overlay", height=480,
                    paper_bgcolor='#0A0C10', 
                    plot_bgcolor='#0A0C10',  
                    xaxis_title="Resulting Payback Horizon (Years)",
                    yaxis=dict(autorange="reversed"), 
                    title=f"Tornado Sensitivity Matrix (Baseline Payback Pivot: {baseline_val:.2f} Years)",
                    legend=dict(orientation="h", yanchor="bottom", y=-0.3, xanchor="center", x=0.5)
                )
                
                tornado.update_xaxes(gridcolor="#1e222b")
                tornado.update_yaxes(gridcolor="#1e222b")
                
                st.plotly_chart(tornado, use_container_width=True)
                
        except Exception as e:
            st.error(f"❌ Core processing error encountered: {e}")
