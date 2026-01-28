"""LazyCloud Pricing Dashboard - Monte Carlo powered.

Hetzner CPX compute + JuiceFS Cloud storage (metered).
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from models.billing import METER_PRICES_CENTS, MeterNames, UsageUnits

from pricing.costs import (
    STORAGE_COST_PER_GB_MONTH,
    SUBSCRIPTION_PRICES,
    TIER_LIMITS,
    WORKER_INSTANCE,
    OverheadFactors,
    Tier,
)
from pricing.simulator import (
    SCENARIOS,
    Scenario,
    TierDistribution,
    monte_carlo,
    monte_carlo_breakeven,
    monte_carlo_growth,
)

st.set_page_config(
    page_title="LazyCloud Pricing",
    page_icon="☁️",
    layout="wide",
)


def fmt_pct(stats: dict, key: str = "p50") -> str:
    return f"{stats[key]:.1%}"


def fmt_dollar(stats: dict, key: str = "p50") -> str:
    return f"${stats[key]:,.0f}"


def fmt_range(stats: dict, fmt: str = "dollar") -> str:
    """Format p10-p90 range for help text."""
    if fmt == "dollar":
        return f"p10: ${stats['p10']:,.0f} / p90: ${stats['p90']:,.0f}"
    return f"p10: {stats['p10']:.1%} / p90: {stats['p90']:.1%}"


def main() -> None:
    """Main dashboard entry point."""
    st.title("LazyCloud Pricing Simulator")
    st.caption("Hetzner CPX compute + JuiceFS Cloud storage (metered)")

    # ===== Sidebar =====
    with st.sidebar:
        st.header("Scenario")
        scenario_key = st.radio(
            "Select scenario",
            list(SCENARIOS.keys()),
            format_func=lambda k: SCENARIOS[k].name,
            index=1,  # base case default
        )
        scenario: Scenario = SCENARIOS[scenario_key]
        st.caption(scenario.description)

        st.divider()

        st.header("Simulation")
        num_sims = st.select_slider(
            "Simulations per analysis",
            options=[100, 500, 1000, 2000, 5000],
            value=1000,
        )

        st.header("Metered Pricing")
        for meter in [
            MeterNames.CPU_USAGE,
            MeterNames.MEMORY_USAGE,
            MeterNames.BUILD_MINUTES,
        ]:
            price = METER_PRICES_CENTS[meter]
            st.text(f"{meter.value}: {price}¢")
        storage_price = UsageUnits.get_price_dollars(MeterNames.STORAGE_USAGE)
        st.text(f"Storage: ${storage_price:.2f}/GB-mo")

        st.subheader("Subscriptions")
        for tier, price in SUBSCRIPTION_PRICES.items():
            st.text(f"{tier.value}: ${price}/mo")

        st.subheader("Infrastructure")
        st.text(f"Worker: {WORKER_INSTANCE.name}")
        st.text(f"  {WORKER_INSTANCE.vcpu} vCPU, {WORKER_INSTANCE.memory_gb}GB RAM")
        st.text(f"  ${WORKER_INSTANCE.monthly_price}/mo")
        st.text(f"Storage cost: ${STORAGE_COST_PER_GB_MONTH:.3f}/GB-mo")
        st.text("  JuiceFS Cloud + Hetzner Object Storage")

    # Main content
    tab1, tab2, tab3, tab4 = st.tabs(
        ["Parameters", "Single Customer", "Portfolio", "Growth"]
    )

    # ===== TAB 1: Simulation Parameters (editable, seeded from scenario) =====
    custom_distributions: dict[Tier, TierDistribution] = {}

    with tab1:
        st.header(f"Parameters: {scenario.name}")
        st.caption("Adjust distributions — changes apply to all tabs")

        # Overhead / utilization
        utilization = st.slider(
            "Node Utilization %",
            50,
            95,
            int(scenario.overhead.node_utilization * 100),
            key="utilization",
        )
        custom_overhead = OverheadFactors(node_utilization=utilization / 100)
        st.text(f"Cost multiplier: {custom_overhead.cost_multiplier:.2f}x")

        # Tier mix
        st.subheader("Tier Mix")
        c1, c2, c3 = st.columns(3)
        dev_pct = c1.slider(
            "Developer %",
            0,
            100,
            int(scenario.tier_mix[Tier.DEVELOPER] * 100),
        )
        pro_pct = c2.slider(
            "Pro %",
            0,
            100 - dev_pct,
            min(int(scenario.tier_mix[Tier.PRO] * 100), 100 - dev_pct),
        )
        scale_pct = 100 - dev_pct - pro_pct
        c3.metric("Scale %", f"{scale_pct}%")

        tier_mix = {
            Tier.DEVELOPER: dev_pct / 100,
            Tier.PRO: pro_pct / 100,
            Tier.SCALE: scale_pct / 100,
        }

        # Per-tier distributions
        st.subheader("Tier Distributions")
        for tier in Tier:
            defaults = scenario.distributions[tier]
            limits = TIER_LIMITS[tier]
            key = tier.value

            max_deploy_display = min(limits["max_deployments"], 200)
            st.markdown(f"**{tier.value.title()}**")
            c1, c2, c3, c4, c5 = st.columns(5)

            deploy_min, deploy_max = c1.slider(
                "Deployments",
                1,
                max_deploy_display,
                (defaults.deployments_min, min(defaults.deployments_max, max_deploy_display)),
                key=f"{key}_deploy",
            )
            svc_min, svc_max = c2.slider(
                "Svc/deploy",
                1,
                20,
                (defaults.services_per_deployment_min, defaults.services_per_deployment_max),
                key=f"{key}_svc",
            )
            cpu_min, cpu_max = c3.slider(
                "CPU/svc",
                0.25,
                float(limits["max_cpu"]),
                (defaults.cpu_min, min(defaults.cpu_max, limits["max_cpu"])),
                step=0.25,
                key=f"{key}_cpu",
            )
            builds_min, builds_max = c4.slider(
                "Builds/mo",
                0,
                300,
                (defaults.builds_min, defaults.builds_max),
                key=f"{key}_builds",
            )
            storage_min, storage_max = c5.slider(
                "Storage/vol (GB)",
                0.5,
                100.0,
                (defaults.storage_min, defaults.storage_max),
                step=0.5,
                key=f"{key}_storage",
            )

            custom_distributions[tier] = TierDistribution(
                tier=tier,
                deployments_min=deploy_min,
                deployments_max=deploy_max,
                services_per_deployment_min=svc_min,
                services_per_deployment_max=svc_max,
                cpu_min=cpu_min,
                cpu_max=cpu_max,
                builds_min=builds_min,
                builds_max=builds_max,
                storage_min=storage_min,
                storage_max=storage_max,
            )

        st.divider()
        st.subheader("Memory Model")
        st.markdown(
            """
        Memory per service = CPU x ratio, where ratio ~ Beta(3, 1.5) scaled to [1, 4].
        Skews toward 1:3 - 1:4 (memory-heavy web apps). Mean ~ 2.8 GB per CPU core.
        Minimum enforced: 0.25 CPU, 0.5 GB per service.
        """
        )

        st.subheader("Storage Model")
        st.markdown(
            f"""
        Storage is **metered** at ${storage_price:.2f}/GB-month.
        Backed by JuiceFS Cloud (managed metadata) + Hetzner Object Storage (data).
        Infrastructure cost: ${STORAGE_COST_PER_GB_MONTH:.3f}/GB-month (~{(1 - STORAGE_COST_PER_GB_MONTH / storage_price):.0%} margin).
        ~60% of services use a volume. Storage is decoupled from compute nodes.
        Ephemeral storage (20Gi/service) is from node local disk (not metered).
        """
        )

    # ===== TAB 2: Single Customer (MC with n=1) =====
    with tab2:
        st.header("Single Customer Distribution")
        st.caption(
            "Monte Carlo with 1 customer — what does the 'average' customer look like?"
        )

        with st.spinner("Simulating..."):
            mc1 = monte_carlo(
                1, num_sims, tier_mix, custom_distributions, custom_overhead
            )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Revenue", fmt_dollar(mc1["revenue"]), help=fmt_range(mc1["revenue"]))
        c2.metric("Cost", fmt_dollar(mc1["cost"]), help=fmt_range(mc1["cost"]))
        c3.metric("Profit", fmt_dollar(mc1["profit"]), help=fmt_range(mc1["profit"]))
        c4.metric(
            "Margin", fmt_pct(mc1["margin"]), help=fmt_range(mc1["margin"], "pct")
        )

        # Confidence intervals
        st.subheader("Confidence Intervals")
        ci_data = []
        for name, key in [
            ("Revenue", "revenue"),
            ("Cost", "cost"),
            ("Profit", "profit"),
        ]:
            m = mc1[key]
            ci_data.append(
                {
                    "Metric": name,
                    "10th %ile": f"${m['p10']:,.2f}",
                    "Median": f"${m['p50']:,.2f}",
                    "90th %ile": f"${m['p90']:,.2f}",
                    "Mean": f"${m['mean']:,.2f}",
                }
            )
        m = mc1["margin"]
        ci_data.append(
            {
                "Metric": "Margin",
                "10th %ile": f"{m['p10']:.1%}",
                "Median": f"{m['p50']:.1%}",
                "90th %ile": f"{m['p90']:.1%}",
                "Mean": f"{m['mean']:.1%}",
            }
        )
        st.dataframe(pd.DataFrame(ci_data), hide_index=True, use_container_width=True)

        # Per-tier single customer
        st.subheader("By Tier (1 customer, forced tier)")
        tier_data = []
        for tier in Tier:
            single_tier_mix = {t: (1.0 if t == tier else 0.0) for t in Tier}
            mc_tier = monte_carlo(
                1, num_sims, single_tier_mix, custom_distributions, custom_overhead
            )
            tier_data.append(
                {
                    "Tier": tier.value.title(),
                    "Revenue (median)": f"${mc_tier['revenue']['p50']:,.2f}",
                    "Cost (median)": f"${mc_tier['cost']['p50']:,.2f}",
                    "Profit (median)": f"${mc_tier['profit']['p50']:,.2f}",
                    "Margin (median)": f"{mc_tier['margin']['p50']:.1%}",
                }
            )
        st.dataframe(pd.DataFrame(tier_data), hide_index=True, use_container_width=True)

    # ===== TAB 3: Portfolio =====
    with tab3:
        st.header("Portfolio Simulation")

        num_customers = st.slider("Number of Customers", 10, 1000, 100, step=10)

        with st.spinner("Simulating..."):
            mc = monte_carlo(
                num_customers, num_sims, tier_mix, custom_distributions, custom_overhead
            )

        c1, c2, c3, c4 = st.columns(4)
        c1.metric(
            "Monthly Revenue", fmt_dollar(mc["revenue"]), help=fmt_range(mc["revenue"])
        )
        c2.metric("Monthly Cost", fmt_dollar(mc["cost"]), help=fmt_range(mc["cost"]))
        c3.metric(
            "Monthly Profit", fmt_dollar(mc["profit"]), help=fmt_range(mc["profit"])
        )
        c4.metric("Margin", fmt_pct(mc["margin"]), help=fmt_range(mc["margin"], "pct"))

        c5, c6 = st.columns(2)
        if mc["arpu"]:
            c5.metric("ARPU (median)", f"${mc['arpu']['p50']:,.2f}")
        c6.metric("Projected ARR", f"${mc['revenue']['p50'] * 12:,.0f}")

        # Break-even
        st.subheader("Break-Even Analysis")
        with st.spinner("Running break-even..."):
            be_results = monte_carlo_breakeven(
                num_simulations=min(num_sims, 500),
                tier_mix=tier_mix,
                distributions=custom_distributions,
                overhead=custom_overhead,
            )

        be_data = []
        for r in be_results:
            be_data.append(
                {
                    "customers": r["customers"],
                    "revenue_p50": r["revenue"]["p50"],
                    "cost_p50": r["cost"]["p50"],
                    "profit_p50": r["profit"]["p50"],
                    "profit_p10": r["profit"]["p10"],
                    "margin_p50": r["margin"]["p50"],
                }
            )
        be_df = pd.DataFrame(be_data)

        # Find break-even
        profitable = be_df[be_df["profit_p50"] > 0]
        if not profitable.empty:
            be_n = int(profitable.iloc[0]["customers"])
            st.success(f"Break-even (median) at approximately **{be_n} customers**")

            pessimistic = be_df[be_df["profit_p10"] > 0]
            if not pessimistic.empty:
                be_p10 = int(pessimistic.iloc[0]["customers"])
                st.info(
                    f"Break-even (pessimistic p10) at approximately **{be_p10} customers**"
                )
        else:
            st.warning("Not profitable at any tested scale (median)")

        # Chart
        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name="Revenue (median)",
                x=be_df["customers"],
                y=be_df["revenue_p50"],
                marker_color="#22c55e",
            )
        )
        fig.add_trace(
            go.Bar(
                name="Cost (median)",
                x=be_df["customers"],
                y=be_df["cost_p50"],
                marker_color="#ef4444",
            )
        )
        fig.update_layout(
            title="Revenue vs Cost by Scale",
            xaxis_title="Customers",
            yaxis_title="Monthly ($)",
            barmode="group",
        )
        st.plotly_chart(fig, use_container_width=True)

        fig2 = go.Figure()
        fig2.add_trace(
            go.Scatter(
                x=be_df["customers"],
                y=be_df["profit_p50"],
                mode="lines+markers",
                name="Profit (median)",
                line={"color": "#3b82f6"},
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=be_df["customers"],
                y=be_df["profit_p10"],
                mode="lines",
                name="Profit (p10)",
                line={"color": "#3b82f6", "dash": "dash"},
            )
        )
        fig2.add_hline(y=0, line_dash="dash", line_color="gray")
        fig2.update_layout(
            title="Profit by Scale (with pessimistic band)",
            xaxis_title="Customers",
            yaxis_title="Monthly Profit ($)",
        )
        st.plotly_chart(fig2, use_container_width=True)

        # Table
        st.subheader("Detailed Data")
        display_df = be_df.copy()
        display_df.columns = [
            "Customers",
            "Revenue",
            "Cost",
            "Profit (median)",
            "Profit (p10)",
            "Margin",
        ]
        display_df["Revenue"] = display_df["Revenue"].apply(lambda x: f"${x:,.0f}")
        display_df["Cost"] = display_df["Cost"].apply(lambda x: f"${x:,.0f}")
        display_df["Profit (median)"] = display_df["Profit (median)"].apply(
            lambda x: f"${x:,.0f}"
        )
        display_df["Profit (p10)"] = display_df["Profit (p10)"].apply(
            lambda x: f"${x:,.0f}"
        )
        display_df["Margin"] = display_df["Margin"].apply(lambda x: f"{x:.1%}")
        st.dataframe(display_df, hide_index=True, use_container_width=True)

    # ===== TAB 4: Growth Projection =====
    with tab4:
        st.header("Growth Projection")

        col1, col2, col3 = st.columns(3)
        initial = col1.number_input("Initial Customers", 10, 500, 50, step=10)
        growth = col2.slider("Monthly Growth %", 5, 25, 10)
        churn = col3.slider("Monthly Churn %", 1, 10, 5)

        st.caption(f"Net monthly growth: {growth - churn}%")

        with st.spinner("Simulating 24-month growth..."):
            growth_results = monte_carlo_growth(
                initial,
                24,
                growth / 100,
                churn / 100,
                num_simulations=min(num_sims, 500),
                tier_mix=tier_mix,
                distributions=custom_distributions,
                overhead=custom_overhead,
            )

        # Key metrics at month 24
        final = growth_results[-1]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Month 24 Customers", f"{final['customers']:,}")
        c2.metric("Month 24 MRR", fmt_dollar(final["revenue"]))
        c3.metric("Month 24 Profit", fmt_dollar(final["profit"]))
        c4.metric("Projected ARR", f"${final['revenue']['p50'] * 12:,.0f}")

        # Build dataframe
        gdf = pd.DataFrame(
            [
                {
                    "month": r["month"],
                    "customers": r["customers"],
                    "revenue_p50": r["revenue"]["p50"],
                    "cost_p50": r["cost"]["p50"],
                    "profit_p50": r["profit"]["p50"],
                    "profit_p10": r["profit"]["p10"],
                    "margin_p50": r["margin"]["p50"],
                }
                for r in growth_results
            ]
        )

        # Customer growth
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=gdf["customers"],
                mode="lines+markers",
                name="Customers",
                line={"color": "#8b5cf6"},
            )
        )
        fig.update_layout(
            title="Customer Growth", xaxis_title="Month", yaxis_title="Customers"
        )
        st.plotly_chart(fig, use_container_width=True)

        # Revenue / Cost / Profit with bands
        fig2 = go.Figure()
        fig2.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=gdf["revenue_p50"],
                mode="lines",
                name="Revenue",
                line={"color": "#22c55e"},
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=gdf["cost_p50"],
                mode="lines",
                name="Cost",
                line={"color": "#ef4444"},
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=gdf["profit_p50"],
                mode="lines",
                name="Profit (median)",
                line={"color": "#3b82f6"},
            )
        )
        fig2.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=gdf["profit_p10"],
                mode="lines",
                name="Profit (p10)",
                line={"color": "#3b82f6", "dash": "dash"},
            )
        )
        fig2.add_hline(y=0, line_dash="dash", line_color="gray")
        fig2.update_layout(
            title="Revenue, Cost, Profit Over Time (median + pessimistic)",
            xaxis_title="Month",
            yaxis_title="Monthly ($)",
        )
        st.plotly_chart(fig2, use_container_width=True)

        # Margin
        fig3 = go.Figure()
        fig3.add_trace(
            go.Scatter(
                x=gdf["month"],
                y=[m * 100 for m in gdf["margin_p50"]],
                mode="lines+markers",
                name="Margin %",
                line={"color": "#f59e0b"},
            )
        )
        fig3.update_layout(
            title="Margin Over Time (median)",
            xaxis_title="Month",
            yaxis_title="Margin (%)",
        )
        st.plotly_chart(fig3, use_container_width=True)


if __name__ == "__main__":
    main()
