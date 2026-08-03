"""
Take Cash / Vault controls for the Streamlit sidebar.
"""

from __future__ import annotations

import streamlit as st

from kalshi_bot.vault import (
    VaultConfig,
    enqueue_set_config,
    enqueue_take_cash,
    load_recent_skims,
    load_vault_config,
)

from ..data.state_store import StateStore


def render_vault_panel() -> None:
    store = StateStore()
    portfolio = store.get_portfolio()
    vcfg = load_vault_config()

    st.divider()
    st.markdown("**Take Cash / Vault**")
    st.caption(
        "Move paper profit out of the trading book. "
        "Example: +$200 profit → take $100 to vault."
    )

    c1, c2 = st.columns(2)
    with c1:
        st.metric("Vault", f"${portfolio.vault_balance:,.2f}")
    with c2:
        st.metric("Equity", f"${portfolio.total_equity:,.2f}")
    st.caption(
        f"Trading ${portfolio.balance:,.2f} · "
        f"skimmable ${portfolio.skimmable_profit:,.2f}"
    )

    auto = st.toggle(
        "Auto take-profit",
        value=bool(vcfg.auto_enabled),
        help="When trading profit hits the trigger, skim the amount into the vault.",
        key="vault_auto_toggle",
    )
    trigger = st.number_input(
        "Profit trigger ($)",
        min_value=1.0,
        value=float(vcfg.profit_trigger),
        step=50.0,
        key="vault_trigger_input",
    )
    skim = st.number_input(
        "Skim amount ($)",
        min_value=0.0,
        value=float(vcfg.skim_amount),
        step=25.0,
        key="vault_skim_input",
    )

    if st.button("Save auto settings", use_container_width=True, key="vault_save_cfg"):
        new_cfg = VaultConfig(
            auto_enabled=bool(auto),
            profit_trigger=float(trigger),
            skim_amount=float(skim),
        )
        enqueue_set_config(new_cfg)
        st.success(
            f"Saved — auto={'ON' if auto else 'OFF'}, "
            f"trigger ${trigger:.0f}, skim ${skim:.0f}"
        )

    st.markdown("**Manual take**")
    manual_amt = st.number_input(
        "Amount ($)",
        min_value=0.0,
        value=min(100.0, max(0.0, float(portfolio.skimmable_profit))),
        step=10.0,
        key="vault_manual_amt",
    )
    if st.button("Take cash now", use_container_width=True, type="primary", key="vault_take_btn"):
        if manual_amt <= 0:
            st.warning("Enter an amount > 0")
        elif manual_amt > portfolio.skimmable_profit + 1e-6:
            st.warning(
                f"Can only take up to skimmable profit "
                f"(${portfolio.skimmable_profit:,.2f})"
            )
        else:
            enqueue_take_cash(float(manual_amt), reason="manual")
            st.success(
                f"Queued take ${manual_amt:,.2f} — "
                "bot applies within ~5s"
            )

    skims = load_recent_skims(8)
    if skims:
        st.markdown("**Recent skims**")
        for ev in skims[:5]:
            ts = str(ev.get("ts", ""))[:19].replace("T", " ")
            st.caption(
                f"{ts}  ${float(ev.get('amount', 0)):.2f}  "
                f"({ev.get('reason', '?')}) → vault "
                f"${float(ev.get('vault_after', 0)):.2f}"
            )
