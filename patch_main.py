import re

with open('pair_bot/main.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Add global_ticker_loop before pair_loop
global_ticker_loop = '''
# ─────────────────────────────────────────────────────────────────────────────
# 전체 티커 캐싱 루프 (Dual Loop 아키텍처)
# ─────────────────────────────────────────────────────────────────────────────
async def global_ticker_loop(exchange: ccxt_async.Exchange, bot_state: BotState):
    logger = logging.getLogger("pair_bot")
    logger.info(f"Global Ticker Loop 시작 (주기: {POLL_INTERVAL_SEC}초)")
    while True:
        try:
            tickers = await exchange.fetch_tickers()
            if tickers:
                bot_state.global_tickers = tickers
        except Exception as e:
            logger.warning(f"[GlobalTicker] 티커 조회 실패: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# 단일 페어 감시 루프
'''
code = code.replace('# ─────────────────────────────────────────────────────────────────────────────\n# 단일 페어 감시 루프 (Z-Score / 진입 / 청산 핵심 로직 유지)\n# ─────────────────────────────────────────────────────────────────────────────', global_ticker_loop.strip())

# 2. Add last_pnl_fetch_time inside pair_loop
code = code.replace('corr_sample_counter = 0  # 10초 폴링을 1분 단위로 다운샘플링하기 위한 카운터', 'corr_sample_counter = 0  # 3초 폴링을 1분 단위로 다운샘플링하기 위한 카운터\n    last_pnl_fetch_time = 0.0  # 초기 1회는 즉시 조회하도록 0으로 설정')

# 3. Replace price fetch inside pair_loop
old_price_fetch = '''            # ── 1. 가격 조회 ────────────────────────────────────────────────
            price_a, price_b = await asyncio.gather(
                fetch_mid_price(exchange, sym_a),
                fetch_mid_price(exchange, sym_b),
            )

            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                logger.warning(f"[{prefix}] 가격 조회 실패 — 다음 사이클 대기")
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue'''

new_price_fetch = '''            # ── 1. 가격 조회 (Global Ticker 캐시 사용) ─────────────────────────
            ticker_a = bot_state.global_tickers.get(sym_a)
            ticker_b = bot_state.global_tickers.get(sym_b)
            
            if not ticker_a or not ticker_b or not ticker_a.get('last') or not ticker_b.get('last'):
                # 아직 캐시가 안 차오른 경우 대기
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue
                
            price_a = float(ticker_a['last'])
            price_b = float(ticker_b['last'])
            
            if math.isnan(price_a) or math.isnan(price_b) or price_b == 0:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue'''
code = code.replace(old_price_fetch, new_price_fetch)

# 4. Change corr_sample_counter limit
code = code.replace('corr_sample_counter >= 6', 'corr_sample_counter >= 20')
code = code.replace('10초 폴링 x 6 = 1분', '3초 폴링 x 20 = 1분')

# 5. Dual loop PnL logic
old_pnl_logic = '''                    unrealized_pct = 0.0
                    pnl_calc_ok = False
                    upnl_a = 0.0
                    upnl_b = 0.0
                    gross_pnl = 0.0
                    total_fee = 0.0
                    unrealized_net = 0.0
                    try:
                        positions_raw = await exchange.fetch_positions()
                        upnl_a = 0.0
                        upnl_b = 0.0
                        for p in positions_raw:
                            sym = p.get("symbol", "")
                            contracts = abs(float(p.get("contracts", 0)))
                            if contracts == 0:
                                continue
                            raw_upnl = float(p.get("unrealizedPnl", 0.0))
                            base_sym = sym.split(':')[0]
                            if base_sym == sym_a.split(':')[0]:
                                upnl_a = raw_upnl
                            elif base_sym == sym_b.split(':')[0]:
                                upnl_b = raw_upnl

                        gross_pnl = upnl_a + upnl_b
                        qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
                        qty_b = (pos.margin_b * LEVERAGE) / pos.price_b
                        entry_notional = (qty_a * pos.price_a) + (qty_b * pos.price_b)
                        exit_notional  = (qty_a * price_a) + (qty_b * price_b)
                        total_fee = (entry_notional * TAKER_FEE_RATE) + (exit_notional * MAKER_FEE_RATE)
                        unrealized_net = gross_pnl - total_fee
                        total_margin   = pos.margin_a + pos.margin_b
                        unrealized_pct = (unrealized_net / total_margin * 100) if total_margin > 0 else 0.0
                        pnl_calc_ok = True
                    except Exception as e:
                        logger.warning(f"[{prefix}] 거래소 PnL 조회 실패 → 자체 수식 Fallback 발동: {e}")'''

new_pnl_logic = '''                    unrealized_pct = 0.0
                    pnl_calc_ok = False
                    upnl_a = 0.0
                    upnl_b = 0.0
                    gross_pnl = 0.0
                    total_fee = 0.0
                    unrealized_net = 0.0
                    
                    now = time.time()
                    if now - last_pnl_fetch_time >= 15.0:
                        try:
                            positions_raw = await exchange.fetch_positions()
                            upnl_a = 0.0
                            upnl_b = 0.0
                            for p in positions_raw:
                                sym = p.get("symbol", "")
                                contracts = abs(float(p.get("contracts", 0)))
                                if contracts == 0:
                                    continue
                                raw_upnl = float(p.get("unrealizedPnl", 0.0))
                                base_sym = sym.split(':')[0]
                                if base_sym == sym_a.split(':')[0]:
                                    upnl_a = raw_upnl
                                elif base_sym == sym_b.split(':')[0]:
                                    upnl_b = raw_upnl

                            gross_pnl = upnl_a + upnl_b
                            qty_a = (pos.margin_a * LEVERAGE) / pos.price_a
                            qty_b = (pos.margin_b * LEVERAGE) / pos.price_b
                            entry_notional = (qty_a * pos.price_a) + (qty_b * pos.price_b)
                            exit_notional  = (qty_a * price_a) + (qty_b * price_b)
                            total_fee = (entry_notional * TAKER_FEE_RATE) + (exit_notional * MAKER_FEE_RATE)
                            unrealized_net = gross_pnl - total_fee
                            total_margin   = pos.margin_a + pos.margin_b
                            unrealized_pct = (unrealized_net / total_margin * 100) if total_margin > 0 else 0.0
                            pnl_calc_ok = True
                            last_pnl_fetch_time = now
                        except Exception as e:
                            logger.warning(f"[{prefix}] 거래소 PnL 조회 실패 → 자체 수식 Fallback 발동: {e}")
                    else:
                        # 15초 쿨타임 중: 자체 수식 Fallback 발동 (API Weight 절감)
                        pass'''
code = code.replace(old_pnl_logic, new_pnl_logic)

# 6. Add global_ticker_loop to tasks in main_loop
old_task_append = 'tasks.append(btc_turbulence_monitor(exchange, bot_state, notifier))'
new_task_append = '''tasks.append(global_ticker_loop(exchange, bot_state))
    tasks.append(btc_turbulence_monitor(exchange, bot_state, notifier))'''
code = code.replace(old_task_append, new_task_append)

# Also update the prefetch window size multiplying factor
code = code.replace('for _ in range(6):', 'for _ in range(20):')
code = code.replace('1분(60초) = 10초 폴링 * 6회 반복', '1분(60초) = 3초 폴링 * 20회 반복')

with open('pair_bot/main.py', 'w', encoding='utf-8') as f:
    f.write(code)

print('Success')
