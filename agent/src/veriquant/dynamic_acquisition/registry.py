"""Versioned, fail-closed capability registry for approved-proxy data APIs.

The registry is deliberately static.  A model or caller may choose among these
capabilities, but cannot invent an API, field, parameter, or time policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


REGISTRY_VERSION = "veriquant.dynamic-acquisition-capabilities/v1"


class CapabilityGroup(str, Enum):
    MARKET = "MARKET"
    REFERENCE = "REFERENCE"
    FUNDAMENTAL = "FUNDAMENTAL"
    EVENT = "EVENT"
    FLOW_LIQUIDITY = "FLOW_LIQUIDITY"
    MACRO_REGIME = "MACRO_REGIME"


class PaginationStrategy(str, Enum):
    DATE = "DATE"
    SYMBOL_DATE = "SYMBOL_DATE"
    OFFSET = "OFFSET"
    SINGLE_QUERY = "SINGLE_QUERY"


class TimeSemantics(str, Enum):
    CLOSE_AVAILABLE = "CLOSE_AVAILABLE"
    ANNOUNCEMENT_AVAILABLE = "ANNOUNCEMENT_AVAILABLE"
    EVENT_AVAILABLE = "EVENT_AVAILABLE"
    RELEASE_AVAILABLE = "RELEASE_AVAILABLE"
    REFERENCE_AS_OF = "REFERENCE_AS_OF"


class QueryRejected(ValueError):
    """Raised before transport invocation for anything outside the registry."""


@dataclass(frozen=True, slots=True)
class ApiCapability:
    api_name: str
    group: CapabilityGroup
    fields: frozenset[str]
    parameters: frozenset[str]
    required_any_parameters: tuple[frozenset[str], ...]
    primary_key: tuple[str, ...]
    pagination: PaginationStrategy
    time_semantics: TimeSemantics
    max_rows_per_call: int
    revision_policy: str

    def validate(
        self,
        *,
        params: Mapping[str, Any],
        fields: tuple[str, ...],
    ) -> None:
        if not isinstance(params, Mapping):
            raise QueryRejected("params must be a mapping")
        keys = frozenset(str(key) for key in params)
        forbidden = keys & _FORBIDDEN_PARAMETER_NAMES
        if forbidden:
            raise QueryRejected("credential and endpoint parameters are forbidden")
        unknown_params = keys - self.parameters
        if unknown_params:
            raise QueryRejected(
                f"parameters outside {self.api_name} allowlist: "
                f"{','.join(sorted(unknown_params))}"
            )
        if self.required_any_parameters and not any(
            required.issubset(keys) for required in self.required_any_parameters
        ):
            raise QueryRejected(f"required parameter set missing for {self.api_name}")
        if not fields:
            raise QueryRejected("at least one field is required")
        if len(fields) != len(set(fields)):
            raise QueryRejected("duplicate fields are forbidden")
        unknown_fields = set(fields) - self.fields
        if unknown_fields:
            raise QueryRejected(
                f"fields outside {self.api_name} allowlist: "
                f"{','.join(sorted(unknown_fields))}"
            )


_FORBIDDEN_PARAMETER_NAMES = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "url",
        "endpoint",
        "base_url",
    }
)


def _cap(
    name: str,
    group: CapabilityGroup,
    fields: str,
    params: str,
    primary_key: str,
    pagination: PaginationStrategy,
    semantics: TimeSemantics,
    *,
    required_any: tuple[str, ...] = (),
    max_rows: int = 5000,
    revision: str = "append-only-by-availability-time",
) -> ApiCapability:
    return ApiCapability(
        api_name=name,
        group=group,
        fields=frozenset(fields.split()),
        parameters=frozenset(params.split()),
        required_any_parameters=tuple(
            frozenset(item.split()) for item in required_any
        ),
        primary_key=tuple(primary_key.split()),
        pagination=pagination,
        time_semantics=semantics,
        max_rows_per_call=max_rows,
        revision_policy=revision,
    )


_CAPABILITIES = (
    # Market, calendar, membership, and trade-state data.
    _cap("daily", CapabilityGroup.MARKET,
         "ts_code trade_date open high low close pre_close change pct_chg vol amount",
         "ts_code trade_date start_date end_date", "ts_code trade_date",
         PaginationStrategy.SYMBOL_DATE, TimeSemantics.CLOSE_AVAILABLE,
         required_any=("trade_date", "start_date end_date"), max_rows=6000),
    _cap("adj_factor", CapabilityGroup.MARKET,
         "ts_code trade_date adj_factor", "ts_code trade_date start_date end_date",
         "ts_code trade_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.CLOSE_AVAILABLE, required_any=("trade_date", "start_date end_date")),
    _cap("daily_basic", CapabilityGroup.MARKET,
         "ts_code trade_date close turnover_rate turnover_rate_f volume_ratio pe pe_ttm pb ps ps_ttm dv_ratio dv_ttm total_share float_share free_share total_mv circ_mv",
         "ts_code trade_date start_date end_date", "ts_code trade_date",
         PaginationStrategy.SYMBOL_DATE, TimeSemantics.CLOSE_AVAILABLE,
         required_any=("trade_date", "start_date end_date"), max_rows=6000),
    _cap("index_daily", CapabilityGroup.MARKET,
         "ts_code trade_date open high low close pre_close change pct_chg vol amount",
         "ts_code trade_date start_date end_date", "ts_code trade_date",
         PaginationStrategy.SYMBOL_DATE, TimeSemantics.CLOSE_AVAILABLE,
         required_any=("trade_date", "start_date end_date")),
    _cap("trade_cal", CapabilityGroup.REFERENCE,
         "exchange cal_date is_open pretrade_date", "exchange start_date end_date is_open",
         "exchange cal_date", PaginationStrategy.DATE, TimeSemantics.REFERENCE_AS_OF,
         required_any=("start_date end_date",), max_rows=10000),
    _cap("stock_basic", CapabilityGroup.REFERENCE,
         "ts_code symbol name area industry fullname enname cnspell market exchange curr_type list_status list_date delist_date is_hs act_name act_ent_type",
         "ts_code name market exchange list_status is_hs offset limit",
         "ts_code", PaginationStrategy.OFFSET, TimeSemantics.REFERENCE_AS_OF,
         max_rows=5000, revision="snapshot-with-capture-time"),
    _cap("index_weight", CapabilityGroup.REFERENCE,
         "index_code con_code trade_date weight", "index_code trade_date start_date end_date",
         "index_code con_code trade_date", PaginationStrategy.DATE,
         TimeSemantics.REFERENCE_AS_OF, required_any=("trade_date", "start_date end_date")),
    _cap("suspend_d", CapabilityGroup.EVENT,
         "ts_code trade_date suspend_timing suspend_type", "ts_code trade_date start_date end_date suspend_type",
         "ts_code trade_date suspend_timing", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.EVENT_AVAILABLE, required_any=("trade_date", "start_date end_date")),
    _cap("stk_limit", CapabilityGroup.MARKET,
         "ts_code trade_date pre_close up_limit down_limit", "ts_code trade_date start_date end_date",
         "ts_code trade_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.CLOSE_AVAILABLE, required_any=("trade_date", "start_date end_date")),
    # Fundamental statements and derived indicators.  ann_date/f_ann_date are
    # intentionally available so downstream PIT normalization need not guess.
    _cap("income", CapabilityGroup.FUNDAMENTAL,
         "ts_code ann_date f_ann_date end_date report_type comp_type basic_eps diluted_eps total_revenue revenue int_income prem_earned comm_income n_commis_income n_oth_income n_oth_b_income prem_income out_prem une_prem_reser reins_income n_sec_tb_income n_sec_uw_income n_asset_mg_income oth_b_income fv_value_chg_gain invest_income ass_invest_income forex_gain total_cogs oper_cost int_exp comm_exp biz_tax_surchg sell_exp admin_exp fin_exp assets_impair_loss prem_refund compens_payout reser_insur_liab div_payt reins_exp oper_exp compens_payout_refu insur_reser_refu reins_cost_refund other_bus_cost operate_profit non_oper_income non_oper_exp nca_disploss total_profit income_tax n_income n_income_attr_p minority_gain oth_compr_income t_compr_income compr_inc_attr_p compr_inc_attr_m_s ebit ebitda insurance_exp undist_profit distable_profit update_flag",
         "ts_code ann_date start_date end_date period report_type comp_type offset limit",
         "ts_code end_date report_type ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    _cap("balancesheet", CapabilityGroup.FUNDAMENTAL,
         "ts_code ann_date f_ann_date end_date report_type comp_type total_share cap_rese undistr_porfit surplus_rese special_rese money_cap trad_asset notes_receiv accounts_receiv oth_receiv prepayment div_receiv int_receiv inventories amor_exp nca_within_1y sett_rsrv loanto_oth_bank_fi premium_receiv reinsur_receiv reinsur_res_receiv pur_resale_fa oth_cur_assets total_cur_assets fa_avail_for_sale htm_invest lt_eqt_invest invest_real_estate time_deposits oth_assets lt_rec fixed_assets cip const_materials fixed_assets_disp produc_bio_assets oil_and_gas_assets intan_assets r_and_d goodwill lt_amor_exp defer_tax_assets decr_in_disbur oth_nca total_nca cash_reser_cb depos_in_oth_bfi prec_metals deriv_assets rr_reins_une_prem rr_reins_outstd_cla rr_reins_lins_liab rr_reins_lthins_liab refund_depos ph_pledge_loans refund_cap_depos indep_acct_assets client_depos client_prov transac_seat_fee invest_as_receiv total_assets lt_borr st_borr cb_borr depos_ib_deposits loan_oth_bank trading_fl notes_payable acct_payable adv_receipts sold_for_repur_fa comm_payable payroll_payable taxes_payable int_payable div_payable oth_payable acc_exp deferred_inc st_bonds_payable payable_to_reinsurer rsrv_insur_cont acting_trading_sec acting_uw_sec non_cur_liab_due_1y oth_cur_liab total_cur_liab bond_payable lt_payable specific_payables estimated_liab defer_tax_liab defer_inc_non_cur_liab oth_ncl total_ncl depos_oth_bfi deriv_liab depos received_sec_deposit agent_trade_sec agent_uw_sec total_liab treasury_share ordin_risk_reser forex_differ invest_loss_unconf minority_int total_hldr_eqy_exc_min_int total_hldr_eqy_inc_min_int total_liab_hldr_eqy update_flag",
         "ts_code ann_date start_date end_date period report_type comp_type offset limit",
         "ts_code end_date report_type ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    _cap("cashflow", CapabilityGroup.FUNDAMENTAL,
         "ts_code ann_date f_ann_date end_date comp_type report_type net_profit finan_exp c_fr_sale_sg recp_tax_rends n_depos_incr_fi n_incr_loans_cb n_inc_borr_oth_fi prem_fr_orig_contr n_incr_insured_dep n_reinsur_prem n_incr_disp_tfa ifc_cash_incr n_incr_disp_faas oth_cash_recp_ral_oper c_inf_fr_operate total_c_inflow buy_goods_pay_wages_payable pay_tax oth_cash_pay_ral_oper c_outf_fr_operate n_cashflow_act invest_income disp_assets_cash recp_return_invest oth_cash_recp_ral_inv_act c_inf_fr_invest total_c_inflow_inv capex oth_cash_pay_ral_inv_act c_outf_fr_invest n_cashflow_inv_act c_inf_fr_fnc_act c_outf_fr_fnc_act n_cash_flows_fnc_act eff_fx_flu_cash n_incr_cash_cash_equ c_cash_equ_beg_period c_cash_equ_end_period update_flag",
         "ts_code ann_date start_date end_date period report_type comp_type offset limit",
         "ts_code end_date report_type ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    _cap("fina_indicator", CapabilityGroup.FUNDAMENTAL,
         "ts_code ann_date end_date eps dt_eps total_revenue_ps revenue_ps capital_rese_ps surplus_rese_ps undist_profit_ps extra_item profit_dedt gross_margin current_ratio quick_ratio cash_ratio invturn_days arturn_days inv_turn ar_turn ca_turn fa_turn assets_turn op_income valuechange_income interst_income daa ebit ebitda fcff fcfe current_exint noncurrent_exint interestdebt netdebt tangible_asset working_capital networking_capital invest_capital retained_earnings diluted2_eps bps ocfps retainedps cfps ebit_ps fcff_ps fcfe_ps netprofit_margin grossprofit_margin cogs_of_sales expense_of_sales profit_to_gr saleexp_to_gr adminexp_of_gr finaexp_of_gr impai_ttm gc_of_gr op_of_gr ebit_of_gr roe roe_waa roe_dt roa npta roic roe_yearly roa2_yearly debt_to_assets assets_to_eqt dp_assets_to_eqt ca_to_assets nca_to_assets tbassets_to_totalassets int_to_talcap eqt_to_talcapital currentdebt_to_debt longdeb_to_debt ocf_to_shortdebt debt_to_eqt eqt_to_debt eqt_to_interestdebt tangibleasset_to_debt tangasset_to_intdebt tangibleasset_to_netdebt ocf_to_debt ocf_to_interestdebt ocf_to_netdebt ebit_to_interest debt_to_tangibleeqt ebitda_to_debt turn_days roa_yearly roe_avg opincome_of_ebt investincome_of_ebt n_op_profit_of_ebt tax_to_ebt dtprofit_to_profit salescash_to_or ocf_to_or ocf_to_opincome capitalized_to_da q_opincome q_investincome q_dtprofit q_eps q_netprofit_margin q_gsprofit_margin q_exp_to_sales q_profit_to_gr q_saleexp_to_gr q_adminexp_to_gr q_finaexp_to_gr q_impair_to_gr q_gc_to_gr q_op_to_gr q_roe q_dt_roe q_npta q_opincome_to_ebt q_investincome_to_ebt q_dtprofit_to_profit q_salescash_to_or q_ocf_to_sales q_ocf_to_or basic_eps_yoy dt_eps_yoy cfps_yoy op_yoy ebt_yoy netprofit_yoy dt_netprofit_yoy ocf_yoy roe_yoy bps_yoy assets_yoy eqt_yoy tr_yoy or_yoy q_gr_yoy q_gr_qoq q_sales_yoy q_sales_qoq q_op_yoy q_op_qoq q_profit_yoy q_profit_qoq q_netprofit_yoy q_netprofit_qoq equity_yoy rd_exp update_flag",
         "ts_code ann_date start_date end_date period offset limit",
         "ts_code end_date ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    # Announced events.
    _cap("forecast", CapabilityGroup.EVENT,
         "ts_code ann_date end_date type p_change_min p_change_max net_profit_min net_profit_max last_parent_net first_ann_date summary change_reason",
         "ts_code ann_date start_date end_date period type offset limit",
         "ts_code end_date ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    _cap("express", CapabilityGroup.EVENT,
         "ts_code ann_date end_date revenue operate_profit total_profit n_income total_assets total_hldr_eqy_exc_min_int diluted_eps diluted_roe yoy_net_profit bps yoy_sales yoy_op yoy_tp yoy_dedu_np yoy_eps yoy_roe growth_assets yoy_equity growth_bps or_last_year op_last_year tp_last_year np_last_year eps_last_year open_net_assets open_bps perf_summary is_audit remark",
         "ts_code ann_date start_date end_date period offset limit",
         "ts_code end_date ann_date", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "start_date end_date")),
    _cap("dividend", CapabilityGroup.EVENT,
         "ts_code end_date ann_date div_proc stk_div stk_bo_rate stk_co_rate cash_div cash_div_tax record_date ex_date pay_date div_listdate imp_ann_date base_date base_share",
         "ts_code ann_date end_date record_date ex_date imp_ann_date offset limit",
         "ts_code end_date ann_date div_proc", PaginationStrategy.SYMBOL_DATE,
         TimeSemantics.ANNOUNCEMENT_AVAILABLE, required_any=("ts_code", "ann_date", "end_date")),
    # Flow/liquidity and macro regime.
    _cap("moneyflow", CapabilityGroup.FLOW_LIQUIDITY,
         "ts_code trade_date buy_sm_vol buy_sm_amount sell_sm_vol sell_sm_amount buy_md_vol buy_md_amount sell_md_vol sell_md_amount buy_lg_vol buy_lg_amount sell_lg_vol sell_lg_amount buy_elg_vol buy_elg_amount sell_elg_vol sell_elg_amount net_mf_vol net_mf_amount",
         "ts_code trade_date start_date end_date", "ts_code trade_date",
         PaginationStrategy.SYMBOL_DATE, TimeSemantics.CLOSE_AVAILABLE,
         required_any=("trade_date", "start_date end_date")),
    _cap("shibor", CapabilityGroup.MACRO_REGIME,
         "date on 1w 2w 1m 3m 6m 9m 1y", "date start_date end_date",
         "date", PaginationStrategy.DATE, TimeSemantics.RELEASE_AVAILABLE,
         required_any=("date", "start_date end_date")),
)


CAPABILITIES: Mapping[str, ApiCapability] = MappingProxyType(
    {capability.api_name: capability for capability in _CAPABILITIES}
)


class CapabilityRegistry:
    """Read-only view of a specific registry version."""

    version = REGISTRY_VERSION

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(CAPABILITIES))

    def get(self, api_name: str) -> ApiCapability:
        try:
            return CAPABILITIES[api_name]
        except KeyError as exc:
            raise QueryRejected("API is outside the approved capability registry") from exc

    def validate(
        self,
        api_name: str,
        *,
        params: Mapping[str, Any],
        fields: tuple[str, ...],
    ) -> ApiCapability:
        capability = self.get(api_name)
        capability.validate(params=params, fields=fields)
        return capability


DEFAULT_REGISTRY = CapabilityRegistry()


__all__ = [
    "ApiCapability",
    "CAPABILITIES",
    "CapabilityGroup",
    "CapabilityRegistry",
    "DEFAULT_REGISTRY",
    "PaginationStrategy",
    "QueryRejected",
    "REGISTRY_VERSION",
    "TimeSemantics",
]
