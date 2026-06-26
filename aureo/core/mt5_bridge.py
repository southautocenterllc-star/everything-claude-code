"""Puente MT5 — corre DENTRO de wine-python (Windows) para ejecutar en MetaTrader 5.

Áureo (Linux) lo invoca: `wine <python-win> core/mt5_bridge.py <accion> ...`.
SOLO para cuenta DEMO mientras no haya estrategia con edge (ver research). El
arnés del proyecto prohíbe automatizar dinero real sin edge validado.

Acciones:
  info                         → imprime cuenta + precio actual (JSON)
  order SELL|BUY <vol> <sl> <tp> [symbol]   → manda orden a mercado
  modify <ticket> <sl> <tp> [symbol]        → cambia SL/TP (breakeven)
  close <symbol>               → cierra posición del símbolo

Credenciales por entorno: MT5_LOGIN, MT5_PASSWORD, MT5_SERVER.
"""

import json
import os
import sys

import MetaTrader5 as mt5

TERMINAL = r"C:\Program Files\MetaTrader 5\terminal64.exe"
SYMBOL_DEFAULT = "XAUUSD"
MAGIC = 770311  # identifica las órdenes de Áureo


def _out(d):
    print("AUREO_MT5 " + json.dumps(d, default=str))


def _init():
    login = int(os.environ.get("MT5_LOGIN", "0"))
    pw = os.environ.get("MT5_PASSWORD", "")
    server = os.environ.get("MT5_SERVER", "")
    ok = mt5.initialize(TERMINAL, login=login, password=pw, server=server, portable=True)
    if not ok:
        _out({"ok": False, "stage": "initialize", "error": mt5.last_error()})
        sys.exit(2)
    return True


def cmd_info():
    ai = mt5.account_info()
    if ai is None:
        _out({"ok": False, "error": "account_info None", "last": mt5.last_error()}); return
    sym = SYMBOL_DEFAULT
    mt5.symbol_select(sym, True)
    tick = mt5.symbol_info_tick(sym)
    ti = mt5.terminal_info()
    _out({"ok": True, "login": ai.login, "server": ai.server, "balance": ai.balance,
          "equity": ai.equity, "currency": ai.currency, "leverage": ai.leverage,
          "symbol": sym, "bid": getattr(tick, "bid", None), "ask": getattr(tick, "ask", None),
          # trade_allowed del terminal = botón "Algo Trading" ON; del account =
          # el broker permite operar. Si terminal_trade_allowed=False, toda orden
          # automática se rechaza (retcode 10027) aunque se pueda leer precio.
          "terminal_trade_allowed": getattr(ti, "trade_allowed", None),
          "terminal_connected": getattr(ti, "connected", None),
          "account_trade_allowed": getattr(ai, "trade_allowed", None)})


def cmd_order(side, vol, sl, tp, symbol):
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        _out({"ok": False, "error": f"sin tick para {symbol}", "last": mt5.last_error()}); return
    is_buy = side.upper() == "BUY"
    price = tick.ask if is_buy else tick.bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(vol),
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price, "deviation": 20, "magic": MAGIC,
        "comment": "AUREO demo", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if float(sl) > 0:
        req["sl"] = float(sl)
    if float(tp) > 0:
        req["tp"] = float(tp)
    r = mt5.order_send(req)
    _out({"ok": r is not None and r.retcode == mt5.TRADE_RETCODE_DONE,
          "retcode": getattr(r, "retcode", None), "comment": getattr(r, "comment", None),
          "order": getattr(r, "order", None), "price": getattr(r, "price", None),
          "volume": getattr(r, "volume", None), "side": side, "symbol": symbol})


def cmd_modify(ticket, sl, tp, symbol):
    """Modifica SL/TP de una posición abierta (TRADE_ACTION_SLTP). Usado para
    breakeven: mover el SL a la entrada sin cerrar la posición."""
    pos = mt5.positions_get(ticket=int(ticket))
    if not pos:
        _out({"ok": False, "error": f"sin posición ticket={ticket}", "last": mt5.last_error()}); return
    p = pos[0]
    if p.magic != MAGIC:
        _out({"ok": False, "error": f"ticket {ticket} no es de AUREO (magic={p.magic})"}); return
    new_sl = float(sl) if float(sl) > 0 else p.sl
    # Baranda anti-aflojar: el SL solo puede APRETAR (acercarse o ir más allá de
    # la entrada), nunca alejarse. BUY → subir; SELL → bajar. Si no había SL
    # (p.sl==0) cualquier SL protector pasa. Defensa en profundidad: el flujo
    # automático ya solo aprieta, esto frena un modify manual que afloje.
    if p.sl and new_sl != p.sl:
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        afloja = (is_buy and new_sl < p.sl) or (not is_buy and new_sl > p.sl)
        if afloja:
            _out({"ok": False, "error": f"SL aflojaría stop (BUY sube/SELL baja). "
                  f"actual={p.sl} pedido={new_sl}", "ticket": int(ticket)}); return
    req = {"action": mt5.TRADE_ACTION_SLTP, "symbol": symbol,
           "position": int(ticket), "magic": MAGIC,
           "sl": new_sl,
           "tp": float(tp) if float(tp) > 0 else p.tp}
    r = mt5.order_send(req)
    _out({"ok": r is not None and r.retcode == mt5.TRADE_RETCODE_DONE,
          "retcode": getattr(r, "retcode", None), "comment": getattr(r, "comment", None),
          "ticket": int(ticket), "sl": req["sl"], "tp": req["tp"]})


def cmd_close(symbol):
    pos = mt5.positions_get(symbol=symbol)
    if not pos:
        _out({"ok": True, "msg": "sin posiciones", "symbol": symbol}); return
    results = []
    for p in pos:
        tick = mt5.symbol_info_tick(symbol)
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": p.volume,
               "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
               "position": p.ticket, "price": tick.bid if is_buy else tick.ask,
               "deviation": 20, "magic": MAGIC, "comment": "AUREO close",
               "type_filling": mt5.ORDER_FILLING_IOC}
        r = mt5.order_send(req)
        results.append({"ticket": p.ticket, "retcode": getattr(r, "retcode", None)})
    _out({"ok": True, "closed": results})


def cmd_report():
    """Posiciones abiertas + deals cerrados de Áureo (por MAGIC) — para el
    sistema de aprendizaje: reconciliar resultados de cada trade."""
    import datetime as _dt
    pos = mt5.positions_get() or []
    open_pos = [{"ticket": p.ticket, "symbol": p.symbol,
                 "type": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
                 "volume": p.volume, "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                 "profit": p.profit, "magic": p.magic, "time": p.time}
                for p in pos if p.magic == MAGIC]
    frm = _dt.datetime.now() - _dt.timedelta(days=30)
    deals = mt5.history_deals_get(frm, _dt.datetime.now()) or []
    closed = [{"ticket": d.ticket, "order": d.order, "position_id": d.position_id,
               "symbol": d.symbol, "type": "BUY" if d.type == mt5.DEAL_TYPE_BUY else "SELL",
               "volume": d.volume, "price": d.price, "profit": d.profit,
               "entry": d.entry, "time": d.time, "magic": d.magic}
              for d in deals if d.magic == MAGIC]
    _out({"ok": True, "open": open_pos, "closed": closed})


def main():
    args = sys.argv[1:]
    if not args:
        _out({"ok": False, "error": "uso: info | order SIDE vol sl tp [symbol] | close [symbol] | report"}); return
    _init()
    try:
        cmd = args[0]
        if cmd == "info":
            cmd_info()
        elif cmd == "report":
            cmd_report()
        elif cmd == "order":
            side, vol, sl, tp = args[1], args[2], args[3], args[4]
            symbol = args[5] if len(args) > 5 else SYMBOL_DEFAULT
            cmd_order(side, vol, sl, tp, symbol)
        elif cmd == "modify":
            ticket, sl, tp = args[1], args[2], args[3]
            symbol = args[4] if len(args) > 4 else SYMBOL_DEFAULT
            cmd_modify(ticket, sl, tp, symbol)
        elif cmd == "close":
            cmd_close(args[1] if len(args) > 1 else SYMBOL_DEFAULT)
        else:
            _out({"ok": False, "error": f"comando desconocido {cmd}"})
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()