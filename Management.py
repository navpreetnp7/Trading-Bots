import sys
sys.path.append('../')

import random
import pika
import time
import pandas as pd
import numpy as np
import json
from time import sleep

import collections
from collections import deque
import datetime
import sys
import logging
import threading
import abc
import gym
import json
import argparse
import configparser
import collections
from collections import deque
import uuid
import os
import datetime
from qpython import qconnection

import MessageCommunication.test_pb2
from MessageCommunication.Communication import Communication
semaphore = threading.Semaphore()

# logging.basicConfig(handlers=[logging.FileHandler('save/management_onlinelearning.log'), logging.StreamHandler()],
#                     format='%(asctime)s - %(levelname)s - %(message)s',
#                     level=logging.WARNING)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    level=logging.INFO)


class Management(Communication):
    def __init__(self, strategy, starting_money,
                 market_event_securities, market_event_queue, securities,
                 host=None, bot_id=None):

        # address of the rabbitMQ server
        self.host = 'localhost' if host is None else host

        self.store = qconnection.QConnection(host='localhost', port=3061, pandas=True)
        self.store.open()
        
        # unique id for every bot (needed for routing ACK messages)
        self.bot_id = str(uuid.uuid4()) if bot_id is None else bot_id

        super(Management, self).__init__(market_event_securities, securities, self.host, self.bot_id)

        self.logger = logging.getLogger('Management')

        # duration
        self.duration_map = {'GE': 0.25, 'ZT': 1.8, 'ZF': 4.5, 'ZN': 8.0, 'TN': 9.0, 'ZB': 20.0, 'UB': 30.0}

        # convert point to dollar
        self.value_point = {'GE': 25, 'ZT': 2000, 'ZF': 1000, 'ZN': 1000, 'TN': 1000, 'ZB': 1000, 'UB': 1000, 'GC': 10}

        # all securities to listen to, e.g.
        # [
        #     'ZFH0:MBO', 'ZTH0:MBO', 'UBH0:MBO', 'ZNH0:MBO', 'ZBH0:MBO',
        #     'TNH0:MBO', 'GCG0:MBO', 'GEU2:MBO', 'GEM4:MBO', 'GEM2:MBO',
        #     'GEM0:MBO', 'GEH0:MBO', 'GEZ0:MBO', 'GEZ1:MBO', 'GEM1:MBO',
        #     'GEH4:MBO', 'GEU1:MBO', 'GEZ2:MBO', 'GEM3:MBO', 'GEH2:MBO',
        #     'GEH1:MBO', 'GEH3:MBO', 'GEU0:MBO', 'GEZ3:MBO', 'GEU3:MBO'
        # ]

        # margin
        self.margin = 1.0

        # all securities to listen to
        self.market_event_queue = market_event_queue

        # dict of dict per security + level
        self.market_dict = self._init_market_dict()

        # TODO: add more indicators of the whole markets
        self.ask_trend = self._init_market_dict()
        self.bid_trend = self._init_market_dict()

        # mdi price of current market
        self.mid_market = self._init_sec_prices()

        # securities to trade
        self.securities = securities
        self.sec_len = len(self.securities)
        self.inventory = self._init_inventory()
        self.inventoryValue = 0.0

        self.strategy = strategy
        self.starting_money = starting_money
        self.cash_balance = self.starting_money
        self.net_worth = self.cash_balance

        # ID assigned by bot across securities
        self.internalID = 0

        # Dictionary to store latest level data for each of the securities.
        self.latest_level_data = {}

        # ID assigned by market maker per security when ACKed
        self.exIds_to_inIds = [dict() for _ in range(len(self.securities))]    # TODO: change list to dict!
        self.inIds_to_exIds = dict()
        self.inIds_to_orders_sent = dict()  # orders sent but not acked
        self.inIds_to_orders_confirmed = dict()  # orders confirmed by matching agent
        # self.inIds_to_delete = dict() # orders deleted but not acked

        # cumulative traded volume and vwap
        self.vwap = {s: 0 for s in self.securities}
        self.traded_volume = {s: 0 for s in self.securities}

        # self.outputfile = "save/" + self.strategy + "_detailed_logs.txt"

        # order status per security under processing
        self.order_status = {sec: {'internal_id': -1, 'filled': False} for sec in self.securities}

        # limits set up by ClearingAgent per symbol
        self.limits = [{'TimeDelay': -1,
                        'InventoryLimit': 1000000,
                        'OrdersOutStandingLimit': 1000000,
                        'OrdersSendLimit': 1000000,
                        'ShutDown': False}
                       for _ in range(len(self.securities))]
        self.previous_order_time = 0

    def _save_order_being_sent(self, order):
        self.inIds_to_orders_sent[order["orderNo"]] = order

    def _check_condition(self, symb):


        secIdx = self._get_sec_idx(symb)

        if any([l['ShutDown'] for l in self.limits]):
            # save PnL into kdb
            pnldict={'traderid': self.bot_id,
                     'pnl': self.net_worth/self.starting_money - 1,
                     'dt': datetime.datetime.now()}
            pnldf = pd.DataFrame(pnldict, index=[0])
            self.store('{`pnl upsert x}', pnldf)
            
            self.channel.close()
            self.channel2.close()
            self.logger.warning(f'Bot {self.bot_id} is shutting down and connection is closed!')
            self.store.close()
            sys.exit()

        elif self.net_worth < 0:
            self.logger.warning(f'Bot {self.bot_id} has low net worth!')

        elif time.time() - self.previous_order_time >= self.limits[secIdx]['TimeDelay'] and \
            self.inventory[symb] <= self.limits[secIdx]['InventoryLimit'] and \
            sum([v['remainingQty'] for k,v in self.inIds_to_orders_confirmed.copy().items() if v['symb']==symb]
                ) <= self.limits[secIdx]['OrdersOutStandingLimit'] and \
            len([v['remainingQty'] for k,v in self.inIds_to_orders_confirmed.copy().items() if v['symb']==symb]
                ) <= self.limits[secIdx]['OrdersSendLimit']:

            return True

        self.logger.warning(f"Order skipped due to limit hit for {symb}!")

        return False

    def cancel_order(self, order):
        self._cancel_order(order)

    # def cancel_orders(self, inIds):
    #     for inId in inIds:
    #         if inId in self.inIds_to_orders_confirmed:
    #             order = self.inIds_to_orders_confirmed[inId]
    #             if inId in self.inIds_to_exIds:
    #                 order["orderNo"] = self.inIds_to_exIds[inId]
    #                 self.talk2._cancel_order(order)
    #                 self.inIds_to_delete[inId] = order
    #             print("~~~~~~")
    #             print("Order ID To Delete:", inId)
    #             print("Order", order)
    #             print("~~~~~~")

    def send_order(self, order):
        # No need to check the condition below as we can always borrow money or inventory.
        # If the net worth < 0, the env will be reset. So also no need to add condition here.
        # if order["side"] == 'B' and self.cash_budget < order["price"] * order["origQty"]:
        #     logging.warning("Cash : " + str(self.cash_budget))
        #     logging.warning("Not enough cash to buy " + str(order["origQty"]) + " " + order["symb"])
        #     return False
        # elif order["side"] == 'S' and self.inventory[order["symb"]] < order["origQty"]:
        #     logging.warning(order["symb"] + " : " + str(self.inventory[order["symb"]]))
        #     logging.warning("Not enough " + order["symb"] + " to sell")
        #     return False

        if order['origQty'] <= 0 or order['price'] <= 0:
            return False

        if self._check_condition(order['symb']):
            # instead, internal ID was assigned during order generation process
            # # assign internal ID
            # order["orderNo"] = self.internalID

            # record it on the list of orders sent
            self._save_order_being_sent(order)

            # sent
            self.logger.debug("Order sent:\n {} - {} - {}".format(order["orderNo"], order['side'], order['price']))
            self._send_order(order)
            # self._send_order(order)

            # record the time
            self.previous_order_time = time.time()

            # # increment internal ID for next order
            # self.internalID += 1

            # self.cash_budget -= order["price"] * order["origQty"]

            return True

    def _update_with_trade(self, tradeobj, side, exId):
        # buy side = 1, sell side = -1
        # self._update_order_pnl()
        if tradeobj.symbol in self.securities:
            # update inventory and cash based on the trade
            self._update_inventory(tradeobj.symbol, tradeobj.tradeSize * side)
            self._update_cash(tradeobj.tradeSize,
                              self.margin * self.value_point[tradeobj.symbol[:2]] * tradeobj.tradePrice * (-side))
            self._update_order_remain(exId, tradeobj.tradeSize, tradeobj.symbol)

            # update value based on the old mid market
            # to be further updated by callback for levels since callback for trade is followed by callback for levels
            self._update_inventory_value()
            self._update_net_worth()

            self.logger.debug(" [X] Cash : %s" % str(self.cash_balance))
            self.logger.debug(" [X] Inventory Value : %s" % str(self.inventoryValue))
            self.logger.debug(" [X] Portfolio Value : %s" % str(self.net_worth))
            # with open(self.outputfile, "a") as myfile:
            #     myfile.write(" [X] Cash : %s\n" % str(self.cash_balance))
            #     myfile.write(" [X] Inventory Value : %s\n" % str(self.inventoryValue))
            #     myfile.write(" [X] Portfolio Value : %s\n" % str(self.net_worth))

    def _update_inventory(self, symbol, size):
        self.inventory[symbol] += size
        self.logger.debug(" [X] inventory:")
        # with open(self.outputfile, "a") as myfile:
        #     for sec in self.securities:
        #         logging.info("%s : %d" % (sec, self.inventory[sec]))
        #         myfile.write("%s : %d\n" % (sec, self.inventory[sec]))

    def _update_inventory_value(self, ):
        # global current_observation
        # inventoryValue = 0.0
        # for idx, sec in enumerate(self.securities):
        #     if self.sec_len > 1:
        #         inventoryValue += self.value_point[sec[:2]] * self.inventory[sec] * 0.5 * (
        #                 current_observation["L1BidPrice"].iloc[0].iloc[idx] + current_observation["L1AskPrice"].iloc[0].iloc[idx])
        #
        #     else:
        #         inventoryValue += self.value_point[sec[:2]] * self.inventory[sec] * 0.5 * (
        #                 current_observation["L1BidPrice"].iloc[0] + current_observation["L1AskPrice"].iloc[0])
        #
        # self.inventoryValue = inventoryValue
        # logging.debug(" [X] inventory value: %d" % self.inventoryValue)
        # for sec in self.securities:
        #     logging.debug("%s : %d" % (sec, self.inventory[sec]))
        inventoryValue = 0.0
        for sec in self.securities:
            if self.mid_market[sec] is not None:
                inventoryValue += self.value_point[sec[:2]]*self.inventory[sec] * self.mid_market[sec]
        self.inventoryValue = inventoryValue
        self.logger.debug(" [X] inventory value: %d" % self.inventoryValue)

    def _update_cash(self, size, price):
        self.cash_balance += size * price
        self.logger.debug(" [X] cash balance: %d" % self.cash_balance)

    def _update_net_worth(self, ):
        self.net_worth = self.cash_balance + self.inventoryValue
        self.logger.debug(" [X] Networth value: %d" % self.net_worth)

    def _get_sec_idx(self, symb):
        # the idx of the security in inventory
        for idx, sec in enumerate(self.securities):
            if sec == symb:
                return idx
        return None

    def _update_order_remain(self, exId, size, symb):
        secIdx = self._get_sec_idx(symb)
        inId = self.exIds_to_inIds[secIdx][exId]
        self.inIds_to_orders_confirmed[inId]["remainingQty"] -= size
        if self.inIds_to_orders_confirmed[inId]["remainingQty"] <= 0.0:
            self.logger.debug(f"~~~~~~~~~~~~~~~~~inId {inId} finished")
            self.inIds_to_orders_confirmed.pop(inId)

    # only accept trade which belongs to this bot
    def _condition_to_accept_trade(self, tradeobj):
        exId = 0
        secIdx = self._get_sec_idx(tradeobj.symbol)
        self.logger.debug(
            f'Receiving Trade for {tradeobj.symbol}: {tradeobj.buyOrderNo}, {tradeobj.sellOrderNo}, {tradeobj.tradePrice}, {tradeobj.tradeSize}'
        )
        if tradeobj.buyOrderNo in list(self.exIds_to_inIds[secIdx].keys()):
            self.logger.debug("Order %s : Buy Order %d is filled with quantity %f of price %s\n" % (
                str(tradeobj.buyOrderNo), self.exIds_to_inIds[secIdx][tradeobj.buyOrderNo], tradeobj.tradeSize,
                tradeobj.tradePrice))
            self.logger.debug(
                f'         (remaining qty = {self.inIds_to_orders_confirmed[self.exIds_to_inIds[secIdx][tradeobj.buyOrderNo]]["remainingQty"]})')
            # with open(self.outputfile, "a") as myfile:
            #     myfile.write("Order %s : Buy Order %d is filled with quantity %d of price %s\n" % (
            #         str(tradeobj.buyOrderNo), self.exIds_to_inIds[secIdx][tradeobj.buyOrderNo], tradeobj.tradeSize,
            #         tradeobj.tradePrice))
            return tradeobj.buyOrderNo, 1

        elif tradeobj.sellOrderNo in list(self.exIds_to_inIds[secIdx].keys()):
            self.logger.debug("Order %s : Sell Order %d is filled with quantity %f of price %s\n" % (
                str(tradeobj.sellOrderNo), self.exIds_to_inIds[secIdx][tradeobj.sellOrderNo], tradeobj.tradeSize,
                tradeobj.tradePrice))
            self.logger.debug(
                f'         (remaining qty = {self.inIds_to_orders_confirmed[self.exIds_to_inIds[secIdx][tradeobj.sellOrderNo]]["remainingQty"]})')
            # with open(self.outputfile, "a") as myfile:
            #     myfile.write("Order %s : Sell Order %d is filled with quantity %d of price %s\n" % (
            #         str(tradeobj.sellOrderNo), self.exIds_to_inIds[secIdx][tradeobj.sellOrderNo], tradeobj.tradeSize,
            #         tradeobj.tradePrice))
            return tradeobj.sellOrderNo, -1

        else:
            return exId, 0

    def callback_for_trades(self, tradeobj):

        # update volume, TWAP, etc.
        volume = self.traded_volume[tradeobj.symbol] + tradeobj.tradeSize
        self.vwap[tradeobj.symbol] = (self.vwap[tradeobj.symbol] * self.traded_volume[tradeobj.symbol] +
                                      tradeobj.tradePrice * tradeobj.tradeSize)/volume
        self.traded_volume[tradeobj.symbol] = volume

        # update agent level values
        exId, side = self._condition_to_accept_trade(tradeobj)
        if side == -1 or side == 1:
            self._update_with_trade(tradeobj, side, exId)

            self._model_reaction_to_trades(tradeobj.symbol, exId)

    def _model_reaction_to_trades(self, symb, exId):
        pass

    def _update_with_ack(self, aMobj):

        # orderNo (external no assigned by market maker)
        inId = aMobj.internalOrderNo
        exId = aMobj.orderNo

        secIdx = self._get_sec_idx(aMobj.symb)
        # print("????????????")
        # print(aMobj)
        # print("????????????")
        # if exId in self.exIds_to_inIds and self.exIds_to_inIds[exId] in self.inIds_to_delete:
        #     print("!!!!!!!!!!!")
        #     print(aMobj)
        #     print("!!!!!!!!!!!")

        if aMobj.action == "A" and (inId in self.inIds_to_orders_sent):
            try:
                self.inIds_to_orders_confirmed[inId] = self.inIds_to_orders_sent.pop(inId)
                # if exId in self.exIds_to_inIds:
                #     print("!!!!!!!!!!?????????? DUPLICATE EXTERNAL ID !!!!!!!!!!??????????", exId)
                #     print("External ID:", exId)
                #     print("Internal ID:", self.exIds_to_inIds[exId])
                #     print("Change to:", inId)
                self.exIds_to_inIds[secIdx][exId] = inId
                self.inIds_to_exIds[inId] = exId
            except KeyError:
                self.logger.exception("Key {} not in dict, cannot be removed".format(inId))
            self.logger.debug("Order added: ExId: %s -> InId: %s" % (exId, inId))

        elif aMobj.action == "D" and (self.exIds_to_inIds[secIdx][exId] in self.inIds_to_orders_confirmed):
            try:
                inId = self.exIds_to_inIds[secIdx][exId]
                self.inIds_to_orders_sent[inId] = self.inIds_to_orders_confirmed.pop(inId)
                # print("~~~~~~~~~~~~~~~~~inId deleted", inId)
                # print("~~~~~~~~inIds_to_delete~~~~~~~~:", self.inIds_to_delete)
                # self.inIds_to_delete.pop(inId)
                # self.exIds_to_inIds[secIdx][exId] = inId
                # self.inIds_to_exIds[inId] = exId
            except KeyError:
                self.logger.exception("Key {} not in dict, cannot be removed".format(inId))

            self.logger.debug("Order deleted: ExId: %s -> InId: %s" % (exId, inId))

        elif aMobj.action == "M" and (self.exIds_to_inIds[secIdx][inId] in self.inIds_to_orders_confirmed):
            # in 'M' mode, inId = exId of old order; exId = exId of new order
            try:
                # update inId to exId mapping
                inId = self.exIds_to_inIds[secIdx][inId]
                self.exIds_to_inIds[secIdx][exId] = inId
                self.inIds_to_exIds[inId] = exId
            except KeyError:
                self.logger.exception("Key {} not in dict, cannot be removed".format(inId))

            self.logger.debug("Order modified: ExId: %s -> InId: %s" % (exId, inId))


        # elif aMobj.action == "F":
        #     inId = self.exIds_to_inIds[exId]
        #     try:
        #         self.inIds_to_delete.pop(inId)
        #     except KeyError:
        #         logging.exception("Key {} not in dict, cannot be removed".format(inId))

        # semaphore.release()
        # if len(self.inIds_to_delete) > 0:
        # print("----------")
        # print("Order Ex ID Ack:", exId)
        # print("Order In ID Ack:", self.exIds_to_inIds[exId])
        # print("inIds_to_delete:", self.inIds_to_delete)
        # print("Order", aMobj)
        # print("----------")

    def callback_for_acks(self, aMobj):
        if (aMobj.symb in self.securities) and (aMobj.strategy == self.strategy):
            self._update_with_ack(aMobj)

    def _update_trend(self, trend, symbol, lv, oldprice, newprice):
        if newprice > oldprice:
            trend[symbol][lv] = 1
        elif newprice < oldprice:
            trend[symbol][lv] = -1
        else:
            trend[symbol][lv] = 0

    def _update_market_dict(self, tob):
        sym = tob["symb"]

        # update all levels needed
        for lv in self.market_event_queue:
            if tob[lv + "AskPrice"] is not None and tob[lv + "BidPrice"] is not None:
                if len(self.market_dict[sym][lv]) > 0:
                    self._update_trend(self.bid_trend, sym, lv,
                                       oldprice=self.market_dict[sym][lv]["BidPrice"],
                                       newprice=tob[lv + "BidPrice"])
                    self._update_trend(self.ask_trend, sym, lv,
                                       oldprice=self.market_dict[sym][lv]["AskPrice"],
                                       newprice=tob[lv + "AskPrice"])
                self.market_dict[sym][lv] = {"AskPrice": tob[lv + "AskPrice"],
                                             "BidPrice": tob[lv + "BidPrice"],
                                             "AskSize": tob[lv + "AskSize"],
                                             "BidSize": tob[lv + "BidSize"]}
        # update the mid market
        self.mid_market[sym] = 0.5 * (self.market_dict[sym]["L1"]["AskPrice"] + self.market_dict[sym]["L1"]["BidPrice"])

    def _update_latest_level_data(self, tob):
        symb = tob["symb"]
        self.latest_level_data[symb] = tob

    def callback_for_levels(self, tob):
        # update the whole market
        self._update_market_dict(tob)
        self._update_latest_level_data(tob)

        # update the inventory
        if tob["symb"] in self.securities:
            self._update_inventory_value()
            self._update_net_worth()

            # action defined by bot
            self._model_reaction_to_level(tob)

    def _model_reaction_to_level(self, tob):
        pass

    def callback_for_clearing(self, obj):

        secIdx = self._get_sec_idx(obj.symb)

        if (obj.symb in self.securities) and (secIdx is not None):
            self.limits[secIdx]['TimeDelay'] = obj.TimeDelay
            self.limits[secIdx]['InventoryLimit'] = obj.InventoryLimit
            self.limits[secIdx]['OrdersOutStandingLimit'] = obj.OrdersOutStandingLimit
            self.limits[secIdx]['OrdersSendLimit'] = obj.OrdersSendLimit
            self.limits[secIdx]['ShutDown'] = obj.ShutDown

            self.logger.info(f'Order limits updated by Clearing Agent for symbol: {obj.symb} \n'
                          f'{self.limits[secIdx]}')
        else:
            self.logger.warning(f'Order limits updated by Clearing Agent for symbol: {obj.symb} failed!')

    def _init_inventory(self):
        inventory = dict()
        for sec in self.securities:
            inventory[sec] = 0.0
        return inventory

    def _init_market_dict(self):
        market_dict = dict()
        for sec in self.market_event_securities:
            sym_dict = dict()
            for e in self.market_event_queue:
                sym_dict[e] = {}
            market_dict[sec] = sym_dict
        return market_dict

    def _init_sec_prices(self):
        sec_state = dict()
        for sec in self.market_event_securities:
            sec_state.setdefault(sec, None)
        return sec_state

