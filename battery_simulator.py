import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

def simulate_battery_roi(
    solar_file, 
    fluvius_files, 
    battery_capacity_kwh, 
    max_power_kw, 
    efficiency_charge=0.96, 
    efficiency_discharge=0.95, 
    price_offtake=0.39, 
    price_injection=0.04, 
    battery_cost=4500.0,
    calc_start_date=None,     
    calc_end_date=None,       
    plot_start_date=None,     
    plot_end_date=None,
    generate_graph=True,
    solar_scale_factor=1.0 # Added scaling factor parameter
):
    # ==========================================
    # 1. LOAD & PREP DATA
    # ==========================================
    df_raw = pd.concat([
        pd.read_csv(f, sep=';', decimal=',', encoding='utf-8', low_memory=False) 
        for f in fluvius_files
    ], ignore_index=True)
    
    df_raw['timestamp'] = pd.to_datetime(df_raw['Van (datum)'] + ' ' + df_raw['Van (tijdstip)'], format='%d-%m-%Y %H:%M:%S')
    df_raw['Volume'] = df_raw['Volume'].fillna(0.0)
    df_raw['direction'] = df_raw['Register'].apply(lambda x: 'offtake' if 'Afname' in str(x) else ('injection' if 'Injectie' in str(x) else 'other'))
    
    df = df_raw.pivot_table(index='timestamp', columns='direction', values='Volume', aggfunc='sum').reset_index()
    df.columns.name = None
    df = df.rename(columns={'offtake': 'historic_offtake_kwh', 'injection': 'historic_injection_kwh'})
    df = df.sort_values('timestamp').reset_index(drop=True)
    df['date_only'] = df['timestamp'].dt.normalize()

    # Load Solar Data
    df_solar = pd.read_excel(solar_file)
    df_solar = df_solar.rename(columns={'kWh produced': 'daily_production_kwh', 'date': 'solar_date'})
    df_solar['solar_date'] = pd.to_datetime(df_solar['solar_date'], errors='coerce')
    df_solar = df_solar.drop_duplicates(subset=['solar_date'])

    # Merge & Fill Missing Days via Historical Averages
    df = pd.merge(df, df_solar, left_on='date_only', right_on='solar_date', how='left')
    df['day_of_year'] = df['timestamp'].dt.dayofyear
    historical_averages = df.groupby('day_of_year')['daily_production_kwh'].transform('mean')
    df['daily_production_kwh'] = df['daily_production_kwh'].fillna(historical_averages)

    # ==========================================
    # 2. HOUSE PRODUCTION & CONSUMPTION MODEL
    # ==========================================
    df['daily_injection_kwh'] = df.groupby('date_only')['historic_injection_kwh'].transform('sum')
    df['daily_self_consumed_kwh'] = (df['daily_production_kwh'] - df['daily_injection_kwh']).clip(lower=0)

    hours = df['timestamp'].dt.hour + df['timestamp'].dt.minute / 60.0
    weights = np.exp(-0.5 * ((hours - 13.5) / 2.5) ** 2)
    df['weight'] = np.where(weights < 0.05, 0, weights)
    
    daily_weight_sum = df.groupby('date_only')['weight'].transform('sum')
    df['normalized_weight'] = df['weight'] / daily_weight_sum

    df['distributed_self_consumed_kwh'] = df['daily_self_consumed_kwh'] * df['normalized_weight']
    df['estimated_production_kwh'] = df['historic_injection_kwh'] + df['distributed_self_consumed_kwh']
    df['estimated_consumption_kwh'] = df['historic_offtake_kwh'] + df['estimated_production_kwh'] - df['historic_injection_kwh']

    # --- NEW: APPLY SOLAR SCALING FACTOR ---
    # Scale 15-minute generation
    df['scaled_production_kwh'] = df['estimated_production_kwh'] * solar_scale_factor
    
    # Recalculate grid flow dynamics before battery (production_scaled - consumption_original)
    df['net_grid_before_battery'] = df['scaled_production_kwh'] - df['estimated_consumption_kwh']
    df['scaled_injection_before_battery'] = df['net_grid_before_battery'].clip(lower=0)
    df['scaled_offtake_before_battery'] = (-df['net_grid_before_battery']).clip(lower=0)

    df = df.drop(columns=['solar_date', 'day_of_year', 'weight', 'normalized_weight', 'distributed_self_consumed_kwh', 'daily_injection_kwh', 'daily_self_consumed_kwh'], errors='ignore')

    # Filter Calculation Date Range
    if calc_start_date:
        df = df[df['timestamp'] >= pd.to_datetime(calc_start_date, dayfirst=True)]
    if calc_end_date:
        end_dt = pd.to_datetime(calc_end_date, dayfirst=True)
        if end_dt.hour == 0 and end_dt.minute == 0:
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
        df = df[df['timestamp'] <= end_dt]
    df = df.reset_index(drop=True)

    if len(df) == 0:
        raise ValueError("Selected calculation date range contains no data.")

    # ==========================================
    # 3. BATTERY DISPATCH SIMULATION LOOP
    # ==========================================
    max_transfer_per_15min_kwh = max_power_kw * 0.25
    current_soc = 0.0  
    soc_history, new_injection_history, new_offtake_history = [], [], []

    # Loop over the newly derived scaled baseline instead of the raw historical parameters
    for inj, off in zip(df['scaled_injection_before_battery'], df['scaled_offtake_before_battery']):
        net_energy = inj - off 
        
        if net_energy > 0:
            room_available = (battery_capacity_kwh - current_soc) / efficiency_charge
            charge_amount = min(net_energy, max_transfer_per_15min_kwh, room_available)
            current_soc += (charge_amount * efficiency_charge)
            new_injection_history.append(net_energy - charge_amount)
            new_offtake_history.append(0.0)
        elif net_energy < 0:
            deficit = abs(net_energy)
            available_to_house = current_soc * efficiency_discharge
            discharge_amount = min(deficit, max_transfer_per_15min_kwh, available_to_house)
            current_soc -= (discharge_amount / efficiency_discharge)
            new_injection_history.append(0.0)
            new_offtake_history.append(deficit - discharge_amount)
        else:
            new_injection_history.append(0.0)
            new_offtake_history.append(0.0)
            
        soc_history.append(current_soc)

    df['battery_soc_kwh'] = soc_history
    df['new_injection_kwh'] = new_injection_history
    df['new_offtake_kwh'] = new_offtake_history

    # ==========================================
    # 4. FINANCIAL CALCULATIONS & CLEAN ALIGNMENT
    # ==========================================
    days_in_dataset = (df['timestamp'].max() - df['timestamp'].min()).days
    years_in_dataset = max(days_in_dataset, 1) / 365.25
    
    # Financials now cleanly reference the scaled base case
    annual_orig_offtake = df['scaled_offtake_before_battery'].sum() / years_in_dataset
    annual_orig_injection = df['scaled_injection_before_battery'].sum() / years_in_dataset
    annual_new_offtake = df['new_offtake_kwh'].sum() / years_in_dataset
    annual_new_injection = df['new_injection_kwh'].sum() / years_in_dataset

    cost_no_bat = annual_orig_offtake * price_offtake
    rev_no_bat = annual_orig_injection * price_injection
    bill_no_bat = cost_no_bat - rev_no_bat 

    cost_with_bat = annual_new_offtake * price_offtake
    rev_with_bat = annual_new_injection * price_injection
    bill_with_bat = cost_with_bat - rev_with_bat 

    annual_savings = bill_no_bat - bill_with_bat
    payback_years = (battery_cost / annual_savings) if annual_savings > 0 else float('inf')

    # Pristine Monospaced Column Alignment
    receipt = (
        f"==================================================\n"
        f"       📊 ANNUAL FINANCIAL MATRIX BREAKDOWN       \n"
        f"==================================================\n"
        f" Battery Specs:     {battery_capacity_kwh:5.1f} kWh Net | {max_power_kw:4.1f} kW Inverter\n"
        f" Solar Scale:       {solar_scale_factor*100:5.1f}%\n"
        f" Calc Duration:     {years_in_dataset:5.2f} Years total window\n"
        f" Network Tariffs:   Offtake €{price_offtake:.2f}/kWh | Injection €{price_injection:.2f}/kWh\n"
        f"--------------------------------------------------\n"
        f" SCENARIO A: WITHOUT BATTERY SYSTEM\n"
        f"  • Grid Offtake Expenditures:     € {cost_no_bat:>8,.2f}\n"
        f"  • Grid Injection Revenue:        € {rev_no_bat:>8,.2f}\n"
        f"  • Total Net Utility Invoice:     € {bill_no_bat:>8,.2f}\n"
        f"--------------------------------------------------\n"
        f" SCENARIO B: WITH OPTIMIZED BATTERY SYSTEM\n"
        f"  • Grid Offtake Expenditures:     € {cost_with_bat:>8,.2f}\n"
        f"  • Grid Injection Revenue:        € {rev_with_bat:>8,.2f}\n"
        f"  • Total Net Utility Invoice:     € {bill_with_bat:>8,.2f}\n"
        f"--------------------------------------------------\n"
        f" KEY FINANCIAL PERFORMANCE INDICATORS\n"
        f"  • Upfront Asset Investment:     € {battery_cost:>8,.2f}\n"
        f"  • Consolidated Annual Savings:   € {annual_savings:>8,.2f}\n"
        f"  • Estimated Net Payback Period:    {payback_years:>8.2f} Years\n"
        f"==================================================\n"
    )

    if not generate_graph:
        return {"payback_years": payback_years, "financial_receipt": receipt}

    # ==========================================
    # 5. GRAPH STYLING (SEAMLESS DEEP BLACK)
    # ==========================================
    df['battery_soc_percent'] = (df['battery_soc_kwh'] / battery_capacity_kwh) * 100
    
    plot_start = pd.to_datetime(plot_start_date, dayfirst=True) if plot_start_date else df['timestamp'].min()
    plot_end = pd.to_datetime(plot_end_date, dayfirst=True) if plot_end_date else df['timestamp'].max()
    if plot_end_date and plot_end.hour == 0:
         plot_end = plot_end.replace(hour=23, minute=59, second=59)
    
    df_slice = df[(df['timestamp'] >= plot_start) & (df['timestamp'] <= plot_end)].copy()

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.14, 
        subplot_titles=("1. Household Internal Energy Profiles", "2. Physical Utility Meter Exchange Flows", "3. Chemical Battery State of Charge"),
        row_heights=[0.35, 0.45, 0.20]
    )

    # Subplot 1
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['scaled_production_kwh'], name='Estimated Solar Gen. (Scaled)', line=dict(color='#ff7f0e', width=2), fill='tozeroy', legend='legend'), row=1, col=1)
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['estimated_consumption_kwh'], name='Gross Consumption Demand', line=dict(color='#9467bd', width=2), legend='legend'), row=1, col=1)
    
    # Subplot 2: Shows the scaled baselines without battery vs new dispatch configurations
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['scaled_injection_before_battery'], name='Scaled Injection (No Bat)', line=dict(color='rgba(44, 160, 44, 0.65)', dash='dot', width=2.5), legend='legend2'), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['scaled_offtake_before_battery'], name='Scaled Offtake (No Bat)', line=dict(color='rgba(214, 39, 40, 0.65)', dash='dot', width=2.5), legend='legend2'), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['new_injection_kwh'], name='New Injection (With Bat)', line=dict(color='rgba(0, 230, 115, 1.0)', width=2), legend='legend2'), row=2, col=1)
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['new_offtake_kwh'], name='New Offtake (With Bat)', line=dict(color='rgba(255, 51, 51, 1.0)', width=2), legend='legend2'), row=2, col=1)
    
    # Subplot 3
    fig.add_trace(go.Scatter(x=df_slice['timestamp'], y=df_slice['battery_soc_percent'], name='Battery SOC Gauge (%)', line=dict(color='#1f77b4', width=2), fill='tozeroy', legend='legend3'), row=3, col=1)

    fig.update_layout(
        height=950, 
        template="plotly_dark", 
        hovermode="x unified",
        paper_bgcolor='#0A0C10', 
        plot_bgcolor='#0A0C10',  
        margin=dict(l=50, r=50, t=80, b=120),
        legend=dict(orientation="h", yanchor="top", y=0.74, xanchor="center", x=0.5, bgcolor="rgba(10,10,10,0.7)"),
        legend2=dict(orientation="h", yanchor="top", y=0.26, xanchor="center", x=0.5, bgcolor="rgba(10,10,10,0.7)"),
        legend3=dict(orientation="h", yanchor="top", y=-0.12, xanchor="center", x=0.5, bgcolor="rgba(10,10,10,0.7)")
    )
    fig.update_yaxes(title_text="Energy (kWh)", gridcolor="#1e222b", row=1, col=1)
    fig.update_yaxes(title_text="Energy (kWh)", gridcolor="#1e222b", row=2, col=1)
    fig.update_yaxes(title_text="SOC (%)", range=[-5, 105], gridcolor="#1e222b", row=3, col=1)
    fig.update_xaxes(gridcolor="#1e222b")

    return {"payback_years": payback_years, "financial_receipt": receipt, "dashboard_figure": fig}
