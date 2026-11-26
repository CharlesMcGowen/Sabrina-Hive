/*
 * rust/c_shim.c — Thin non-inline C99 export shim around h1_v2_kernel.h
 * Used when running Rust <-> C99 bit-parity tests (`--cfg feature="c_ffi"`):
 *   gcc -std=c99 -O2 -c rust/c_shim.c -I. -o c_shim.o && ar rcs libc_shim.a c_shim.o
 *   rustc --edition 2021 --test --cfg 'feature="c_ffi"' -L . -l static=c_shim rust/src/lib.rs -o test_rust_ffi
 */
#include "../h1_v2_kernel.h"

bool c_h1v2_book_is_fresh(const h1v2_book10_t *b, uint64_t now_ns, double max_age_s) {
    return h1v2_book_is_fresh(b, now_ns, max_age_s);
}

double c_h1v2_book_vwap(const h1v2_level_t *levels, uint32_t n, double qty, bool is_bid) {
    return h1v2_book_vwap(levels, n, qty, is_bid);
}

double c_h1v2_ask_vwap_capped(const h1v2_level_t *asks, uint32_t n, double qty, double limit_px) {
    return h1v2_ask_vwap_capped(asks, n, qty, limit_px);
}

double c_h1v2_book_vwap_with_limit(
    const h1v2_level_t *levels,
    uint32_t n,
    double qty,
    double limit_price,
    bool is_bid,
    double *marginal_px_out
) {
    return h1v2_book_vwap_with_limit(levels, n, qty, limit_price, is_bid, marginal_px_out);
}

h1v2_friction_t c_h1v2_compute_friction(double entry_ref_px, double exit_bid_vwap_px, double f_buy, double f_sell) {
    return h1v2_compute_friction(entry_ref_px, exit_bid_vwap_px, f_buy, f_sell);
}

h1v2_decision_t c_h1v2_evaluate_entry(
    const h1v2_book10_t *book,
    const h1v2_features_t *f,
    const h1v2_params_t *p,
    double available_cash_usd
) {
    return h1v2_evaluate_entry(book, f, p, available_cash_usd);
}

h1v2_ssm_eval_t c_h1v2_evaluate_ssm_proposal(
    const h1v2_book10_t *book,
    const h1v2_ssm_proposal_t *prop,
    const h1v2_params_t *p
) {
    return h1v2_evaluate_ssm_proposal(book, prop, p);
}

h1v2_maker_order_t c_h1v2_maker_arrive(
    const h1v2_book10_t *arrival_book,
    uint64_t expected_epoch,
    double limit_price,
    double tick_size,
    double order_qty,
    double arrive_s,
    double ttl_s
) {
    return h1v2_maker_arrive(arrival_book, expected_epoch, limit_price, tick_size, order_qty, arrive_s, ttl_s);
}

h1v2_maker_status_t c_h1v2_maker_on_book(h1v2_maker_order_t *ord, const h1v2_book10_t *book, double now_s) {
    return h1v2_maker_on_book(ord, book, now_s);
}

h1v2_maker_status_t c_h1v2_maker_on_trade(
    h1v2_maker_order_t *ord,
    uint64_t trade_epoch,
    double trade_price,
    double trade_qty,
    bool is_sell_aggressor,
    double now_s
) {
    return h1v2_maker_on_trade(ord, trade_epoch, trade_price, trade_qty, is_sell_aggressor, now_s);
}

h1v2_exit_eval_t c_h1v2_evaluate_exit(
    h1v2_position_t *pos,
    const h1v2_book10_t *book,
    const h1v2_exit_features_t *ef,
    double f_taker
) {
    return h1v2_evaluate_exit(pos, book, ef, f_taker);
}

h1v2_exit_eval_t c_h1v2_evaluate_ssm_exit(
    h1v2_position_t *pos,
    const h1v2_book10_t *book,
    const h1v2_exit_features_t *ef,
    double f_taker,
    h1v2_ssm_action_t ssm_action
) {
    return h1v2_evaluate_ssm_exit(pos, book, ef, f_taker, ssm_action);
}

double c_h1v2_strict_epoch_break_proceeds(
    const h1v2_book10_t *last_book,
    double open_qty,
    double f_taker,
    double drift_taker_bp,
    double *unrec_out
) {
    return h1v2_strict_epoch_break_proceeds(last_book, open_qty, f_taker, drift_taker_bp, unrec_out);
}
