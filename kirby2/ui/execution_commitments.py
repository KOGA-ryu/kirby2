"""Client-knowledge commitments. Venue snapshots are deliberately not consumed."""
from __future__ import annotations
from copy import deepcopy


class ExecutionRefusal(ValueError):
    def __init__(self, code, message, category="SYSTEM_CONSTRAINT"):
        super().__init__(message)
        self.code, self.category = code, category


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ExecutionRefusal("INVALID_INPUT",f"{name} must be an integer in [{low},{high}].","LEARNER_INSTRUCTION")
    return value


class Commitments:
    """Every accepted entry path reserves before routing to the sole venue.

    Terminal receipts account for quantities, not whole-order optimism. Economic
    IDs are independent of transport message IDs, so a redelivery is harmless.
    Unsupported corrections and inconsistent quantities lock new exposure.
    """
    def __init__(self, maximum=1000):
        self.maximum = integer(maximum,"share cap",1,1000)
        self.position = 0
        self.orders = {}
        self.receipts = {}
        self.unknown = None

    def interval(self):
        buys = sum(o['unresolved'] for o in self.orders.values() if o['side']=='buy')
        sells = sum(o['unresolved'] for o in self.orders.values() if o['side']=='sell')
        return self.position-sells, self.position+buys

    def reserve(self, order_id, side, quantity):
        if self.unknown:
            raise ExecutionRefusal("UNKNOWN_EXPOSURE","Execution state is uncertain; cancel or freeze for review.")
        if side not in ('buy','sell') or order_id in self.orders:
            raise ExecutionRefusal("INVALID_ORDER","Order identity or side is invalid.","LEARNER_INSTRUCTION")
        integer(quantity,"quantity",1,self.maximum)
        low,high = self.interval()
        if (side=='buy' and high+quantity>self.maximum) or (side=='sell' and low-quantity<0):
            raise ExecutionRefusal("EXPOSURE_CAP",f"Possible exposure [{low},{high}] cannot reserve {quantity} more {side} shares. Cap 0–{self.maximum}.")
        self.orders[order_id] = dict(side=side,quantity=quantity,unresolved=quantity,filled=0,
            cancelled=0,expired=0,rejected=0,pending_cancel=False,acknowledged=False)

    def observe(self, message):
        payload = message['client_payload']
        event_type, data = payload.get('event_type'), payload.get('event_data',{})
        if event_type is None: return
        identity = payload.get('mechanics_sequence')
        if type(identity) is not int:
            self.unknown = 'MISSING_ECONOMIC_ID'; return
        if event_type=='TRADE':
            trade_id = data.get('trade_id')
            if type(trade_id) is not str or not trade_id:
                self.unknown = 'MISSING_TRADE_ID'; return
            identity = 'TRADE:'+trade_id
        content = (event_type,deepcopy(data))
        prior = self.receipts.get(identity)
        if prior is not None:
            if prior != content: self.unknown = 'CONFLICTING_DUPLICATE'
            return
        self.receipts[identity] = content
        if event_type in ('TRADE_CORRECTION','TRADE_BUST','ORDER_REPLACED'):
            self.unknown = 'UNSUPPORTED_CORRECTION'; return
        order_id = data.get('order_id')
        order = self.orders.get(order_id)
        try:
            if event_type=='TRADE':
                quantity = integer(data.get('quantity'),'reported fill',1,1_000_000)
                for key in (data.get('maker_order_id'),data.get('taker_order_id')):
                    if key not in self.orders: continue
                    row = self.orders[key]
                    self._account(row,'filled',quantity)
                    self.position += quantity if row['side']=='buy' else -quantity
            elif order is not None:
                if event_type=='ORDER_CANCELLED':
                    self._account(order,'cancelled',integer(data.get('cancelled_quantity'),'cancel quantity',1,1_000_000))
                    order['pending_cancel'] = False
                elif event_type=='ORDER_EXPIRED':
                    self._account(order,'expired',integer(data.get('expired_quantity'),'expiry quantity',1,1_000_000))
                elif event_type=='ORDER_ACCEPTED':
                    order['acknowledged'] = True
                elif event_type=='ORDER_REJECTED':
                    reason = data.get('reason','')
                    if reason=='CANCEL_NOT_ACTIVE':
                        # A fully filled order can reject cancellation before its
                        # fill report arrives. It releases no reserved capacity.
                        order['pending_cancel'] = False
                    elif not order['acknowledged'] and order['filled']==0 and 'filled_quantity' not in data:
                        self._account(order,'rejected',order['unresolved'])
                    else:
                        self.unknown = 'UNACCOUNTED_REJECTION'
            low,high = self.interval()
            if low<0 or high>self.maximum: self.unknown = 'RECONCILIATION_OUTSIDE_CAP'
        except (ExecutionRefusal,ValueError,TypeError):
            self.unknown = 'INCONSISTENT_ECONOMIC_QUANTITY'

    def _account(self, order, kind, quantity):
        if quantity>order['unresolved']:
            raise ValueError('reported quantity exceeds unresolved commitment')
        order[kind] += quantity
        order['unresolved'] -= quantity

    def view(self):
        low,high = self.interval()
        return dict(confirmed_position=self.position,buy_commitment=high-self.position,
                    sell_commitment=self.position-low,possible_position=[low,high],
                    maximum_shares=self.maximum,unknown=self.unknown,orders=deepcopy(self.orders))
