//! Standalone deterministic Rust kernel & unit test suite for Sabrina Track B (`H1-v2.1-PDLR` + SSM Safety Envelope).
//! Runs completely offline with zero external crates (`cargo test --offline` or `rustc --test`).
//! Optional `--features c_ffi` cross-checks native Rust output directly against `h1_v2_kernel.h`.
//!
//! EXECUTION LOCK: `EXECUTION_AUTHORIZED` is permanently `false`.

#![cfg_attr(not(feature = "c_ffi"), forbid(unsafe_code))]

pub const EXECUTION_AUTHORIZED: bool = false;
pub const MAX_LEVELS: usize = 10;
pub const MIN_HISTORY_CREDIT_S: u64 = 1800;
pub const MAX_BOOK_AGE_S: f64 = 5.0;
pub const MIN_NET_EDGE_BP: f64 = 5.0;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExecMode {
    None = 0,
    MakerPostOnly = 1,
    TakerIoc = 2,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MakerStatus {
    PendingArrival = 0,
    Resting = 1,
    Filled = 2,
    PostOnlyRejected = 3,
    ExpiredTtl = 4,
    CanceledEpochOrCrc = 5,
    CanceledOutsideTop10 = 6,
    StaleBidDriftCanceled = 7,
    SupportPulledCanceled = 8,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ExitReason {
    None = 0,
    HardStopLoss = 1,
    DepthCollapse = 2,
    ScheduledPreClose = 3,
    HorizonMax600s = 4,
    TakeProfit = 5,
    TrailingProfitLock = 6,
    Post180sStall = 7,
    SsmExitNow = 8,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SsmAction {
    Hold = 0,
    EnterMakerPostOnly = 1,
    EnterTakerIoc = 2,
    ExitNow = 3,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SsmRejectReason {
    None = 0,
    InvalidInput = 1,
    HistoryCredit = 2,
    StaleBook = 3,
    HoldOrExit = 4,
    Toxicity = 5,
    ScheduledBlackout = 6,
    SizingOrDepth = 7,
    FrictionOrHurdle = 8,
    SpreadOrMarginalLimit = 9,
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Default)]
pub struct Level {
    pub price: f64,
    pub qty: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Book10 {
    pub bids: [Level; MAX_LEVELS],
    pub asks: [Level; MAX_LEVELS],
    pub n_bids: u32,
    pub n_asks: u32,
    pub recv_ns: u64,
    pub stream_epoch: u64,
    pub crc32_ok: bool,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Params {
    pub f_maker: f64,
    pub f_taker: f64,
    pub tick_size: f64,
    pub lot_size: f64,
    pub order_min_qty: f64,
    pub cost_min_usd: f64,
    pub beta_0_bp: f64,
    pub beta_1: f64,
    pub beta_2: f64,
    pub drift_maker_bp: f64,
    pub drift_taker_bp: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Features {
    pub mid_now: f64,
    pub mid_max_900s_lag30s: f64,
    pub mid_min_600s: f64,
    pub mid_max_600s: f64,
    pub mid_min_60s_lag15s: f64,
    pub b1_min_15s: f64,
    pub b1_min_60s_lag15s: f64,
    pub vwap_900s: f64,
    pub rho5_mean_30s: f64,
    pub rho5_min_10s: f64,
    pub b5_now: f64,
    pub b5_max_30s: f64,
    pub b5_mean_30s: f64,
    pub b5_mean_prev60s: f64,
    pub ofi_norm_30s: f64,
    pub aggr_imb_60s: f64,
    pub trade_count_60s: u32,
    pub vol_usd_60s: f64,
    pub vol_usd_600s: f64,
    pub spread_bp_now: f64,
    pub spread_comp_60s_bp: f64,
    pub history_credit_s: u64,
    pub scheduled_rem_s: f64,
    pub l_buy_s: f64,
    pub l_sell_s: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Friction {
    pub round_trip_fee_bp: f64,
    pub book_walk_bp: f64,
    pub cbe_bp: f64,
    pub valid: bool,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Decision {
    pub mode: ExecMode,
    pub order_qty: f64,
    pub limit_price: f64,
    pub expected_gross_bp: f64,
    pub round_trip_fee_bp: f64,
    pub book_walk_bp: f64,
    pub cbe_bp: f64,
    pub drift_reserve_bp: f64,
    pub net_expected_edge_bp: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct SsmProposal {
    pub action: SsmAction,
    pub expected_gross_edge_bp: f64,
    pub toxicity_prob: f64,
    pub max_toxicity_threshold: f64,
    pub book_age_s: f64,
    pub history_credit_s: u64,
    pub scheduled_rem_s: f64,
    pub l_buy_s: f64,
    pub l_sell_s: f64,
    pub equity_usd: f64,
    pub available_cash_usd: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct SsmEval {
    pub decision: Decision,
    pub reject_reason: SsmRejectReason,
    pub top10_depth_usd: f64,
    pub position_notional_usd: f64,
    pub toxicity_prob: f64,
    pub execution_authorized: bool,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct MakerOrder {
    pub status: MakerStatus,
    pub stream_epoch: u64,
    pub limit_price: f64,
    pub tick_size: f64,
    pub order_qty: f64,
    pub arrive_s: f64,
    pub ttl_s: f64,
    pub q_ahead: f64,
    pub prev_visible_qty: f64,
    pub q_exec_after_ahead: f64,
    pub fill_s: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct Position {
    pub entry_fill_px: f64,
    pub qty: f64,
    pub f_buy: f64,
    pub entry_fill_s: f64,
    pub entry_g_hat_bp: f64,
    pub entry_cbe_bp: f64,
    pub b5_at_fill: f64,
    pub peak_net_bp: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct ExitFeatures {
    pub now_s: f64,
    pub scheduled_rem_s: f64,
    pub l_sell_s: f64,
    pub b5_now: f64,
    pub rho5_mean_30s: f64,
    pub ofi_norm_30s: f64,
    pub ofi_norm_15s: f64,
    pub aggr_imb_60s: f64,
    pub aggr_imb_15s: f64,
}

#[repr(C)]
#[derive(Clone, Copy, Debug)]
pub struct ExitEval {
    pub reason: ExitReason,
    pub bid_vwap_px: f64,
    pub current_net_bp: f64,
    pub updated_peak_net_bp: f64,
}

pub fn validate_params(p: &Params) -> bool {
    p.f_maker >= -0.0010
        && p.f_maker <= 0.0100
        && p.f_taker >= 0.0000
        && p.f_taker <= 0.0100
        && p.tick_size > 0.0
        && p.lot_size > 0.0
        && p.order_min_qty > 0.0
        && p.cost_min_usd >= 0.0
        && p.beta_0_bp >= -50.0
        && p.beta_0_bp <= 10.0
        && p.beta_1 >= 0.25
        && p.beta_1 <= 0.70
        && p.beta_2.abs() <= 1e-12
        && p.drift_maker_bp >= 3.0
        && p.drift_taker_bp >= 5.0
}

pub fn validate_book10(b: &Book10) -> bool {
    let nb = b.n_bids as usize;
    let na = b.n_asks as usize;
    if !b.crc32_ok || !(5..=MAX_LEVELS).contains(&nb) || !(5..=MAX_LEVELS).contains(&na) {
        return false;
    }
    for i in 0..nb {
        if !(b.bids[i].price > 0.0 && b.bids[i].qty >= 0.0) {
            return false;
        }
        if i > 0 && !(b.bids[i].price < b.bids[i - 1].price) {
            return false;
        }
    }
    for i in 0..na {
        if !(b.asks[i].price > 0.0 && b.asks[i].qty >= 0.0) {
            return false;
        }
        if i > 0 && !(b.asks[i].price > b.asks[i - 1].price) {
            return false;
        }
    }
    b.bids[0].price < b.asks[0].price
}

pub fn book_is_fresh(b: &Book10, now_ns: u64, max_age_s: f64) -> bool {
    if !validate_book10(b) || !max_age_s.is_finite() || max_age_s < 0.0 || now_ns < b.recv_ns {
        return false;
    }
    let age_s = ((now_ns - b.recv_ns) as f64) * 1e-9;
    age_s <= max_age_s + 1e-12
}

pub fn book_vwap(levels: &[Level], qty: f64, is_bid: bool) -> f64 {
    if !(qty > 0.0) || levels.is_empty() || levels.len() > MAX_LEVELS {
        return -1.0;
    }
    let mut rem = qty;
    let mut notional = 0.0;
    for (i, lv) in levels.iter().enumerate() {
        if !(lv.price > 0.0 && lv.qty >= 0.0) {
            return -1.0;
        }
        if i > 0 {
            if is_bid && !(lv.price < levels[i - 1].price) {
                return -1.0;
            }
            if !is_bid && !(lv.price > levels[i - 1].price) {
                return -1.0;
            }
        }
        if rem > 0.0 {
            let take = rem.min(lv.qty);
            notional += take * lv.price;
            rem -= take;
        }
    }
    if rem <= 1e-12 { notional / qty } else { -1.0 }
}

/// Patch 4/5: Marginal limit-price-capped ask walk for TAKER_IOC.
pub fn ask_vwap_capped(asks: &[Level], qty: f64, limit_px: f64) -> f64 {
    if !(qty > 0.0 && limit_px > 0.0) || !limit_px.is_finite() || asks.is_empty() || asks.len() > MAX_LEVELS {
        return -1.0;
    }
    let mut rem = qty;
    let mut notional = 0.0;
    for (i, lv) in asks.iter().enumerate() {
        if !(lv.price > 0.0 && lv.qty >= 0.0) {
            return -1.0;
        }
        if i > 0 && !(lv.price > asks[i - 1].price) {
            return -1.0;
        }
        if rem > 0.0 && lv.price <= limit_px + 1e-12 {
            let take = rem.min(lv.qty);
            notional += take * lv.price;
            rem -= take;
        }
    }
    if rem <= 1e-12 { notional / qty } else { -1.0 }
}

/// Patch 5: Limit-price-capped book walk returning `(vwap, marginal_px)` (or `(-1.0, -1.0)` if insufficient).
pub fn book_vwap_with_limit(levels: &[Level], qty: f64, limit_price: f64, is_bid: bool) -> (f64, f64) {
    if !(qty > 0.0 && limit_price > 0.0)
        || !limit_price.is_finite()
        || levels.is_empty()
        || levels.len() > MAX_LEVELS
    {
        return (-1.0, -1.0);
    }
    let mut rem = qty;
    let mut notional = 0.0;
    let mut marginal_px = -1.0;
    for (i, lv) in levels.iter().enumerate() {
        let px = lv.price;
        let lv_qty = lv.qty;
        if !(px > 0.0 && lv_qty >= 0.0) {
            return (-1.0, -1.0);
        }
        if i > 0 {
            if is_bid && !(px < levels[i - 1].price) {
                return (-1.0, -1.0);
            }
            if !is_bid && !(px > levels[i - 1].price) {
                return (-1.0, -1.0);
            }
        }
        if rem > 0.0 {
            let allowed = if is_bid {
                px >= limit_price - 1e-12
            } else {
                px <= limit_price + 1e-12
            };
            if !allowed {
                break;
            }
            if lv_qty > 0.0 {
                let take = rem.min(lv_qty);
                notional += take * px;
                rem -= take;
                marginal_px = px;
            }
        }
    }
    if rem <= 1e-12 {
        (notional / qty, marginal_px)
    } else {
        (-1.0, -1.0)
    }
}

pub fn sum_notional_usd(levels: &[Level]) -> f64 {
    levels.iter().take(MAX_LEVELS).map(|x| x.price * x.qty).sum()
}

/// Patch M-4: Deeper passive bid support preserving exact round_trip_fee_bp + book_walk_bp == cbe_bp.
pub fn compute_friction(entry_ref_px: f64, exit_bid_vwap_px: f64, f_buy: f64, f_sell_taker: f64) -> Friction {
    let invalid = Friction { round_trip_fee_bp: 0.0, book_walk_bp: 0.0, cbe_bp: 0.0, valid: false };
    if !(entry_ref_px > 0.0 && exit_bid_vwap_px > 0.0) {
        return invalid;
    }
    if !((-0.0010..=0.0100).contains(&f_buy) && (0.0..1.0).contains(&f_sell_taker)) {
        return invalid;
    }
    let round_trip_fee_bp = 10000.0 * ((1.0 + f_buy) / (1.0 - f_sell_taker) - 1.0);
    let cbe_bp = 10000.0 * ((entry_ref_px * (1.0 + f_buy)) / (exit_bid_vwap_px * (1.0 - f_sell_taker)) - 1.0);
    let book_walk_bp = cbe_bp - round_trip_fee_bp;
    Friction {
        round_trip_fee_bp,
        book_walk_bp,
        cbe_bp,
        valid: round_trip_fee_bp.is_finite() && cbe_bp.is_finite() && book_walk_bp.is_finite(),
    }
}

fn empty_decision() -> Decision {
    Decision {
        mode: ExecMode::None,
        order_qty: 0.0,
        limit_price: 0.0,
        expected_gross_bp: 0.0,
        round_trip_fee_bp: 0.0,
        book_walk_bp: 0.0,
        cbe_bp: 0.0,
        drift_reserve_bp: 0.0,
        net_expected_edge_bp: -1e9,
    }
}

pub fn evaluate_entry(book: &Book10, f: &Features, p: &Params, available_cash_usd: f64) -> Decision {
    let none = empty_decision();
    if !validate_book10(book) || !validate_params(p) || !(available_cash_usd > 0.0) || f.history_credit_s < MIN_HISTORY_CREDIT_S {
        return none;
    }
    if !(f.mid_max_900s_lag30s > 0.0 && f.mid_min_600s > 0.0 && f.mid_now > 0.0) {
        return none;
    }
    let disloc_bp = 10000.0 * (1.0 - f.mid_now / f.mid_max_900s_lag30s);
    let range_600_bp = 10000.0 * (f.mid_max_600s / f.mid_min_600s - 1.0);
    if disloc_bp < 220.0 || range_600_bp < 260.0 {
        return none;
    }
    if f.mid_now < f.mid_min_60s_lag15s * 1.0015 || f.b1_min_15s < f.b1_min_60s_lag15s {
        return none;
    }
    if f.rho5_mean_30s < 0.12 || f.rho5_min_10s < 0.05 {
        return none;
    }
    if f.b5_max_30s <= 0.0 || f.b5_now < 0.70 * f.b5_max_30s {
        return none;
    }
    if f.b5_mean_prev60s <= 0.0 || f.b5_mean_30s < 1.15 * f.b5_mean_prev60s {
        return none;
    }
    if f.ofi_norm_30s < 0.25 || f.aggr_imb_60s < 0.15 || f.trade_count_60s < 4 {
        return none;
    }
    if f.vol_usd_600s <= 0.0 || (f.vol_usd_60s * 10.0) < 0.80 * f.vol_usd_600s {
        return none;
    }
    if f.spread_bp_now < 0.0 || f.spread_bp_now > 35.0 || (f.spread_bp_now > 15.0 && f.spread_comp_60s_bp < 5.0) {
        return none;
    }

    let nb = book.n_bids as usize;
    let na = book.n_asks as usize;
    let sum_bids: f64 = book.bids[..nb].iter().map(|x| x.qty).sum();
    let sum_asks: f64 = book.asks[..na].iter().map(|x| x.qty).sum();
    let r_usd = 0.25 * available_cash_usd;
    let depth_cap_qty = 0.10 * sum_bids.min(sum_asks);
    let depth_cap_usd = 0.10 * sum_notional_usd(&book.bids[..nb]).min(sum_notional_usd(&book.asks[..na]));
    let d_eff = disloc_bp.min(1200.0);

    let mut maker_out = none;
    if (book.asks[0].price - book.bids[0].price) >= (p.tick_size - 1e-12)
        && f.scheduled_rem_s >= (180.0 + 30.0 + f.l_buy_s + f.l_sell_s + 10.0)
    {
        let p_post = book.bids[0].price;
        let fee_pos = p.f_maker.max(0.0);
        let raw_q = (r_usd / (p_post * (1.0 + fee_pos))).min(depth_cap_usd / p_post).min(depth_cap_qty);
        let q = ((raw_q + 1e-12) / p.lot_size).floor() * p.lot_size;
        let bid_vwap = book_vwap(&book.bids[..nb], q, true);
        if q >= p.order_min_qty
            && bid_vwap > 0.0
            && (q * p_post) >= p.cost_min_usd
            && (q * p_post) <= depth_cap_usd + 1e-9
            && (q * p_post * (1.0 + fee_pos)) <= r_usd + 1e-9
        {
            let fr = compute_friction(p_post, bid_vwap, p.f_maker, p.f_taker);
            if fr.valid {
                let vwap_gap = 10000.0 * (0.0_f64).max((f.vwap_900s - p_post) / p_post);
                let anchor = 0.60 * vwap_gap + 0.40 * d_eff;
                let g_hat = (p.beta_0_bp + p.beta_1 * anchor).clamp(0.0, 800.0);
                let edge = g_hat - fr.book_walk_bp - fr.round_trip_fee_bp - p.drift_maker_bp;
                if edge >= MIN_NET_EDGE_BP {
                    maker_out = Decision {
                        mode: ExecMode::MakerPostOnly,
                        order_qty: q,
                        limit_price: p_post,
                        expected_gross_bp: g_hat,
                        round_trip_fee_bp: fr.round_trip_fee_bp,
                        book_walk_bp: fr.book_walk_bp,
                        cbe_bp: fr.cbe_bp,
                        drift_reserve_bp: p.drift_maker_bp,
                        net_expected_edge_bp: edge,
                    };
                }
            }
        }
    }

    if maker_out.mode == ExecMode::MakerPostOnly
        && disloc_bp >= 280.0
        && f.ofi_norm_30s >= 1.25
        && f.aggr_imb_60s >= 0.40
        && f.scheduled_rem_s >= (180.0 + f.l_buy_s + f.l_sell_s + 10.0)
    {
        let raw_q = (r_usd / (book.asks[0].price * (1.0 + p.f_taker)))
            .min(depth_cap_usd / book.asks[0].price)
            .min(depth_cap_qty);
        let mut q = ((raw_q + 1e-12) / p.lot_size).floor() * p.lot_size;
        let mut ask_vwap = book_vwap(&book.asks[..na], q, false);
        let mut bid_vwap = book_vwap(&book.bids[..nb], q, true);
        if ask_vwap > 0.0 && ((q * ask_vwap * (1.0 + p.f_taker)) > r_usd + 1e-9 || (q * ask_vwap) > depth_cap_usd + 1e-9) {
            let max_q_cash = r_usd / (ask_vwap * (1.0 + p.f_taker));
            let max_q_depth = depth_cap_usd / ask_vwap;
            q = ((max_q_cash.min(max_q_depth) + 1e-12) / p.lot_size).floor() * p.lot_size;
            ask_vwap = book_vwap(&book.asks[..na], q, false);
            bid_vwap = book_vwap(&book.bids[..nb], q, true);
        }
        if q >= p.order_min_qty
            && ask_vwap > 0.0
            && bid_vwap > 0.0
            && (q * ask_vwap) >= p.cost_min_usd
            && (q * ask_vwap) <= depth_cap_usd + 1e-9
            && (q * ask_vwap * (1.0 + p.f_taker)) <= r_usd + 1e-9
        {
            let fr = compute_friction(ask_vwap, bid_vwap, p.f_taker, p.f_taker);
            if fr.valid {
                let vwap_gap = 10000.0 * (0.0_f64).max((f.vwap_900s - ask_vwap) / ask_vwap);
                let anchor = 0.60 * vwap_gap + 0.40 * d_eff;
                let g_hat = (p.beta_0_bp + p.beta_1 * anchor).clamp(0.0, 800.0);
                let edge = g_hat - fr.book_walk_bp - fr.round_trip_fee_bp - p.drift_taker_bp;
                if edge >= MIN_NET_EDGE_BP {
                    let slip_cap = (0.0025_f64).max(0.35 * edge * 1e-4);
                    let limit_px = ((book.asks[0].price * (1.0 + slip_cap) + 1e-12) / p.tick_size).floor() * p.tick_size;
                    if ask_vwap_capped(&book.asks[..na], q, limit_px) > 0.0 {
                        return Decision {
                            mode: ExecMode::TakerIoc,
                            order_qty: q,
                            limit_price: limit_px,
                            expected_gross_bp: g_hat,
                            round_trip_fee_bp: fr.round_trip_fee_bp,
                            book_walk_bp: fr.book_walk_bp,
                            cbe_bp: fr.cbe_bp,
                            drift_reserve_bp: p.drift_taker_bp,
                            net_expected_edge_bp: edge,
                        };
                    }
                }
            }
        }
    }

    maker_out
}

/// Deliverable 3: Deterministic Rust mirror of `h1v2_evaluate_ssm_proposal`.
pub fn evaluate_ssm_proposal(book: &Book10, prop: &SsmProposal, p: &Params) -> SsmEval {
    let mut ev = SsmEval {
        decision: empty_decision(),
        reject_reason: SsmRejectReason::InvalidInput,
        top10_depth_usd: 0.0,
        position_notional_usd: 0.0,
        toxicity_prob: prop.toxicity_prob,
        execution_authorized: EXECUTION_AUTHORIZED,
    };
    if !validate_book10(book) || !validate_params(p) {
        return ev;
    }
    if !prop.expected_gross_edge_bp.is_finite()
        || !prop.toxicity_prob.is_finite()
        || !prop.max_toxicity_threshold.is_finite()
        || !prop.book_age_s.is_finite()
        || !prop.l_buy_s.is_finite()
        || !prop.l_sell_s.is_finite()
        || !prop.equity_usd.is_finite()
        || !prop.available_cash_usd.is_finite()
        || !(prop.equity_usd > 0.0 && prop.available_cash_usd > 0.0)
        || prop.l_buy_s < 0.0
        || prop.l_sell_s < 0.0
    {
        return ev;
    }

    let nb = book.n_bids as usize;
    let na = book.n_asks as usize;
    let top10_depth_usd = sum_notional_usd(&book.bids[..nb]).min(sum_notional_usd(&book.asks[..na]));
    ev.top10_depth_usd = top10_depth_usd;

    if prop.history_credit_s < MIN_HISTORY_CREDIT_S {
        ev.reject_reason = SsmRejectReason::HistoryCredit;
        return ev;
    }
    if !(prop.book_age_s >= 0.0 && prop.book_age_s <= MAX_BOOK_AGE_S + 1e-12) {
        ev.reject_reason = SsmRejectReason::StaleBook;
        return ev;
    }
    if prop.action != SsmAction::EnterMakerPostOnly && prop.action != SsmAction::EnterTakerIoc {
        ev.reject_reason = SsmRejectReason::HoldOrExit;
        return ev;
    }
    if !(prop.max_toxicity_threshold >= 0.0 && prop.max_toxicity_threshold <= 1.0)
        || !(prop.toxicity_prob >= 0.0 && prop.toxicity_prob <= prop.max_toxicity_threshold + 1e-12)
    {
        ev.reject_reason = SsmRejectReason::Toxicity;
        return ev;
    }

    let equity_cap_usd = 0.25 * prop.equity_usd.min(prop.available_cash_usd);
    let depth_cap_usd = 0.10 * top10_depth_usd;
    let sum_bids: f64 = book.bids[..nb].iter().map(|x| x.qty).sum();
    let sum_asks: f64 = book.asks[..na].iter().map(|x| x.qty).sum();
    let depth_cap_qty = 0.10 * sum_bids.min(sum_asks);
    let g_hat = prop.expected_gross_edge_bp.clamp(0.0, 800.0);

    if prop.action == SsmAction::EnterMakerPostOnly {
        if prop.scheduled_rem_s < (180.0 + 30.0 + prop.l_buy_s + prop.l_sell_s + 10.0) {
            ev.reject_reason = SsmRejectReason::ScheduledBlackout;
            return ev;
        }
        if (book.asks[0].price - book.bids[0].price) < (p.tick_size - 1e-12) {
            ev.reject_reason = SsmRejectReason::SpreadOrMarginalLimit;
            return ev;
        }
        let p_post = book.bids[0].price;
        let fee_pos = p.f_maker.max(0.0);
        let raw_q = (equity_cap_usd / (p_post * (1.0 + fee_pos)))
            .min(depth_cap_usd / p_post)
            .min(depth_cap_qty);
        let q = ((raw_q + 1e-12) / p.lot_size).floor() * p.lot_size;
        let notional = q * p_post;
        let debit = notional * (1.0 + fee_pos);
        let bid_vwap = book_vwap(&book.bids[..nb], q, true);
        if !(q >= p.order_min_qty
            && bid_vwap > 0.0
            && notional >= p.cost_min_usd
            && notional <= depth_cap_usd + 1e-9
            && debit <= equity_cap_usd + 1e-9)
        {
            ev.reject_reason = SsmRejectReason::SizingOrDepth;
            return ev;
        }
        let fr = compute_friction(p_post, bid_vwap, p.f_maker, p.f_taker);
        if !fr.valid {
            ev.reject_reason = SsmRejectReason::FrictionOrHurdle;
            return ev;
        }
        let edge_after_cbe = g_hat - fr.cbe_bp;
        let net_edge = edge_after_cbe - p.drift_maker_bp;
        if edge_after_cbe < MIN_NET_EDGE_BP || net_edge < MIN_NET_EDGE_BP {
            ev.reject_reason = SsmRejectReason::FrictionOrHurdle;
            return ev;
        }
        ev.decision = Decision {
            mode: ExecMode::MakerPostOnly,
            order_qty: q,
            limit_price: p_post,
            expected_gross_bp: g_hat,
            round_trip_fee_bp: fr.round_trip_fee_bp,
            book_walk_bp: fr.book_walk_bp,
            cbe_bp: fr.cbe_bp,
            drift_reserve_bp: p.drift_maker_bp,
            net_expected_edge_bp: net_edge,
        };
        ev.position_notional_usd = notional;
        ev.reject_reason = SsmRejectReason::None;
        return ev;
    }

    // SsmAction::EnterTakerIoc
    if prop.scheduled_rem_s < (180.0 + prop.l_buy_s + prop.l_sell_s + 10.0) {
        ev.reject_reason = SsmRejectReason::ScheduledBlackout;
        return ev;
    }
    let ask1 = book.asks[0].price;
    let raw_q = (equity_cap_usd / (ask1 * (1.0 + p.f_taker)))
        .min(depth_cap_usd / ask1)
        .min(depth_cap_qty);
    let mut q = ((raw_q + 1e-12) / p.lot_size).floor() * p.lot_size;
    let mut ask_vwap = book_vwap(&book.asks[..na], q, false);
    let mut bid_vwap = book_vwap(&book.bids[..nb], q, true);
    if ask_vwap > 0.0
        && ((q * ask_vwap * (1.0 + p.f_taker)) > equity_cap_usd + 1e-9 || (q * ask_vwap) > depth_cap_usd + 1e-9)
    {
        let max_q_eq = equity_cap_usd / (ask_vwap * (1.0 + p.f_taker));
        let max_q_dp = depth_cap_usd / ask_vwap;
        q = ((max_q_eq.min(max_q_dp) + 1e-12) / p.lot_size).floor() * p.lot_size;
        ask_vwap = book_vwap(&book.asks[..na], q, false);
        bid_vwap = book_vwap(&book.bids[..nb], q, true);
    }
    let notional = if ask_vwap > 0.0 { q * ask_vwap } else { 0.0 };
    let debit = notional * (1.0 + p.f_taker);
    if !(q >= p.order_min_qty
        && ask_vwap > 0.0
        && bid_vwap > 0.0
        && notional >= p.cost_min_usd
        && notional <= depth_cap_usd + 1e-9
        && debit <= equity_cap_usd + 1e-9)
    {
        ev.reject_reason = SsmRejectReason::SizingOrDepth;
        return ev;
    }
    let fr = compute_friction(ask_vwap, bid_vwap, p.f_taker, p.f_taker);
    if !fr.valid {
        ev.reject_reason = SsmRejectReason::FrictionOrHurdle;
        return ev;
    }
    let edge_after_cbe = g_hat - fr.cbe_bp;
    let net_edge = edge_after_cbe - p.drift_taker_bp;
    if edge_after_cbe < MIN_NET_EDGE_BP || net_edge < MIN_NET_EDGE_BP {
        ev.reject_reason = SsmRejectReason::FrictionOrHurdle;
        return ev;
    }
    let slip_cap = (0.0025_f64).max(0.35 * net_edge * 1e-4);
    let limit_px = ((ask1 * (1.0 + slip_cap) + 1e-12) / p.tick_size).floor() * p.tick_size;
    if !(ask_vwap_capped(&book.asks[..na], q, limit_px) > 0.0) {
        ev.reject_reason = SsmRejectReason::SpreadOrMarginalLimit;
        return ev;
    }
    ev.decision = Decision {
        mode: ExecMode::TakerIoc,
        order_qty: q,
        limit_price: limit_px,
        expected_gross_bp: g_hat,
        round_trip_fee_bp: fr.round_trip_fee_bp,
        book_walk_bp: fr.book_walk_bp,
        cbe_bp: fr.cbe_bp,
        drift_reserve_bp: p.drift_taker_bp,
        net_expected_edge_bp: net_edge,
    };
    ev.position_notional_usd = notional;
    ev.reject_reason = SsmRejectReason::None;
    ev
}

pub fn maker_arrive(
    arrival_book: &Book10,
    expected_epoch: u64,
    limit_price: f64,
    tick_size: f64,
    order_qty: f64,
    arrive_s: f64,
    ttl_s: f64,
) -> MakerOrder {
    let mut ord = MakerOrder {
        status: MakerStatus::PendingArrival,
        stream_epoch: expected_epoch,
        limit_price,
        tick_size,
        order_qty,
        arrive_s,
        ttl_s,
        q_ahead: 0.0,
        prev_visible_qty: 0.0,
        q_exec_after_ahead: 0.0,
        fill_s: -1.0,
    };
    if !validate_book10(arrival_book)
        || arrival_book.stream_epoch != expected_epoch
        || !(limit_price > 0.0 && tick_size > 0.0 && order_qty > 0.0)
    {
        ord.status = MakerStatus::CanceledEpochOrCrc;
        return ord;
    }
    if limit_price >= arrival_book.asks[0].price - 0.25 * tick_size {
        ord.status = MakerStatus::PostOnlyRejected;
        return ord;
    }
    if arrival_book.bids[0].price < limit_price - 0.25 * tick_size {
        ord.status = MakerStatus::StaleBidDriftCanceled;
        return ord;
    }
    let nb = arrival_book.n_bids as usize;
    if limit_price < arrival_book.bids[nb - 1].price - 0.25 * tick_size {
        ord.status = MakerStatus::CanceledOutsideTop10;
        return ord;
    }
    let mut ahead = 0.0;
    for lv in &arrival_book.bids[..nb] {
        if (lv.price - limit_price).abs() <= 0.25 * tick_size {
            ahead = lv.qty;
            break;
        }
    }
    if ahead <= 0.0 {
        ord.status = MakerStatus::SupportPulledCanceled;
        return ord;
    }
    ord.q_ahead = ahead;
    ord.prev_visible_qty = ahead;
    ord.status = MakerStatus::Resting;
    ord
}

pub fn maker_on_book(ord: &mut MakerOrder, book: &Book10, now_s: f64) -> MakerStatus {
    if ord.status != MakerStatus::Resting {
        return ord.status;
    }
    if !validate_book10(book) || book.stream_epoch != ord.stream_epoch {
        ord.status = MakerStatus::CanceledEpochOrCrc;
        return ord.status;
    }
    if now_s > ord.arrive_s + ord.ttl_s {
        ord.status = MakerStatus::ExpiredTtl;
        return ord.status;
    }
    if book.asks[0].price <= ord.limit_price + 0.25 * ord.tick_size {
        ord.status = MakerStatus::Filled;
        ord.fill_s = now_s;
        return ord.status;
    }
    let mut visible_at_px = 0.0;
    for lv in &book.bids[..(book.n_bids as usize)] {
        if (lv.price - ord.limit_price).abs() <= 0.25 * ord.tick_size {
            visible_at_px = lv.qty;
            break;
        }
    }
    if visible_at_px <= 1e-12 {
        ord.status = MakerStatus::SupportPulledCanceled;
        return ord.status;
    }
    if ord.prev_visible_qty > 0.0 && visible_at_px < ord.prev_visible_qty {
        ord.q_ahead *= visible_at_px / ord.prev_visible_qty;
    }
    ord.prev_visible_qty = visible_at_px;
    ord.status
}

pub fn maker_on_trade(
    ord: &mut MakerOrder,
    trade_epoch: u64,
    trade_price: f64,
    trade_qty: f64,
    is_sell_aggressor: bool,
    now_s: f64,
) -> MakerStatus {
    if ord.status != MakerStatus::Resting {
        return ord.status;
    }
    if trade_epoch != ord.stream_epoch || !(trade_price > 0.0 && trade_qty > 0.0) {
        ord.status = MakerStatus::CanceledEpochOrCrc;
        return ord.status;
    }
    if now_s > ord.arrive_s + ord.ttl_s {
        ord.status = MakerStatus::ExpiredTtl;
        return ord.status;
    }
    if trade_price < ord.limit_price - 0.25 * ord.tick_size {
        ord.status = MakerStatus::Filled;
        ord.fill_s = now_s;
        return ord.status;
    }
    if is_sell_aggressor && (trade_price - ord.limit_price).abs() <= 0.25 * ord.tick_size {
        let consume_ahead = ord.q_ahead.min(trade_qty);
        ord.q_ahead -= consume_ahead;
        ord.prev_visible_qty = (ord.prev_visible_qty - trade_qty).max(0.0);
        ord.q_exec_after_ahead += trade_qty - consume_ahead;
        if ord.q_exec_after_ahead + 1e-12 >= ord.order_qty {
            ord.status = MakerStatus::Filled;
            ord.fill_s = now_s;
        }
    }
    ord.status
}

pub fn evaluate_exit(pos: &mut Position, book: &Book10, ef: &ExitFeatures, f_taker: f64) -> ExitEval {
    let mut ev = ExitEval {
        reason: ExitReason::None,
        bid_vwap_px: -1.0,
        current_net_bp: -10000.0,
        updated_peak_net_bp: pos.peak_net_bp,
    };
    if !(pos.qty > 0.0 && pos.entry_fill_px > 0.0) {
        return ev;
    }
    if !validate_book10(book) {
        ev.reason = ExitReason::DepthCollapse;
        return ev;
    }
    let bid_vwap = book_vwap(&book.bids[..book.n_bids as usize], pos.qty, true);
    if bid_vwap <= 0.0 {
        ev.reason = ExitReason::DepthCollapse;
        return ev;
    }
    let entry_debit = pos.qty * pos.entry_fill_px * (1.0 + pos.f_buy);
    let net_liq = pos.qty * bid_vwap * (1.0 - f_taker);
    let r_net_bp = 10000.0 * (net_liq / entry_debit - 1.0);
    if r_net_bp > pos.peak_net_bp {
        pos.peak_net_bp = r_net_bp;
    }
    ev.bid_vwap_px = bid_vwap;
    ev.current_net_bp = r_net_bp;
    ev.updated_peak_net_bp = pos.peak_net_bp;

    let held_s = ef.now_s - pos.entry_fill_s;
    let tp_target_bp = (60.0_f64).max(0.75 * (pos.entry_g_hat_bp - pos.entry_cbe_bp));

    ev.reason = if r_net_bp <= -260.0 {
        ExitReason::HardStopLoss
    } else if ef.b5_now < 0.30 * pos.b5_at_fill && ef.aggr_imb_60s <= -0.45 {
        ExitReason::DepthCollapse
    } else if ef.scheduled_rem_s <= (ef.l_sell_s + 10.0) {
        ExitReason::ScheduledPreClose
    } else if held_s >= 600.0 {
        ExitReason::HorizonMax600s
    } else if r_net_bp >= tp_target_bp && (ef.ofi_norm_15s <= 0.0 || ef.aggr_imb_15s <= 0.0) {
        ExitReason::TakeProfit
    } else if pos.peak_net_bp >= 50.0 && r_net_bp <= (15.0_f64).max(pos.peak_net_bp - 45.0) {
        ExitReason::TrailingProfitLock
    } else if held_s >= 180.0 && ef.rho5_mean_30s < -0.05 && ef.aggr_imb_60s < -0.15 && ef.ofi_norm_30s < 0.0 {
        ExitReason::Post180sStall
    } else {
        ExitReason::None
    };
    ev
}

pub fn evaluate_ssm_exit(
    pos: &mut Position,
    book: &Book10,
    ef: &ExitFeatures,
    f_taker: f64,
    ssm_action: SsmAction,
) -> ExitEval {
    let mut ev = evaluate_exit(pos, book, ef, f_taker);
    if ev.reason == ExitReason::None && ssm_action == SsmAction::ExitNow && pos.qty > 0.0 {
        ev.reason = ExitReason::SsmExitNow;
    }
    ev
}

pub fn strict_epoch_break_proceeds(last_book: &Book10, open_qty: f64, f_taker: f64, drift_taker_bp: f64) -> (f64, f64) {
    if !validate_book10(last_book) || !(open_qty > 0.0) {
        return (0.0, open_qty.max(0.0));
    }
    let mut rem = open_qty;
    let mut gross_usd = 0.0;
    for lv in &last_book.bids[..(last_book.n_bids as usize).min(MAX_LEVELS)] {
        if rem <= 0.0 {
            break;
        }
        let take = rem.min(lv.qty);
        gross_usd += take * lv.price;
        rem -= take;
    }
    let unrec = if rem > 1e-12 { rem } else { 0.0 };
    let penalty = (drift_taker_bp * 1e-4).max(0.0025);
    (gross_usd * (1.0 - f_taker) * (1.0 - penalty), unrec)
}

#[cfg(feature = "c_ffi")]
mod ffi {
    use super::*;
    extern "C" {
        pub fn c_h1v2_book_is_fresh(b: *const Book10, now_ns: u64, max_age_s: f64) -> bool;
        pub fn c_h1v2_book_vwap(levels: *const Level, n: u32, qty: f64, is_bid: bool) -> f64;
        pub fn c_h1v2_ask_vwap_capped(asks: *const Level, n: u32, qty: f64, limit_px: f64) -> f64;
        pub fn c_h1v2_book_vwap_with_limit(
            levels: *const Level,
            n: u32,
            qty: f64,
            limit_price: f64,
            is_bid: bool,
            marginal_px_out: *mut f64,
        ) -> f64;
        pub fn c_h1v2_compute_friction(entry_px: f64, bid_vwap: f64, f_buy: f64, f_sell: f64) -> Friction;
        pub fn c_h1v2_evaluate_entry(book: *const Book10, f: *const Features, p: *const Params, cash: f64) -> Decision;
        pub fn c_h1v2_evaluate_ssm_proposal(book: *const Book10, prop: *const SsmProposal, p: *const Params) -> SsmEval;
        pub fn c_h1v2_maker_arrive(
            arrival_book: *const Book10,
            expected_epoch: u64,
            limit_price: f64,
            tick_size: f64,
            order_qty: f64,
            arrive_s: f64,
            ttl_s: f64,
        ) -> MakerOrder;
        pub fn c_h1v2_maker_on_book(ord: *mut MakerOrder, book: *const Book10, now_s: f64) -> MakerStatus;
        pub fn c_h1v2_maker_on_trade(
            ord: *mut MakerOrder,
            trade_epoch: u64,
            trade_price: f64,
            trade_qty: f64,
            is_sell_aggressor: bool,
            now_s: f64,
        ) -> MakerStatus;
        pub fn c_h1v2_evaluate_exit(pos: *mut Position, book: *const Book10, ef: *const ExitFeatures, f_taker: f64) -> ExitEval;
        pub fn c_h1v2_evaluate_ssm_exit(
            pos: *mut Position,
            book: *const Book10,
            ef: *const ExitFeatures,
            f_taker: f64,
            ssm_action: SsmAction,
        ) -> ExitEval;
        pub fn c_h1v2_strict_epoch_break_proceeds(
            last_book: *const Book10,
            open_qty: f64,
            f_taker: f64,
            drift_taker_bp: f64,
            unrec_out: *mut f64,
        ) -> f64;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ref_book() -> Book10 {
        let mut bids = [Level::default(); MAX_LEVELS];
        let mut asks = [Level::default(); MAX_LEVELS];
        for i in 0..MAX_LEVELS {
            bids[i] = Level { price: 1.0000 - 0.0004 * (i as f64), qty: 5.0 + (i as f64) };
            asks[i] = Level { price: 1.0018 + 0.0005 * (i as f64), qty: 4.0 + (i as f64) };
        }
        Book10 { bids, asks, n_bids: 10, n_asks: 10, recv_ns: 2_000_000_000_000, stream_epoch: 1, crc32_ok: true }
    }

    fn ref_params() -> Params {
        Params {
            f_maker: 0.0040,
            f_taker: 0.0080,
            tick_size: 0.0001,
            lot_size: 0.01,
            order_min_qty: 0.50,
            cost_min_usd: 1.00,
            beta_0_bp: -10.0,
            beta_1: 0.50,
            beta_2: 0.0,
            drift_maker_bp: 6.0,
            drift_taker_bp: 12.0,
        }
    }

    fn ref_features() -> Features {
        Features {
            mid_now: 1.0009,
            mid_max_900s_lag30s: 1.0372020725,
            mid_min_600s: 0.9980,
            mid_max_600s: 1.0372,
            mid_min_60s_lag15s: 0.9985,
            b1_min_15s: 0.9995,
            b1_min_60s_lag15s: 0.9980,
            vwap_900s: 1.0320,
            rho5_mean_30s: 0.25,
            rho5_min_10s: 0.10,
            b5_now: 12.0,
            b5_max_30s: 15.0,
            b5_mean_30s: 12.5,
            b5_mean_prev60s: 9.5,
            ofi_norm_30s: 0.85,
            aggr_imb_60s: 0.30,
            trade_count_60s: 9,
            vol_usd_60s: 450.0,
            vol_usd_600s: 3200.0,
            spread_bp_now: 18.0,
            spread_comp_60s_bp: 9.0,
            history_credit_s: 1800,
            scheduled_rem_s: 620.0,
            l_buy_s: 4.315,
            l_sell_s: 0.695,
        }
    }

    fn ref_ssm_proposal(action: SsmAction, expected_gross_edge_bp: f64) -> SsmProposal {
        SsmProposal {
            action,
            expected_gross_edge_bp,
            toxicity_prob: 0.15,
            max_toxicity_threshold: 0.35,
            book_age_s: 1.2,
            history_credit_s: 1800,
            scheduled_rem_s: 620.0,
            l_buy_s: 4.315,
            l_sell_s: 0.695,
            equity_usd: 12.19,
            available_cash_usd: 12.19,
        }
    }

    #[test]
    fn test_10level_vwap_marginal_cap_and_no_double_count() {
        let b = ref_book();
        let ask_vwap = book_vwap(&b.asks, 10.0, false);
        let expected = (4.0 * 1.0018 + 5.0 * 1.0023 + 1.0 * 1.0028) / 10.0;
        assert!((ask_vwap - expected).abs() < 1e-12);
        assert_eq!(book_vwap(&b.bids, 95.01, true), -1.0);
        assert_eq!(ask_vwap_capped(&b.asks, 5.0, 1.0021), -1.0);
        assert!((ask_vwap_capped(&b.asks, 4.0, 1.0021) - 1.0018).abs() < 1e-12);

        let (vwap_a, marg_a) = book_vwap_with_limit(&b.asks, 9.0, 1.0023, false);
        assert!((vwap_a - (4.0 * 1.0018 + 5.0 * 1.0023) / 9.0).abs() < 1e-12);
        assert!((marg_a - 1.0023).abs() < 1e-12);
        assert_eq!(book_vwap_with_limit(&b.asks, 10.0, 1.0023, false), (-1.0, -1.0));
        let (vwap_b, marg_b) = book_vwap_with_limit(&b.bids, 11.0, 0.9996, true);
        assert!((vwap_b - (5.0 * 1.0000 + 6.0 * 0.9996) / 11.0).abs() < 1e-12);
        assert!((marg_b - 0.9996).abs() < 1e-12);
        assert_eq!(book_vwap_with_limit(&b.bids, 12.0, 0.9996, true), (-1.0, -1.0));

        for (fb, fs) in [(0.0040, 0.0080), (0.0080, 0.0080), (-0.0002, 0.0040)] {
            let fr = compute_friction(1.0020, 0.9995, fb, fs);
            assert!(fr.valid);
            assert!(((fr.round_trip_fee_bp + fr.book_walk_bp) - fr.cbe_bp).abs() < 1e-12);
        }
        let deeper = compute_friction(0.9996, 1.0000, 0.0040, 0.0080);
        assert!(deeper.valid);
        assert!(((deeper.round_trip_fee_bp + deeper.book_walk_bp) - deeper.cbe_bp).abs() < 1e-12);
    }

    #[test]
    fn test_sizing_beta2_lock_maker_before_taker_and_exits() {
        let b = ref_book();
        let p = ref_params();
        let f = ref_features();

        let mut p_bad_b2 = p;
        p_bad_b2.beta_2 = 0.10;
        assert!(!validate_params(&p_bad_b2));

        let dm = evaluate_entry(&b, &f, &p, 12.19);
        assert_eq!(dm.mode, ExecMode::MakerPostOnly);
        assert!(dm.order_qty * dm.limit_price * (1.0 + p.f_maker) <= 0.25 * 12.19 + 1e-9);
        assert!(dm.net_expected_edge_bp >= 5.0);

        let mut ft = f;
        ft.mid_max_900s_lag30s = 1.0500;
        ft.vwap_900s = 1.0450;
        ft.ofi_norm_30s = 1.60;
        ft.aggr_imb_60s = 0.48;
        let dt = evaluate_entry(&b, &ft, &p, 12.19);
        assert_eq!(dt.mode, ExecMode::TakerIoc);
        assert!(dt.net_expected_edge_bp >= 5.0);

        let mut p_no_maker = p;
        p_no_maker.drift_maker_bp = 500.0;
        assert_eq!(evaluate_entry(&b, &ft, &p_no_maker, 12.19).mode, ExecMode::None);

        let mut pos = Position {
            entry_fill_px: 1.0000, qty: 3.0, f_buy: 0.0040, entry_fill_s: 1000.0,
            entry_g_hat_bp: 180.0, entry_cbe_bp: 121.0, b5_at_fill: 12.0, peak_net_bp: -120.0,
        };
        let ef_600 = ExitFeatures {
            now_s: 1600.0, scheduled_rem_s: 500.0, l_sell_s: 0.695, b5_now: 12.0,
            rho5_mean_30s: 0.1, ofi_norm_30s: 0.2, ofi_norm_15s: 0.1, aggr_imb_60s: 0.2, aggr_imb_15s: 0.1,
        };
        assert_eq!(evaluate_ssm_exit(&mut pos, &b, &ef_600, 0.0080, SsmAction::Hold).reason, ExitReason::HorizonMax600s);
    }

    #[test]
    fn test_hardened_maker_and_ssm_guardrails() {
        let mut b = ref_book();
        let p = ref_params();

        assert!(book_is_fresh(&b, b.recv_ns + 5_000_000_000, 5.0));
        assert!(!book_is_fresh(&b, b.recv_ns + 5_000_000_001, 5.0));

        let mut drifted = b;
        for i in 0..MAX_LEVELS {
            drifted.bids[i].price = 0.9996 - 0.0004 * (i as f64);
        }
        assert_eq!(maker_arrive(&drifted, 1, 1.0000, 0.0001, 3.0, 104.315, 30.0).status, MakerStatus::StaleBidDriftCanceled);

        let mut ord = maker_arrive(&b, 1, 1.0000, 0.0001, 3.0, 100.0, 30.0);
        assert_eq!(ord.status, MakerStatus::Resting);
        assert!((ord.q_ahead - 5.0).abs() < 1e-12);

        b.bids[0].qty = 2.5;
        assert_eq!(maker_on_book(&mut ord, &b, 102.0), MakerStatus::Resting);
        assert!((ord.q_ahead - 2.5).abs() < 1e-12);

        b.bids[0].qty = 20.0;
        assert_eq!(maker_on_book(&mut ord, &b, 103.0), MakerStatus::Resting);
        assert!((ord.q_ahead - 2.5).abs() < 1e-12);

        assert_eq!(maker_on_trade(&mut ord, 1, 1.0000, 5.5, true, 104.0), MakerStatus::Filled);

        let b_ref = ref_book();
        let mut ord2 = maker_arrive(&b_ref, 1, 1.0000, 0.0001, 3.0, 100.0, 30.0);
        assert_eq!(maker_on_book(&mut ord2, &drifted, 105.0), MakerStatus::SupportPulledCanceled);

        let ok_maker = evaluate_ssm_proposal(&b_ref, &ref_ssm_proposal(SsmAction::EnterMakerPostOnly, 180.0), &p);
        assert!(!ok_maker.execution_authorized);
        assert_eq!(ok_maker.reject_reason, SsmRejectReason::None);
        assert_eq!(ok_maker.decision.mode, ExecMode::MakerPostOnly);
        assert!(ok_maker.position_notional_usd <= 0.25 * 12.19 + 1e-9);
        assert!(ok_maker.position_notional_usd <= 0.10 * ok_maker.top10_depth_usd + 1e-9);

        let mut bad_age = ref_ssm_proposal(SsmAction::EnterMakerPostOnly, 180.0);
        bad_age.book_age_s = 5.01;
        assert_eq!(evaluate_ssm_proposal(&b_ref, &bad_age, &p).reject_reason, SsmRejectReason::StaleBook);

        let mut bad_tox = ref_ssm_proposal(SsmAction::EnterMakerPostOnly, 180.0);
        bad_tox.toxicity_prob = 0.40;
        assert_eq!(evaluate_ssm_proposal(&b_ref, &bad_tox, &p).reject_reason, SsmRejectReason::Toxicity);
    }

    #[cfg(feature = "c_ffi")]
    #[test]
    fn test_c_header_bit_parity() {
        let b_ref = ref_book();
        let mut b = b_ref;
        let p = ref_params();
        let f = ref_features();

        // 1. c_h1v2_book_is_fresh
        assert_eq!(
            book_is_fresh(&b_ref, b_ref.recv_ns + 5_000_000_000, 5.0),
            unsafe { ffi::c_h1v2_book_is_fresh(&b_ref, b_ref.recv_ns + 5_000_000_000, 5.0) }
        );
        assert_eq!(
            book_is_fresh(&b_ref, b_ref.recv_ns + 5_000_000_001, 5.0),
            unsafe { ffi::c_h1v2_book_is_fresh(&b_ref, b_ref.recv_ns + 5_000_000_001, 5.0) }
        );

        // 2. c_h1v2_book_vwap
        let r_ask_vwap = book_vwap(&b_ref.asks[..b_ref.n_asks as usize], 10.0, false);
        let c_ask_vwap = unsafe { ffi::c_h1v2_book_vwap(b_ref.asks.as_ptr(), b_ref.n_asks, 10.0, false) };
        assert!((r_ask_vwap - c_ask_vwap).abs() < 1e-12);
        let r_bid_vwap_insuff = book_vwap(&b_ref.bids[..b_ref.n_bids as usize], 95.01, true);
        let c_bid_vwap_insuff = unsafe { ffi::c_h1v2_book_vwap(b_ref.bids.as_ptr(), b_ref.n_bids, 95.01, true) };
        assert!((r_bid_vwap_insuff - c_bid_vwap_insuff).abs() < 1e-12);

        // 3. c_h1v2_ask_vwap_capped
        let r_cap = ask_vwap_capped(&b_ref.asks[..b_ref.n_asks as usize], 5.0, 1.0021);
        let c_cap = unsafe { ffi::c_h1v2_ask_vwap_capped(b_ref.asks.as_ptr(), b_ref.n_asks, 5.0, 1.0021) };
        assert!((r_cap - c_cap).abs() < 1e-12);
        let r_cap_ok = ask_vwap_capped(&b_ref.asks[..b_ref.n_asks as usize], 4.0, 1.0021);
        let c_cap_ok = unsafe { ffi::c_h1v2_ask_vwap_capped(b_ref.asks.as_ptr(), b_ref.n_asks, 4.0, 1.0021) };
        assert!((r_cap_ok - c_cap_ok).abs() < 1e-12);

        // 4. c_h1v2_book_vwap_with_limit
        let (r_lim_a, r_marg_a) = book_vwap_with_limit(&b_ref.asks[..b_ref.n_asks as usize], 9.0, 1.0023, false);
        let mut c_marg_a = 0.0;
        let c_lim_a = unsafe {
            ffi::c_h1v2_book_vwap_with_limit(b_ref.asks.as_ptr(), b_ref.n_asks, 9.0, 1.0023, false, &mut c_marg_a)
        };
        assert!((r_lim_a - c_lim_a).abs() < 1e-12);
        assert!((r_marg_a - c_marg_a).abs() < 1e-12);

        let (r_lim_b, r_marg_b) = book_vwap_with_limit(&b_ref.bids[..b_ref.n_bids as usize], 11.0, 0.9996, true);
        let mut c_marg_b = 0.0;
        let c_lim_b = unsafe {
            ffi::c_h1v2_book_vwap_with_limit(b_ref.bids.as_ptr(), b_ref.n_bids, 11.0, 0.9996, true, &mut c_marg_b)
        };
        assert!((r_lim_b - c_lim_b).abs() < 1e-12);
        assert!((r_marg_b - c_marg_b).abs() < 1e-12);

        let (r_lim_fail, r_marg_fail) = book_vwap_with_limit(&b_ref.asks[..b_ref.n_asks as usize], 10.0, 1.0023, false);
        let mut c_marg_fail = 0.0;
        let c_lim_fail = unsafe {
            ffi::c_h1v2_book_vwap_with_limit(b_ref.asks.as_ptr(), b_ref.n_asks, 10.0, 1.0023, false, &mut c_marg_fail)
        };
        assert!((r_lim_fail - c_lim_fail).abs() < 1e-12);
        assert!((r_marg_fail - c_marg_fail).abs() < 1e-12);

        // 5. c_h1v2_compute_friction
        let r_fr = compute_friction(1.0020, 0.9995, 0.0040, 0.0080);
        let c_fr = unsafe { ffi::c_h1v2_compute_friction(1.0020, 0.9995, 0.0040, 0.0080) };
        assert_eq!(r_fr.valid, c_fr.valid);
        assert!((r_fr.round_trip_fee_bp - c_fr.round_trip_fee_bp).abs() < 1e-12);
        assert!((r_fr.book_walk_bp - c_fr.book_walk_bp).abs() < 1e-12);
        assert!((r_fr.cbe_bp - c_fr.cbe_bp).abs() < 1e-12);

        // 6. c_h1v2_evaluate_entry
        let r_dec = evaluate_entry(&b_ref, &f, &p, 12.19);
        let c_dec = unsafe { ffi::c_h1v2_evaluate_entry(&b_ref, &f, &p, 12.19) };
        assert_eq!(r_dec.mode, c_dec.mode);
        assert!((r_dec.order_qty - c_dec.order_qty).abs() < 1e-12);
        assert!((r_dec.limit_price - c_dec.limit_price).abs() < 1e-12);
        assert!((r_dec.cbe_bp - c_dec.cbe_bp).abs() < 1e-12);
        assert!((r_dec.net_expected_edge_bp - c_dec.net_expected_edge_bp).abs() < 1e-12);

        // 7. c_h1v2_evaluate_ssm_proposal
        let prop_m = ref_ssm_proposal(SsmAction::EnterMakerPostOnly, 185.0);
        let r_ssm = evaluate_ssm_proposal(&b_ref, &prop_m, &p);
        let c_ssm = unsafe { ffi::c_h1v2_evaluate_ssm_proposal(&b_ref, &prop_m, &p) };
        assert_eq!(r_ssm.reject_reason, c_ssm.reject_reason);
        assert_eq!(r_ssm.decision.mode, c_ssm.decision.mode);
        assert_eq!(r_ssm.execution_authorized, c_ssm.execution_authorized);
        assert!((r_ssm.decision.order_qty - c_ssm.decision.order_qty).abs() < 1e-12);
        assert!((r_ssm.decision.cbe_bp - c_ssm.decision.cbe_bp).abs() < 1e-12);

        // 8, 9, 10. c_h1v2_maker_arrive, c_h1v2_maker_on_book, c_h1v2_maker_on_trade
        let mut r_ord = maker_arrive(&b_ref, 1, 1.0000, 0.0001, 2.0, 100.0, 30.0);
        let mut c_ord = unsafe { ffi::c_h1v2_maker_arrive(&b_ref, 1, 1.0000, 0.0001, 2.0, 100.0, 30.0) };
        assert_eq!(r_ord.status, c_ord.status);
        assert!((r_ord.q_ahead - c_ord.q_ahead).abs() < 1e-12);

        b.bids[0].qty = 2.5;
        let r_book_st = maker_on_book(&mut r_ord, &b, 101.0);
        let c_book_st = unsafe { ffi::c_h1v2_maker_on_book(&mut c_ord, &b, 101.0) };
        assert_eq!(r_book_st, c_book_st);
        assert!((r_ord.q_ahead - c_ord.q_ahead).abs() < 1e-12);

        let r_trade_st = maker_on_trade(&mut r_ord, 1, 1.0000, 5.0, true, 102.0);
        let c_trade_st = unsafe { ffi::c_h1v2_maker_on_trade(&mut c_ord, 1, 1.0000, 5.0, true, 102.0) };
        assert_eq!(r_trade_st, c_trade_st);
        assert_eq!(r_ord.status, c_ord.status);
        assert!((r_ord.q_ahead - c_ord.q_ahead).abs() < 1e-12);
        assert!((r_ord.q_exec_after_ahead - c_ord.q_exec_after_ahead).abs() < 1e-12);
        assert!((r_ord.fill_s - c_ord.fill_s).abs() < 1e-12);

        // 11. c_h1v2_evaluate_exit
        let base_pos = Position {
            entry_fill_px: 1.0000,
            qty: 3.0,
            f_buy: 0.0040,
            entry_fill_s: 1000.0,
            entry_g_hat_bp: 180.0,
            entry_cbe_bp: 121.0,
            b5_at_fill: 12.0,
            peak_net_bp: -120.0,
        };
        let ef_exit = ExitFeatures {
            now_s: 1600.0,
            scheduled_rem_s: 500.0,
            l_sell_s: 0.695,
            b5_now: 12.0,
            rho5_mean_30s: 0.1,
            ofi_norm_30s: 0.2,
            ofi_norm_15s: 0.1,
            aggr_imb_60s: 0.2,
            aggr_imb_15s: 0.1,
        };
        let mut r_pos1 = base_pos;
        let mut c_pos1 = base_pos;
        let r_ex = evaluate_exit(&mut r_pos1, &b_ref, &ef_exit, 0.0080);
        let c_ex = unsafe { ffi::c_h1v2_evaluate_exit(&mut c_pos1, &b_ref, &ef_exit, 0.0080) };
        assert_eq!(r_ex.reason, c_ex.reason);
        assert!((r_ex.bid_vwap_px - c_ex.bid_vwap_px).abs() < 1e-12);
        assert!((r_ex.current_net_bp - c_ex.current_net_bp).abs() < 1e-12);
        assert!((r_ex.updated_peak_net_bp - c_ex.updated_peak_net_bp).abs() < 1e-12);
        assert!((r_pos1.peak_net_bp - c_pos1.peak_net_bp).abs() < 1e-12);

        // 12. c_h1v2_evaluate_ssm_exit
        let ef_hold = ExitFeatures { now_s: 1100.0, ..ef_exit };
        let mut r_pos2 = base_pos;
        let mut c_pos2 = base_pos;
        let r_ssm_ex = evaluate_ssm_exit(&mut r_pos2, &b_ref, &ef_hold, 0.0080, SsmAction::ExitNow);
        let c_ssm_ex = unsafe {
            ffi::c_h1v2_evaluate_ssm_exit(&mut c_pos2, &b_ref, &ef_hold, 0.0080, SsmAction::ExitNow)
        };
        assert_eq!(r_ssm_ex.reason, c_ssm_ex.reason);
        assert_eq!(r_ssm_ex.reason, ExitReason::SsmExitNow);
        assert!((r_ssm_ex.bid_vwap_px - c_ssm_ex.bid_vwap_px).abs() < 1e-12);
        assert!((r_ssm_ex.current_net_bp - c_ssm_ex.current_net_bp).abs() < 1e-12);
        assert!((r_ssm_ex.updated_peak_net_bp - c_ssm_ex.updated_peak_net_bp).abs() < 1e-12);

        // 13. c_h1v2_strict_epoch_break_proceeds
        let (r_proc, r_unrec) = strict_epoch_break_proceeds(&b_ref, 100.0, 0.0080, 12.0);
        let mut c_unrec = 0.0;
        let c_proc = unsafe {
            ffi::c_h1v2_strict_epoch_break_proceeds(&b_ref, 100.0, 0.0080, 12.0, &mut c_unrec)
        };
        assert!((r_proc - c_proc).abs() < 1e-12);
        assert!((r_unrec - c_unrec).abs() < 1e-12);
    }
}
